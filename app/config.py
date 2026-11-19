import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Config:
    LAB_NAME = "契约换版台"
    SECRET = os.environ.get("SECRET_KEY", "cc-schema-dev-secret")
    ADMIN_USER = "admin"
    ADMIN_PASSWORD = "123456"
    DEFAULT_DB = str(ROOT / "instance" / "schema_lab.db")

    @classmethod
    def database(cls) -> str:
        return os.environ.get("DATABASE_PATH", cls.DEFAULT_DB)

    @classmethod
    def rollback_window_minutes(cls) -> int:
        """回退窗口：换版应用之后，允许多久之内整笔退回上一版。"""
        return int(os.environ.get("ROLLBACK_WINDOW_MINUTES", "30"))

    @classmethod
    def lock_ttl_seconds(cls) -> int:
        """值班锁租期：过期后允许别的值班接手。"""
        return int(os.environ.get("ROLLBACK_LOCK_TTL", "30"))

    @classmethod
    def lock_wait_seconds(cls) -> float:
        """同库写锁排队等待时长。"""
        return float(os.environ.get("ROLLBACK_LOCK_WAIT", "2"))
