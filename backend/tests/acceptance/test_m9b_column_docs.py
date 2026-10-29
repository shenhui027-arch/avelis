"""字段含义以数据库列注释为准（M6）：Avelis 不另设显示名，原样返回列名并标出注释状态，缺口如实暴露，推动数据库侧补齐。

演示数据：
- shop.orders.region 有注释“销售区域名称”；视图 shop.daily_sales 的 region 直接取自它，本身没有注释（inherited）；
- shop.daily_sales.sales_amount 是聚合列，本身与上游都没有可沿用的注释（missing）；
- dim.ext_region 是 MySQL 外部表，经身份映射取源表 shop_dim.dim_region 的注释（linked）；
- PostgreSQL store.shops.city 没有注释，视图 store.city_daily_sales.city 直接取自它。
全部在真实 StarRocks / PostgreSQL 上执行，补注释走真实 DDL 与采集。
"""

from __future__ import annotations

import dataclasses

import pytest

from tests.conftest import Api

pytestmark = pytest.mark.acceptance
DAY = "2026-09-15"
ctx: dict = {}


def pg(stack, *statements: str) -> None:
    import demo

    _, env, _ = stack
    with demo.pg_connect(env) as conn:
        for statement in statements:
            conn.execute(statement)


def collect(api: Api, ds: str) -> None:
    assert api.post(f"/api/datasources/{ds}/collect?wait=true")["status"] == "succeeded"


def docs_of(api: Api, qualified: str) -> dict[str, dict]:
    asset = next(a for a in api.get("/api/assets") if a["qualified"] == qualified)
    return {c["name"]: c["doc"] for c in api.get(f"/api/assets/{asset['id']}")["columns"]}


def gaps(api: Api, ds: str) -> tuple[dict, dict[tuple[str, str], dict]]:
    data = api.get("/api/metadata/comment-gaps", params={"datasource_id": ds})
    summary = next(x for x in data["datasources"] if x["datasource_id"] == ds)
    return summary, {(x["asset"], x["column"]): x for x in data["items"]}


def test_00_setup(stack, admin: Api):
    from avelis import demo_setup

    client, env, _ = stack
    demo_setup.setup(reset=False, metrics=True)
    pw = env["AVELIS_DEMO_USER_PASSWORD"]
    sources = {x["name"]: x["id"] for x in admin.get("/api/datasources")}
    ctx.update(apis={u: Api(client, u, pw) for u in ("wang", "sun")}, sr=sources["演示 StarRocks"],
               pg=sources["门店库（PostgreSQL）"])
    for ds in (ctx["sr"], ctx["pg"]):
        collect(admin, ds)


def test_01_ask_returns_raw_column_with_comment_status(admin: Api):
    a = ctx["apis"]["wang"].post("/api/ask", {"question": f"{DAY} 各区域实付销售额"})
    assert a["status"] == "answered", a
    assert a["result"]["columns"][0] == "region"  # 原样列名，不用后端起的显示名
    assert "labels" not in a["result"]
    region = a["result"]["column_docs"]["region"]
    assert region["status"] == "inherited" and region["comment"] == "销售区域名称"
    assert region["asset"] == "default_catalog.shop.daily_sales"
    assert region["source"] == "default_catalog.shop.orders.region"


def test_02_asset_columns_carry_doc_status(admin: Api):
    orders = docs_of(admin, "default_catalog.shop.orders")
    assert orders["region"] == {**orders["region"], "status": "ok", "comment": "销售区域名称"}
    daily = docs_of(admin, "default_catalog.shop.daily_sales")
    assert daily["region"]["status"] == "inherited" and daily["sales_amount"]["status"] == "missing"
    assert daily["sales_amount"]["comment"] is None
    # 外部表：含义在源库维护，取源表同名列注释，不计为缺口
    ext = docs_of(admin, "default_catalog.dim.ext_region")
    assert ext["region_name"]["status"] == "linked" and ext["region_name"]["comment"] == "区域名称"
    assert ext["region_name"]["source"].endswith("shop_dim.dim_region.region_name")


def test_03_gap_list_drives_db_side_fix(stack, admin: Api):
    before, items = gaps(admin, ctx["pg"])
    city = items[("storedb.store.shops", "city")]
    assert city["status"] == "missing" and city["suggestion"] is None
    assert items[("storedb.store.city_daily_sales", "city")]["status"] == "missing"
    assert ("storedb.store.sales", "amount") not in items  # 已有注释
    assert before["gaps"] == before["inherited"] + before["missing"] and before["completeness"] < 1
    # 数据库侧补注释 → 采集 → 缺口减少；直接取值的视图列提示可沿用上游注释
    pg(stack, "COMMENT ON COLUMN store.shops.city IS '门店所在城市'")
    try:
        collect(admin, ctx["pg"])
        after, items = gaps(admin, ctx["pg"])
        assert ("storedb.store.shops", "city") not in items
        view_city = items[("storedb.store.city_daily_sales", "city")]
        assert view_city["status"] == "inherited" and view_city["suggestion"] == "门店所在城市"
        assert view_city["source"] == "storedb.store.shops.city"
        assert after["ok"] == before["ok"] + 1 and after["completeness"] > before["completeness"]
    finally:
        pg(stack, "COMMENT ON COLUMN store.shops.city IS NULL")
        collect(admin, ctx["pg"])
    assert ("storedb.store.shops", "city") in gaps(admin, ctx["pg"])[1]


def test_04_gap_list_only_shows_visible_assets(admin: Api):
    sun = ctx["apis"]["sun"]
    visible = {a["id"] for a in sun.get("/api/assets")}
    data = sun.get("/api/metadata/comment-gaps")
    assert data["items"] and {x["asset_id"] for x in data["items"]} <= visible
    everything = admin.get("/api/metadata/comment-gaps")
    assert len(everything["items"]) > len(data["items"])


def test_05_publish_check_warns_or_blocks_on_gaps(admin: Api, monkeypatch):
    from avelis.config import get_settings
    from avelis.governance import metrics

    paid = next(m for m in admin.get("/api/metrics") if m["code"] == "paid_sales")["versions"][-1]["id"]
    check = admin.post(f"/api/metric-versions/{paid}/check")
    gap = "字段 shop.daily_sales.sales_amount 缺少数据库注释"
    assert any(gap in w for w in check["warnings"]) and not any(gap in b for b in check["blockers"])
    assert any("shop.daily_sales.region" in w and "可沿用" in w for w in check["warnings"])
    # AVELIS_REQUIRE_COLUMN_COMMENTS=true：缺注释阻断发布
    strict = dataclasses.replace(get_settings(), require_column_comments=True)
    monkeypatch.setattr(metrics, "get_settings", lambda: strict)
    check = admin.post(f"/api/metric-versions/{paid}/check")
    assert not check["ok"] and any(gap in b for b in check["blockers"])
