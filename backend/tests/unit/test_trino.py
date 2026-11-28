"""Trino 适配器契约：引擎登记、采集查询的拼装、问数会话属性、错误归类、执行前核对与视图血缘；不连接外部数据库。

行为依据 2026-11-28 在 Trino 482 上的实测（见 connectors/trino.py 模块说明）。
"""

from __future__ import annotations

import pytest
from trino.exceptions import TrinoQueryError, TrinoUserError

from avelis.analysis.explore import profile_for
from avelis.connectors.base import QueryError
from avelis.connectors.trino import CLIENT_SLACK_SECONDS, TrinoConnector
from avelis.engines import get_engine
from avelis.engines.base import DDL_AUDIT, LIVE_CHECK, METADATA, QUERY, VIEW_LINEAGE
from avelis.lineage.sql import parse_query

VIEW_TEXT = (
    "SELECT\n  u.city\n, SUM(o.amt) total\nFROM\n  (pg.public.orders o\n"
    "INNER JOIN users u ON (u.user_id = o.user_id))\n"
    "WHERE (o.amt > 0)\nGROUP BY u.city\n"
)


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows: list[list] = []
        self.cancelled = False

    def execute(self, sql, params=None):
        self.conn.executed.append(sql)
        for key, result in self.conn.responses:
            if key in sql:
                if isinstance(result, Exception):
                    raise result
                columns, rows = result
                self.description = [(c,) for c in columns]
                self._rows = [list(r) for r in rows]
                return
        raise AssertionError(f"未预期的语句：{sql}")

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchmany(self, count):
        self.conn.fetch_counts.append(count)
        return self._rows[:count]

    def cancel(self):
        self.cancelled = True
        self.conn.cancels += 1


class Connection:
    def __init__(self, responses):
        self.responses = responses
        self.executed: list[str] = []
        self.fetch_counts: list[int] = []
        self.cancels = 0
        self.closed = False

    def cursor(self):
        return Cursor(self)

    def close(self):
        self.closed = True


def connector(monkeypatch, responses, options=None):
    conn = Connection(responses)
    calls = []
    c = TrinoConnector("trino", 8080, "query", "", options or {"catalog": "memory", "schemas": ["shop"]})

    def connect(**kwargs):
        calls.append(kwargs)
        return conn

    monkeypatch.setattr(c, "_connect", connect)
    return c, conn, calls


def test_engine_registration():
    engine = get_engine("trino")
    assert engine.label == "Trino" and engine.default_port == 8080
    assert engine.capabilities == frozenset({METADATA, VIEW_LINEAGE, QUERY, LIVE_CHECK}) and not engine.supports(
        DDL_AUDIT
    )
    assert engine.name_levels == ("catalog", "schema", "table") and engine.sql.catalog_in_sql
    assert engine.dialect_for({"catalog": "memory"}).default_catalog == "memory"
    assert engine.collector is TrinoConnector and engine.querier is TrinoConnector
    assert not engine.credentials_required({}) and engine.credentials_required({"auth": "PASSWORD"})
    profile = profile_for("trino")
    assert profile is not None and profile.dialect == "trino" and profile.catalog_in_sql


def test_password_auth_requires_https_and_credentials():
    with pytest.raises(ValueError, match="HTTPS"):
        TrinoConnector("t", 1, "u", "p", {"catalog": "c", "auth": "PASSWORD"})
    with pytest.raises(ValueError, match="账号与口令"):
        TrinoConnector("t", 1, "u", "", {"catalog": "c", "auth": "PASSWORD", "tls_mode": "require"})
    with pytest.raises(ValueError, match="不支持"):
        TrinoConnector("t", 1, "", "", {"catalog": "c", "auth": "KERBEROS"})
    assert TrinoConnector("t", 1, "", "", {"catalog": "c"}).user == "avelis"


def test_connect_maps_tls_and_auth(monkeypatch):
    import trino

    seen = []
    monkeypatch.setattr(trino.dbapi, "connect", lambda **kw: seen.append(kw) or "conn")
    TrinoConnector("t", 8080, "", "", {"catalog": "memory"})._connect(timeout_seconds=9)
    plain = seen[-1]
    assert plain["http_scheme"] == "http" and plain["auth"] is None and plain["user"] == "avelis"
    assert plain["catalog"] == "memory" and plain["request_timeout"] == 9 and "verify" not in plain
    TrinoConnector("t", 8443, "u", "p", {"catalog": "c", "auth": "PASSWORD", "tls_mode": "require"})._connect()
    secure = seen[-1]
    assert secure["http_scheme"] == "https" and secure["verify"] is False
    assert isinstance(secure["auth"], trino.auth.BasicAuthentication)
    TrinoConnector("t", 8443, "u", "p", {"catalog": "c", "auth": "PASSWORD", "tls_mode": "verify-full"})._connect()
    assert seen[-1]["verify"] is True


def snapshot_responses(views=VIEW_TEXT):
    return [
        ("information_schema.schemata", (["schema_name"], [["shop"], ["default"]])),
        (
            "information_schema.tables",
            (
                ["table_schema", "table_name", "table_type"],
                [["shop", "users", "BASE TABLE"], ["shop", "v_city", "VIEW"]],
            ),
        ),
        (
            "system.jdbc.columns",
            (
                ["table_schem", "table_name", "column_name", "type_name", "is_nullable", "remarks", "ordinal_position"],
                [
                    ["shop", "users", "city", "varchar", "YES", "城市", 2],
                    ["shop", "users", "user_id", "integer", "NO", None, 1],
                    ["shop", "v_city", "city", "varchar", "YES", None, 1],
                    ["shop", "v_city", "total", "decimal(38,2)", "YES", None, 2],
                ],
            ),
        ),
        ("system.metadata.table_comments", (["schema_name", "table_name", "comment"], [["shop", "users", "用户"]])),
        (
            "information_schema.views",
            (["table_schema", "table_name", "view_definition"], [["shop", "v_city", views]] if views else []),
        ),
        ("system.runtime.nodes", (["node_version"], [["482"]])),
    ]


def test_snapshot_collects_tables_views_columns_and_comments(monkeypatch):
    c, conn, _ = connector(monkeypatch, snapshot_responses())
    snap = c.snapshot()
    assets = {a.qualified: a for a in snap.assets}
    assert set(assets) == {"memory.shop.users", "memory.shop.v_city"} and snap.warnings == []
    users = assets["memory.shop.users"]
    assert users.kind == "table" and users.comment == "用户" and users.physical_id is None and users.key_columns == []
    assert [(col.name, col.data_type, col.nullable, col.comment) for col in users.columns] == [
        ("user_id", "integer", False, None),
        ("city", "varchar", True, "城市"),
    ]
    view = assets["memory.shop.v_city"]
    assert view.kind == "view" and view.definition == VIEW_TEXT
    assert snap.server_identity["version"] == "482" and snap.server_identity.get("instance_id") is None
    # 只查配置的 Catalog 与模式；名称以字面量写进 SQL
    assert all("'shop'" in sql for sql in conn.executed if "jdbc.columns" in sql or "information_schema.views" in sql)
    assert any(
        sql.lstrip().upper().startswith("SELECT") and '"memory".information_schema.tables' in sql
        for sql in conn.executed
    )
    assert conn.closed


def test_snapshot_fails_when_schema_is_not_visible(monkeypatch):
    responses = snapshot_responses()
    responses[0] = ("information_schema.schemata", (["schema_name"], [["default"]]))
    c, conn, _ = connector(monkeypatch, responses)
    with pytest.raises(RuntimeError, match="shop"):
        c.snapshot()
    assert not any("system.jdbc.columns" in sql for sql in conn.executed)


def test_snapshot_warns_when_view_definition_missing(monkeypatch):
    c, _, _ = connector(monkeypatch, snapshot_responses(views=None))
    snap = c.snapshot()
    assert any("v_city" in w for w in snap.warnings)


@pytest.mark.parametrize(
    "options",
    [{"schemas": ["shop"]}, {"catalog": "mem ory", "schemas": ["shop"]}, {"catalog": "memory", "schemas": []}],
)
def test_snapshot_rejects_bad_configuration(monkeypatch, options):
    c, conn, _ = connector(monkeypatch, [], options=options)
    with pytest.raises(ValueError):
        c.snapshot()
    assert conn.executed == []


def test_select_sets_session_limits_timezone_and_cancels_remaining_rows(monkeypatch):
    c, conn, calls = connector(monkeypatch, [("SELECT 1", (["total"], [[1], [2], [3]]))])
    result = c.execute_select(
        "SELECT 1", timeout_seconds=20, mem_limit_bytes=2 * 1024**3, max_rows=2, timezone="Asia/Shanghai"
    )
    assert calls == [
        {
            "timeout_seconds": 20 + CLIENT_SLACK_SECONDS,
            "session_properties": {"query_max_run_time": "20s", "query_max_memory": f"{2 * 1024**3}B"},
            "timezone": "Asia/Shanghai",
        }
    ]
    assert result.columns == ["total"] and result.rows == [[1], [2]] and result.truncated
    assert conn.fetch_counts == [3] and conn.cancels == 1 and conn.closed


def test_select_without_memory_limit_or_truncation(monkeypatch):
    c, conn, calls = connector(monkeypatch, [("SELECT 1", (["v"], [[1]]))])
    result = c.execute_select("SELECT 1", timeout_seconds=5, mem_limit_bytes=0, max_rows=10)
    assert calls[0]["session_properties"] == {"query_max_run_time": "5s"} and calls[0]["timezone"] is None
    assert not result.truncated and conn.cancels == 0


def test_invalid_arguments_are_rejected_before_connecting(monkeypatch):
    c, _, calls = connector(monkeypatch, [])
    with pytest.raises(QueryError) as caught:
        c.execute_select("SELECT 1", timeout_seconds=0, mem_limit_bytes=0, max_rows=10)
    assert caught.value.code == "timeout"
    with pytest.raises(QueryError) as caught:
        c.execute_select("SELECT 1", timeout_seconds=5, mem_limit_bytes=0, max_rows=-1)
    assert caught.value.code == "other"
    with pytest.raises(QueryError, match="Mars/Olympus"):
        c.execute_select("SELECT 1", timeout_seconds=5, mem_limit_bytes=0, max_rows=10, timezone="Mars/Olympus")
    assert calls == []


def _trino_error(name, kind="USER_ERROR", message="boom"):
    cls = TrinoUserError if kind == "USER_ERROR" else TrinoQueryError
    return cls({"errorName": name, "errorType": kind, "errorCode": 1, "message": message}, "q1")


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            _trino_error("TABLE_NOT_FOUND", message="line 1:15: Table 'memory.shop.nope' does not exist"),
            "invalid_object",
        ),
        (_trino_error("COLUMN_NOT_FOUND", message="line 1:8: Column 'nope' cannot be resolved"), "invalid_object"),
        (_trino_error("PERMISSION_DENIED", message="Access Denied: Cannot insert into table"), "permission"),
        (
            _trino_error("EXCEEDED_TIME_LIMIT", "INSUFFICIENT_RESOURCES", "Query exceeded maximum time limit of 3.00s"),
            "timeout",
        ),
        (
            _trino_error(
                "EXCEEDED_GLOBAL_MEMORY_LIMIT", "INSUFFICIENT_RESOURCES", "Query exceeded distributed user memory limit"
            ),
            "other",
        ),
    ],
)
def test_errors_are_classified_by_error_name(monkeypatch, error, code):
    c, conn, _ = connector(monkeypatch, [("SELECT 1", error)])
    with pytest.raises(QueryError) as caught:
        c.execute_select("SELECT 1", timeout_seconds=5, mem_limit_bytes=0, max_rows=10)
    assert caught.value.code == code and caught.value.db_errno == error.error_name
    assert caught.value.message == error.message and conn.closed


def test_request_timeout_is_classified_as_timeout(monkeypatch):
    import requests

    c, _, _ = connector(monkeypatch, [("SELECT 1", requests.exceptions.ReadTimeout("read timed out"))])
    with pytest.raises(QueryError) as caught:
        c.execute_select("SELECT 1", timeout_seconds=5, mem_limit_bytes=0, max_rows=10)
    assert caught.value.code == "timeout"


def test_describe_objects_reads_columns_and_view_definitions(monkeypatch):
    responses = snapshot_responses()[2:]
    c, conn, _ = connector(monkeypatch, responses)
    out = c.describe_objects([("shop", "users"), ("shop", "v_city"), ("shop", "gone"), ("shop", "x; DROP")])
    assert out[("shop", "users")] == {
        "columns": [["user_id", "integer"], ["city", "varchar"]],
        "definition": None,
        "physical_id": None,
    }
    assert out[("shop", "v_city")]["definition"] == VIEW_TEXT
    assert out[("shop", "gone")] == {"columns": [], "definition": None, "physical_id": None}
    assert out[("shop", "x; DROP")] == {"columns": [], "definition": None, "physical_id": None}
    assert not any("x; DROP" in sql for sql in conn.executed) and conn.closed
    c2, conn2, calls2 = connector(monkeypatch, [])
    assert c2.describe_objects([("shop", "bad name")]) == {
        ("shop", "bad name"): {"columns": [], "definition": None, "physical_id": None}
    }
    assert calls2 == []


def test_stored_view_definition_parses_to_column_lineage():
    dialect = get_engine("trino").dialect_for({"catalog": "memory"})
    schema = {
        "memory": {"shop": {"users": {"user_id": "integer", "city": "varchar"}}},
        "pg": {"public": {"orders": {"id": "bigint", "user_id": "integer", "amt": "decimal(10,2)"}}},
    }
    parsed = parse_query(VIEW_TEXT, dialect=dialect, schema=schema, default_db="shop")
    assert parsed.status == "ok", parsed.reason
    assert parsed.tables == ["memory.shop.users", "pg.public.orders"]
    total = next(col for col in parsed.columns if col.name == "total")
    assert {(s.ref.table, s.ref.column, s.relation) for s in total.sources} == {
        ("pg.public.orders", "amt", "value_aggregated")
    }
    assert ("pg.public.orders", "amt", "filter") in {(i.ref.table, i.ref.column, i.relation) for i in parsed.influences}
