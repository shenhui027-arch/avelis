"""E1 注入框架（experiments/agent-v2/e1.py）：用假模型跑通 D0–D3 四种条件，核对注入只在实验进程里生效、
注入不占用分析额度、注入的字符数单独记录，退出后产品流程恢复原样。"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

from avelis.analysis import agent
from avelis.analysis.tools import AnalysisToolbox, Budget
from tests.unit.test_analysis_pipeline import WINDOW, Phased, catalog, proposal_reply
from tests.unit.test_analysis_toolbox import governed, make_box

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("e1", ROOT / "experiments" / "agent-v2" / "e1.py")
e1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e1)
cost_spec = importlib.util.spec_from_file_location("e1_cost", ROOT / "experiments" / "agent-v2" / "cost.py")
cost = importlib.util.module_from_spec(cost_spec)
cost_spec.loader.exec_module(cost)

MATERIAL = {
    "scope": {"question_type": "change", "metric_codes": ["m_b"], "filters": [{"dimension": "地区", "values": ["甲"]}],
              "start_date": WINDOW["start"], "end_date": WINDOW["end"], "compare_start": WINDOW["compare_start"],
              "compare_end": WINDOW["compare_end"]},
    "assets": [{"qualified": "c.db.visible", "why": "来源"}, {"qualified": "c.db.hidden", "why": "无权的表"}],
    "evidence": [{"tool": "query_metric", "args": {"metric_codes": ["m_b"], "start_date": WINDOW["start"],
                                                   "end_date": WINDOW["end"], "purpose": "实验注入"}}],
}


def run(condition, monkeypatch):
    box = make_box(budget=Budget(), catalog=catalog(), monkeypatch=monkeypatch)
    box.run.window, box.run.question = dict(WINDOW), "地区甲的指标乙为什么下降？"
    monkeypatch.setattr(agent.service, "version_sources", lambda session, version: {})  # 来源由本测试的元数据替身提供。
    prechecked, seen = [], {}
    monkeypatch.setattr(box, "precheck", lambda scope: prechecked.append(scope.metric_codes) or [])

    def describe_asset(qualified):
        if qualified.endswith("hidden"):
            return json.dumps({"error": "资产不存在或当前用户不可见"})
        return json.dumps({"kind": "table", "comment": "可见表", "columns": [
            {"name": "amount", "type": "decimal", "doc": {"comment": "金额"}}]})

    def query_metric(**args):
        box.budget.used_queries += 1  # 真实查询会计数：注入之后要恢复
        ds = governed(box, ["指标乙"], [["42"]])
        return json.dumps({"dataset": f"D{ds.seq}"})
    monkeypatch.setattr(box, "describe_asset", describe_asset)
    monkeypatch.setattr(box, "query_metric", query_metric)

    def act(state, config):
        seen["user"] = state["messages"][1].content
        box.report = {"title": "中性结果", "summary": "证据不足", "claims": []}
        return {"messages": [], "phase": "verify", "rounds": 1, "notes": []}
    monkeypatch.setattr(agent, "act", act)
    monkeypatch.setattr(agent, "verify", lambda state, config: {"phase": "done"})
    model = Phased([AIMessage(content="", tool_calls=[{"name": "submit_scope", "id": "s1", "args": {
        "question_type": "change", "metric_codes": ["m_a"], "clarification": "是指标甲还是指标乙？"}}]),
        proposal_reply()])
    holder = {}
    with e1.install(condition, MATERIAL, rebuild=lambda: holder.update(graph=agent.build_graph(MemorySaver()))) as stats:
        config = {"configurable": {"thread_id": condition, "toolbox": box, "model": model}}
        holder["graph"].invoke(agent.initial_state(box.run.question, "2026-10-30", "UTC", WINDOW), config)
        state = holder["graph"].get_state(config).values
    return box, state, stats, seen, prechecked, model


def injected(box):
    return [s for s in box.session.added if getattr(s, "kind", None) == "inject"]


def test_d0_is_the_unmodified_product_flow(monkeypatch):
    box, state, stats, seen, prechecked, model = run("D0", monkeypatch)
    assert state["phase"] == "clarify" and prechecked == [] and injected(box) == []  # B0：带追问时停在待澄清
    assert stats["injected_chars"] == 0 and len(model.seen) == 1


def test_d1_replaces_only_the_scope(monkeypatch):
    box, state, stats, seen, prechecked, model = run("D1", monkeypatch)
    assert len(model.seen) == 2  # 理解与假设提议各请求一次模型，实验只替换范围。
    assert model.tool_choice == ["submit_scope", "submit_proposal"]
    assert prechecked == [["m_b"]] and box.plan["model_scope"]["metric_codes"] == ["m_a"] and box.clarification is None
    assert [s.tool for s in injected(box)] == ["scope"] and "实验提供的相关资产" not in seen["user"]
    assert box.datasets() == {}


def test_d2_adds_accessible_metadata_and_counts_its_characters(monkeypatch):
    box, state, stats, seen, prechecked, model = run("D2", monkeypatch)
    assert "实验提供的相关资产" in seen["user"] and "c.db.visible" in seen["user"] and "金额" in seen["user"]
    assert "c.db.hidden" not in seen["user"] and any("c.db.hidden" in f for f in stats["failed"])  # 无权的不注入
    meta = [s for s in injected(box) if s.tool == "metadata"][0]
    assert meta.output["chars"] > 0 and stats["injected_chars"] == meta.output["chars"]
    assert box.datasets() == {}  # D2 不给查询结果


def test_d3_adds_raw_query_evidence_without_spending_the_analysis_budget(monkeypatch):
    box, state, stats, seen, prechecked, model = run("D3", monkeypatch)
    assert stats["evidence"] == ["D1"] and "D1" in seen["user"]
    assert box.budget.used_queries == 0  # 注入的查询不占用分析额度
    evidence = [s for s in injected(box) if s.tool == "evidence"][0]
    assert evidence.output["budget_used_by_injection"]["used_queries"] == 1
    meta = [s for s in injected(box) if s.tool == "metadata"][0]
    assert meta.output["evidence_chars"] > 0 and stats["injected_chars"] == meta.output["chars"] + meta.output["evidence_chars"]
    assert meta.output["evidence_omitted"] == stats["evidence_omitted"] == []


def test_d3_counts_only_evidence_blocks_that_fit_in_the_context_pack(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    kept = governed(box, ["指标乙"], [["42"]])
    omitted = governed(box, ["指标乙"], [["省略的证据" * 100]])
    kept_ref, omitted_ref = f"D{kept.seq}", f"D{omitted.seq}"
    kept_block = box._pack_block(kept_ref, kept)
    omitted_block = box._pack_block(omitted_ref, omitted)
    box.injected_refs = [kept_ref, omitted_ref]
    monkeypatch.setattr(box, "PACK_CHARS", len(kept_block) + 400)  # 正好只够第一块与省略标记

    with e1.install("D3", {"assets": []}, rebuild=lambda: None) as stats:
        pack = box.context_pack()

    assert kept_block in pack and omitted_block not in pack
    assert "其余 1 项放不下" in pack and omitted_ref in pack
    meta = [s for s in injected(box) if s.tool == "metadata"][0]
    assert meta.output["evidence_chars"] == len(kept_block)
    assert stats["injected_chars"] == len(kept_block)
    assert meta.output["evidence_omitted"] == stats["evidence_omitted"] == [omitted_ref]


def test_install_restores_the_product_functions(monkeypatch):
    before = (agent.understand, agent.precheck, AnalysisToolbox.context_pack)
    run("D3", monkeypatch)
    assert (agent.understand, agent.precheck, AnalysisToolbox.context_pack) == before


def test_plan_is_48_runs_in_a_fixed_shuffled_order():
    material = e1.load_material()
    assert list(material) == ["h10", "e01", "h07", "f02", "h09", "d02"]
    order = e1.plan(list(material), list(e1.CONDITIONS), 2)
    assert len(order) == 48 and order == e1.plan(list(material), list(e1.CONDITIONS), 2)
    assert {(o["scenario"], o["condition"]) for o in order if o["repeat"] == 1} == {
        (s, c) for s in material for c in e1.CONDITIONS}
    bounds = e1.budget_bounds(len(order))
    assert bounds["model_requests"] == 48 * 16 and bounds["seconds_wall"] == 48 * 300
    for item in material.values():  # 材料不含报告答案：只有范围、资产定位与查询
        assert set(item) <= {"question", "scope", "assets", "evidence"}
        assert all(set(e) == {"tool", "args"} and e["tool"] in ("query_metric", "query_sql") for e in item.get("evidence", []))


@pytest.mark.parametrize(("at", "expected"), [
    ("2026-10-30T02:00:00+00:00", "peak"),  # 转北京时间为周五 10:00
    ("2026-10-31T10:00:00+08:00", "off_peak"),
    ("2026-10-30T12:30:00+08:00", "off_peak"),
    ("2026-10-30T09:00:00+08:00", "peak"),
    ("2026-10-30T12:00:00+08:00", "off_peak"),
    ("2026-10-30T14:00:00+08:00", "peak"),
    ("2026-10-30T18:00:00+08:00", "off_peak"),
])
def test_cost_price_period_uses_beijing_workday_hours(at, expected):
    assert cost.price_period(datetime.fromisoformat(at)) == expected


def test_cost_price_period_requires_timezone():
    with pytest.raises(ValueError, match="时区"):
        cost.price_period(datetime(2026, 10, 30, 10))


def test_request_cost_counts_cache_misses_and_output_without_double_counting_reasoning():
    usage = {"input_tokens": 1_000_000, "cache_hit_tokens": 250_000,
             "output_tokens": 100_000, "reasoning_tokens": 60_000}
    assert cost.request_cost(usage, datetime.fromisoformat("2026-10-30T10:00:00+08:00")) == Decimal("2.31")
    assert cost.request_cost(usage, datetime.fromisoformat("2026-10-31T10:00:00+08:00")) == Decimal("1.155")
    assert cost.request_cost({**usage, "cache_hit_tokens": None},
                             datetime.fromisoformat("2026-10-30T10:00:00+08:00")) == Decimal("2.8")


def test_run_cost_includes_error_usage_and_counts_missing_usage():
    result = cost.run_cost({"steps": [
        {"kind": "model", "status": "ok", "at": "2026-10-30T10:00:00+08:00",
         "output": {"input_tokens": 1_000_000, "cache_hit_tokens": 250_000, "output_tokens": 100_000}},
        {"kind": "model", "status": "error", "at": "2026-10-31T10:00:00+08:00",
         "output": {"input_tokens": 500_000, "cache_hit_tokens": None, "output_tokens": 50_000,
                    "error_code": "model_truncated"}},
        {"kind": "model", "status": "error", "output": {"error": "连接中断"}},
        {"kind": "model", "status": "ok", "output": {"input_tokens": None, "output_tokens": None}},
        {"kind": "tool", "output": {"input_tokens": 999_999}},
    ]})
    assert result == {"input_tokens": 1_500_000, "cache_hit_tokens": 250_000,
                      "cache_miss_tokens": 1_250_000, "output_tokens": 150_000, "missing_usage": 2,
                      "peak_cny": "2.31", "off_peak_cny": "0.70", "total_cny": "3.01"}
    assert cost.run_cost({})["total_cny"] == "0"
    json.dumps(result)


@pytest.mark.parametrize(("usage", "input_tokens", "output_tokens", "expected_cost"), [
    ({"input_tokens": 300_000}, 300_000, 0, "0.6"),
    ({"input_tokens": None, "output_tokens": 100_000}, 0, 100_000, "0.8"),
])
def test_run_cost_counts_partial_usage_as_missing_and_keeps_known_cost(usage, input_tokens, output_tokens, expected_cost):
    result = cost.run_cost({"steps": [{"kind": "model", "status": "ok", "at": "2026-10-30T10:00:00+08:00",
                                      "output": usage}]})
    assert result["missing_usage"] == 1
    assert result["input_tokens"] == result["cache_miss_tokens"] == input_tokens
    assert result["output_tokens"] == output_tokens
    assert Decimal(result["total_cny"]) == Decimal(expected_cost)


@pytest.mark.parametrize("error_code", ["budget_tokens", "budget_output_tokens"])
def test_run_cost_does_not_charge_or_reserve_for_skipped_budget_requests(error_code):
    result = cost.run_cost({"steps": [{"kind": "model", "status": "skipped", "output": {"error_code": error_code}}]})
    assert result == {"input_tokens": 0, "cache_hit_tokens": 0, "cache_miss_tokens": 0, "output_tokens": 0,
                      "missing_usage": 0, "peak_cny": "0", "off_peak_cny": "0", "total_cny": "0"}


def test_ledger_refuses_to_start_when_the_next_reserve_does_not_fit():
    ledger = cost.Ledger("30", "28.5", per_run_reserve_cny="1.5")
    assert ledger.can_start()  # 刚好留够一次运行的预留时可以开始
    ledger.add(Decimal("0.01"))
    assert not ledger.can_start()
    assert ledger.view() == {"cap_cny": "30", "spent_cny": "28.51", "per_run_reserve_cny": "1.5",
                             "remaining_cny": "1.49", "can_start": False}
    json.dumps(ledger.view())


@pytest.mark.parametrize("amount", ["-1", "NaN", "Infinity"])
def test_ledger_rejects_invalid_amounts(amount):
    with pytest.raises(ValueError, match="非负有限数"):
        cost.Ledger("30", per_run_reserve_cny=amount)
    ledger = cost.Ledger("30", per_run_reserve_cny="1.5")
    with pytest.raises(ValueError, match="非负有限数"):
        ledger.add(amount)


COSTED_STEP = {"kind": "model", "status": "ok", "at": "2026-10-30T10:00:00+08:00",
               "output": {"input_tokens": 300_000, "cache_hit_tokens": 0, "output_tokens": 0}}
MISSING_USAGE_STEP = {"kind": "model", "status": "error", "output": {"error": "连接中断"}}
COSTED_RUN = {"id": "unit-test-run", "status": "succeeded", "steps": [COSTED_STEP]}


@pytest.mark.parametrize(("spent", "run", "expected_runs", "known_cost", "accounted_cost", "reason"), [
    pytest.param("0", COSTED_RUN, 1, "0.6", "0.6", None, id="complete-usage"),
    pytest.param("1", COSTED_RUN, 0, "0", "0", None, id="initial-reserve-does-not-fit"),
    pytest.param("0", None, 1, "0", "1.5", "未取得运行详情", id="missing-run"),
    pytest.param("0", {}, 1, "0", "1.5", "未取得运行详情", id="empty-run"),
    pytest.param("0", {"steps": [COSTED_STEP]}, 1, "0.6", "2.1", "运行详情缺少 id", id="missing-id"),
    pytest.param("0", {"id": "unit-test-run"}, 1, "0", "1.5", "运行详情缺少步骤记录", id="missing-steps"),
    pytest.param("0", {"id": "unit-test-run", "steps": None}, 1, "0", "1.5", "运行详情缺少步骤记录",
                 id="none-steps"),
    pytest.param("0", {"id": "unit-test-run", "steps": []}, 1, "0", "1.5", "运行详情缺少步骤记录",
                 id="empty-steps"),
    pytest.param("0", {**COSTED_RUN, "steps": [COSTED_STEP, MISSING_USAGE_STEP]}, 1, "0.6", "2.1",
                 "1 个模型步骤缺少用量", id="partial-usage"),
    pytest.param("0", {**COSTED_RUN, "steps": [{**COSTED_STEP, "output": {"input_tokens": 300_000}}]},
                 1, "0.6", "2.1", "1 个模型步骤缺少用量", id="missing-output-usage"),
    pytest.param("0", {**COSTED_RUN, "steps": [{**COSTED_STEP, "output": {"output_tokens": 100_000}}]},
                 1, "0.8", "2.3", "1 个模型步骤缺少用量", id="missing-input-usage"),
    pytest.param("0", {**COSTED_RUN, "steps": [COSTED_STEP,
                 {"kind": "model", "status": "skipped", "output": {"error_code": "budget_tokens"}}]},
                 1, "0.6", "0.6", None, id="skipped-input-budget"),
    pytest.param("0", {**COSTED_RUN, "steps": [COSTED_STEP,
                 {"kind": "model", "status": "skipped", "output": {"error_code": "budget_output_tokens"}}]},
                 1, "0.6", "0.6", None, id="skipped-output-budget"),
    pytest.param("0", {"steps": [COSTED_STEP, MISSING_USAGE_STEP]}, 1, "0.6", "2.1",
                 "运行详情缺少 id；1 个模型步骤缺少用量", id="reserve-added-once"),
])
def test_e1_run_stops_before_spending_the_next_reserve_and_saves_cost(monkeypatch, tmp_path, capsys,
                                                                    spent, run, expected_runs, known_cost,
                                                                    accounted_cost, reason):
    import tests.eval as eval_package

    calls = {"load_env": 0, "client": 0, "ask": 0}

    def load_env():
        calls["load_env"] += 1
        return {"AVELIS_DEMO_USER_PASSWORD": "unit-test-key"}

    def client(env):
        calls["client"] += 1
        return object()

    def ask(client, headers, question):
        calls["ask"] += 1
        return run

    monkeypatch.setitem(sys.modules, "demo", SimpleNamespace(STATE=tmp_path, load_env=load_env))
    monkeypatch.setitem(sys.modules, "eval_analysis", SimpleNamespace(
        _ask=ask, _client=client, _login=lambda *args: {}, code_version=lambda: {"commit": "unit-test"}))
    monkeypatch.setattr(eval_package, "scoring_v2", SimpleNamespace(
        VERSION="unit-test", load_questions=lambda: {"questions": [{"id": "one", "user": "unit-test-user"}]},
        score=lambda *args: {"passed": True}), raising=False)
    monkeypatch.setattr(e1, "load_material", lambda: {"one": {}})
    monkeypatch.setattr(e1, "install", lambda *args: contextlib.nullcontext({"condition": args[0]}))
    monkeypatch.setattr(sys, "argv", ["e1.py", "--run", "--conditions", "D0,D1", "--repeat", "1",
                                      "--cap", "2", "--spent", spent, "--reserve", "1.5"])

    assert e1.main() == 0
    assert calls == {"load_env": expected_runs, "client": expected_runs, "ask": expected_runs}
    paths = list((tmp_path / "experiments" / "e1").glob("*.json"))
    assert len(paths) == 1
    data = json.loads(paths[0].read_text(encoding="utf-8"))
    assert len(data["results"]) == expected_runs
    output = capsys.readouterr().out
    assert "费用预留不足" in data["stop_reason"] and "费用预留不足" in output
    assert Decimal(data["ledger"]["spent_cny"]) == Decimal(spent) + Decimal(accounted_cost)
    if expected_runs:
        result = data["results"][0]
        assert Decimal(result["cost"]["total_cny"]) == Decimal(known_cost)
        assert Decimal(result["cost_accounted_cny"]) == Decimal(accounted_cost)
        assert result["cost_estimated"] is bool(reason)
        assert result["cost_estimate_reason"] == reason
        assert f"本次入账 {result['cost_accounted_cny']} 元" in output
        if reason:
            assert f"估算原因：{reason}" in output
        else:
            assert "估算原因" not in output
        assert result["ledger"] == data["ledger"]
