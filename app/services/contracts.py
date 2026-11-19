from datetime import timedelta

from app.config import Config
from app.db import get_db, write_tx
from app.services.errors import ServiceError
from app.services.timeutil import iso, parse_db_ts

READONLY = "只读历史"


def _fields(conn, contract_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT field_name, field_type FROM contract_fields WHERE contract_id=?",
        (contract_id,),
    ).fetchall()
    return {r["field_name"]: r["field_type"] for r in rows}


def _contract_view(row) -> dict:
    item = dict(row)
    # 误升版被整笔回滚后落『停用』，不可再读；部分留存落『只读历史』，仍可查。
    item["readable"] = item["status"] != "停用"
    return item


def list_contracts(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM contract_fields f WHERE f.contract_id=c.id) AS field_count "
            "FROM schema_contracts c ORDER BY c.table_name, c.version_no"
        ).fetchall()
        return [_contract_view(r) for r in rows]


def get_contract_fields(contract_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not row:
            raise ServiceError("契约不存在", 404)
        if row["status"] == "停用":
            raise ServiceError(
                f"{row['table_name']} v{row['version_no']} 已停用封存，契约不可读；"
                "若想留档可查，回退时选『部分留存』",
                409,
            )
        return {"contract": _contract_view(row), "fields": _fields(conn, contract_id)}


def run_breaking_check(contract_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        draft = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not draft:
            raise ServiceError("契约不存在", 404)
        if draft["status"] != "草稿":
            raise ServiceError("仅草稿可做破坏性检查", 409)
        live = conn.execute(
            "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'",
            (draft["table_name"],),
        ).fetchone()
        if not live:
            raise ServiceError("无现行契约可对照", 409)
        old_f = _fields(conn, live["id"])
        new_f = _fields(conn, draft["id"])
        removed = sorted(set(old_f) - set(new_f))
        type_changed = sorted(
            k for k in set(old_f) & set(new_f) if old_f[k] != new_f[k]
        )
        added = sorted(set(new_f) - set(old_f))
        breaking = bool(removed or type_changed)
        return {
            "contract_id": contract_id,
            "table_name": draft["table_name"],
            "from_version": live["version_no"],
            "to_version": draft["version_no"],
            "removed_fields": removed,
            "type_changed_fields": type_changed,
            "added_fields": added,
            "breaking": breaking,
            "verdict": "破坏性" if breaking else "兼容",
        }


def state_of_contract(conn, table_name: str, version_no: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM schema_contracts WHERE table_name=? AND version_no=?",
        (table_name, version_no),
    ).fetchone()
    return dict(row) if row else None


def catalog_pointer(conn, table_name: str) -> int | None:
    row = conn.execute(
        "SELECT live_version FROM catalog_tables WHERE table_name=?", (table_name,)
    ).fetchone()
    return int(row["live_version"]) if row else None


def latest_apply_run(conn, table_name: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM apply_runs WHERE table_name=? AND result='已应用' "
        "ORDER BY id DESC LIMIT 1",
        (table_name,),
    ).fetchone()
    return dict(row) if row else None


def live_contract(conn, table_name: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'",
        (table_name,),
    ).fetchone()
    return dict(row) if row else None


def page_of(conn, table_name: str, version_no: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM catalog_pages WHERE table_name=? AND version_no=?",
        (table_name, version_no),
    ).fetchone()
    return dict(row) if row else None


def current_page(conn, table_name: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM catalog_pages WHERE table_name=? AND page_state='现行' "
        "ORDER BY page_no DESC LIMIT 1",
        (table_name,),
    ).fetchone()
    return dict(row) if row else None


def source_skew(conn, table_name: str, live: dict | None = None) -> str | None:
    """契约版与目录版（指针 + 附页）必须同源；拧了返回白话原因。"""
    if live is None:
        row = conn.execute(
            "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'",
            (table_name,),
        ).fetchone()
        live = dict(row) if row else None
    if not live:
        return None
    pointer = catalog_pointer(conn, table_name)
    if pointer != live["version_no"]:
        return (
            f"契约现行 v{live['version_no']} 与目录指针 v{pointer} 拧了，"
            "先把目录对账拉回同一版号，再谈回退"
        )
    page = current_page(conn, table_name)
    if page is None:
        return (
            f"目录附页缺了 v{live['version_no']} 的现行页，先补齐附页再谈回退"
        )
    if page["version_no"] != live["version_no"]:
        return (
            f"契约现行 v{live['version_no']} 与目录附页现行页 v{page['version_no']} 拧了，"
            "先对齐附页再谈回退"
        )
    return None


def _next_page_no(conn, table_name: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(page_no), 0) AS n FROM catalog_pages WHERE table_name=?",
        (table_name,),
    ).fetchone()
    return int(row["n"]) + 1


def _event(conn, table, version, event, from_state, to_state, actor, note=""):
    conn.execute(
        "INSERT INTO version_events(table_name, version_no, event, from_state, to_state, actor, note) "
        "VALUES (?,?,?,?,?,?,?)",
        (table, version, event, from_state or "", to_state or "", actor or "", note or ""),
    )


def apply_contract(
    contract_id: int,
    actor: str = "",
    note: str = "",
    db_path: str | None = None,
) -> dict:
    """换版应用：草稿升现行，旧现行停用；契约、目录指针、目录附页一次改写。"""
    from app.services import locks

    with write_tx(db_path) as conn:
        draft = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not draft:
            raise ServiceError("契约不存在", 404)
        if draft["status"] != "草稿":
            raise ServiceError("仅草稿可换版应用", 409)
        table = draft["table_name"]
        locks.acquire(conn, table, actor)
        live = conn.execute(
            "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'",
            (table,),
        ).fetchone()
        from_ver = live["version_no"] if live else None
        if live:
            conn.execute("UPDATE schema_contracts SET status='停用' WHERE id=?", (live["id"],))
        conn.execute("UPDATE schema_contracts SET status='现行' WHERE id=?", (contract_id,))
        conn.execute(
            "INSERT INTO catalog_tables(table_name, live_version) VALUES (?,?) "
            "ON CONFLICT(table_name) DO UPDATE SET live_version=excluded.live_version, "
            "registered_at=datetime('now')",
            (table, draft["version_no"]),
        )
        page_no = _next_page_no(conn, table)
        conn.execute(
            "UPDATE catalog_pages SET page_state='停用', updated_at=datetime('now') "
            "WHERE table_name=? AND page_state='现行'",
            (table,),
        )
        conn.execute(
            "INSERT INTO catalog_pages(table_name, version_no, page_no, page_state) "
            "VALUES (?,?,?,'现行') "
            "ON CONFLICT(table_name, version_no) DO UPDATE SET "
            "page_state='现行', page_no=excluded.page_no, updated_at=datetime('now')",
            (table, draft["version_no"], page_no),
        )
        cur = conn.execute(
            "INSERT INTO apply_runs(contract_id, table_name, from_version, to_version, result, actor, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (contract_id, table, from_ver, draft["version_no"], "已应用", actor, note),
        )
        run_id = cur.lastrowid
        applied_at = conn.execute(
            "SELECT created_at FROM apply_runs WHERE id=?", (run_id,)
        ).fetchone()["created_at"]
        _event(
            conn, table, draft["version_no"], "换版",
            live["status"] if live else "无", "现行", actor, note,
        )
        locks.release(conn, table, actor)
        row = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        result = _contract_view(row)
        result["apply_run_id"] = run_id
        result["from_version"] = from_ver
        result["applied_at"] = iso(applied_at)
        deadline = parse_db_ts(applied_at)
        if deadline is not None:
            deadline = deadline + timedelta(minutes=Config.rollback_window_minutes())
        result["window_deadline"] = iso(deadline)
        result["window_minutes"] = Config.rollback_window_minutes()
        return result


def list_timeline(table_name: str | None = None, db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        if table_name:
            rows = conn.execute(
                "SELECT * FROM version_events WHERE table_name=? ORDER BY id ASC",
                (table_name,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM version_events ORDER BY table_name, id"
            ).fetchall()
        items = []
        for r in rows:
            item = dict(r)
            item["at"] = iso(r["created_at"])
            items.append(item)
        return items


def list_catalog_pages(table_name: str | None = None, db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        if table_name:
            rows = conn.execute(
                "SELECT * FROM catalog_pages WHERE table_name=? ORDER BY version_no",
                (table_name,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM catalog_pages ORDER BY table_name, version_no"
            ).fetchall()
        items = []
        for r in rows:
            item = dict(r)
            item["updated_at"] = iso(r["updated_at"])
            items.append(item)
        return items
