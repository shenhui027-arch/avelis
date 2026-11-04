"""保存的维表对齐说明随阅读者当前权限投影，不改写原始证据。"""

from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from avelis.analysis import service
from avelis.api import ask
from avelis.api.deps import current_user, get_session
from avelis.models import AnalysisStep, Answer, AskThread, Asset, IdentityLink, User, UserRole
from tests.unit.test_analysis_dataset_access import dataset_db  # noqa: F401


@pytest.fixture
def aligned_answer(dataset_db):
    session, user, run, dataset, _, _, version = dataset_db
    dimension = Asset(
        id="dimension",
        datasource_id="source",
        kind="table",
        catalog_name="local",
        db_name="data",
        name="private_regions",
        domain_id="visible",
    )
    alignment = {
        "dimension": "region",
        "original": "alias_a",
        "value": "member_a",
        "method": "dimension_table",
        "asset_id": dimension.id,
        "table": "local.data.private_regions",
        "column": "private_label",
    }
    thread = AskThread(id="thread", user_id=user.id, title="查询", status="closed")
    answer = Answer(
        id="answer",
        asked_by=user.id,
        thread_id=thread.id,
        question="查询",
        status="answered",
        model_adapter="offline",
        intent={"filters": [{"dimension": "region", "values": ["member_a"]}]},
        result={"columns": ["value"], "rows": [[7]], "column_roles": {}},
        evidence={"filter_alignment": [alignment], "filter_alignment_unknown": [], "note": "可读证据"},
        metric_version_ids=[version.id],
        steps=[{"node": "understand", "filter_alignment": [alignment], "filter_alignment_unknown": []}],
        explanation="alias_a 通过 local.data.private_regions.private_label 对齐为 member_a",
        explanation_source="model",
    )
    session.add_all([dimension, thread, answer])
    session.commit()
    return session, user, run, dataset, dimension, answer, alignment


def assert_answer_alignment_hidden(view):
    assert view["filter_alignment"] == []
    assert "filter_alignment" not in view["evidence"]
    assert "filter_alignment_unknown" not in view["evidence"]
    assert view["steps"] == [{"node": "understand"}]
    assert view["explanation"] is None and view["explanation_source"] is None
    assert view["result"]["rows"] == [[7]]
    assert view["intent"]["filters"][0]["values"] == ["member_a"]
    assert "private_regions" not in str(view) and "private_label" not in str(view)


@pytest.mark.parametrize("endpoint", ["/api/answers/answer", "/api/answers", "/api/ask/threads/thread"])
def test_answer_endpoints_project_alignment_after_dimension_access_is_revoked(aligned_answer, endpoint):
    session, user, _, _, dimension, answer, alignment = aligned_answer
    saved_evidence, saved_steps = deepcopy(answer.evidence), deepcopy(answer.steps)
    app = FastAPI()
    app.include_router(ask.router)
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[get_session] = lambda: session

    def read(client):
        response = client.get(endpoint)
        assert response.status_code == 200, response.text
        payload = response.json()
        return payload[0] if isinstance(payload, list) else payload["answers"][0] if "answers" in payload else payload

    with TestClient(app) as client:
        assert read(client)["filter_alignment"] == [alignment]
        dimension.domain_id = "hidden"
        session.commit()
        assert_answer_alignment_hidden(read(client))

    assert answer.evidence == saved_evidence and answer.steps == saved_steps
    assert answer.explanation is not None


def test_answer_projection_uses_current_reader_and_requires_access_context(aligned_answer):
    session, user, _, _, dimension, answer, alignment = aligned_answer
    dimension.domain_id = "hidden"
    admin = User(id="admin", username="admin", display_name="管理员", password_hash="unused")
    session.add_all([admin, UserRole(user_id=admin.id, role="admin")])
    session.commit()

    assert_answer_alignment_hidden(ask.answer_view(answer, session, user=user))
    assert ask.answer_view(answer, session, user=admin)["filter_alignment"] == [alignment]
    # 工作台等没有传权限上下文的旧调用方不能直接暴露维表说明。
    assert_answer_alignment_hidden(ask.answer_view(answer))


@pytest.mark.parametrize("source", ["legacy", "deleted", "upstream"])
def test_answer_alignment_requires_a_traceable_currently_readable_source(aligned_answer, source):
    session, user, _, _, dimension, answer, _ = aligned_answer
    if source == "legacy":
        evidence = deepcopy(answer.evidence)
        evidence["filter_alignment"][0].pop("asset_id")
        answer.evidence = evidence
    elif source == "deleted":
        session.delete(dimension)
    else:
        upstream = Asset(
            id="dimension-upstream",
            datasource_id="source",
            kind="table",
            catalog_name="local",
            db_name="data",
            name="upstream",
            domain_id="hidden",
        )
        session.add_all(
            [
                upstream,
                IdentityLink(
                    source_asset_id=dimension.id, target_asset_id=upstream.id, status="confirmed", method="manual"
                ),
            ]
        )
    session.commit()

    assert_answer_alignment_hidden(ask.answer_view(answer, session, user=user))


@pytest.mark.parametrize("source", ["report", "snapshot"])
@pytest.mark.parametrize("legacy", [False, True])
def test_analysis_alignment_sources_join_existing_restricted_step_projection(aligned_answer, source, legacy):
    session, user, run, dataset, dimension, _, alignment = aligned_answer
    explanation = {key: value for key, value in alignment.items() if key != "asset_id"} if legacy else alignment
    run.report = {"evidence": {"note": "可读证据"}}
    if source == "report":
        run.report = {
            "evidence": {"filter_alignment": [explanation], "filter_alignment_unknown": [], "note": "可读证据"}
        }
    else:
        run.checkpoint = {"snapshot": {"state": {"filter_alignment": [explanation]}}}
    step = AnalysisStep(
        run_id=run.id,
        seq=1,
        kind="model",
        status="ok",
        args={"table": alignment["table"]},
        reason=alignment["column"],
        output={"filter_alignment": [explanation]},
    )
    session.add(step)
    session.commit()
    saved_report, saved_checkpoint, saved_output = deepcopy(run.report), deepcopy(run.checkpoint), deepcopy(step.output)
    if not legacy:
        assert service.run_view(session, run, user=user)["filter_alignment"] == [explanation]
    dimension.domain_id = "hidden"
    session.commit()

    view = service.run_view(session, run, user=user)

    assert view["filter_alignment"] == []
    assert view["report"]["evidence"] == {"note": "可读证据"}
    assert view["steps"][0]["args"] == {}
    assert view["steps"][0]["reason"] is None
    assert view["steps"][0]["output"] == {"restricted": True}
    assert view["datasets"][0]["rows"] == dataset.rows and not view["datasets"][0].get("restricted")
    assert "private_regions" not in str(view) and "private_label" not in str(view)
    assert run.report == saved_report and run.checkpoint == saved_checkpoint and step.output == saved_output


def test_lookup_run_checks_alignment_from_saved_answer_even_without_run_copy(aligned_answer):
    session, user, run, _, dimension, answer, _ = aligned_answer
    run.report = {
        "kind": "lookup",
        "answer_id": answer.id,
        "title": "查询",
        "summary": "按 local.data.private_regions.private_label 对齐",
    }
    session.add(
        AnalysisStep(
            run_id=run.id,
            seq=1,
            kind="model",
            status="ok",
            output={"note": "local.data.private_regions.private_label"},
        )
    )
    dimension.domain_id = "hidden"
    session.commit()

    view = service.run_view(session, run, user=user)

    assert_answer_alignment_hidden(view["answer"])
    assert view["report"]["restricted"] is True
    assert view["steps"][0]["output"] == {"restricted": True}
    assert "private_regions" not in str(view) and "private_label" not in str(view)


@pytest.mark.parametrize("source", ["report", "snapshot"])
def test_confirmed_candidate_keeps_source_dependency_for_revocation(aligned_answer, source):
    session, user, run, dataset, dimension, answer, _ = aligned_answer
    explanation = {
        "dimension": "region",
        "original": "alias_a",
        "value": "member_a",
        "method": "confirmed",
        "asset_id": dimension.id,
    }
    answer.evidence = {"filter_alignment": [explanation], "note": "可读证据"}
    answer.steps = [{"node": "understand", "filter_alignment": [explanation]}]
    run.report = {"evidence": {"note": "可读证据"}}
    if source == "report":
        run.report["evidence"]["filter_alignment"] = [explanation]
    else:
        run.checkpoint = {"snapshot": {"state": {"filter_alignment": [explanation]}}}
    step = AnalysisStep(run_id=run.id, seq=1, kind="plan", status="ok", output={"filter_alignment": [explanation]})
    session.add(step)
    session.commit()
    saved_report, saved_evidence, saved_checkpoint = (
        deepcopy(run.report),
        deepcopy(answer.evidence),
        deepcopy(run.checkpoint),
    )
    assert service.run_view(session, run, user=user)["filter_alignment"] == [explanation]
    assert ask.answer_view(answer, session, user=user)["filter_alignment"] == [explanation]

    dimension.domain_id = "hidden"
    session.commit()

    assert_answer_alignment_hidden(ask.answer_view(answer, session, user=user))
    view = service.run_view(session, run, user=user)
    assert view["filter_alignment"] == [] and view["report"]["evidence"] == {"note": "可读证据"}
    assert view["steps"][0]["output"] == {"restricted": True}
    assert view["datasets"][0]["rows"] == dataset.rows
    assert run.report == saved_report and answer.evidence == saved_evidence and run.checkpoint == saved_checkpoint


def test_answer_projects_alignment_retained_only_in_earlier_steps(aligned_answer):
    session, user, _, _, dimension, answer, _ = aligned_answer
    answer.evidence = {"note": "可读证据"}
    dimension.domain_id = "hidden"
    session.commit()

    assert_answer_alignment_hidden(ask.answer_view(answer, session, user=user))


@pytest.mark.parametrize("kind", ["model", "tool"])
def test_analysis_projects_alignment_retained_only_in_earlier_steps(aligned_answer, kind):
    session, user, run, dataset, dimension, _, alignment = aligned_answer
    run.report, run.checkpoint = {}, {}
    session.add(
        AnalysisStep(
            run_id=run.id,
            seq=1,
            kind=kind,
            dataset_id=dataset.id if kind == "tool" else None,
            status="ok",
            output={"filter_alignment": [alignment], "row_count": 1},
        )
    )
    dimension.domain_id = "hidden"
    session.commit()

    view = service.run_view(session, run, user=user)

    assert view["filter_alignment"] == []
    assert view["steps"][0]["output"] == ({"row_count": 1} if kind == "tool" else {"restricted": True})
    assert view["datasets"][0]["rows"] == dataset.rows
    assert "private_regions" not in str(view) and "private_label" not in str(view)


@pytest.fixture
def candidate_clarification(aligned_answer):
    session, user, run, dataset, dimension, answer, _ = aligned_answer
    item = {
        "dimension": "region",
        "original": "shared_alias",
        "options": ["private_key_a", "private_key_b", "public_member"],
        "candidate_sources": {"private_key_a": [dimension.id], "private_key_b": [dimension.id]},
    }
    clarification = {
        "kind": "clarify",
        "question": "Choose private_key_a, private_key_b or public_member",
        "dimension": "region",
        "options": item["options"],
        "items": [item],
    }
    run.status = "waiting"
    run.checkpoint = {
        "pending": clarification,
        "snapshot": {
            "state": {
                "clarification": clarification["question"],
                "clarification_details": clarification,
                "clarifications": [{**clarification, "answer": "private_key_a"}],
            }
        },
    }
    run.report = {
        "summary": clarification["question"],
        "clarification": clarification,
        "clarifications": [{**clarification, "answer": "private_key_b"}],
    }
    step = AnalysisStep(
        run_id=run.id, seq=1, kind="clarify", tool="understand", status="waiting", args={}, output=clarification
    )
    session.add(step)
    answer.evidence = {"clarification": clarification, "alignment_candidates": [item]}
    answer.steps = [
        {
            "node": "understand",
            "alignment_candidates": [item],
            "tool_calls": [{"args": {"question": clarification["question"]}}],
        }
    ]
    answer.message = clarification["question"]
    session.commit()
    return session, user, run, dataset, dimension, answer, clarification


@pytest.mark.parametrize("full", [True, False])
@pytest.mark.parametrize("revoked", ["domain", "deleted", "upstream"])
def test_run_projects_saved_candidate_clarifications_on_revocation(candidate_clarification, full, revoked):
    session, user, run, dataset, dimension, _, clarification = candidate_clarification
    saved_checkpoint, saved_report = deepcopy(run.checkpoint), deepcopy(run.report)
    assert service.run_view(session, run, user=user, full=full)["pending"] == clarification
    if revoked == "domain":
        dimension.domain_id = "hidden"
    elif revoked == "deleted":
        session.delete(dimension)
    else:
        upstream = Asset(
            id="private_upstream",
            datasource_id="source",
            kind="table",
            catalog_name="local",
            db_name="data",
            name="upstream",
            domain_id="hidden",
        )
        session.add_all(
            [
                upstream,
                IdentityLink(
                    source_asset_id=dimension.id, target_asset_id=upstream.id, status="confirmed", method="manual"
                ),
            ]
        )
    session.commit()

    view = service.run_view(session, run, user=user, full=full)

    assert "private_key_a" not in str(view) and "private_key_b" not in str(view)
    assert view["pending"]["items"][0]["options"] == ["public_member"]
    assert view["pending"]["options"] == ["public_member"]
    if full:
        assert view["steps"][0]["output"]["items"][0]["options"] == ["public_member"]
        assert view["datasets"][0]["rows"] == dataset.rows
    assert run.checkpoint == saved_checkpoint and run.report == saved_report


def test_ask_projects_saved_candidate_text_and_preserves_original_evidence(candidate_clarification):
    session, user, _, _, dimension, answer, _ = candidate_clarification
    saved_evidence, saved_steps = deepcopy(answer.evidence), deepcopy(answer.steps)
    assert "private_key_a" in str(ask.answer_view(answer, session, user=user))
    dimension.domain_id = "hidden"
    session.commit()

    view = ask.answer_view(answer, session, user=user)

    assert "private_key_a" not in str(view) and "private_key_b" not in str(view)
    assert view["evidence"]["clarification"]["items"][0]["options"] == ["public_member"]
    assert view["message"] is None and view["steps"] == []
    assert answer.evidence == saved_evidence and answer.steps == saved_steps


def test_ask_model_does_not_receive_revoked_candidate_history(candidate_clarification, monkeypatch):
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from avelis.ask import agent
    from tests.unit.test_ask_filter_alignment import make_box

    session, user, _, _, dimension, answer, _ = candidate_clarification
    box = make_box(monkeypatch)
    dimension.domain_id = "hidden"
    session.commit()
    seen = []

    def invoke(messages):
        seen.extend(messages)
        return AIMessage("Please clarify")

    model = SimpleNamespace(bind_tools=lambda _: SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(agent.service, "visible_catalog", lambda *args, **kwargs: box.catalog)
    monkeypatch.setattr(agent.modelcfg, "resolve", lambda *args: SimpleNamespace(model=model, adapter="offline"))
    monkeypatch.setattr(agent.modelcfg, "policy", lambda *args: {"send_results": True})
    state = {
        "steps": answer.steps,
        "messages": [
            HumanMessage("start"),
            ToolMessage("private_key_a", tool_call_id="old"),
            AIMessage("choose private_key_b"),
            HumanMessage("continue"),
        ],
    }
    agent.understand(state, {"configurable": {"session": session, "user": user, "answer": answer}})
    assert "private_key_a" not in str(seen) and "private_key_b" not in str(seen)
    assert "continue" in str(seen)


def test_candidate_sources_survive_analysis_and_ask_merge(candidate_clarification, monkeypatch):
    from avelis.analysis import agent as analysis_agent
    from avelis.ask import agent as ask_agent
    from avelis.ask.alignment import AlignmentResult
    from avelis.ask.intent import DimensionFilter
    from tests.unit.test_ask_filter_alignment import make_box

    session, user, _, _, dimension, _, _ = candidate_clarification
    box = make_box(monkeypatch, codes=("metric_a", "metric_b"))
    box.session, box.user = session, user
    pending = {
        "dimension": "dimension_a",
        "original": "alias",
        "candidates": ["private_key_a", "public_member"],
        "has_dimension_table": True,
        "candidate_sources": {"private_key_a": [dimension.id]},
    }
    monkeypatch.setattr(
        ask_agent,
        "align_filters",
        lambda *args: AlignmentResult(
            [DimensionFilter(dimension="dimension_a", values=["alias"])], unresolved=[pending]
        ),
    )
    result = box._align_filters(["metric_a", "metric_b"], [DimensionFilter(dimension="dimension_a", values=["alias"])])
    _, details = analysis_agent._filter_clarification(result.unresolved)
    assert details["items"][0]["candidate_sources"] == {"private_key_a": [dimension.id]}
    dimension.domain_id = "hidden"
    session.commit()
    from avelis.ask.service import project_alignment_clarifications

    projected, restricted = project_alignment_clarifications(session, user, details)
    assert restricted and projected["items"][0]["options"] == ["public_member"]
    assert "private_key_a" not in str(projected)
