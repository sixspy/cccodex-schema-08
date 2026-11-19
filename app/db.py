import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 两名值班同时写时，让后到者在库锁上排队，而不是立刻报错。
    conn.execute(f"PRAGMA busy_timeout = {int(Config.lock_wait_seconds() * 1000)}")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_tx(db_path: str | None = None):
    """写事务：BEGIN IMMEDIATE 先拿写锁，读到哪儿写到哪儿都在同一把锁里。

    回退与换版都走这里，避免两名值班同时读到旧版指针后再各写一次。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_contracts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('现行','草稿','停用','只读历史')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(table_name, version_no)
);

CREATE TABLE IF NOT EXISTS contract_fields (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL REFERENCES schema_contracts(id),
  field_name TEXT NOT NULL,
  field_type TEXT NOT NULL,
  UNIQUE(contract_id, field_name)
);

CREATE TABLE IF NOT EXISTS catalog_tables (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  live_version INTEGER NOT NULL,
  registered_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 目录附页：目录自己按版本留的页，回退时与契约各退各的，必须对齐。
CREATE TABLE IF NOT EXISTS catalog_pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  page_no INTEGER NOT NULL,
  page_state TEXT NOT NULL CHECK (page_state IN ('现行','停用','只读历史')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(table_name, version_no)
);

CREATE TABLE IF NOT EXISTS apply_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL,
  table_name TEXT NOT NULL,
  from_version INTEGER,
  to_version INTEGER NOT NULL,
  result TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of TEXT NOT NULL,
  watermark TEXT NOT NULL DEFAULT '',
  matched INTEGER NOT NULL,
  drifted INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 对账水线的逐表版号：同一张表要看到契约版 / 目录版 / 附页版三个数。
CREATE TABLE IF NOT EXISTS reconcile_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id INTEGER NOT NULL REFERENCES reconcile_reports(id),
  table_name TEXT NOT NULL,
  contract_version INTEGER,
  catalog_version INTEGER,
  page_version INTEGER,
  aligned INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);

-- 版本时间线：登记 / 换版 / 回退 / 复验，一条不可改。
CREATE TABLE IF NOT EXISTS version_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  event TEXT NOT NULL,
  from_state TEXT NOT NULL DEFAULT '',
  to_state TEXT NOT NULL DEFAULT '',
  actor TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 回滚卡片：一张卡只贯彻一条命运。
CREATE TABLE IF NOT EXISTS rollbacks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('整笔回滚','部分留存')),
  fate TEXT NOT NULL,
  mis_version INTEGER NOT NULL,
  target_version INTEGER NOT NULL,
  mis_state TEXT NOT NULL,
  mis_readable INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT '已回退',
  actor TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  window_deadline TEXT NOT NULL,
  apply_run_id INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 回退后的两轮复验：两轮都要同一版号才算收口。
CREATE TABLE IF NOT EXISTS rollback_verifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rollback_id INTEGER NOT NULL REFERENCES rollbacks(id),
  round_no INTEGER NOT NULL,
  contract_version INTEGER,
  catalog_version INTEGER,
  page_version INTEGER,
  consistent INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(rollback_id, round_no)
);

-- 值班锁：同表同一时刻只许一名值班改写版指针。
CREATE TABLE IF NOT EXISTS resource_locks (
  table_name TEXT PRIMARY KEY,
  holder TEXT NOT NULL,
  acquired_at TEXT NOT NULL DEFAULT (datetime('now')),
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

SEED = [
    ("dw_orders_d", 1, "现行", "初始现行", [("order_id", "INT"), ("amount", "DECIMAL"), ("biz_date", "DATE")]),
    ("dw_orders_d", 2, "草稿", "拟删 amount 改币种", [("order_id", "INT"), ("currency", "STRING"), ("biz_date", "DATE")]),
    ("dw_payments_d", 1, "现行", "支付现行", [("pay_id", "INT"), ("order_id", "INT"), ("paid_at", "TIMESTAMP")]),
]


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """给老库补上后加的列；CREATE TABLE IF NOT EXISTS 不会改已存在的表。"""
    wanted = {
        "apply_runs": {
            "actor": "TEXT NOT NULL DEFAULT ''",
            "note": "TEXT NOT NULL DEFAULT ''",
        },
        "reconcile_reports": {
            "watermark": "TEXT NOT NULL DEFAULT ''",
        },
    }
    for table, columns in wanted.items():
        existing = {
            r["name"]
            for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    conn.execute(
        "UPDATE reconcile_reports SET watermark=as_of WHERE watermark=''"
    )


def _migrate(conn: sqlite3.Connection) -> None:
    """老库的 status 约束里没有『只读历史』，就地重建表并把数据抬过去。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='schema_contracts'"
    ).fetchone()
    if not row or "只读历史" in (row["sql"] or ""):
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        CREATE TABLE schema_contracts_new (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          table_name TEXT NOT NULL,
          version_no INTEGER NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('现行','草稿','停用','只读历史')),
          note TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          UNIQUE(table_name, version_no)
        );
        INSERT INTO schema_contracts_new(id, table_name, version_no, status, note, created_at)
          SELECT id, table_name, version_no, status, note, created_at FROM schema_contracts;
        DROP TABLE schema_contracts;
        ALTER TABLE schema_contracts_new RENAME TO schema_contracts;
        """
    )
    conn.execute("PRAGMA foreign_keys = ON")


def _seed(conn: sqlite3.Connection) -> None:
    for table, ver, status, note, fields in SEED:
        cur = conn.execute(
            "INSERT INTO schema_contracts(table_name, version_no, status, note) VALUES (?,?,?,?)",
            (table, ver, status, note),
        )
        cid = cur.lastrowid
        for fname, ftype in fields:
            conn.execute(
                "INSERT INTO contract_fields(contract_id, field_name, field_type) VALUES (?,?,?)",
                (cid, fname, ftype),
            )
        if status == "现行":
            conn.execute(
                "INSERT OR REPLACE INTO catalog_tables(table_name, live_version) VALUES (?,?)",
                (table, ver),
            )
            conn.execute(
                "INSERT INTO catalog_pages(table_name, version_no, page_no, page_state) "
                "VALUES (?,?,?, '现行')",
                (table, ver, 1),
            )
        conn.execute(
            "INSERT INTO version_events(table_name, version_no, event, from_state, to_state, actor, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (table, ver, "登记", "无", status, "system", note),
        )
    conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")


def _backfill(conn: sqlite3.Connection) -> None:
    """老库已有数据时，补齐目录附页与时间线，不改动既有版号。"""
    rows = conn.execute("SELECT * FROM schema_contracts").fetchall()
    for c in rows:
        exists = conn.execute(
            "SELECT 1 FROM catalog_pages WHERE table_name=? AND version_no=?",
            (c["table_name"], c["version_no"]),
        ).fetchone()
        if not exists and c["status"] != "草稿":
            page_no = conn.execute(
                "SELECT COALESCE(MAX(page_no), 0) + 1 FROM catalog_pages WHERE table_name=?",
                (c["table_name"],),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO catalog_pages(table_name, version_no, page_no, page_state) "
                "VALUES (?,?,?,?)",
                (c["table_name"], c["version_no"], page_no, c["status"]),
            )
    if not conn.execute("SELECT 1 FROM version_events LIMIT 1").fetchone():
        for c in rows:
            conn.execute(
                "INSERT INTO version_events(table_name, version_no, event, from_state, to_state, actor, note) "
                "VALUES (?,?,?,?,?,?,?)",
                (c["table_name"], c["version_no"], "登记", "无", c["status"], "system", c["note"]),
            )


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _ensure_columns(conn)
        if not conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone():
            _seed(conn)
        else:
            _backfill(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
