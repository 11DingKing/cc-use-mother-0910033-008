"""确定性分配内核测试。"""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quota_alloc.allocation import (
    R_CAPPED,
    R_FLOOR_GUARANTEE,
    R_NOT_ELIGIBLE,
    R_PRO_RATA,
    R_RESIDUAL,
    R_ROUND_UP,
    R_WAITLIST,
    R_WAITLIST_PROMOTED,
    R_ZERO_SCORE,
    STATUS_ACTIVE,
    STATUS_REVOKED,
    STATUS_WAITLIST,
    Subject,
    allocate,
    make_rule_params,
    reallocate_release,
    score_int,
)


def s(sid, size, output, comp=80, route="A", eligible=True, waitlist=False, name=None):
    return Subject(sid, name or sid, size, route, output, comp, eligible, waitlist)


def rules(floors=None, caps=None, output_weight=1.0, compliance_weight=0.0,
          route_coeff=None):
    return make_rule_params(
        output_weight=output_weight,
        route_coeff=route_coeff if route_coeff is not None else {"A": 1.0, "B": 0.5},
        compliance_weight=compliance_weight,
        floors=floors or {"large": 100, "medium": 40, "small": 20, "micro": 10},
        caps=caps or {"large": 100000, "medium": 100000, "small": 100000, "micro": 100000},
    )


class AllocationTest(unittest.TestCase):
    def test_small_firm_gets_floor_even_with_tiny_output(self) -> None:
        # 纯按比例小企业几乎拿不到；保底保证基本额度
        subjects = [
            s("E01", "large", 10_000),
            s("E02", "small", 1),
        ]
        result = allocate(subjects, rules(caps={k: 10**9 for k in
                                                ("large", "medium", "small", "micro")}),
                          total_quota=1000)
        by = result.by_id
        self.assertEqual(by["E02"].allocated, 20)
        self.assertIn(R_FLOOR_GUARANTEE, by["E02"].reasons)
        self.assertEqual(result.allocated_sum + result.remainder, 1000)

    def test_deterministic_tie_break_by_subject_id(self) -> None:
        # 同分同保底，尾差 1 单位必须归编号较小者
        subjects = [s("E09", "small", 10), s("E01", "small", 10), s("E05", "small", 10)]
        r = rules(floors={"large": 0, "medium": 0, "small": 0, "micro": 0},
                  caps={k: 10**9 for k in ("large", "medium", "small", "micro")})
        result = allocate(subjects, r, total_quota=10)
        runs = [allocate(subjects, r, 10) for _ in range(5)]
        self.assertTrue(all(run.explain() == result.explain() for run in runs))
        by = result.by_id
        self.assertEqual(by["E01"].allocated, 4)  # 10/3 → 3 家各 3，尾差给 E01
        self.assertEqual(by["E05"].allocated, 3)
        self.assertEqual(by["E09"].allocated, 3)
        self.assertIn(R_ROUND_UP, by["E01"].reasons)

    def test_caps_are_enforced_and_freed_pool_redistributed(self) -> None:
        subjects = [s("E01", "large", 10_000), s("E02", "large", 10_000),
                    s("E03", "small", 100)]
        caps = {"large": 300, "medium": 300, "small": 10**9, "micro": 10**9}
        result = allocate(subjects, rules(caps=caps), total_quota=1000)
        by = result.by_id
        self.assertEqual(by["E01"].allocated, 300)
        self.assertEqual(by["E02"].allocated, 300)
        self.assertIn(R_CAPPED, by["E01"].reasons)
        # 两大户触顶后余量流向 E03
        self.assertEqual(by["E03"].allocated, 400)
        self.assertEqual(result.remainder, 0)

    def test_ineligible_and_waitlist_get_zero_with_reasons(self) -> None:
        subjects = [s("E01", "small", 50, eligible=False),
                    s("E02", "small", 50, waitlist=True),
                    s("E03", "small", 50)]
        result = allocate(subjects, rules(), total_quota=500)
        by = result.by_id
        self.assertEqual(by["E01"].allocated, 0)
        self.assertIn(R_NOT_ELIGIBLE, by["E01"].reasons)
        self.assertEqual(by["E02"].allocated, 0)
        self.assertIn(R_WAITLIST, by["E02"].reasons)
        self.assertGreater(by["E03"].allocated, 0)

    def test_unknown_route_scores_zero(self) -> None:
        subjects = [s("E01", "small", 100, route="ZZ")]
        r = rules()
        self.assertEqual(score_int(subjects[0], r), 0)
        result = allocate(subjects, r, total_quota=100)
        self.assertEqual(result.by_id["E01"].allocated, 20)  # 只有保底
        self.assertIn(R_ZERO_SCORE, result.by_id["E01"].reasons)

    def test_route_coeff_changes_share(self) -> None:
        subjects = [s("E01", "large", 100, route="A"),
                    s("E02", "large", 100, route="B")]
        r = rules(floors={k: 0 for k in ("large", "medium", "small", "micro")},
                  caps={k: 10**9 for k in ("large", "medium", "small", "micro")})
        result = allocate(subjects, r, total_quota=300)
        by = result.by_id
        # A 系数 1.0，B 系数 0.5 → 2:1
        self.assertEqual((by["E01"].allocated, by["E02"].allocated), (200, 100))
        self.assertIn(R_PRO_RATA, by["E01"].reasons)

    def test_tail_remainder_when_everyone_capped(self) -> None:
        subjects = [s("E01", "small", 10)]
        r = rules(floors={"large": 0, "medium": 0, "small": 5, "micro": 0},
                  caps={"large": 0, "medium": 0, "small": 10, "micro": 0})
        result = allocate(subjects, r, total_quota=100)
        self.assertEqual(result.by_id["E01"].allocated, 10)
        self.assertEqual(result.remainder, 90)  # 余量留存，不超额发放

    def test_infeasible_total_prefers_micro(self) -> None:
        subjects = [s("E01", "large", 10), s("E02", "micro", 10)]
        r = rules(floors={"large": 100, "medium": 40, "small": 20, "micro": 10},
                  caps={k: 10**9 for k in ("large", "medium", "small", "micro")})
        result = allocate(subjects, r, total_quota=15)
        by = result.by_id
        self.assertFalse(result.feasible)
        self.assertEqual(by["E02"].allocated, 10)  # 小微优先足额
        self.assertEqual(by["E01"].allocated, 5)
        self.assertEqual(result.floor_shortfall["E01"], 95)

    def test_compliance_weight_enters_score(self) -> None:
        good = s("E01", "small", 100, comp=100)
        bad = s("E02", "small", 100, comp=0)
        r = rules(output_weight=0.0, compliance_weight=1.0)
        self.assertGreater(score_int(good, r), score_int(bad, r))
        self.assertEqual(score_int(bad, r), 0)


class ReallocationTest(unittest.TestCase):
    def _world(self):
        subjects = [
            s("E01", "large", 1000),
            s("E02", "medium", 200),
            s("E03", "small", 10, waitlist=True),
            s("E04", "micro", 5, waitlist=True),
        ]
        r = rules(floors={"large": 100, "medium": 40, "small": 20, "micro": 10},
                  caps={k: 10**9 for k in ("large", "medium", "small", "micro")})
        result = allocate(subjects, r, total_quota=1000)
        current = {ln.subject_id: ln.allocated for ln in result.lines}
        statuses = {s_.subject_id: (STATUS_WAITLIST if s_.waitlist else STATUS_ACTIVE)
                    for s_ in subjects}
        return subjects, r, current, statuses

    def test_relinquish_promotes_waitlist_floors_then_residual(self) -> None:
        subjects, r, current, statuses = self._world()
        released = current["E01"]
        out = reallocate_release(subjects=subjects, rules=r, current=current,
                                 released_subject="E01", released_amount=released,
                                 statuses={**statuses, "E01": "relinquished"})
        self.assertEqual(out.promoted, ["E03", "E04"])  # 编号升序
        by = out.lines
        # 两家候补先各拿保底 20 / 10，随后还参与尾差比例重分
        self.assertGreaterEqual(by["E03"].after, 20)
        self.assertGreaterEqual(by["E04"].after, 10)
        self.assertIn(R_WAITLIST_PROMOTED, by["E03"].reasons)
        self.assertIn(R_RESIDUAL, by["E02"].reasons)
        # 守恒：释放额 = 各家增量 + 留存
        gains = sum(max(0, ln.after - ln.before) for sid, ln in by.items()
                    if sid != "E01")
        self.assertEqual(gains + out.retained, released)
        self.assertEqual(by["E01"].after, 0)

    def test_revoke_uses_same_rules_and_is_deterministic(self) -> None:
        subjects, r, current, statuses = self._world()
        kw = dict(subjects=subjects, rules=r, current=current,
                  released_subject="E02", released_amount=current["E02"],
                  statuses={**statuses, "E02": STATUS_REVOKED})
        a = reallocate_release(**kw)
        b = reallocate_release(**kw)
        self.assertEqual([ln.__dict__ for ln in a.lines.values()],
                         [ln.__dict__ for ln in b.lines.values()])
        self.assertEqual(a.events, b.events)
        # E03 保底 20；释放 40+，E04 保底 10 也应满足（E02 初始 = 40 保底+比例）
        self.assertIn("E03", a.promoted)

    def test_small_release_only_promotes_first_waitlist(self) -> None:
        # E09 释放 40：先 E01 拿满保底 30，剩 10 不足 E02 保底 30（确定不切分保底）
        subjects = [s("E01", "small", 100, waitlist=True),
                    s("E02", "small", 100, waitlist=True),
                    s("E09", "large", 1000)]
        r = rules(floors={"large": 100, "medium": 40, "small": 30, "micro": 10},
                  caps={k: 10**9 for k in ("large", "medium", "small", "micro")})
        out = reallocate_release(
            subjects=subjects, rules=r,
            current={"E01": 0, "E02": 0, "E09": 40},
            released_subject="E09", released_amount=40,
            statuses={"E01": STATUS_WAITLIST, "E02": STATUS_WAITLIST,
                      "E09": "relinquished"},
        )
        self.assertEqual(out.promoted, ["E01"])
        # E01 先整额保底 30，尾差阶段在册可分者仅剩 E01，再得 10
        self.assertEqual(out.lines["E01"].after, 40)
        self.assertEqual(out.lines["E02"].after, 0)
        self.assertEqual(out.retained, 0)

    def test_residual_retained_when_all_capped(self) -> None:
        subjects = [s("E01", "large", 100), s("E09", "large", 100)]
        r = rules(floors={k: 0 for k in ("large", "medium", "small", "micro")},
                  caps={"large": 50, "medium": 0, "small": 0, "micro": 0})
        out = reallocate_release(
            subjects=subjects, rules=r, current={"E01": 50, "E09": 50},
            released_subject="E09", released_amount=50,
            statuses={"E01": STATUS_ACTIVE, "E09": "relinquished"},
        )
        self.assertEqual(out.lines["E01"].after, 50)  # 已在 50 上限
        self.assertEqual(out.retained, 50)


class RandomizedPropertyTest(unittest.TestCase):
    """随机化不变量：守恒、保底、上限、非负、原因非空、确定性、重分守恒/不超限。"""

    def test_randomized_invariants(self) -> None:
        rng = random.Random(20261005)
        sizes_l = ["large", "medium", "small", "micro"]
        routes = ["A", "B", "C", "X"]
        trials = 2000
        for trial in range(trials):
            n = rng.randint(1, 8)
            floors = {z: rng.randint(0, 30) for z in sizes_l}
            caps = {z: floors[z] + rng.randint(0, 200) for z in sizes_l}
            ow, cw = 1, rng.choice([0, 0.1, 1, 2])
            rr_rules = make_rule_params(
                output_weight=ow, compliance_weight=cw,
                route_coeff={"A": 1.0, "B": rng.choice([0.0, 0.5, 1.5]),
                             "C": rng.choice([0.25, 0.75])},
                floors=floors, caps=caps)
            subs = []
            for i in range(n):
                st = rng.choices(["ok", "wait", "bad"], [8, 2, 1])[0]
                subs.append(Subject(
                    f"E{i:02d}", f"n{i}", rng.choice(sizes_l),
                    rng.choice(routes), rng.randint(0, 500), rng.randint(0, 100),
                    eligible=st != "bad", waitlist=st == "wait"))
            total = rng.choice([0, 1, 5, 50, rng.randint(0, 400), 10**6])
            res = allocate(subs, rr_rules, total)
            self.assertEqual(res.allocated_sum + res.remainder, total,
                             f"trial {trial}: 守恒")
            by_sub = {x.subject_id: x for x in subs}
            for ln in res.lines:
                zz = by_sub[ln.subject_id]
                self.assertLessEqual(ln.allocated, caps[zz.size],
                                     f"trial {trial}: 不超上限")
                if res.feasible and zz.eligible and not zz.waitlist:
                    self.assertGreaterEqual(ln.allocated, floors[zz.size],
                                            f"trial {trial}: 保底")
                self.assertGreaterEqual(ln.allocated, 0)
                self.assertTrue(ln.reasons, f"trial {trial}: 原因非空")
            res2 = allocate(subs, rr_rules, total)
            self.assertEqual(
                [(l.subject_id, l.allocated, tuple(l.reasons)) for l in res.lines],
                [(l.subject_id, l.allocated, tuple(l.reasons)) for l in res2.lines],
                f"trial {trial}: 确定性")
            holders = [l.subject_id for l in res.lines if l.allocated > 0
                       and by_sub[l.subject_id].eligible and not by_sub[l.subject_id].waitlist]
            if holders:
                target = rng.choice(holders)
                current = {l.subject_id: l.allocated for l in res.lines}
                statuses = {}
                for x in subs:
                    if not x.eligible:
                        statuses[x.subject_id] = "ineligible"
                    elif x.waitlist:
                        statuses[x.subject_id] = STATUS_WAITLIST
                    else:
                        statuses[x.subject_id] = STATUS_ACTIVE
                statuses[target] = rng.choice(["relinquished", "revoked"])
                reall = reallocate_release(
                    subjects=subs, rules=rr_rules, current=current,
                    released_subject=target, released_amount=current[target],
                    statuses=statuses)
                gain = sum(max(0, l.after - l.before)
                           for sid, l in reall.lines.items() if sid != target)
                self.assertEqual(gain + reall.retained, current[target],
                                 f"trial {trial}: 重分守恒")
                for sid, l in reall.lines.items():
                    self.assertLessEqual(l.after, caps[by_sub[sid].size],
                                         f"trial {trial}: 重分不超上限")
                    self.assertGreaterEqual(l.after, 0)


if __name__ == "__main__":
    unittest.main()
