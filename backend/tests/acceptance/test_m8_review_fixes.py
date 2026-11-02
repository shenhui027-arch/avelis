"""验收评审（Codex，2026-10-29）发现的治理与权限问题：每条都在真实环境复现原场景，断言修复后的实际行为。

编号对应评审报告：#1 指纹漏掉中间表达式；#2 待批准版本借“已恢复”绕过审批；#3 跨域批准沿用旧快照；
#4 无列引用的查询漏建表级依赖；#5 维度取值绕过来源权限；#6 粒度核验返回隐藏来源的值；#9 关联侧可加度量；
#11 OR 关联推导唯一性；#12 实例身份变化；#13 采集前同名重建；#16 旁路读取入口。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.conftest import Api

pytestmark = pytest.mark.acceptance
DAY = "2026-09-15"
TABLE_PROPS = 'DUPLICATE KEY(id) DISTRIBUTED BY HASH(id) BUCKETS 1 PROPERTIES("replication_num" = "1")'
ctx: dict = {}


def sr(stack, *statements: str) -> None:
    _, _, sr_root = stack
    conn = sr_root()
    with conn.cursor() as cur:
        for statement in statements:
            cur.execute(statement)
    conn.close()


def collect(api: Api, ds: str) -> dict:
    run = api.post(f"/api/datasources/{ds}/collect?wait=true")
    assert run["status"] == "succeeded", run
    return run


def assets(api: Api) -> dict[str, str]:
    return {a["qualified"]: a["id"] for a in api.get("/api/assets")}


def assign(asset_id: str, domain: str = "trade") -> None:
    owner = ctx["apis"]["zhang" if domain == "trade" else "zhao"]
    owner.patch(f"/api/assets/{asset_id}", {"domain_id": ctx["domains"][domain]})


def value_of(answer: dict) -> Decimal:
    assert answer["status"] == "answered", answer
    return Decimal(str(answer["result"]["rows"][0][-1]))


def new_metric(code: str, name: str, spec: dict, submit: bool = True, approve: bool = True) -> str:
    zhang, li = ctx["apis"]["zhang"], ctx["apis"]["li"]
    m = zhang.post("/api/metrics", {"code": code, "name": name, "domain_id": ctx["domains"]["trade"]}, expect=201)
    v = zhang.post(f"/api/metrics/{m['id']}/versions", spec, expect=201)
    if submit:
        zhang.post(f"/api/metric-versions/{v['id']}/submit")
    if approve:
        assert li.post(f"/api/metric-versions/{v['id']}/approve", {"note": "评审修复验收"})["status"] == "published"
    return v["id"]


def test_00_setup(stack, admin: Api):
    from avelis import demo_setup

    client, env, _ = stack
    demo_setup.setup(reset=False, metrics=True)
    pw = env["AVELIS_DEMO_USER_PASSWORD"]
    admin.post(
        "/api/users",
        {
            "username": "wu",
            "display_name": "吴（只属交易域的维护者）",
            "password": pw,
            "roles": ["steward"],
            "must_change_password": False,
        },
        expect=201,
    )
    apis = {u: Api(client, u, pw) for u in ("zhang", "zhao", "li", "wang", "sun", "wu")}
    domains = {d["code"]: d["id"] for d in admin.get("/api/domains")}
    users = {u["username"]: u["id"] for u in admin.get("/api/users")}
    apis["zhang"].put(f"/api/domains/{domains['trade']}/members", {"user_id": users["wu"], "role": "maintainer"})
    sources = {d["name"]: d["id"] for d in admin.get("/api/datasources")}
    metrics = {m["code"]: m for m in admin.get("/api/metrics")}
    ctx.update(
        apis=apis,
        domains=domains,
        sr=sources["演示 StarRocks"],
        mysql=sources["电商维表库（MySQL）"],
        versions={code: m["versions"][-1]["id"] for code, m in metrics.items()},
    )
    assert value_of(apis["wang"].post("/api/ask", {"question": f"{DAY} 厨具类目销售额"})) == Decimal("270.00")


def test_01_cte_inner_expression_change_affects_metric(stack, admin: Api):  # 1
    sr(
        stack,
        "CREATE VIEW shop.m8_cte AS WITH t AS (SELECT order_date, region, paid_amount - refund_amount AS net "
        "FROM shop.orders) SELECT order_date, region, SUM(net) AS total FROM t GROUP BY order_date, region",
    )
    collect(admin, ctx["sr"])
    view = assets(admin)["default_catalog.shop.m8_cte"]
    assign(view)
    v = new_metric(
        "m8_cte_total",
        "CTE 合计",
        {
            "description": "CTE",
            "base_asset_id": view,
            "expression": "SUM(total)",
            "time_column": "order_date",
            "grain": ["order_date", "region"],
        },
    )
    sr(
        stack,
        "ALTER VIEW shop.m8_cte AS WITH t AS (SELECT order_date, region, paid_amount + refund_amount AS net "
        "FROM shop.orders) SELECT order_date, region, SUM(net) AS total FROM t GROUP BY order_date, region",
    )
    collect(admin, ctx["sr"])
    assert admin.get(f"/api/metric-versions/{v}")["status"] == "affected"


def test_02b_restore_returns_pending_version_to_review(stack, admin: Api):  # 2
    from avelis.db import session_scope
    from avelis.models import MetricVersion, ReviewTask

    # 用视图上的待批准版本复现完整路径：受影响 → 定义恢复 → 复核“已恢复” → 回到待批准（不是已发布）
    sr(
        stack,
        "CREATE VIEW shop.m8_restore AS SELECT order_date, SUM(paid_amount) AS s FROM shop.orders GROUP BY order_date",
    )
    collect(admin, ctx["sr"])
    view = assets(admin)["default_catalog.shop.m8_restore"]
    assign(view)
    v = new_metric(
        "m8_restore",
        "恢复测试",
        {
            "description": "x",
            "base_asset_id": view,
            "expression": "SUM(s)",
            "time_column": "order_date",
            "grain": ["order_date"],
        },
        approve=False,
    )
    sr(
        stack,
        "ALTER VIEW shop.m8_restore AS SELECT order_date, SUM(refund_amount) AS s FROM shop.orders GROUP BY order_date",
    )
    collect(admin, ctx["sr"])
    assert admin.get(f"/api/metric-versions/{v}")["status"] == "affected"
    sr(
        stack,
        "ALTER VIEW shop.m8_restore AS SELECT order_date, SUM(paid_amount) AS s FROM shop.orders GROUP BY order_date",
    )
    collect(admin, ctx["sr"])
    zhang = ctx["apis"]["zhang"]
    task = next(t for t in zhang.get("/api/review-tasks") if t["target"]["id"] == v)
    zhang.post(f"/api/review-tasks/{task['id']}/resolve", {"resolution": "restored", "note": "定义已恢复"})
    version = admin.get(f"/api/metric-versions/{v}")
    assert version["status"] == "pending_review" and version["reviewed_by"] is None
    # 仍须复核者正式批准；负责人不能自审
    zhang.post(f"/api/metric-versions/{v}/approve", {"note": "自己批"}, expect=403)
    assert ctx["apis"]["li"].post(f"/api/metric-versions/{v}/approve", {"note": "正式复核"})["status"] == "published"
    with session_scope() as s:
        assert s.get(MetricVersion, v).reviewed_by is not None
        assert s.scalar(select(ReviewTask.status).where(ReviewTask.id == task["id"])) == "resolved"


def test_03_join_path_reapproval_after_domain_change(admin: Api):  # 3
    zhang, zhao, wang = (ctx["apis"][u] for u in ("zhang", "zhao", "wang"))
    paths = {p["code"]: p for p in admin.get("/api/join-paths")}
    i_p, p_c = paths["items_product"], paths["product_category"]
    assert i_p["status"] == p_c["status"] == "approved"
    product_asset = i_p["right"]["asset_id"]  # 商品维表：items_product 的右侧、product_category 的左侧

    def status(path):
        return next(p for p in admin.get("/api/join-paths") if p["id"] == path["id"])["status"]

    def approve(path, *owners):
        for owner, domain in owners:
            owner.post(
                f"/api/join-paths/{path['id']}/decisions", {"domain_id": ctx["domains"][domain], "decision": "approved"}
            )

    # 商品维表改归交易域（跨域迁移需要两边的维护权限，由管理员执行）：涉及它的两条路径都回到待批准
    admin.patch(f"/api/assets/{product_asset}", {"domain_id": ctx["domains"]["trade"]})
    assert status(i_p) == status(p_c) == "pending"
    blocked = wang.post("/api/ask", {"question": f"{DAY} 厨具类目销售额"})
    assert blocked["status"] == "blocked" and blocked["error_code"] == "join_path_not_approved"
    # 按当前涉及的业务域重新批准：items_product 两侧都在交易域；product_category 跨交易域与商品域
    approve(i_p, (zhang, "trade"))
    approve(p_c, (zhang, "trade"))
    assert status(p_c) == "pending"  # 商品域旧批准已作废，不沿用
    approve(p_c, (zhao, "product"))
    assert status(i_p) == status(p_c) == "approved"
    assert value_of(wang.post("/api/ask", {"question": f"{DAY} 厨具类目销售额"})) == Decimal("270.00")
    # 改回商品域：两条路径再次按新的业务域组合重新批准
    admin.patch(f"/api/assets/{product_asset}", {"domain_id": ctx["domains"]["product"]})
    approve(i_p, (zhang, "trade"), (zhao, "product"))
    approve(p_c, (zhao, "product"))
    assert status(i_p) == status(p_c) == "approved"
    assert value_of(wang.post("/api/ask", {"question": f"{DAY} 厨具类目销售额"})) == Decimal("270.00")


def test_04_count_without_columns_still_guards_sources(stack, admin: Api):  # 4
    from avelis.db import session_scope
    from avelis.models import LineageEdge

    sr(
        stack,
        "CREATE VIEW shop.m8_count AS SELECT CURRENT_DATE() AS day, COUNT(1) AS cnt "
        "FROM shop_mysql.shop_dim.dim_customer",
    )
    collect(admin, ctx["sr"])
    found = assets(admin)
    view, customer = found["default_catalog.shop.m8_count"], found["shop_mysql.shop_dim.dim_customer"]
    assign(view)
    with session_scope() as s:
        rel = {
            (e.source_asset_id, e.relation)
            for e in s.scalars(
                select(LineageEdge).where(LineageEdge.target_asset_id == view, LineageEdge.status == "active")
            )
        }
    assert (customer, "table") in rel
    new_metric(
        "m8_count",
        "客户计数",
        {"description": "x", "base_asset_id": view, "expression": "SUM(cnt)", "time_column": "day", "grain": ["day"]},
    )
    denied = ctx["apis"]["sun"].post("/api/ask", {"question": "今天 客户计数"})
    assert denied["status"] in ("denied", "clarify") and denied["sql"] is None


def test_05_dimension_values_respect_full_source_permission(admin: Api):  # 5
    from avelis.ask.service import _dimension_values
    from avelis.config import get_settings
    from avelis.db import session_scope
    from avelis.models import MetricVersion, User

    with session_scope() as s:
        version = s.get(MetricVersion, ctx["versions"]["category_sales"])
        sun = s.scalar(select(User).where(User.username == "sun"))
        wang = s.scalar(select(User).where(User.username == "wang"))
        assert "category" not in _dimension_values(s, sun, version, get_settings())  # 类目维表属于商品域
        assert "厨具" in _dimension_values(s, wang, version, get_settings())["category"]


def test_06_grain_verify_needs_full_source_permission(stack, admin: Api):  # 6
    found = assets(admin)
    view = found["default_catalog.shop.m8_count"]
    wu = ctx["apis"]["wu"]
    grain = wu.post(f"/api/assets/{view}/grains", {"columns": ["cnt"], "reason": "评审 #6"}, expect=201)
    refused = wu.post(f"/api/grains/{grain['id']}/verify", expect=403)
    assert "dim_customer" in refused["detail"]


def test_09_additive_measure_from_joined_side_is_blocked(admin: Api):  # 9
    zhang = ctx["apis"]["zhang"]
    paths = {p["code"]: p for p in admin.get("/api/join-paths")}
    items = assets(admin)["default_catalog.shop.order_items"]
    m = zhang.post(
        "/api/metrics",
        {"code": "m8_side", "name": "明细上的订单金额", "domain_id": ctx["domains"]["trade"]},
        expect=201,
    )
    v = zhang.post(
        f"/api/metrics/{m['id']}/versions",
        {
            "description": "以明细为基础，求订单侧金额",
            "base_asset_id": items,
            "expression": "SUM(o.paid_amount)",
            "time_column": "o.order_date",
            "joins": [{"alias": "o", "join_path_id": paths["items_orders"]["id"]}],
            "grain": ["order_date"],
            "dedupe_basis": "N:1",
        },
        expect=201,
    )
    refused = zhang.post(f"/api/metric-versions/{v['id']}/submit", expect=409)
    assert any("关联侧" in b for b in refused["blockers"])


def test_11_or_join_does_not_propagate_uniqueness(stack, admin: Api):  # 11
    sr(
        stack,
        "CREATE VIEW shop.m8_or AS SELECT o.order_id, o.paid_amount FROM shop.orders o "
        "JOIN shop.order_items i ON o.order_id = i.order_id OR o.region = 'x'",
    )
    collect(admin, ctx["sr"])
    view = admin.get(f"/api/assets/{assets(admin)['default_catalog.shop.m8_or']}")
    assert view["parse"]["status"] == "partial" and "OR" in view["parse"]["reason"]
    assert not [g for g in view["grains"] if g["origin"] == "passthrough"]


def test_13_recreated_before_collection_is_stale(stack, admin: Api):  # 13
    sr(
        stack,
        f"CREATE TABLE shop.m8_t (id BIGINT, d DATE, v INT) {TABLE_PROPS}",
        "INSERT INTO shop.m8_t VALUES (1, '2026-09-15', 7)",
    )
    collect(admin, ctx["sr"])
    table = assets(admin)["default_catalog.shop.m8_t"]
    assign(table)
    new_metric(
        "m8_t_sum",
        "重建测试量",
        {"description": "x", "base_asset_id": table, "expression": "SUM(v)", "time_column": "d", "grain": ["d"]},
    )
    wang = ctx["apis"]["wang"]
    assert value_of(wang.post("/api/ask", {"question": f"{DAY} 重建测试量"})) == Decimal("7")
    sr(
        stack,
        "DROP TABLE shop.m8_t FORCE",
        f"CREATE TABLE shop.m8_t (id BIGINT, d DATE, v INT) {TABLE_PROPS}",
        "INSERT INTO shop.m8_t VALUES (1, '2026-09-15', 9)",
    )
    stale = wang.post("/api/ask", {"question": f"{DAY} 重建测试量"})
    assert stale["status"] == "blocked" and stale["error_code"] == "metadata_stale", stale
    assert "删除后同名重建" in stale["message"] and not stale["cache_hit"]


def test_12_instance_identity_change_starts_new_generation(admin: Api):  # 12
    from avelis.db import session_scope
    from avelis.models import Datasource

    before = assets(admin)["def.shop_dim.dim_category"]
    with session_scope() as s:  # 模拟“同一地址后面换成了另一台 MySQL 实例”：记录的实例身份与实际不同
        ds = s.get(Datasource, ctx["mysql"])
        ds.server_identity = {**(ds.server_identity or {}), "instance_id": "another-instance"}
    collect(admin, ctx["mysql"])
    after = assets(admin)["def.shop_dim.dim_category"]
    assert after != before and admin.get(f"/api/assets/{before}")["status"] == "deleted"
    cs = admin.get(f"/api/change-sets/{admin.get('/api/change-sets')[0]['id']}")
    assert any(e["kind"] == "server_identity_changed" and e["after"].get("new_generation") for e in cs["events"])
    assert admin.get(f"/api/metric-versions/{ctx['versions']['category_sales']}")["status"] == "affected"


def test_16_side_entry_points_respect_visibility(admin: Api):  # 16
    sun = ctx["apis"]["sun"]
    sun.get("/api/audit", expect=403)
    product = ("def.", "shop_mysql.", "default_catalog.dim.")
    product_only = [
        cs
        for cs in admin.get("/api/change-sets")
        if all(
            not e["asset"] or e["asset"].startswith(product)
            for e in admin.get(f"/api/change-sets/{cs['id']}")["events"]
        )
    ]
    assert product_only
    visible = {cs["id"] for cs in sun.get("/api/change-sets")}
    assert product_only[0]["id"] not in visible
    sun.get(f"/api/change-sets/{product_only[0]['id']}", expect=404)
    assert all(a["asked_by"] == sun.get("/api/me")["id"] for a in sun.get("/api/workbench")["blocked_answers"])
    assert not [p for p in sun.get("/api/join-paths") if p["code"] in ("items_product", "product_category")]
