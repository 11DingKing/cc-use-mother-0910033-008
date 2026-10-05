"""业务编排层：快照、规则版本、试算方案、差异解释、原子发布与发布后处理。"""
from __future__ import annotations

from typing import Any, Sequence

from .allocation import (
    R_REASON_LABELS,
    STATUS_ACTIVE,
    STATUS_RELINQUISHED,
    STATUS_REVOKED,
    STATUS_WAITLIST,
    Subject,
    allocate,
    make_rule_params,
    reallocate_release,
    validate_rule_params,
)
from .storage import Repository, canonical_json, content_hash, utc_now


class ApiError(Exception):
    """面向 API 的业务错误（携带稳定错误码）。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _subject_from_dict(raw: dict) -> Subject:
    try:
        return Subject(
            subject_id=str(raw["subject_id"]),
            name=str(raw.get("name", raw["subject_id"])),
            size=str(raw["size"]),
            tech_route=str(raw["tech_route"]),
            output=int(raw["output"]),
            compliance_score=int(raw["compliance_score"]),
            eligible=bool(raw.get("eligible", True)),
            waitlist=bool(raw.get("waitlist", False)),
        )
    except KeyError as exc:
        raise ApiError("invalid_subject", f"企业数据缺少字段：{exc}") from exc
    except (TypeError, ValueError) as exc:
        raise ApiError("invalid_subject", str(exc)) from exc


def _hydrate(subjects_raw: list[dict]) -> list[Subject]:
    subjects = [_subject_from_dict(r) for r in subjects_raw]
    ids = [s.subject_id for s in subjects]
    if len(ids) != len(set(ids)):
        raise ApiError("duplicate_subject", "资格快照内企业编号重复")
    return subjects


def _rules_from_params(params: dict):
    return make_rule_params(
        output_weight=params["output_weight"] / 10_000,
        route_coeff={k: v / 10_000 for k, v in params["route_coeff"].items()},
        compliance_weight=params["compliance_weight"] / 10_000,
        floors=params["floors"],
        caps=params["caps"],
    )


class QuotaService:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    # ---- 资格快照 -------------------------------------------------------

    def create_snapshot(self, subjects: list[dict], note: str | None = None) -> dict:
        hydrate = _hydrate(subjects)  # 先校验再落库
        payload = {
            "subjects": [
                {
                    "subject_id": s.subject_id,
                    "name": s.name,
                    "size": s.size,
                    "tech_route": s.tech_route,
                    "output": s.output,
                    "compliance_score": s.compliance_score,
                    "eligible": s.eligible,
                    "waitlist": s.waitlist,
                }
                for s in hydrate
            ]
        }
        snapshot_id, created = self.repo.upsert_snapshot(payload, note)
        return {"snapshot_id": snapshot_id, "created": created,
                "subject_count": len(hydrate)}

    def get_snapshot(self, snapshot_id: str) -> dict:
        try:
            return self.repo.get_snapshot(snapshot_id)
        except KeyError as exc:
            raise ApiError("not_found", str(exc), 404) from exc

    def list_snapshots(self) -> list[dict]:
        return self.repo.list_snapshots()

    # ---- 规则版本 -------------------------------------------------------

    def create_rule_version(self, *, name: str, params: dict,
                            note: str | None = None) -> dict:
        try:
            normalized = validate_rule_params(
                output_weight=params["output_weight"],
                route_coeff=params["route_coeff"],
                compliance_weight=params["compliance_weight"],
                floors=params["floors"],
                caps=params["caps"],
            )
        except KeyError as exc:
            raise ApiError("invalid_rules", f"规则参数缺少字段：{exc}") from exc
        except ValueError as exc:
            raise ApiError("invalid_rules", str(exc)) from exc
        rid, created = self.repo.upsert_rule_version(name, normalized, note)
        return {"rule_version_id": rid, "created": created, "name": name,
                "normalized": normalized}

    def get_rule_version(self, rule_version_id: str) -> dict:
        try:
            return self.repo.get_rule_version(rule_version_id)
        except KeyError as exc:
            raise ApiError("not_found", str(exc), 404) from exc

    def list_rule_versions(self) -> list[dict]:
        return self.repo.list_rule_versions()

    # ---- 试算方案 -------------------------------------------------------

    def run_scenario(self, *, snapshot_id: str, rule_version_id: str,
                     total_quota: int, name: str | None = None) -> dict:
        snapshot = self.get_snapshot(snapshot_id)
        version = self.get_rule_version(rule_version_id)
        try:
            total_quota = int(total_quota)
            if total_quota < 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ApiError("invalid_quota", "总额度须为非负整数") from exc

        subjects = _hydrate(snapshot["subjects"])
        rules = _rules_from_params(version["params"])
        result = allocate(subjects, rules, total_quota)
        explain = result.explain()
        result_hash = content_hash({
            "snapshot": snapshot["content_hash"],
            "rules": version["params_hash"],
            "total_quota": total_quota,
            "lines": explain,
        })
        scenario_id = "scn_" + result_hash[:16]
        stored = {
            "scenario_id": scenario_id,
            "name": name,
            "snapshot_id": snapshot_id,
            "rule_version_id": rule_version_id,
            "total_quota": total_quota,
            "result_hash": result_hash,
            "result": {
                "total_quota": result.total_quota,
                "allocated_sum": result.allocated_sum,
                "remainder": result.remainder,
                "feasible": result.feasible,
                "floor_shortfall": result.floor_shortfall,
                "lines": explain,
            },
        }
        self.repo.insert_scenario(stored)
        return stored

    def get_scenario(self, scenario_id: str) -> dict:
        try:
            return self.repo.get_scenario(scenario_id)
        except KeyError as exc:
            raise ApiError("not_found", str(exc), 404) from exc

    def list_scenarios(self) -> list[dict]:
        return self.repo.list_scenarios()

    def diff_scenarios(self, scenario_id_a: str, scenario_id_b: str) -> dict:
        """逐企业对比两个试算方案，并给出可读的差异解释。"""
        a = self.get_scenario(scenario_id_a)
        b = self.get_scenario(scenario_id_b)
        la = {ln["subject_id"]: ln for ln in a["result"]["lines"]}
        lb = {ln["subject_id"]: ln for ln in b["result"]["lines"]}
        all_ids = sorted(set(la) | set(lb))
        per_subject: list[dict] = []
        for sid in all_ids:
            xa, xb = la.get(sid), lb.get(sid)
            delta = (xb["allocated"] if xb else 0) - (xa["allocated"] if xa else 0)
            ra = set(xa["reasons"]) if xa else set()
            rb = set(xb["reasons"]) if xb else set()
            texts: list[str] = []
            if delta > 0:
                texts.append(f"方案B多分 {delta}")
            elif delta < 0:
                texts.append(f"方案B少分 {-delta}")
            else:
                texts.append("两方案额度相同")
            gained_codes = rb - ra
            lost_codes = ra - rb
            if gained_codes:
                texts.append("新增原因：" + "、".join(R_REASON_LABELS[c] for c in sorted(gained_codes)))
            if lost_codes:
                texts.append("不再适用：" + "、".join(R_REASON_LABELS[c] for c in sorted(lost_codes)))
            per_subject.append({
                "subject_id": sid,
                "allocated_a": xa["allocated"] if xa else None,
                "allocated_b": xb["allocated"] if xb else None,
                "delta_b_minus_a": delta,
                "explanation": "；".join(texts),
            })
        winners = [p["subject_id"] for p in per_subject if p["delta_b_minus_a"] > 0]
        losers = [p["subject_id"] for p in per_subject if p["delta_b_minus_a"] < 0]
        same = [p["subject_id"] for p in per_subject if p["delta_b_minus_a"] == 0]
        root_causes: list[str] = []
        if a["snapshot_id"] != b["snapshot_id"]:
            root_causes.append("两方案基于不同资格快照（企业构成或资格状态不同）")
        if a["rule_version_id"] != b["rule_version_id"]:
            root_causes.append("两方案采用不同规则版本（权重/路线系数/保底/上限不同）")
        if a["total_quota"] != b["total_quota"]:
            root_causes.append(
                f"总额度不同：方案A {a['total_quota']} → 方案B {b['total_quota']}"
            )
        if not root_causes:
            root_causes.append("输入完全一致，结果相同（确定性内核）")
        return {
            "scenario_a": {"id": scenario_id_a, **self._scenario_head(a)},
            "scenario_b": {"id": scenario_id_b, **self._scenario_head(b)},
            "root_causes": root_causes,
            "summary": {
                "better_in_b": winners,
                "worse_in_b": losers,
                "unchanged": same,
                "allocated_a": a["result"]["allocated_sum"],
                "allocated_b": b["result"]["allocated_sum"],
                "remainder_a": a["result"]["remainder"],
                "remainder_b": b["result"]["remainder"],
            },
            "per_subject": per_subject,
        }

    @staticmethod
    def _scenario_head(s: dict) -> dict:
        return {
            "snapshot_id": s["snapshot_id"],
            "rule_version_id": s["rule_version_id"],
            "total_quota": s["total_quota"],
            "allocated_sum": s["result"]["allocated_sum"],
            "remainder": s["result"]["remainder"],
            "feasible": s["result"]["feasible"],
        }

    # ---- 正式发布（原子写入配额分录）-----------------------------------

    def publish(self, scenario_id: str) -> dict:
        scenario = self.get_scenario(scenario_id)
        if self.repo.publication_exists_for_scenario(scenario_id):
            raise ApiError("already_published", "该试算方案已正式发布，不可重复发布", 409)
        snapshot = self.get_snapshot(scenario["snapshot_id"])
        version = self.get_rule_version(scenario["rule_version_id"])
        subjects = _hydrate(snapshot["subjects"])
        eligible_flag = {s.subject_id: s.eligible for s in subjects}
        waitlist_flag = {s.subject_id: s.waitlist for s in subjects}

        lines = scenario["result"]["lines"]
        ledger_payload = [
            {"subject_id": ln["subject_id"], "seq": i,
             "allocated": ln["allocated"], "score": ln["score"],
             "floor": ln["floor"], "cap": ln["cap"], "capped": ln["capped"],
             "reasons": ln["reasons"], "note": ln["note"]}
            for i, ln in enumerate(sorted(lines, key=lambda x: x["subject_id"]))
        ]
        ledger_hash = content_hash({
            "scenario_id": scenario_id,
            "result_hash": scenario["result_hash"],
            "entries": ledger_payload,
        })
        publication_id = "pub_" + ledger_hash[:16]

        conn = self.repo.conn
        with self.repo.transaction(immediate=True) as c:
            # 事务内二次检查，防止并发重复发布
            row = c.execute(
                "SELECT 1 FROM publications WHERE scenario_id=?", (scenario_id,)
            ).fetchone()
            if row:
                raise ApiError("already_published", "该试算方案已正式发布", 409)
            c.execute(
                "INSERT INTO publications VALUES (?,?,?,?,?,?,?,?)",
                (publication_id, scenario_id, scenario["snapshot_id"],
                 scenario["rule_version_id"], scenario["total_quota"],
                 utc_now(), "published", ledger_hash),
            )
            for i, ln in enumerate(ledger_payload):
                c.execute(
                    "INSERT INTO ledger_entries VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (publication_id, ln["subject_id"], i, ln["allocated"],
                     ln["score"], ln["floor"], ln["cap"], int(ln["capped"]),
                     canonical_json(ln["reasons"]), ln["note"]),
                )
                sid = ln["subject_id"]
                if not eligible_flag[sid]:
                    status = "ineligible"
                elif waitlist_flag[sid]:
                    status = STATUS_WAITLIST
                else:
                    status = STATUS_ACTIVE
                c.execute(
                    "INSERT INTO holdings VALUES (?,?,?,?)",
                    (publication_id, sid, ln["allocated"], status),
                )
            c.execute("UPDATE scenarios SET published=1 WHERE scenario_id=?",
                      (scenario_id,))
        return {
            "publication_id": publication_id,
            "scenario_id": scenario_id,
            "ledger_hash": ledger_hash,
            "entry_count": len(ledger_payload),
            "total_quota": scenario["total_quota"],
            "published_at": self.repo.get_publication(publication_id)["published_at"],
        }

    def get_publication(self, publication_id: str) -> dict:
        try:
            pub = self.repo.get_publication(publication_id)
        except KeyError as exc:
            raise ApiError("not_found", str(exc), 404) from exc
        entries = self.repo.list_ledger_entries(publication_id)
        holdings = self.repo.list_holdings(publication_id)
        holding_by_id = {h["subject_id"]: h for h in holdings}
        for e in entries:
            h = holding_by_id.get(e["subject_id"])
            e["current_amount"] = h["amount"] if h else e["allocated"]
            e["current_status"] = h["status"] if h else "active"
            e["reason_text"] = [R_REASON_LABELS.get(r, r) for r in e["reasons"]]
        pub["entries"] = entries
        pub["events"] = self.repo.list_events(publication_id)
        pub["allocated_sum"] = sum(e["allocated"] for e in entries)
        pub["current_sum"] = sum(h["amount"] for h in holdings if h["status"] != "ineligible")
        return pub

    def list_publications(self) -> list[dict]:
        return self.repo.list_publications()

    # ---- 发布后：放弃 / 资格撤销（确定式候补递补 + 尾差重分）-----------

    def _release(self, publication_id: str, subject_id: str, event_type: str,
                 new_status: str) -> dict:
        try:
            self.repo.get_publication(publication_id)
        except KeyError as exc:
            raise ApiError("not_found", str(exc), 404) from exc
        if event_type not in ("relinquish", "revoke"):
            raise ApiError("invalid_event", "未知发布后事件类型")

        conn = self.repo.conn
        with self.repo.transaction(immediate=True) as c:
            holdings_rows = c.execute(
                "SELECT subject_id, amount, status FROM holdings WHERE publication_id=?",
                (publication_id,),
            ).fetchall()
            holdings = {r["subject_id"]: dict(r) for r in holdings_rows}
            if subject_id not in holdings:
                raise ApiError("not_found", f"发布中不存在企业 {subject_id}", 404)
            target = holdings[subject_id]
            if target["status"] in (STATUS_RELINQUISHED, STATUS_REVOKED):
                raise ApiError("already_released",
                               f"企业 {subject_id} 已{('放弃' if target['status']==STATUS_RELINQUISHED else '被撤销')}",
                               409)
            if target["status"] == "ineligible":
                raise ApiError("not_holder", f"企业 {subject_id} 本无资格", 409)
            released_amount = int(target["amount"])
            if released_amount <= 0:
                raise ApiError("nothing_to_release",
                               f"企业 {subject_id} 当前持有额度为 0，无可释放额度", 409)

            pub = c.execute(
                "SELECT snapshot_id, rule_version_id FROM publications WHERE publication_id=?",
                (publication_id,),
            ).fetchone()
            snapshot = self.repo.get_snapshot(pub["snapshot_id"])
            version = self.repo.get_rule_version(pub["rule_version_id"])
            subjects = _hydrate(snapshot["subjects"])
            rules = _rules_from_params(version["params"])

            statuses = {sid: h["status"] for sid, h in holdings.items()}
            statuses[subject_id] = new_status
            current = {sid: h["amount"] for sid, h in holdings.items()}
            result = reallocate_release(
                subjects=subjects, rules=rules, current=current,
                released_subject=subject_id, released_amount=released_amount,
                statuses=statuses,
            )

            seq = self.repo.next_event_seq(c, publication_id)
            changes = result.changes()
            c.execute(
                "INSERT INTO ledger_events VALUES (?,?,?,?,?,?,?,?)",
                (publication_id, seq, utc_now(), event_type, subject_id,
                 released_amount, result.retained, canonical_json({
                     "promoted": result.promoted,
                     "distributed": result.distributed,
                     "changes": changes,
                     "events": result.events,
                 })),
            )
            # 更新持有量与状态：释放主体清零，其余按重分结果更新
            c.execute("UPDATE holdings SET amount=0, status=? WHERE publication_id=? AND subject_id=?",
                      (new_status, publication_id, subject_id))
            for ln in result.lines.values():
                if ln.subject_id == subject_id:
                    continue
                was_waitlist = holdings[ln.subject_id]["status"] == STATUS_WAITLIST
                if ln.after != ln.before:
                    if was_waitlist and ln.after > 0:
                        # 候补实际拿到额度（含保底为 0 但参与尾差）即转为在册
                        c.execute(
                            "UPDATE holdings SET amount=?, status=? WHERE publication_id=? AND subject_id=?",
                            (ln.after, STATUS_ACTIVE, publication_id, ln.subject_id),
                        )
                    else:
                        c.execute(
                            "UPDATE holdings SET amount=? WHERE publication_id=? AND subject_id=?",
                            (ln.after, publication_id, ln.subject_id),
                        )

        return {
            "publication_id": publication_id,
            "seq": seq,
            "event_type": event_type,
            "subject_id": subject_id,
            "released_amount": released_amount,
            "distributed": result.distributed,
            "retained": result.retained,
            "promoted": result.promoted,
            "changes": result.changes(),
            "events": result.events,
        }

    def relinquish(self, publication_id: str, subject_id: str) -> dict:
        return self._release(publication_id, subject_id, "relinquish",
                             STATUS_RELINQUISHED)

    def revoke(self, publication_id: str, subject_id: str) -> dict:
        return self._release(publication_id, subject_id, "revoke", STATUS_REVOKED)
