"""关联提议的离线持久化、权限、复用及现有分域审批。"""

import re
from types import SimpleNamespace

import pytest
from sqlalchemy import JSON, MetaData, create_engine, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from avelis.api.governance import join_path_view
from avelis.db import Base
from avelis.governance import experiences, joins
from avelis.governance.errors import Conflict, Forbidden, Invalid
from avelis.models import (
    AnalysisRun,
    Asset,
    AuditEvent,
    ColumnVersion,
    Domain,
    DomainMember,
    Experience,
    ExperienceVersion,
    Grain,
    JoinPath,
    User,
    UserRole,
)


@pytest.fixture
def state():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for table in Base.metadata.tables.values():
        copy = table.to_metadata(metadata)
        for column in copy.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        actor = User(id="analyst", username="analyst", display_name="提议人", password_hash="unused")
        owner_a = User(id="owner_a", username="owner_a", display_name="负责人甲", password_hash="unused")
        owner_b = User(id="owner_b", username="owner_b", display_name="负责人乙", password_hash="unused")
        session.add_all([actor, owner_a, owner_b, UserRole(user_id=actor.id, role="analyst"),
                         Domain(id="domain_a", code="domain_a", name="域甲", owner_id=owner_a.id),
                         Domain(id="domain_b", code="domain_b", name="域乙", owner_id=owner_b.id)])
        session.add_all([DomainMember(domain_id=domain_id, user_id=actor.id, role="viewer")
                         for domain_id in ("domain_a", "domain_b")])
        for side, domain in (("left", "domain_a"), ("right", "domain_b")):
            session.add(Asset(id=side, datasource_id="ds", kind="table", catalog_name="catalog", db_name="space",
                              name=f"{side}_records", domain_id=domain, current_version_id=f"{side}_v"))
            session.add_all([ColumnVersion(asset_version_id=f"{side}_v", column_id=f"{side}_{column}",
                                           ordinal=index, name=column, data_type="VARCHAR")
                             for index, column in enumerate(("key_a", "key_b"))])
        session.add(Grain(asset_id="right", asset_version_id="right_v", columns=["key_a", "key_b"],
                          origin="observed", status="verified"))
        session.commit()
        yield session, actor, owner_a, owner_b
    engine.dispose()


def propose(state, **changes):
    session, actor, *_ = state
    return joins.propose(session, actor, **({"left_asset_id": "left", "left_columns": ["key_a", "key_b"],
                                            "right_asset_id": "right", "right_columns": ["key_a", "key_b"],
                                            "source": {"run_id": "run_a", "checkpoint": "assumptions",
                                                       "user_text": "按两个键关联"}} | changes))


@pytest.mark.parametrize("role", ["analyst", "steward", "reviewer"])
def test_proposal_persists_pending_with_source_cardinality_and_creator(state, role):
    session, actor, *_ = state
    session.scalar(select(UserRole).where(UserRole.user_id == actor.id)).role = role
    path = propose(state)
    session.commit()
    session.expire(path)
    assert path.status == "pending"
    assert path.cardinality == "n1"
    assert path.created_by == actor.id
    assert path.source == {"run_id": "run_a", "checkpoint": "assumptions", "user_text": "按两个键关联"}
    assert joins.required_domains(session, path) == ["domain_a", "domain_b"]
    assert re.fullmatch(r"[a-z0-9_]{1,64}", path.code)
    assert path.name == "left_records 与 right_records 按 key_a、key_b 关联"
    view = join_path_view(session, path)
    assert view["created_by"] == "提议人"
    assert view["source"] == path.source
    event = session.scalar(select(AuditEvent).where(AuditEvent.action == "join_path.propose"))
    assert event.object_id == path.id
    assert event.actor_id == actor.id
    assert event.detail["reused"] is False
    assert event.detail["source"] == path.source


def test_repeat_reverse_and_reordered_pairs_reuse_path_and_keep_original_source(state):
    session, *_ = state
    path = propose(state)
    assert propose(state).id == path.id
    assert propose(state, left_columns=["key_b", "key_a"], right_columns=["key_b", "key_a"]).id == path.id
    assert propose(state, left_asset_id="right", right_asset_id="left", left_columns=["key_b", "key_a"],
                   right_columns=["key_b", "key_a"], source={"run_id": "run_b"}).id == path.id
    assert session.scalar(select(func.count()).select_from(JoinPath)) == 1
    assert path.source["run_id"] == "run_a"
    session.flush()
    events = list(session.scalars(select(AuditEvent).where(AuditEvent.action == "join_path.propose")))
    assert [event.detail["reused"] for event in events] == [False, True, True, True]


@pytest.mark.parametrize("status", ["approved", "pending", "rejected", "affected"])
def test_existing_path_in_either_direction_is_referenced_without_creation(state, status):
    session, *_ = state
    path = propose(state, left_asset_id="right", right_asset_id="left")
    path.status = status
    assert propose(state).id == path.id
    assert path.status == status
    assert session.scalar(select(func.count()).select_from(JoinPath)) == 1


def test_column_pairing_matters_when_reusing(state):
    first = propose(state)
    second = propose(state, right_columns=["key_b", "key_a"])
    assert first.id != second.id


def test_code_collision_adds_suffix_and_non_ascii_names_remain_valid(state):
    session, *_ = state
    first = propose(state)
    first.code = "left_records_key_a_key_b_right_records_key_b_key_a"
    second = propose(state, right_columns=["key_b", "key_a"])
    assert second.code == first.code + "_2"
    session.get(Asset, "left").name = "左侧 记录"
    session.get(Asset, "right").name = "右侧/记录"
    third = propose(state, left_columns=["key_a"], right_columns=["key_a"])
    assert re.fullmatch(r"[a-z0-9_]{1,64}", third.code)


def hide_code_once(session, monkeypatch, code):
    scalar = session.scalar
    query = select(JoinPath.id).where(JoinPath.code == code)
    hidden = False

    def stale_scalar(statement, *args, **kwargs):
        nonlocal hidden
        if not hidden and statement.compare(query):
            hidden = True
            return None
        return scalar(statement, *args, **kwargs)

    monkeypatch.setattr(session, "scalar", stale_scalar)


@pytest.mark.parametrize("reverse", [False, True])
def test_unique_conflict_reuses_concurrent_path_and_preserves_outer_transaction(state, monkeypatch, reverse):
    session, actor, *_ = state
    changes = {"left_asset_id": "right", "right_asset_id": "left",
               "left_columns": ["key_b", "key_a"], "right_columns": ["key_b", "key_a"]} if reverse else {}
    existing = propose(state, **changes)
    existing.code = "left_records_key_a_key_b_right_records_key_a_key_b"
    session.commit()
    actor.display_name = "确认后仍保留"
    find = joins._existing_path
    lookups = 0

    def stale_path(*args):
        nonlocal lookups
        lookups += 1
        return None if lookups == 1 else find(*args)

    monkeypatch.setattr(joins, "_existing_path", stale_path)
    hide_code_once(session, monkeypatch, existing.code)
    path = propose(state, source={"run_id": "concurrent_run"})
    session.commit()

    assert path.id == existing.id
    assert lookups == 2
    assert path.source["run_id"] == "run_a"
    assert session.scalar(select(func.count()).select_from(JoinPath)) == 1
    assert session.get(User, actor.id).display_name == "确认后仍保留"
    events = list(session.scalars(select(AuditEvent).where(AuditEvent.action == "join_path.propose")))
    assert [event.detail["reused"] for event in events] == [False, True]
    assert events[-1].detail["source"] == {"run_id": "concurrent_run"}


def test_unique_conflict_with_another_join_retries_next_suffix(state, monkeypatch):
    session, *_ = state
    existing = propose(state, right_columns=["key_b", "key_a"])
    existing.code = "left_records_key_a_key_b_right_records_key_a_key_b"
    session.commit()
    hide_code_once(session, monkeypatch, existing.code)

    path = propose(state)
    session.commit()

    assert path.id != existing.id
    assert path.code == existing.code + "_2"
    assert session.scalar(select(func.count()).select_from(JoinPath)) == 2


def test_repeated_code_conflicts_stop_after_bounded_retries(state, monkeypatch):
    session, *_ = state
    codes = []
    error = IntegrityError("insert", {}, SimpleNamespace(
        sqlstate="23505", diag=SimpleNamespace(constraint_name="uq_join_path_code")))

    def conflict(session, actor, code, *args):
        codes.append(code)
        raise error

    monkeypatch.setattr(joins, "_create", conflict)
    with pytest.raises(Conflict, match="并发冲突"):
        propose(state)

    base = "left_records_key_a_key_b_right_records_key_a_key_b"
    assert codes == [base, *(f"{base}_{suffix}" for suffix in range(2, 6))]
    assert session.is_active
    assert session.scalar(select(func.count()).select_from(JoinPath)) == 0


@pytest.mark.parametrize("sqlstate, constraint", [("23503", "fk_join_path_left_asset_id_asset"),
                                                 ("23505", "pk_join_path")])
def test_unrelated_integrity_error_is_not_retried(state, monkeypatch, sqlstate, constraint):
    error = IntegrityError("insert", {}, SimpleNamespace(
        sqlstate=sqlstate, diag=SimpleNamespace(constraint_name=constraint)))
    calls = []

    def fail(*args):
        calls.append(None)
        raise error

    monkeypatch.setattr(joins, "_create", fail)
    with pytest.raises(IntegrityError) as caught:
        propose(state)
    assert caught.value is error
    assert len(calls) == 1


def test_analyst_cannot_directly_create_or_approve_and_domains_still_both_approve(state):
    session, actor, owner_a, owner_b = state
    with pytest.raises(Forbidden):
        joins.create(session, actor, code="manual", name="关联", left_asset_id="left", left_columns=["key_a"],
                     right_asset_id="right", right_columns=["key_a"])
    path = propose(state)
    with pytest.raises(Forbidden):
        joins.decide(session, actor, path, "domain_a", "approved", None)
    joins.decide(session, owner_a, path, "domain_a", "approved", None)
    assert path.status == "pending"
    joins.decide(session, owner_b, path, "domain_b", "approved", None)
    assert path.status == "approved"
    assert joins.effectively_approved(session, path)


def test_proposal_requires_supported_role_and_both_assets_visible_even_when_path_exists(state):
    session, actor, *_ = state
    propose(state)
    membership = session.scalar(select(DomainMember).where(DomainMember.user_id == actor.id,
                                                           DomainMember.domain_id == "domain_b"))
    session.delete(membership)
    with pytest.raises(Forbidden):
        propose(state)
    session.scalar(select(UserRole).where(UserRole.user_id == actor.id)).role = "viewer"
    with pytest.raises(Forbidden):
        propose(state)


@pytest.mark.parametrize("changes", [{"left_columns": []}, {"right_columns": ["key_a"]},
                                     {"left_columns": ["missing", "key_b"]}])
def test_proposal_validates_columns_and_matching_key_counts(state, changes):
    with pytest.raises(Invalid):
        propose(state, **changes)


def save_join_experience(state):
    session, actor, *_ = state
    session.add(AnalysisRun(id="experience_run", user_id=actor.id, question="确认关联", trigger="manual",
                            status="succeeded"))
    content = ("space.left_records.key_a = space.right_records.key_a AND "
               "left_records.key_b = right_records.key_b")
    saved = experiences.save_experience(session, actor, kind="join", content=content, refs=[],
                                       source={"run_id": "experience_run", "checkpoint": "assumptions",
                                               "user_text": "原始提议"})
    session.commit()
    return saved


def test_experience_conversion_creates_real_pending_path_and_keeps_revoked_version(state):
    session, actor, *_ = state
    saved = save_join_experience(state)
    path = experiences.to_join_path(session, actor, saved["id"])
    session.commit()
    session.expire_all()
    assert path.status == "pending"
    assert (path.left_asset_id, path.right_asset_id) == ("left", "right")
    assert path.left_columns == path.right_columns == ["key_a", "key_b"]
    assert path.cardinality == "n1"
    assert path.source == {"run_id": "experience_run", "checkpoint": "assumptions",
                           "user_text": saved["content"]}
    assert session.scalar(select(func.count()).select_from(JoinPath)) == 1
    experience = session.get(Experience, saved["id"])
    version = session.get(ExperienceVersion, saved["version_id"])
    assert experience.status == "revoked"
    assert experience.current_version == 1
    assert version.content == saved["content"]
    assert version.superseded_by is None
    assert session.scalar(select(func.count()).select_from(ExperienceVersion)) == 1
    actions = set(session.scalars(select(AuditEvent.action)))
    assert {"experience.save", "join_path.propose", "experience.revoke"} <= actions


def test_experience_conversion_reuses_real_reverse_approved_path_without_creating(state):
    session, actor, owner_a, owner_b = state
    existing = propose(state, left_asset_id="right", right_asset_id="left",
                       left_columns=["key_b", "key_a"], right_columns=["key_b", "key_a"])
    joins.decide(session, owner_a, existing, "domain_a", "approved", None)
    joins.decide(session, owner_b, existing, "domain_b", "approved", None)
    original_source = existing.source.copy()
    saved = save_join_experience(state)
    converted = experiences.to_join_path(session, actor, saved["id"])
    session.commit()
    session.expire_all()
    assert converted.id == existing.id
    assert converted.status == "approved"
    assert converted.source == original_source
    assert session.scalar(select(func.count()).select_from(JoinPath)) == 1
    assert session.get(Experience, saved["id"]).status == "revoked"
    assert session.get(ExperienceVersion, saved["version_id"]).content == saved["content"]
