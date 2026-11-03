"""数据集重跑：真实 SQLite 持久化与问数入口，不连接外部数据源。"""

from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import JSON, MetaData, create_engine, event, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from avelis.analysis import dataset_access, rerun
from avelis.ask import service as ask
from avelis.connectors.base import QueryResult
from avelis.db import utcnow
from avelis.governance.errors import Conflict, Forbidden, Invalid, NotFound
from avelis.lineage.sql import SqlDialect
from avelis.models import AnalysisDataset, AnalysisRun, AnalysisStep, AnalysisTask, Answer, AuditEvent, Datasource, User

REAL_FULFIL = ask.fulfil


@pytest.fixture
def state(monkeypatch):
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for model in (User, AnalysisTask, AnalysisRun, AnalysisDataset, AnalysisStep, Answer, AuditEvent):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        user = User(id="actor", username="actor", display_name="查询用户", password_hash="unused")
        run = AnalysisRun(
            id="run",
            user_id=user.id,
            question="检查变化",
            trigger="manual",
            status="succeeded",
            report={"title": "原报告", "datasets": ["D1"]},
            verification={"ok": True},
            model_adapter="offline-test",
        )
        intent = {
            "metric_code": "metric",
            "metric_codes": ["metric"],
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
            "compare": "previous_period",
        }
        source = AnalysisDataset(
            id="source",
            run_id=run.id,
            seq=1,
            kind="governed",
            title="指标 2026-01-01~2026-01-03 对比上一期",
            columns=["指标"],
            rows=[[1]],
            meta={"intent": intent, "purpose": "检查变化", "versions": {"metric": "old-version"}},
        )
        session.add_all(
            [user, run, source, AnalysisStep(run_id=run.id, seq=1, kind="tool", tool="query_metric", status="ok")]
        )
        session.commit()
        metric = SimpleNamespace(name="指标")
        version = SimpleNamespace(
            id="current-version", kind="base", unit="件", unit_scale=1, expression="SUM(b.amount)"
        )
        catalog = ask.Catalog([], {"metric": (metric, version)})
        calls, commits = [], []
        monkeypatch.setattr(dataset_access, "require_dataset_access", lambda *args: calls.append("access"))

        def visible_catalog(s, actor, *, with_values):
            assert s is session and actor is user and with_values is False
            calls.append("catalog")
            return catalog

        def fulfil(s, actor, intent, answer, visible):
            assert s is session and actor is user and visible is catalog
            calls.append(intent)
            answer.status = "answered"
            answer.intent = intent.model_dump(mode="json")
            answer.result = {"columns": ["value"], "rows": [["4"]], "truncated": False}
            answer.sql = f"SELECT value WHERE day BETWEEN '{intent.start_date}' AND '{intent.end_date}'"
            answer.metric_version_ids = ["current-version"]
            answer.executed_at = utcnow()

        monkeypatch.setattr(ask, "visible_catalog", visible_catalog)
        monkeypatch.setattr(ask, "fulfil", fulfil)
        event.listen(session, "after_commit", lambda s: commits.append(True))
        yield SimpleNamespace(
            session=session, user=user, run=run, source=source, calls=calls, catalog=catalog, commits=commits
        )
    engine.dispose()


WINDOW = {"start": "2026-02-10", "end": "2026-02-12"}


def execute(state, window=None):
    return rerun.rerun_dataset(state.session, state.user, state.run, state.source, window or dict(WINDOW))


def test_previous_period_creates_next_dataset_and_step_without_changing_report(state):
    new = execute(state)
    assert state.calls[:2] == ["access", "catalog"]
    intent = state.calls[-1]
    assert (intent.compare_start, intent.compare_end) == (date(2026, 2, 7), date(2026, 2, 9))
    assert new.seq == 2 and new.kind == "governed" and new.inputs == []
    assert new.title == "指标 2026-02-10~2026-02-12 对比上一期"
    assert new.columns == ["指标"] and new.rows == [["4"]]
    assert new.meta["rerun_of"] == {"run_id": "run", "ref": "D1", "dataset_id": "source"}
    assert new.meta["intent"]["compare_start"] == "2026-02-07"
    assert new.meta["original_intent"] == state.source.meta["intent"]
    assert new.meta["window"] == WINDOW and new.meta["versions"] == {"metric": "current-version"}
    assert new.meta["metric_version_ids"] == ["current-version"]
    assert new.meta["units"] == {"指标": {"kind": "level", "unit": "件", "additive": True}}
    assert new.meta["root_kinds"] == ["governed"] and new.meta["closure"] == []
    assert not new.meta["cached"] and new.meta["executed_at"]
    answer = state.session.get(Answer, new.answer_id)
    assert answer.asked_by == state.user.id and answer.status == "answered" and answer.duration_ms is not None
    step = state.session.scalar(select(AnalysisStep).where(AnalysisStep.seq == 2))
    assert step.kind == "tool" and step.tool == "rerun" and step.dataset_id == new.id
    assert step.args == {"ref": "D1", "window": WINDOW} and step.output["dataset"] == "D2"
    assert state.run.report == {"title": "原报告", "datasets": ["D1"]}
    assert state.run.verification == {"ok": True} and state.run.status == "succeeded"
    assert state.commits == []
    state.session.commit()
    state.session.expire_all()
    assert state.session.get(AnalysisDataset, new.id).meta["rerun_of"]["dataset_id"] == "source"


def test_sequences_follow_maximum_and_repeated_reruns_append(state):
    state.source.seq = 4
    state.session.flush()
    first, second = execute(state), execute(state)
    assert (first.seq, second.seq) == (5, 6)
    assert first.meta["rerun_of"]["ref"] == "D4"
    steps = list(state.session.scalars(select(AnalysisStep).order_by(AnalysisStep.seq)))
    assert [s.seq for s in steps] == [1, 2, 3]


@pytest.mark.parametrize("status", ["queued", "running", "waiting"])
def test_busy_run_is_rejected_after_refreshing_locked_status(state, status):
    state.session.execute(
        update(AnalysisRun)
        .where(AnalysisRun.id == state.run.id)
        .values(status=status)
        .execution_options(synchronize_session=False)
    )
    assert state.run.status == "succeeded"
    with pytest.raises(Conflict):
        execute(state)
    assert state.calls == []


@pytest.mark.parametrize("status", ["failed", "stopped", "clarify"])
def test_other_terminal_statuses_allow_rerun(state, status):
    state.run.status = status
    assert execute(state).seq == 2 and state.run.status == status


def test_only_owner_can_rerun_even_when_another_user_can_read(state):
    other = User(id="another", username="another", display_name="其他用户", password_hash="unused")
    with pytest.raises(NotFound):
        rerun.rerun_dataset(state.session, other, state.run, state.source, WINDOW)
    assert state.calls == []


@pytest.mark.parametrize("kind,intent", [("calc", {"metric_codes": ["metric"]}), ("governed", {})])
def test_only_governed_datasets_with_saved_intent_can_rerun(state, kind, intent):
    state.source.kind, state.source.meta = kind, {"intent": intent}
    with pytest.raises(Conflict):
        execute(state)
    assert state.calls == []


def test_custom_comparison_requires_new_dates_and_replaces_both_ranges(state):
    state.source.meta = {
        **state.source.meta,
        "intent": {
            **state.source.meta["intent"],
            "compare": "custom",
            "compare_start": "2025-12-01",
            "compare_end": "2025-12-03",
        },
    }
    state.source.title = "指标 2026-01-01~2026-01-03 对比2025-12-01~2025-12-03"
    with pytest.raises(Invalid, match="compare_start"):
        execute(state)
    new = execute(state, {**WINDOW, "compare_start": "2025-11-01", "compare_end": "2025-11-03"})
    assert state.calls[-1].compare_start == date(2025, 11, 1)
    assert new.title == "指标 2026-02-10~2026-02-12 对比2025-11-01~2025-11-03"


def test_custom_title_does_not_replace_new_current_window_when_it_matches_old_comparison(state):
    state.source.meta = {
        **state.source.meta,
        "intent": {
            **state.source.meta["intent"],
            "compare": "custom",
            "compare_start": WINDOW["start"],
            "compare_end": WINDOW["end"],
        },
    }
    state.source.title = "指标 2026-01-01~2026-01-03 对比2026-02-10~2026-02-12"
    new = execute(state, {**WINDOW, "compare_start": "2025-11-01", "compare_end": "2025-11-03"})
    assert new.title == "指标 2026-02-10~2026-02-12 对比2025-11-01~2025-11-03"


def test_previous_year_uses_existing_leap_day_rule(state):
    state.source.meta = {
        **state.source.meta,
        "intent": {
            **state.source.meta["intent"],
            "compare": "previous_year",
            "compare_start": "2025-01-01",
            "compare_end": "2025-01-03",
        },
    }
    new = execute(state, {"start": "2028-02-29", "end": "2028-02-29"})
    assert new.meta["intent"]["compare_start"] == new.meta["intent"]["compare_end"] == "2027-02-28"


@pytest.mark.parametrize(
    "window", [{"start": "bad", "end": "2026-02-12"}, {"start": "2026-03-01", "end": "2026-02-12"}]
)
def test_invalid_windows_are_rejected_before_query(state, window):
    with pytest.raises(Invalid):
        execute(state, window)
    assert "catalog" not in state.calls


def test_revoked_historical_source_access_stops_before_catalog(state, monkeypatch):
    def deny(*args):
        raise Forbidden("你已没有该数据的访问权限")

    monkeypatch.setattr(dataset_access, "require_dataset_access", deny)
    with pytest.raises(Forbidden):
        execute(state)
    assert state.calls == []


def test_metric_missing_from_current_catalog_is_forbidden(state):
    state.catalog.versions.clear()
    with pytest.raises(Forbidden, match="你已没有该数据的访问权限"):
        execute(state)
    assert state.session.scalar(select(Answer)) is None


@pytest.mark.parametrize(
    "status,error_code,error", [("denied", "permission", Forbidden), ("error", "db_unavailable", Conflict)]
)
def test_failed_query_keeps_answer_and_audit_after_caller_rollback(state, monkeypatch, status, error_code, error):
    def fail(s, user, intent, answer, catalog):
        answer.status, answer.error_code = status, error_code
        answer.message = "来源权限已撤销" if status == "denied" else "来源执行失败"

    monkeypatch.setattr(ask, "fulfil", fail)
    with pytest.raises(error, match="你已没有该数据的访问权限" if status == "denied" else "来源执行失败"):
        execute(state)
    assert state.commits == [True]
    state.session.rollback()
    with Session(state.session.get_bind()) as session:
        answer = session.scalar(select(Answer))
        assert answer.status == status and answer.error_code == error_code
        assert answer.asked_by == "actor" and answer.duration_ms is not None
        audit = session.scalar(select(AuditEvent))
        assert audit.actor_id == "actor" and audit.action == "ask" and audit.object_type == "answer"
        assert audit.object_id == answer.id
        assert audit.detail["status"] == status and audit.detail["error_code"] == error_code
        assert [dataset.id for dataset in session.scalars(select(AnalysisDataset))] == ["source"]
        steps = list(session.scalars(select(AnalysisStep)))
        assert len(steps) == 1 and steps[0].tool == "query_metric"


def test_partial_subquery_cache_hit_is_recorded(state, monkeypatch):
    original = ask.fulfil

    def cached(*args):
        original(*args)
        answer = args[3]
        answer.cache_hit = False
        answer.plan = {"subqueries": [{"cache_hit": True}, {"cache_hit": False}]}

    monkeypatch.setattr(ask, "fulfil", cached)
    assert execute(state).meta["cached"] is True


def test_real_fulfil_compiles_new_windows_into_cache_keys_and_records_partial_hits(state, monkeypatch):
    """保留真实计划展开、SQL 编译校验和缓存键，只替换来源元数据与连接器。"""
    monkeypatch.setattr(ask, "fulfil", REAL_FULFIL)
    metric, version = state.catalog.versions["metric"]
    metric.code = "metric"
    for name, value in {
        "status": "published",
        "expression": "SUM(b.amount)",
        "time_column": "b.day",
        "timezone": "UTC",
        "dependency_fingerprint": "unchanged",
        "dimensions": [],
        "filters": None,
        "grain": [],
        "joins": [],
        "version_no": 2,
    }.items():
        setattr(version, name, value)
    asset = SimpleNamespace(
        id="asset",
        datasource_id="datasource",
        current_version_id="asset-version",
        catalog_name="catalog",
        db_name="database",
        name="records",
        kind="table",
    )
    datasource = SimpleNamespace(id="datasource")
    resolved = {"aliases": {"b": asset}, "steps": [], "dependency": []}
    monkeypatch.setattr(
        ask,
        "describe_version",
        lambda *args: {
            name: getattr(version, name)
            for name in ("expression", "time_column", "timezone", "filters", "dimensions", "grain", "joins")
        },
    )
    monkeypatch.setattr(ask, "_version_evidence", lambda *args: {"id": version.id})
    monkeypatch.setattr(ask, "dependency_drift", lambda *args: [])
    monkeypatch.setattr(ask, "validate_spec", lambda *args: resolved)
    monkeypatch.setattr(ask, "upstream_closure", lambda *args: {asset.id: asset})
    monkeypatch.setattr(ask.access, "domain_ids", lambda *args: {"domain"})
    monkeypatch.setattr(ask.access, "can_read_asset", lambda *args: True)
    monkeypatch.setattr(ask, "_column_type", lambda *args: "date")
    monkeypatch.setattr(ask, "_columns", lambda *args: ["day", "amount"])
    monkeypatch.setattr(ask, "dialect_of", lambda *args: SqlDialect("mysql", "catalog"))
    monkeypatch.setattr(ask, "_live_check", lambda *args: [])
    monkeypatch.setattr(ask, "freshness_boundary", lambda *args: {})
    monkeypatch.setattr(
        ask, "_asset_evidence", lambda *args: {"asset_id": asset.id, "qualified": "catalog.database.records"}
    )
    monkeypatch.setattr(ask, "_lineage_evidence", lambda *args: [])
    monkeypatch.setattr(ask, "_dimension_docs", lambda *args: {})
    get = state.session.get
    monkeypatch.setattr(
        state.session,
        "get",
        lambda model, key, **kwargs: datasource if model is Datasource else get(model, key, **kwargs),
    )
    monkeypatch.setattr(state.session, "refresh", lambda *args, **kwargs: None)
    cache, executed = {}, []
    monkeypatch.setattr(ask, "_cache_get", lambda key: cache.get(key))
    monkeypatch.setattr(ask, "_cache_put", lambda key, value, *args: cache.__setitem__(key, value))

    def execute_select(sql, **kwargs):
        executed.append(sql)
        return QueryResult(columns=["value"], rows=[[len(executed)]], truncated=False)

    connector = SimpleNamespace(execute_select=execute_select, freshness=lambda *args: {"rows": 10})
    monkeypatch.setattr(ask, "query_connector", lambda *args: connector)
    old = execute(state, {"start": "2026-01-01", "end": "2026-01-03"})
    new = execute(state)
    assert not old.meta["cached"] and not new.meta["cached"]
    assert len(executed) == len(cache) == 4
    assert old.rows[0][0] == "1" and new.rows[0][0] == "3"
    assert "2026-02-10" in executed[2] and "2026-02-12" in executed[2]
    assert "2026-02-07" in executed[3] and "2026-02-09" in executed[3]
    repeated = execute(state)
    assert repeated.meta["cached"] and repeated.rows == new.rows and len(executed) == 4
    partial = execute(state, {"start": "2026-02-13", "end": "2026-02-15"})
    answer = state.session.get(Answer, partial.answer_id)
    assert len(executed) == len(cache) == 5
    assert partial.meta["cached"] and not answer.cache_hit
    assert [q["cache_hit"] for q in answer.plan["subqueries"]] == [False, True]
