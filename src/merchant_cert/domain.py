"""商户材料与认证状态的领域对象与街区规则。

状态规则集中在 ``derive_status`` 一个纯函数里，服务层只负责取数和落库，
这样公开查询、证据链回放、批量扫描看到的是同一套规则。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# 街区要求的三类分项材料：无障碍、价格公示、安全服务
CATEGORIES = ("accessibility", "price_disclosure", "safety_service")
CATEGORY_LABELS = {
    "accessibility": "无障碍",
    "price_disclosure": "价格公示",
    "safety_service": "安全服务",
}

# 公开状态码
STATUS_LABELS = {
    "draft": "未建档完成",
    "incomplete": "材料不齐",
    "pending": "评审中",
    "certified": "认证有效",
    "expiring_soon": "临近到期",
    "expired": "证书过期",
    "rectifying": "整改中",
    "suspended": "认证暂停",
}

# 到期前多少天开始生成续证待办
RENEW_WINDOW_DAYS = 30

# 材料版本状态
V_PENDING = "pending"
V_APPROVED = "approved"
V_REJECTED = "rejected"
V_SUPERSEDED = "superseded"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def within_renew_window(valid_until: str, moment: str,
                        window_days: int = RENEW_WINDOW_DAYS) -> bool:
    """材料是否已进入到期前的续证窗口（含已过期）。"""
    deadline = parse_iso(moment) + timedelta(days=window_days)
    return parse_iso(valid_until) <= deadline


def is_expired(valid_until: str, moment: str) -> bool:
    return parse_iso(valid_until) <= parse_iso(moment)


@dataclass(frozen=True)
class Merchant:
    merchant_id: str
    owner_id: str
    name: str = ""
    created_at: str = ""


# 旧骨架使用 Record 命名，保留别名以兼容既有入口。
Record = Merchant


@dataclass(frozen=True)
class MaterialVersion:
    version_id: str
    merchant_id: str
    category: str
    version: int
    content_ref: str
    content_hash: str
    uploaded_by: str
    uploaded_at: str
    valid_from: str
    valid_until: str
    status: str = V_PENDING
    approved_at: str | None = None
    superseded_at: str | None = None


@dataclass(frozen=True)
class Review:
    review_id: str
    version_id: str
    merchant_id: str
    reviewer_id: str
    decision: str  # approve / reject
    comment: str
    created_at: str


@dataclass(frozen=True)
class Inspection:
    inspection_id: str
    merchant_id: str
    inspector_id: str
    category: str | None  # None 表示覆盖全分项的综合抽查
    result: str  # pass / fail
    comment: str
    created_at: str
    voided_at: str | None = None


@dataclass(frozen=True)
class RemediationTask:
    task_id: str
    merchant_id: str
    inspection_id: str
    category: str | None
    status: str  # open / resolved / cancelled
    created_at: str
    due_at: str = ""
    resolved_at: str | None = None
    resolution_version_id: str | None = None


@dataclass(frozen=True)
class Suspension:
    suspension_id: str
    merchant_id: str
    reason: str
    operator_id: str
    suspended_at: str
    resolved_at: str | None = None


@dataclass(frozen=True)
class Dispute:
    dispute_id: str
    merchant_id: str
    ref_type: str  # 当前支持 inspection
    ref_id: str
    raised_by: str
    reason: str
    status: str  # open / upheld / rejected
    created_at: str
    resolved_at: str | None = None
    reviewer_id: str = ""
    resolution_comment: str = ""


@dataclass(frozen=True)
class Todo:
    todo_id: str
    merchant_id: str
    kind: str  # material_review / renewal / remediation / dispute_review
    dedup_key: str
    reason: str
    status: str  # open / done / cancelled
    created_at: str
    ref_type: str = ""
    ref_id: str = ""
    completed_at: str | None = None


@dataclass(frozen=True)
class Event:
    seq: int
    merchant_id: str
    actor_id: str
    action: str
    payload: dict
    created_at: str
    prev_hash: str
    entry_hash: str


def event_hash(seq: int, merchant_id: str, actor_id: str, action: str,
               payload: dict, created_at: str, prev_hash: str) -> str:
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256()
    digest.update(prev_hash.encode("utf-8"))
    digest.update(f"{seq}|{merchant_id}|{actor_id}|{action}|{created_at}|".encode("utf-8"))
    digest.update(body.encode("utf-8"))
    return digest.hexdigest()


GENESIS_HASH = "0" * 64


def _effective_approved(versions: list[MaterialVersion], moment: str,
                        category: str) -> MaterialVersion | None:
    """某分项在指定时刻仍然生效的已通过版本。"""
    candidates = [
        v for v in versions
        if v.category == category
        and v.status == V_APPROVED
        and (v.approved_at or "") <= moment
        and (v.superseded_at is None or v.superseded_at > moment)
        and v.uploaded_at <= moment
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda v: v.approved_at or "")


def derive_category_state(versions: list[MaterialVersion], category: str,
                          moment: str) -> dict:
    effective = _effective_approved(versions, moment, category)
    if effective is not None and not is_expired(effective.valid_until, moment):
        return {"category": category, "state": "compliant", "version": effective}

    visible = [v for v in versions if v.category == category and v.uploaded_at <= moment]
    if not visible:
        return {"category": category, "state": "missing", "version": None}

    newest = max(visible, key=lambda v: (v.version, v.uploaded_at))
    if newest.status == V_PENDING:
        return {"category": category, "state": "pending", "version": newest}
    # 历史通过版本（含被替代或已过期）视为过期断档。
    ever_approved = any(v.status in (V_APPROVED, V_SUPERSEDED) for v in visible)
    if ever_approved or newest.status == V_APPROVED:
        return {"category": category, "state": "expired", "version": newest}
    return {"category": category, "state": "rejected", "version": newest}


def derive_status(merchant: Merchant,
                  versions: list[MaterialVersion],
                  inspections: list[Inspection] | None = None,
                  remediations: list[RemediationTask] | None = None,
                  suspensions: list[Suspension] | None = None,
                  disputes: list[Dispute] | None = None,
                  moment: str | None = None) -> dict:
    """按街区规则计算商户在某一时刻的认证状态。

    优先级：暂停 > 整改中 > 过期 > 评审中/材料不齐 > 认证有效。
    只有三类分项全部持有效内的通过版本，且没有未结整改时，才允许使用统一标识。
    """
    moment = moment or now_iso()
    inspections = inspections or []
    remediations = remediations or []
    suspensions = suspensions or []
    disputes = disputes or []

    category_states = {
        c: derive_category_state(versions, c, moment) for c in CATEGORIES
    }

    active_suspension = next(
        (s for s in suspensions
         if s.suspended_at <= moment and (s.resolved_at is None or s.resolved_at > moment)),
        None,
    )
    open_tasks = [
        t for t in remediations
        if t.created_at <= moment
        and (t.status == "open"
             or (t.resolved_at is not None and t.resolved_at > moment))
    ]
    active_inspection_ids = {t.inspection_id for t in open_tasks}
    active_failures = [
        i for i in inspections
        if i.inspection_id in active_inspection_ids
        and i.result == "fail"
        and (i.voided_at is None or i.voided_at > moment)
    ]
    open_disputes = [
        d for d in disputes
        if d.created_at <= moment
        and (d.status == "open"
             or (d.resolved_at is not None and d.resolved_at > moment))
    ]

    ever_certified = all(
        any(v.category == c and v.status in (V_APPROVED, V_SUPERSEDED)
            and v.uploaded_at <= moment
            for v in versions)
        for c in CATEGORIES
    )
    states = {c: category_states[c]["state"] for c in CATEGORIES}
    all_compliant = all(s == "compliant" for s in states.values())

    reasons: list[str] = []
    if active_suspension:
        status = "suspended"
        reasons.append("suspended")
    elif active_failures:
        status = "rectifying"
        for ins in active_failures:
            reasons.append(f"rectifying:{ins.category or 'all'}")
    elif all_compliant:
        status = "certified"
        soonest = min(
            category_states[c]["version"].valid_until
            for c in CATEGORIES
        )
        if within_renew_window(soonest, moment):
            reasons.append("expiring_soon")
    elif any(s == "expired" for s in states.values()) and ever_certified:
        status = "expired"
        reasons.extend(f"expired:{c}" for c in CATEGORIES if states[c] == "expired")
    elif any(s == "pending" for s in states.values()):
        status = "pending"
        reasons.extend(f"pending:{c}" for c in CATEGORIES if states[c] == "pending")
    else:
        status = "incomplete" if versions else "draft"
        reasons.extend(f"missing:{c}" for c in CATEGORIES if states[c] == "missing")
        reasons.extend(f"rejected:{c}" for c in CATEGORIES if states[c] == "rejected")

    effective_versions = {
        c: category_states[c]["version"]
        for c in CATEGORIES
        if category_states[c]["state"] == "compliant"
    }
    cert_valid_until = (
        min(v.valid_until for v in effective_versions.values())
        if all_compliant and effective_versions else None
    )

    return {
        "merchant_id": merchant.merchant_id,
        "name": merchant.name,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "reasons": reasons,
        "categories": states,
        "category_labels": CATEGORY_LABELS,
        "mark_allowed": status == "certified",
        "cert_valid_until": cert_valid_until,
        "suspension": active_suspension,
        "open_remediations": open_tasks,
        "dispute_open": bool(open_disputes),
        "as_of": moment,
    }
