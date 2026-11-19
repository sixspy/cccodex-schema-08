import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _items(client, path):
    return (await (await client.get(path)).json()).get("items", [])


async def _draft_id(client):
    items = await _items(client, "/api/contracts")
    return next(c["id"] for c in items if c["status"] == "草稿")


async def _apply(client, actor="甲"):
    cid = await _draft_id(client)
    r = await client.post(f"/api/contracts/{cid}/apply", json={"actor": actor})
    assert r.status == 200
    return (await r.json())["contract"]


async def test_rollback_retain_mode_keeps_misread(authed):
    applied = await _apply(authed)
    assert applied["version_no"] == 2

    r = await authed.post(
        "/api/rollback/dw_orders_d",
        json={"mode": "部分留存", "actor": "甲"},
    )
    assert r.status == 200
    card = (await r.json())["rollback"]
    assert card["mis_state"] == "只读历史"
    assert card["mis_readable"] is True
    assert card["verified"] is True
    assert [v["consistent"] for v in card["verifications"]] == [True, True]
    assert {v["round_no"] for v in card["verifications"]} == {1, 2}

    items = await _items(authed, "/api/contracts")
    mis = next(c for c in items if c["version_no"] == 2)
    assert mis["status"] == "只读历史"
    assert mis["readable"] is True
    live = next(c for c in items if c["status"] == "现行" and c["table_name"] == "dw_orders_d")
    assert live["version_no"] == 1

    # 只读历史仍能读到字段。
    detail = await authed.get(f"/api/contracts/{mis['id']}")
    assert detail.status == 200
    assert "currency" in (await detail.json())["fields"]


async def test_rollback_full_mode_seals_misread(authed):
    await _apply(authed)
    r = await authed.post("/api/rollback/dw_orders_d", json={"mode": "整笔回滚", "actor": "甲"})
    assert r.status == 200
    card = (await r.json())["rollback"]
    assert card["mis_state"] == "停用"
    assert card["mis_readable"] is False

    items = await _items(authed, "/api/contracts")
    mis = next(c for c in items if c["version_no"] == 2)
    assert mis["status"] == "停用"
    assert mis["readable"] is False
    detail = await authed.get(f"/api/contracts/{mis['id']}")
    assert detail.status == 409


async def test_rollback_requires_one_fate(authed):
    await _apply(authed)
    r = await authed.post("/api/rollback/dw_orders_d", json={"actor": "甲"})
    assert r.status == 400
    assert "二选一" in (await r.json())["error"]


async def test_rollback_blocks_when_contract_catalog_skewed(authed, client):
    await _apply(authed)
    from app.db import get_db

    db_path = client.app["db_path"]
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE catalog_tables SET live_version=9 WHERE table_name='dw_orders_d'"
        )
    preview = (await (await authed.get("/api/rollback/dw_orders_d/preview")).json())["preview"]
    assert preview["can_rollback"] is False
    assert "拧了" in preview["blocked_reason"]

    r = await authed.post("/api/rollback/dw_orders_d", json={"mode": "整笔回滚", "actor": "甲"})
    assert r.status == 409
    assert "拧了" in (await r.json())["error"]


async def test_rollback_blocks_when_page_skewed(authed, client):
    await _apply(authed)
    from app.db import get_db

    with get_db(client.app["db_path"]) as conn:
        conn.execute(
            "UPDATE catalog_pages SET page_state='停用' "
            "WHERE table_name='dw_orders_d' AND version_no=2"
        )
    r = await authed.post("/api/rollback/dw_orders_d", json={"mode": "整笔回滚", "actor": "甲"})
    assert r.status == 409
    assert "附页" in (await r.json())["error"]


async def test_rollback_window_closes(authed, client):
    await _apply(authed)
    from app.db import get_db

    with get_db(client.app["db_path"]) as conn:
        conn.execute("UPDATE apply_runs SET created_at=datetime('now','-90 minutes')")
    preview = (await (await authed.get("/api/rollback/dw_orders_d/preview")).json())["preview"]
    assert preview["window_open"] is False
    assert "窗口" in preview["blocked_reason"]

    r = await authed.post("/api/rollback/dw_orders_d", json={"mode": "部分留存", "actor": "甲"})
    assert r.status == 409
    assert "窗口" in (await r.json())["error"]


async def test_second_operator_cannot_double_write(authed):
    await _apply(authed)
    claim = await authed.post("/api/rollback/dw_orders_d/lock", json={"actor": "甲"})
    assert claim.status == 200

    denied = await authed.post(
        "/api/rollback/dw_orders_d", json={"mode": "整笔回滚", "actor": "乙"}
    )
    assert denied.status == 409
    assert "甲" in (await denied.json())["error"]

    # 交还后仍归甲，回退可过。
    assert (await authed.post("/api/rollback/dw_orders_d/lock", json={"actor": "甲"})).status == 200
    ok = await authed.post("/api/rollback/dw_orders_d", json={"mode": "整笔回滚", "actor": "甲"})
    assert ok.status == 200


async def test_reconcile_same_version_after_rollback(authed):
    await _apply(authed)
    await authed.post("/api/rollback/dw_orders_d", json={"mode": "部分留存", "actor": "甲"})

    rep = (await (await authed.post("/api/reconcile-reports", json={})).json())["report"]
    assert rep["drifted"] == 0
    line = next(li for li in rep["lines"] if li["table_name"] == "dw_orders_d")
    assert line["contract_version"] == line["catalog_version"] == line["page_version"] == 1
    assert line["aligned"] is True


async def test_timeline_records_both_destinies(authed):
    await _apply(authed)
    await authed.post("/api/rollback/dw_orders_d", json={"mode": "整笔回滚", "actor": "甲"})

    events = await _items(authed, "/api/timeline?table=dw_orders_d")
    kinds = [e["event"] for e in events]
    assert "换版" in kinds
    assert "回退让位" in kinds
    assert "挂回现行" in kinds
    displaced = next(e for e in events if e["event"] == "回退让位")
    assert displaced["version_no"] == 2
    assert displaced["to_state"] == "停用"

    pages = await _items(authed, "/api/catalog-pages?table=dw_orders_d")
    live_pages = [p for p in pages if p["page_state"] == "现行"]
    assert len(live_pages) == 1
    assert live_pages[0]["version_no"] == 1


async def test_verification_rounds_recorded_and_listed(authed):
    await _apply(authed)
    card = (await (await authed.post(
        "/api/rollback/dw_orders_d", json={"mode": "部分留存", "actor": "甲"}
    )).json())["rollback"]

    listed = (await (await authed.get("/api/rollback/rollbacks")).json())["items"]
    row = next(r for r in listed if r["id"] == card["id"])
    assert row["verified"] is True
    assert row["status"] == "已回退·两轮一致"
    assert [v["round_no"] for v in row["verifications"]] == [1, 2]
    assert all(v["consistent"] is True for v in row["verifications"])
    for v in row["verifications"]:
        assert v["contract_version"] == v["catalog_version"] == v["page_version"] == 1

    from app.db import get_db

    with get_db(authed.app["db_path"]) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM rollback_verifications WHERE rollback_id=?",
            (card["id"],),
        ).fetchone()[0] == 2


async def test_concurrent_rollback_from_two_operators_is_serialized(authed, client):
    """两名值班同时点回退：写事务串行化，只落一张卡、只改一次版指针。"""
    import asyncio

    db_path = client.app["db_path"]
    await _apply(authed)

    from app.services import rollback_table
    from app.services.errors import ServiceError

    async def attempt(actor, mode):
        try:
            return ("ok", await asyncio.to_thread(
                rollback_table, "dw_orders_d", mode, actor=actor, db_path=db_path
            ))
        except ServiceError as exc:
            return ("blocked", exc.message)

    results = await asyncio.gather(
        attempt("甲", "整笔回滚"),
        attempt("乙", "部分留存"),
    )
    outcomes = sorted(r[0] for r in results)
    assert outcomes == ["blocked", "ok"]

    from app.db import get_db

    with get_db(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM rollbacks").fetchone()[0] == 1
        live = conn.execute(
            "SELECT version_no FROM schema_contracts "
            "WHERE table_name='dw_orders_d' AND status='现行'"
        ).fetchall()
        assert [r["version_no"] for r in live] == [1]
        assert conn.execute(
            "SELECT live_version FROM catalog_tables WHERE table_name='dw_orders_d'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM catalog_pages "
            "WHERE table_name='dw_orders_d' AND page_state='现行'"
        ).fetchone()[0] == 1
