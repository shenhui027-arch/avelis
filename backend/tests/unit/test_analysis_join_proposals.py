"""结构化关联从模型提议到检查点确认的离线验证。"""

from copy import deepcopy

import pytest
from sqlalchemy import func, select

from avelis import jobs
from avelis.analysis import agent, service
from avelis.analysis.tools import AnalysisToolbox, Budget
from avelis.api.schemas import AnalysisRunOut
from avelis.ask.service import Catalog
from avelis.governance import joins
from avelis.governance.errors import Conflict, Forbidden, GovernanceError, Invalid, NotFound
from avelis.models import AnalysisRun, Asset, DomainMember, JoinPath, UserRole
from tests.unit.test_analysis_hitl import reply
from tests.unit.test_analysis_pipeline import Phased
from tests.unit.test_join_proposals import state  # noqa: F401


SPEC = {"left_asset": "catalog.space.left_records", "left_columns": ["key_a", "key_b"],
        "right_asset": "catalog.space.right_records", "right_columns": ["key_a", "key_b"]}
TEXT = "space.left_records.key_a = space.right_records.key_a AND space.left_records.key_b = space.right_records.key_b"


def waiting(state, monkeypatch, *, spec=SPEC, run_id="run_a"):
    session, actor, *_ = state
    item = {"id": "P1", "kind": "join", "source": "inferred", "content": TEXT, "refs": []}
    if spec is not None:
        item["join"] = deepcopy(spec)
    run = AnalysisRun(id=run_id, user_id=actor.id, question="检查关联", status="waiting", trigger="manual",
                      confirmation_mode="auto", window={}, budget={},
                      checkpoint={"pending": {"kind": "assumptions"}, "proposal": {"items": [item]}})
    session.add(run)
    session.commit()
    queued = []
    monkeypatch.setattr(jobs, "_enqueue", lambda *args: queued.append(args) or "resume_job")
    return run, queued


def confirm(session, actor, run):
    return service.resume_run(session, actor, run.id,
                              {"decisions": [{"item_id": "P1", "decision": "confirmed"}]})


@pytest.mark.parametrize("changes", [{"left_asset": "space.left_records"},
                                     {"left_columns": ["missing", "key_b"]},
                                     {"right_columns": ["missing", "key_b"]},
                                     {"right_columns": ["key_a"]}, {"left_columns": []}])
def test_invalid_join_structure_is_removed_without_losing_text(state, changes):
    session, actor, *_ = state
    item = {"kind": "join", "content": TEXT, "join": SPEC | changes}
    assert agent.validate_proposal_join(session, actor, item) is None
    assert item == {"kind": "join", "content": TEXT}


def test_invisible_asset_removes_join_structure(state):
    session, actor, *_ = state
    member = session.scalar(select(DomainMember).where(DomainMember.user_id == actor.id,
                                                       DomainMember.domain_id == "domain_b"))
    session.delete(member)
    item = {"kind": "join", "content": TEXT, "join": deepcopy(SPEC)}
    assert agent.validate_proposal_join(session, actor, item) is None
    assert item == {"kind": "join", "content": TEXT}


@pytest.mark.parametrize("spec", [None, {}, {"left_columns": "key_a"}, SPEC | {"right_columns": []}])
def test_malformed_join_does_not_invalidate_proposal(spec):
    proposal = agent.Proposal.model_validate({"items": [{"kind": "join", "content": TEXT, "join": spec}]})
    assert proposal.items[0].join is None and proposal.items[0].content == TEXT


@pytest.mark.parametrize("valid", [False, True])
def test_model_proposal_validates_join_before_checkpoint(state, monkeypatch, valid):
    session, actor, *_ = state
    run, _ = waiting(state, monkeypatch)
    box = AnalysisToolbox(session, actor, run, Catalog([], {}), Budget())
    monkeypatch.setattr(box, "recall_experiences", lambda *args: None)
    spec = SPEC if valid else SPEC | {"left_columns": ["missing", "key_b"]}
    model = Phased([reply("submit_proposal", {"items": [{"kind": "join", "content": TEXT, "join": spec}]}, "p")])
    result = agent.propose({"question": "检查关联"}, {"configurable": {"toolbox": box, "model": model}})
    item = result["proposal"]["items"][0]
    assert item["content"] == TEXT and item["source"] == "inferred"
    assert ("join" in item) is valid
    assert not box.denials


def test_confirmation_persists_pending_path_source_and_reference_without_experience(state, monkeypatch):
    session, actor, *_ = state
    run, queued = waiting(state, monkeypatch)
    confirm(session, actor, run)
    session.commit()
    session.expire_all()
    path = session.scalar(select(JoinPath))
    assert path.status == "pending"
    assert path.source == {"run_id": "run_a", "checkpoint": "assumptions", "user_text": TEXT}
    assert run.checkpoint["join_candidates"] == [{"item_id": "P1", "id": path.id, "status": "pending"}]
    assert run.checkpoint["candidates"] == []
    view = AnalysisRunOut.model_validate(service.run_view(session, run, user=actor))
    assert view.join_candidates == run.checkpoint["join_candidates"]
    assert view.proposal.items[0].join == SPEC
    assert view.checkpoint_warnings == []
    with pytest.raises(Conflict):
        confirm(session, actor, run)
    assert len(queued) == 1 and session.scalar(select(func.count()).select_from(JoinPath)) == 1


@pytest.mark.parametrize("existing", [False, True])
def test_confirmations_across_runs_reuse_path_in_either_direction(state, monkeypatch, existing):
    session, actor, *_ = state
    if existing:
        path = joins.propose(session, actor, left_asset_id="right", left_columns=["key_b", "key_a"],
                             right_asset_id="left", right_columns=["key_b", "key_a"], source={"user_text": "原提议"})
        path.status = "approved"
    run, _ = waiting(state, monkeypatch)
    confirm(session, actor, run)
    first = run.checkpoint["join_candidates"][0]
    second, _ = waiting(state, monkeypatch, run_id="run_b")
    confirm(session, actor, second)
    assert second.checkpoint["join_candidates"] == [first]
    assert first["status"] == ("approved" if existing else "pending")
    assert session.scalar(select(func.count()).select_from(JoinPath)) == 1


@pytest.mark.parametrize("spec", [None, SPEC | {"left_columns": ["missing", "key_b"]}])
def test_confirmed_join_without_valid_structure_records_checkpoint_warning(state, monkeypatch, spec):
    session, actor, *_ = state
    run, _ = waiting(state, monkeypatch, spec=spec)
    confirm(session, actor, run)
    assert run.checkpoint["warnings"] == ["这条关联缺少结构，无法提议为关联路径"]
    assert run.checkpoint["candidates"] == run.checkpoint["join_candidates"] == []
    assert "join" not in run.checkpoint["proposal"]["items"][0]
    assert session.scalar(select(func.count()).select_from(JoinPath)) == 0
    assert service.run_view(session, run)["checkpoint_warnings"] == run.checkpoint["warnings"]


@pytest.mark.parametrize("side", ["left", "right"])
def test_unassigned_asset_join_warns_and_confirmation_continues(state, monkeypatch, side):
    session, actor, *_ = state
    session.get(Asset, side).domain_id = None
    session.scalar(select(UserRole).where(UserRole.user_id == actor.id)).role = "steward"
    run, queued = waiting(state, monkeypatch)
    confirm(session, actor, run)
    session.commit()
    session.expire_all()
    warning = (f"这条关联没能提议为关联路径：资产 space.{side}_records 未归属业务域，"
               "无法确定由谁批准关联路径")
    assert run.status == "queued" and len(queued) == 1
    assert run.checkpoint["resume_pending"] and "pending" not in run.checkpoint
    assert run.checkpoint["warnings"] == [warning]
    assert run.checkpoint["proposal"]["items"][0]["join"] == SPEC
    assert run.checkpoint["candidates"] == run.checkpoint["join_candidates"] == []
    assert session.scalar(select(func.count()).select_from(JoinPath)) == 0
    assert service.run_view(session, run)["checkpoint_warnings"] == [warning]


@pytest.mark.parametrize("error", [GovernanceError, Conflict, Forbidden, Invalid, NotFound])
def test_join_proposal_governance_error_becomes_checkpoint_warning(state, monkeypatch, error):
    session, actor, *_ = state
    run, queued = waiting(state, monkeypatch)

    def fail(*args, **kwargs):
        raise error("提议失败")

    monkeypatch.setattr(joins, "propose", fail)
    confirm(session, actor, run)
    assert run.status == "queued" and len(queued) == 1
    assert run.checkpoint["warnings"] == ["这条关联没能提议为关联路径：提议失败"]
    assert run.checkpoint["join_candidates"] == []


@pytest.mark.parametrize("target,error", [(joins, RuntimeError), (service.experiences, Conflict)])
def test_confirmation_does_not_swallow_unrelated_errors(state, monkeypatch, target, error):
    session, actor, *_ = state
    run, queued = waiting(state, monkeypatch)

    def fail(*args, **kwargs):
        raise error("不应忽略")

    monkeypatch.setattr(target, "propose" if target is joins else "candidates_from_feedback", fail)
    with pytest.raises(error, match="不应忽略"):
        confirm(session, actor, run)
    assert run.status == "waiting" and not queued


@pytest.mark.parametrize("phase", ["propose", "checkpoint"])
def test_inherited_join_is_revalidated_before_it_reaches_model(state, monkeypatch, phase):
    session, actor, *_ = state
    run, _ = waiting(state, monkeypatch)
    box = AnalysisToolbox(session, actor, run, Catalog([], {}), Budget())
    monkeypatch.setattr(box, "recall_experiences", lambda *args: None)
    monkeypatch.setattr(box, "check_resume_permissions", lambda: None)
    inherited = {"items": [{"id": "old", "kind": "join", "source": "inferred", "content": TEXT,
                             "refs": [], "status": "confirmed", "join": SPEC | {"left_columns": ["missing"]}}]}
    model = Phased([reply("submit_proposal", {"items": []}, "p")])
    config = {"configurable": {"toolbox": box, "model": model}}
    data = {"question": "继续检查", "inherited": inherited, "proposal": {"items": []},
            "unattended": False, "confirmation_mode": "auto"}
    result = getattr(agent, phase)(data, config)
    assert "join" not in result["inherited"]["items"][0]
    assert box.confirmed_items[0]["content"] == TEXT and "join" not in box.confirmed_items[0]
    if phase == "propose":
        assert '"join":' not in model.seen[0][1].content


def test_graph_resume_uses_confirmed_proposal_without_restoring_discarded_structure(state, monkeypatch):
    session, actor, *_ = state
    run, _ = waiting(state, monkeypatch, spec=None)
    run.checkpoint = {**run.checkpoint, "resume_pending": True}
    box = AnalysisToolbox(session, actor, run, Catalog([], {}), Budget())
    monkeypatch.setattr(box, "check_resume_permissions", lambda: None)
    monkeypatch.setattr(agent, "interrupt", lambda value: {"decisions": [{"item_id": "P1", "decision": "confirmed"}]})
    stale = deepcopy(run.checkpoint["proposal"])
    stale["items"][0]["join"] = deepcopy(SPEC)
    result = agent.checkpoint({"proposal": stale, "unattended": False, "confirmation_mode": "auto"},
                               {"configurable": {"toolbox": box, "model": None}})
    assert "join" not in result["proposal"]["items"][0]
    assert "join" not in result["confirmed_items"][0]
