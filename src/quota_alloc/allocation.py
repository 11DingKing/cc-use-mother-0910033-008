"""确定性配额分配内核（纯整数精确运算）。

设计原则
========
1. 全程整数运算：权重以"分"为单位（SCALE = 10000），水填过程中判断是否触顶
   一律交叉相乘（``bound * W <= remaining * score``），整数化只对"最终未触顶
   集合"做一次最大余数法。全程不出现 float，尾差归属不受浮点 ulp 影响。
2. 分配顺序（保底→上限约束比例→尾差）与平局裁决（企业编号升序）全部确定，
   同输入必然同输出，便于复核与审计。
3. 每家企业的最终结果都附带原因链（入选/落选/保底/上限/尾差等）。

两阶段水填算法（最高费率水填，Highest-Rates Water-Filling）
-----------------------------------------------------------
- 阶段一：按规模给最低保障 floor（小型企业也有基本额度）；
- 阶段二：在剩余池内按得分权重分配，每家企业有有效上限
  ``min(自身上限, 保底+剩余)``。迭代中按公共水位用交叉相乘找出本批触顶者，
  触顶者整额冻结，余量在未冻结者间继续重分；直到无人触顶或池耗尽；
- 阶段三：对最终未触顶集合做最大余数法——先下取整，余量按余数
  （并列按企业编号升序）逐分发放，尾差归属因此完全确定；全员触顶后仍有
  余量则确定留存，不强行发放。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

SCALE = 10_000  # 权重系数的定点精度：1 分（0.0001）

# 原因码（稳定字符串，供 API 与审计直接引用）
R_NOT_ELIGIBLE = "not_eligible"          # 不在资格名单
R_ELIGIBILITY_REVOKED = "revoked"        # 发布后资格被撤销
R_RELINQUISHED = "relinquished"          # 主动放弃
R_FLOOR_GUARANTEE = "floor_guarantee"    # 仅获得/含保底
R_PRO_RATA = "pro_rata"                  # 按得分比例分得
R_CAPPED = "capped"                      # 触及上限
R_ROUND_UP = "round_up"                  # 最大余数法多得 1 分尾差
R_ROUND_DOWN = "round_down"              # 最大余数法舍入
R_ZERO_POOL = "zero_pool"                # 池子在保底后已无余量
R_ZERO_SCORE = "zero_score"              # 得分 0，比例阶段不参与
R_WAITLIST_PROMOTED = "waitlist_promoted"  # 候补递补获得额度
R_WAITLIST = "waitlist"                  # 列候补但无额可递补
R_QUOTA_EXHAUSTED = "quota_exhausted"    # 总额度已分尽
R_TOTAL_TOO_SMALL = "total_too_small"    # 总额度不足以覆盖保底之和
R_PROMOTION_FLOOR = "promotion_floor"    # 候补递补先补足保底
R_RESIDUAL = "residual"                  # 参与释放额度的尾差重分
R_RESIDUAL_RETAINED = "residual_retained"  # 全员触顶，余量留存
R_REASON_LABELS = {
    R_NOT_ELIGIBLE: "不具备资格",
    R_ELIGIBILITY_REVOKED: "资格已撤销",
    R_RELINQUISHED: "企业已放弃",
    R_FLOOR_GUARANTEE: "适用最低保障",
    R_PRO_RATA: "按产量/路线/履约得分比例分配",
    R_CAPPED: "触及上限被截断",
    R_ROUND_UP: "尾差最大余数法递补 +1",
    R_ROUND_DOWN: "尾差舍入未获得 +1",
    R_ZERO_POOL: "保底后无剩余池",
    R_ZERO_SCORE: "路线系数或得分为 0",
    R_WAITLIST_PROMOTED: "候补递补获得额度",
    R_WAITLIST: "候补身份，初始轮空",
    R_QUOTA_EXHAUSTED: "可分配额度已尽",
    R_TOTAL_TOO_SMALL: "总额度不足以覆盖全部保底",
    R_PROMOTION_FLOOR: "候补递补按规模保底整额补足",
    R_RESIDUAL: "参与释放额度的尾差比例重分",
    R_RESIDUAL_RETAINED: "所有在册企业均触上限，余量留存",
}


@dataclass(frozen=True)
class Subject:
    """资格快照中的一家企业（不可变，保证试算可复现）。"""

    subject_id: str
    name: str
    size: str                 # large / medium / small / micro
    tech_route: str           # 技术路线代码
    output: int               # 产量（非负整数，单位自定）
    compliance_score: int     # 历史履约分 0..100
    eligible: bool = True     # 快照时点是否具备资格
    waitlist: bool = False    # 是否候补企业

    def __post_init__(self) -> None:
        if not self.subject_id or not isinstance(self.subject_id, str):
            raise ValueError("企业编号不能为空")
        if self.output < 0:
            raise ValueError(f"{self.subject_id}: 产量不能为负")
        if not 0 <= self.compliance_score <= 100:
            raise ValueError(f"{self.subject_id}: 履约分须在 0..100")
        if self.size not in ("large", "medium", "small", "micro"):
            raise ValueError(f"{self.subject_id}: 未知规模 {self.size}")


@dataclass(frozen=True)
class RuleParams:
    """规则版本参数。所有系数为 SCALE 定点整数。"""

    output_weight: int
    route_coeff: dict[str, int]
    compliance_weight: int
    floors: dict[str, int]
    caps: dict[str, int]

    def floor_for(self, size: str) -> int:
        return int(self.floors.get(size, 0))

    def cap_for(self, size: str) -> int:
        return int(self.caps.get(size, 10**18))


def _coeff(value: float | int, name: str) -> int:
    """把 0.0001 精度的浮点/整数系数转成定点整数，并拒绝过细精度。"""
    scaled = int(round(float(value) * SCALE))
    if scaled < 0:
        raise ValueError(f"{name} 不能为负")
    if abs(scaled / SCALE - float(value)) > 1e-9 + 1e-12:
        raise ValueError(f"{name} 精度超过 {SCALE} 分位")
    return scaled


def validate_rule_params(
    *,
    output_weight: float | int,
    route_coeff: dict[str, float | int],
    compliance_weight: float | int,
    floors: dict[str, int],
    caps: dict[str, int],
) -> dict:
    """校验并归一化规则参数（浮点系数 → 定点整数）。"""
    sizes = ("large", "medium", "small", "micro")
    for size in sizes:
        if size not in floors:
            raise ValueError(f"缺少规模 {size} 的保底")
        if size not in caps:
            raise ValueError(f"缺少规模 {size} 的上限")
    for size in sizes:
        f, c = int(floors[size]), int(caps[size])
        if f < 0 or c < 0:
            raise ValueError(f"规模 {size} 的保底/上限不能为负")
        if f > c:
            raise ValueError(f"规模 {size} 的保底 {f} 超过上限 {c}")
    if not route_coeff:
        raise ValueError("至少配置一条技术路线系数")
    normalized_routes = {k: _coeff(v, f"路线系数 {k}") for k, v in route_coeff.items()}
    ow = _coeff(output_weight, "产量权重")
    cw = _coeff(compliance_weight, "履约权重")
    if ow == 0 and cw == 0:
        raise ValueError("产量权重与履约权重不能同时为 0")
    return {
        "output_weight": ow,
        "route_coeff": normalized_routes,
        "compliance_weight": cw,
        "floors": {k: int(v) for k, v in floors.items()},
        "caps": {k: int(v) for k, v in caps.items()},
    }


def make_rule_params(
    *,
    output_weight: float | int,
    route_coeff: dict[str, float | int],
    compliance_weight: float | int,
    floors: dict[str, int],
    caps: dict[str, int],
) -> RuleParams:
    """构造经过校验的 RuleParams。"""
    return RuleParams(**validate_rule_params(
        output_weight=output_weight,
        route_coeff=route_coeff,
        compliance_weight=compliance_weight,
        floors=floors,
        caps=caps,
    ))


def score_int(subject: Subject, rules: RuleParams) -> int:
    """定点整数得分 = (产量*产量权重 + 履约分*履约权重) * 路线系数 / SCALE。"""
    route = rules.route_coeff.get(subject.tech_route, 0)
    raw = subject.output * rules.output_weight + subject.compliance_score * rules.compliance_weight
    return raw * route // SCALE


@dataclass
class AllocationLine:
    subject_id: str
    allocated: int
    score: int
    floor: int
    cap: int
    capped: bool
    reasons: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class _WaterFill:
    """精确水填结果。"""

    adds: dict[str, int]          # 各主体整数增量
    capped: set[str]              # 触顶主体
    bumped: set[str]              # 最大余数法 +1 的主体
    residue_num: dict[str, int]   # 最终未触顶集合中各主体余数分子（分母为 denom）
    denom: int                    # 余数分母（最终未触顶集合得分之和）
    leftover: int                 # 全员触顶后确定留存的余量


def _exact_waterfill(
    scores: dict[str, int], pool: int, bounds: dict[str, int],
    ordered_ids: Sequence[str],
) -> _WaterFill:
    """带上限的纯整数水填 + 最大余数法整数化。

    每轮按公共水位 remaining/W 用交叉相乘判断触顶（bound*W <= remaining*score），
    一批触顶者整额冻结后继续；无人触顶时对当前集合做一次最大余数法并结束。
    """
    adds = {k: 0 for k in ordered_ids}
    capped: set[str] = set()
    bumped: set[str] = set()
    residue_num = {k: 0 for k in ordered_ids}
    active = [k for k in ordered_ids if scores.get(k, 0) > 0 and bounds.get(k, 0) > 0]
    remaining = pool
    denom = 1

    while active and remaining > 0:
        weight = sum(scores[k] for k in active)
        frozen = [k for k in active
                  if bounds[k] * weight <= remaining * scores[k]]
        if not frozen:
            # 无人触顶：余量在 active 内一次分尽（最大余数法）。
            # bound > 精确份额（严格）对所有人生成立，故 floor(份额)+1 <= bound，
            # 每一单位尾差都能合法发放。
            base = {k: (remaining * scores[k]) // weight for k in active}
            nums = {k: remaining * scores[k] - base[k] * weight for k in active}
            units = remaining - sum(base.values())
            for k in sorted(active, key=lambda x: (-nums[x], x)):
                if units <= 0:
                    break
                bumped.add(k)
                units -= 1
            for k in active:
                adds[k] += base[k] + (1 if k in bumped else 0)
                residue_num[k] = nums[k]
            denom = weight
            remaining = 0
            break
        # 触顶者整额冻结（同批金额互不影响，按编号升序仅为过程确定性）
        for k in sorted(frozen):
            adds[k] += bounds[k]
            remaining -= bounds[k]
            active.remove(k)
            capped.add(k)

    return _WaterFill(adds=adds, capped=capped, bumped=bumped,
                      residue_num=residue_num, denom=denom, leftover=remaining)


@dataclass
class AllocationResult:
    total_quota: int
    allocated_sum: int
    remainder: int              # 未分出的尾差（全员触顶或保底超总额时非 0）
    feasible: bool              # 总额度是否足以覆盖保底之和
    lines: list[AllocationLine]
    floor_shortfall: dict[str, int]

    @property
    def by_id(self) -> dict[str, AllocationLine]:
        return {line.subject_id: line for line in self.lines}

    def explain(self) -> list[dict]:
        return [
            {
                "subject_id": ln.subject_id,
                "allocated": ln.allocated,
                "score": ln.score,
                "floor": ln.floor,
                "cap": ln.cap,
                "capped": ln.capped,
                "reasons": ln.reasons,
                "note": ln.note,
            }
            for ln in self.lines
        ]


def allocate(
    subjects: Iterable[Subject],
    rules: RuleParams,
    total_quota: int,
) -> AllocationResult:
    """执行一次确定性试算分配。

    结果始终包含每家企业一行（含不合格与候补），allocated=0 时同样给出原因，
    满足"API 返回每家企业获得或未获得额度的原因"。
    """
    if total_quota < 0:
        raise ValueError("总额度不能为负")
    subjects = list(subjects)
    ids = [s.subject_id for s in subjects]
    if len(ids) != len(set(ids)):
        raise ValueError("企业编号重复")

    # 企业编号升序是所有平局的最终裁决顺序。
    ordered = sorted(subjects, key=lambda s: s.subject_id)

    lines: list[AllocationLine] = []
    active: list[Subject] = []
    floor_sum = 0
    floor_shortfall: dict[str, int] = {}

    for s in ordered:
        floor = rules.floor_for(s.size)
        cap = rules.cap_for(s.size)
        score = score_int(s, rules) if s.eligible and not s.waitlist else 0
        if not s.eligible:
            lines.append(AllocationLine(s.subject_id, 0, score, floor, cap, False,
                                        [R_NOT_ELIGIBLE], "资格快照标记为不合格，不参与分配"))
        elif s.waitlist:
            lines.append(AllocationLine(s.subject_id, 0, score, floor, cap, False,
                                        [R_WAITLIST], "候补企业，初始分配不占额度，待递补"))
        else:
            active.append(s)
            floor_sum += floor

    feasible = total_quota >= floor_sum
    allocations: dict[str, int] = {}
    capped_flags: dict[str, bool] = {}
    bumped: set[str] = set()
    residue_num: dict[str, int] = {}
    remainder = 0

    if not feasible:
        # 极端情形：保底之和超过总额度。确定规则——优先小企业
        # （micro→small→medium→large）足额保底，同档按企业编号升序逐分发放。
        remaining = total_quota
        rank = {"micro": 0, "small": 1, "medium": 2, "large": 3}
        for s in sorted(active, key=lambda s: (rank[s.size], s.subject_id)):
            floor = rules.floor_for(s.size)
            give = min(floor, remaining)
            allocations[s.subject_id] = give
            capped_flags[s.subject_id] = False
            residue_num[s.subject_id] = 0
            if give < floor:
                floor_shortfall[s.subject_id] = floor - give
            remaining -= give
        remainder = remaining
    else:
        pool_after_floor = total_quota - floor_sum
        scores = {s.subject_id: score_int(s, rules) for s in active}
        bounds = {s.subject_id: max(0, rules.cap_for(s.size) - rules.floor_for(s.size))
                  for s in active}
        wf = _exact_waterfill(
            scores, pool_after_floor, bounds, [s.subject_id for s in active],
        )
        for s in active:
            k = s.subject_id
            allocations[k] = rules.floor_for(s.size) + wf.adds.get(k, 0)
            capped_flags[k] = k in wf.capped or (bounds[k] <= 0 and scores[k] > 0)
            residue_num[k] = wf.residue_num.get(k, 0)
        bumped = wf.bumped
        remainder = wf.leftover

    floors_by_id = {s.subject_id: rules.floor_for(s.size) for s in active}
    caps_by_id = {s.subject_id: rules.cap_for(s.size) for s in active}
    scores_by_id = {s.subject_id: score_int(s, rules) for s in active}
    pool_after_floor = max(0, total_quota - floor_sum)
    allocated_sum = 0

    for s in active:
        k = s.subject_id
        alloc = allocations[k]
        allocated_sum += alloc
        reasons: list[str] = []
        if not feasible:
            reasons.append(R_TOTAL_TOO_SMALL)
            if floors_by_id[k] > 0:
                reasons.append(R_FLOOR_GUARANTEE)
        else:
            extra = alloc - floors_by_id[k]
            if alloc == floors_by_id[k] and floors_by_id[k] > 0:
                reasons.append(R_FLOOR_GUARANTEE)
            if extra > 0:
                reasons.append(R_PRO_RATA)
            if capped_flags[k]:
                reasons.append(R_CAPPED)
            if scores_by_id[k] == 0 and pool_after_floor > 0:
                reasons.append(R_ZERO_SCORE)
            if k in bumped:
                reasons.append(R_ROUND_UP)
            elif residue_num.get(k, 0) > 0:
                reasons.append(R_ROUND_DOWN)
            if pool_after_floor == 0 and floors_by_id[k] == 0:
                reasons.append(R_ZERO_POOL)
        note_parts = []
        if capped_flags[k]:
            note_parts.append(f"触及规模上限 {caps_by_id[k]}")
        if k in bumped:
            note_parts.append("最大余数法尾差多得 1 单位")
        if not note_parts and alloc == 0:
            note_parts.append("得分或剩余池为 0，未获比例额度")
        lines.append(AllocationLine(
            k, alloc, scores_by_id[k], floors_by_id[k], caps_by_id[k],
            capped_flags[k], reasons, "；".join(note_parts),
        ))

    lines.sort(key=lambda ln: ln.subject_id)
    return AllocationResult(
        total_quota=total_quota,
        allocated_sum=allocated_sum,
        remainder=remainder,
        feasible=feasible,
        lines=lines,
        floor_shortfall=floor_shortfall,
    )


def build_line_reasons(line: AllocationLine) -> dict[str, object]:
    """把单个结果行转成 API 友好的原因解释。"""
    return {
        "subject_id": line.subject_id,
        "allocated": line.allocated,
        "reasons": line.reasons,
        "reason_text": [R_REASON_LABELS.get(r, r) for r in line.reasons],
        "note": line.note,
    }


# ---------------------------------------------------------------------
# 发布后：放弃 / 资格撤销 → 候补递补 → 尾差重分（同一套确定规则）
# ---------------------------------------------------------------------

STATUS_ACTIVE = "active"
STATUS_RELINQUISHED = "relinquished"
STATUS_REVOKED = "revoked"
STATUS_WAITLIST = "waitlist"


@dataclass
class ReallocationLine:
    subject_id: str
    before: int
    after: int
    delta: int
    reasons: list[str]
    note: str = ""


@dataclass
class ReallocationResult:
    released_subject: str
    released_amount: int
    distributed: int
    retained: int
    promoted: list[str]
    lines: dict[str, ReallocationLine]
    events: list[dict]

    def changes(self) -> list[dict]:
        return [
            {
                "subject_id": ln.subject_id,
                "before": ln.before,
                "after": ln.after,
                "delta": ln.delta,
                "reasons": ln.reasons,
                "reason_text": [R_REASON_LABELS.get(r, r) for r in ln.reasons],
                "note": ln.note,
            }
            for ln in sorted(self.lines.values(), key=lambda x: x.subject_id)
        ]


def reallocate_release(
    *,
    subjects: Sequence[Subject],
    rules: RuleParams,
    current: dict[str, int],
    released_subject: str,
    released_amount: int,
    statuses: dict[str, str],
) -> ReallocationResult:
    """发布后释放额度（放弃或撤销）的确定式重分。

    固定顺序：
      1) 释放主体额度清零，登记原因（relinquished / revoked）；
      2) 候补递补：按企业编号升序，逐个以"规模保底整额"补足，余量不足整额
         保底即关闭递补通道（确定，不切分保底）；
      3) 尾差重分：剩余池在在册活跃企业（含新递补者）之间按得分比例、受
         各自上限余量约束做纯整数水填与最大余数法整数化；
      4) 全员触顶后仍有剩余 → 留存尾差，登记事件，不强行发放。

    statuses 中 released_subject 已由调用方置为 relinquished/revoked；
    候补企业为 waitlist；其余 active。
    """
    if released_amount < 0:
        raise ValueError("释放额度不能为负")
    by_id = {s.subject_id: s for s in subjects}
    if released_subject not in by_id:
        raise ValueError(f"未知企业 {released_subject}")

    before = {sid: int(current.get(sid, 0)) for sid in by_id}
    after = dict(before)
    reasons: dict[str, list[str]] = {sid: [] for sid in by_id}
    notes: dict[str, str] = {sid: "" for sid in by_id}
    events: list[dict] = []

    release_status = statuses.get(released_subject, STATUS_RELINQUISHED)
    if release_status not in (STATUS_RELINQUISHED, STATUS_REVOKED):
        release_status = STATUS_RELINQUISHED
    after[released_subject] = 0
    reasons[released_subject].append(
        R_RELINQUISHED if release_status == STATUS_RELINQUISHED else R_ELIGIBILITY_REVOKED
    )
    notes[released_subject] = "释放全部已发额度"
    events.append({"step": "release", "subject_id": released_subject,
                   "amount": released_amount, "rule": release_status})

    pool = released_amount
    promoted: list[str] = []

    # ---- 阶段一：候补递补保底（企业编号升序，整额保底，不足即停）------
    waitlist_ids = sorted(
        sid for sid, st in statuses.items()
        if st == STATUS_WAITLIST and sid in by_id and by_id[sid].eligible
    )
    promotions_open = True
    for sid in waitlist_ids:
        need = rules.floor_for(by_id[sid].size) - after[sid]
        if need <= 0:
            # 保底为 0（或已足）：无需保底即可在册，标记递补并进入尾差阶段
            promoted.append(sid)
            reasons[sid].append(R_WAITLIST_PROMOTED)
            notes[sid] = "候补递补（规模保底为 0），参与尾差比例重分"
            events.append({"step": "promotion_floor", "subject_id": sid,
                           "amount": 0, "rule": R_PROMOTION_FLOOR})
            continue
        if not promotions_open or pool < need:
            # 确定规则：保底只整额发放，不切分；递补通道关闭，
            # 后续候补同样不再递补，余量进入在册企业尾差重分或留存。
            promotions_open = False
            reasons[sid].extend([R_WAITLIST, R_QUOTA_EXHAUSTED])
            notes[sid] = (f"释放池余量 {pool} 不足规模保底 {need}，"
                          "保底整额规则下本次不递补，继续列候补")
            events.append({"step": "waitlist_skip", "subject_id": sid,
                           "amount": 0, "rule": R_QUOTA_EXHAUSTED})
            continue
        after[sid] += need
        pool -= need
        promoted.append(sid)
        reasons[sid].extend([R_WAITLIST_PROMOTED, R_PROMOTION_FLOOR])
        notes[sid] = f"候补递补，按规模保底整额补足 {need}"
        events.append({"step": "promotion_floor", "subject_id": sid,
                       "amount": need, "rule": R_PROMOTION_FLOOR})

    promoted_set = set(promoted)

    # ---- 阶段二：尾差按比例重分（在册活跃企业 + 已递补候补）------------
    def _in_residual_pool(sid: str) -> bool:
        if sid == released_subject:
            return False
        st = statuses.get(sid, STATUS_ACTIVE)
        if st == STATUS_ACTIVE:
            return by_id[sid].eligible
        return st == STATUS_WAITLIST and sid in promoted_set

    active_ids = sorted(sid for sid in by_id if _in_residual_pool(sid))
    scores = {sid: score_int(by_id[sid], rules) for sid in active_ids}
    headroom = {sid: max(0, rules.cap_for(by_id[sid].size) - after[sid])
                for sid in active_ids}

    retained = pool
    if pool > 0 and any(scores[sid] > 0 and headroom[sid] > 0 for sid in active_ids):
        wf = _exact_waterfill(scores, pool, headroom, active_ids)
        for sid in active_ids:
            add = wf.adds.get(sid, 0)
            if add <= 0:
                continue
            after[sid] += add
            if statuses.get(sid, STATUS_ACTIVE) == STATUS_WAITLIST and sid not in promoted_set:
                promoted.append(sid)
                promoted_set.add(sid)
                reasons[sid].append(R_WAITLIST_PROMOTED)
            reasons[sid].append(R_RESIDUAL)
            if sid in wf.bumped:
                reasons[sid].append(R_ROUND_UP)
            events.append({"step": "residual", "subject_id": sid,
                           "amount": add, "rule": R_RESIDUAL})
        retained = wf.leftover
        if retained > 0:
            events.append({"step": "residual_retained", "subject_id": None,
                           "amount": retained, "rule": R_RESIDUAL_RETAINED})

    # 未获增量的活跃企业：逐户说明轮空原因（审计要求）
    for sid in active_ids:
        if after[sid] == before[sid] and not reasons[sid]:
            if scores[sid] == 0:
                reasons[sid].append(R_ZERO_SCORE)
                notes[sid] = "路线系数或得分为 0，不参与尾差比例重分"
            elif headroom[sid] <= 0:
                reasons[sid].append(R_CAPPED)
                notes[sid] = "已在规模上限，无上限余量承接释放额度"
            else:
                reasons[sid].append(R_QUOTA_EXHAUSTED)
                notes[sid] = "释放池经候补保底与尾差重分后已尽"

    total_gain = sum(max(0, after[s] - before[s]) for s in by_id if s != released_subject)
    retained = max(retained, released_amount - total_gain)
    if retained > 0 and not any(e["step"] == "residual_retained" for e in events):
        events.append({"step": "retained", "subject_id": None,
                       "amount": retained, "rule": R_RESIDUAL_RETAINED})
    promoted.sort()

    lines = {
        sid: ReallocationLine(sid, before[sid], after[sid], after[sid] - before[sid],
                              reasons[sid], notes[sid])
        for sid in by_id
        if sid == released_subject or after[sid] != before[sid] or reasons[sid]
    }
    return ReallocationResult(
        released_subject=released_subject,
        released_amount=released_amount,
        distributed=released_amount - retained,
        retained=retained,
        promoted=promoted,
        lines=lines,
        events=events,
    )
