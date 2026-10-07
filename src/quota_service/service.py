"""业务服务层：资格快照、规则版本、试算方案、正式发布与后续确定规则调整。"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .allocation import (
    BASIS,
    Enterprise,
    EnterpriseAllocation,
    RuleConfig,
    allocate,
    redistribute,
    waitlist_order,
)
from .storage import connect

# 方案状态（对应领域契约状态机）：草稿 → 已确认 → 执行中 → 已封存
STATUS_DRAFT = "草稿"
STATUS_CONFIRMED = "已确认"
STATUS_RUNNING = "执行中"
STATUS_SEALED = "已封存"
PUBLISHED_STATUSES = (STATUS_CONFIRMED, STATUS_RUNNING)

ENTRY_INITIAL = "初始分配"
ENTRY_SURRENDER = "放弃"
ENTRY_REVOCATION = "撤销"
ENTRY_BACKFILL = "递补"


class ServiceError(Exception):
    """可预期的业务错误，status 为 HTTP 状态码。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_enterprise(raw: dict) -> Enterprise:
    if not isinstance(raw, dict):
        raise ServiceError(400, f"企业数据必须是对象：{raw!r}")
    try:
        output = int(raw["output"])
        rate = int(raw["compliance_rate"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ServiceError(400, f"企业数据缺少字段或数值非法：{raw!r}") from exc
    if output < 0:
        raise ServiceError(400, "产量不能为负")
    if not 0 <= rate <= BASIS:
        raise ServiceError(400, "履约率须在 0-10000 基点之间")
    enterprise_id = str(raw.get("enterprise_id") or "").strip()
    if not enterprise_id:
        raise ServiceError(400, "企业编号不能为空")
    return Enterprise(
        enterprise_id=enterprise_id,
        name=str(raw.get("name") or enterprise_id),
        category=str(raw.get("category") or "未分类"),
        output=output,
        tech_route=str(raw.get("tech_route") or "未标注"),
        compliance_rate=rate,
        eligible=bool(raw.get("eligible", True)),
        note=str(raw.get("note") or ""),
    )


class QuotaService:
    """配额分配核心服务。所有写操作在单个事务内完成，保证原子性。"""

    def __init__(self, db_path: str | Path = ":memory:"):
        self._conn = connect(db_path)
        self._lock = threading.RLock()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # 资格快照与规则版本
    # ------------------------------------------------------------------
    def create_snapshot(self, period: str, enterprises: list[dict]) -> dict:
        """保存一期资格快照；快照保存后不可修改，保证试算可复现。"""
        if not period:
            raise ServiceError(400, "周期不能为空")
        if not isinstance(enterprises, list) or not enterprises:
            raise ServiceError(400, "快照至少包含一家企业")
        parsed = [_parse_enterprise(item) for item in enterprises]
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO snapshots(period, created_at) VALUES (?, ?)", (period, _now())
            )
            snapshot_id = cur.lastrowid
            try:
                self._conn.executemany(
                    """INSERT INTO snapshot_enterprises
                       (snapshot_id, enterprise_id, name, category, output, tech_route,
                        compliance_rate, eligible, note)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    [
                        (snapshot_id, e.enterprise_id, e.name, e.category, e.output,
                         e.tech_route, e.compliance_rate, int(e.eligible), e.note)
                        for e in parsed
                    ],
                )
            except sqlite3.IntegrityError as exc:
                raise ServiceError(400, "同一快照内企业编号不能重复") from exc
        return {"snapshot_id": snapshot_id, "period": period, "enterprise_count": len(parsed)}

    def create_rule_version(self, name: str, config: dict) -> dict:
        """保存一个分配规则版本（系数、保底、上限），保存后不可修改。"""
        if not name:
            raise ServiceError(400, "规则版本名称不能为空")
        try:
            parsed = RuleConfig.from_dict(config)
        except ValueError as exc:
            raise ServiceError(400, str(exc)) from exc
        with self._lock, self._conn:
            try:
                cur = self._conn.execute(
                    "INSERT INTO rule_versions(name, created_at, config_json) VALUES (?,?,?)",
                    (name, _now(), json.dumps(parsed.to_dict(), ensure_ascii=False, sort_keys=True)),
                )
            except sqlite3.IntegrityError as exc:
                raise ServiceError(409, f"规则版本名称已存在：{name}") from exc
        return {"rule_version_id": cur.lastrowid, "name": name}

    # ------------------------------------------------------------------
    # 试算方案
    # ------------------------------------------------------------------
    def run_trial(self, period: str, snapshot_id: int, rule_version_id: int, total_quota: int) -> dict:
        """基于指定快照与规则版本试算一个方案；同一周期可保留多个试算方案互相对比。"""
        total_quota = int(total_quota)
        if total_quota < 0:
            raise ServiceError(400, "总额度不能为负")
        with self._lock:
            snapshot = self._snapshot_or_404(snapshot_id)
            rules = self._rules_or_404(rule_version_id)
            outcome = allocate(total_quota, snapshot, rules)
            with self._conn:
                cur = self._conn.execute(
                    """INSERT INTO scenarios(period, snapshot_id, rule_version_id, total_quota, status, created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (period, snapshot_id, rule_version_id, total_quota, STATUS_DRAFT, _now()),
                )
                scenario_id = cur.lastrowid
                self._conn.executemany(
                    """INSERT INTO scenario_results
                       (scenario_id, enterprise_id, amount, weight, floor, cap,
                        reason_code, reason_detail, components_json)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    [
                        (scenario_id, r.enterprise_id, r.amount, r.weight, r.floor, r.cap,
                         r.reason_code, r.reason_detail,
                         json.dumps(r.components, ensure_ascii=False, sort_keys=True))
                        for r in outcome.values()
                    ],
                )
        return self.get_scenario(scenario_id)

    def get_scenario(self, scenario_id: int) -> dict:
        with self._lock:
            row = self._conn.execute("SELECT * FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
            if row is None:
                raise ServiceError(404, f"试算方案不存在：{scenario_id}")
            results = self._results_of(scenario_id)
        waitlist = waitlist_order(
            {r["enterprise_id"]: EnterpriseAllocation(
                r["enterprise_id"], r["amount"], r["weight"], r["floor"], r["cap"],
                r["reason_code"], r["reason_detail"]) for r in results}
        )
        return {
            "id": row["id"],
            "period": row["period"],
            "status": row["status"],
            "total_quota": row["total_quota"],
            "snapshot_id": row["snapshot_id"],
            "rule_version_id": row["rule_version_id"],
            "results": results,
            "waitlist": waitlist,
        }

    def diff_scenarios(self, a_id: int, b_id: int) -> dict:
        """对比两个试算方案，逐家解释差异来源。"""
        with self._lock:
            a = self.get_scenario(a_id)
            b = self.get_scenario(b_id)
            rules_a = self._rules_or_404(a["rule_version_id"])
            rules_b = self._rules_or_404(b["rule_version_id"])
            facts_a = {e.enterprise_id: e for e in self._snapshot_or_404(a["snapshot_id"])}
            facts_b = {e.enterprise_id: e for e in self._snapshot_or_404(b["snapshot_id"])}
        res_a = {r["enterprise_id"]: r for r in a["results"]}
        res_b = {r["enterprise_id"]: r for r in b["results"]}
        items = []
        for eid in sorted(set(res_a) | set(res_b)):
            ra = res_a.get(eid)
            rb = res_b.get(eid)
            amount_a = ra["amount"] if ra else 0
            amount_b = rb["amount"] if rb else 0
            explanations = self._explain(eid, facts_a.get(eid), facts_b.get(eid),
                                         rules_a, rules_b, a["total_quota"], b["total_quota"],
                                         ra, rb)
            items.append({
                "enterprise_id": eid,
                "amount_a": amount_a,
                "amount_b": amount_b,
                "delta": amount_b - amount_a,
                "explanations": explanations,
            })
        return {
            "a": a_id, "b": b_id,
            "total_quota_a": a["total_quota"], "total_quota_b": b["total_quota"],
            "items": items,
        }

    @staticmethod
    def _explain(eid, fa, fb, rules_a, rules_b, total_a, total_b, ra, rb) -> list[str]:
        notes: list[str] = []
        if fa is not None and fb is not None:
            if fa.eligible != fb.eligible:
                notes.append(f"资格状态变化：{'合格' if fa.eligible else '不合格'}→{'合格' if fb.eligible else '不合格'}")
            if fa.output != fb.output:
                notes.append(f"产量 {fa.output}→{fb.output}")
            if fa.tech_route != fb.tech_route:
                notes.append(f"技术路线「{fa.tech_route}」→「{fb.tech_route}」")
            if fa.compliance_rate != fb.compliance_rate:
                notes.append(f"履约率 {fa.compliance_rate / 100:.2f}%→{fb.compliance_rate / 100:.2f}%")
        fact = fb or fa
        if fact is not None:
            ta, tb = rules_a.tech_factor(fact.tech_route), rules_b.tech_factor(fact.tech_route)
            if ta != tb:
                notes.append(f"技术路线「{fact.tech_route}」系数 {ta / BASIS:g}→{tb / BASIS:g}")
            ca, cb = rules_a.compliance_factor(fact.compliance_rate), rules_b.compliance_factor(fact.compliance_rate)
            if ca != cb:
                notes.append(f"履约系数 {ca / BASIS:g}→{cb / BASIS:g}（履约率 {fact.compliance_rate / 100:.2f}%）")
            fla, flb = rules_a.floor_for(fact.category), rules_b.floor_for(fact.category)
            if fla != flb:
                notes.append(f"类别「{fact.category}」保底 {fla}→{flb}")
            cpa = rules_a.cap_for(fact.category, total_a)
            cpb = rules_b.cap_for(fact.category, total_b)
            if cpa != cpb:
                notes.append(f"类别「{fact.category}」上限 {cpa}→{cpb}")
        if total_a != total_b:
            notes.append(f"总额度 {total_a}→{total_b}")
        if ra and rb and ra["reason_code"] != rb["reason_code"]:
            notes.append(f"结果类型 {ra['reason_code']}→{rb['reason_code']}")
        delta = (rb["amount"] if rb else 0) - (ra["amount"] if ra else 0)
        if not notes and delta != 0:
            notes.append("他企业权重或资格变化导致相对份额变化")
        if not notes:
            notes.append("无变化")
        return notes

    # ------------------------------------------------------------------
    # 正式发布（原子写入配额分录）
    # ------------------------------------------------------------------
    def publish(self, scenario_id: int) -> dict:
        """正式发布试算方案：在单个事务内写入全部配额分录并翻转状态。

        同一周期只允许一个正式发布方案；任何一步失败整体回滚。
        """
        with self._lock:
            row = self._conn.execute("SELECT * FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
            if row is None:
                raise ServiceError(404, f"试算方案不存在：{scenario_id}")
            if row["status"] != STATUS_DRAFT:
                raise ServiceError(409, f"只能发布草稿状态的方案，当前状态：{row['status']}")
            results = self._results_of(scenario_id)
            now = _now()
            try:
                with self._conn:
                    cur = self._conn.execute(
                        "UPDATE scenarios SET status=?, published_at=? WHERE id=? AND status=?",
                        (STATUS_CONFIRMED, now, scenario_id, STATUS_DRAFT),
                    )
                    if cur.rowcount != 1:
                        raise ServiceError(409, "方案状态已变化，发布失败")
                    self._conn.executemany(
                        """INSERT INTO quota_entries
                           (period, scenario_id, enterprise_id, entry_type, amount, reason, created_at)
                           VALUES (?,?,?,?,?,?,?)""",
                        [
                            (row["period"], scenario_id, r["enterprise_id"], ENTRY_INITIAL,
                             r["amount"], r["reason_detail"], now)
                            for r in results if r["amount"] > 0
                        ],
                    )
            except sqlite3.IntegrityError as exc:
                raise ServiceError(409, f"周期 {row['period']} 已存在正式发布的方案") from exc
        return {
            "scenario_id": scenario_id,
            "period": row["period"],
            "status": STATUS_CONFIRMED,
            "entries": sum(1 for r in results if r["amount"] > 0),
            "total_allocated": sum(r["amount"] for r in results),
        }

    # ------------------------------------------------------------------
    # 发布后的确定规则调整：放弃、撤销、递补、封存
    # ------------------------------------------------------------------
    def surrender(self, period: str, enterprise_id: str, amount: int, note: str = "") -> dict:
        """企业主动放弃部分额度，释放量按确定规则递补。"""
        amount = int(amount)
        if amount <= 0:
            raise ServiceError(400, "放弃数量必须为正")
        with self._lock:
            scenario = self._published_or_409(period)
            if self._is_revoked(period, enterprise_id):
                raise ServiceError(409, f"企业 {enterprise_id} 资格已撤销")
            holding = self._holdings(period).get(enterprise_id, 0)
            if amount > holding:
                raise ServiceError(400, f"企业 {enterprise_id} 当前持有 {holding}，放弃数量超出")
            reason = f"企业主动放弃 {amount} 单位额度" + (f"：{note}" if note else "")
            with self._conn:
                self._insert_entry(period, scenario["id"], enterprise_id, ENTRY_SURRENDER, -amount, reason)
                grants = self._redistribute_locked(period, scenario, amount, enterprise_id,
                                                   f"{enterprise_id} 放弃 {amount} 单位额度")
                self._bump_status(scenario)
        return {"period": period, "freed": amount, "grants": grants}

    def revoke(self, period: str, enterprise_id: str, reason: str) -> dict:
        """撤销企业资格：持有额度全部收回，按确定规则递补，且不再参与后续递补。"""
        if not reason:
            raise ServiceError(400, "撤销原因不能为空")
        with self._lock:
            scenario = self._published_or_409(period)
            known = {r["enterprise_id"] for r in self._results_of(scenario["id"])}
            if enterprise_id not in known:
                raise ServiceError(404, f"企业 {enterprise_id} 不在本期方案中")
            holding = self._holdings(period).get(enterprise_id, 0)
            try:
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO revocations(period, enterprise_id, reason, created_at) VALUES (?,?,?,?)",
                        (period, enterprise_id, reason, _now()),
                    )
                    if holding > 0:
                        self._insert_entry(period, scenario["id"], enterprise_id,
                                           ENTRY_REVOCATION, -holding, f"资格撤销：{reason}")
                    grants = self._redistribute_locked(period, scenario, holding, enterprise_id,
                                                       f"{enterprise_id} 资格被撤销")
                    self._bump_status(scenario)
            except sqlite3.IntegrityError as exc:
                raise ServiceError(409, f"企业 {enterprise_id} 在本周期已被撤销") from exc
        return {"period": period, "revoked": enterprise_id, "freed": holding, "grants": grants}

    def seal(self, period: str) -> dict:
        """封存周期：之后不再接受放弃、撤销等调整。"""
        with self._lock:
            scenario = self._published_or_409(period)
            with self._conn:
                self._conn.execute("UPDATE scenarios SET status=? WHERE id=?",
                                   (STATUS_SEALED, scenario["id"]))
        return {"period": period, "status": STATUS_SEALED}

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def allocations(self, period: str) -> dict:
        """每家企业当前额度与获得/未获得原因。"""
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM scenarios WHERE period=? AND status IN (?,?,?)
                   ORDER BY id DESC LIMIT 1""",
                (period, *PUBLISHED_STATUSES, STATUS_SEALED),
            ).fetchone()
            if row is None:
                raise ServiceError(404, f"周期 {period} 尚无正式发布的方案")
            results = self._results_of(row["id"])
            holdings = self._holdings(period)
            events = self._events_by_enterprise(period)
            revoked = self._revoked_ids(period)
            names = self._names_of(row["snapshot_id"])
        items = []
        for r in sorted(results, key=lambda x: x["enterprise_id"]):
            eid = r["enterprise_id"]
            current = holdings.get(eid, 0)
            if eid in revoked:
                status, reason = "已撤销", "资格已撤销，额度全部收回"
            elif current > 0:
                status, reason = "持有", r["reason_detail"]
            elif r["reason_code"] in ("zero_weight", "pool_exhausted"):
                status, reason = "候补", r["reason_detail"]
            else:
                status, reason = "未获得", r["reason_detail"]
            items.append({
                "enterprise_id": eid,
                "name": names.get(eid, eid),
                "status": status,
                "initial": r["amount"],
                "current": current,
                "reason": reason,
                "events": events.get(eid, []),
            })
        return {
            "period": period,
            "scenario_id": row["id"],
            "status": row["status"],
            "total_current": sum(holdings.values()),
            "items": items,
        }

    def ledger(self, period: str) -> dict:
        """配额分录流水。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM quota_entries WHERE period=? ORDER BY id", (period,)
            ).fetchall()
        return {
            "period": period,
            "entries": [
                {
                    "id": r["id"],
                    "enterprise_id": r["enterprise_id"],
                    "entry_type": r["entry_type"],
                    "amount": r["amount"],
                    "reason": r["reason"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
        }

    # ------------------------------------------------------------------
    # 内部 helpers
    # ------------------------------------------------------------------
    def _snapshot_or_404(self, snapshot_id: int) -> list[Enterprise]:
        rows = self._conn.execute(
            "SELECT * FROM snapshot_enterprises WHERE snapshot_id=? ORDER BY enterprise_id",
            (snapshot_id,),
        ).fetchall()
        if not rows:
            raise ServiceError(404, f"资格快照不存在：{snapshot_id}")
        return [
            Enterprise(r["enterprise_id"], r["name"], r["category"], r["output"],
                       r["tech_route"], r["compliance_rate"], bool(r["eligible"]), r["note"])
            for r in rows
        ]

    def _rules_or_404(self, rule_version_id: int) -> RuleConfig:
        row = self._conn.execute("SELECT * FROM rule_versions WHERE id=?", (rule_version_id,)).fetchone()
        if row is None:
            raise ServiceError(404, f"规则版本不存在：{rule_version_id}")
        return RuleConfig.from_dict(json.loads(row["config_json"]))

    def _results_of(self, scenario_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM scenario_results WHERE scenario_id=? ORDER BY enterprise_id",
            (scenario_id,),
        ).fetchall()
        return [
            {
                "enterprise_id": r["enterprise_id"],
                "amount": r["amount"],
                "weight": r["weight"],
                "floor": r["floor"],
                "cap": r["cap"],
                "reason_code": r["reason_code"],
                "reason_detail": r["reason_detail"],
                "components": json.loads(r["components_json"]),
            }
            for r in rows
        ]

    def _published_or_409(self, period: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM scenarios WHERE period=? AND status IN (?,?) ORDER BY id DESC LIMIT 1",
            (period, *PUBLISHED_STATUSES),
        ).fetchone()
        if row is None:
            sealed = self._conn.execute(
                "SELECT 1 FROM scenarios WHERE period=? AND status=?", (period, STATUS_SEALED)
            ).fetchone()
            if sealed:
                raise ServiceError(409, f"周期 {period} 已封存，不再接受调整")
            raise ServiceError(409, f"周期 {period} 尚未发布正式方案")
        return row

    def _holdings(self, period: str) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT enterprise_id, SUM(amount) AS total FROM quota_entries WHERE period=? GROUP BY enterprise_id",
            (period,),
        ).fetchall()
        return {r["enterprise_id"]: r["total"] for r in rows}

    def _is_revoked(self, period: str, enterprise_id: str) -> bool:
        return enterprise_id in self._revoked_ids(period)

    def _revoked_ids(self, period: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT enterprise_id FROM revocations WHERE period=?", (period,)
        ).fetchall()
        return {r["enterprise_id"] for r in rows}

    def _names_of(self, snapshot_id: int) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT enterprise_id, name FROM snapshot_enterprises WHERE snapshot_id=?", (snapshot_id,)
        ).fetchall()
        return {r["enterprise_id"]: r["name"] for r in rows}

    def _events_by_enterprise(self, period: str) -> dict[str, list[dict]]:
        rows = self._conn.execute(
            """SELECT enterprise_id, entry_type, amount, reason FROM quota_entries
               WHERE period=? AND entry_type != ? ORDER BY id""",
            (period, ENTRY_INITIAL),
        ).fetchall()
        events: dict[str, list[dict]] = {}
        for r in rows:
            events.setdefault(r["enterprise_id"], []).append(
                {"entry_type": r["entry_type"], "amount": r["amount"], "reason": r["reason"]}
            )
        return events

    def _insert_entry(self, period, scenario_id, enterprise_id, entry_type, amount, reason) -> None:
        self._conn.execute(
            """INSERT INTO quota_entries
               (period, scenario_id, enterprise_id, entry_type, amount, reason, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (period, scenario_id, enterprise_id, entry_type, amount, reason, _now()),
        )

    def _redistribute_locked(self, period, scenario, freed, source_id, cause) -> dict[str, int]:
        """在事务内按确定规则递补释放额度。调用方须已持有锁并处于事务中。"""
        if freed <= 0:
            return {}
        results = self._results_of(scenario["id"])
        revoked = self._revoked_ids(period)
        eligible_ids = [r["enterprise_id"] for r in results if r["reason_code"] != "ineligible"]
        candidates = [eid for eid in eligible_ids if eid != source_id and eid not in revoked]
        weights = {r["enterprise_id"]: r["weight"] for r in results}
        floors = {r["enterprise_id"]: r["floor"] for r in results}
        caps = {r["enterprise_id"]: r["cap"] for r in results}
        holdings = self._holdings(period)
        waitlist = waitlist_order(
            {r["enterprise_id"]: EnterpriseAllocation(
                r["enterprise_id"], r["amount"], r["weight"], r["floor"], r["cap"],
                r["reason_code"], r["reason_detail"]) for r in results}
        )
        grants = redistribute(freed, candidates, weights, floors, caps, holdings, waitlist)
        for eid in sorted(grants):
            self._insert_entry(period, scenario["id"], eid, ENTRY_BACKFILL, grants[eid],
                               f"因{cause}释放额度，按候补与权重确定规则递补 {grants[eid]}")
        return grants

    def _bump_status(self, scenario) -> None:
        if scenario["status"] == STATUS_CONFIRMED:
            self._conn.execute("UPDATE scenarios SET status=? WHERE id=?",
                               (STATUS_RUNNING, scenario["id"]))
