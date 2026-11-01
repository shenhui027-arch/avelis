"""Oracle 统一审计的离线契约：成功 DDL、UTC/旧版时钟、策略就绪、原生错误与保守列归因。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import oracledb
import pytest

from avelis.collect.attribution import Window, attribute
from avelis.connectors.ddl import AuditUnavailable, column_refs
from avelis.connectors.oracle import OracleConnector
from avelis.engines import get_engine
from avelis.i18n import Msg

NOW = datetime(2026, 11, 1, 8)
SINCE, UNTIL = NOW - timedelta(minutes=5), NOW + timedelta(minutes=5)


def _record(statement="ALTER TABLE APP.ORDERS ADD channel VARCHAR2(20)", **changes):
    return dict(at=NOW, user="DDL_USER", action="ALTER TABLE", schema="APP", name="ORDERS",
                statement=statement, return_code=0, audit_type="Standard") | changes


def _policy(option="ALTER TABLE", kind="STANDARD ACTION", schema=None, object_type=None,
            enabled="BY USER", entity="ALL USERS", success="YES"):
    return option, kind, schema, object_type, enabled, entity, success


class _Cursor:
    def __init__(self, conn):
        self.conn, self.rows = conn, []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, binds=None):
        self.conn.calls.append((sql, binds))
        for marker, error in self.conn.errors.items():
            if marker in sql:
                raise error
        if "EVENT_TIMESTAMP_UTC" in sql and self.conn.old_timestamp:
            raise oracledb.DatabaseError('ORA-00904: "EVENT_TIMESTAMP_UTC": invalid identifier')
        if sql.startswith("ALTER SESSION"):
            return self
        if "AUDIT_UNIFIED_ENABLED_POLICIES" in sql:
            self.rows = self.conn.policies
            return self
        assert "FROM UNIFIED_AUDIT_TRAIL" in sql
        assert "AUDIT_TYPE = 'Standard'" in sql and "RETURN_CODE = 0" in sql
        assert "OBJECT_NAME IS NOT NULL" in sql
        actions = {v for k, v in binds.items() if k.startswith("a")}
        schemas = {v for k, v in binds.items() if k.startswith("s") and k != "since"}
        records = [r for r in self.conn.records if r["audit_type"] == "Standard" and r["return_code"] == 0
                   and r["action"] in actions and r["schema"] in schemas and r["name"] is not None]
        if sql.startswith("SELECT 1"):
            assert "ROWNUM = 1" in sql and "since" not in binds and "until" not in binds
            self.rows = [(1,)] if records else []
            return self
        assert " >= :since" in sql and " <= :until" in sql
        records = [r for r in records if binds["since"] <= _utc(r["at"]) <= binds["until"]]
        self.rows = [tuple(r[k] for k in ("at", "user", "action", "schema", "name", "statement"))
                     for r in records]
        return self

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self, records=(), policies=(), errors=None, old_timestamp=False):
        self.records, self.policies = records, policies
        self.errors, self.old_timestamp = errors or {}, old_timestamp
        self.calls, self.closed = [], False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True
        return False

    def cursor(self):
        return _Cursor(self)


class _Clob:
    def __init__(self, connection, text):
        self.connection, self.text, self.reads = connection, text, 0

    def read(self):
        assert not self.connection.closed
        self.reads += 1
        return self.text


def _utc(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _connector(monkeypatch, connection):
    connector = OracleConnector("offline.invalid", 1521, "COLLECTOR", "unused",
                                {"service": "FREEPDB1", "schemas": ["app"]})
    monkeypatch.setattr(connector, "_connect", lambda: connection)
    return connector


def test_only_successful_standard_ddl_with_bound_schema_and_window(monkeypatch):
    connection = _Connection()
    clob = _Clob(connection, "ALTER TABLE app.orders ADD channel VARCHAR2(20)")
    connection.records = [_record(clob), _record(return_code=904, user="FAILED"),
                          _record(action="SELECT", user="READER"), _record(schema="PRIVATE", user="OTHER_SCHEMA"),
                          _record(audit_type="Database Vault", user="OTHER_AUDIT"),
                          _record(at=SINCE - timedelta(seconds=1), user="OLD"),
                          _record(at=UNTIL + timedelta(seconds=1), user="LATER"), _record(name=None)]
    connector = _connector(monkeypatch, connection)
    events = connector.ddl_events(SINCE, UNTIL)
    assert len(events) == 1 and events[0].db_user == "DDL_USER"
    assert events[0].objects == [("APP", "ORDERS")] and events[0].statement_type == "ALTER TABLE"
    assert events[0].at == NOW and events[0].certain is None
    assert connector.ddl_identifier_fold == "upper" and connector.ddl_audit_source == "oracle:UNIFIED_AUDIT_TRAIL"
    assert clob.reads == 1 and connection.closed
    assert "VARCHAR2" not in repr(events[0])
    refs = column_refs(events[0].statement, "ORDERS", connector.ddl_identifier_fold, schema="APP")
    assert [token.text for token in refs.columns] == ["channel"] and not refs.unknown
    sql, binds = connection.calls[0]
    assert "EVENT_TIMESTAMP_UTC" in sql and binds["s0"] == "APP"
    assert "APP" not in sql and binds["since"] == SINCE and binds["until"] == UNTIL
    assert len(connection.calls) == 1


def test_old_timestamp_falls_back_only_when_utc_column_is_absent(monkeypatch):
    connection = _Connection([_record()], old_timestamp=True)
    events = _connector(monkeypatch, connection).ddl_events(SINCE, UNTIL)
    assert events[0].at == NOW
    assert connection.calls[1] == ("ALTER SESSION SET TIME_ZONE = '+00:00'", None)
    sql, binds = connection.calls[2]
    assert "SELECT EVENT_TIMESTAMP," in sql and "EVENT_TIMESTAMP_UTC" not in sql
    assert binds["since"] == SINCE and binds["until"] == UNTIL


def test_aware_datetimes_are_normalized_to_utc_without_avelis_host_timezone(monkeypatch):
    offset = timezone(timedelta(hours=8))
    connection = _Connection([_record(at=NOW.replace(tzinfo=timezone.utc).astimezone(offset))])
    events = _connector(monkeypatch, connection).ddl_events(SINCE.replace(tzinfo=timezone.utc).astimezone(offset),
                                                            UNTIL.replace(tzinfo=timezone.utc).astimezone(offset))
    assert events[0].at == NOW and events[0].at.tzinfo is None
    assert connection.calls[0][1]["since"] == SINCE


@pytest.mark.parametrize("error,cause,fragment", [
    ('ORA-01031: insufficient privileges', "no_permission", "AUDIT_VIEWER"),
    ('ORA-00942: table or view does not exist', "no_permission", "AUDIT_VIEWER"),
    ('ORA-41900: missing privilege', "no_permission", "AUDIT_VIEWER"),
    ('ORA-00904: "SQL_TEXT": invalid identifier', "incompatible", "结构"),
    ('ORA-00932: inconsistent datatypes', "incompatible", "结构"),
    ('ORA-03113: end-of-file on communication channel\nsecond line', "error", "读取"),
])
def test_native_failures_report_cause_and_original_error(monkeypatch, error, cause, fragment):
    connection = _Connection(errors={"FROM UNIFIED_AUDIT_TRAIL": oracledb.DatabaseError(error)})
    with pytest.raises(AuditUnavailable) as caught:
        _connector(monkeypatch, connection).ddl_events(SINCE, UNTIL)
    assert caught.value.cause == cause and fragment in str(caught.value)
    assert error.splitlines()[0] in str(caught.value) and "second line" not in str(caught.value)
    assert isinstance(caught.value.args[0], Msg)
    assert connection.closed and len(connection.calls) == 1  # 非 UTC 缺列错误不能触发旧版回退


def test_empty_window_with_historical_success_is_not_misreported_as_disabled(monkeypatch):
    connection = _Connection([_record(at=SINCE - timedelta(days=1))])
    assert _connector(monkeypatch, connection).ddl_events(SINCE, UNTIL) == []
    assert len(connection.calls) == 2
    assert all("AUDIT_UNIFIED_ENABLED_POLICIES" not in sql for sql, _ in connection.calls)


@pytest.mark.parametrize("policy", [
    _policy(), _policy("CREATE VIEW", entity="DEPLOY_USER"),
    _policy("DROP TABLE", enabled="EXCEPT USER", entity="COLLECTOR"),
    _policy("ALTER TABLE", enabled="BY GRANTED ROLE", entity="DDL_ROLE"),
    _policy("ALTER ANY TABLE", kind="SYSTEM PRIVILEGE"),
    _policy("ALTER", kind="OBJECT ACTION", schema="APP", object_type="TABLE"),
    _policy("ALL", kind="STANDARD ACTION"),
    _policy("DDL_ROLE", kind="ROLE PRIVILEGE"),  # 无法排除覆盖 DDL 的未知类型，保守保留来源
])
def test_empty_history_with_successful_related_policy_is_ready(monkeypatch, policy):
    connection = _Connection(policies=[policy])
    assert _connector(monkeypatch, connection).ddl_events(SINCE, UNTIL) == []
    assert "AUDIT_UNIFIED_POLICIES p" in connection.calls[-1][0]


@pytest.mark.parametrize("policies", [
    [], [_policy(success="NO")], [_policy("SELECT TABLE")], [_policy("CREATE SESSION", kind="SYSTEM PRIVILEGE")],
    [_policy(enabled="INVALID")], [_policy(entity=None)], [_policy(schema="OTHER_SCHEMA")],
    [_policy("ALTER", kind="OBJECT ACTION", object_type="SEQUENCE")],
    [_policy("ALL", kind="OBJECT ACTION", object_type="SEQUENCE")],
])
def test_no_history_and_no_related_success_policy_is_not_enabled(monkeypatch, policies):
    connection = _Connection(records=[_record(return_code=904)], policies=policies)
    with pytest.raises(AuditUnavailable) as caught:
        _connector(monkeypatch, connection).ddl_events(SINCE, UNTIL)
    assert caught.value.cause == "not_enabled"
    assert "没有相关的成功 DDL 记录" in str(caught.value) and "审计策略" in str(caught.value)
    assert isinstance(caught.value.args[0], Msg)


@pytest.mark.parametrize("kind", ["INVALID", "XS ACTION", "OLS_ACTION", "DATAPUMP ACTION", "DIRECT LOAD ACTION",
                                 "DV ACTION"])
def test_nonstandard_policy_types_do_not_enable_standard_ddl_auditing(monkeypatch, kind):
    connection = _Connection(policies=[_policy("ALTER TABLE", kind=kind)])
    with pytest.raises(AuditUnavailable) as caught:
        _connector(monkeypatch, connection).ddl_events(SINCE, UNTIL)
    assert caught.value.cause == "not_enabled"


@pytest.mark.parametrize("error,cause", [("ORA-01031: denied", "no_permission"),
                                       ('ORA-00904: "ENABLED_OPTION": missing', "incompatible"),
                                       ("ORA-03113: connection lost", "error")])
def test_policy_probe_failure_does_not_pretend_audit_is_disabled(monkeypatch, error, cause):
    connection = _Connection(errors={"AUDIT_UNIFIED_ENABLED_POLICIES": oracledb.DatabaseError(error)})
    with pytest.raises(AuditUnavailable) as caught:
        _connector(monkeypatch, connection).ddl_events(SINCE, UNTIL)
    assert caught.value.cause == cause


@pytest.mark.parametrize("changes", [{"user": None}, {"at": "bad timestamp"}, {"statement": b"not a CLOB"}])
def test_invalid_required_values_are_incompatible(monkeypatch, changes):
    connection = _Connection([_record(**changes)])
    if "at" in changes:
        # 真实驱动返回的异常类型来自结果结构；让替身仍执行到连接器的值验证。
        monkeypatch.setattr(_Cursor, "fetchall", lambda _: [("bad timestamp", "DDL_USER", "ALTER TABLE",
                                                             "APP", "ORDERS", "ALTER TABLE APP.ORDERS ADD X INT")])
        connection.records = []
    with pytest.raises(AuditUnavailable) as caught:
        _connector(monkeypatch, connection).ddl_events(SINCE, UNTIL)
    assert caught.value.cause == "incompatible" and isinstance(caught.value.args[0], Msg)


def test_clob_read_database_failure_is_unavailable(monkeypatch):
    class BrokenClob:
        def read(self):
            raise oracledb.DatabaseError("ORA-03113: lost while reading CLOB")

    with pytest.raises(AuditUnavailable, match="lost while reading CLOB") as caught:
        _connector(monkeypatch, _Connection([_record(BrokenClob())])).ddl_events(SINCE, UNTIL)
    assert caught.value.cause == "error"


@pytest.mark.parametrize("row", [(NOW, "DDL_USER"), tuple(range(7))])
def test_incompatible_event_row_shape_is_an_audit_unavailable(monkeypatch, row):
    connection = _Connection()
    monkeypatch.setattr(_Cursor, "fetchall", lambda _: [row])
    with pytest.raises(AuditUnavailable) as caught:
        _connector(monkeypatch, connection).ddl_events(SINCE, UNTIL)
    assert caught.value.cause == "incompatible"


@pytest.mark.parametrize("statements,column,expected,actor", [
    (["ALTER TABLE APP.ORDERS ADD channel VARCHAR2(20)"], "CHANNEL", "matched", "DDL_USER"),
    (["ALTER TABLE APP.ORDERS MODIFY amount NUMBER(20,2)"], "CHANNEL", "unmatched", None),
    ([None], "CHANNEL", "ambiguous", None),
    (['ALTER TABLE APP.ORDERS ADD "Channel" VARCHAR2(20)'], "CHANNEL", "unmatched", None),
    (["ALTER TABLE APP.ORDERS ADD channel VARCHAR2(20)"], "Channel", "unmatched", None),
    (['ALTER TABLE APP.ORDERS ADD "Channel" VARCHAR2(20)'], "Channel", "matched", "DDL_USER"),
    (["CREATE OR REPLACE VIEW APP.ORDERS AS SELECT * FROM APP.BASE"], "CHANNEL", "matched", "DDL_USER"),
])
def test_column_attribution_uses_real_sql_and_oracle_identifier_folding(monkeypatch, statements, column,
                                                                    expected, actor):
    connection = _Connection([_record(statement, action="CREATE VIEW" if statement and statement.startswith("CREATE")
                                     else "ALTER TABLE") for statement in statements])
    connector = _connector(monkeypatch, connection)
    event = {"kind": "column_added", "asset_id": "asset", "column_name": column, "before": {}, "after": {}}
    session = SimpleNamespace(get=lambda *_: SimpleNamespace(db_name="APP", name="ORDERS"))
    window = Window(SINCE, SINCE + timedelta(seconds=1), UNTIL, UNTIL + timedelta(seconds=1))
    result = attribute(session, get_engine("oracle"), connector, [event], window)
    assert result["status"] == "ok"
    assert event["attribution_status"] == expected and event["actor_db_user"] == actor
    assert all(not statement or statement not in str(event["attribution"]) for statement in statements)
    assert all("statement" not in candidate for candidate in event["attribution"].get("statements", []))


def test_no_permission_is_preserved_by_attribution(monkeypatch):
    connection = _Connection(errors={"UNIFIED_AUDIT_TRAIL": oracledb.DatabaseError("ORA-01031: denied")})
    event = {"kind": "column_added", "asset_id": None, "column_name": "CHANNEL", "before": {}, "after": {}}
    result = attribute(SimpleNamespace(), get_engine("oracle"), _connector(monkeypatch, connection), [event],
                       Window(SINCE, SINCE, UNTIL, UNTIL))
    assert result["status"] == "unavailable" and result["cause"] == "no_permission"
    assert event["actor_db_user"] is None and "AUDIT_VIEWER" in event["attribution"]["reason"]
