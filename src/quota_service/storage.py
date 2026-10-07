"""SQLite 持久化：模式定义与连接。

配额分录（quota_entries）为只增不改的流水账：
正式发布、放弃、撤销、递补都以分录形式原子写入，当前持有量由分录求和得出。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_enterprises (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    enterprise_id TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    output INTEGER NOT NULL,
    tech_route TEXT NOT NULL,
    compliance_rate INTEGER NOT NULL,
    eligible INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (snapshot_id, enterprise_id)
);
CREATE TABLE IF NOT EXISTS rule_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    config_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scenarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    rule_version_id INTEGER NOT NULL REFERENCES rule_versions(id),
    total_quota INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT '草稿',
    created_at TEXT NOT NULL,
    published_at TEXT
);
-- 每个周期至多一个正式发布方案（已确认/执行中/已封存均占用该名额）
CREATE UNIQUE INDEX IF NOT EXISTS uq_published_period
    ON scenarios(period) WHERE status IN ('已确认', '执行中', '已封存');
CREATE TABLE IF NOT EXISTS scenario_results (
    scenario_id INTEGER NOT NULL REFERENCES scenarios(id),
    enterprise_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    weight INTEGER NOT NULL,
    floor INTEGER NOT NULL,
    cap INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    reason_detail TEXT NOT NULL,
    components_json TEXT NOT NULL,
    PRIMARY KEY (scenario_id, enterprise_id)
);
CREATE TABLE IF NOT EXISTS quota_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period TEXT NOT NULL,
    scenario_id INTEGER NOT NULL REFERENCES scenarios(id),
    enterprise_id TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    amount INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_entries_period ON quota_entries(period, enterprise_id);
CREATE TABLE IF NOT EXISTS revocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period TEXT NOT NULL,
    enterprise_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (period, enterprise_id)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """打开（必要时初始化）数据库连接。"""
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
