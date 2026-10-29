"""M8 分析智能体验收（ADR 0007）：合成电商数据集（scripts/ec_data.py，已灌入演示 StarRocks）。

确定性部分不依赖模型：直接驱动工具箱，断言受治理查询的值与生成数据集的标准答案（.demo/ec-truth.json）一致，
计算工具与数字核验的结果正确，越权与预算被拒绝；导出、任务暂停、定时入队走 HTTP API 与 worker 函数。
真实模型部分（llm）：对预埋了原因的问题发起分析，断言报告找到原因、核验通过、每一步都经过治理。
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest

from tests.conftest import Api

pytestmark = pytest.mark.acceptance
ctx: dict = {}
AUG, SEP = ("2026-08-01", "2026-08-31"), ("2026-09-01", "2026-09-30")


def truth() -> dict:
    import demo
    import ec_data

    path = demo.STATE / "ec-truth.json"
    if not path.exists():
        path.write_text(json.dumps(ec_data.generate().truth, ensure_ascii=False), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


def toolbox(username: str, budget=None):
    """以某个用户身份建一个分析运行与工具箱（不经过模型）。"""
    from avelis.analysis.tools import AnalysisToolbox, Budget
    from avelis.ask import service
    from avelis.db import session_scope
    from avelis.models import AnalysisRun, User
    from sqlalchemy import select

    scope = session_scope()
    session = scope.__enter__()
    user = session.scalar(select(User).where(User.username == username))
    run = AnalysisRun(user_id=user.id, question="验收：工具箱", trigger="manual", status="running")
    session.add(run)
    session.flush()
    box = AnalysisToolbox(session, user, run, service.visible_catalog(session, user, with_values=False),
                          budget or Budget())
    return scope, session, run, box


def rows(ds_json: str) -> list[dict]:
    view = json.loads(ds_json)
    assert "error" not in view, view
    return [dict(zip(view["columns"], r)) for r in view["rows"]]


def test_00_setup_ec_domain_and_metrics(stack, admin: Api):
    import demo
    import ec_data
    from avelis import demo_setup

    client, env, _ = stack
    conn = demo.sr_root(env)
    try:
        assert ec_data.loaded_checksum(conn), "电商数据集未灌入：先运行 uv run scripts/ec_data.py load"
    finally:
        conn.close()
    demo_setup.setup(reset=False, metrics=True, ec=True)
    pw = env["AVELIS_DEMO_USER_PASSWORD"]
    ctx["apis"] = {u: Api(client, u, pw) for u in ("zhang", "li", "wang", "sun")}
    metrics = {m["code"]: m for m in admin.get("/api/metrics")}
    for code in ("ec_gmv", "ec_orders", "ec_aov", "ec_store_sales", "ec_visitors", "ec_conversion", "ec_roi",
                 "ec_delivery_hours", "ec_satisfaction", "ec_category_sales", "ec_refund"):
        assert metrics[code]["versions"][0]["status"] == "published", (code, metrics[code])
    ds = next(d for d in admin.get("/api/datasources") if d["name"] == demo_setup.EC_SOURCE)
    assets = admin.get("/api/assets", params={"datasource_id": ds["id"]})
    assert len(assets) == len(ec_data.TABLES) + len(ec_data.VIEWS)
    domain = next(d for d in admin.get("/api/domains") if d["code"] == "ec")
    assert all(a["domain_id"] == domain["id"] for a in assets)
    ctx["truth"] = truth()


def test_01_governed_query_matches_generated_truth_and_contribution(admin: Api):
    scope, session, run, box = toolbox("wang")
    try:
        d1 = rows(box.query_metric(metric_codes=["ec_gmv"], start_date=SEP[0], end_date=SEP[1], group_by=["region"],
                                   filters=[{"dimension": "channel", "values": ["门店"]}], compare="custom",
                                   compare_start=AUG[0], compare_end=AUG[1], purpose="各区域门店成交额 9 月对比自然月 8 月"))
        by_region = {r["region"]: r for r in d1}
        expected = ctx["truth"]["region_channel_month"]
        assert Decimal(str(by_region["华南"]["成交额"])) == Decimal(expected[str(("SC", "门店", "2026-09"))]["paid_amount"])
        assert Decimal(str(by_region["华南"]["成交额·对比期"])) == Decimal(
            expected[str(("SC", "门店", "2026-08"))]["paid_amount"])  # 自定义对比期：自然月 8 月
        # 差额拆解的各区域变化相加等于总变化
        contribution = json.loads(box.contribution(dataset="D1", keys=["region"], current="成交额",
                                                   previous="成交额·对比期"))
        changes = [Decimal(str(r[contribution["columns"].index("change")])) for r in contribution["rows"]]
        assert len(changes) == 4 and sum(changes) == Decimal(str(contribution["summary"]["total"]["change"]))
        # 门店维度下钻：深圳福田中心店 9 月 10 日关店，是华南门店成交额下降最多的门店
        rows(box.query_metric(metric_codes=["ec_store_sales"], start_date=SEP[0], end_date=SEP[1],
                                   group_by=["store"], filters=[{"dimension": "region", "values": ["华南"]}],
                                   compare="previous_period", purpose="华南各门店 9 月对比上一期"))
        drill = rows(box.contribution(dataset="D3", keys=["store"], current="门店成交额", previous="门店成交额·上一期"))
        assert drill[0]["store"] == "深圳福田中心店" and Decimal(str(drill[0]["change"])) < 0
        ctx["contribution_ds"] = {"rows": drill}
    finally:
        scope.__exit__(None, None, None)


def test_02_factor_decomposition_and_daily_gap(admin: Api):
    scope, session, run, box = toolbox("wang")
    try:
        rows(box.query_metric(metric_codes=["ec_gmv", "ec_orders", "ec_aov"], start_date=SEP[0], end_date=SEP[1],
                              filters=[{"dimension": "region", "values": ["华东"]}, {"dimension": "channel",
                                                                                   "values": ["线上"]}],
                              compare="previous_period", purpose="华东线上成交额 = 订单数 × 客单价"))
        out = json.loads(box.factor_decompose(dataset="D1", total={"name": "成交额", "current": "成交额",
                                                                   "previous": "成交额·上一期"},
                                              factors=[{"name": "订单数", "current": "订单数", "previous": "订单数·上一期"},
                                                       {"name": "客单价", "current": "客单价", "previous": "客单价·上一期"}]))
        assert "error" not in out, out
        parts = {r[0]: r for r in out["rows"]}
        change = Decimal(str(out["summary"]["total"]["change"]))
        assert sum(Decimal(str(r[4])) for r in out["rows"]) == change  # 各因子贡献相加正好等于总差额
        assert Decimal(str(parts["订单数"][4])) < 0  # 抖音投放削减：订单数下降是主因
        # 按日看门店订单：北京 3 家店 9 月 28 日客流计数器故障，门店日视图当天缺这 3 家店
        daily = rows(box.query_metric(metric_codes=["ec_store_orders"], start_date="2026-09-27", end_date="2026-09-28",
                                      group_by=["store"], filters=[{"dimension": "city", "values": ["北京"]}],
                                      time_grain="day", purpose="北京门店按日"))
        per_day = {d: len([r for r in daily if r["period"] == d]) for d in ("2026-09-27", "2026-09-28")}
        assert per_day == {"2026-09-27": 5, "2026-09-28": 2}
        cover = json.loads(box.coverage(dataset="D3", keys=["store"], start_date="2026-09-27", end_date="2026-09-28"))
        assert cover["summary"]["members_with_gaps"] == 3 and cover["summary"]["missing_total"] == 3
        # 同一天按订单为主表的口径不缺：数据缺口来自客流表（靠血缘定位）
        lineage = json.loads(box.lineage(qualified="default_catalog.ec_dw.dws_store_daily"))
        sources = {e["source"] for e in lineage["edges"]}
        assert "default_catalog.ec_org.store_daily_traffic" in sources
    finally:
        scope.__exit__(None, None, None)


def test_03_permission_budget_and_verification(admin: Api):
    from avelis.analysis.tools import Budget
    from avelis.analysis.verify import verify_report

    scope, session, run, box = toolbox("sun")  # 孙悦不在电商经营域
    try:
        denied = json.loads(box.query_metric(metric_codes=["ec_gmv"], start_date=SEP[0], end_date=SEP[1],
                                             purpose="越权"))
        assert denied["status"] == "denied" and not box.datasets()
        assert "ec_gmv" not in {m["code"] for m in json.loads(box.search_metrics())["metrics"]}
        assert "error" in json.loads(box.describe_asset(qualified="default_catalog.ec_dw.dws_region_daily"))
    finally:
        scope.__exit__(None, None, None)
    scope, session, run, box = toolbox("wang", Budget(tool_calls=3, queries=1))
    try:
        rows(box.query_metric(metric_codes=["ec_refund"], start_date=SEP[0], end_date=SEP[1], group_by=["region"],
                              purpose="退款"))
        again = json.loads(box.query_metric(metric_codes=["ec_refund"], start_date=AUG[0], end_date=AUG[1],
                                            purpose="超出查询预算"))
        assert "上限" in again["error"]
        box.search_metrics()
        over = json.loads(box.search_metrics())
        assert "上限" in over["error"]  # 工具次数用完
        d1 = {r["region"]: r["退款金额"] for r in rows(json.dumps(box._view("D1", box.datasets()["D1"]),
                                                                ensure_ascii=False, default=str))}
        report = verify_report([
            {"type": "fact", "text": f"华南 9 月退款 {d1['华南']}", "refs": ["D1"]},
            {"type": "fact", "text": "华南 9 月退款 1.00", "refs": ["D1"]}],
            {"D1": {"rows": [dict(zip(box.datasets()["D1"].columns, r)) for r in box.datasets()["D1"].rows]}})
        assert [c["status"] for c in report["claims"]] == ["verified", "unverified"]
    finally:
        scope.__exit__(None, None, None)


def test_04_api_requires_model_and_result_policy(admin: Api):
    wang = ctx["apis"]["wang"]
    refused = wang.post("/api/analysis/runs", {"question": "为什么 9 月华南门店成交额下降？"}, expect=409)
    assert "支持工具调用的模型" in refused["detail"]
    admin.put("/api/model-settings", {"policy": {"send_results": False, "send_code": True}})
    off = wang.post("/api/analysis/runs", {"question": "为什么 9 月华南门店成交额下降？"}, expect=409)
    assert "需保密请接入本地模型" in off["detail"]
    admin.put("/api/model-settings", {"policy": {"send_results": True, "send_code": True}})


def test_05_export_and_dataset_csv(admin: Api):
    """用确定性工具箱产出的运行验证导出：PDF / Markdown / CSV 与报告一致，他人不可见。"""
    from avelis.analysis.verify import verify_report

    scope, session, run, box = toolbox("wang")
    try:
        d = rows(box.query_metric(metric_codes=["ec_visitors"], start_date=SEP[0], end_date=SEP[1],
                                  group_by=["district"], purpose="各商圈等级客流"))
        top = max(d, key=lambda r: Decimal(str(r["进店客流"])))
        run.report = {"title": "商圈等级与客流", "summary": "核心商圈客流最高。", "claims": [
            {"type": "fact", "text": f"{top['district']} 9 月客流 {top['进店客流']}", "refs": ["D1"]},
            {"type": "fact", "text": "社区店客流 1", "refs": ["D1"]}]}
        ds = box.datasets()["D1"]
        run.verification = verify_report(run.report["claims"], {"D1": {"rows": [dict(zip(ds.columns, r)) for r in ds.rows]}})
        run.status = "succeeded"
        run_id = run.id
    finally:
        scope.__exit__(None, None, None)
    wang, li = ctx["apis"]["wang"], ctx["apis"]["li"]
    view = wang.get(f"/api/analysis/runs/{run_id}")
    assert view["verification"]["unverified"] == 1 and view["datasets"][0]["ref"] == "D1"
    md = wang.client.get(f"/api/analysis/runs/{run_id}/export", params={"format": "md"}, headers=wang.headers)
    assert md.status_code == 200 and "未核实的数字：1" in md.text and "## 附录：数据集" in md.text
    pdf = wang.client.get(f"/api/analysis/runs/{run_id}/export", params={"format": "pdf"}, headers=wang.headers)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and len(pdf.content) > 5000
    csv = wang.client.get(f"/api/analysis/runs/{run_id}/datasets/1/csv", headers=wang.headers)
    assert csv.content.startswith(b"\xef\xbb\xbf") and "进店客流" in csv.content.decode("utf-8-sig")
    li.get(f"/api/analysis/runs/{run_id}", expect=404)  # 只有发起人与管理员可见


def test_06_tasks_pause_and_schedule(admin: Api):
    from avelis.analysis import service as analysis_service
    from avelis.db import session_scope, utcnow
    from avelis.models import AnalysisTask, Job

    wang = ctx["apis"]["wang"]
    task = wang.post("/api/analysis/tasks", {"title": "月度门店成交额归因", "question": "上个月门店成交额为什么变化？",
                                             "window": {"kind": "last_month"}, "schedule_cron": "0 9 1 * *"},
                     expect=201)
    assert task["status"] == "active" and task["next_run_at"]
    wang.post("/api/analysis/tasks", {"question": "x", "schedule_cron": "61 9 * * *"}, expect=422)
    # 未配置分析模型：手动运行时任务暂停并通知（不静默跳过）
    paused = wang.post(f"/api/analysis/tasks/{task['id']}/run", expect=409)
    assert "暂停" in paused["detail"]
    assert wang.get("/api/analysis/tasks")[0]["status"] == "paused"
    note = wang.get("/api/notifications", params={"unread": True})[0]
    assert note["kind"] == "analysis_paused" and "分析模式需要" in note["body"]
    assert wang.client.post(f"/api/notifications/{note['id']}/read", headers=wang.headers).status_code == 204
    assert not wang.get("/api/notifications", params={"unread": True})
    # 恢复后到期：worker 的定时扫描入队一次分析任务，重复扫描不重复入队
    wang.patch(f"/api/analysis/tasks/{task['id']}", {"status": "active"})
    with session_scope() as session:
        session.get(AnalysisTask, task["id"]).next_run_at = utcnow() - timedelta(minutes=1)
    first, second = analysis_service.schedule_due(), analysis_service.schedule_due()
    assert len(first) == 1 and second == []
    with session_scope() as session:
        job = session.get(Job, first[0])
        assert job.kind == "analysis" and job.payload == {"task_id": task["id"], "trigger": "schedule"}
        assert session.get(AnalysisTask, task["id"]).next_run_at.replace(tzinfo=None) > utcnow()
        job.status = "failed"  # 不在验收中真的运行（无模型）


def assert_grounded(run: dict) -> None:
    """真实模型的报告：事实与计算类结论至少 80% 核验通过；未核实的每一条都列出了数字，并在导出中同样标出。
    （模型偶尔会写进自己推算的计数，核验如实标出；确定性部分的核验断言见 test_03、test_05。）"""
    checks = [c for c in run["verification"]["claims"] if c["type"] in ("fact", "calc")]
    assert checks, run["report"]
    bad = [c for c in checks if c["status"] == "unverified"]
    assert len(bad) <= len(checks) * 0.2, run["verification"]
    for c in bad:
        assert c["numbers"] and any(not n["verified"] for n in c["numbers"]) or c["missing_refs"], c


@pytest.mark.llm
def test_07_real_model_finds_planted_cause(stack, admin: Api):
    """真实模型（.env 中的 DeepSeek）：为什么 9 月华南门店成交额比 8 月低？预埋原因：深圳福田中心店 9 月 10 日关店。"""
    from avelis.bootstrap import model_from_env

    model_from_env()
    wang = ctx["apis"]["wang"]
    run = wang.post("/api/analysis/runs?wait=true", {
        "question": "为什么 2026 年 9 月华南区域门店成交额比 8 月低？原因是什么？",
        "window": {"kind": "fixed", "start": SEP[0], "end": SEP[1], "compare_start": AUG[0],
                   "compare_end": AUG[1]}}, expect=201)
    assert run["status"] == "succeeded", run.get("error")
    report_text = json.dumps(run["report"], ensure_ascii=False)
    assert "福田" in report_text, run["report"]
    assert_grounded(run)
    governed = [d for d in run["datasets"] if d["kind"] == "governed"]
    assert governed and all(d["sql"] for d in governed)
    used = run["budget"]["used"]
    assert used["queries"] <= run["budget"]["limits"]["queries"] and used["tool_calls"] <= 30
    assert run["steps"][0]["tool"] in ("submit_plan", "search_metrics", "describe_metric", "changes")
    ctx["llm_run"] = run["id"]


@pytest.mark.llm
def test_08_real_model_online_orders_cause(admin: Api):
    """华东 9 月线上订单为什么下降？预埋原因：抖音投放 9 月起削减 60%。"""
    run = ctx["apis"]["wang"].post("/api/analysis/runs?wait=true", {
        "question": "华东区域 2026 年 9 月线上订单数为什么比 8 月少？",
        "window": {"kind": "fixed", "start": SEP[0], "end": SEP[1], "compare_start": AUG[0],
                   "compare_end": AUG[1]}}, expect=201)
    assert run["status"] == "succeeded", run.get("error")
    assert "抖音" in json.dumps(run["report"], ensure_ascii=False), run["report"]
    assert_grounded(run)
    pdf = ctx["apis"]["wang"].client.get(f"/api/analysis/runs/{run['id']}/export", params={"format": "pdf"},
                                         headers=ctx["apis"]["wang"].headers)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    if not run["verification"]["ok"]:
        md = ctx["apis"]["wang"].client.get(f"/api/analysis/runs/{run['id']}/export", params={"format": "md"},
                                            headers=ctx["apis"]["wang"].headers)
        assert "未核实的数字" in md.text  # 导出与报告页一致：未核实的数字同样标出
