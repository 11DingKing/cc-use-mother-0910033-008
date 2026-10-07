"""分配引擎单元测试：保底、上限、尾差、候补与递补的确定规则。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quota_service.allocation import (
    Enterprise,
    RuleConfig,
    allocate,
    redistribute,
    waitlist_order,
)

RULES = RuleConfig.from_dict({
    "tech_factors": {"路线A": 10000, "路线B": 8000},
    "compliance_bands": [
        {"min_rate": 0, "factor": 8000},
        {"min_rate": 6000, "factor": 10000},
        {"min_rate": 9000, "factor": 11000},
    ],
    "floors": {"小微": 100},
    "caps": {"大型": 850},
})


def ent(eid, category, output, route="路线A", rate=10000, eligible=True, note=""):
    return Enterprise(eid, f"企业{eid}", category, output, route, rate, eligible, note)


class AllocateTest(unittest.TestCase):
    def test_small_enterprise_gets_floor(self) -> None:
        """纯按比例会让小微企业归零，保底规则保证其最低额度。"""
        result = allocate(1000, [
            ent("E1", "大型", 1000),
            ent("E2", "小微", 1),
            ent("E3", "小微", 0, route="路线B", rate=9000),
        ], RULES)
        self.assertEqual(result["E2"].floor, 100)
        self.assertGreaterEqual(result["E2"].amount, 100)
        self.assertEqual(result["E3"].amount, 100)  # 权重为零仍有保底
        self.assertEqual(sum(r.amount for r in result.values()), 1000)

    def test_cap_is_enforced_and_excess_redistributed(self) -> None:
        rules = RuleConfig.from_dict({
            "tech_factors": {"路线A": 10000},
            "compliance_bands": [],
            "floors": {"小微": 100},
            "caps": {"大型": 700},
        })
        result = allocate(1000, [ent("E1", "大型", 1000), ent("E2", "小微", 1)], rules)
        self.assertEqual(result["E1"].amount, 700)
        self.assertEqual(result["E1"].reason_code, "capped")
        self.assertEqual(result["E2"].amount, 300)  # 上限约束释放的额度流向未触顶企业
        self.assertEqual(sum(r.amount for r in result.values()), 1000)

    def test_remainder_tie_break_is_deterministic(self) -> None:
        """尾差：小数余量相同按企业编号升序，重复计算结果一致。"""
        rules = RuleConfig.from_dict({"tech_factors": {"路线A": 10000}, "compliance_bands": []})
        enterprises = [ent("A02", "普通", 10), ent("A01", "普通", 10)]
        first = allocate(11, enterprises, rules)
        second = allocate(11, enterprises, rules)
        self.assertEqual({k: v.amount for k, v in first.items()},
                         {k: v.amount for k, v in second.items()})
        self.assertEqual(first["A01"].amount, 6)  # 余量并列，编号小者得尾差
        self.assertEqual(first["A02"].amount, 5)
        self.assertEqual(first["A01"].components["尾差"], 1)

    def test_floors_scaled_down_when_pool_too_small(self) -> None:
        result = allocate(150, [ent("E1", "小微", 10), ent("E2", "小微", 20)], RULES)
        self.assertEqual(result["E1"].amount, 75)
        self.assertEqual(result["E2"].amount, 75)
        self.assertEqual(result["E1"].reason_code, "floor_scaled")

    def test_zero_weight_and_ineligible_reasons(self) -> None:
        result = allocate(1000, [
            ent("E1", "大型", 100),
            ent("E2", "新入", 0),
            ent("E3", "大型", 50, eligible=False, note="履约不达标"),
        ], RULES)
        self.assertEqual(result["E2"].reason_code, "zero_weight")
        self.assertEqual(result["E2"].amount, 0)
        self.assertEqual(result["E3"].reason_code, "ineligible")
        self.assertIn("履约不达标", result["E3"].reason_detail)
        self.assertEqual(waitlist_order(result), ["E2"])

    def test_waitlist_includes_under_floor_enterprises(self) -> None:
        rules = RuleConfig.from_dict({
            "tech_factors": {"路线A": 10000},
            "compliance_bands": [],
            "floors": {"小微": 100, "新入": 30},
        })
        result = allocate(100, [
            ent("E1", "大型", 1000),
            ent("E2", "小微", 1),
            ent("E5", "新入", 0),
        ], rules)
        # 保底总额 130 超过池子 100，按比例压缩：E2 77、E5 23，均未足额
        self.assertEqual(result["E2"].amount, 77)
        self.assertEqual(result["E5"].amount, 23)
        self.assertEqual(result["E1"].reason_code, "pool_exhausted")
        self.assertEqual(waitlist_order(result), ["E1", "E2", "E5"])  # 按权重降序


class RedistributeTest(unittest.TestCase):
    def test_waitlist_floor_filled_before_proportional(self) -> None:
        """释放额度先补候补企业的保底缺口，余量再按权重分配。"""
        grants = redistribute(
            freed=65,
            candidate_ids=["E1", "E3", "E5"],
            weights={"E1": 110_000_000_000, "E3": 0, "E5": 880_000_000},
            floors={"E1": 0, "E3": 100, "E5": 30},
            caps={"E1": 850, "E3": 150, "E5": 150},
            holdings={"E1": 0, "E3": 65, "E5": 20},
            waitlist=["E1", "E5", "E3"],
        )
        # E1 保底为 0 跳过；E5 补 10 足额；E3 补 35 足额；剩余 20 按权重全归 E1
        self.assertEqual(grants, {"E5": 10, "E3": 35, "E1": 20})
        self.assertEqual(sum(grants.values()), 65)

    def test_redistribute_respects_cap(self) -> None:
        grants = redistribute(
            freed=100,
            candidate_ids=["E1", "E2"],
            weights={"E1": 100, "E2": 100},
            floors={"E1": 0, "E2": 0},
            caps={"E1": 30, "E2": 1000},
            holdings={"E1": 0, "E2": 0},
            waitlist=[],
        )
        self.assertEqual(grants, {"E1": 30, "E2": 70})


if __name__ == "__main__":
    unittest.main()
