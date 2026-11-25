"""SQLite 持久化：版本化口径 + 现行版本指针 + 发布核对记录。"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from flask import current_app, g

from .domain import Range, Rule
from .seed import SEED_RANGE, build_seed_rules

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "hydrology.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS range_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    wl_min     INTEGER NOT NULL,
    wl_max     INTEGER NOT NULL,
    fl_min     INTEGER NOT NULL,
    fl_max     INTEGER NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES rules_versions(id),
    ord        INTEGER NOT NULL,
    zone       TEXT NOT NULL,
    priority   INTEGER NOT NULL,
    wl_min     INTEGER NOT NULL,
    wl_max     INTEGER NOT NULL,
    fl_min     INTEGER NOT NULL,
    fl_max     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rules_version ON rules(version_id);

CREATE TABLE IF NOT EXISTS publish_attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    kind          TEXT NOT NULL,              -- seed | ranges | rules | bundle
    source        TEXT NOT NULL DEFAULT 'web',-- web | api | selftest
    accepted      INTEGER NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    range_id      INTEGER,                    -- 成功后新生效的量程版本
    rules_id      INTEGER,                    -- 成功后新生效的规则版本
    base_range_id INTEGER,                    -- 复核时参照的届时生效量程版本
    base_rules_id INTEGER,                    -- 复核时参照的届时生效规则版本
    report_json   TEXT NOT NULL,
    candidate_json TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def resolve_database_path(configured: Optional[str] = None) -> str:
    path = configured or os.environ.get("HYDROLOGY_DB") or str(DEFAULT_DB_PATH)
    return str(path)


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=20.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=20000")
    # WAL：发布事务复核期间，查单条读数的读请求不被阻塞。
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    return conn


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """串行化发布者的立即写事务；读者在 WAL 下仍可并发查询。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def init_database(path: str) -> None:
    """建表并在空库时播种现行口径。并发首启由 BEGIN IMMEDIATE 串行去重。"""
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        with immediate(conn):
            row = conn.execute(
                "SELECT value FROM meta WHERE key='current_range_id'"
            ).fetchone()
            if row is not None:
                return
            _seed_locked(conn)
    finally:
        conn.close()


def _seed_locked(conn: sqlite3.Connection) -> None:
    ts = now_iso()
    cur = conn.execute(
        """INSERT INTO range_versions
           (wl_min, wl_max, fl_min, fl_max, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            SEED_RANGE.wl_min,
            SEED_RANGE.wl_max,
            SEED_RANGE.fl_min,
            SEED_RANGE.fl_max,
            "初始播种口径",
            ts,
        ),
    )
    range_id = cur.lastrowid

    cur = conn.execute(
        "INSERT INTO rules_versions (note, created_at) VALUES (?, ?)",
        ("初始播种分区表", ts),
    )
    rules_id = cur.lastrowid

    for r in build_seed_rules():
        conn.execute(
            """INSERT INTO rules
               (version_id, ord, zone, priority, wl_min, wl_max, fl_min, fl_max)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rules_id,
                r.ord if r.ord is not None else 0,
                r.zone,
                r.priority,
                r.wl_min,
                r.wl_max,
                r.fl_min,
                r.fl_max,
            ),
        )

    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('current_range_id', ?)",
        (str(range_id),),
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('current_rules_id', ?)",
        (str(rules_id),),
    )
    import json

    from .domain import verify

    report = verify(SEED_RANGE, build_seed_rules())
    conn.execute(
        """INSERT INTO publish_attempts
           (created_at, kind, source, accepted, note, range_id, rules_id,
            base_range_id, base_rules_id, report_json, candidate_json)
           VALUES (?, 'seed', 'system', 1, ?, ?, ?, NULL, NULL, ?, NULL)""",
        (
            ts,
            "系统初始播种",
            range_id,
            rules_id,
            json.dumps(report, ensure_ascii=False),
        ),
    )


# --------------------------------------------------------------------------- #
# 读取现行口径
# --------------------------------------------------------------------------- #


def _meta_int(conn: sqlite3.Connection, key: str) -> Optional[int]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return int(row["value"]) if row is not None else None


def read_current_ids(conn: sqlite3.Connection) -> tuple[Optional[int], Optional[int]]:
    """只读取现行两个版本指针。

    两个指针在同一条 ``BEGIN IMMEDIATE`` 发布事务内先后更新并随事务一起
    提交，WAL 快照读保证这里看到的要么都是旧版本、要么都是新版本，不会
    读到跨事务的“半新半旧”指针组合。
    """
    return (
        _meta_int(conn, "current_range_id"),
        _meta_int(conn, "current_rules_id"),
    )


def load_versioned(
    conn: sqlite3.Connection, range_id: int, rules_id: int
) -> dict[str, Any]:
    """按指定版本号读取量程与分区表（与现行指针解耦，供快照缓存使用）。"""
    result: dict[str, Any] = {
        "range_id": range_id,
        "rules_id": rules_id,
        "range": None,
        "range_meta": None,
        "rules_version_meta": None,
        "rules": [],
    }
    row = conn.execute(
        "SELECT * FROM range_versions WHERE id=?", (range_id,)
    ).fetchone()
    if row is not None:
        result["range"] = Range(
            wl_min=row["wl_min"],
            wl_max=row["wl_max"],
            fl_min=row["fl_min"],
            fl_max=row["fl_max"],
        )
        result["range_meta"] = dict(row)

    vrow = conn.execute(
        "SELECT * FROM rules_versions WHERE id=?", (rules_id,)
    ).fetchone()
    if vrow is not None:
        result["rules_version_meta"] = dict(vrow)
    rows = conn.execute(
        "SELECT * FROM rules WHERE version_id=? ORDER BY ord, id",
        (rules_id,),
    ).fetchall()
    result["rules"] = [
        Rule(
            id=r["id"],
            ord=r["ord"],
            zone=r["zone"],
            priority=r["priority"],
            wl_min=r["wl_min"],
            wl_max=r["wl_max"],
            fl_min=r["fl_min"],
            fl_max=r["fl_max"],
        )
        for r in rows
    ]
    return result


def load_current_overview(conn: sqlite3.Connection) -> dict[str, Any]:
    """首页首屏的轻量口径：只读版本指针、量程元数据与规则条数，不读规则行。

    整张分区表改由 ``/rules-table`` 在资料员点开折叠面板时再加载。
    """
    range_id, rules_id = read_current_ids(conn)
    overview: dict[str, Any] = {
        "range_id": range_id,
        "rules_id": rules_id,
        "range": None,
        "range_meta": None,
        "rules_version_meta": None,
        "rules": [],
        "rule_count": 0,
    }
    if range_id is not None:
        row = conn.execute(
            "SELECT * FROM range_versions WHERE id=?", (range_id,)
        ).fetchone()
        if row is not None:
            overview["range"] = Range(
                wl_min=row["wl_min"],
                wl_max=row["wl_max"],
                fl_min=row["fl_min"],
                fl_max=row["fl_max"],
            )
            overview["range_meta"] = dict(row)
    if rules_id is not None:
        vrow = conn.execute(
            "SELECT * FROM rules_versions WHERE id=?", (rules_id,)
        ).fetchone()
        if vrow is not None:
            overview["rules_version_meta"] = dict(vrow)
        count_row = conn.execute(
            "SELECT COUNT(*) AS c FROM rules WHERE version_id=?", (rules_id,)
        ).fetchone()
        overview["rule_count"] = int(count_row["c"]) if count_row else 0
    return overview


def load_current(conn: sqlite3.Connection) -> dict[str, Any]:
    range_id, rules_id = read_current_ids(conn)
    if range_id is None or rules_id is None:
        return {
            "range_id": range_id,
            "rules_id": rules_id,
            "range": None,
            "range_meta": None,
            "rules_version_meta": None,
            "rules": [],
        }
    return load_versioned(conn, range_id, rules_id)


def list_attempts(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM publish_attempts ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_attempt(conn: sqlite3.Connection, attempt_id: int) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM publish_attempts WHERE id=?", (attempt_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def get_db() -> sqlite3.Connection:
    """请求作用域连接（路径取自 app.config['DATABASE']）。"""
    if "db_conn" not in g:
        g.db_conn = connect(current_app.config["DATABASE"])
    return g.db_conn


def close_db(exc: Optional[BaseException] = None) -> None:
    conn = g.pop("db_conn", None)
    if conn is not None:
        conn.close()
