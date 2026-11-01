"""真实 SQLite 验证续问摘要、证据复制与实际模型输入。"""

import json
from copy import deepcopy
from datetime import timedelta

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import select

from avelis.analysis import agent
from avelis.analysis import conversation_context as context
from avelis.analysis.dataset_projection import RESTRICTED_TEXT
from avelis.analysis.tools import AnalysisToolbox, Budget
from avelis.analysis.verify import ground_report
from avelis.ask import service as ask
from avelis.models import AnalysisDataset, AnalysisRun

from .test_analysis_dataset_access import dataset_db  # noqa: F401  pytest 夹具按参数名注入


def child(session, parent, id_="child"):
    run = AnalysisRun(
        id=id_,
        parent_run_id=parent.id,
        conversation_id=parent.conversation_id,
        user_id=parent.user_id,
        question="继续核对未明部分",
        trigger="manual",
        status="queued",
    )
    session.add(run)
    session.flush()
    return run


def saved_report(run, dataset):
    dataset.columns = ["member", "value"]
    dataset.rows = [["成员甲", 7]]
    run.report, run.verification = ground_report(
        {
            "title": ["原报告"],
            "summary": ["上一轮发现集中变化"],
            "unexplained": ["还没查清时间差异"],
            "claims": [{"type": "fact", "parts": ["关键值：", {"dataset": "D1", "column": "value"}], "refs": ["D1"]}],
            "causes": [
                {
                    "type": "member",
                    "entity": {"evidence_id": "D1#1"},
                    "direction": "up",
                    "contribution": {"dataset": "D1", "column": "value"},
                    "evidence": ["D1"],
                }
            ],
        },
        {
            "D1": {
                "kind": "governed",
                "columns": dataset.columns,
                "rows": [{"member": "成员甲", "value": 7}],
                "units": {"value": {"kind": "delta", "unit": "件"}},
            }
        },
        {},
    )


def test_copy_preserves_evidence_units_flags_and_remaps_nested_references(dataset_db):
    session, user, parent, source, *_ = dataset_db
    saved_report(parent, source)
    source.columns = [*source.columns, "dataset"]
    source.rows = [[*row, "D1"] for row in source.rows]
    source.truncated = True
    source.meta = {
        **source.meta,
        "root_kinds": ["governed"],
        "assumption": True,
        "partial": True,
        "explore": False,
        "units": {"value": {"kind": "level", "unit": "件"}},
        "summary_units": {"value": {"kind": "level", "unit": "件"}},
        "sql": "SELECT D1 FROM facts",
    }
    derived = AnalysisDataset(
        run_id=parent.id,
        seq=2,
        kind="calc",
        title="计算 D1",
        inputs=["D1"],
        columns=["name", "dataset", "column", "rows", "value"],
        rows=[["D1", "D1", "value", 1, 7]],
        meta={
            "closure": ["D1"],
            "args": {"values": [{"dataset": "D1"}], "formula": "x"},
            "summary": {"factor_decomposition": {"dataset": "D1"}},
            "root_kinds": ["governed"],
            "assumption": True,
            "partial": True,
        },
    )
    session.add(derived)
    run = child(session, parent)
    session.add(
        AnalysisDataset(
            run_id=run.id, seq=1, kind="context", title="本轮既有证据", columns=[], rows=[], meta={"asset_ids": []}
        )
    )
    session.flush()
    original = deepcopy(source.meta)

    history = context.build_followup_context(session, user, parent, run)
    copied = list(
        session.scalars(select(AnalysisDataset).where(AnalysisDataset.run_id == run.id).order_by(AnalysisDataset.seq))
    )
    base, calc = copied[1:]
    assert base.id != source.id and base.seq == 2 and calc.seq == 3
    assert base.meta == {**original, "copied_from": {"run_id": parent.id, "ref": "D1"}}
    assert base.rows[0][2] == "D1"  # 来源业务列即使名为 dataset 也不是结构引用。
    assert source.meta == original and base.truncated and calc.inputs == ["D2"]
    assert calc.meta["closure"] == ["D2"]
    assert calc.meta["args"]["values"] == [{"dataset": "D2"}]
    assert calc.meta["summary"]["factor_decomposition"] == {"dataset": "D2"}
    assert calc.rows == [["D1", "D2", "value", 1, 7]]  # 普通成员值不改编号。
    assert history["turns"][0]["summary"] == "上一轮发现集中变化"
    assert history["turns"][0]["causes"][0]["label"] == "成员甲"
    assert "7" in history["turns"][0]["causes"][0]["contribution"]
    assert history["turns"][0]["unexplained"] == "还没查清时间差异"
    assert history["turns"][0]["claims"][0]["datasets"] == [
        {"run_id": parent.id, "ref": "D1", "copied_ref": "D2", "title": source.title}
    ]
    base.rows[0][1] = 99
    base.meta["units"]["value"]["unit"] = "其他单位"
    assert source.rows[0][1] == 7 and source.meta["units"]["value"]["unit"] == "件"


def test_revoked_source_and_dependent_calc_are_not_copied_or_exposed(dataset_db):
    session, user, parent, source, asset, *_ = dataset_db
    saved_report(parent, source)
    session.add(
        AnalysisDataset(
            run_id=parent.id,
            seq=2,
            kind="calc",
            title="成员甲的派生数据",
            inputs=["D1"],
            columns=["value"],
            rows=[[7]],
            meta={"closure": ["D1"]},
        )
    )
    asset.domain_id = "hidden"
    session.flush()
    run = child(session, parent)
    history = context.build_followup_context(session, user, parent, run)
    assert context._datasets(session, run.id) == {}
    text = json.dumps(history, ensure_ascii=False)
    assert "成员甲" not in text and "关键值" not in text
    assert history["turns"][0]["claims"] == [] and history["copied_datasets"] == []


def test_refresh_rechecks_permission_without_creating_more_copies(dataset_db):
    session, user, parent, source, asset, *_ = dataset_db
    saved_report(parent, source)
    run = child(session, parent)
    context.build_followup_context(session, user, parent, run)
    count = len(context._datasets(session, run.id))
    assert context.refresh_followup_context(session, user, run)["copied_datasets"]
    asset.domain_id = "hidden"
    session.flush()
    history = context.refresh_followup_context(session, user, run)
    assert history["copied_datasets"] == [] and "成员甲" not in json.dumps(history, ensure_ascii=False)
    assert len(context._datasets(session, run.id)) == count


def test_history_uses_actual_followup_text_and_maps_older_evidence_across_turns(dataset_db):
    session, user, parent, source, *_ = dataset_db
    saved_report(parent, source)
    first = child(session, parent)
    context.build_followup_context(session, user, parent, first)
    first.question = "拼接后的运行问题"
    first.checkpoint = {"inherited": {"user_text": "真正的用户追问"}}
    first.report = {"title": "续问报告", "summary": "续问发现"}
    second = child(session, first, "second")
    history = context.build_followup_context(session, user, first, second)
    assert [turn["run_id"] for turn in history["turns"]] == [parent.id, first.id]
    assert history["turns"][1]["question"] == "真正的用户追问"
    assert history["turns"][0]["claims"][0]["datasets"][0]["copied_ref"] == "D1"
    assert history["previous_turns"] == 2


def test_followup_from_an_older_turn_keeps_existing_later_turns(dataset_db):
    session, user, parent, *_ = dataset_db
    existing = child(session, parent, "existing")
    run = child(session, parent)
    history = context.build_followup_context(session, user, parent, run)
    assert [turn["run_id"] for turn in history["turns"]] == [parent.id, existing.id]


def test_history_is_bounded_with_traceable_early_summary_and_omission_counts(dataset_db):
    session, user, root, *_ = dataset_db
    root.report = {"title": "最初结论", "summary": "最初发现"}
    parent = root
    for number in range(1, 35):
        previous = child(session, parent, f"turn-{number}")
        previous.created_at = root.created_at + timedelta(seconds=number)
        previous.question = "长问题" * 1000
        previous.report = {
            "title": "长标题" * 100,
            "summary": "长摘要" * 2000,
            "unexplained": "未解决" * 1000,
            "claims": [{"text": "关键结论" * 1000} for _ in range(6)],
        }
        parent = previous
    run = child(session, parent)
    run.created_at = root.created_at + timedelta(seconds=36)
    history = context.build_followup_context(session, user, parent, run)
    assert len(json.dumps(history, ensure_ascii=False)) <= context.CONTEXT_CHARS
    assert history["previous_turns"] == 35 and history["omitted_turns"] > 0
    assert history["earlier_turns"][0]["run_id"] == root.id
    assert history["earlier_turns"][0]["summary"] == "最初发现"
    assert history["turns"][-1]["run_id"] == parent.id


def test_confirmed_scope_still_passes_history_to_reason_and_retains_it_when_compacted(dataset_db, monkeypatch):
    session, user, parent, source, *_ = dataset_db
    saved_report(parent, source)
    run = child(session, parent)
    inherited = {
        "scope": {"question_type": "other", "metric_codes": []},
        "user_text": run.question,
        "conversation": context.build_followup_context(session, user, parent, run),
    }
    run.checkpoint = {"inherited": inherited}
    parent.display_title = "只用于显示的秘密标题"
    toolbox = AnalysisToolbox(session, user, run, ask.Catalog([], {}), Budget())
    monkeypatch.setattr(ask, "visible_catalog", lambda *args, **kwargs: ask.Catalog([], {}))
    config = {"configurable": {"toolbox": toolbox, "model": None}}
    state = agent.initial_state(run.question, "2026-01-01", "Asia/Taipei", {}, inherited=inherited)
    understood = agent.understand(state, config)
    assert understood["phase"] == "precheck" and toolbox.datasets()["D1"].rows == source.rows
    assert json.loads(toolbox.view_dataset("D1"))["rows"] == source.rows
    seen = []

    def act(state, config):
        seen.extend(state["messages"])
        toolbox.report = {"claims": []}
        return {"messages": state["messages"], "notes": []}

    monkeypatch.setattr(agent, "act", act)
    monkeypatch.setattr(agent, "verify", lambda *args: {"phase": "done"})
    agent.reason({**state, **understood}, config)
    text = "\n".join(message.content for message in seen)
    assert all(fragment in text for fragment in ("上一轮发现集中变化", "成员甲", "还没查清时间差异", "view_dataset"))
    assert "只用于显示的秘密标题" not in text and "copied_ref" in text
    initial = list(seen)
    for index in range(30):
        seen.extend(
            [
                AIMessage(
                    content="旧推理" * 200,
                    tool_calls=[{"id": f"tool-{index}", "name": "view_dataset", "args": {"ref": "D1"}}],
                ),
                ToolMessage(content="旧输出" * 300, tool_call_id=f"tool-{index}"),
            ]
        )
    before = toolbox.budget.estimate_input_tokens(seen, force=True, full=True)
    agent._compact(seen, toolbox, "manual", before, [])
    assert seen[: len(initial)] == initial
    assert "上一轮发现集中变化" in str(seen)
    assert toolbox.datasets()["D1"].rows == source.rows


def test_queue_time_revocation_removes_history_before_the_first_model_input(dataset_db, monkeypatch):
    session, user, parent, source, asset, *_ = dataset_db
    saved_report(parent, source)
    run = child(session, parent)
    inherited = {"conversation": context.build_followup_context(session, user, parent, run)}
    asset.domain_id = "hidden"
    session.flush()
    box = AnalysisToolbox(session, user, run, ask.Catalog([], {}), Budget())
    state = {"question": run.question, "inherited": inherited}
    refreshed = agent._followup_inherited(state, box)
    assert box.datasets() == {} and "成员甲" not in agent._conversation_text(refreshed)
    assert refreshed["conversation"]["copied_datasets"] == []
    assert box._dataset_seq_floor == 1
    created = box._dataset("context", "新证据", [], [], meta={"asset_ids": []})
    assert created.seq == 2


def test_understand_model_receives_history_when_reselecting_scope(dataset_db, monkeypatch):
    session, user, parent, source, *_ = dataset_db
    saved_report(parent, source)
    run = child(session, parent)
    inherited = {
        "user_text": run.question,
        "previous_question": parent.question,
        "conversation": context.build_followup_context(session, user, parent, run),
    }
    box = AnalysisToolbox(session, user, run, ask.Catalog([], {}), Budget())
    monkeypatch.setattr(box, "candidate_metrics", lambda question: [])
    seen = []

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

    monkeypatch.setattr(agent, "_model_for", lambda *args, **kwargs: (Model(), False))

    def invoke(bound, messages, *args, **kwargs):
        seen.extend(messages)
        return AIMessage(
            content="",
            tool_calls=[
                {"id": "scope", "name": "submit_scope", "args": {"question_type": "other", "metric_codes": []}}
            ],
        )

    monkeypatch.setattr(agent, "_invoke", invoke)
    state = agent.initial_state(run.question, "2026-01-01", "Asia/Taipei", {}, inherited=inherited)
    result = agent.understand(state, {"configurable": {"toolbox": box, "model": None}})
    text = "\n".join(message.content for message in seen)
    assert result["phase"] == "precheck"
    assert all(fragment in text for fragment in ("上一轮发现集中变化", "成员甲", "还没查清时间差异"))


def test_legacy_unattributed_report_fields_are_hidden_after_revocation(dataset_db):
    session, user, parent, source, asset, *_ = dataset_db
    parent.report = {
        "title": "秘密值甲",
        "summary": "秘密值乙",
        "unexplained": "秘密值丙",
        "claims": [{"type": "inference", "text": "秘密值丁"}],
    }
    asset.domain_id = "hidden"
    session.flush()
    history = context.build_followup_context(session, user, parent, child(session, parent))
    text = json.dumps(history, ensure_ascii=False)
    assert "秘密值" not in text and RESTRICTED_TEXT in text


def test_copy_remaps_structural_references_without_changing_business_filters_or_rows(dataset_db):
    session, user, parent, source, *_ = dataset_db
    source.columns = ["dataset", "ref", "value"]
    source.rows = [["D1", "D1", 7]]
    literal = {"dataset": "D1", "ref": "D1", "evidence_id": "D1#1"}
    source.meta = {**source.meta, "intent": {"filters": [literal]}, "original_intent": {"filters": [literal]}}
    dataset = AnalysisDataset(
        run_id=parent.id,
        seq=2,
        kind="calc",
        title="派生证据",
        inputs=["D1"],
        columns=["name", "dataset", "column", "rows", "value"],
        rows=[["x", "D1", "value", 1, 7]],
        meta={
            "closure": ["D1"],
            "args": {
                "dataset": "D1",
                "ref": "D1",
                "formula": "x",
                "match": literal,
                "filters": [literal],
                "values": [{"dataset": "D1", "match": literal, "exclude": [literal]}],
            },
            "summary": {"factor_decomposition": {"dataset": "D1"}},
        },
    )
    session.add(dataset)
    run = child(session, parent)
    session.add(
        AnalysisDataset(
            run_id=run.id, seq=1, kind="context", title="已有证据", columns=[], rows=[], meta={"asset_ids": []}
        )
    )
    session.flush()
    context.build_followup_context(session, user, parent, run)
    copied = context._datasets(session, run.id)
    assert copied["D2"].rows == [["D1", "D1", 7]]
    assert copied["D2"].meta["intent"] == source.meta["intent"]
    assert copied["D2"].meta["original_intent"] == source.meta["original_intent"]
    args = copied["D3"].meta["args"]
    assert args["dataset"] == "D2" and args["ref"] == "D2"
    assert args["match"] == literal and args["filters"] == [literal]
    assert args["values"] == [{"dataset": "D2", "match": literal, "exclude": [literal]}]
    assert copied["D3"].rows == [["x", "D2", "value", 1, 7]]
    assert copied["D3"].meta["summary"]["factor_decomposition"]["dataset"] == "D2"


@pytest.mark.parametrize("character", ['"', "\x01"])
def test_json_escaping_stays_within_budget_and_keeps_latest_turn(dataset_db, character):
    session, user, root, source, *_ = dataset_db
    repeated = character * 20_000
    root.report = {"title": "最初结论", "summary": "最初发现"}
    parent = root
    for number in range(1, 35):
        previous = child(session, parent, f"turn-{number}")
        previous.created_at = root.created_at + timedelta(seconds=number)
        previous.question = repeated
        previous.report = {
            "title": repeated,
            "summary": repeated,
            "unexplained": repeated,
            "causes": [{"label": repeated, "contribution": repeated} for _ in range(4)],
            "claims": [{"text": repeated} for _ in range(5)],
        }
        parent = previous
    for number in range(1, 25):
        session.add(
            AnalysisDataset(
                run_id=parent.id,
                seq=number,
                kind="governed",
                title=character * 200,
                columns=["value"],
                rows=[[7]],
                meta=deepcopy(source.meta),
            )
        )
    run = child(session, parent)
    run.created_at = root.created_at + timedelta(seconds=36)
    history = context.build_followup_context(session, user, parent, run)
    assert len(json.dumps(history, ensure_ascii=False)) <= context.CONTEXT_CHARS
    assert history["turns"][-1]["run_id"] == parent.id
    assert history["turns"][-1]["question"] and history["turns"][-1]["summary"]
    assert history["previous_turns"] == 35
