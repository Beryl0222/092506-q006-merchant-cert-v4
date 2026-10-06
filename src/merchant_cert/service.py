"""认证档案应用服务。

服务层只做取数、鉴权、落待办和记事件；“某商户当前是什么状态”完全由
``domain.derive_status`` 按街区规则推导，因此对外查询、批量扫描、证据链回放
永远得到一致结论。

角色：
- ``public``    公众，只能看脱敏后的公开状态；
- ``owner``     商户本人，看自己的完整档案；
- ``reviewer``  评审，看分项材料与评审意见，处理材料评审和争议复核；
- ``inspector`` 抽查人员，记录抽查不合格并跟踪整改；
- ``operator``  街区运营方，暂停/恢复认证，查看全量档案与待办。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from .domain import (
    CATEGORIES,
    CATEGORY_LABELS,
    V_APPROVED,
    V_PENDING,
    V_REJECTED,
    V_SUPERSEDED,
    Dispute,
    Inspection,
    MaterialVersion,
    Merchant,
    RemediationTask,
    Review,
    Suspension,
    Todo,
    derive_status,
    event_hash,
    is_expired,
    now_iso,
    parse_iso,
    within_renew_window,
)
from .store import Store

ROLE_PUBLIC = "public"
ROLE_OWNER = "owner"
ROLE_REVIEWER = "reviewer"
ROLE_INSPECTOR = "inspector"
ROLE_OPERATOR = "operator"
INTERNAL_ROLES = {ROLE_OWNER, ROLE_REVIEWER, ROLE_INSPECTOR, ROLE_OPERATOR}


class AuthError(PermissionError):
    """角色或归属不满足操作要求。"""


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def iso_after(days: float, moment: str | None = None) -> str:
    base = parse_iso(moment) if moment else datetime.now(timezone.utc)
    return (base + timedelta(days=days)).isoformat()


def content_hash_of(content_ref: str) -> str:
    return hashlib.sha256(content_ref.encode("utf-8")).hexdigest()


class Service:
    def __init__(self, store: Store | None = None, *, auto_reconcile: bool = True) -> None:
        self.store = store or Store()
        # 重启即对账：库里记录在、待办丢了要补回；待办在、记录没了要作废。
        # 唯一索引保证不会重复生成。
        if auto_reconcile:
            self.reconcile_todos()

    # ---------- 基础 ----------

    def health(self) -> dict[str, str]:
        return {"service": "merchant_cert", "status": "ok"}

    def register(self, record_id: str, owner_id: str, name: str = "",
                 actor: dict | None = None, at: str | None = None) -> dict:
        moment = at or now_iso()
        merchant = self.store.save_merchant(
            Merchant(record_id, owner_id, name, moment))
        self.store.append_event(
            record_id, (actor or {}).get("id", owner_id), "merchant.registered",
            {"owner_id": owner_id, "name": name}, moment)
        return {"merchant_id": merchant.merchant_id, "owner_id": merchant.owner_id,
                "name": merchant.name, "state": "draft", "created_at": merchant.created_at}

    def find(self, record_id: str) -> dict | None:
        merchant = self.store.get_merchant(record_id)
        return merchant.__dict__.copy() if merchant else None

    def _require_merchant(self, merchant_id: str) -> Merchant:
        merchant = self.store.get_merchant(merchant_id)
        if merchant is None:
            raise KeyError(f"商户不存在: {merchant_id}")
        return merchant

    @staticmethod
    def _actor(actor: dict, roles: set[str]) -> dict:
        if not actor or "id" not in actor or "role" not in actor:
            raise AuthError("缺少操作人信息")
        if actor["role"] not in roles:
            raise AuthError(f"角色 {actor['role']} 无权执行该操作")
        return actor

    def _require_owner(self, merchant: Merchant, actor: dict) -> None:
        self._actor(actor, {ROLE_OWNER})
        if actor["id"] != merchant.owner_id:
            raise AuthError("只能操作本商户档案")

    def _bundle(self, merchant_id: str, moment: str | None = None) -> dict:
        moment = moment or now_iso()
        return {
            "merchant": self.store.get_merchant(merchant_id),
            "versions": self.store.list_versions(merchant_id),
            "inspections": self.store.list_inspections(merchant_id),
            "remediations": self.store.list_remediations(merchant_id),
            "suspensions": self.store.list_suspensions(merchant_id),
            "disputes": self.store.list_disputes(merchant_id),
            "moment": moment,
        }

    def snapshot(self, merchant_id: str, at: str | None = None) -> dict:
        merchant = self._require_merchant(merchant_id)
        bundle = self._bundle(merchant_id, at)
        return derive_status(merchant, bundle["versions"], bundle["inspections"],
                             bundle["remediations"], bundle["suspensions"],
                             bundle["disputes"], bundle["moment"])

    # ---------- 分项材料版本 ----------

    def upload_material(self, merchant_id: str, category: str, content_ref: str,
                        valid_until: str, actor: dict, *,
                        valid_from: str | None = None,
                        at: str | None = None) -> dict:
        merchant = self._require_merchant(merchant_id)
        self._require_owner(merchant, actor)
        if category not in CATEGORIES:
            raise ValueError(f"未知材料分项: {category}")
        moment = at or now_iso()
        digest = content_hash_of(content_ref)

        # 同一材料重复上传：命中既有版本，直接返回，不再生成评审待办。
        existing = self.store.find_version_by_hash(merchant_id, category, digest)
        if existing is not None:
            self.store.append_event(
                merchant_id, actor["id"], "material.upload_dedup_hit",
                {"category": category, "version_id": existing.version_id,
                 "content_hash": digest}, moment)
            return {"version_id": existing.version_id, "version": existing.version,
                    "category": category, "status": existing.status,
                    "duplicate": True, "todo_created": False}

        number = self.store.next_version_number(merchant_id, category)
        version_id = _uid("ver")
        version = MaterialVersion(
            version_id=version_id, merchant_id=merchant_id, category=category,
            version=number, content_ref=content_ref, content_hash=digest,
            uploaded_by=actor["id"], uploaded_at=moment,
            valid_from=valid_from or moment, valid_until=valid_until,
            status=V_PENDING)
        self.store.insert_version(version)
        self.store.append_event(
            merchant_id, actor["id"], "material.uploaded",
            {"category": category, "version": number, "version_id": version_id,
             "content_hash": digest, "valid_until": valid_until}, moment)

        # 每个待评审版本恰好一条评审待办，重复上传、重启对账都不会多出。
        _, created = self._open_todo(
            merchant_id, "material_review", f"review:{version_id}",
            f"评审{CATEGORY_LABELS[category]}材料第{number}版",
            ref_type="material_version", ref_id=version_id, at=moment)
        return {"version_id": version_id, "version": number, "category": category,
                "status": V_PENDING, "duplicate": False, "todo_created": created}

    def review_version(self, version_id: str, decision: str, comment: str,
                       actor: dict, *, at: str | None = None) -> dict:
        self._actor(actor, {ROLE_REVIEWER, ROLE_OPERATOR})
        if decision not in ("approve", "reject"):
            raise ValueError("评审结论只能是 approve 或 reject")
        version = self.store.get_version(version_id)
        if version is None:
            raise KeyError(f"材料版本不存在: {version_id}")
        if version.status != V_PENDING:
            raise ValueError(f"版本已审结，当前状态: {version.status}")
        moment = at or now_iso()

        review = Review(_uid("rev"), version_id, version.merchant_id,
                        actor["id"], decision, comment, moment)
        self.store.insert_review(review)
        if decision == "approve":
            self.store.approve_version(version_id, moment)
            superseded = self.store.supersede_versions(
                version.merchant_id, version.category, version_id, moment)
            # 新合规版本生效：旧版本的续期待办作废，按新版本有效期重新评估。
            for old_id in superseded:
                self.store.cancel_todo_by_key(
                    f"renewal:{version.merchant_id}:{version.category}:{old_id}", moment)
            self._ensure_renewal_todo(
                self.store.get_version(version_id), at=moment)
            payload_action = "material.approved"
        else:
            self.store.reject_version(version_id)
            payload_action = "material.rejected"
        self.store.complete_todo_by_key(f"review:{version_id}", moment)
        self.store.append_event(
            version.merchant_id, actor["id"], payload_action,
            {"version_id": version_id, "category": version.category,
             "comment": comment, "review_id": review.review_id}, moment)
        return {"version_id": version_id, "decision": decision,
                "status": "approved" if decision == "approve" else V_REJECTED,
                "review_id": review.review_id}

    # ---------- 抽查、整改与复核 ----------

    def record_inspection(self, merchant_id: str, result: str, actor: dict, *,
                          category: str | None = None, comment: str = "",
                          inspection_id: str | None = None,
                          at: str | None = None) -> dict:
        merchant = self._require_merchant(merchant_id)
        self._actor(actor, {ROLE_INSPECTOR, ROLE_OPERATOR})
        if result not in ("pass", "fail"):
            raise ValueError("抽查结果只能是 pass 或 fail")
        if category is not None and category not in CATEGORIES:
            raise ValueError(f"未知材料分项: {category}")
        moment = at or now_iso()
        inspection_id = inspection_id or _uid("insp")

        # 同一抽查单重复提交不能生成第二条整改待办。
        existing = self.store.get_inspection(inspection_id)
        if existing is not None:
            return {"inspection_id": inspection_id, "duplicate": True,
                    "todo_created": False, "result": existing.result}

        inspection = Inspection(inspection_id, merchant_id, actor["id"], category,
                                result, comment, moment)
        self.store.insert_inspection(inspection)
        self.store.append_event(
            merchant_id, actor["id"], "inspection.recorded",
            {"inspection_id": inspection_id, "result": result,
             "category": category, "comment": comment}, moment)

        todo_created = False
        task_id = None
        if result == "fail":
            # remediation_tasks 对 inspection_id 唯一，待办 dedup_key 同样唯一。
            task = RemediationTask(
                _uid("task"), merchant_id, inspection_id, category, "open",
                moment, due_at=iso_after(15, moment))
            self.store.insert_remediation(task)
            _, todo_created = self._open_todo(
                merchant_id, "remediation", f"remediation:{inspection_id}",
                f"抽查不合格整改" + (f"（{CATEGORY_LABELS[category]}）" if category else "（综合）"),
                ref_type="inspection", ref_id=inspection_id, at=moment)
            task_id = task.task_id
        return {"inspection_id": inspection_id, "result": result,
                "duplicate": False, "todo_created": todo_created,
                "remediation_task_id": task_id}

    def resolve_remediation(self, inspection_id: str, version_id: str | None,
                            actor: dict, *, comment: str = "",
                            at: str | None = None) -> dict:
        """整改复核：只有凭当前合规版本才能通过并恢复状态。"""
        self._actor(actor, {ROLE_INSPECTOR, ROLE_REVIEWER, ROLE_OPERATOR})
        moment = at or now_iso()
        task = self.store.get_remediation_by_inspection(inspection_id)
        if task is None:
            raise KeyError(f"抽查单没有整改任务: {inspection_id}")
        if task.status != "open":
            raise ValueError(f"整改任务已完结: {task.status}")

        # 校验恢复依据：失败分项（综合抽查=所有分项）必须各有有效期内的通过版本。
        required = [task.category] if task.category else list(CATEGORIES)
        versions = self.store.list_versions(task.merchant_id)
        compliant_ids: set[str] = set()
        from .domain import derive_category_state
        for category in required:
            cat_state = derive_category_state(versions, category, moment)
            if cat_state["state"] != "compliant":
                raise ValueError(
                    f"整改复核未通过：{CATEGORY_LABELS[category]}没有有效期内的合规版本，"
                    "不能恢复统一标识")
            compliant_ids.add(cat_state["version"].version_id)
        if version_id is not None and version_id not in compliant_ids:
            raise ValueError("指定的恢复依据不是当前合规版本，整改只能从合规版本恢复")
        resolution_id = version_id or next(iter(compliant_ids))

        self.store.resolve_remediation(task.task_id, moment, resolution_id)
        self.store.complete_todo_by_key(f"remediation:{inspection_id}", moment)
        self.store.append_event(
            task.merchant_id, actor["id"], "remediation.resolved",
            {"inspection_id": inspection_id, "task_id": task.task_id,
             "resolution_version_id": resolution_id, "comment": comment}, moment)

        snapshot = self.snapshot(task.merchant_id, moment)
        return {"task_id": task.task_id, "resolution_version_id": resolution_id,
                "status_after": snapshot["status"],
                "mark_allowed": snapshot["mark_allowed"]}

    # ---------- 暂停 / 恢复 ----------

    def suspend(self, merchant_id: str, reason: str, actor: dict, *,
                suspension_id: str | None = None, at: str | None = None) -> dict:
        merchant = self._require_merchant(merchant_id)
        self._actor(actor, {ROLE_OPERATOR})
        moment = at or now_iso()
        snapshot = self.snapshot(merchant_id, moment)
        if snapshot["status"] == "suspended":
            return {"suspension_id": None, "duplicate": True, "status": "suspended"}
        suspension_id = suspension_id or _uid("susp")
        item = Suspension(suspension_id, merchant_id, reason, actor["id"], moment)
        self.store.insert_suspension(item)
        self.store.append_event(
            merchant_id, actor["id"], "certification.suspended",
            {"suspension_id": suspension_id, "reason": reason}, moment)
        # 公开查询实时推导，暂停在下一次查询立即生效；历史凭证全部保留。
        return {"suspension_id": suspension_id, "duplicate": False,
                "status": self.snapshot(merchant_id, moment)["status"]}

    def resume(self, suspension_id: str, actor: dict, *, at: str | None = None) -> dict:
        self._actor(actor, {ROLE_OPERATOR})
        moment = at or now_iso()
        # 暂停记录只追加、不删除：这里只登记解除时间，凭证链完整保留。
        rows = self.store.connection.execute(
            "SELECT merchant_id FROM suspensions WHERE suspension_id=?",
            (suspension_id,)).fetchone()
        if rows is None:
            raise KeyError(f"暂停记录不存在: {suspension_id}")
        merchant_id = rows[0]
        self.store.resolve_suspension(suspension_id, moment)
        self.store.append_event(
            merchant_id, actor["id"], "certification.resumed",
            {"suspension_id": suspension_id}, moment)
        # 恢复后是什么状态由规则重算：整改未结、材料过期都不能借恢复洗白。
        snapshot = self.snapshot(merchant_id, moment)
        return {"suspension_id": suspension_id, "status_after": snapshot["status"],
                "mark_allowed": snapshot["mark_allowed"]}

    # ---------- 争议复核 ----------

    def raise_dispute(self, merchant_id: str, ref_type: str, ref_id: str,
                      reason: str, actor: dict, *, at: str | None = None) -> dict:
        merchant = self._require_merchant(merchant_id)
        self._require_owner(merchant, actor)
        if ref_type != "inspection":
            raise ValueError("当前仅支持对抽查结果提出争议")
        target = self.store.get_inspection(ref_id)
        if target is None or target.merchant_id != merchant_id:
            raise KeyError("争议指向的抽查记录不存在")
        moment = at or now_iso()

        # 同一抽查单重复提争议不生成第二条复核待办。
        for old in self.store.list_disputes(merchant_id):
            if old.ref_type == ref_type and old.ref_id == ref_id and old.status == "open":
                return {"dispute_id": old.dispute_id, "duplicate": True,
                        "todo_created": False}
        dispute = Dispute(_uid("disp"), merchant_id, ref_type, ref_id,
                          actor["id"], reason, "open", moment)
        self.store.insert_dispute(dispute)
        self.store.append_event(
            merchant_id, actor["id"], "dispute.raised",
            {"dispute_id": dispute.dispute_id, "ref_id": ref_id, "reason": reason},
            moment)
        _, todo_created = self._open_todo(
            merchant_id, "dispute_review", f"dispute_review:{dispute.dispute_id}",
            "抽查争议复核", ref_type="dispute", ref_id=dispute.dispute_id, at=moment)
        return {"dispute_id": dispute.dispute_id, "duplicate": False,
                "todo_created": todo_created}

    def resolve_dispute(self, dispute_id: str, upheld: bool, actor: dict, *,
                        comment: str = "", at: str | None = None) -> dict:
        self._actor(actor, {ROLE_REVIEWER, ROLE_OPERATOR})
        dispute = self.store.get_dispute(dispute_id)
        if dispute is None:
            raise KeyError(f"争议不存在: {dispute_id}")
        if dispute.status != "open":
            raise ValueError(f"争议已完结: {dispute.status}")
        moment = at or now_iso()
        new_status = "upheld" if upheld else "rejected"
        self.store.resolve_dispute(dispute_id, new_status, actor["id"], comment, moment)
        self.store.complete_todo_by_key(f"dispute_review:{dispute_id}", moment)

        inspection_voided = False
        if upheld:
            # 争议成立：抽查结果作废、整改任务与待办撤销；状态交回规则推导，
            # 材料仍过期就仍然是过期，不会因为撤销抽查而自动合规。
            self.store.void_inspection(dispute.ref_id, moment)
            task = self.store.get_remediation_by_inspection(dispute.ref_id)
            if task is not None and task.status == "open":
                self.store.cancel_remediation(task.task_id, moment)
                self.store.cancel_todo_by_key(f"remediation:{dispute.ref_id}", moment)
            inspection_voided = True
        self.store.append_event(
            dispute.merchant_id, actor["id"], "dispute.resolved",
            {"dispute_id": dispute_id, "decision": new_status,
             "inspection_voided": inspection_voided, "comment": comment}, moment)
        snapshot = self.snapshot(dispute.merchant_id, moment)
        return {"dispute_id": dispute_id, "decision": new_status,
                "inspection_voided": inspection_voided,
                "status_after": snapshot["status"],
                "mark_allowed": snapshot["mark_allowed"]}

    # ---------- 到期批量扫描 ----------

    def _ensure_renewal_todo(self, version: MaterialVersion,
                             at: str | None = None) -> bool:
        moment = at or now_iso()
        if version.status != V_APPROVED:
            return False
        if is_expired(version.valid_until, moment):
            reason = f"{CATEGORY_LABELS[version.category]}材料已过期，请立即续证"
        elif within_renew_window(version.valid_until, moment):
            days = (parse_iso(version.valid_until) - parse_iso(moment)).days
            reason = f"{CATEGORY_LABELS[version.category]}材料将于{days}天内到期"
        else:
            return False
        _, created = self._open_todo(
            version.merchant_id, "renewal",
            f"renewal:{version.merchant_id}:{version.category}:{version.version_id}",
            reason, ref_type="material_version", ref_id=version.version_id, at=moment)
        return created

    def scan_expirations(self, *, at: str | None = None) -> dict:
        """全街区批量扫描：为进入续证窗口或已过期的版本补续期待办。

        待办按版本去重，扫描任意次数结果幂等。
        """
        moment = at or now_iso()
        opened = 0
        affected: list[str] = []
        for merchant_id in self.store.list_merchant_ids():
            for version in self.store.list_versions(merchant_id):
                if self._ensure_renewal_todo(version, moment):
                    opened += 1
                    affected.append(merchant_id)
            # 扫描同时给出受影响商户的实时状态，过期、整改、暂停都不能用标识。
        not_markable = [
            mid for mid in self.store.list_merchant_ids()
            if not self.snapshot(mid, moment)["mark_allowed"]
        ]
        return {"scanned_at": moment, "renewal_todos_opened": opened,
                "merchants_with_due_renewal": sorted(set(affected)),
                "merchants_not_markable": sorted(set(not_markable))}

    # ---------- 待办与重启幂等 ----------

    def _open_todo(self, merchant_id: str, kind: str, dedup_key: str,
                   reason: str, *, ref_type: str = "", ref_id: str = "",
                   at: str | None = None) -> tuple[Todo, bool]:
        todo = Todo(_uid("todo"), merchant_id, kind, dedup_key, reason, "open",
                    at or now_iso(), ref_type, ref_id)
        return self.store.add_todo(todo)

    def reconcile_todos(self, *, at: str | None = None) -> dict:
        """按档案现状重建待办集合：缺的补回、多的作废、已有的不动。

        服务启动时自动执行一次，保证重启后待办与业务记录一致且不重复。
        """
        moment = at or now_iso()
        opened = 0
        cancelled = 0
        for merchant_id in self.store.list_merchant_ids():
            for version in self.store.list_versions(merchant_id):
                review_key = f"review:{version.version_id}"
                renewal_key = (f"renewal:{merchant_id}:{version.category}:"
                               f"{version.version_id}")
                if version.status == V_PENDING:
                    if self._open_todo(
                            merchant_id, "material_review", review_key,
                            f"待评审：{CATEGORY_LABELS[version.category]}第{version.version}版",
                            ref_type="material_version", ref_id=version.version_id,
                            at=moment)[1]:
                        opened += 1
                else:
                    if self.store.cancel_todo_by_key(review_key, moment):
                        cancelled += 1
                if version.status == V_APPROVED:
                    if self._ensure_renewal_todo(version, moment):
                        opened += 1
                elif version.status in (V_SUPERSEDED, V_REJECTED):
                    if self.store.cancel_todo_by_key(renewal_key, moment):
                        cancelled += 1

            for task in self.store.list_remediations(merchant_id):
                if task.status == "open":
                    if self._open_todo(
                            merchant_id, "remediation",
                            f"remediation:{task.inspection_id}",
                            "待整改复核", ref_type="inspection",
                            ref_id=task.inspection_id, at=moment)[1]:
                        opened += 1
                else:
                    if self.store.cancel_todo_by_key(
                            f"remediation:{task.inspection_id}", moment):
                        cancelled += 1

            for dispute in self.store.list_disputes(merchant_id):
                if dispute.status == "open":
                    if self._open_todo(
                            merchant_id, "dispute_review",
                            f"dispute_review:{dispute.dispute_id}",
                            "待争议复核", ref_type="dispute",
                            ref_id=dispute.dispute_id, at=moment)[1]:
                        opened += 1
                else:
                    if self.store.cancel_todo_by_key(
                            f"dispute_review:{dispute.dispute_id}", moment):
                        cancelled += 1
        return {"reconciled_at": moment, "opened": opened, "cancelled": cancelled}

    def list_todos(self, actor: dict, *, merchant_id: str | None = None,
                   status: str = "open") -> list[dict]:
        role = self._actor(actor, INTERNAL_ROLES)["role"]
        if role == ROLE_OWNER:
            merchant = self._require_merchant(merchant_id or "")
            if actor["id"] != merchant.owner_id:
                raise AuthError("只能查看本商户待办")
            scope = merchant_id
        elif role == ROLE_REVIEWER:
            scope = merchant_id
        else:
            scope = merchant_id
        todos = self.store.list_todos(scope, status)
        if role == ROLE_REVIEWER:
            todos = [t for t in todos if t.kind in ("material_review", "dispute_review")]
        elif role == ROLE_INSPECTOR:
            todos = [t for t in todos if t.kind == "remediation"]
        return [t.__dict__.copy() for t in todos]

    # ---------- 对外查询与角色可见范围 ----------

    def public_status(self, merchant_id: str, *, at: str | None = None) -> dict:
        """公众视角：只给结论与分项是否合规，不暴露材料内容、评审和争议细节。"""
        snapshot = self.snapshot(merchant_id, at)
        return {
            "merchant_id": snapshot["merchant_id"],
            "name": snapshot["name"],
            "status": snapshot["status"],
            "status_label": snapshot["status_label"],
            "mark_allowed": snapshot["mark_allowed"],
            "cert_valid_until": snapshot["cert_valid_until"],
            "categories": {
                c: {"label": CATEGORY_LABELS[c],
                    "compliant": snapshot["categories"][c] == "compliant"}
                for c in CATEGORIES
            },
            "as_of": snapshot["as_of"],
        }

    def dossier(self, merchant_id: str, actor: dict, *, at: str | None = None) -> dict:
        """按角色裁剪的完整档案。"""
        merchant = self._require_merchant(merchant_id)
        role = actor.get("role") if actor else ROLE_PUBLIC
        if role == ROLE_PUBLIC or not actor:
            return self.public_status(merchant_id, at=at)
        if role == ROLE_OWNER and actor.get("id") != merchant.owner_id:
            raise AuthError("只能查看本商户档案")
        if role not in INTERNAL_ROLES:
            raise AuthError(f"未知角色: {role}")

        bundle = self._bundle(merchant_id, at)
        snapshot = derive_status(merchant, bundle["versions"], bundle["inspections"],
                                 bundle["remediations"], bundle["suspensions"],
                                 bundle["disputes"], bundle["moment"])

        def version_view(v: MaterialVersion) -> dict:
            data = v.__dict__.copy()
            data["category_label"] = CATEGORY_LABELS[v.category]
            return data

        visible_versions = list(bundle["versions"])
        versions_view = []
        for v in visible_versions:
            view = version_view(v)
            if role == ROLE_INSPECTOR:
                view.pop("content_ref", None)
                view.pop("content_hash", None)
            versions_view.append(view)

        result = {
            "merchant_id": merchant.merchant_id,
            "name": merchant.name,
            "owner_id": merchant.owner_id,
            "role": role,
            "snapshot": _snapshot_view(snapshot),
            "versions": versions_view,
            "as_of": bundle["moment"],
        }
        if role == ROLE_INSPECTOR:
            result.update({
                "inspections": [i.__dict__.copy() for i in bundle["inspections"]],
                "remediations": [t.__dict__.copy() for t in bundle["remediations"]],
            })
            return result

        result["reviews"] = [r.__dict__.copy() for r in self.store.list_reviews(merchant_id)]
        if role in (ROLE_OWNER, ROLE_REVIEWER, ROLE_OPERATOR):
            result["disputes"] = [d.__dict__.copy() for d in bundle["disputes"]]
        if role in (ROLE_OWNER, ROLE_OPERATOR):
            result["inspections"] = [i.__dict__.copy() for i in bundle["inspections"]]
            result["remediations"] = [t.__dict__.copy() for t in bundle["remediations"]]
            result["suspensions"] = [s.__dict__.copy() for s in bundle["suspensions"]]
            result["todos"] = [t.__dict__.copy()
                               for t in self.store.list_todos(merchant_id)]
        return result

    # ---------- 证据链 ----------

    def evidence_chain(self, merchant_id: str, actor: dict | None = None, *,
                       at: str | None = None) -> dict:
        """给出某次公开状态对应的完整证据链。

        公众可查（证据链是公开状态的解释），但只回放在该时刻生效的结论性凭证；
        内部评论仅对内部角色开放。链尾哈希可独立校验事件未被篡改。
        """
        merchant = self._require_merchant(merchant_id)
        role = (actor or {}).get("role", ROLE_PUBLIC)
        if role not in INTERNAL_ROLES | {ROLE_PUBLIC}:
            raise AuthError(f"未知角色: {role}")
        moment = at or now_iso()
        bundle = self._bundle(merchant_id, moment)
        snapshot = derive_status(merchant, bundle["versions"], bundle["inspections"],
                                 bundle["remediations"], bundle["suspensions"],
                                 bundle["disputes"], moment)

        reviews = self.store.list_reviews(merchant_id)
        category_evidence: dict[str, dict] = {}
        for category in CATEGORIES:
            state_name = snapshot["categories"][category]
            from .domain import derive_category_state
            cat = derive_category_state(bundle["versions"], category, moment)
            v = cat["version"]
            evidence = {"state": state_name, "category_label": CATEGORY_LABELS[category]}
            if v is not None:
                evidence["version"] = {
                    "version_id": v.version_id, "version": v.version,
                    "content_hash": v.content_hash,
                    "uploaded_at": v.uploaded_at,
                    "valid_from": v.valid_from, "valid_until": v.valid_until,
                    "status": v.status,
                }
                related = [r for r in reviews if r.version_id == v.version_id]
                if related:
                    r = related[-1]
                    evidence["review"] = {
                        "review_id": r.review_id, "decision": r.decision,
                        "reviewer_id": r.reviewer_id, "created_at": r.created_at,
                    }
                    if role in INTERNAL_ROLES:
                        evidence["review"]["comment"] = r.comment
            category_evidence[category] = evidence

        active = [t for t in bundle["remediations"]
                  if t.created_at <= moment
                  and (t.status == "open"
                       or (t.resolved_at is not None and t.resolved_at > moment))]
        chain = {
            "merchant_id": merchant_id,
            "as_of": moment,
            "public_status": self.public_status(merchant_id, at=moment),
            "category_evidence": category_evidence,
            "active_remediations": [
                {"task_id": t.task_id, "inspection_id": t.inspection_id,
                 "category": t.category, "created_at": t.created_at,
                 "resolution_version_id": t.resolution_version_id}
                for t in active],
            "active_suspension": (
                snapshot["suspension"].__dict__.copy()
                if snapshot["suspension"] else None),
        }
        if role in INTERNAL_ROLES:
            chain["disputes"] = [d.__dict__.copy() for d in bundle["disputes"]]
            chain["inspections"] = [i.__dict__.copy() for i in bundle["inspections"]]

        events = self.store.list_events(merchant_id)
        prefix = [e for e in events if e.created_at <= moment]
        chain_valid, bad_seq = _verify_events(prefix)
        chain["events"] = [
            {"seq": e.seq, "actor_id": e.actor_id, "action": e.action,
             "payload": e.payload, "created_at": e.created_at,
             "entry_hash": e.entry_hash}
            for e in prefix
        ]
        chain["chain_valid"] = chain_valid
        chain["chain_tip"] = prefix[-1].entry_hash if prefix else None
        chain["chain_first_bad_seq"] = bad_seq
        return chain


def _snapshot_view(snapshot: dict) -> dict:
    view = {k: v for k, v in snapshot.items()
            if k not in ("suspension", "open_remediations")}
    view["suspension_id"] = snapshot["suspension"].suspension_id if snapshot["suspension"] else None
    view["open_remediation_ids"] = [t.task_id for t in snapshot["open_remediations"]]
    return view


def _verify_events(events) -> tuple[bool, int | None]:
    prev = "0" * 64
    for e in events:
        digest = event_hash(e.seq, e.merchant_id, e.actor_id, e.action,
                            e.payload, e.created_at, prev)
        if digest != e.entry_hash or e.prev_hash != prev:
            return False, e.seq
        prev = e.entry_hash
    return True, None
