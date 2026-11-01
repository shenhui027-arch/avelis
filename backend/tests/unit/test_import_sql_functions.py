"""SQL 函数规则的原子追加与全量替换回归。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier, Lock
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from avelis.api import imports
from avelis.api.deps import current_user, get_session
from avelis.i18n import locale_context
from avelis.models import AppSetting, User
from tests.unit.test_import_api_batches import SettingSession, allowed, client_for  # noqa: F401


def test_append_preserves_saved_rules_and_is_idempotent(allowed, monkeypatch):
    record = Mock()
    monkeypatch.setattr(imports.audit, "record", record)
    session = SettingSession()
    session.setting = AppSetting(key="import.sql_functions:ds-1", value={"sql_functions": ["legacy.execute"]})
    with client_for(session) as client:
        added = client.post("/api/imports/sql-functions/ds-1", json={"sql_function": " new.execute "})
        updated_at = session.setting.updated_at
        repeated = client.post("/api/imports/sql-functions/ds-1", json={"sql_function": "new.execute"})
        assert session.setting.updated_at == updated_at
        loaded = client.get("/api/imports/sql-functions/ds-1")
        replaced = client.put("/api/imports/sql-functions/ds-1", json={"sql_functions": ["replacement.execute"]})
    assert added.status_code == repeated.status_code == loaded.status_code == replaced.status_code == 200
    expected = {"datasource_id": "ds-1", "sql_functions": ["legacy.execute", "new.execute"]}
    assert added.json() == repeated.json() == loaded.json() == expected
    assert replaced.json()["sql_functions"] == ["replacement.execute"]
    assert record.call_count == 2  # 同名追加不重复修改时间或写审计。
    assert record.call_args_list[0].kwargs["sql_functions"] == expected["sql_functions"]
    assert updated_at is not None
    assert len(session.queries) == 3
    assert all("datasource" in str(query) and "FOR UPDATE" in str(query) for query in session.queries)


def test_append_creates_setting_after_datasource_lock(allowed, monkeypatch):
    monkeypatch.setattr(imports.audit, "record", Mock())
    session = SettingSession()
    reads = []
    original_get = session.get

    def get(entity, object_id, **kwargs):
        if entity is AppSetting:
            assert session.queries
            reads.append(kwargs)
        return original_get(entity, object_id, **kwargs)

    session.get = get
    with client_for(session) as client:
        response = client.post("/api/imports/sql-functions/ds-1", json={"sql_function": "client.run_sql"})
    assert response.status_code == 200, response.text
    assert response.json() == {"datasource_id": "ds-1", "sql_functions": ["client.run_sql"]}
    assert session.setting.key == "import.sql_functions:ds-1"
    assert reads == [{"populate_existing": True}]
    query = session.queries[0].compile(dialect=postgresql.dialect())
    assert "datasource.id" in str(query) and "FOR UPDATE" in str(query)
    assert "ds-1" in query.params.values()


@pytest.mark.parametrize("method", ["post", "put"])
def test_rule_writes_reject_missing_datasource_without_creating_setting(allowed, monkeypatch, method):
    record = Mock()
    monkeypatch.setattr(imports.audit, "record", record)
    session = SettingSession()
    session.scalar = lambda _: None
    body = {"sql_function": "new.execute"} if method == "post" else {"sql_functions": ["new.execute"]}
    with client_for(session) as client:
        response = getattr(client, method)("/api/imports/sql-functions/missing", json=body)
    assert response.status_code == 404
    assert session.setting is None
    record.assert_not_called()


def test_append_checks_access_before_locking_or_writing(monkeypatch):
    def denied(*_):
        raise HTTPException(403, "forbidden")

    monkeypatch.setattr(imports.service, "require_import_access", denied)
    session = SettingSession()
    with client_for(session) as client:
        response = client.post("/api/imports/sql-functions/ds-1", json={"sql_function": "new.execute"})
    assert response.status_code == 403
    assert session.queries == [] and session.setting is None


@pytest.mark.parametrize("name", ["", " ", "a()", "a..b", "x" * 129, "--password"])
def test_append_reuses_function_name_validation(allowed, name):
    session = SettingSession()
    with client_for(session) as client:
        response = client.post("/api/imports/sql-functions/ds-1", json={"sql_function": name})
    assert response.status_code == 422
    assert session.queries == [] and session.setting is None


@pytest.mark.parametrize("locale,expected", [
    ("en", "Execution function names must be valid identifiers or dotted names, with at most 128 characters"),
    ("zh-TW", "執行函式名稱必須是有效的識別字或點分名稱，且不超過 128 個字元"),
])
def test_single_function_validation_is_localized(locale, expected):
    with locale_context(locale), pytest.raises(ValueError) as error:
        imports.SqlFunction(sql_function="a()")
    assert expected in str(error.value)


@pytest.mark.parametrize("initial", [None, {"sql_functions": ["legacy.execute"]}])
def test_concurrent_appends_reload_rules_under_shared_datasource_lock(allowed, monkeypatch, initial):
    monkeypatch.setattr(imports.audit, "record", Mock())
    state = {"value": deepcopy(initial)}
    datasource_lock = Lock()
    ready = Barrier(2)

    class ConcurrentSession(SettingSession):
        def __init__(self):
            super().__init__()
            self.locked = False
            # 两个请求在拿锁前都持有旧快照。
            if initial is not None:
                self.setting = AppSetting(key="import.sql_functions:ds-1", value=deepcopy(initial))

        def scalar(self, query):
            assert "datasource" in str(query) and "FOR UPDATE" in str(query)
            ready.wait(timeout=5)
            datasource_lock.acquire()
            self.locked = True
            return "ds-1"

        def get(self, entity, object_id, **kwargs):
            assert entity is AppSetting and self.locked and kwargs.get("populate_existing")
            value = state["value"]
            self.setting = AppSetting(key=object_id, value=deepcopy(value)) if value is not None else None
            return self.setting

        def flush(self):
            assert self.locked
            state["value"] = deepcopy(self.setting.value)

        def close(self):
            if self.locked:
                datasource_lock.release()
                self.locked = False

    def append(name):
        session = ConcurrentSession()
        app = FastAPI()
        app.include_router(imports.router)
        app.dependency_overrides[current_user] = lambda: User(id="actor-1")

        def transaction():
            try:
                yield session
            finally:
                session.close()

        app.dependency_overrides[get_session] = transaction
        with TestClient(app) as client:
            return client.post("/api/imports/sql-functions/ds-1", json={"sql_function": name})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(append, ["first.execute", "second.execute"]))
    assert all(response.status_code == 200 for response in responses)
    expected = {"first.execute", "second.execute"}
    if initial is not None:
        expected.add("legacy.execute")
    assert set(state["value"]["sql_functions"]) == expected
    assert len(state["value"]["sql_functions"]) == len(expected)
