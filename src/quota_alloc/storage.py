"""SQLite 存储层。

关键约束：
- 资格快照与规则版本以内容哈希寻址，同一输入永远指向同一行，可复现；
- 正式发布在单个 ``BEGIN IMMEDIATE`` 事务内写入发布头、全量配额分录与
  持有量表，要么全部可见，要么全部回滚（原子写入）；
- 发布后的放弃/撤销同样以事务追加事件并更新持有量，事件序号单调。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id   TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    note          TEXT,
    subject_count INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,
    payload       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_versions (
    rule_version_id TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    name            TEXT NOT NULL,
    params_hash     TEXT NOT NULL,
    params          TEXT NOT NULL,
    note            TEXT
);
CREATE TABLE IF NOT EXISTS scenarios (
    scenario_id    TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL,
    name           TEXT,
    snapshot_id    TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    rule_version_id TEXT NOT NULL REFERENCES rule_versions(rule_version_id),
    total_quota    INTEGER NOT NULL,
    result_hash    TEXT NOT NULL,
    result         TEXT NOT NULL,
    published      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS publications (
    publication_id TEXT PRIMARY KEY,
    scenario_id    TEXT NOT NULL UNIQUE REFERENCES scenarios(scenario_id),
    snapshot_id    TEXT NOT NULL,
    rule_version_id TEXT NOT NULL,
    total_quota    INTEGER NOT NULL,
    published_at   TEXT NOT NULL,
    status         TEXT NOT NULL,
    ledger_hash    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_entries (
    publication_id TEXT NOT NULL REFERENCES publications(publication_id),
    subject_id     TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    allocated      INTEGER NOT NULL,
    score          INTEGER NOT NULL,
    floor          INTEGER NOT NULL,
    cap            INTEGER NOT NULL,
    capped         INTEGER NOT NULL,
    reasons        TEXT NOT NULL,
    note           TEXT,
    PRIMARY KEY (publication_id, subject_id)
);
CREATE TABLE IF NOT EXISTS holdings (
    publication_id TEXT NOT NULL REFERENCES publications(publication_id),
    subject_id     TEXT NOT NULL,
    amount         INTEGER NOT NULL,
    status         TEXT NOT NULL,
    PRIMARY KEY (publication_id, subject_id)
);
CREATE TABLE IF NOT EXISTS ledger_events (
    publication_id  TEXT NOT NULL REFERENCES publications(publication_id),
    seq             INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    subject_id      TEXT NOT NULL,
    released_amount INTEGER NOT NULL,
    retained        INTEGER NOT NULL,
    payload         TEXT NOT NULL,
    PRIMARY KEY (publication_id, seq)
);
"""


def canonical_json(obj: Any) -> str:
    """规范 JSON：排序键、无空白、不转义非 ASCII。所有哈希的唯一序列化口径。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Repository:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(
            self.path, detect_types=sqlite3.PARSE_DECLTYPES, isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        # 单连接跨线程（ThreadingHTTPServer）：用写锁串行化所有写事务，
        # 避免不同请求的 BEGIN/COMMIT 交错；SQLite 自身的 BEGIN IMMEDIATE
        # 继续保证多进程/多连接之间的互斥。
        self._write_lock = threading.RLock()
        self._conn.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """串行化写入事务；异常一律回滚。"""
        conn = self._conn
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    # ---- 快照 -----------------------------------------------------------

    def upsert_snapshot(self, payload: dict, note: str | None = None) -> tuple[str, bool]:
        """按内容哈希写入/复用资格快照。返回 (id, created_now)。"""
        subjects = payload["subjects"]
        h = content_hash(subjects)
        snapshot_id = "snp_" + h[:16]
        with self._write_lock:
            row = self._conn.execute(
                "SELECT snapshot_id FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            if row:
                return snapshot_id, False
            with self.transaction() as c:
                c.execute(
                    "INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
                    (snapshot_id, utc_now(), note, len(subjects), h,
                     canonical_json(subjects)),
                )
        return snapshot_id, True

    def get_snapshot(self, snapshot_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if not row:
            raise KeyError(f"快照不存在：{snapshot_id}")
        return {
            "snapshot_id": row["snapshot_id"],
            "created_at": row["created_at"],
            "note": row["note"],
            "subject_count": row["subject_count"],
            "content_hash": row["content_hash"],
            "subjects": json.loads(row["payload"]),
        }

    def list_snapshots(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT snapshot_id, created_at, note, subject_count, content_hash "
            "FROM snapshots ORDER BY created_at, snapshot_id"
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- 规则版本 -------------------------------------------------------

    def upsert_rule_version(self, name: str, normalized: dict,
                            note: str | None = None) -> tuple[str, bool]:
        h = content_hash(normalized)
        rid = "rlv_" + h[:16]
        with self._write_lock:
            row = self._conn.execute(
                "SELECT rule_version_id FROM rule_versions WHERE rule_version_id=?", (rid,)
            ).fetchone()
            if row:
                return rid, False
            with self.transaction() as c:
                c.execute(
                    "INSERT INTO rule_versions VALUES (?,?,?,?,?,?)",
                    (rid, utc_now(), name, h, canonical_json(normalized), note),
                )
        return rid, True

    def get_rule_version(self, rule_version_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM rule_versions WHERE rule_version_id=?", (rule_version_id,)
        ).fetchone()
        if not row:
            raise KeyError(f"规则版本不存在：{rule_version_id}")
        return {
            "rule_version_id": row["rule_version_id"],
            "created_at": row["created_at"],
            "name": row["name"],
            "params_hash": row["params_hash"],
            "note": row["note"],
            "params": json.loads(row["params"]),
        }

    def list_rule_versions(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT rule_version_id, created_at, name, params_hash, note "
            "FROM rule_versions ORDER BY created_at, rule_version_id"
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- 试算方案 -------------------------------------------------------

    def insert_scenario(self, scenario: dict) -> tuple[str, bool]:
        sid = scenario["scenario_id"]
        with self._write_lock:
            row = self._conn.execute(
                "SELECT scenario_id FROM scenarios WHERE scenario_id=?", (sid,)
            ).fetchone()
            if row:
                return sid, False
            with self.transaction() as c:
                c.execute(
                    "INSERT INTO scenarios (scenario_id, created_at, name, snapshot_id,"
                    " rule_version_id, total_quota, result_hash, result, published)"
                    " VALUES (?,?,?,?,?,?,?,?,0)",
                    (sid, utc_now(), scenario.get("name"), scenario["snapshot_id"],
                     scenario["rule_version_id"], scenario["total_quota"],
                     scenario["result_hash"], canonical_json(scenario["result"])),
                )
        return sid, True

    def get_scenario(self, scenario_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if not row:
            raise KeyError(f"试算方案不存在：{scenario_id}")
        return {
            "scenario_id": row["scenario_id"],
            "created_at": row["created_at"],
            "name": row["name"],
            "snapshot_id": row["snapshot_id"],
            "rule_version_id": row["rule_version_id"],
            "total_quota": row["total_quota"],
            "result_hash": row["result_hash"],
            "published": bool(row["published"]),
            "result": json.loads(row["result"]),
        }

    def list_scenarios(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT scenario_id, created_at, name, snapshot_id, rule_version_id,"
            " total_quota, result_hash, published FROM scenarios"
            " ORDER BY created_at, scenario_id"
        ).fetchall()
        return [dict(r, published=bool(r["published"])) for r in rows]

    # ---- 发布与分录（由 service 在单事务内调用）-------------------------

    def publication_exists_for_scenario(self, scenario_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM publications WHERE scenario_id=?", (scenario_id,)
        ).fetchone() is not None

    def get_publication(self, publication_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM publications WHERE publication_id=?", (publication_id,)
        ).fetchone()
        if not row:
            raise KeyError(f"发布不存在：{publication_id}")
        return dict(row)

    def list_publications(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT publication_id, scenario_id, snapshot_id, rule_version_id,"
            " total_quota, published_at, status, ledger_hash"
            " FROM publications ORDER BY published_at, publication_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_ledger_entries(self, publication_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT subject_id, seq, allocated, score, floor, cap, capped, reasons, note"
            " FROM ledger_entries WHERE publication_id=? ORDER BY seq",
            (publication_id,),
        ).fetchall()
        return [
            {
                "subject_id": r["subject_id"],
                "seq": r["seq"],
                "allocated": r["allocated"],
                "score": r["score"],
                "floor": r["floor"],
                "cap": r["cap"],
                "capped": bool(r["capped"]),
                "reasons": json.loads(r["reasons"]),
                "note": r["note"],
            }
            for r in rows
        ]

    def list_holdings(self, publication_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT subject_id, amount, status FROM holdings"
            " WHERE publication_id=? ORDER BY subject_id",
            (publication_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_events(self, publication_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT seq, created_at, event_type, subject_id, released_amount,"
            " retained, payload FROM ledger_events WHERE publication_id=? ORDER BY seq",
            (publication_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(r["payload"])
            out.append(d)
        return out

    def next_event_seq(self, conn: sqlite3.Connection, publication_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS n FROM ledger_events WHERE publication_id=?",
            (publication_id,),
        ).fetchone()
        return int(row["n"])
