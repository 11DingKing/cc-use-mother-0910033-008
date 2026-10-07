"""确定性配额分配引擎。

所有分配均为确定规则：相同输入必然得到相同输出，不依赖时间、随机数或字典遍历顺序。

分配顺序：
1. 保底：每家企业先获得所属类别的最低保障额度（不超过其上限）；
   若保底总额超过额度池，则按保底额比例压缩（同样走尾差确定规则）。
2. 加权：剩余额度按 产量 × 技术路线系数 × 履约系数 的权重比例分配，
   触及类别上限的企业先封顶，剩余继续在未触顶企业间按比例分配。
3. 尾差：取整后的剩余单位按小数余量降序、企业编号升序逐个分配，确定归属。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

BASIS = 10_000  # 系数基点：10000 表示 1.0


@dataclass(frozen=True)
class Enterprise:
    """资格快照中的企业事实。"""

    enterprise_id: str
    name: str
    category: str
    output: int  # 产量
    tech_route: str  # 技术路线
    compliance_rate: int  # 历史履约率，基点 0-10000
    eligible: bool
    note: str = ""


@dataclass(frozen=True)
class RuleConfig:
    """分配规则版本内容。"""

    tech_factors: dict[str, int]  # 技术路线 → 系数基点
    compliance_bands: tuple[tuple[int, int], ...]  # (履约率阈值基点, 系数基点)，按阈值升序
    floors: dict[str, int]  # 企业类别 → 保底额度
    caps: dict[str, int]  # 企业类别 → 上限额度
    default_floor: int = 0
    default_cap: int | None = None  # None 表示不限（有效上限取总额度）

    @classmethod
    def from_dict(cls, raw: dict) -> "RuleConfig":
        """解析并校验规则配置，非法输入抛出 ValueError。"""
        if not isinstance(raw, dict):
            raise ValueError("规则配置必须是对象")
        tech = raw.get("tech_factors", {})
        if not isinstance(tech, dict) or any(not isinstance(v, int) or v <= 0 for v in tech.values()):
            raise ValueError("tech_factors 必须是 技术路线→正整数系数 的映射")
        bands_raw = raw.get("compliance_bands", [])
        bands: list[tuple[int, int]] = []
        seen: set[int] = set()
        for item in bands_raw:
            threshold = int(item["min_rate"])
            factor = int(item["factor"])
            if not 0 <= threshold <= BASIS or factor <= 0:
                raise ValueError("compliance_bands 阈值须在 0-10000 之间且系数为正")
            if threshold in seen:
                raise ValueError("compliance_bands 阈值不能重复")
            seen.add(threshold)
            bands.append((threshold, factor))
        bands.sort()
        floors = {str(k): int(v) for k, v in raw.get("floors", {}).items()}
        caps = {str(k): int(v) for k, v in raw.get("caps", {}).items()}
        if any(v < 0 for v in floors.values()) or any(v <= 0 for v in caps.values()):
            raise ValueError("保底不能为负，上限必须为正")
        for category in floors.keys() & caps.keys():
            if floors[category] > caps[category]:
                raise ValueError(f"类别 {category} 的保底不能高于上限")
        default_floor = int(raw.get("default_floor", 0))
        default_cap = raw.get("default_cap")
        default_cap = None if default_cap is None else int(default_cap)
        if default_floor < 0 or (default_cap is not None and default_cap <= 0):
            raise ValueError("默认保底不能为负，默认上限必须为正")
        if default_cap is not None and default_floor > default_cap:
            raise ValueError("默认保底不能高于默认上限")
        return cls(dict(tech), tuple(bands), floors, caps, default_floor, default_cap)

    def to_dict(self) -> dict:
        return {
            "tech_factors": dict(self.tech_factors),
            "compliance_bands": [{"min_rate": t, "factor": f} for t, f in self.compliance_bands],
            "floors": dict(self.floors),
            "caps": dict(self.caps),
            "default_floor": self.default_floor,
            "default_cap": self.default_cap,
        }

    def tech_factor(self, route: str) -> int:
        return self.tech_factors.get(route, BASIS)

    def compliance_factor(self, rate: int) -> int:
        factor = BASIS
        for threshold, value in self.compliance_bands:
            if rate >= threshold:
                factor = value
        return factor

    def floor_for(self, category: str) -> int:
        return self.floors.get(category, self.default_floor)

    def cap_for(self, category: str, total_quota: int) -> int:
        """有效上限：未设置类别上限时以总额度为限。"""
        cap = self.caps.get(category, self.default_cap)
        return total_quota if cap is None else min(cap, total_quota)


@dataclass
class EnterpriseAllocation:
    """单家企业的分配结果与原因。"""

    enterprise_id: str
    amount: int
    weight: int
    floor: int
    cap: int
    reason_code: str
    reason_detail: str
    components: dict[str, int] = field(default_factory=dict)


def weight_of(ent: Enterprise, rules: RuleConfig) -> int:
    """分配权重 = 产量 × 技术路线系数 × 履约系数。"""
    return ent.output * rules.tech_factor(ent.tech_route) * rules.compliance_factor(ent.compliance_rate)


def _water_fill(remaining: int, weights: dict[str, int], headroom: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
    """在上限约束下按权重比例分配 remaining 个整数单位。

    返回 (份额字典, 尾差单位字典)。确定规则：
    - 精确份额触及剩余上限的企业先封顶，余量在未触顶企业间继续按比例分配；
    - 无人触顶时先取整数份额，尾差按小数余量降序、企业编号升序逐个分配。
    """
    amounts = {eid: 0 for eid in weights}
    remainder_units = {eid: 0 for eid in weights}
    active = {eid for eid, w in weights.items() if w > 0 and headroom.get(eid, 0) > 0}
    while remaining > 0 and active:
        total_w = sum(weights[e] for e in active)
        capped = [e for e in sorted(active) if Fraction(remaining * weights[e], total_w) >= headroom[e]]
        if capped:
            for eid in capped:
                amounts[eid] += headroom[eid]
                remaining -= headroom[eid]
                active.discard(eid)
            continue
        exact = {e: Fraction(remaining * weights[e], total_w) for e in active}
        shares = {e: x.numerator // x.denominator for e, x in exact.items()}
        leftover = remaining - sum(shares.values())
        for eid, share in shares.items():
            amounts[eid] += share
        # 尾差确定归属：小数余量降序，余量相同按企业编号升序
        order = sorted(active, key=lambda e: (Fraction(shares[e]) - exact[e], e))
        for eid in order[:leftover]:
            amounts[eid] += 1
            remainder_units[eid] += 1
        remaining = 0
    return amounts, remainder_units


def allocate(total_quota: int, enterprises: list[Enterprise], rules: RuleConfig) -> dict[str, EnterpriseAllocation]:
    """对一期额度池执行完整分配，返回每家企业的结果（含未获配原因）。"""
    if total_quota < 0:
        raise ValueError("总额度不能为负")
    results: dict[str, EnterpriseAllocation] = {}
    eligible: list[Enterprise] = []
    for ent in sorted(enterprises, key=lambda e: e.enterprise_id):
        if ent.eligible:
            eligible.append(ent)
        else:
            results[ent.enterprise_id] = EnterpriseAllocation(
                ent.enterprise_id, 0, 0, 0, 0,
                "ineligible",
                f"资格不符（{ent.note or '未通过资格审核'}），不参与本期分配",
            )
    caps = {e.enterprise_id: rules.cap_for(e.category, total_quota) for e in eligible}
    floors = {e.enterprise_id: min(rules.floor_for(e.category), caps[e.enterprise_id]) for e in eligible}
    weights = {e.enterprise_id: weight_of(e, rules) for e in eligible}
    sum_floor = sum(floors.values())

    if eligible and sum_floor >= total_quota:
        # 保底总额覆盖额度池：按保底额比例压缩，尾差规则同上
        amounts, _ = _water_fill(total_quota, floors, floors)
        for ent in eligible:
            eid = ent.enterprise_id
            amount = amounts[eid]
            if amount > 0:
                code = "floor_scaled"
                detail = f"保底总额 {sum_floor} 不低于总额度 {total_quota}，按保底比例压缩获得 {amount}"
            else:
                code = "zero_weight" if weights[eid] == 0 else "pool_exhausted"
                detail = "权重为零，未获分配，进入候补队列" if weights[eid] == 0 else "保底压缩后额度池已分完，进入候补队列"
            results[eid] = EnterpriseAllocation(
                eid, amount, weights[eid], floors[eid], caps[eid], code, detail, {"保底压缩": amount}
            )
        return results

    extra, remainder_units = _water_fill(
        total_quota - sum_floor, weights, {eid: caps[eid] - floors[eid] for eid in caps}
    )
    for ent in eligible:
        eid = ent.enterprise_id
        floor, cap = floors[eid], caps[eid]
        proportional = extra[eid] - remainder_units[eid]
        amount = floor + extra[eid]
        components = {"保底": floor, "比例": proportional, "尾差": remainder_units[eid]}
        if amount == 0:
            if weights[eid] == 0:
                code, detail = "zero_weight", "产量或系数为零，未获分配，进入候补队列"
            else:
                code, detail = "pool_exhausted", "额度池已分完，未获分配，进入候补队列"
        elif extra[eid] > 0 and amount == cap and cap < total_quota:
            code = "capped"
            detail = f"保底 {floor} + 比例 {proportional} + 尾差 {remainder_units[eid]}，共 {amount}，已达类别上限 {cap}"
        else:
            code = "allocated"
            detail = f"保底 {floor} + 比例 {proportional} + 尾差 {remainder_units[eid]}，共 {amount}"
        results[eid] = EnterpriseAllocation(eid, amount, weights[eid], floor, cap, code, detail, components)
    return results


def waitlist_order(results: dict[str, EnterpriseAllocation]) -> list[str]:
    """候补队列：未获分配或保底未足额的合格企业，按权重降序、编号升序排列。"""
    pending = [
        r for r in results.values()
        if r.reason_code in ("zero_weight", "pool_exhausted")
        or (r.reason_code == "floor_scaled" and r.amount < r.floor)
    ]
    return [r.enterprise_id for r in sorted(pending, key=lambda r: (-r.weight, r.enterprise_id))]


def redistribute(
    freed: int,
    candidate_ids: list[str],
    weights: dict[str, int],
    floors: dict[str, int],
    caps: dict[str, int],
    holdings: dict[str, int],
    waitlist: list[str],
) -> dict[str, int]:
    """释放额度的确定递补规则。

    1. 候补企业按队列顺序依次补足保底（不超过上限与剩余释放量）；
    2. 仍有剩余时，在全部候选企业间按权重比例分配（含上限与尾差规则）。
    """
    grants: dict[str, int] = {}
    remaining = freed
    candidates = set(candidate_ids)
    for eid in waitlist:
        if remaining <= 0:
            break
        if eid not in candidates:
            continue
        target = min(floors.get(eid, 0), caps.get(eid, 0)) - holdings.get(eid, 0)
        if target <= 0:
            continue
        give = min(target, remaining)
        grants[eid] = give
        remaining -= give
    if remaining > 0:
        headroom = {eid: caps.get(eid, 0) - holdings.get(eid, 0) - grants.get(eid, 0) for eid in candidate_ids}
        extra, _ = _water_fill(remaining, {eid: weights.get(eid, 0) for eid in candidate_ids}, headroom)
        for eid, value in extra.items():
            if value > 0:
                grants[eid] = grants.get(eid, 0) + value
    return grants
