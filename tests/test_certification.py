"""认证档案服务的验收测试。

覆盖：角色可见范围、到期批量扫描、争议复核、服务重启后的待办幂等性、
证据链、暂停即时生效与历史保留、整改只能从合规版本恢复。
"""
import json
import os
import tempfile
import unittest

from merchant_cert.api import handle
from merchant_cert.domain import CATEGORIES
from merchant_cert.service import (
    AuthError,
    Service,
    iso_after,
)
from merchant_cert.store import Store

T0 = "2026-01-01T00:00:00+00:00"
OWNER = {"id": "owner-1", "role": "owner"}
OWNER_OTHER = {"id": "owner-9", "role": "owner"}
REVIEWER = {"id": "rev-1", "role": "reviewer"}
INSPECTOR = {"id": "insp-1", "role": "inspector"}
OPERATOR = {"id": "op-1", "role": "operator"}


def build_certified_service(merchant_id="m-1", owner_id="owner-1",
                            days=365, moment=T0):
    """构造一个三类材料全部通过的已认证商户。"""
    service = Service(Store())
    owner = {"id": owner_id, "role": "owner"}
    service.register(merchant_id, owner_id, "示范店", OPERATOR, at=moment)
    valid_until = iso_after(days, moment)
    version_ids = []
    for category in CATEGORIES:
        result = service.upload_material(
            merchant_id, category, f"{category}-content-v1",
            valid_until, owner, at=moment)
        service.review_version(result["version_id"], "approve", "符合要求",
                               REVIEWER, at=moment)
        version_ids.append(result["version_id"])
    return service, owner, version_ids


class 状态规则测试(unittest.TestCase):
    def test_完整认证生命周期(self):
        service = Service(Store())
        service.register("m-1", "owner-1", "店", OPERATOR)
        self.assertEqual(service.snapshot("m-1", at=T0)["status"], "draft")

        first = service.upload_material(
            "m-1", "accessibility", "acc", iso_after(365, T0), OWNER, at=T0)
        self.assertEqual(service.snapshot("m-1", at=T0)["status"], "pending")

        service.review_version(first["version_id"], "reject", "不合规",
                               REVIEWER, at=T0)
        self.assertEqual(service.snapshot("m-1", at=T0)["status"], "incomplete")

        # 重新上传并逐项通过：缺两项 -> 缺一项 -> 认证有效
        accepted = service.upload_material(
            "m-1", "accessibility", "acc-v2", iso_after(365, T0), OWNER, at=T0)
        service.review_version(accepted["version_id"], "approve", "ok",
                               REVIEWER, at=T0)
        self.assertEqual(service.snapshot("m-1", at=T0)["status"], "incomplete")
        for category in ("price_disclosure", "safety_service"):
            r = service.upload_material(
                "m-1", category, category, iso_after(365, T0), OWNER, at=T0)
            service.review_version(r["version_id"], "approve", "ok",
                                   REVIEWER, at=T0)
        snapshot = service.snapshot("m-1", at=T0)
        self.assertEqual(snapshot["status"], "certified")
        self.assertTrue(snapshot["mark_allowed"])

        # 到期前 30 天：仍认证有效，但带临近到期提示
        soon = service.snapshot("m-1", at=iso_after(336, T0))
        self.assertEqual(soon["status"], "certified")
        self.assertIn("expiring_soon", soon["reasons"])

        # 跨过有效期：立即不可用标识，无需后台任务
        expired = service.snapshot("m-1", at=iso_after(366, T0))
        self.assertEqual(expired["status"], "expired")
        self.assertFalse(expired["mark_allowed"])

    def test_新版本通过后旧版本被替代但历史保留(self):
        service, owner, v1_ids = build_certified_service()
        renewed = service.upload_material(
            "m-1", "accessibility", "acc-content-v2",
            iso_after(800, T0), owner, at=iso_after(100, T0))
        service.review_version(renewed["version_id"], "approve", "续证通过",
                               REVIEWER, at=iso_after(100, T0))
        versions = service.store.list_versions("m-1", "accessibility")
        statuses = {v.version: v.status for v in versions}
        self.assertEqual(statuses[1], "superseded")
        self.assertEqual(statuses[2], "approved")
        # 旧评审意见仍在
        reviews = service.store.list_reviews("m-1")
        self.assertGreaterEqual(len(reviews), 4)


class 重复上传幂等测试(unittest.TestCase):
    def test_同一材料重复上传只生成一条待办(self):
        service, owner, _ = build_certified_service()
        before = len(service.store.list_todos())
        again = service.upload_material(
            "m-1", "accessibility", "accessibility-content-v1",
            iso_after(365, T0), owner, at=T0)
        self.assertTrue(again["duplicate"])
        self.assertFalse(again["todo_created"])
        self.assertEqual(len(service.store.list_todos()), before)

    def test_同一抽查单重复提交只生成一条整改待办(self):
        service, _, _ = build_certified_service()
        first = service.record_inspection(
            "m-1", "fail", INSPECTOR, category="accessibility",
            inspection_id="insp-x", at=iso_after(10, T0))
        second = service.record_inspection(
            "m-1", "fail", INSPECTOR, category="accessibility",
            inspection_id="insp-x", at=iso_after(11, T0))
        self.assertTrue(second["duplicate"])
        self.assertFalse(second["todo_created"])
        remediation_todos = [
            t for t in service.store.list_todos() if t.kind == "remediation"]
        self.assertEqual(len(remediation_todos), 1)


class 角色可见范围测试(unittest.TestCase):
    def setUp(self):
        self.service, self.owner, _ = build_certified_service()
        self.service.record_inspection(
            "m-1", "fail", INSPECTOR, category="accessibility",
            inspection_id="insp-1", comment="内部抽查记录", at=iso_after(5, T0))

    def test_公众只见脱敏结论(self):
        public = self.service.public_status("m-1", at=T0)
        self.assertEqual(set(public),
                         {"merchant_id", "name", "status", "status_label",
                          "mark_allowed", "cert_valid_until", "categories",
                          "as_of"})
        self.assertNotIn("reviews", public)

    def test_公众证据链不含内部评论(self):
        chain = self.service.evidence_chain("m-1", at=T0)
        review = chain["category_evidence"]["accessibility"]["review"]
        self.assertEqual(review["decision"], "approve")
        self.assertNotIn("comment", review)
        self.assertNotIn("inspections", chain)

    def test_内部角色可看评论(self):
        chain = self.service.evidence_chain("m-1", REVIEWER, at=T0)
        self.assertEqual(
            chain["category_evidence"]["accessibility"]["review"]["comment"],
            "符合要求")
        self.assertIn("inspections", chain)

    def test_商户只能看自己的档案(self):
        self.service.dossier("m-1", self.owner)
        with self.assertRaises(AuthError):
            self.service.dossier("m-1", OWNER_OTHER)

    def test_抽查人员看不到证据引用和评审意见(self):
        dossier = self.service.dossier("m-1", INSPECTOR, at=T0)
        self.assertNotIn("content_ref", dossier["versions"][0])
        self.assertNotIn("reviews", dossier)
        self.assertNotIn("disputes", dossier)
        self.assertIn("inspections", dossier)

    def test_商户能看到证据引用(self):
        dossier = self.service.dossier("m-1", self.owner, at=T0)
        self.assertEqual(dossier["versions"][0]["content_ref"],
                         "accessibility-content-v1")

    def test_评审待办按角色过滤(self):
        # 再造一个待评审版本，给评审可见
        self.service.upload_material(
            "m-1", "safety_service", "new-version-content",
            iso_after(400, T0), self.owner, at=iso_after(6, T0))
        review_todos = self.service.list_todos(REVIEWER)
        self.assertTrue(review_todos)
        self.assertTrue(all(t["kind"] in ("material_review", "dispute_review")
                            for t in review_todos))
        inspection_todos = self.service.list_todos(INSPECTOR)
        self.assertTrue(all(t["kind"] == "remediation"
                            for t in inspection_todos))
        self.assertEqual(len(inspection_todos), 1)

    def test_无角色不能写(self):
        with self.assertRaises(AuthError):
            self.service.suspend("m-1", "x", REVIEWER)
        with self.assertRaises(AuthError):
            self.service.record_inspection("m-1", "fail", REVIEWER)


class 到期批量扫描测试(unittest.TestCase):
    def test_批量扫描幂等(self):
        service_a, _, _ = build_certified_service(merchant_id="m-1")
        # 第二个商户
        owner2 = {"id": "owner-2", "role": "owner"}
        service_a.register("m-2", "owner-2", "二店", OPERATOR)
        for category in CATEGORIES:
            r = service_a.upload_material(
                "m-2", category, f"m2-{category}", iso_after(200, T0),
                owner2, at=T0)
            service_a.review_version(r["version_id"], "approve", "ok",
                                     REVIEWER, at=T0)

        # 尚早：无待办
        self.assertEqual(
            service_a.scan_expirations(at=T0)["renewal_todos_opened"], 0)
        # 第 180 天：m-2 的三份进入 30 天窗口
        scan = service_a.scan_expirations(at=iso_after(180, T0))
        self.assertEqual(scan["renewal_todos_opened"], 3)
        self.assertIn("m-2", scan["merchants_with_due_renewal"])
        self.assertNotIn("m-1", scan["merchants_with_due_renewal"])
        # 再扫与重启对账都不重复
        self.assertEqual(
            service_a.scan_expirations(at=iso_after(181, T0))[
                "renewal_todos_opened"], 0)
        self.assertEqual(
            service_a.reconcile_todos(at=iso_after(182, T0))["opened"], 0)

        # 过期后扫描：m-2 出现在不可用标识名单
        late = service_a.scan_expirations(at=iso_after(340, T0))
        self.assertIn("m-2", late["merchants_not_markable"])


class 暂停与恢复测试(unittest.TestCase):
    def test_暂停立即影响查询且保留凭证(self):
        service, owner, version_ids = build_certified_service()
        suspension = service.suspend("m-1", "申报调查", OPERATOR,
                                     at=iso_after(5, T0))
        public = service.public_status("m-1", at=iso_after(5, T0))
        self.assertEqual(public["status"], "suspended")
        self.assertFalse(public["mark_allowed"])

        # 历史凭证全部保留
        dossier = service.dossier("m-1", owner, at=iso_after(5, T0))
        self.assertEqual(len(dossier["versions"]), 3)
        self.assertEqual(len(dossier["reviews"]), 3)
        self.assertEqual(len(dossier["suspensions"]), 1)
        chain = service.evidence_chain("m-1", owner, at=iso_after(5, T0))
        self.assertTrue(chain["chain_valid"])
        actions = [e["action"] for e in chain["events"]]
        self.assertIn("material.approved", actions)
        self.assertIn("certification.suspended", actions)

    def test_重复暂停幂等(self):
        service, _, _ = build_certified_service()
        service.suspend("m-1", "x", OPERATOR, suspension_id="s-1")
        again = service.suspend("m-1", "x", OPERATOR, suspension_id="s-1")
        self.assertTrue(again["duplicate"])

    def test_整改未结时恢复暂停仍是整改中(self):
        service, _, _ = build_certified_service()
        inspection = service.record_inspection(
            "m-1", "fail", INSPECTOR, at=iso_after(2, T0))
        suspension = service.suspend("m-1", "调查", OPERATOR,
                                     at=iso_after(3, T0))
        result = service.resume(suspension["suspension_id"], OPERATOR,
                                at=iso_after(4, T0))
        self.assertEqual(result["status_after"], "rectifying")
        self.assertFalse(result["mark_allowed"])

    def test_恢复后合规即重新可用(self):
        service, _, _ = build_certified_service()
        suspension = service.suspend("m-1", "调查", OPERATOR, at=T0)
        result = service.resume(suspension["suspension_id"], OPERATOR,
                                at=iso_after(1, T0))
        self.assertEqual(result["status_after"], "certified")
        self.assertTrue(result["mark_allowed"])


class 整改复核测试(unittest.TestCase):
    def test_材料过期时不能凭旧版本恢复(self):
        service, owner, _ = build_certified_service(days=5)
        inspection = service.record_inspection(
            "m-1", "fail", INSPECTOR, at=iso_after(4, T0))
        # 第 10 天材料已过期，整改复核被拒绝
        with self.assertRaises(ValueError):
            service.resolve_remediation(
                inspection["inspection_id"], None, INSPECTOR,
                at=iso_after(10, T0))
        self.assertEqual(
            service.snapshot("m-1", at=iso_after(10, T0))["status"],
            "rectifying")

    def test_凭新合规版本恢复(self):
        service, owner, _ = build_certified_service(days=5)
        inspection = service.record_inspection(
            "m-1", "fail", INSPECTOR, at=iso_after(4, T0))
        # 三类全部重新提交并通过
        for category in CATEGORIES:
            r = service.upload_material(
                "m-1", category, f"{category}-renewed",
                iso_after(400, T0), owner, at=iso_after(6, T0))
            service.review_version(r["version_id"], "approve", "整改合格",
                                   REVIEWER, at=iso_after(6, T0))
        result = service.resolve_remediation(
            inspection["inspection_id"], None, INSPECTOR, at=iso_after(7, T0))
        self.assertEqual(result["status_after"], "certified")
        self.assertTrue(result["mark_allowed"])
        # 整改待办已完结
        self.assertFalse(any(
            t.kind == "remediation" and t.status == "open"
            for t in service.store.list_todos()))

    def test_指定非合规版本不能恢复(self):
        service, owner, version_ids = build_certified_service()
        inspection = service.record_inspection(
            "m-1", "fail", INSPECTOR, category="accessibility", at=T0)
        # 拿价格公示的版本号冒充无障碍的恢复依据
        with self.assertRaises(ValueError):
            service.resolve_remediation(
                inspection["inspection_id"], version_ids[1], INSPECTOR, at=T0)


class 争议复核测试(unittest.TestCase):
    def test_争议成立撤销抽查与整改(self):
        service, owner, _ = build_certified_service()
        inspection = service.record_inspection(
            "m-1", "fail", INSPECTOR, category="safety_service",
            inspection_id="insp-d", at=iso_after(2, T0))
        self.assertEqual(
            service.snapshot("m-1", at=iso_after(2, T0))["status"], "rectifying")
        dispute = service.raise_dispute(
            "m-1", "inspection", "insp-d", "抽查程序有问题", owner,
            at=iso_after(3, T0))
        # 重复提争议不产生第二条待办
        again = service.raise_dispute(
            "m-1", "inspection", "insp-d", "抽查程序有问题", owner,
            at=iso_after(3, T0))
        self.assertTrue(again["duplicate"])

        result = service.resolve_dispute(
            dispute["dispute_id"], True, REVIEWER, comment="抽查程序违规",
            at=iso_after(4, T0))
        self.assertEqual(result["decision"], "upheld")
        self.assertTrue(result["inspection_voided"])
        self.assertEqual(result["status_after"], "certified")
        self.assertTrue(result["mark_allowed"])
        # 抽查记录保留但标记作废，整改待办撤销
        self.assertIsNotNone(
            service.store.get_inspection("insp-d").voided_at)
        self.assertFalse(any(
            t.kind == "remediation" and t.status == "open"
            for t in service.store.list_todos()))

    def test_争议驳回维持整改(self):
        service, owner, _ = build_certified_service()
        inspection = service.record_inspection(
            "m-1", "fail", INSPECTOR, at=iso_after(2, T0))
        dispute = service.raise_dispute(
            "m-1", "inspection", inspection["inspection_id"], "不服", owner,
            at=iso_after(3, T0))
        result = service.resolve_dispute(
            dispute["dispute_id"], False, REVIEWER, comment="抽查有效",
            at=iso_after(4, T0))
        self.assertEqual(result["decision"], "rejected")
        self.assertFalse(result["inspection_voided"])
        self.assertEqual(result["status_after"], "rectifying")

    def test_只有评审角色可裁决争议(self):
        service, owner, _ = build_certified_service()
        inspection = service.record_inspection(
            "m-1", "fail", INSPECTOR, at=T0)
        dispute = service.raise_dispute(
            "m-1", "inspection", inspection["inspection_id"], "不服",
            owner, at=T0)
        with self.assertRaises(AuthError):
            service.resolve_dispute(dispute["dispute_id"], True, INSPECTOR)


class 重启幂等测试(unittest.TestCase):
    def test_服务重启后待办不重复不丢失(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "cert.db")
            service = Service(Store(path), auto_reconcile=False)
            service.register("m-1", "owner-1", "店", OPERATOR, at=T0)
            pending = service.upload_material(
                "m-1", "accessibility", "acc", iso_after(400, T0),
                OWNER, at=T0)
            approved = service.upload_material(
                "m-1", "price_disclosure", "price", iso_after(10, T0),
                OWNER, at=T0)
            service.review_version(approved["version_id"], "approve", "ok",
                                   REVIEWER, at=T0)
            inspection = service.record_inspection(
                "m-1", "fail", INSPECTOR, inspection_id="insp-1", at=T0)
            dispute = service.raise_dispute(
                "m-1", "inspection", "insp-1", "异议", OWNER, at=T0)
            expected = sorted(
                (t.kind, t.dedup_key, t.status)
                for t in service.store.list_todos())
            service.store.close()

            service2 = Service(Store(path))  # 构造时自动对账
            after = sorted(
                (t.kind, t.dedup_key, t.status)
                for t in service2.store.list_todos())
            self.assertEqual(after, expected)
            service2.store.close()

            service3 = Service(Store(path))  # 再重启一次仍然一致
            after3 = sorted(
                (t.kind, t.dedup_key, t.status)
                for t in service3.store.list_todos())
            self.assertEqual(after3, expected)

            # 业务推进后重启：已完结的待办不会被对账重新打开
            service3.resolve_dispute(
                dispute["dispute_id"], True, REVIEWER, at=iso_after(1, T0))
            service3.store.close()
            service4 = Service(Store(path))
            open_kinds = sorted(
                t.kind for t in service4.store.list_todos(status="open"))
            self.assertNotIn("dispute_review", open_kinds)
            self.assertNotIn("remediation", open_kinds)
            self.assertIn("material_review", open_kinds)  # 仍待评审
            service4.store.close()


class 证据链测试(unittest.TestCase):
    def test_证据链回放某次公开状态(self):
        service, owner, _ = build_certified_service()
        service.record_inspection(
            "m-1", "fail", INSPECTOR, category="accessibility",
            inspection_id="insp-1", at=iso_after(10, T0))
        chain = service.evidence_chain("m-1", OPERATOR, at=iso_after(10, T0))
        self.assertTrue(chain["chain_valid"])
        self.assertEqual(chain["public_status"]["status"], "rectifying")
        self.assertTrue(chain["active_remediations"])
        # 每个分项都能指到具体版本与评审
        for category in CATEGORIES:
            evidence = chain["category_evidence"][category]
            self.assertIn("version", evidence)
            self.assertEqual(evidence["version"]["status"], "approved")
            self.assertEqual(evidence["review"]["decision"], "approve")

    def test_历史时刻回放不受后续事件影响(self):
        service, _, _ = build_certified_service()
        at_certified = service.evidence_chain("m-1", at=iso_after(5, T0))
        self.assertEqual(
            at_certified["public_status"]["status"], "certified")
        service.suspend("m-1", "调查", OPERATOR, at=iso_after(10, T0))
        # 暂停事件之后的回放显示暂停；之前的回放仍是认证有效
        self.assertEqual(
            service.evidence_chain("m-1", at=iso_after(11, T0))[
                "public_status"]["status"], "suspended")
        self.assertEqual(
            service.evidence_chain("m-1", at=iso_after(5, T0))[
                "public_status"]["status"], "certified")

    def test_整改完结后可回放整改中的历史状态(self):
        service, owner, _ = build_certified_service()
        inspection = service.record_inspection(
            "m-1", "fail", INSPECTOR, at=iso_after(10, T0))
        service.resolve_remediation(
            inspection["inspection_id"], None, INSPECTOR, at=iso_after(12, T0))
        # 第 11 天仍在整改中；第 13 天已恢复
        self.assertEqual(
            service.snapshot("m-1", at=iso_after(11, T0))["status"],
            "rectifying")
        self.assertEqual(
            service.snapshot("m-1", at=iso_after(13, T0))["status"],
            "certified")
        chain = service.evidence_chain("m-1", at=iso_after(11, T0))
        self.assertTrue(chain["active_remediations"])
        self.assertEqual(chain["public_status"]["status"], "rectifying")

    def test_篡改事件可被发现(self):
        service, _, _ = build_certified_service()
        service.store.connection.execute(
            "UPDATE events SET payload=? WHERE seq=1",
            (json.dumps({"tampered": True}, ensure_ascii=False),))
        service.store.connection.commit()
        chain = service.evidence_chain("m-1")
        self.assertFalse(chain["chain_valid"])
        self.assertEqual(chain["chain_first_bad_seq"], 1)


class API适配测试(unittest.TestCase):
    def test_主要动作走JSON适配层(self):
        service = Service(Store())

        def call(payload):
            return json.loads(handle(json.dumps(payload, ensure_ascii=False),
                                     service))

        self.assertEqual(call({"action": "health"})["status"], "ok")
        call({"action": "register", "actor": OPERATOR,
              "record_id": "m-1", "owner_id": "owner-1", "name": "店"})
        for category in CATEGORIES:
            uploaded = call({
                "action": "upload_material", "actor": OWNER,
                "merchant_id": "m-1", "category": category,
                "content_ref": f"{category}-v1",
                "valid_until": iso_after(365, T0), "at": T0})
            call({"action": "review_version", "actor": REVIEWER,
                  "version_id": uploaded["version_id"], "decision": "approve",
                  "at": T0})
        self.assertEqual(
            call({"action": "public_status", "merchant_id": "m-1"})[
                "status"], "certified")
        chain = call({"action": "evidence_chain", "merchant_id": "m-1"})
        self.assertTrue(chain["chain_valid"])


if __name__ == "__main__":
    unittest.main()
