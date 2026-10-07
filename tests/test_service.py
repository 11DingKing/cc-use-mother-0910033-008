"""服务层测试：快照、规则版本、多试算方案差异、原子发布、放弃/撤销/递补。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quota_service.service import QuotaService, ServiceError

PERIOD = "2026"
ENTERPRISES = [
    {"enterprise_id": "E1", "name": "大型甲", "category": "大型", "output": 1000,
     "tech_route": "路线A", "compliance_rate": 10000, "eligible": True},
    {"enterprise_id": "E2", "name": "小微乙", "category": "小微", "output": 1,
     "tech_route": "路线A", "compliance_rate": 10000, "eligible": True},
    {"enterprise_id": "E3", "name": "小微丙", "category": "小微", "output": 0,
     "tech_route": "路线B", "compliance_rate": 9000, "eligible": True},
    {"enterprise_id": "E4", "name": "失信丁", "category": "大型", "output": 500,
     "tech_route": "路线A", "compliance_rate": 4000,
     "eligible": False, "note": "履约不达标"},
]
RULES_V1 = {
    "tech_factors": {"路线A": 10000, "路线B": 8000},
    "compliance_bands": [
        {"min_rate": 0, "factor": 8000},
        {"min_rate": 6000, "factor": 10000},
        {"min_rate": 9000, "factor": 11000},
    ],
    "floors": {"小微": 100},
    "caps": {"大型": 850},
}
RULES_V2 = {**RULES_V1, "tech_factors": {"路线A": 12000, "路线B": 8000}, "caps": {"大型": 780}}


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.service = QuotaService(":memory:")
        self.snapshot_id = self.service.create_snapshot(PERIOD, ENTERPRISES)["snapshot_id"]
        self.rule_v1 = self.service.create_rule_version("v1", RULES_V1)["rule_version_id"]

    def tearDown(self) -> None:
        self.service.close()

    def trial(self, total=1000, rule=None, period=PERIOD):
        return self.service.run_trial(period, self.snapshot_id, rule or self.rule_v1, total)


class TrialAndDiffTest(ServiceTestBase):
    def test_trial_results_explain_every_enterprise(self) -> None:
        scenario = self.trial()
        results = {r["enterprise_id"]: r for r in scenario["results"]}
        self.assertEqual(results["E1"]["amount"], 799)
        self.assertEqual(results["E2"]["amount"], 101)  # 保底 100 + 尾差 1
        self.assertEqual(results["E3"]["amount"], 100)  # 权重为零仍有保底
        self.assertEqual(results["E4"]["amount"], 0)
        self.assertEqual(results["E4"]["reason_code"], "ineligible")
        for r in results.values():
            self.assertTrue(r["reason_detail"])
        self.assertEqual(sum(r["amount"] for r in results.values()), 1000)

    def test_multiple_trials_and_diff_explanation(self) -> None:
        rule_v2 = self.service.create_rule_version("v2", RULES_V2)["rule_version_id"]
        a = self.trial()
        b = self.trial(rule=rule_v2)
        diff = self.service.diff_scenarios(a["id"], b["id"])
        items = {i["enterprise_id"]: i for i in diff["items"]}
        self.assertEqual(items["E1"]["delta"], 780 - 799)
        joined = "；".join(items["E1"]["explanations"])
        self.assertIn("技术路线「路线A」系数 1→1.2", joined)
        self.assertIn("上限 850→780", joined)
        self.assertEqual(items["E2"]["delta"], 120 - 101)
        self.assertIn("技术路线", "；".join(items["E4"]["explanations"]))  # 未获配企业也给出规则变化说明

    def test_trial_rejects_unknown_references(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.run_trial(PERIOD, 999, self.rule_v1, 1000)
        self.assertEqual(ctx.exception.status, 404)


class PublishTest(ServiceTestBase):
    def test_publish_writes_entries_atomically(self) -> None:
        scenario = self.trial()
        result = self.service.publish(scenario["id"])
        self.assertEqual(result["status"], "已确认")
        self.assertEqual(result["entries"], 3)  # E4 无额度不写分录
        self.assertEqual(result["total_allocated"], 1000)
        ledger = self.service.ledger(PERIOD)["entries"]
        self.assertEqual(sum(e["amount"] for e in ledger), 1000)
        self.assertTrue(all(e["entry_type"] == "初始分配" for e in ledger))

    def test_second_publish_is_rejected_without_partial_writes(self) -> None:
        first = self.trial()
        second = self.trial()
        self.service.publish(first["id"])
        with self.assertRaises(ServiceError) as ctx:
            self.service.publish(second["id"])
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(ServiceError):
            self.service.publish(first["id"])  # 重复发布同一方案
        ledger = self.service.ledger(PERIOD)["entries"]
        self.assertEqual(len(ledger), 3)  # 原子性：失败不产生任何分录

    def test_adjustments_before_publish_are_rejected(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.surrender(PERIOD, "E1", 10)
        self.assertEqual(ctx.exception.status, 409)


class AdjustmentTest(ServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.scenario = self.trial()
        self.service.publish(self.scenario["id"])

    def holdings(self):
        return {i["enterprise_id"]: i["current"]
                for i in self.service.allocations(PERIOD)["items"]}

    def test_surrender_triggers_deterministic_backfill(self) -> None:
        result = self.service.surrender(PERIOD, "E1", 100, note="产线检修")
        self.assertEqual(result["freed"], 100)
        self.assertEqual(result["grants"], {"E2": 100})  # E3 权重为零，E1 为来源企业
        holdings = self.holdings()
        self.assertEqual(holdings["E1"], 699)
        self.assertEqual(holdings["E2"], 201)
        self.assertEqual(sum(holdings.values()), 1000)
        scenario = self.service.get_scenario(self.scenario["id"])
        self.assertEqual(scenario["status"], "执行中")

    def test_surrender_validation(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.surrender(PERIOD, "E1", 10_000)
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(ServiceError):
            self.service.surrender(PERIOD, "E1", 0)

    def test_revoke_reclaims_and_redistributes(self) -> None:
        self.service.surrender(PERIOD, "E1", 100)
        result = self.service.revoke(PERIOD, "E2", "查出申报造假")
        self.assertEqual(result["freed"], 201)
        self.assertEqual(result["grants"], {"E1": 151})  # E1 触及上限 850，余量无人可接
        holdings = self.holdings()
        self.assertEqual(holdings["E1"], 850)
        self.assertEqual(holdings["E2"], 0)
        self.assertEqual(holdings["E3"], 100)
        items = {i["enterprise_id"]: i for i in self.service.allocations(PERIOD)["items"]}
        self.assertEqual(items["E2"]["status"], "已撤销")
        with self.assertRaises(ServiceError) as ctx:
            self.service.revoke(PERIOD, "E2", "重复撤销")
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(ServiceError) as ctx:
            self.service.surrender(PERIOD, "E2", 1)
        self.assertEqual(ctx.exception.status, 409)  # 已撤销企业不能再放弃

    def test_revoke_unknown_enterprise(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.revoke(PERIOD, "E9", "不存在")
        self.assertEqual(ctx.exception.status, 404)

    def test_allocations_explain_every_enterprise(self) -> None:
        allocations = self.service.allocations(PERIOD)
        items = {i["enterprise_id"]: i for i in allocations["items"]}
        self.assertEqual(items["E1"]["status"], "持有")
        self.assertEqual(items["E4"]["status"], "未获得")
        self.assertIn("履约不达标", items["E4"]["reason"])
        self.assertEqual(allocations["total_current"], 1000)

    def test_seal_blocks_further_adjustments(self) -> None:
        self.service.seal(PERIOD)
        with self.assertRaises(ServiceError) as ctx:
            self.service.surrender(PERIOD, "E1", 10)
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(ServiceError):
            self.service.revoke(PERIOD, "E1", "已封存")


if __name__ == "__main__":
    unittest.main()
