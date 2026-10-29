"""第二种引擎验收：PostgreSQL 18（演示门店库 storedb.store）。

在真实 PostgreSQL 上验证引擎适配层：采集（只读系统目录）、视图列级血缘、主键粒度、物理身份（改名 / 删后重建）、
口径发布与问数执行（PostgreSQL 方言）、执行前核对、粒度核验、超时与错误归类、只读事务。全部经 HTTP API。
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from tests.conftest import Api

pytestmark = pytest.mark.acceptance
DAY = "2026-09-15"
ctx: dict = {}


def pg(stack, *statements: str):
    import demo

    _, env, _ = stack
    with demo.pg_connect(env) as conn:
        for statement in statements:
            conn.execute(statement)


def collect(api: Api) -> dict:
    run = api.post(f"/api/datasources/{ctx['pg']}/collect?wait=true")
    assert run["status"] == "succeeded", run
    return run


def assets(api: Api) -> dict[str, dict]:
    return {a["qualified"]: a for a in api.get("/api/assets", params={"datasource_id": ctx["pg"]})}


def latest_events(api: Api) -> set[tuple]:
    cs = api.get(f"/api/change-sets/{api.get('/api/change-sets')[0]['id']}")
    return {(e["kind"], e["asset_id"], e["column"]) for e in cs["events"]}


def value_of(answer: dict) -> Decimal:
    assert answer["status"] == "answered", answer
    return Decimal(str(answer["result"]["rows"][0][-1]))


def test_00_create_and_collect(stack, admin: Api):
    from avelis import demo_setup

    client, env, _ = stack
    demo_setup.setup(reset=False)
    pw = env["AVELIS_DEMO_USER_PASSWORD"]
    ctx["apis"] = {u: Api(client, u, pw) for u in ("zhang", "li", "wang")}
    engines = {e["kind"]: e for e in admin.get("/api/engines")}
    assert engines["postgres"]["name_levels"] == ["database", "schema", "table"]
    assert {o["key"] for o in engines["postgres"]["options"]} == {"database", "schemas"}
    base = {"name": "门店库（PostgreSQL）", "kind": "postgres", "host": "127.0.0.1", "port": int(env["SHOP_PG_PORT"]),
            "collect": {"username": "avelis_collector", "password": env["SHOP_PG_COLLECTOR_PASSWORD"]},
            "query": {"username": "avelis_reader", "password": env["SHOP_PG_READER_PASSWORD"]}}
    refused = admin.post("/api/datasources", {**base, "options": {"database": "storedb"}}, expect=422)
    assert "采集的模式" in refused["detail"]
    ds = admin.post("/api/datasources", {**base, "options": {"database": "storedb", "schemas": ["store"]}}, expect=201)
    assert ds["engine"]["kind"] == "postgres" and "query" in ds["engine"]["capabilities"]
    ctx["pg"] = ds["id"]
    run = collect(admin)
    assert run["stats"]["assets_observed"] == 4
    found = assets(admin)
    assert set(found) == {"storedb.store.shops", "storedb.store.sales", "storedb.store.returns",
                          "storedb.store.city_daily_sales"}
    ctx["ids"] = {q.rsplit(".", 1)[1]: a["id"] for q, a in found.items()}
    zhang = ctx["apis"]["zhang"]
    trade = next(d["id"] for d in admin.get("/api/domains") if d["code"] == "trade")
    for asset_id in ctx["ids"].values():
        zhang.patch(f"/api/assets/{asset_id}", {"domain_id": trade})
    ctx["trade"] = trade


def test_01_view_lineage_and_grains(admin: Api):
    ids = ctx["ids"]
    view = admin.get(f"/api/assets/{ids['city_daily_sales']}")
    assert view["kind"] == "view" and view["parse"]["status"] == "ok"
    assert view["definition"]["raw_sql"].count("store.") >= 2  # 采集时 search_path 为空：表名完全限定
    assert all(c["fingerprint_status"] == "ok" for c in view["columns"])
    assert any(g["columns"] == ["sale_date", "city"] and g["origin"] == "parsed" for g in view["grains"])
    sales = admin.get(f"/api/assets/{ids['sales']}")
    assert any(g["columns"] == ["sale_id"] and g["origin"] == "observed" for g in sales["grains"])
    graph = admin.get("/api/lineage/graph", params={"asset_id": ids["city_daily_sales"]})
    edge = next(e for e in graph["edges"] if e["source"] == ids["sales"] and e["target"] == ids["city_daily_sales"])
    rel = {(c["target_column"], c["source_column"]): c["relation"] for c in edge["columns"]}
    assert rel[("sales_amount", "amount")] == "value_aggregated"
    assert rel[(None, "status")] == "filter" and rel[(None, "shop_id")] == "join_key"


def test_02_publish_and_ask_in_postgres_dialect(admin: Api):
    zhang, li, wang = (ctx["apis"][u] for u in ("zhang", "li", "wang"))
    m = zhang.post("/api/metrics", {"code": "store_sales", "name": "门店销售额", "domain_id": ctx["trade"]},
                   expect=201)
    v = zhang.post(f"/api/metrics/{m['id']}/versions", {
        "description": "门店成交金额按日期、城市汇总（不含作废单）", "base_asset_id": ctx["ids"]["city_daily_sales"],
        "expression": "SUM(sales_amount)", "time_column": "sale_date",
        "dimensions": [{"name": "city", "column": "city", "label": "城市"}], "grain": ["sale_date", "city"]},
        expect=201)
    zhang.post(f"/api/metric-versions/{v['id']}/submit")
    assert li.post(f"/api/metric-versions/{v['id']}/approve", {"note": "PG 验收"})["status"] == "published"
    ctx["store_v1"] = v["id"]
    answer = wang.post("/api/ask", {"question": f"{DAY} 上海门店销售额"})
    assert value_of(answer) == Decimal("250.00")
    assert '"storedb"."store"."city_daily_sales"' in answer["sql"]  # PostgreSQL 方言：双引号标识符
    assert answer["freshness"]["tables"] == {}  # PostgreSQL 不报告新鲜度，不以统计信息冒充
    assert value_of(wang.post("/api/ask", {"question": f"{DAY} 北京门店销售额"})) == Decimal("80.00")


def test_03_grain_verify_on_postgres(admin: Api):
    zhang = ctx["apis"]["zhang"]
    bad = zhang.post(f"/api/assets/{ctx['ids']['returns']}/grains", {"columns": ["sale_id"], "reason": "错误声明"},
                     expect=201)
    checked = zhang.post(f"/api/grains/{bad['id']}/verify")
    assert checked["status"] == "violated" and checked["verify_detail"]["duplicate_example"][0] == "9002"
    assert '"storedb"."store"."returns"' in checked["verify_detail"]["sql"]


def test_04_identity_rename_and_recreate(stack, admin: Api):
    returns_id = ctx["ids"]["returns"]
    pg(stack, "ALTER TABLE store.returns RENAME TO refunds")
    collect(admin)
    found = assets(admin)
    assert found["storedb.store.refunds"]["id"] == returns_id  # oid 不变：同一资产
    assert ("renamed", returns_id, None) in latest_events(admin)
    pg(stack, "DROP TABLE store.refunds",
       "CREATE TABLE store.refunds (return_id bigint PRIMARY KEY, sale_id bigint NOT NULL, "
       "return_date date NOT NULL, amount numeric(18, 2) NOT NULL)")
    collect(admin)
    recreated = assets(admin)["storedb.store.refunds"]["id"]
    assert recreated != returns_id and admin.get(f"/api/assets/{returns_id}")["status"] == "deleted"


def test_05_definition_change_live_check_then_affected(stack, admin: Api):
    wang = ctx["apis"]["wang"]
    pg(stack, "CREATE OR REPLACE VIEW store.city_daily_sales AS "
              "SELECT s.sale_date, h.city, SUM(s.amount) AS sales_amount, COUNT(DISTINCT s.sale_id) AS sales_count "
              "FROM store.sales s JOIN store.shops h ON h.shop_id = s.shop_id "
              "GROUP BY s.sale_date, h.city")  # 去掉作废单过滤：上海变为 310
    stale = wang.post("/api/ask", {"question": f"{DAY} 上海门店销售额"})
    assert stale["status"] == "blocked" and stale["error_code"] == "metadata_stale", stale
    assert "视图定义已变化" in stale["message"] and stale["result"] == {} and stale["executed_at"] is None
    collect(admin)
    view_id = ctx["ids"]["city_daily_sales"]
    events = latest_events(admin)
    assert ("definition_changed", view_id, None) in events
    assert ("derivation_changed", view_id, "sales_amount") in events
    assert admin.get(f"/api/metric-versions/{ctx['store_v1']}")["status"] == "affected"
    blocked = wang.post("/api/ask", {"question": f"{DAY} 上海门店销售额"})
    assert blocked["status"] == "blocked" and blocked["error_code"] == "metric_affected" and blocked["sql"] is None


def test_06_timeout_and_errors_are_reported_honestly(stack):
    import demo

    from avelis.connectors.base import QueryError
    from avelis.connectors.postgres import PostgresConnector

    _, env, _ = stack
    reader = PostgresConnector("127.0.0.1", int(env["SHOP_PG_PORT"]), "avelis_reader",
                               env["SHOP_PG_READER_PASSWORD"], {"database": "storedb"})
    started = time.monotonic()
    with pytest.raises(QueryError) as timeout:
        reader.execute_select("SELECT pg_sleep(5)", timeout_seconds=1, mem_limit_bytes=0, max_rows=1)
    assert timeout.value.code == "timeout" and timeout.value.db_errno == "57014"
    assert time.monotonic() - started < 4
    with pytest.raises(QueryError) as missing:
        reader.execute_select("SELECT nope FROM store.sales", timeout_seconds=5, mem_limit_bytes=0, max_rows=1)
    assert missing.value.code == "invalid_object"
    with pytest.raises(QueryError) as write:
        reader.execute_select("DELETE FROM store.sales", timeout_seconds=5, mem_limit_bytes=0, max_rows=1)
    assert write.value.code == "permission"
    with demo.pg_connect(env, "avelis_reader") as conn:
        assert conn.execute("SELECT COUNT(*) FROM store.sales").fetchone()[0] == 5
    # 采集账号只读系统目录，读不到业务数据
    with demo.pg_connect(env, "avelis_collector") as conn:
        with pytest.raises(Exception, match="permission denied"):
            conn.execute("SELECT COUNT(*) FROM store.sales")
