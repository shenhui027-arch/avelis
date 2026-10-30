"""M1 验收：在真实 StarRocks 3.5.14 + 真实 MySQL 维表库上跑通故事 1–6 与故事 9。

全部操作经由 HTTP API，以不同角色的真实账号执行；断言检查实际执行行为（是否执行了 SQL、返回值、
数据库中的状态与审计），不只看状态码。用例按顺序共享状态（同一模块内）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from avelis.demo_setup import PRODUCT_ASSETS, TRADE_ASSETS
from tests.conftest import Api

pytestmark = pytest.mark.acceptance

V1_SQL = ("SELECT order_date, region, SUM(paid_amount) AS sales_amount "
          "FROM shop.orders GROUP BY order_date, region")
V2_SQL = ("SELECT order_date, region, SUM(paid_amount - refund_amount) AS sales_amount "
          "FROM shop.orders GROUP BY order_date, region")
DAY = "2026-09-15"

ctx: dict = {}


def alter_daily_sales(stack, sql: str) -> None:
    _, _, sr_root = stack
    conn = sr_root()
    with conn.cursor() as cur:
        cur.execute(f"ALTER VIEW shop.daily_sales AS {sql}")
    conn.close()


def collect(api: Api, ds_id: str) -> dict:
    run = api.post(f"/api/datasources/{ds_id}/collect?wait=true")
    assert run["status"] == "succeeded", run
    return run


def asset_id(api: Api, qualified: str) -> str:
    hits = [a for a in api.get("/api/assets") if a["qualified"] == qualified]
    assert len(hits) == 1, f"{qualified}: {hits}"
    return hits[0]["id"]


def value_of(answer: dict) -> Decimal:
    assert answer["status"] == "answered", answer
    assert answer["result"]["row_count"] == 1, answer["result"]
    return Decimal(str(answer["result"]["rows"][0][-1]))


def latest_change_set(api: Api) -> dict:
    sets = api.get("/api/change-sets")
    return api.get(f"/api/change-sets/{sets[0]['id']}")


# --- 准备：用户、业务域、数据源、端点别名（全部经 API，由管理员完成） ----------------------

def test_00_setup(stack, admin: Api):
    client, env, _ = stack
    pw = env["AVELIS_DEMO_USER_PASSWORD"]
    users = {
        "zhang": ("张敏（交易域负责人）", ["steward"]),
        "zhao": ("赵磊（商品域负责人）", ["steward"]),
        "li": ("李娜（复核者）", ["reviewer"]),
        "wang": ("王强（分析师）", ["analyst"]),
        "sun": ("孙悦（仅交易域）", ["analyst"]),
        "qian": ("钱程（无业务域）", ["analyst"]),
    }
    ids = {}
    for username, (name, roles) in users.items():
        ids[username] = admin.post("/api/users", {"username": username, "display_name": name, "password": pw,
                                                  "roles": roles}, expect=201)["id"]
    trade = admin.post("/api/domains", {"code": "trade", "name": "交易域", "owner_id": ids["zhang"]}, expect=201)
    product = admin.post("/api/domains", {"code": "product", "name": "商品域", "owner_id": ids["zhao"]}, expect=201)
    apis = {u: Api(client, u, pw) for u in users}
    for member, role in (("wang", "viewer"), ("li", "viewer"), ("sun", "viewer")):
        apis["zhang"].put(f"/api/domains/{trade['id']}/members", {"user_id": ids[member], "role": role})
    for member, role in (("wang", "viewer"), ("li", "viewer"), ("zhang", "viewer")):
        apis["zhao"].put(f"/api/domains/{product['id']}/members", {"user_id": ids[member], "role": role})

    mysql = admin.post("/api/datasources", {
        "name": "电商维表库（MySQL）", "kind": "mysql", "host": "127.0.0.1", "port": int(env["SHOP_MYSQL_PORT"]),
        "options": {"databases": ["shop_dim"]},
        "collect": {"username": "avelis_collector", "password": env["SHOP_MYSQL_COLLECTOR_PASSWORD"]},
    }, expect=201)
    sr = admin.post("/api/datasources", {
        "name": "演示 StarRocks", "kind": "starrocks", "host": "127.0.0.1", "port": int(env["SR_QUERY_PORT"]),
        "options": {"databases": ["shop", "ads", "dim"], "catalogs": ["shop_mysql"],
                    "catalog_databases": {"shop_mysql": ["shop_dim"]}},
        "collect": {"username": "avelis_collector", "password": env["SR_COLLECTOR_PASSWORD"]},
        "query": {"username": "avelis_reader", "password": env["SR_READER_PASSWORD"]},
    }, expect=201)
    # 口令只写不读
    assert all(c["password"] == "已设置（不回传）" for c in sr["credentials"])
    assert env["SR_READER_PASSWORD"] not in str(admin.get("/api/datasources"))
    admin.post("/api/endpoint-aliases", {"datasource_id": mysql["id"], "host": "shop-mysql", "port": 3306,
                                         "note": "StarRocks 容器网络内的地址"}, expect=201)
    ctx.update(users=ids, apis=apis, trade=trade["id"], product=product["id"], mysql=mysql["id"], sr=sr["id"])


# --- 故事 1：采集真实表结构与视图 SQL，展示可追溯的列级依赖 --------------------------------

def test_01_collect_and_lineage(admin: Api):
    collect(admin, ctx["mysql"])
    run = collect(admin, ctx["sr"])
    assert run["stats"]["assets_observed"] == 12
    ids = {q: asset_id(admin, q) for q in (
        "default_catalog.shop.orders", "default_catalog.shop.daily_sales", "default_catalog.shop.order_items",
        "default_catalog.shop.refund_raw", "default_catalog.ads.region_board", "default_catalog.dim.ext_region",
        "shop_mysql.shop_dim.dim_product", "shop_mysql.shop_dim.dim_category", "def.shop_dim.dim_product",
        "def.shop_dim.dim_region")}
    ctx["assets"] = ids

    daily = admin.get(f"/api/assets/{ids['default_catalog.shop.daily_sales']}")
    assert daily["kind"] == "view" and daily["parse"]["status"] == "ok"
    assert "sum(`shop`.`orders`.`paid_amount`)" in daily["definition"]["raw_sql"].lower()
    sales = next(c for c in daily["columns"] if c["name"] == "sales_amount")
    assert sales["fingerprint_status"] == "ok"
    ctx["fp_v1"] = sales["fingerprint"]

    graph = admin.get("/api/lineage/graph", params={"asset_id": ids["default_catalog.shop.daily_sales"]})
    edge = next(e for e in graph["edges"] if e["source"] == ids["default_catalog.shop.orders"]
                and e["target"] == ids["default_catalog.shop.daily_sales"])
    by_target = {(c["target_column"], c["source_column"]): c["relation"] for c in edge["columns"]}
    # 取值与影响分开：sales_amount 的值来自 paid_amount（聚合）；order_date、region 既是取值也是分组键
    assert by_target[("sales_amount", "paid_amount")] == "value_aggregated"
    assert by_target[("order_date", "order_date")] == "value_direct"
    assert by_target[(None, "region")] == "group_by"
    assert ("sales_amount", "refund_amount") not in by_target
    assert all(c["origin"] == "parsed" for c in edge["columns"])

    # 粒度：主键表直接观察；视图由 GROUP BY 推出
    orders = admin.get(f"/api/assets/{ids['default_catalog.shop.orders']}")
    assert {"columns": ["order_id"], "origin": "observed"}.items() <= orders["grains"][0].items()
    assert any(g["columns"] == ["order_date", "region"] and g["origin"] == "parsed" for g in daily["grains"])

    # 外部表自动接到 MySQL 真实表（Catalog 表与旧式外部表两种写法）
    product = admin.get(f"/api/assets/{ids['shop_mysql.shop_dim.dim_product']}")
    assert product["identity_link"]["status"] == "confirmed"
    assert product["identity_link"]["target"]["id"] == ids["def.shop_dim.dim_product"]
    assert any(g["origin"] == "linked" and g["columns"] == ["product_id"] for g in product["grains"])
    region = admin.get(f"/api/assets/{ids['default_catalog.dim.ext_region']}")
    assert region["identity_link"]["target"]["id"] == ids["def.shop_dim.dim_region"]
    assert region["identity_link"]["status"] in ("confirmed", "mismatch")

    # 重复采集幂等：不产生新版本、不产生变更集
    sets_before = len(admin.get("/api/change-sets"))
    again = collect(admin, ctx["sr"])
    assert again["stats"].get("new_versions", 0) == 0
    assert len(admin.get("/api/change-sets")) == sets_before

    # 资产归属业务域（未归属的资产默认只有管理员、维护者可见，查询者查不到，见 M4 用例）
    zhang, zhao = ctx["apis"]["zhang"], ctx["apis"]["zhao"]
    by_name = {a["qualified"]: a["id"] for a in admin.get("/api/assets")}
    for q in TRADE_ASSETS:
        zhang.patch(f"/api/assets/{by_name[q]}", {"domain_id": ctx["trade"]})
    for q in PRODUCT_ASSETS:
        zhao.patch(f"/api/assets/{by_name[q]}", {"domain_id": ctx["product"]})
    assert not [a["qualified"] for a in admin.get("/api/assets") if a["domain_id"] is None]


# --- 故事 2：创建并发布“实付销售额”V1 -------------------------------------------------------

PAID_SPEC = {
    "description": "订单实付金额按下单日期、区域汇总（不扣退款）",
    "expression": "SUM(sales_amount)", "time_column": "order_date", "timezone": "Asia/Shanghai",
    "dimensions": [{"name": "region", "column": "region", "label": "区域"}],
    "grain": ["order_date", "region"],
}


def test_02_publish_paid_sales(admin: Api):
    zhang, li = ctx["apis"]["zhang"], ctx["apis"]["li"]
    admin.patch(f"/api/users/{ctx['users']['zhang']}", {"roles": ["steward", "reviewer"]})
    metric = zhang.post("/api/metrics", {"code": "paid_sales", "name": "实付销售额", "domain_id": ctx["trade"],
                                         "owner_id": ctx["users"]["zhang"]}, expect=201)
    version = zhang.post(f"/api/metrics/{metric['id']}/versions",
                         {**PAID_SPEC, "base_asset_id": ctx["assets"]["default_catalog.shop.daily_sales"]},
                         expect=201)
    assert version["status"] == "draft"
    version = zhang.post(f"/api/metric-versions/{version['id']}/submit")
    assert version["status"] == "pending_review"
    # 创建人即使同时是维护者、复核者，也不能自审
    zhang.post(f"/api/metric-versions/{version['id']}/approve", {"note": "自己批"}, expect=403)
    admin.patch(f"/api/users/{ctx['users']['zhang']}", {"roles": ["steward"]})
    version = li.post(f"/api/metric-versions/{version['id']}/approve", {"note": "口径与负责人确认"})
    assert version["status"] == "published"
    assert version["reviewed_by"] == ctx["users"]["li"] and version["submitted_by"] == ctx["users"]["zhang"]
    assert version["timezone"] == "Asia/Shanghai" and version["grain"] == ["order_date", "region"]
    assert version["dependency"][0]["fingerprint"]
    ctx.update(paid_metric=metric["id"], paid_v1=version["id"])


# --- 故事 3：真实执行，得到 300，并展示 SQL、口径版本与来源证据 ---------------------------------

def test_03_ask_paid_sales(admin: Api):
    wang = ctx["apis"]["wang"]
    answer = wang.post("/api/ask", {"question": f"{DAY} 华东区实付销售额是多少？"})
    assert value_of(answer) == Decimal("300.00")
    assert "daily_sales" in answer["sql"] and "LIMIT" in answer["sql"]
    assert answer["metric_version_ids"] == [ctx["paid_v1"]]
    assert answer["evidence"]["metric_version"]["version_no"] == 1
    assert answer["evidence"]["model"]["is_demo"] is True
    lineage = next(x for x in answer["evidence"]["lineage"] if x["column"].endswith("daily_sales.sales_amount"))
    assert lineage["role"] == "measure"
    assert any(s["source"].endswith("orders.paid_amount") and s["relation"] == "value_aggregated"
               for s in lineage["sources"])
    assert answer["executed_at"] and not answer["cache_hit"]
    assert answer["freshness"]["tables"]["shop.orders"]
    ctx["answer_300"] = answer["id"]

    cached = wang.post("/api/ask", {"question": f"{DAY} 华东区实付销售额是多少？"})
    assert value_of(cached) == Decimal("300.00") and cached["cache_hit"] is True

    # 没有业务域权限的用户检索不到该口径，也就不会执行
    denied = ctx["apis"]["qian"].post("/api/ask", {"question": f"{DAY} 华东区实付销售额"})
    assert denied["status"] == "clarify" and denied["sql"] is None


# --- 故事 4–5：视图改为净额后，发现变化、标记受影响、拦截旧口径 ---------------------------------

def _change_to_v2_and_collect(stack, admin: Api) -> dict:
    alter_daily_sales(stack, V2_SQL)
    collect(admin, ctx["sr"])
    return latest_change_set(admin)


def test_04_05_definition_change_blocks_old_metric(stack, admin: Api):
    cs = _change_to_v2_and_collect(stack, admin)
    daily_id = ctx["assets"]["default_catalog.shop.daily_sales"]
    kinds = {(e["kind"], e["asset_id"], e["column"]) for e in cs["events"]}
    assert ("definition_changed", daily_id, None) in kinds
    assert ("derivation_changed", daily_id, "sales_amount") in kinds
    # 输出列名与类型都没变：不是 schema 变化
    assert not any(e["kind"].startswith("column_") and e["asset_id"] == daily_id for e in cs["events"])
    diff = next(e for e in cs["events"] if e["kind"] == "definition_changed")
    assert "refund_amount" in diff["after"]["canonical_sql"] and "refund_amount" not in diff["before"]["canonical_sql"]
    assert cs["effective_window"]["after"] and cs["effective_window"]["before"]

    impacted = {(i["object_type"], i["object_id"]): i for i in cs["impacts"]}
    assert ("metric_version", ctx["paid_v1"]) in impacted
    assert ("asset", ctx["assets"]["default_catalog.ads.region_board"]) in impacted
    task = next(t for t in cs["tasks"] if t["target"]["id"] == ctx["paid_v1"])
    assert task["assignee_id"] == ctx["users"]["zhang"] and task["status"] == "open"
    ctx["task_1"] = task["id"]

    version = admin.get(f"/api/metric-versions/{ctx['paid_v1']}")
    assert version["status"] == "affected"
    # 指纹纳入规范化定义（评审 #1）：定义一变，视图的各列都漂移；被改写的 sales_amount 必在其中
    assert "sales_amount" in {d["column"] for d in version["drift"]}

    # 再问原“实付销售额”：后端拦截，不执行 SQL，不把 270 当作已确认的实付销售额返回，也不切换口径
    wang = ctx["apis"]["wang"]
    blocked = wang.post("/api/ask", {"question": f"{DAY} 华东区实付销售额是多少？"})
    assert blocked["status"] == "blocked" and blocked["error_code"] == "metric_affected"
    assert blocked["sql"] is None and blocked["executed_at"] is None and blocked["result"] == {}
    assert blocked["metric_version_ids"] == [ctx["paid_v1"]]
    assert "受影响" in blocked["message"]
    # 历史答案保留原定义，并标记需要重新确认
    old = wang.get(f"/api/answers/{ctx['answer_300']}")
    assert old["recheck_status"] == "needs_recheck" and value_of(old) == Decimal("300.00")
    audit = admin.get("/api/audit", params={"object_type": "metric_version", "object_id": ctx["paid_v1"]})
    assert any(e["action"] == "metric_version.affected" and e["actor"] == "system" for e in audit)


# --- 故事 6（甲）：负责人选择恢复原定义 ---------------------------------------------------------

def test_06a_restore_original_definition(stack, admin: Api):
    zhang, wang = ctx["apis"]["zhang"], ctx["apis"]["wang"]
    # 定义仍是净额时不能按“已恢复”结案
    zhang.post(f"/api/review-tasks/{ctx['task_1']}/resolve",
               {"resolution": "restored", "note": "准备恢复"}, expect=409)
    alter_daily_sales(stack, V1_SQL)
    collect(admin, ctx["sr"])
    daily = admin.get(f"/api/assets/{ctx['assets']['default_catalog.shop.daily_sales']}")
    assert next(c for c in daily["columns"] if c["name"] == "sales_amount")["fingerprint"] == ctx["fp_v1"]
    # 观察到的定义已恢复，但业务批准尚未恢复：仍然拦截
    still = wang.post("/api/ask", {"question": f"{DAY} 华东区实付销售额是多少？"})
    assert still["status"] == "blocked" and still["sql"] is None
    # 只有任务负责人能处理
    ctx["apis"]["li"].post(f"/api/review-tasks/{ctx['task_1']}/resolve",
                           {"resolution": "restored", "note": "代处理"}, expect=403)
    task = zhang.post(f"/api/review-tasks/{ctx['task_1']}/resolve",
                      {"resolution": "restored", "note": "已在数据库恢复 v1 定义并重新采集核对"})
    assert task["status"] == "resolved" and task["resolved_by"] == ctx["users"]["zhang"]
    assert admin.get(f"/api/metric-versions/{ctx['paid_v1']}")["status"] == "published"
    answer = wang.post("/api/ask", {"question": f"{DAY} 华东区实付销售额是多少？"})
    assert value_of(answer) == Decimal("300.00")


# --- 故事 6（乙）：再次改为净额，负责人按流程发布“退款后净销售额” ---------------------------------

def test_06b_publish_net_sales(stack, admin: Api):
    zhang, li, wang = ctx["apis"]["zhang"], ctx["apis"]["li"], ctx["apis"]["wang"]
    cs = _change_to_v2_and_collect(stack, admin)
    task = next(t for t in cs["tasks"] if t["target"]["id"] == ctx["paid_v1"])
    assert task["assignee_id"] == ctx["users"]["zhang"]
    assert zhang.get("/api/review-tasks")[0]["id"] == task["id"]

    net = zhang.post("/api/metrics", {"code": "net_sales", "name": "退款后净销售额", "domain_id": ctx["trade"],
                                      "aliases": ["净销售额"]}, expect=201)
    before = wang.post("/api/ask", {"question": f"{DAY} 华东区退款后净销售额"})
    assert before["status"] == "clarify"  # 未发布前不可用
    v = zhang.post(f"/api/metrics/{net['id']}/versions", {
        **PAID_SPEC, "description": "实付金额扣除退款后按日期、区域汇总",
        "base_asset_id": ctx["assets"]["default_catalog.shop.daily_sales"]}, expect=201)
    zhang.post(f"/api/metric-versions/{v['id']}/submit")
    v = li.post(f"/api/metric-versions/{v['id']}/approve", {"note": "确认为退款后净额"})
    assert v["status"] == "published"

    # 用另一个指标的版本承接时，必须是已发布版本
    zhang.post(f"/api/review-tasks/{task['id']}/resolve",
               {"resolution": "new_version", "note": "缺少新口径"}, expect=422)
    done = zhang.post(f"/api/review-tasks/{task['id']}/resolve", {
        "resolution": "new_version", "note": "视图已改为净额，原实付口径停用，改由退款后净销售额承接",
        "linked_metric_version_id": v["id"]})
    assert done["status"] == "resolved"

    answer = wang.post("/api/ask", {"question": f"{DAY} 华东区退款后净销售额是多少"})
    assert value_of(answer) == Decimal("270.00")
    assert answer["metric_version_ids"] == [v["id"]]
    assert answer["evidence"]["metric_version"]["metric_name"] == "退款后净销售额"

    paid = admin.get(f"/api/metric-versions/{ctx['paid_v1']}")
    assert paid["status"] == "retired"
    assert [t["to"] for t in paid["transitions"]] == ["draft", "pending_review", "published", "affected",
                                                      "published", "affected", "retired"]
    old = wang.post("/api/ask", {"question": f"{DAY} 华东区实付销售额是多少？"})
    assert old["status"] == "blocked" and old["error_code"] == "metric_retired" and "退款后净销售额" in old["message"]
    history = wang.get(f"/api/answers/{ctx['answer_300']}")
    assert history["metric_version_ids"] == [ctx["paid_v1"]] and value_of(history) == Decimal("300.00")
    assert admin.get(f"/api/change-sets/{cs['id']}")["status"] == "resolved"


# --- 故事 9：粒度与放大 -------------------------------------------------------------------

def test_09_grain_fan_out(admin: Api):
    zhang, zhao, li, wang = (ctx["apis"][u] for u in ("zhang", "zhao", "li", "wang"))
    a = ctx["assets"]

    def path(code, name, left, lcols, right, rcols):
        return zhang.post("/api/join-paths", {"code": code, "name": name, "left_asset_id": a[left],
                                              "left_columns": lcols, "right_asset_id": a[right],
                                              "right_columns": rcols}, expect=201)

    o_i = path("orders_items", "订单 → 订单明细", "default_catalog.shop.orders", ["order_id"],
               "default_catalog.shop.order_items", ["order_id"])
    o_r = path("orders_refunds", "订单 → 退款明细", "default_catalog.shop.orders", ["order_id"],
               "default_catalog.shop.refund_raw", ["order_id"])
    i_o = path("items_orders", "订单明细 → 订单", "default_catalog.shop.order_items", ["order_id"],
               "default_catalog.shop.orders", ["order_id"])
    i_p = path("items_product", "订单明细 → 商品（MySQL）", "default_catalog.shop.order_items", ["product_id"],
               "shop_mysql.shop_dim.dim_product", ["product_id"])
    p_c = zhao.post("/api/join-paths", {"code": "product_category", "name": "商品 → 类目",
                                        "left_asset_id": a["shop_mysql.shop_dim.dim_product"],
                                        "left_columns": ["category_id"],
                                        "right_asset_id": a["shop_mysql.shop_dim.dim_category"],
                                        "right_columns": ["category_id"]}, expect=201)
    assert (o_i["cardinality"], o_r["cardinality"], i_o["cardinality"]) == ("1n", "1n", "n1")
    assert i_p["cardinality"] == "n1" and p_c["cardinality"] == "n1"
    assert i_p["cardinality_basis"]["right_grain"]["origin"] == "linked"  # 来自 MySQL 主键
    assert set(i_p["required_domains"]) == {ctx["trade"], ctx["product"]}

    for p in (o_i, o_r, i_o):
        zhang.post(f"/api/join-paths/{p['id']}/decisions", {"domain_id": ctx["trade"], "decision": "approved"})
    zhao.post(f"/api/join-paths/{p_c['id']}/decisions", {"domain_id": ctx["product"], "decision": "approved"})
    # 跨域：只有一方批准时仍是 pending；非负责人不能代表该域
    zhang.post(f"/api/join-paths/{i_p['id']}/decisions", {"domain_id": ctx["trade"], "decision": "approved"})
    zhang.post(f"/api/join-paths/{i_p['id']}/decisions", {"domain_id": ctx["product"], "decision": "approved"},
               expect=403)
    assert next(p for p in admin.get("/api/join-paths") if p["id"] == i_p["id"])["status"] == "pending"

    # 明细模型表的粒度：人工声明并用只读查询核验
    grain = zhang.post(f"/api/assets/{a['default_catalog.shop.refund_raw']}/grains",
                       {"columns": ["refund_id"], "reason": "退款单号唯一"}, expect=201)
    assert zhang.post(f"/api/grains/{grain['id']}/verify")["status"] == "verified"
    bad = zhang.post(f"/api/assets/{a['default_catalog.shop.refund_raw']}/grains",
                     {"columns": ["order_id"], "reason": "错误声明"}, expect=201)
    checked = zhang.post(f"/api/grains/{bad['id']}/verify")
    assert checked["status"] == "violated" and checked["verify_detail"]["duplicate_example"][0] == "1001"

    # 订单金额经 1:N 关联后求和：StarRocks 实际返回 400，系统阻止发布并给出原因
    m = zhang.post("/api/metrics", {"code": "paid_with_items", "name": "有明细订单实付额",
                                    "domain_id": ctx["trade"]}, expect=201)
    spec = {"description": "有订单明细的订单实付金额", "base_asset_id": a["default_catalog.shop.orders"],
            "expression": "SUM(paid_amount)", "time_column": "order_date",
            "dimensions": [{"name": "region", "column": "region", "label": "区域"}],
            "joins": [{"alias": "i", "join_path_id": o_i["id"]}], "grain": ["order_date", "region"],
            "dedupe_basis": "订单粒度为 order_id"}
    draft = zhang.post(f"/api/metrics/{m['id']}/versions", spec, expect=201)
    refused = zhang.post(f"/api/metric-versions/{draft['id']}/submit", expect=409)
    assert any("1:N" in b and "重复计数" in b for b in refused["blockers"])
    assert admin.get(f"/api/metric-versions/{draft['id']}")["status"] == "draft"

    # 改为先聚合再关联：通过，结果 300
    spec["joins"] = [{"alias": "i", "join_path_id": o_i["id"], "pre_aggregate": True}]
    spec["dedupe_basis"] = "订单明细先按 order_id 聚合到订单粒度再关联（N:1）"
    zhang.put(f"/api/metric-versions/{draft['id']}", spec)
    zhang.post(f"/api/metric-versions/{draft['id']}/submit")
    li.post(f"/api/metric-versions/{draft['id']}/approve", {"note": "先聚合再关联，不放大"})
    answer = wang.post("/api/ask", {"question": f"{DAY} 华东区有明细订单实付额"})
    assert value_of(answer) == Decimal("300.00")
    assert "GROUP BY" in answer["sql"] and "row_count" in answer["sql"]

    # 跨域：明细 → 商品（MySQL，经 Catalog）N:1，可通过；类目销售按明细金额求和
    cat = zhang.post("/api/metrics", {"code": "category_sales", "name": "类目销售额", "domain_id": ctx["trade"]},
                     expect=201)
    cspec = {"description": "订单明细实付金额按类目汇总", "base_asset_id": a["default_catalog.shop.order_items"],
             "expression": "SUM(item_amount)", "time_column": "o.order_date",
             "dimensions": [{"name": "category", "column": "c.category_name", "label": "类目"},
                            {"name": "region", "column": "o.region", "label": "区域"}],
             "joins": [{"alias": "o", "join_path_id": i_o["id"]},
                       {"alias": "p", "join_path_id": i_p["id"]},
                       {"alias": "c", "from": "p", "join_path_id": p_c["id"]}],
             "grain": ["order_date", "category"], "dedupe_basis": "明细 → 订单、商品、类目均为 N:1"}
    cv = zhang.post(f"/api/metrics/{cat['id']}/versions", cspec, expect=201)
    pending = zhang.post(f"/api/metric-versions/{cv['id']}/submit", expect=409)
    assert any("items_product" in b and "尚未批准" in b for b in pending["blockers"])
    zhao.post(f"/api/join-paths/{i_p['id']}/decisions", {"domain_id": ctx["product"], "decision": "approved"})
    zhang.post(f"/api/metric-versions/{cv['id']}/submit")
    checked = admin.get(f"/api/metric-versions/{cv['id']}")["publish_check"]
    assert [j["cardinality"] for j in checked["joins"]] == ["n1", "n1", "n1"]
    li.post(f"/api/metric-versions/{cv['id']}/approve", {"note": "跨域路径两域已批准"})
    answer = wang.post("/api/ask", {"question": f"{DAY} 厨具类目销售额"})
    assert value_of(answer) == Decimal("270.00")
    assert "shop_mysql.shop_dim.dim_product" in answer["sql"]
    sources = {s["qualified"] for s in answer["evidence"]["sources"]}
    assert "def.shop_dim.dim_product" in sources  # 依据追到 MySQL 真实表

    # 只属于交易域的用户：口径可见，但来源含商品域资产，后端拒绝执行
    denied = ctx["apis"]["sun"].post("/api/ask", {"question": f"{DAY} 厨具类目销售额"})
    assert denied["status"] in ("denied", "clarify") and denied["sql"] is None


# --- 后台任务：经 Redis 唤醒、以 job 表为准的采集 ---------------------------------------------

def test_10_collect_via_job_queue(admin: Api):
    from avelis import cache, jobs
    from avelis.db import advisory_lock

    queued = admin.post(f"/api/datasources/{ctx['sr']}/collect")
    assert queued["status"] == "queued"
    assert admin.get(f"/api/jobs/{queued['job_id']}")["status"] == "queued"
    _, job_id = cache.client().brpop([cache.JOB_QUEUE], timeout=5)
    assert job_id == queued["job_id"]
    jobs.run_job(job_id)
    job = admin.get(f"/api/jobs/{job_id}")
    assert job["status"] == "succeeded", job
    runs = admin.get(f"/api/datasources/{ctx['sr']}/runs")
    assert runs[0]["id"] == job["result"]["run_id"] and runs[0]["stats"].get("new_versions", 0) == 0
    # 同一数据源的采集互斥
    with advisory_lock(f"collect:{ctx['sr']}"):
        admin.post(f"/api/datasources/{ctx['sr']}/collect?wait=true", expect=409)


# --- M2：问数智能体（LangGraph）-----------------------------------------------------------

def test_11_agent_multi_turn_clarification_survives_restart(stack, admin: Api):
    from sqlalchemy import text

    from avelis.ask import agent
    from avelis.db import get_engine

    wang = ctx["apis"]["wang"]
    first = wang.post("/api/ask", {"question": "华东区退款后净销售额是多少？"})
    assert first["status"] == "clarify" and "日期" in first["message"] and first["sql"] is None
    thread = first["thread_id"]
    assert wang.get("/api/ask/threads")[0]["status"] == "waiting"
    # 模拟服务重启：丢弃进程内的图与连接池，会话状态从 PostgreSQL 恢复
    agent.reset_state_cache()
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM avelis_agent.checkpoints WHERE thread_id = :t"),
                            {"t": thread}).scalar() > 0
    # 别人不能续接这个会话
    ctx["apis"]["sun"].post("/api/ask", {"question": DAY, "thread_id": thread}, expect=404)
    second = wang.post("/api/ask", {"question": DAY, "thread_id": thread})
    assert value_of(second) == Decimal("270.00") and second["thread_id"] == thread
    assert [s["node"] for s in second["steps"]][-4:] == ["clarify", "understand", "govern", "explain"]
    history = wang.get(f"/api/ask/threads/{thread}")
    assert [a["status"] for a in history["answers"]] == ["clarify", "answered"]


def test_12_agent_ambiguity_offers_candidates(admin: Api):
    wang = ctx["apis"]["wang"]
    first = wang.post("/api/ask", {"question": f"{DAY} 华东区销售额"})
    assert first["status"] == "clarify"
    assert "退款后净销售额" in first["evidence"]["clarification"]["options"]
    second = wang.post("/api/ask", {"question": "退款后净销售额", "thread_id": first["thread_id"]})
    assert value_of(second) == Decimal("270.00")


class _Scripted:
    """按脚本依次返回模型回复的假模型：只用于验证工具调用循环与治理边界，不代表真实模型能力。"""

    def __init__(self, replies):
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

        class Model(GenericFakeChatModel):
            def bind_tools(self, tools, **kwargs):
                return self

        self.model = Model(messages=iter(replies))


def _call(name, args, i):
    from langchain_core.messages import AIMessage

    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{i}"}])


def test_13_agent_tool_loop_with_scripted_model(admin: Api, monkeypatch):
    from avelis import modelcfg

    replies = [
        _call("search_metrics", {"keyword": "净销售额"}, 1),
        _call("match_dimension_values", {"metric_code": "net_sales", "dimension": "region", "text": "华东区"}, 2),
        # 编造的取值会被工具拒绝，模型必须更正
        _call("submit_intent", {"metric_codes": ["net_sales"], "start_date": DAY, "end_date": DAY,
                                "filters": [{"dimension": "region", "values": ["火星区"]}]}, 3),
        _call("submit_intent", {"metric_codes": ["net_sales"], "start_date": DAY, "end_date": DAY,
                                "filters": [{"dimension": "region", "values": ["华东"]}]}, 4),
        # 同一会话的追问：“那华北呢？”
        _call("submit_intent", {"metric_codes": ["net_sales"], "start_date": DAY, "end_date": DAY,
                                "filters": [{"dimension": "region", "values": ["华北"]}]}, 5),
        # 试图越权使用不可见的指标
        _call("submit_intent", {"metric_codes": ["secret_metric"], "start_date": DAY, "end_date": DAY}, 6),
        _call("ask_user", {"question": "没有找到该指标，请确认", "options": []}, 7),
    ]
    scripted = _Scripted(replies)
    monkeypatch.setattr(modelcfg, "resolve",
                        lambda session, purpose, **kw: modelcfg.Resolved(scripted.model, "scripted:test")
                        if purpose == "ask" else None)
    wang = ctx["apis"]["wang"]
    answer = wang.post("/api/ask", {"question": "9月15日华东区的净销售额"})
    assert value_of(answer) == Decimal("270.00")
    tools = [c["tool"] for c in answer["steps"][0]["tool_calls"]]
    assert tools == ["search_metrics", "match_dimension_values", "submit_intent", "submit_intent"]
    assert answer["evidence"]["model"]["is_demo"] is False
    follow = wang.post("/api/ask", {"question": "那华北呢？", "thread_id": answer["thread_id"]})
    assert value_of(follow) == Decimal("150.00")
    refused = wang.post("/api/ask", {"question": "查一下 secret_metric"})
    assert refused["status"] == "clarify" and refused["sql"] is None


@pytest.mark.llm
def test_14_model_settings_and_real_model(stack, admin: Api):
    """真实模型端到端：使用 .env 中的测试密钥（不进仓库），经模型配置接口登记。"""
    _, env, _ = stack
    key, name = env.get("AVELIS_MODEL_API_KEY"), env.get("AVELIS_MODEL_NAME")
    if not key or not name:
        pytest.skip("未配置 AVELIS_MODEL_API_KEY / AVELIS_MODEL_NAME")
    ctx["apis"]["zhang"].post("/api/model-providers", {"code": "x", "preset": "deepseek"}, expect=403)
    provider = admin.post("/api/model-providers", {
        "code": "deepseek", "preset": "deepseek", "base_url": env.get("AVELIS_MODEL_BASE_URL") or None,
        "api_key": key}, expect=201)
    assert provider["api_key"].endswith(key[-4:]) and key not in str(admin.get("/api/model-providers"))
    assert provider["endpoint"].endswith("/chat/completions")
    fetched = admin.post(f"/api/model-providers/{provider['id']}/fetch-models")["models"]
    assert name in fetched
    # 未启用的模型不能被指定
    admin.put("/api/model-settings", {"assignments": {"ask": {"provider_id": provider["id"], "model_id": name}}},
              expect=409)
    admin.patch(f"/api/model-providers/{provider['id']}", {"models": [{"id": name, "enabled": True}]})
    checked = admin.post(f"/api/model-providers/{provider['id']}/check", {"model_id": name})
    assert checked["ok"] is True and checked["tool_calling"] is True, checked
    settings = admin.put("/api/model-settings", {"assignments": {
        "ask": {"provider_id": provider["id"], "model_id": name}}})
    assert settings["mode"] == "configured" and settings["policy"]["send_results"] is True  # 默认允许（ADR 0007）

    wang = ctx["apis"]["wang"]
    answer = wang.post("/api/ask", {"question": "2026年9月15日华东区的退款后净销售额是多少？"})
    assert answer["evidence"]["model"]["adapter"] == f"deepseek:{name}"
    assert value_of(answer) == Decimal("270.00"), answer
    assert answer["explanation_source"] == "model"  # 默认把查询结果发给结果解释模型（ADR 0007）
    follow = wang.post("/api/ask", {"question": "那华北呢？", "thread_id": answer["thread_id"]})
    assert value_of(follow) == Decimal("150.00"), follow
    # 已失效的口径：真实模型可以追问或交给后端拦截，但不能执行 SQL，也不能悄悄改用其他口径
    retired = wang.post("/api/ask", {"question": "2026年9月15日华东区的实付销售额是多少？"})
    assert retired["status"] in ("blocked", "clarify") and retired["sql"] is None, retired
    assert retired["metric_version_ids"] in ([], [ctx["paid_v1"]])
    # 每轮回答只记录本轮的步骤
    assert [s["node"] for s in follow["steps"]] == ["understand", "govern", "explain"]
    audit = admin.get("/api/audit", params={"object_type": "model_provider"})
    assert {"model_provider.create", "model_provider.check"} <= {e["action"] for e in audit}
    assert key not in str(audit)
    admin.put("/api/model-settings", {"assignments": {"ask": None}})


# --- M3：工作台汇总 ---------------------------------------------------------------------

def test_15_workbench_summary(admin: Api):
    zhang, li = ctx["apis"]["zhang"], ctx["apis"]["li"]
    wb = admin.get("/api/workbench")
    assert [s["key"] for s in wb["stages"]] == ["collect", "lineage", "change", "metric", "ask"]
    collect = wb["stages"][0]
    assert collect["note"] == "2 / 2 数据源正常"
    assert {d["name"] for d in wb["datasources"]} == {"电商维表库（MySQL）", "演示 StarRocks"}
    assert len(wb["cadence"]) == 24 and any(c["runs"] for c in wb["cadence"])
    assert wb["recent_changes"] and wb["recent_changes"][0]["at"]
    # 被拦截的提问可见（管理员）
    assert wb["blocked_answers"] and {a["status"] for a in wb["blocked_answers"]} <= {"blocked", "denied", "clarify"}
    # 负责人看到自己负责的指标；复核者看到待批准的版本
    mine = zhang.get("/api/workbench")["my_metrics"]
    assert {m["name"] for m in mine} >= {"实付销售额", "退款后净销售额"}
    m = zhang.post("/api/metrics", {"code": "orders_cnt", "name": "订单数", "domain_id": ctx["trade"]}, expect=201)
    v = zhang.post(f"/api/metrics/{m['id']}/versions", {
        "description": "订单笔数", "base_asset_id": ctx["assets"]["default_catalog.shop.orders"],
        "expression": "COUNT(DISTINCT order_id)", "time_column": "order_date",
        "dimensions": [{"name": "region", "column": "region", "label": "区域"}], "grain": ["order_date", "region"]},
        expect=201)
    zhang.post(f"/api/metric-versions/{v['id']}/submit")
    todos = li.get("/api/workbench")["todos"]
    assert any(t["kind"] == "approve" and t["title"] == "订单数 V1" for t in todos)
    assert not any(t["kind"] == "approve" for t in zhang.get("/api/workbench")["todos"])  # 不提示自审
