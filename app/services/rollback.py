"""误升回退：一笔事务里把契约、目录指针、目录附页一起退到位。

回退命运只许贯彻一条：
- 整笔回滚：契约与目录一起退回旧版，误升版落『停用』，不可再读；
- 部分留存：旧版挂回现行，误升版落『只读历史』，仍可查阅。

两条路都会写：apply_runs（结果=已回退）、rollbacks（回滚卡片）、
version_events（版本时间线）、reconcile 水线（后续对账）、
rollback_verifications（两轮复验）。
"""
from datetime import datetime, timedelta

from app.config import Config
from app.db import get_db, write_tx
from app.services import locks
from app.services.contracts import (
    catalog_pointer,
    current_page,
    latest_apply_run,
    live_contract,
    source_skew,
)
from app.services.errors import ServiceError
from app.services.timeutil import iso, parse_db_ts, utcnow

MODE_FULL = "整笔回滚"
MODE_RETAIN = "部分留存"
MODE_FATES = {
    MODE_FULL: {
        "fate": "整笔回滚 · 误升版封存不可读",
        "mis_state": "停用",
        "mis_readable": False,
    },
    MODE_RETAIN: {
        "fate": "部分留存 · 误升版留档可读",
        "mis_state": "只读历史",
        "mis_readable": True,
    },
}


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


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


def _window(run: dict) -> tuple[datetime | None, datetime | None]:
    applied = parse_db_ts(run.get("created_at"))
    if applied is None:
        return None, None
    return applied, applied + timedelta(minutes=Config.rollback_window_minutes())


def _card_view(row, verifications: list[dict] | None = None) -> dict:
    item = dict(row)
    item["window_deadline"] = iso(row["window_deadline"])
    item["created_at"] = iso(row["created_at"])
    item["mis_readable"] = bool(row["mis_readable"])
    item["verifications"] = verifications or []
    return item


def _verify_round(conn, rollback_id: int, round_no: int, table_name: str, expected: int) -> dict:
    live_ver = conn.execute(
        "SELECT version_no FROM schema_contracts WHERE table_name=? AND status='现行'",
        (table_name,),
    ).fetchone()
    pointer = catalog_pointer(conn, table_name)
    page = current_page(conn, table_name)
    page_ver = page["version_no"] if page else None
    contract_ver = live_ver["version_no"] if live_ver else None
    consistent = (
        contract_ver == expected and pointer == expected and page_ver == expected
    )
    detail = (
        f"第{round_no}轮：契约v{contract_ver}/目录v{pointer}/附页v{page_ver}"
        + ("，三处同版" if consistent else "，版号还没对齐")
    )
    conn.execute(
        "INSERT INTO rollback_verifications"
        "(rollback_id, round_no, contract_version, catalog_version, page_version, consistent, detail) "
        "VALUES (?,?,?,?,?,?,?)",
        (rollback_id, round_no, contract_ver, pointer, page_ver, 1 if consistent else 0, detail),
    )
    return {
        "round_no": round_no,
        "contract_version": contract_ver,
        "catalog_version": pointer,
        "page_version": page_ver,
        "consistent": consistent,
        "detail": detail,
    }


def _block_reason(conn, table_name: str, live: dict) -> tuple[str | None, dict | None, datetime | None]:
    """回退前一票否决：返回（白话原因, 最近换版流水, 窗口截止）。

    预览与执行共用这一处判断，页面看到的拦因就是点回退时收到的拦因。
    """
    run = latest_apply_run(conn, table_name)
    _, deadline = _window(run) if run else (None, None)
    already = conn.execute(
        "SELECT id FROM rollbacks WHERE table_name=? AND target_version=? "
        "ORDER BY id DESC LIMIT 1",
        (table_name, live["version_no"]),
    ).fetchone()
    skew = source_skew(conn, table_name, live)
    if skew:
        return skew, run, deadline
    if not run:
        return "这一版从登记起就是现行，没有换版流水，不需要回退", run, deadline
    if run["to_version"] != live["version_no"]:
        if already:
            return (
                f"这一笔已经回退过（回滚卡片 #{already['id']}），同一笔误升只退一次；"
                "要再走请重新换版",
                run,
                deadline,
            )
        return (
            f"现行 v{live['version_no']} 不是最近一次换版升上去的，回退点不明，"
            "先核对版本时间线",
            run,
            deadline,
        )
    if not run["from_version"]:
        return (
            f"v{live['version_no']} 是首次升版，之前没有现行版本，没有可退回的上一版",
            run,
            deadline,
        )
    if deadline is not None and utcnow() > deadline:
        return (
            f"回滚窗口已在 {iso(deadline)} 关闭，超出换版后 "
            f"{Config.rollback_window_minutes()} 分钟的时限；要走只能重新换版再退",
            run,
            deadline,
        )
    return None, run, deadline


def rollback_table(
    table_name: str,
    mode: str,
    actor: str = "",
    note: str = "",
    db_path: str | None = None,
) -> dict:
    if mode not in MODE_FATES:
        raise ServiceError("回退命运只能二选一：整笔回滚或部分留存", 400)
    fate = MODE_FATES[mode]
    actor = (actor or "").strip() or "匿名值班"

    with write_tx(db_path) as conn:
        live = live_contract(conn, table_name)
        if not live:
            raise ServiceError(f"「{table_name}」没有现行契约，无从回退", 409)

        reason, run, deadline = _block_reason(conn, table_name, live)
        if reason:
            raise ServiceError(reason, 409)
        now = utcnow()

        # 抢锁放在所有只读核验之后、任何写入之前。
        locks.acquire(conn, table_name, actor)

        target = conn.execute(
            "SELECT * FROM schema_contracts WHERE table_name=? AND version_no=?",
            (table_name, run["from_version"]),
        ).fetchone()
        if not target:
            raise ServiceError(
                f"「{table_name}」找不到 v{run['from_version']} 的契约条目，回退目标缺失",
                409,
            )

        mis_state_before = live["status"]

        # ① 契约各退各的：误升版让位，目标版挂回现行。
        conn.execute(
            "UPDATE schema_contracts SET status=? WHERE id=?",
            (fate["mis_state"], live["id"]),
        )
        conn.execute(
            "UPDATE schema_contracts SET status='现行' WHERE id=?", (target["id"],)
        )

        # ② 目录指针退回同一版号。
        conn.execute(
            "UPDATE catalog_tables SET live_version=?, registered_at=datetime('now') "
            "WHERE table_name=?",
            (target["version_no"], table_name),
        )

        # ③ 目录附页各退各的：误升版的页跟着命运走，目标页翻回现行。
        #    先给误升版的页定命，再把其余现行页收掉，最后把目标页翻现行。
        conn.execute(
            "UPDATE catalog_pages SET page_state=?, updated_at=datetime('now') "
            "WHERE table_name=? AND version_no=?",
            (fate["mis_state"], table_name, live["version_no"]),
        )
        conn.execute(
            "UPDATE catalog_pages SET page_state='停用', updated_at=datetime('now') "
            "WHERE table_name=? AND page_state='现行' AND version_no<>?",
            (table_name, target["version_no"]),
        )
        existing_target_page = conn.execute(
            "SELECT id FROM catalog_pages WHERE table_name=? AND version_no=?",
            (table_name, target["version_no"]),
        ).fetchone()
        if existing_target_page:
            conn.execute(
                "UPDATE catalog_pages SET page_state='现行', updated_at=datetime('now') "
                "WHERE id=?",
                (existing_target_page["id"],),
            )
        else:
            conn.execute(
                "INSERT INTO catalog_pages(table_name, version_no, page_no, page_state) "
                "VALUES (?,?,?,'现行')",
                (table_name, target["version_no"], _next_page_no(conn, table_name)),
            )

        # ④ 回退本身也记一笔应用流水（result='已回退'，不干扰『最近一次换版』的判定）。
        conn.execute(
            "INSERT INTO apply_runs(contract_id, table_name, from_version, to_version, result, actor, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (target["id"], table_name, live["version_no"], target["version_no"], "已回退", actor, note),
        )

        # ⑤ 回滚卡片：窗口时限 + 命运芯片。
        card = conn.execute(
            "INSERT INTO rollbacks(table_name, mode, fate, mis_version, target_version, "
            "mis_state, mis_readable, status, actor, note, window_deadline, apply_run_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                table_name,
                mode,
                fate["fate"],
                live["version_no"],
                target["version_no"],
                fate["mis_state"],
                1 if fate["mis_readable"] else 0,
                "已回退",
                actor,
                note,
                _fmt(deadline) if deadline else _fmt(now),
                run["id"],
            ),
        )
        rollback_id = card.lastrowid

        # ⑥ 版本时间线记两笔，误升版与旧现行各自的去向都能查到。
        _event(
            conn, table_name, live["version_no"], "回退让位",
            mis_state_before, fate["mis_state"], actor,
            f"{mode}：{live['version_no']} 让位于 v{target['version_no']}",
        )
        _event(
            conn, table_name, target["version_no"], "挂回现行",
            "停用", "现行", actor, f"回退 {mode}",
        )

        locks.release(conn, table_name, actor)

        # ⑦ 第一轮复验在同一笔里落库；第二轮回提交后另开一次读取，
        #    两轮都读到同一版号才算收口。
        first = _verify_round(conn, rollback_id, 1, table_name, target["version_no"])

        row = conn.execute("SELECT * FROM rollbacks WHERE id=?", (rollback_id,)).fetchone()
        view = _card_view(row, [first])
        view["table_name"] = table_name
        view["window_minutes"] = Config.rollback_window_minutes()

    # 事务已提交：第二轮复验另开连接再读数。
    with get_db(db_path) as conn:
        second = _verify_round(conn, rollback_id, 2, table_name, target["version_no"])
        verified = bool(first["consistent"] and second["consistent"])
        status = "已回退·两轮一致" if verified else "复验不符"
        conn.execute(
            "UPDATE rollbacks SET status=? WHERE id=?", (status, rollback_id)
        )
        row = conn.execute("SELECT * FROM rollbacks WHERE id=?", (rollback_id,)).fetchone()
        view = _card_view(row, [first, second])
        view["table_name"] = table_name
        view["window_minutes"] = Config.rollback_window_minutes()
        view["verified"] = verified
        return view


def list_rollbacks(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute("SELECT * FROM rollbacks ORDER BY id DESC").fetchall()
        out = []
        for r in rows:
            verifications = [
                dict(v)
                | {
                    "created_at": iso(v["created_at"]),
                    "consistent": bool(v["consistent"]),
                }
                for v in conn.execute(
                    "SELECT * FROM rollback_verifications WHERE rollback_id=? "
                    "ORDER BY round_no",
                    (r["id"],),
                ).fetchall()
            ]
            card = _card_view(r, verifications)
            card["verified"] = (
                len(verifications) == 2
                and all(v["consistent"] for v in verifications)
            )
            out.append(card)
        return out


def rollback_preview(table_name: str, db_path: str | None = None) -> dict:
    """回退卡片预览：返回窗口剩余时限与两条命运各自的后果。"""
    with get_db(db_path) as conn:
        live = live_contract(conn, table_name)
        if not live:
            raise ServiceError(f"「{table_name}」没有现行契约，无从回退", 404)
        return _preview(conn, table_name, live)


def _preview(conn, table_name: str, live: dict) -> dict:
    run = latest_apply_run(conn, table_name)
    applied_at, deadline = _window(run) if run else (None, None)
    remaining = None
    if deadline is not None:
        remaining = max(0, int((deadline - utcnow()).total_seconds() // 60))
    options = [
        {
            "mode": mode,
            "fate": f["fate"],
            "mis_state": f["mis_state"],
            "mis_readable": f["mis_readable"],
        }
        for mode, f in MODE_FATES.items()
    ]
    already = conn.execute(
        "SELECT id FROM rollbacks WHERE table_name=? AND target_version=? "
        "ORDER BY id DESC LIMIT 1",
        (table_name, live["version_no"]),
    ).fetchone()
    # 预览与执行共用同一处判因，页面看到的拦因就是点回退时收到的拦因。
    reason, _, _ = _block_reason(conn, table_name, live)
    return {
        "table_name": table_name,
        "live_version": live["version_no"],
        "target_version": run["from_version"] if run else None,
        "applied_at": iso(applied_at) if applied_at else None,
        "window_deadline": iso(deadline) if deadline else None,
        "window_minutes": Config.rollback_window_minutes(),
        "minutes_left": remaining,
        "window_open": bool(deadline and utcnow() <= deadline),
        "blocked_reason": reason,
        "can_rollback": reason is None,
        "lock_holder": (locks.holder_of(conn, table_name) or {}).get("holder"),
        "rollback_id": already["id"] if already else None,
        "options": options if reason is None else [],
    }


def rollback_targets(db_path: str | None = None) -> list[dict]:
    """回退卡片要联查的表：目录里有的表都列出来，各自带窗口与拦截原因。"""
    with get_db(db_path) as conn:
        names = [
            r["table_name"]
            for r in conn.execute(
                "SELECT table_name FROM catalog_tables ORDER BY table_name"
            ).fetchall()
        ]
        out = []
        for name in names:
            live = live_contract(conn, name)
            if not live:
                out.append(
                    {
                        "table_name": name,
                        "live_version": None,
                        "window_open": False,
                        "minutes_left": None,
                        "blocked_reason": "没有现行契约",
                        "options": [],
                    }
                )
                continue
            out.append(_preview(conn, name, live))
        return out
