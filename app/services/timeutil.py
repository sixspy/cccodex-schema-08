"""时间工具：库里统一存 UTC，出接口统一给 ISO-8601（Z 结尾）。"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_db_ts(value) -> datetime | None:
    """解析 SQLite 的 ``datetime('now')``（UTC 裸时间）或 ISO 串。"""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(value) -> str | None:
    dt = parse_db_ts(value)
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today_watermark() -> str:
    """对账水线：按本地日期取版号快照的日期标签。"""
    return datetime.now().date().isoformat()
