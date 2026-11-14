"""档案页说明的读写与模型起草：内存 SQLite，模型用假对象，血缘用桩。"""

from __future__ import annotations

import pytest
from sqlalchemy import JSON, MetaData, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis import modelcfg
from avelis.governance import dossier, lineage_notes
from avelis.governance.errors import Conflict, Forbidden, Invalid, NotFound
from avelis.models import (
    Asset,
    AssetVersion,
    AuditEvent,
    CollectionRun,
    Datasource,
    Domain,
    DomainMember,
    LineageNote,
    User,
    UserRole,
)


class Reply:
    def __init__(self, content):
        self.content = content


class FakeModel:
    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return Reply(self.content)


class FakeResolved:
    adapter = "fake:model-1"

    def __init__(self, model):
        self.model = model

    def safe_error(self, error):
        return "safe"


LINEAGE = {
    "col_x": {
        "column": "col_x",
        "comment": None,
        "relation": "value_computed",
        "expression": "s_a.col_p + s_b.col_q",
        "sources": [
            {"asset_id": None, "ref": "s_a", "column": "col_p"},
            {"asset_id": None, "ref": "s_b", "column": "col_q"},
        ],
        "basis_hash": "h-x1",
    },
    "col_y": {
        "column": "col_y",
        "comment": "注释乙",
        "relation": "value_direct",
        "expression": "s_a.col_r",
        "sources": [{"asset_id": None, "ref": "s_a", "column": "col_r"}],
        "basis_hash": "h-y1",
    },
}


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (
        User,
        UserRole,
        Domain,
        DomainMember,
        Datasource,
        CollectionRun,
        Asset,
        AssetVersion,
        LineageNote,
        AuditEvent,
    ):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    lineage = {k: dict(v) for k, v in LINEAGE.items()}
    monkeypatch.setattr(dossier, "field_lineage", lambda session, asset: list(lineage.values()))
    state = {"model": FakeModel('{"summary": "整表说明", "fields": {"col_x": "说明甲", "col_y": "说明乙"}}')}
    monkeypatch.setattr(
        modelcfg, "resolve", lambda session, purpose, **kw: state.get("resolved", FakeResolved(state["model"]))
    )
    monkeypatch.setattr(modelcfg, "policy", lambda session: {"send_results": True, "send_code": True})
    with Session(engine, expire_on_commit=False) as session:
        users = {
            name: User(id=name, username=name, display_name=f"{name}名", password_hash="x")
            for name in ("admin", "owner", "maint", "member", "steward", "plain")
        }
        session.add_all(users.values())
        session.add_all(
            [
                UserRole(user_id="admin", role="admin"),
                UserRole(user_id="steward", role="steward"),
                UserRole(user_id="plain", role="analyst"),
                Domain(id="dom", code="dom", name="域", owner_id="owner"),
                DomainMember(domain_id="dom", user_id="owner", role="owner"),
                DomainMember(domain_id="dom", user_id="maint", role="maintainer"),
                DomainMember(domain_id="dom", user_id="member", role="viewer"),
                Datasource(id="ds", name="源", kind="fake", host="h", port=1),
            ]
        )
        asset = Asset(
            id="asset", datasource_id="ds", kind="table", catalog_name="c", db_name="d", name="t_a", domain_id="dom"
        )
        loose = Asset(id="loose", datasource_id="ds", kind="table", catalog_name="c", db_name="d", name="t_b")
        session.add_all([asset, loose])
        session.flush()
        yield type(
            "Env",
            (),
            {"session": session, "users": users, "asset": asset, "loose": loose, "state": state, "lineage": lineage},
        )
    engine.dispose()


def set_comment(env, comment):
    version = AssetVersion(
        id="v1",
        asset_id="asset",
        version_no=1,
        observed_run_id="r",
        last_seen_run_id="r",
        kind="table",
        catalog_name="c",
        db_name="d",
        name="t_a",
        comment=comment,
        content_hash="x",
    )
    env.session.add(version)
    env.asset.current_version_id = "v1"
    env.session.flush()


def test_can_edit_identities(env):
    s, u = env.session, env.users
    assert lineage_notes.can_edit(s, u["admin"], env.asset)
    assert lineage_notes.can_edit(s, u["owner"], env.asset)
    assert lineage_notes.can_edit(s, u["maint"], env.asset)
    assert not lineage_notes.can_edit(s, u["member"], env.asset)
    assert not lineage_notes.can_edit(s, u["steward"], env.asset)
    assert lineage_notes.can_edit(s, u["steward"], env.loose)
    assert lineage_notes.can_edit(s, u["admin"], env.loose)
    assert not lineage_notes.can_edit(s, u["plain"], env.loose)
    assert not lineage_notes.can_edit(s, u["owner"], env.loose)


def test_model_available(env, monkeypatch):
    assert lineage_notes.model_available(env.session)
    monkeypatch.setattr(modelcfg, "policy", lambda session: {"send_code": False})
    assert not lineage_notes.model_available(env.session)
    monkeypatch.setattr(modelcfg, "policy", lambda session: {"send_code": True})
    monkeypatch.setattr(modelcfg, "resolve", lambda session, purpose, **kw: None)
    assert not lineage_notes.model_available(env.session)


def test_save_creates_confirmed_human_note_and_audits(env):
    out = lineage_notes.save(env.session, env.users["owner"], env.asset, "col_x", "  自己写的  ")
    assert out["text"] == "自己写的"
    assert out["status"] == "confirmed"
    assert out["origin"] == "human"
    assert out["column"] == "col_x"
    assert out["confirmed_by"] == {"id": "owner", "display_name": "owner名"}
    assert out["confirmed_at"] and out["updated_at"]
    note = env.session.scalar(select(LineageNote))
    assert note.basis_hash == "h-x1"
    event = env.session.scalar(select(AuditEvent).where(AuditEvent.action == "lineage_note.confirm"))
    assert event.object_id == "asset" and event.actor_id == "owner"


def test_save_table_note_uses_none_column_and_no_basis(env):
    out = lineage_notes.save(env.session, env.users["admin"], env.asset, "", "整表")
    assert out["column"] is None
    assert env.session.scalar(select(LineageNote)).basis_hash is None


def test_save_confirms_draft_keeps_model_origin_and_rewrite_is_human(env):
    lineage_notes.generate(env.session, env.users["owner"], env.asset, None, summary=False)
    draft = lineage_notes.notes_for(env.session, env.asset, {"col_x": "h-x1", "col_y": "h-y1"})["col_x"]
    assert draft["status"] == "draft"
    confirmed = lineage_notes.save(env.session, env.users["maint"], env.asset, "col_x", "说明甲")
    assert confirmed["status"] == "confirmed" and confirmed["origin"] == "model"
    assert confirmed["confirmed_by"]["id"] == "maint"
    rewritten = lineage_notes.save(env.session, env.users["maint"], env.asset, "col_x", "改过的说明")
    assert rewritten["origin"] == "human"
    assert env.session.scalars(select(LineageNote).where(LineageNote.column_name == "col_x")).all().__len__() == 1


def test_save_rejects_confirmation_of_a_basis_the_user_did_not_see(env):
    """issue 136：页面上看到的是来历 h-x1，确认前来源变成 h-x2 -> 冲突，旧说明不被绑定到新来源。"""
    lineage_notes.generate(env.session, env.users["owner"], env.asset, None, summary=False)
    seen = lineage_notes.notes_for(env.session, env.asset, {"col_x": "h-x1", "col_y": "h-y1"})["col_x"]
    env.lineage["col_x"]["basis_hash"] = "h-x2"
    with pytest.raises(Conflict):
        lineage_notes.save(
            env.session,
            env.users["owner"],
            env.asset,
            "col_x",
            seen["text"],
            expected_basis_hash="h-x1",
            expected_updated_at=seen["updated_at"],
        )
    note = env.session.scalar(select(LineageNote).where(LineageNote.column_name == "col_x"))
    assert (note.status, note.basis_hash) == ("draft", "h-x1")
    assert lineage_notes.notes_for(env.session, env.asset, {"col_x": "h-x2"})["col_x"]["status"] == "stale"
    # 刷新后看到新来历，主动重新确认同一句话是允许的
    fresh = lineage_notes.notes_for(env.session, env.asset, {"col_x": "h-x2"})["col_x"]
    out = lineage_notes.save(
        env.session,
        env.users["owner"],
        env.asset,
        "col_x",
        seen["text"],
        expected_basis_hash="h-x2",
        expected_updated_at=fresh["updated_at"],
    )
    assert out["status"] == "confirmed"
    assert env.session.scalar(select(LineageNote).where(LineageNote.column_name == "col_x")).basis_hash == "h-x2"


def test_save_rejects_overwriting_a_note_changed_since_the_page_loaded(env):
    first = lineage_notes.save(env.session, env.users["owner"], env.asset, "col_x", "第一版")
    # 旧页面当时还没有说明（None），或拿着更早的修订时间：都不能覆盖别人刚保存的说明
    for stale in (None, "2000-01-01T00:00:00"):
        with pytest.raises(Conflict):
            lineage_notes.save(env.session, env.users["maint"], env.asset, "col_x", "旧改写", expected_updated_at=stale)
    assert env.session.scalar(select(LineageNote)).text == "第一版"
    out = lineage_notes.save(
        env.session, env.users["maint"], env.asset, "col_x", "第二版", expected_updated_at=first["updated_at"]
    )
    assert out["text"] == "第二版"
    # 整张表的说明同样校验修订；不传期望值的旧客户端不校验
    lineage_notes.save(env.session, env.users["admin"], env.asset, "", "整表", expected_updated_at=None)
    with pytest.raises(Conflict):
        lineage_notes.save(env.session, env.users["admin"], env.asset, "", "整表二", expected_updated_at=None)
    assert lineage_notes.save(env.session, env.users["admin"], env.asset, "", "整表三")["text"] == "整表三"


def test_save_rejects_bad_input_and_permissions(env):
    with pytest.raises(NotFound):
        lineage_notes.save(env.session, env.users["owner"], env.asset, "col_none", "说明")
    with pytest.raises(Invalid):
        lineage_notes.save(env.session, env.users["owner"], env.asset, "col_x", "   ")
    with pytest.raises(Invalid):
        lineage_notes.save(env.session, env.users["owner"], env.asset, "col_x", "字" * (lineage_notes.MAX_TEXT + 1))
    lineage_notes.save(env.session, env.users["owner"], env.asset, "col_x", "字" * lineage_notes.MAX_TEXT)
    with pytest.raises(Forbidden, match="需要这张表所在业务域的负责人或维护者身份"):
        lineage_notes.save(env.session, env.users["member"], env.asset, "col_x", "说明")
    with pytest.raises(Forbidden):
        lineage_notes.save(env.session, env.users["plain"], env.loose, "", "说明")


def test_notes_for_marks_stale_but_not_table_note(env):
    lineage_notes.save(env.session, env.users["owner"], env.asset, "col_x", "说明甲")
    lineage_notes.save(env.session, env.users["owner"], env.asset, "col_y", "说明乙")
    lineage_notes.save(env.session, env.users["owner"], env.asset, "", "整表")
    notes = lineage_notes.notes_for(env.session, env.asset, {"col_x": "h-x1", "col_y": "changed"})
    assert notes["col_x"]["status"] == "confirmed"
    assert notes["col_y"]["status"] == "stale"
    assert notes[""]["status"] == "confirmed" and notes[""]["column"] is None
    # 字段已没有来历
    gone = lineage_notes.notes_for(env.session, env.asset, {"col_y": "h-y1"})
    assert gone["col_x"]["status"] == "stale"
    assert gone[""]["status"] == "confirmed"


def test_generate_skipped_reasons(env, monkeypatch):
    s, owner = env.session, env.users["owner"]
    monkeypatch.setattr(modelcfg, "policy", lambda session: {"send_code": False})
    assert lineage_notes.generate(s, owner, env.asset, None, summary=True) == {"notes": [], "skipped": "policy"}
    monkeypatch.setattr(modelcfg, "policy", lambda session: {"send_code": True})
    monkeypatch.setattr(modelcfg, "resolve", lambda session, purpose, **kw: None)
    assert lineage_notes.generate(s, owner, env.asset, None, summary=True) == {"notes": [], "skipped": "no_model"}
    monkeypatch.setattr(modelcfg, "resolve", lambda session, purpose, **kw: FakeResolved(env.state["model"]))
    for column in ("col_x", "col_y"):
        lineage_notes.save(s, owner, env.asset, column, "已确认")
    lineage_notes.save(s, owner, env.asset, "", "整表")
    assert lineage_notes.generate(s, owner, env.asset, None, summary=True) == {"notes": [], "skipped": "nothing"}
    assert env.state["model"].calls == []
    with pytest.raises(Forbidden):
        lineage_notes.generate(s, env.users["member"], env.asset, None, summary=True)


def test_generate_writes_drafts_and_audit(env):
    out = lineage_notes.generate(env.session, env.users["owner"], env.asset, None, summary=True)
    assert out["skipped"] is None
    by_column = {n["column"]: n for n in out["notes"]}
    assert set(by_column) == {None, "col_x", "col_y"}
    assert all(n["status"] == "draft" and n["origin"] == "model" and n["confirmed_by"] is None for n in out["notes"])
    stored = {n.column_name: n for n in env.session.scalars(select(LineageNote))}
    assert stored["col_x"].basis_hash == "h-x1" and stored["col_x"].model_adapter == "fake:model-1"
    assert stored[""].basis_hash is None
    event = env.session.scalar(select(AuditEvent).where(AuditEvent.action == "lineage_note.generate"))
    assert event.detail["fields"] == 2 and event.detail["model_adapter"] == "fake:model-1"


def test_generate_does_not_overwrite_confirmed_but_overwrites_stale_and_drafts(env):
    s, owner = env.session, env.users["owner"]
    lineage_notes.save(s, owner, env.asset, "col_x", "人写的")
    lineage_notes.save(s, owner, env.asset, "col_y", "旧说明")
    env.lineage["col_y"]["basis_hash"] = "h-y2"  # col_y 的来历变了 -> 过期
    out = lineage_notes.generate(s, owner, env.asset, None, summary=False)
    assert [n["column"] for n in out["notes"]] == ["col_y"]
    notes = {n.column_name: n for n in s.scalars(select(LineageNote))}
    assert notes["col_x"].text == "人写的" and notes["col_x"].status == "confirmed"
    y = notes["col_y"]
    assert (y.text, y.status, y.origin, y.basis_hash) == ("说明乙", "draft", "model", "h-y2")
    assert y.confirmed_by is None and y.confirmed_at is None and y.model_adapter == "fake:model-1"
    # 草稿再次生成会被覆盖
    env.state["model"].content = '{"fields": {"col_y": "新说明乙"}}'
    lineage_notes.generate(s, owner, env.asset, ["col_y"], summary=False)
    assert s.scalar(select(LineageNote.text).where(LineageNote.column_name == "col_y")) == "新说明乙"


def test_generate_discards_out_of_scope_and_oversized_and_tolerates_fence(env):
    env.state["model"].content = (
        '```json\n{"summary": "整表", "fields": {"col_x": "说明甲", "col_other": "越界", "col_y": "'
        + "字" * (lineage_notes.MAX_TEXT + 1)
        + '"}}\n```'
    )
    out = lineage_notes.generate(env.session, env.users["owner"], env.asset, None, summary=True)
    assert {n["column"] for n in out["notes"]} == {None, "col_x"}
    assert env.session.scalar(select(LineageNote).where(LineageNote.column_name == "col_other")) is None


def test_generate_with_columns_limits_scope(env):
    env.state["model"].content = '{"fields": {"col_x": "甲", "col_y": "乙"}}'
    out = lineage_notes.generate(env.session, env.users["owner"], env.asset, ["col_x"], summary=False)
    assert [n["column"] for n in out["notes"]] == ["col_x"]


def test_generate_redacts_expression_and_prompt_is_data_only(env, monkeypatch):
    env.lineage["col_x"]["expression"] = "CASE WHEN s_a.col_p = 'secret-value' THEN 1 END"
    monkeypatch.setattr(lineage_notes, "redact", lambda text: (text.replace("secret-value", "<redacted>"), 1))
    lineage_notes.generate(env.session, env.users["owner"], env.asset, None, summary=True)
    sent = "\n".join(str(m.content) for m in env.state["model"].calls[0])
    assert "secret-value" not in sent and "<redacted>" in sent
    assert "t_a" in sent and "col_p" in sent


def test_generate_skips_summary_when_table_has_comment(env):
    set_comment(env, "数据库表注释")
    out = lineage_notes.generate(env.session, env.users["owner"], env.asset, None, summary=True)
    assert {n["column"] for n in out["notes"]} == {"col_x", "col_y"}
    payload = env.state["model"].calls[0][1].content
    assert '"need_summary": false' in payload


def test_generate_model_failure_becomes_invalid(env, caplog):
    env.state["model"].error = RuntimeError("boom sk-123")
    with pytest.raises(Invalid, match="模型没有返回可用的说明，请稍后再试"):
        lineage_notes.generate(env.session, env.users["owner"], env.asset, None, summary=True)
    assert "sk-123" not in caplog.text
    env.state["model"].error = None
    env.state["model"].content = "这不是 JSON"
    with pytest.raises(Invalid):
        lineage_notes.generate(env.session, env.users["owner"], env.asset, None, summary=True)
    env.state["model"].content = '{"fields": {"col_other": "越界"}}'
    with pytest.raises(Invalid):
        lineage_notes.generate(env.session, env.users["owner"], env.asset, None, summary=False)
    assert env.session.scalars(select(LineageNote)).all() == []
