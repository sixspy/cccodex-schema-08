"""值班锁：同一张表同一时刻只许一名值班改写版指针。"""
from datetime import timedelta

from app.config import Config
from app.services.errors import ServiceError
from app.services.timeutil import iso, parse_db_ts, utcnow


def _fmt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def acquire(conn, table_name: str, holder: str) -> None:
    """在写事务内抢锁；别人没交还且未过期时直接拦住。"""
    holder = (holder or "").strip() or "匿名值班"
    now = utcnow()
    row = conn.execute(
        "SELECT holder, expires_at FROM resource_locks WHERE table_name=?",
        (table_name,),
    ).fetchone()
    if row and row["holder"] != holder:
        expires = parse_db_ts(row["expires_at"])
        if expires and expires > now:
            raise ServiceError(
                f"「{table_name}」的回退锁还在值班「{row['holder']}」手里，"
                "等锁过期或由对方交还后再点回退",
                409,
            )
    lease = timedelta(seconds=Config.lock_ttl_seconds())
    conn.execute(
        "INSERT INTO resource_locks(table_name, holder, acquired_at, expires_at) "
        "VALUES (?,?,datetime('now'),?) "
        "ON CONFLICT(table_name) DO UPDATE SET holder=excluded.holder, "
        "acquired_at=excluded.acquired_at, expires_at=excluded.expires_at",
        (table_name, holder, _fmt(now + lease)),
    )


def release(conn, table_name: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM resource_locks WHERE table_name=? AND holder=?",
        (table_name, (holder or "").strip() or "匿名值班"),
    )


def holder_of(conn, table_name: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM resource_locks WHERE table_name=?", (table_name,)
    ).fetchone()
    return dict(row) if row else None


def claim(db_path: str | None, table_name: str, holder: str) -> dict:
    """值班开卡占锁：回滚卡片上显示锁主，确认回退还给同一人。"""
    from app.db import write_tx

    with write_tx(db_path) as conn:
        acquire(conn, table_name, holder)
        row = holder_of(conn, table_name)
        if row is None:
            raise ServiceError("回退锁没抢上，请重试", 409)
        row["expires_at"] = iso(row["expires_at"])
        row["acquired_at"] = iso(row["acquired_at"])
        return row


def release_claim(db_path: str | None, table_name: str, holder: str) -> None:
    from app.db import write_tx

    with write_tx(db_path) as conn:
        row = holder_of(conn, table_name)
        if row and row["holder"] != ((holder or "").strip() or "匿名值班"):
            raise ServiceError(
                f"锁在值班「{row['holder']}」手里，不归「{holder}」交还", 409
            )
        release(conn, table_name, holder)
