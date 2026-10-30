"""E1 注入框架（experiments/agent-v2/e1.py）：用假模型跑通 D0–D3 四种条件，核对注入只在实验进程里生效、
注入不占用分析额度、注入的字符数单独记录，退出后产品流程恢复原样。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

from avelis.analysis import agent
from avelis.analysis.tools import AnalysisToolbox, Budget
from tests.unit.test_analysis_pipeline import WINDOW, Phased, catalog
from tests.unit.test_analysis_toolbox import governed, make_box

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("e1", ROOT / "experiments" / "agent-v2" / "e1.py")
e1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e1)

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
        return {"messages": [], "phase": "verify", "rounds": 1, "notes": []}
    monkeypatch.setattr(agent, "act", act)
    monkeypatch.setattr(agent, "verify", lambda state, config: {"phase": "done"})
    model = Phased([AIMessage(content="", tool_calls=[{"name": "submit_scope", "id": "s1", "args": {
        "question_type": "change", "metric_codes": ["m_a"], "clarification": "是指标甲还是指标乙？"}}])])
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
    assert len(model.seen) == 1  # 理解阶段照常请求一次模型
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
    assert bounds["model_requests"] == 48 * 16 and bounds["input_tokens"] == 48 * 160_000
    for item in material.values():  # 材料不含报告答案：只有范围、资产定位与查询
        assert set(item) <= {"question", "scope", "assets", "evidence"}
        assert all(set(e) == {"tool", "args"} and e["tool"] in ("query_metric", "query_sql") for e in item.get("evidence", []))
