"""批次接口用内存会话打桩验证，不创建数据库连接。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from avelis.api import imports
from avelis.api.deps import current_user, get_session
from avelis.models import (
    AppSetting,
    CandidateEdge,
    Datasource,
    ExtractedStatement,
    ImportBatch,
    ImportJob,
    LineageEdge,
    User,
)

NOW = datetime(2026, 11, 1, tzinfo=UTC)


def batch_view(status="done"):
    return {"id": "batch-1", "datasource_id": "ds-1", "status": status, "created_at": NOW.isoformat()}


@pytest.fixture
def allowed(monkeypatch):
    monkeypatch.setattr(imports.service, "require_import_access", lambda *_: None)


def client_for(session):
    app = FastAPI()
    app.include_router(imports.router)
    app.dependency_overrides[current_user] = lambda: User(id="actor-1")
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


class PageSession:
    def __init__(self, items=(), *, scalars=(2, 3), grouped=(("parsed", 3), ("failed", 1))):
        self.items = list(items)
        self.counts = iter(scalars)
        self.grouped = grouped
        self.queries = []
        self.batch = ImportBatch(id="batch-1", datasource_id="ds-1", status="done", options={})

    def get(self, entity, object_id, **kwargs):
        if entity is ImportBatch:
            return self.batch
        if entity is ImportJob:
            return ImportJob(id=object_id, batch_id="batch-1")
        raise AssertionError(entity)

    def execute(self, query):
        self.queries.append(query)
        return SimpleNamespace(all=lambda: list(self.grouped))

    def scalar(self, query):
        self.queries.append(query)
        return next(self.counts)

    def scalars(self, query):
        self.queries.append(query)
        return self.items


def test_statement_page_filters_before_pagination_and_counts_pending_review(allowed):
    statement = ExtractedStatement(id="stmt-1", job_id="file-1", seq=9, line_start=21, line_end=22,
                                   method="sql", status="parsed",
                                   raw_text="INSERT INTO dw.daily_summary SELECT id FROM src.events",
                                   sql_text=None, placeholders=[], writes=[], text_hash="digest")
    session = PageSession([statement])
    with client_for(session) as client:
        response = client.get("/api/imports/batch-1/statements", params={
            "file_id": "file-1", "status": "pending_review", "q": "events%_", "offset": 7, "limit": 2})
    assert response.status_code == 200, response.text
    page = response.json()
    assert (page["total"], page["offset"], page["limit"]) == (3, 7, 2)
    assert page["counts"] == {"parsed": 3, "failed": 1, "pending_review": 2}
    assert page["items"][0]["file_id"] == "file-1" and page["items"][0]["lines"] == [21, 22]
    final = session.queries[-1].compile(dialect=postgresql.dialect())
    assert "EXISTS" in str(final) and "candidate_edge.status" in str(final)
    assert "LIMIT" in str(final) and "OFFSET" in str(final)
    assert "events/%/_" in final.params.values()  # 搜索中的 LIKE 通配符按字面匹配。
    assert 7 in final.params.values() and 2 in final.params.values()
    assert "count(" in str(session.queries[0]).lower()


@pytest.mark.parametrize("params", [{"offset": -1}, {"limit": 0}, {"limit": 501}, {"status": "unknown"}])
def test_statement_page_rejects_invalid_filters(allowed, params):
    with client_for(PageSession()) as client:
        assert client.get("/api/imports/batch-1/statements", params=params).status_code == 422


def candidate(candidate_id="candidate-1", *, status="pending", origin="model"):
    return CandidateEdge(id=candidate_id, job_id="file-1", statement_id="stmt-1", status=status, origin=origin,
                         source_ref="default_catalog.src.events", source_column="id",
                         target_ref="default_catalog.dw.daily_summary", target_column="id", relation="value_direct",
                         evidence={}, modified={}, created_at=NOW,
                         lineage_edge_id=f"{candidate_id}-edge" if status == "written" else None)


def test_candidate_page_filters_and_expands_folded_hops(allowed):
    item = candidate(origin="folded", status="written")
    item.lineage_edge_id = "folded-1"
    edge = LineageEdge(id="folded-1", attributes={"via": ["default_catalog.tmp.stage"], "hops": ["hop-1", "hop-2"]})
    hops = [LineageEdge(id="hop-1", source_ref="default_catalog.src.events", source_column="id",
                         target_ref="default_catalog.tmp.stage", target_column="id", relation="value_direct"),
            LineageEdge(id="hop-2", source_ref="default_catalog.tmp.stage", source_column="id",
                         target_ref="default_catalog.dw.daily_summary", target_column="id", relation="value_direct")]
    session = PageSession(scalars=(1,))
    results = iter([[item], [edge], hops])

    def scalars(query):
        session.queries.append(query)
        return next(results)

    session.scalars = scalars
    with client_for(session) as client:
        response = client.get("/api/imports/batch-1/candidates", params={
            "file_id": "file-1", "status": "written", "origin": "folded", "statement_id": "stmt-1", "limit": 1})
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 1 and len(page["items"]) == 1
    assert page["items"][0]["via"] == ["default_catalog.tmp.stage"]
    assert [hop["id"] for hop in page["items"][0]["hops"]] == ["hop-1", "hop-2"]
    assert page["items"][0]["hops"][1]["target_ref"] == "default_catalog.dw.daily_summary"
    params = session.queries[1].compile().params
    assert {"file-1", "written", "folded", "stmt-1"}.issubset(set(params.values()))


@pytest.mark.parametrize("endpoint", ["statements", "candidates", "files/file-1/content"])
def test_file_from_another_batch_is_rejected_before_loading_content(allowed, endpoint):
    session = PageSession()
    previous_get = session.get
    session.get = lambda entity, object_id: (ImportJob(id=object_id, batch_id="another-batch")
                                             if entity is ImportJob else previous_get(entity, object_id))
    with client_for(session) as client:
        response = client.get(f"/api/imports/batch-1/{endpoint}", params={"file_id": "file-1"})
    assert response.status_code == 404
    assert session.queries == []


class DecisionSession(PageSession):
    def __init__(self, candidates, *, job_status="done", batch_status="needs_review"):
        super().__init__(candidates, scalars=(None,))
        self.job_status = job_status
        self.batch.status = batch_status
        self.commit = Mock()

    def get(self, entity, object_id, **kwargs):
        if entity is CandidateEdge:
            return next((item for item in self.items if item.id == object_id), None)
        if entity is LineageEdge:
            return LineageEdge(id=object_id, status="active")
        if entity is ImportJob:
            return ImportJob(id=object_id, batch_id="batch-1", datasource_id="ds-1", status=self.job_status)
        return super().get(entity, object_id)


@pytest.mark.parametrize("status,decision,missing,job_status", [
    ("accepted", "reject", False, "done"), ("written", "accept", False, "done"),
    ("pending", "accept", True, "done"), ("pending", "reject", False, "superseded"),
])
def test_bulk_decision_prechecks_every_id_without_partial_writes(
        allowed, monkeypatch, status, decision, missing, job_status):
    items = [candidate("valid-1"), candidate("invalid-1", status=status)]
    session = DecisionSession(items[:1] if missing else items, job_status=job_status)
    perform = Mock()
    monkeypatch.setattr(imports.service, "decide", perform)
    with client_for(session) as client:
        response = client.post("/api/candidates/decisions", json={
            "ids": ["valid-1", "invalid-1"], "decision": decision, "reason": "核对原文"})
    assert response.status_code == 409, response.text
    problems = response.json()["detail"]["candidates"]
    assert "invalid-1" in [problem["id"] for problem in problems]
    perform.assert_not_called()
    session.commit.assert_not_called()


def test_bulk_decision_deduplicates_ids_and_uses_one_transaction(allowed, monkeypatch):
    items = [candidate("candidate-1"), candidate("candidate-2", status="written", origin="parsed")]
    session = DecisionSession(items)
    # 所有引用批次的状态刷新由服务负责，这里只验证接口去重和事务边界。
    perform = Mock(side_effect=lambda *_: setattr(session.batch, "status", "done"))
    monkeypatch.setattr(imports.service, "decide", perform)
    with client_for(session) as client:
        response = client.post("/api/candidates/decisions", json={
            "ids": ["candidate-2", "candidate-1", "candidate-2"], "decision": "reject", "reason": "核对原文"})
    assert response.status_code == 200, response.text
    assert response.json() == {"ids": ["candidate-2", "candidate-1"], "decision": "reject", "processed": 2}
    assert [call.args[2].id for call in perform.call_args_list] == ["candidate-2", "candidate-1"]
    assert all(call.args[3:] == ("reject", "核对原文", {}) for call in perform.call_args_list)
    locks = [str(query) for query in session.queries if "FOR UPDATE" in str(query)]
    assert "datasource" in locks[0] and "candidate_edge" in locks[1]
    session.commit.assert_not_called()
    assert session.batch.status == "done"


@pytest.mark.parametrize("decision", ["accept", "reject"])
@pytest.mark.parametrize("ids", [["model", "raw"], ["raw", "model"]])
def test_bulk_decision_rejects_intermediate_without_partial_writes(allowed, monkeypatch, decision, ids):
    items = [candidate("model"), candidate("raw", status="intermediate", origin="parsed")]
    session = DecisionSession(items)
    perform = Mock()
    monkeypatch.setattr(imports.service, "decide", perform)
    with client_for(session) as client:
        response = client.post("/api/candidates/decisions", json={
            "ids": ids, "decision": decision, "reason": "核对原文"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["candidates"] == [
        {"id": "raw", "reason": "中间段只用于查证，请处理折叠后的边"}]
    perform.assert_not_called()
    assert [item.status for item in items] == ["pending", "intermediate"]
    session.commit.assert_not_called()


@pytest.mark.parametrize("decision", ["accept", "reject", "modify"])
def test_single_decision_rejects_intermediate_without_writes(allowed, monkeypatch, decision):
    item = candidate(status="intermediate", origin="parsed")
    session = DecisionSession([item])
    session.scalar = Mock(return_value=item)
    perform = Mock()
    monkeypatch.setattr(imports.service, "decide", perform)
    with client_for(session) as client:
        response = client.post("/api/candidates/candidate-1/decision", json={
            "decision": decision, "reason": "核对原文"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "中间段只用于查证，请处理折叠后的边"
    perform.assert_not_called()
    assert item.status == "intermediate"
    session.commit.assert_not_called()


@pytest.mark.parametrize("edge_status", [None, "intermediate", "superseded"])
def test_bulk_decision_prechecks_written_edge_status_without_partial_writes(allowed, monkeypatch, edge_status):
    items = [candidate("model"), candidate("parsed", status="written", origin="parsed")]
    session = DecisionSession(items)
    original_get = session.get

    def get(entity, object_id, **kwargs):
        if entity is LineageEdge:
            return LineageEdge(id=object_id, status=edge_status) if edge_status is not None else None
        return original_get(entity, object_id, **kwargs)

    session.get = get
    perform = Mock()
    monkeypatch.setattr(imports.service, "decide", perform)
    with client_for(session) as client:
        response = client.post("/api/candidates/decisions", json={
            "ids": ["model", "parsed"], "decision": "reject", "reason": "核对原文"})
    assert response.status_code == 409, response.text
    problems = response.json()["detail"]["candidates"]
    assert len(problems) == 1 and problems[0]["id"] == "parsed"
    if edge_status == "intermediate":
        assert problems[0]["reason"] == "中间段只用于查证，请处理折叠后的边"
    perform.assert_not_called()
    assert [item.status for item in items] == ["pending", "written"]
    session.commit.assert_not_called()


@pytest.mark.parametrize("wait,expected", [(False, 202), (True, 200)])
def test_create_and_params_use_batch_response_and_wait_status(allowed, monkeypatch, wait, expected):
    created = Mock(return_value=batch_view())
    monkeypatch.setattr(imports, "_created", created)
    rewritten = SimpleNamespace(id="batch-2")
    monkeypatch.setattr(imports.batch, "set_params", Mock(return_value=rewritten))
    monkeypatch.setattr(imports, "_run", lambda *_args, **_kwargs: {**batch_view(), "id": "batch-2"})
    with client_for(PageSession()) as client:
        response = client.post(f"/api/imports?wait={str(wait).lower()}", json={
            "datasource_id": "ds-1", "filename": "source.sql", "content": "SELECT id FROM src.events"})
        params = client.put(f"/api/imports/batch-1/params?wait={str(wait).lower()}", json={"params": {"db": "src"}})
    assert response.status_code == expected, response.text
    assert params.status_code == expected and params.json()["id"] == "batch-2"
    assert created.call_args.kwargs["wait"] is wait
    assert created.call_args.kwargs["model_assist"] == {"failed_sql": False, "code": False}
    assert "statements" not in response.json() and "candidates" not in response.json()


@pytest.mark.parametrize("field", ["files", "files[]"])
def test_upload_multiple_files_and_json_options(allowed, monkeypatch, field):
    created = Mock(return_value=batch_view())
    monkeypatch.setattr(imports, "_created", created)
    with client_for(object()) as client:
        response = client.post("/api/imports/upload?wait=true", data={
            "datasource_id": "ds-1", "model_assist": '{"code":true}', "sql_functions": '["run_sql"]'},
            files=[(field, ("a.sql", b"SELECT 1", "text/plain")),
                   (field, ("b.py", b'run_sql("SELECT 2")', "text/plain"))])
    assert response.status_code == 200, response.text
    options = created.call_args.kwargs
    assert [item.source_key for item in options["files"]] == ["a.sql", "b.py"]
    assert options["sql_functions"] == ["run_sql"] and options["model_assist"] == {"code": True, "failed_sql": False}


class SettingSession:
    def __init__(self):
        self.setting = None
        self.queries = []

    def get(self, entity, object_id, **kwargs):
        if entity is Datasource:
            return Datasource(id=object_id)
        assert entity is AppSetting
        return self.setting

    def scalar(self, query):
        self.queries.append(query)
        return "ds-1"

    def add(self, item):
        self.setting = item

    def flush(self):
        pass


def test_sql_functions_saved_outside_datasource_options_and_deduplicated(allowed, monkeypatch):
    monkeypatch.setattr(imports.audit, "record", Mock())
    session = SettingSession()
    with client_for(session) as client:
        changed = client.put("/api/imports/sql-functions/ds-1",
                             json={"sql_functions": ["run_sql", "run_sql", "exec_query"]})
        loaded = client.get("/api/imports/sql-functions/ds-1")
        invalid = client.put("/api/imports/sql-functions/ds-1", json={"sql_functions": ["a()"]})
    assert changed.status_code == loaded.status_code == 200
    assert changed.json() == loaded.json() == {"datasource_id": "ds-1", "sql_functions": ["run_sql", "exec_query"]}
    assert session.setting.key == "import.sql_functions:ds-1"
    assert session.setting.value == {"sql_functions": ["run_sql", "exec_query"]}
    assert imports._sql_functions(session, "ds-1", ["run_sql", "batch_sql"]) == ["run_sql", "exec_query", "batch_sql"]
    assert invalid.status_code == 422


@pytest.mark.parametrize("wait,created,event", [(False, True, "wake"), (True, True, "wait"), (True, False, "wait")])
def test_batch_is_committed_before_worker_is_awakened_or_executed(monkeypatch, wait, created, event):
    events = []
    session = SimpleNamespace(commit=lambda: events.append("commit"), expire_all=lambda: events.append("expire"))
    imported = ImportBatch(id="batch-1", job_id="worker-1")
    monkeypatch.setattr(imports.jobs, "_wake", lambda job_id: events.append("wake"))
    monkeypatch.setattr(imports.jobs, "run_and_wait", lambda job_id, timeout: events.append("wait"))
    monkeypatch.setattr(imports, "get_settings", lambda: SimpleNamespace(worker_running_timeout_seconds=20))
    monkeypatch.setattr(imports.batch, "batch_view", lambda *_: batch_view())
    result = imports._run(session, imported, wait=wait, created=created)
    assert events == ["commit", event, "expire"]
    assert result["duplicate"] is (not created)


def test_create_merges_saved_execution_functions_before_enqueuing(allowed, monkeypatch):
    session = SettingSession()
    session.setting = AppSetting(key="import.sql_functions:ds-1", value={"sql_functions": ["saved_sql"]})
    create = Mock(return_value=(ImportBatch(id="batch-1"), True))
    monkeypatch.setattr(imports.batch, "create_batch", create)
    monkeypatch.setattr(imports, "_run", lambda *_args, **_kwargs: batch_view())
    imports._created(session, User(id="actor-1"), datasource_id="ds-1", filename="source.py", source_type="python",
                     content='run_sql("SELECT id FROM src.events")', default_db="src", params={},
                     model_assist={"failed_sql": False, "code": False}, sql_functions=["run_sql"])
    options = create.call_args.kwargs
    assert options["sql_functions"] == ["saved_sql", "run_sql"]
    assert options["files"][0].language == "python" and options["files"][0].source_key == "source.py"


@pytest.mark.parametrize("functions", ['"run_sql"', '["a()"]', "not-json"])
def test_upload_invalid_execution_functions_return_422(allowed, functions):
    with client_for(object()) as client:
        response = client.post("/api/imports/upload", data={"datasource_id": "ds-1", "sql_functions": functions},
                               files={"file": ("a.sql", b"SELECT 1", "text/plain")})
    assert response.status_code == 422


def test_json_invalid_execution_function_is_an_input_error(allowed):
    with client_for(object()) as client:
        response = client.post("/api/imports", json={"datasource_id": "ds-1", "content": "SELECT 1",
                                                     "sql_functions": ["a()"]})
    assert response.status_code == 422


def test_bulk_decision_refreshes_current_revision_after_datasource_lock(allowed, monkeypatch):
    session = DecisionSession([candidate()])
    original = session.get
    refreshed = []

    def get(entity, object_id, **kwargs):
        if entity is ImportJob and kwargs.get("populate_existing"):
            refreshed.append(object_id)
            return ImportJob(id=object_id, datasource_id="ds-1", batch_id="batch-1", status="superseded")
        return original(entity, object_id, **kwargs)

    session.get = get
    perform = Mock()
    monkeypatch.setattr(imports.service, "decide", perform)
    with client_for(session) as client:
        response = client.post("/api/candidates/decisions", json={
            "ids": ["candidate-1"], "decision": "reject", "reason": "核对原文"})
    assert response.status_code == 409 and refreshed == ["file-1"]
    perform.assert_not_called()
    locks = [query for query in session.queries if "FOR UPDATE" in str(query)]
    assert "datasource" in str(locks[0]) and "candidate_edge" in str(locks[1])
    assert locks[1].get_execution_options()["populate_existing"] is True


def test_visible_files_follow_upload_order_including_duplicates():
    imported = ImportBatch(options={"file_order": ["new-file", "duplicate-file", "new-file"]})
    files = [ImportJob(id="duplicate-file", created_at=NOW, stats={"upload_order": 0}),
             ImportJob(id="reserved-file", created_at=NOW, stats={"duplicate_of": "duplicate-file"}),
             ImportJob(id="new-file", created_at=NOW, stats={"upload_order": 1}),
             ImportJob(id="duplicate-file", created_at=NOW, stats={"upload_order": 0})]
    assert [item.id for item in imports._ordered_files(imported, files)] == ["new-file", "duplicate-file"]


def test_list_reconciles_each_batch_before_projecting_status(allowed, monkeypatch):
    imported = [ImportBatch(id="batch-1", status="running", options={}, stats={}, progress={}, created_at=NOW),
                ImportBatch(id="batch-2", status="done", options={}, stats={}, progress={}, created_at=NOW)]
    session = PageSession()
    results = iter([imported, []])
    session.scalars = lambda _query: next(results)

    def reconcile(current_session, item):
        assert current_session is session
        if item.id == "batch-1":
            item.status = "failed"

    reconciled = Mock(side_effect=reconcile)
    monkeypatch.setattr(imports.batch, "reconcile_batch", reconciled)
    with client_for(session) as client:
        response = client.get("/api/imports")
    assert response.status_code == 200, response.text
    assert [(item["id"], item["status"]) for item in response.json()] == [
        ("batch-1", "failed"), ("batch-2", "done")]
    assert [call.args[1].id for call in reconciled.call_args_list] == ["batch-1", "batch-2"]
