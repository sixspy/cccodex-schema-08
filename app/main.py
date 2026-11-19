import json
import sqlite3
from pathlib import Path

from aiohttp import web
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import init_db
from app.services import (
    apply_contract,
    get_contract_fields,
    list_catalog_pages,
    list_contracts,
    list_reports,
    list_rollbacks,
    list_timeline,
    rollback_preview,
    rollback_table,
    rollback_targets,
    run_breaking_check,
    run_reconcile,
)
from app.services import locks
from app.services.errors import ServiceError

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-schema-session")


def create_app(db_path: str | None = None) -> web.Application:
    if db_path:
        import os

        os.environ["DATABASE_PATH"] = db_path
    init_db(db_path)
    app = web.Application(middlewares=[error_middleware])
    app["db_path"] = db_path
    app.router.add_static("/static/", STATIC)
    app.router.add_get("/", root)
    app.router.add_get("/login", login_page)
    app.router.add_get("/app/", spa_page)
    app.router.add_post("/api/login", api_login)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_get("/api/meta", api_meta)
    app.router.add_get("/api/contracts", api_contracts)
    app.router.add_get("/api/contracts/{cid}", api_contract_detail)
    app.router.add_post("/api/contracts/{cid}/breaking-check", api_breaking)
    app.router.add_post("/api/contracts/{cid}/apply", api_apply)
    app.router.add_get("/api/timeline", api_timeline)
    app.router.add_get("/api/catalog-pages", api_catalog_pages)
    app.router.add_get("/api/reconcile-reports", api_reports)
    app.router.add_post("/api/reconcile-reports", api_reconcile)
    app.router.add_get("/api/rollback/targets", api_rollback_targets)
    app.router.add_get("/api/rollback/rollbacks", api_rollbacks)
    app.router.add_get("/api/rollback/{table}/preview", api_rollback_preview)
    app.router.add_post("/api/rollback/{table}/lock", api_lock_claim)
    app.router.add_delete("/api/rollback/{table}/lock", api_lock_release)
    app.router.add_post("/api/rollback/{table}", api_rollback)
    return app


@web.middleware
async def error_middleware(request: web.Request, handler):
    """服务层抛什么，页面就收白话，不把堆栈漏给前端。"""
    try:
        return await handler(request)
    except ServiceError as exc:
        return json_resp({"ok": False, "error": exc.message}, exc.status_code)
    except json.JSONDecodeError:
        return json_resp({"ok": False, "error": "请求体不是合法 JSON"}, 400)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            return json_resp(
                {"ok": False, "error": "另一名值班正在改写这张表的版指针，稍后重试"},
                409,
            )
        raise


def _session(request: web.Request) -> dict:
    raw = request.cookies.get("schema_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: web.Request) -> str:
    user = _session(request).get("user")
    if user != Config.ADMIN_USER:
        raise ServiceError("未登录", 401)
    return user


def json_resp(data: dict, status: int = 200) -> web.Response:
    return web.Response(
        status=status,
        text=json.dumps(data, ensure_ascii=False),
        content_type="application/json",
    )


def _actor(request: web.Request, body: dict | None = None) -> str:
    """值班名：优先用请求里填的值班代号，其次用登录账号。"""
    if body:
        name = str(body.get("actor") or "").strip()
        if name:
            return name
    return require_user(request)


async def root(request: web.Request):
    if _session(request).get("user") == Config.ADMIN_USER:
        raise web.HTTPFound("/app/")
    raise web.HTTPFound("/login")


async def login_page(_request: web.Request):
    return web.FileResponse(STATIC / "login.html")


async def spa_page(request: web.Request):
    if _session(request).get("user") != Config.ADMIN_USER:
        raise web.HTTPFound("/login")
    return web.FileResponse(STATIC / "index.html")


async def api_login(request: web.Request):
    body = await request.json()
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return json_resp({"ok": False, "error": "账号或密码错误"}, 400)
    resp = json_resp({"ok": True, "user": user})
    resp.set_cookie(
        "schema_session",
        serializer.dumps({"user": user}),
        httponly=True,
        path="/",
    )
    return resp


async def api_logout(_request: web.Request):
    resp = json_resp({"ok": True})
    resp.del_cookie("schema_session", path="/")
    return resp


async def api_meta(request: web.Request):
    require_user(request)
    return json_resp(
        {
            "lab": Config.LAB_NAME,
            "product": "cc-schema-lab",
            "rollback_window_minutes": Config.rollback_window_minutes(),
        }
    )


async def api_contracts(request: web.Request):
    require_user(request)
    return json_resp({"items": list_contracts(request.app.get("db_path"))})


async def api_contract_detail(request: web.Request):
    require_user(request)
    cid = int(request.match_info["cid"])
    return json_resp(get_contract_fields(cid, request.app.get("db_path")))


async def api_breaking(request: web.Request):
    require_user(request)
    cid = int(request.match_info["cid"])
    return json_resp({"check": run_breaking_check(cid, request.app.get("db_path"))})


async def api_apply(request: web.Request):
    body = await _json_body(request)
    cid = int(request.match_info["cid"])
    actor = _actor(request, body)
    note = str(body.get("note") or "").strip()
    return json_resp(
        {"contract": apply_contract(cid, actor=actor, note=note, db_path=request.app.get("db_path"))}
    )


async def api_timeline(request: web.Request):
    require_user(request)
    table = (request.query.get("table") or "").strip() or None
    return json_resp({"items": list_timeline(table, request.app.get("db_path"))})


async def api_catalog_pages(request: web.Request):
    require_user(request)
    table = (request.query.get("table") or "").strip() or None
    return json_resp({"items": list_catalog_pages(table, request.app.get("db_path"))})


async def api_reports(request: web.Request):
    require_user(request)
    return json_resp({"items": list_reports(request.app.get("db_path"))})


async def api_reconcile(request: web.Request):
    require_user(request)
    body = await _json_body(request)
    as_of = str(body.get("as_of") or "").strip()
    return json_resp({"report": run_reconcile(as_of, request.app.get("db_path"))})


async def api_rollback_targets(request: web.Request):
    require_user(request)
    return json_resp({"items": rollback_targets(request.app.get("db_path"))})


async def api_rollbacks(request: web.Request):
    require_user(request)
    return json_resp({"items": list_rollbacks(request.app.get("db_path"))})


async def api_rollback_preview(request: web.Request):
    require_user(request)
    table = request.match_info["table"]
    return json_resp({"preview": rollback_preview(table, request.app.get("db_path"))})


async def api_rollback(request: web.Request):
    body = await _json_body(request)
    table = request.match_info["table"]
    actor = _actor(request, body)
    mode = str(body.get("mode") or "").strip()
    note = str(body.get("note") or "").strip()
    card = rollback_table(table, mode, actor=actor, note=note, db_path=request.app.get("db_path"))
    return json_resp({"rollback": card})


async def api_lock_claim(request: web.Request):
    body = await _json_body(request)
    table = request.match_info["table"]
    actor = _actor(request, body)
    return json_resp({"lock": locks.claim(request.app.get("db_path"), table, actor)})


async def api_lock_release(request: web.Request):
    body = await _json_body(request)
    table = request.match_info["table"]
    actor = _actor(request, body)
    locks.release_claim(request.app.get("db_path"), table, actor)
    return json_resp({"ok": True, "released": table})


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise ServiceError("请求体不是合法 JSON", 400)
    return body if isinstance(body, dict) else {}
