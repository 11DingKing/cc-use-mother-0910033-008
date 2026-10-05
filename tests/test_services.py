"""业务编排层测试：快照/规则版本/试算/差异/发布/放弃/撤销。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quota_alloc.services import ApiError, QuotaService
from quota_alloc.storage import Repository

SUBJECTS = [
    {"subject_id": "E01", "name": "大企甲", "size": "large", "tech_route": "A",
     "output": 5000, "compliance_score": 95},
    {"subject_id": "E02", "name": "中企乙", "size": "medium", "tech_route": "A",
     "output": 800, "compliance_score": 70},
    {"subject_id": "E03", "name": "小企丙", "size": "small", "tech_route": "B",
     "output": 30, "compliance_score": 60},
    {"subject_id": "E04", "name": "微企丁", "size": "micro", "tech_route": "B",
     "output": 5, "compliance_score": 55, "waitlist": True},
    {"subject_id": "E05", "name": "不合格戊", "size": "small", "tech_route": "A",
     "output": 200, "compliance_score": 40, "eligible": False},
]

RULES = {
    "name": "基线规则",
    "output_weight": 1.0,
    "compliance_weight": 0.5,
    "route_coeff": {"A": 1.0, "B": 0.6},
    "floors": {"large": 200, "medium": 80, "small": 40, "micro": 20},
    "caps": {"large": 5000, "medium": 2000, "small": 800, "micro": 400},
}


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = QuotaService(Repository(":memory:"))

    def _scenario(self, total=3000, rules_overrides=None):
        snap = self.svc.create_snapshot(SUBJECTS)
        rules = dict(RULES)
        if rules_overrides:
            rules.update(rules_overrides)
        rlv = self.svc.create_rule_version(name=rules["name"], params=rules)
        return self.svc.run_scenario(
            snapshot_id=snap["snapshot_id"],
            rule_version_id=rlv["rule_version_id"],
            total_quota=total,
            name="方案",
        )

    def test_snapshot_content_addressing_is_idempotent(self) -> None:
        a = self.svc.create_snapshot(SUBJECTS, note="第一次")
        b = self.svc.create_snapshot(SUBJECTS, note="第二次")
        self.assertEqual(a["snapshot_id"], b["snapshot_id"])
        self.assertTrue(a["created"])
        self.assertFalse(b["created"])

    def test_invalid_subject_rejected(self) -> None:
        bad = [{"subject_id": "X1", "size": "small", "tech_route": "A",
                "output": -1, "compliance_score": 50}]
        with self.assertRaises(ApiError) as cm:
            self.svc.create_snapshot(bad)
        self.assertEqual(cm.exception.code, "invalid_subject")

    def test_floor_protects_small_firm(self) -> None:
        scn = self._scenario(total=3000)
        by = {ln["subject_id"]: ln for ln in scn["result"]["lines"]}
        self.assertGreaterEqual(by["E03"]["allocated"], 40)
        self.assertGreaterEqual(by["E01"]["allocated"], 200)
        self.assertEqual(by["E05"]["allocated"], 0)
        self.assertIn("not_eligible", by["E05"]["reasons"])
        self.assertEqual(by["E04"]["allocated"], 0)
        self.assertIn("waitlist", by["E04"]["reasons"])
        # 守恒：分出头寸 + 留存 = 总额度
        self.assertEqual(scn["result"]["allocated_sum"] + scn["result"]["remainder"], 3000)

    def test_scenario_is_reproducible_and_content_addressed(self) -> None:
        a = self._scenario(total=3000)
        b = self._scenario(total=3000)
        self.assertEqual(a["scenario_id"], b["scenario_id"])
        self.assertEqual(a["result"], b["result"])

    def test_diff_explains_rule_change(self) -> None:
        snap = self.svc.create_snapshot(SUBJECTS)
        r1 = self.svc.create_rule_version(name="基线", params=RULES)
        pro_small = dict(RULES)
        pro_small["name"] = "扶小"
        pro_small["floors"] = {"large": 200, "medium": 80, "small": 300, "micro": 100}
        r2 = self.svc.create_rule_version(name="扶小", params=pro_small)
        a = self.svc.run_scenario(snapshot_id=snap["snapshot_id"],
                                  rule_version_id=r1["rule_version_id"], total_quota=3000)
        b = self.svc.run_scenario(snapshot_id=snap["snapshot_id"],
                                  rule_version_id=r2["rule_version_id"], total_quota=3000)
        diff = self.svc.diff_scenarios(a["scenario_id"], b["scenario_id"])
        self.assertTrue(any("规则版本" in c for c in diff["root_causes"]))
        e03 = next(p for p in diff["per_subject"] if p["subject_id"] == "E03")
        self.assertGreater(e03["delta_b_minus_a"], 0)
        self.assertIn("多分", e03["explanation"])

    def test_publish_is_atomic_and_complete(self) -> None:
        scn = self._scenario(total=3000)
        pub = self.svc.publish(scn["scenario_id"])
        self.assertEqual(pub["entry_count"], len(SUBJECTS))  # 含 0 额度行
        detail = self.svc.get_publication(pub["publication_id"])
        # 分录里每家企业都有原因
        for e in detail["entries"]:
            self.assertTrue(e["reasons"])
            self.assertTrue(e["reason_text"])
        # 分录总额守恒
        self.assertEqual(detail["allocated_sum"], 3000)
        with self.assertRaises(ApiError) as cm:
            self.svc.publish(scn["scenario_id"])
        self.assertEqual(cm.exception.code, "already_published")

    def test_relinquish_triggers_deterministic_promotion(self) -> None:
        scn = self._scenario(total=3000)
        pub = self.svc.publish(scn["scenario_id"])
        pid = pub["publication_id"]
        before = {e["subject_id"]: e["current_amount"]
                  for e in self.svc.get_publication(pid)["entries"]}
        out = self.svc.relinquish(pid, "E01")
        self.assertEqual(out["event_type"], "relinquish")
        # E04 是唯一候补，应被递补
        self.assertIn("E04", out["promoted"])
        detail = self.svc.get_publication(pid)
        holdings = {h["subject_id"]: h for h in detail["entries"]}
        self.assertEqual(holdings["E01"]["current_amount"], 0)
        self.assertEqual(holdings["E01"]["current_status"], "relinquished")
        self.assertGreater(holdings["E04"]["current_amount"], 0)
        self.assertEqual(holdings["E04"]["current_status"], "active")
        # 事件已留痕，且总额守恒（释放=分发+留存）
        self.assertEqual(len(detail["events"]), 1)
        self.assertEqual(out["distributed"] + out["retained"], before["E01"])
        # 不能重复放弃
        with self.assertRaises(ApiError) as cm:
            self.svc.relinquish(pid, "E01")
        self.assertEqual(cm.exception.code, "already_released")

    def test_revoke_then_second_release_skips_empty_waitlist(self) -> None:
        scn = self._scenario(total=3000)
        pid = self.svc.publish(scn["scenario_id"])["publication_id"]
        r1 = self.svc.revoke(pid, "E02")
        self.assertTrue(r1["events"])
        # E04 已在第一次事件中递补；再放弃 E03 时已无候补，仅做尾差重分
        r2 = self.svc.relinquish(pid, "E03")
        self.assertEqual(r2["promoted"], [])
        detail = self.svc.get_publication(pid)
        self.assertEqual([e["event_type"] for e in detail["events"]],
                         ["revoke", "relinquish"])
        # 事件序号单调
        self.assertEqual([e["seq"] for e in detail["events"]], [1, 2])

    def test_cannot_relinquish_zero_holding(self) -> None:
        scn = self._scenario(total=3000)
        pid = self.svc.publish(scn["scenario_id"])["publication_id"]
        with self.assertRaises(ApiError) as cm:
            self.svc.relinquish(pid, "E05")  # 本就不合格，无额度
        self.assertEqual(cm.exception.code, "not_holder")


if __name__ == "__main__":
    unittest.main()
