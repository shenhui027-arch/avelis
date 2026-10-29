"""M2 验收（故事 8）：导入 SQL 文件、Python 代码、Shell 脚本，补充完善血缘。

断言实际数据库状态：解析边带行号证据写入；模型候选在处理前不进入已确认血缘；
驳回 / 接受 / 修改后状态正确；动态 SQL 补参数后能解析；重复导入不产生重复边；
没有授权发送代码时候选来自演示规则并有标记；授权后真实模型的候选同样必须人工确认（llm）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from tests.conftest import Api

pytestmark = pytest.mark.acceptance
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "imports"
ctx: dict = {}


def confirmed_edges(job_id: str):
    from avelis.db import session_scope
    from avelis.models import LineageEdge

    with session_scope() as s:
        return [(e.origin, e.source_ref, e.source_column, e.target_ref, e.target_column, e.relation)
                for e in s.scalars(select(LineageEdge).where(LineageEdge.import_job_id == job_id,
                                                             LineageEdge.status == "active"))]


def test_00_setup(stack, admin: Api):
    client, env, _ = stack
    pw = env["AVELIS_DEMO_USER_PASSWORD"]
    admin.post("/api/users", {"username": "zhang", "display_name": "张敏", "password": pw, "roles": ["steward"]},
               expect=201)
    admin.post("/api/users", {"username": "wang", "display_name": "王强", "password": pw, "roles": ["analyst"]},
               expect=201)
    mysql = admin.post("/api/datasources", {
        "name": "电商维表库（MySQL）", "kind": "mysql", "host": "127.0.0.1", "port": int(env["SHOP_MYSQL_PORT"]),
        "options": {"databases": ["shop_dim"]},
        "collect": {"username": "avelis_collector", "password": env["SHOP_MYSQL_COLLECTOR_PASSWORD"]}}, expect=201)
    sr = admin.post("/api/datasources", {
        "name": "演示 StarRocks", "kind": "starrocks", "host": "127.0.0.1", "port": int(env["SR_QUERY_PORT"]),
        "options": {"databases": ["shop", "ads", "dim"], "catalogs": ["shop_mysql"],
                    "catalog_databases": {"shop_mysql": ["shop_dim"]}},
        "collect": {"username": "avelis_collector", "password": env["SR_COLLECTOR_PASSWORD"]},
        "query": {"username": "avelis_reader", "password": env["SR_READER_PASSWORD"]}}, expect=201)
    admin.post("/api/endpoint-aliases", {"datasource_id": mysql["id"], "host": "shop-mysql", "port": 3306},
               expect=201)
    admin.post(f"/api/datasources/{mysql['id']}/collect?wait=true")
    admin.post(f"/api/datasources/{sr['id']}/collect?wait=true")
    ctx.update(sr=sr["id"], admin=admin, zhang=Api(client, "zhang", pw), wang=Api(client, "wang", pw))


def test_01_sql_file_multi_statement():
    zhang = ctx["zhang"]
    content = (FIXTURES / "etl_orders.sql").read_text(encoding="utf-8")
    job = zhang.post("/api/imports", {"datasource_id": ctx["sr"], "filename": "etl_orders.sql", "content": content})
    assert job["duplicate"] is False and job["language"] == "sql" and job["status"] == "done"
    kinds = [(s["statement_type"], s["status"], s["target"]) for s in job["statements"]]
    assert kinds[0][:2] == (None, "skipped") or kinds[0][1] == "skipped"  # USE
    assert ("CREATE TABLE AS", "parsed", "default_catalog.shop.tmp_paid") in kinds
    assert ("CREATE TABLE AS", "parsed", "default_catalog.ads.order_daily_net") in kinds
    assert ("INSERT", "parsed", "default_catalog.ads.category_daily") in kinds
    assert any(s["status"] == "unsupported" and "DROP" in (s["reason"] or "") for s in job["statements"])
    edges = confirmed_edges(job["id"])
    # 临时表串联：orders → tmp_paid → order_daily_net
    assert ("import", "default_catalog.shop.orders", "paid_amount", "default_catalog.shop.tmp_paid", "paid_amount",
            "value_direct") in edges
    assert ("import", "default_catalog.shop.tmp_paid", "refund_amount", "default_catalog.ads.order_daily_net",
            "net_amount", "value_aggregated") in edges
    # 跨库：MySQL 维表经 Catalog 进入 INSERT
    assert ("import", "shop_mysql.shop_dim.dim_category", "category_name", "default_catalog.ads.category_daily",
            "category_name", "value_direct") in edges
    assert ("import", "default_catalog.shop.orders", "status", "default_catalog.shop.tmp_paid", None,
            "filter") in edges
    # 行号证据
    tmp = next(c for c in job["candidates"] if c["target"] == "default_catalog.shop.tmp_paid.paid_amount")
    assert tmp["evidence"]["lines"] == [5, 8] and tmp["status"] == "written" and tmp["origin"] == "parsed"
    ctx["sql_job"] = job

    # 血缘图：导入的目标表未采集，显示为设计时节点；来源是已采集资产
    graph = zhang.get("/api/lineage/graph")
    design = {n["qualified"] for n in graph["nodes"] if n["kind"] == "design_time"}
    assert {"default_catalog.ads.order_daily_net", "default_catalog.ads.category_daily"} <= design

    # 重复导入：返回原任务，不产生重复边
    again = zhang.post("/api/imports", {"datasource_id": ctx["sr"], "filename": "副本.sql", "content": content})
    assert again["duplicate"] is True and again["id"] == job["id"]
    assert len(confirmed_edges(job["id"])) == len(edges)


def test_02_python_code_with_dataframe_and_dynamic_sql():
    zhang = ctx["zhang"]
    content = (FIXTURES / "etl_daily_orders.py").read_text(encoding="utf-8")
    job = zhang.post("/api/imports", {"datasource_id": ctx["sr"], "filename": "etl_daily_orders.py",
                                      "content": content})
    assert job["language"] == "python" and job["status"] == "needs_params"
    by_method = {}
    for s in job["statements"]:
        by_method.setdefault(s["method"], []).append(s)
    refund = next(s for s in by_method["python_ast"] if s["target"] == "default_catalog.ads.refund_daily")
    assert refund["status"] == "parsed" and refund["lines"][0] == 9  # spark.sql( 所在行
    dynamic = next(s for s in by_method["python_ast"] if s["status"] == "needs_params")
    assert dynamic["placeholders"] == ["dt"] and "dt" in dynamic["reason"]
    unrecognized = by_method["unrecognized"]
    assert len(unrecognized) == 1 and unrecognized[0]["status"] == "model"

    # 模型候选：未授权发送代码 → 演示规则，并标明；发出的片段已脱敏
    payload = job["model_payload"][0]
    assert payload["sent_to"] == "未发送（演示规则）" and payload["redactions"] >= 1
    assert "Pa55word" not in payload["text"] and "<redacted>" in payload["text"]
    model = [c for c in job["candidates"] if c["origin"] == "model"]
    assert len(model) >= 2 and all(c["status"] == "pending" for c in model)
    assert all(c["evidence"]["is_demo"] for c in model)
    # 处理前不进入已确认血缘
    assert not [e for e in confirmed_edges(job["id"]) if e[3] == "default_catalog.ads.item_daily"]

    # 补参数后动态 SQL 能解析
    job = zhang.put(f"/api/imports/{job['id']}/params", {"params": {"dt": "2026-09-15"}})
    dynamic = next(s for s in job["statements"] if s["id"] == dynamic["id"])
    assert dynamic["status"] == "parsed" and "'2026-09-15'" in dynamic["sql_text"]
    assert ("import", "default_catalog.shop.orders", "paid_amount", "default_catalog.ads.region_snapshot",
            "paid_total", "value_aggregated") in confirmed_edges(job["id"])
    assert job["status"] == "needs_review"
    ctx["py_job"] = job


def test_03_human_decisions():
    zhang = ctx["zhang"]
    job = ctx["py_job"]
    model = [c for c in job["candidates"] if c["origin"] == "model"]
    table_edge = next(c for c in model if c["relation"] == "table" and c["source_ref"].endswith("shop.orders"))
    agg_edge = next(c for c in model if c["relation"] == "value_aggregated")
    # 分析师不能处理仍待复核的候选，也不能改变候选、血缘边或审计
    assert agg_edge["status"] == "pending"
    edges_before = sorted(confirmed_edges(job["id"]), key=repr)
    admin = ctx["admin"]  # 全量审计只有管理员可读
    audit_before = admin.get("/api/audit", params={"limit": 1000})
    ctx["wang"].post(f"/api/candidates/{agg_edge['id']}/decision", {"decision": "reject", "reason": "x"},
                     expect=403)
    unchanged = zhang.get(f"/api/imports/{job['id']}")
    assert next(c for c in unchanged["candidates"] if c["id"] == agg_edge["id"]) == agg_edge
    assert sorted(confirmed_edges(job["id"]), key=repr) == edges_before
    assert admin.get("/api/audit", params={"limit": 1000}) == audit_before
    zhang.post(f"/api/candidates/{table_edge['id']}/decision", {"decision": "accept"}, expect=422)  # 缺理由
    zhang.post(f"/api/candidates/{table_edge['id']}/decision",
               {"decision": "reject", "reason": "orders 只用于关联，不是数据来源"})
    zhang.post(f"/api/candidates/{agg_edge['id']}/decision",
               {"decision": "accept", "reason": "item_total = SUM(item_amount)，与代码一致"})
    # 修改一条解析边：关系类型更正
    parsed = next(c for c in job["candidates"] if c["origin"] == "parsed"
                  and c["target"] == "default_catalog.ads.refund_daily.refund_date")
    job = zhang.post(f"/api/candidates/{parsed['id']}/decision",
                     {"decision": "modify", "reason": "refund_date 作为分组键输出", "relation": "group_by"})
    states = {c["id"]: c for c in job["candidates"]}
    assert states[table_edge["id"]]["status"] == "rejected" and states[table_edge["id"]]["lineage_edge_id"] is None
    assert states[agg_edge["id"]]["status"] == "accepted" and states[agg_edge["id"]]["decided_by"]
    assert states[parsed["id"]]["status"] == "modified" and states[parsed["id"]]["modified"] == {"relation": "group_by"}
    edges = confirmed_edges(job["id"])
    assert ("model_confirmed", "default_catalog.shop.order_items", "item_amount", "default_catalog.ads.item_daily",
            "item_total", "value_aggregated") in edges
    assert not any(e[1] == "default_catalog.shop.orders" and e[3] == "default_catalog.ads.item_daily"
                   and e[5] == "table" for e in edges)
    assert ("import_manual", "default_catalog.shop.refund_raw", "refund_date", "default_catalog.ads.refund_daily",
            "refund_date", "group_by") in edges
    assert ("import", "default_catalog.shop.refund_raw", "refund_date", "default_catalog.ads.refund_daily",
            "refund_date", "value_direct") not in edges  # 原解析边已失效
    # 仍有未处理的候选 → 任务待复核；全部处理后完成
    pending = [c for c in job["candidates"] if c["status"] == "pending"]
    for c in pending:
        job = zhang.post(f"/api/candidates/{c['id']}/decision", {"decision": "reject", "reason": "演示：不采纳"})
    assert job["status"] == "done"


def test_04_shell_script_and_upload(stack):
    zhang = ctx["zhang"]
    raw = (FIXTURES / "load_snapshot.sh").read_bytes()
    job = zhang.call("POST", "/api/imports/upload", data={"datasource_id": ctx["sr"]},
                     files={"file": ("load_snapshot.sh", raw, "text/x-sh")})
    assert job["language"] == "shell" and job["status"] == "needs_params"
    targets = {s["target"] for s in job["statements"]}
    assert "default_catalog.ads.refund_count" in targets
    job = zhang.put(f"/api/imports/{job['id']}/params", {"params": {"DT": "2026-09-15"}})
    assert job["status"] == "done"
    assert {s["target"] for s in job["statements"]} >= {"default_catalog.ads.region_paid_snapshot",
                                                         "default_catalog.ads.refund_count"}


def test_05_import_does_not_touch_observed_lineage():
    """设计时血缘不参与字段推导指纹，不覆盖数据库观察到的定义。"""
    from avelis.collect.service import current_fingerprints
    from avelis.db import session_scope
    from avelis.models import ColumnFingerprint

    with session_scope() as s:
        count = s.scalar(select(func.count()).select_from(ColumnFingerprint))
        fps = current_fingerprints(s)
    assert not any(k[0] is None for k in fps)
    run = ctx["zhang"].post(f"/api/datasources/{ctx['sr']}/collect?wait=true")
    assert run["stats"].get("event:derivation_changed", 0) == 0
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(ColumnFingerprint)) == count


@pytest.mark.llm
def test_06_real_model_candidates_need_confirmation(stack, admin: Api):
    _, env, _ = stack
    key, name = env.get("AVELIS_MODEL_API_KEY"), env.get("AVELIS_MODEL_NAME")
    if not key or not name:
        pytest.skip("未配置模型密钥")
    provider = admin.post("/api/model-providers", {
        "code": "deepseek", "preset": "deepseek", "base_url": env.get("AVELIS_MODEL_BASE_URL") or None,
        "api_key": key, "models": [{"id": name, "enabled": True}]}, expect=201)
    admin.put("/api/model-settings", {"assignments": {"import": {"provider_id": provider["id"], "model_id": name}},
                                      "policy": {"send_code": True, "send_results": False}})
    content = (FIXTURES / "etl_daily_orders.py").read_text(encoding="utf-8").replace("item_rollup", "item_rollup_v2")
    job = ctx["zhang"].post("/api/imports", {"datasource_id": ctx["sr"], "filename": "etl_v2.py",
                                             "content": content, "params": {"dt": "2026-09-15"}})
    payload = job["model_payload"][0]
    assert payload["sent_to"] == f"deepseek:{name}" and "Pa55word" not in payload["text"]
    model = [c for c in job["candidates"] if c["origin"] == "model"]
    assert model, job["stats"]
    assert all(c["status"] == "pending" and c["evidence"]["is_demo"] is False for c in model)
    assert not [e for e in confirmed_edges(job["id"]) if e[0] == "model_confirmed"]
    # 模型输出经过校验：来源表都是已采集的表
    assert all(c["source_ref"].startswith("default_catalog.shop.") for c in model)
    admin.put("/api/model-settings", {"assignments": {"import": None},
                                      "policy": {"send_code": False, "send_results": False}})
