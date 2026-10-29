"""问数表达能力（M6）验收：排名与前 N、多个指标、环比 / 同比、派生指标，以及它们与治理的结合。

全部在真实 StarRocks 上执行。演示数据：2026-09-15 实付销售额 华东 300 / 华北 150 / 华南 60，订单数 2 / 1 / 1；
华东 2026-09-14 为 90、2026-09-16 为 80。派生指标“客单价”= 实付销售额 / 订单数（华东 2026-09-15 = 150）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.conftest import Api

pytestmark = pytest.mark.acceptance
DAY = "2026-09-15"
V1_SQL = "SELECT order_date, region, SUM(paid_amount) AS sales_amount FROM shop.orders GROUP BY order_date, region"
V2_SQL = ("SELECT order_date, region, SUM(paid_amount - refund_amount) AS sales_amount "
          "FROM shop.orders GROUP BY order_date, region")
ctx: dict = {}


def d(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def answered(answer: dict) -> dict:
    assert answer["status"] == "answered", answer
    return answer


def sr(stack, sql: str) -> None:
    _, _, sr_root = stack
    conn = sr_root()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.close()


def collect(admin: Api) -> None:
    assert admin.post(f"/api/datasources/{ctx['sr']}/collect?wait=true")["status"] == "succeeded"


def test_00_setup_and_publish_derived_metric(stack, admin: Api):
    from avelis import demo_setup

    client, env, _ = stack
    demo_setup.setup(reset=False, metrics=True)
    pw = env["AVELIS_DEMO_USER_PASSWORD"]
    apis = {u: Api(client, u, pw) for u in ("zhang", "li", "wang", "sun")}
    domains = {x["code"]: x["id"] for x in admin.get("/api/domains")}
    sources = {x["name"]: x["id"] for x in admin.get("/api/datasources")}
    ctx.update(apis=apis, sr=sources["演示 StarRocks"])
    zhang = apis["zhang"]
    # 演示准备已经发布了派生指标“客单价”：核对它锁定了组成版本、共有维度为区域
    aov = next(x for x in admin.get("/api/metrics") if x["code"] == "aov")
    published = admin.get(f"/api/metric-versions/{aov['versions'][-1]['id']}")
    assert published["kind"] == "derived" and published["status"] == "published"
    assert [c["metric_code"] for c in published["components"]] == ["paid_sales", "orders_cnt"]
    assert [x["name"] for x in published["dimensions"]] == ["region"]
    # 创建流程本身：公式白名单、提交时锁定、禁止自审
    m = zhang.post("/api/metrics", {"code": "aov_check", "name": "派生校验", "domain_id": domains["trade"]},
                   expect=201)
    zhang.post(f"/api/metrics/{m['id']}/versions", {"kind": "derived", "description": "x",
                                                    "expression": "__import__('os')"}, expect=422)
    v = zhang.post(f"/api/metrics/{m['id']}/versions", {
        "kind": "derived", "description": "实付销售额 / 订单数", "expression": "paid_sales / orders_cnt"}, expect=201)
    assert v["kind"] == "derived" and [x["name"] for x in v["dimensions"]] == ["region"]
    v = zhang.post(f"/api/metric-versions/{v['id']}/submit")
    assert [c["metric_code"] for c in v["components"]] == ["paid_sales", "orders_cnt"]
    zhang.post(f"/api/metric-versions/{v['id']}/approve", {"note": "自审"}, expect=403)
    v = {"id": published["id"]}
    metrics = {x["code"]: x for x in admin.get("/api/metrics")}
    ctx.update(aov=v["id"], paid=metrics["paid_sales"]["versions"][-1]["id"],
               orders=metrics["orders_cnt"]["versions"][-1]["id"])


def test_01_top_n_pushed_down_to_sql(admin: Api):
    a = answered(ctx["apis"]["wang"].post("/api/ask", {"question": f"{DAY} 各区域实付销售额前 2"}))
    assert [(r[0], d(r[1])) for r in a["result"]["rows"]] == [("华东", Decimal("300")), ("华北", Decimal("150"))]
    assert "ORDER BY" in a["sql"] and "DESC" in a["sql"] and "LIMIT 2" in a["sql"]  # 单指标：排名在数据库完成
    assert a["intent"]["order"] == "desc" and a["intent"]["limit"] == 2


def test_02_multiple_metrics_aligned_by_dimension(admin: Api):
    a = answered(ctx["apis"]["wang"].post("/api/ask", {"question": f"{DAY} 各区域实付销售额和订单数"}))
    assert a["result"]["columns"] == ["region", "实付销售额", "订单数"]
    got = {r[0]: (d(r[1]), d(r[2])) for r in a["result"]["rows"]}
    assert got == {"华东": (Decimal("300"), Decimal("2")), "华北": (Decimal("150"), Decimal("1")),
                   "华南": (Decimal("60"), Decimal("1"))}
    assert len(a["plan"]["subqueries"]) == 2 and a["sql"].count("SELECT") == 2
    assert set(a["metric_version_ids"]) == {ctx["paid"], ctx["orders"]}


def test_03_previous_period_and_previous_year(admin: Api):
    wang = ctx["apis"]["wang"]
    mom = answered(wang.post("/api/ask", {"question": "2026-09-16 华东区实付销售额 环比"}))
    assert mom["result"]["columns"] == ["实付销售额", "实付销售额·上一期", "实付销售额·变化", "实付销售额·变化率"]
    assert [d(x) for x in mom["result"]["rows"][0]] == [Decimal("80"), Decimal("300"), Decimal("-220"),
                                                        Decimal("-0.7333")]
    ranges = {s["period"]: s["range"] for s in mom["plan"]["subqueries"]}
    assert ranges == {"current": ["2026-09-16", "2026-09-16"], "previous_period": ["2026-09-15", "2026-09-15"]}
    yoy = answered(wang.post("/api/ask", {"question": "2026-09-16 华东区实付销售额 同比"}))
    # 去年同期没有数据：如实为空，不当作 0，也不计算变化率
    assert [d(x) for x in yoy["result"]["rows"][0]] == [Decimal("80"), None, None, None]


def test_04_derived_metric_computed_from_governed_components(admin: Api):
    wang = ctx["apis"]["wang"]
    a = answered(wang.post("/api/ask", {"question": f"{DAY} 华东区客单价"}))
    assert a["result"]["columns"] == ["客单价"] and d(a["result"]["rows"][0][0]) == Decimal("150")
    assert set(a["metric_version_ids"]) == {ctx["aov"], ctx["paid"], ctx["orders"]}
    assert a["evidence"]["derived"][0]["formula"] == "paid_sales / orders_cnt"
    ranked = answered(wang.post("/api/ask", {"question": f"{DAY} 各区域客单价前 3"}))
    values = [(r[0], d(r[1])) for r in ranked["result"]["rows"]]
    assert {v for _, v in values[:2]} == {Decimal("150")} and values[2] == ("华南", Decimal("60"))


def test_05_component_affected_blocks_derived_and_multi(stack, admin: Api):
    wang, zhang = ctx["apis"]["wang"], ctx["apis"]["zhang"]
    sr(stack, f"ALTER VIEW shop.daily_sales AS {V2_SQL}")
    collect(admin)
    assert admin.get(f"/api/metric-versions/{ctx['paid']}")["status"] == "affected"
    derived = admin.get(f"/api/metric-versions/{ctx['aov']}")
    assert derived["status"] == "affected" and "实付销售额" in derived["status_reason"]  # 级联受影响
    blocked = wang.post("/api/ask", {"question": f"{DAY} 华东区客单价"})
    assert blocked["status"] == "blocked" and blocked["sql"] is None and blocked["result"] == {}
    multi = wang.post("/api/ask", {"question": f"{DAY} 各区域订单数和实付销售额"})
    assert multi["status"] == "blocked" and "实付销售额" in multi["message"] and multi["result"] == {}
    answered(wang.post("/api/ask", {"question": f"{DAY} 各区域订单数"}))  # 无关指标照常

    # 恢复：数据库改回原定义 → 两个复核任务分别“已恢复” → 客单价重新可用
    sr(stack, f"ALTER VIEW shop.daily_sales AS {V1_SQL}")
    collect(admin)
    tasks = {t["target"]["id"]: t for t in zhang.get("/api/review-tasks")}
    zhang.post(f"/api/review-tasks/{tasks[ctx['paid']]['id']}/resolve", {"resolution": "restored", "note": "已恢复"})
    zhang.post(f"/api/review-tasks/{tasks[ctx['aov']]['id']}/resolve", {"resolution": "restored", "note": "组成已恢复"})
    assert admin.get(f"/api/metric-versions/{ctx['aov']}")["status"] == "published"
    a = answered(wang.post("/api/ask", {"question": f"{DAY} 华东区客单价"}))
    assert d(a["result"]["rows"][0][0]) == Decimal("150")


def test_06_plan_respects_source_permissions(admin: Api):
    sun = ctx["apis"]["sun"]
    denied = sun.post("/api/ask", {"question": f"{DAY} 实付销售额和类目销售额"})
    assert denied["status"] == "denied" and denied["error_code"] == "permission" and denied["result"] == {}


@pytest.mark.llm
def test_07_real_model_multi_metric_ranking(stack, admin: Api):
    """真实模型（.env 中的 DeepSeek）：只产生结构化意图；排序、对齐、计算都在后端完成。"""
    from avelis.bootstrap import model_from_env

    model_from_env()
    a = answered(ctx["apis"]["wang"].post("/api/ask", {
        "question": "2026年9月15日各区域的实付销售额和订单数分别是多少？按实付销售额从高到低排"}))
    assert a["intent"]["metric_codes"][:2] == ["paid_sales", "orders_cnt"] and a["intent"]["order"] == "desc"
    assert [r[0] for r in a["result"]["rows"]] == ["华东", "华北", "华南"]
    assert [d(r[1]) for r in a["result"]["rows"]] == [Decimal("300"), Decimal("150"), Decimal("60")]
    assert a["explanation_source"] == "template"
