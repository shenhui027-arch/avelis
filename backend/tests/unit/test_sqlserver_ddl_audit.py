"""默认跟踪的离线契约：只认已提交对象事件，未知模式和没有列文本时不猜。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pymssql
import pytest

from avelis.collect import attribution
from avelis.collect.attribution import Window, attribute
from avelis.connectors.ddl import AuditUnavailable, DdlEvent, column_refs, redefines
from avelis.connectors.sqlserver import SqlServerConnector
from avelis.engines import get_engine

AT = datetime(2026, 10, 30, 8)
SINCE, UNTIL = AT - timedelta(minutes=10), AT + timedelta(minutes=10)


class _Cursor:
    def __init__(self, replies, *, case_sensitive=False):
        self.replies = list(replies)
        self.calls = []
        self.case_sensitive = case_sensitive

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self.reply = self.replies.pop(0)
        if isinstance(self.reply, Exception):
            raise self.reply
        if "sys.fn_trace_gettable(" in sql:
            # 离线模拟服务器的 CI / CS 比较；只有 SQL 明确要求的过滤才生效。
            def same(a, b):
                return a == b if self.case_sensitive else a.casefold() == b.casefold()

            if "t.DatabaseName = %s" in sql:
                self.reply = [row for row in self.reply if same(row["database_name"], params[1])]
            schemas = re.search(r"s\.name IN \((.*?)\)", sql)
            if schemas or "AND 1 = 0" in sql:
                names = re.findall(r"N'([^']*)'", schemas[1]) if schemas else []
                self.reply = [
                    {
                        **row,
                        "schema_name": next(
                            (
                                row["schema_name"]
                                for name in names
                                if row["schema_name"] and same(row["schema_name"], name)
                            ),
                            None,
                        ),
                    }
                    for row in self.reply
                ]

    def fetchone(self):
        return self.reply

    def fetchall(self):
        return self.reply


class _Connection:
    def __init__(self, replies, *, case_sensitive=False):
        self.cur = _Cursor(replies, case_sensitive=case_sensitive)
        self.closed = False

    def cursor(self, *, as_dict):
        assert as_dict
        return self.cur

    def close(self):
        self.closed = True


def _config(**extra):
    return {"enabled": 1, "offset_minutes": 480, "can_read": 1, **extra}


def _row(**extra):
    return {
        "at_local": AT + timedelta(hours=8),
        "db_user": "DOMAIN\\alice",
        "database_name": "storedb",
        "object_name": "sales",
        "event_class": 164,
        "event_subclass": 1,
        "object_type": 8277,
        "schema_name": "store",
        **extra,
    }


def _connector(monkeypatch, rows=None, *, replies=None, schemas=None, database="storedb", case_sensitive=False):
    connector = SqlServerConnector(
        "localhost",
        1433,
        "collector",
        "unused",
        {"database": database, "schemas": ["store"] if schemas is None else schemas},
    )
    connection = _Connection(
        replies if replies is not None else [_config(), {"path": r"C:\MSSQL\Log\log_42.trc", "status": 1}, rows or []],
        case_sensitive=case_sensitive,
    )
    monkeypatch.setattr(connector, "_connect", lambda **_: connection)
    return connector, connection


def test_default_trace_commits_source_time_and_query_contract(monkeypatch):
    connector, conn = _connector(monkeypatch, [_row(), _row(event_class=46, object_type=8278)])
    events = connector.ddl_events(SINCE, UNTIL, cursors=(None, None))
    assert conn.closed
    assert connector.ddl_audit_source == "sqlserver:default_trace"
    assert [(e.at, e.db_user, e.objects, e.statement_type) for e in events] == [
        (AT, "DOMAIN\\alice", [("store", "sales")], "ALTER"),
        (AT, "DOMAIN\\alice", [("store", "sales")], "CREATE"),
    ]
    assert all(e.statement == "" and e.certain is False for e in events)
    assert all(not redefines(e.statement) and column_refs(e.statement, "sales", schema="store").unknown for e in events)
    permission_sql, permission_params = conn.cur.calls[0]
    assert "HAS_PERMS_BY_NAME(NULL, NULL, 'ALTER TRACE') AS can_read" in permission_sql
    assert "HAS_PERMS_BY_NAME(NULL, 'SERVER', 'ALTER TRACE')" not in permission_sql
    assert permission_params is None
    sql, params = conn.cur.calls[-1]
    assert "sys.fn_trace_gettable(%s, DEFAULT)" in sql
    assert "t.EventClass IN (46, 47, 164)" in sql and "t.EventSubClass = 1" in sql
    assert "t.ObjectType IN (8277, 8278)" in sql
    assert "t.DatabaseName = %s" in sql and "o.object_id = t.ObjectID" in sql
    assert "o.name = t.ObjectName" in sql and "o.create_date <= t.StartTime" in sql
    assert "t.EventClass <> 47" in sql
    assert "s.schema_id = o.schema_id AND s.name IN (N'store')" in sql
    assert params == (r"C:\MSSQL\Log\log_42.trc", "storedb", SINCE + timedelta(hours=8), UNTIL + timedelta(hours=8))
    assert "is_default = 1" in conn.cur.calls[1][0]


@pytest.mark.parametrize("database,schemas", [("storedb", ["store"]), ("STOREDB", ["store"]), ("storedb", ["STORE"])])
def test_ci_collation_configuration_variants_keep_the_same_canonical_candidate(monkeypatch, database, schemas):
    connector, conn = _connector(monkeypatch, [_row()], database=database, schemas=schemas)
    events = connector.ddl_events(SINCE, UNTIL)
    assert len(events) == 1 and events[0].objects == [("store", "sales")]
    assert events[0].certain is False and conn.closed


@pytest.mark.parametrize(
    "database,schemas,expected",
    [
        ("storedb", ["store"], [[("store", "sales")], []]),
        ("STOREDB", ["store"], []),
        ("storedb", ["STORE"], [[], [("STORE", "sales")]]),
        ("storedb", [], [[], []]),
    ],
)
def test_cs_collation_keeps_distinct_schema_names_and_database_scope(monkeypatch, database, schemas, expected):
    connector, _ = _connector(
        monkeypatch, [_row(), _row(schema_name="STORE")], database=database, schemas=schemas, case_sensitive=True
    )
    assert [event.objects for event in connector.ddl_events(SINCE, UNTIL)] == expected


@pytest.mark.parametrize("case_sensitive", [False, True])
def test_other_database_is_rejected_by_server_filter(monkeypatch, case_sensitive):
    connector, _ = _connector(monkeypatch, [_row(database_name="anotherdb")], case_sensitive=case_sensitive)
    assert connector.ddl_events(SINCE, UNTIL) == []


@pytest.mark.parametrize("offset_minutes", [-330, 0, 345, 840])
def test_local_start_time_uses_server_offset_not_application_timezone(monkeypatch, offset_minutes):
    delta = timedelta(minutes=offset_minutes)
    connector, _ = _connector(
        monkeypatch,
        replies=[
            _config(offset_minutes=offset_minutes),
            {"path": "trace.trc", "status": 1},
            [_row(at_local=AT + delta)],
        ],
    )
    assert connector.ddl_events(SINCE, UNTIL)[0].at == AT


@pytest.mark.parametrize(
    "change",
    [
        {"event_subclass": 0},
        {"event_subclass": 2},
        {"event_class": 999},
        {"object_type": 22601},
        {"database_name": "anotherdb"},
        {"at_local": SINCE + timedelta(hours=8) - timedelta(milliseconds=1)},
        {"at_local": UNTIL + timedelta(hours=8) + timedelta(milliseconds=1)},
    ],
)
def test_non_commit_other_object_database_or_outside_window_is_not_evidence(monkeypatch, change):
    connector, _ = _connector(monkeypatch, [_row(**change)])
    assert connector.ddl_events(SINCE, UNTIL) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_name": None},  # 已删除、改名或 object_id 复用后目录查询无法确认
        {"schema_name": "private"},  # 不在配置的模式内
        {"event_class": 47},  # 即使同一 object_id 又出现在当前目录，也不能替 DROP 认领模式
    ],
)
def test_unknown_schema_and_deleted_object_do_not_assume_single_configured_schema(monkeypatch, changes):
    connector, _ = _connector(monkeypatch, [_row(**changes)])
    events = connector.ddl_events(SINCE, UNTIL)
    assert len(events) == 1 and events[0].objects == []


def test_object_id_mapping_disambiguates_same_name_in_two_schemas(monkeypatch):
    connector, _ = _connector(monkeypatch, [_row(schema_name="other")], schemas=["store", "other"])
    assert connector.ddl_events(SINCE, UNTIL)[0].objects == [("other", "sales")]


class _Session:
    def get(self, _model, _key):
        return SimpleNamespace(db_name="store", name="sales")


@pytest.mark.parametrize("event_class", [46, 164])
def test_current_schema_is_only_a_candidate_and_empty_text_cannot_attribute_a_column(monkeypatch, event_class):
    connector, _ = _connector(monkeypatch, [_row(event_class=event_class)])
    table = {"kind": "comment_changed", "asset_id": "a1"}
    column = {"kind": "column_type_changed", "asset_id": "a1", "column_name": "amount"}
    result = attribute(
        _Session(),
        get_engine("sqlserver"),
        connector,
        [table, column],
        Window(SINCE, SINCE + timedelta(seconds=1), UNTIL - timedelta(seconds=1), UNTIL),
    )
    assert result["status"] == "ok"
    assert table["attribution_status"] == "ambiguous" and table["actor_db_user"] is None
    assert table["attribution"]["statements"][0]["certain"] is False
    assert column["attribution_status"] == "ambiguous" and column["actor_db_user"] is None
    assert column["attribution"]["redefinitions"] == 0 and column["attribution"]["unrecognized"] == 1
    assert "statement" not in table["attribution"]["statements"][0]


@pytest.mark.parametrize("event_class", [46, 164])
def test_schema_transfer_cannot_reassign_historical_event_to_current_schema(monkeypatch, event_class):
    # store.sales 在事件后经 ALTER SCHEMA other TRANSFER store.sales 转移；
    # object_id、名称、类型、create_date 都没变，当前 sys.objects 仍会恢复出 other。
    connector, _ = _connector(
        monkeypatch, [_row(event_class=event_class, schema_name="other")], schemas=["store", "other"]
    )
    evidence = connector.ddl_events(SINCE, UNTIL)
    assert evidence[0].at == AT and evidence[0].objects == [("other", "sales")]
    assert evidence[0].certain is False
    monkeypatch.setattr(connector, "ddl_events", lambda *_args, **_kwargs: evidence)
    assets = {
        "old": SimpleNamespace(db_name="store", name="sales"),
        "new": SimpleNamespace(db_name="other", name="sales"),
    }
    session = SimpleNamespace(get=lambda _model, key: assets[key])
    old_object = {"kind": "comment_changed", "asset_id": "old"}
    current_object = {"kind": "comment_changed", "asset_id": "new"}
    attribute(
        session,
        get_engine("sqlserver"),
        connector,
        [old_object, current_object],
        Window(SINCE, SINCE + timedelta(seconds=1), UNTIL - timedelta(seconds=1), UNTIL),
    )
    assert old_object["attribution_status"] == "unmatched" and old_object["actor_db_user"] is None
    assert current_object["attribution_status"] == "ambiguous" and current_object["actor_db_user"] is None


def test_drop_with_unknown_schema_is_unmatched(monkeypatch):
    connector, _ = _connector(monkeypatch, [_row(event_class=47)])
    event = {"kind": "removed", "asset_id": "a1"}
    attribute(
        _Session(),
        get_engine("sqlserver"),
        connector,
        [event],
        Window(SINCE, SINCE + timedelta(seconds=1), UNTIL - timedelta(seconds=1), UNTIL),
    )
    assert event["attribution_status"] == "unmatched" and event["actor_db_user"] is None


@pytest.mark.parametrize(
    ("replies", "cause", "message"),
    [
        ([_config(enabled=0, can_read=0)], "not_enabled", "default trace enabled = 0"),
        ([_config(can_read=0)], "no_permission", "ALTER TRACE"),
        ([_config(can_read=None)], "no_permission", "无法判断"),
        ([_config(), None], "not_enabled", "未运行"),
        ([_config(), {"status": 0, "path": "trace.trc"}], "not_enabled", "未运行"),
        ([_config(), {"status": 1, "path": None}], "not_enabled", "未运行"),
        ([None], "incompatible", "时区偏移"),
        ([_config(offset_minutes=None)], "incompatible", "时区偏移"),
        ([_config(offset_minutes=841)], "incompatible", "时区偏移"),
    ],
)
def test_disabled_permission_missing_trace_or_bad_config_are_distinct(monkeypatch, replies, cause, message):
    connector, conn = _connector(monkeypatch, replies=replies)
    with pytest.raises(AuditUnavailable) as error:
        connector.ddl_events(SINCE, UNTIL)
    assert error.value.cause == cause and message in str(error.value)
    assert conn.closed


def test_null_permission_result_is_unknown_and_does_not_read_trace(monkeypatch):
    connector, conn = _connector(monkeypatch, replies=[_config(can_read=None)])
    with pytest.raises(AuditUnavailable) as error:
        connector.ddl_events(SINCE, UNTIL)
    assert error.value.cause == "no_permission"
    assert "无法判断" in str(error.value) and "ALTER TRACE 权限检查返回 NULL" in str(error.value)
    assert len(conn.cur.calls) == 1 and conn.closed


@pytest.mark.parametrize(
    ("number", "cause"),
    [
        (229, "no_permission"),
        (297, "no_permission"),
        (300, "no_permission"),
        (15151, "no_permission"),
        (15247, "no_permission"),
        (207, "incompatible"),
        (208, "incompatible"),
        (195, "incompatible"),
        (50000, "error"),
        (20003, "error"),
    ],
)
@pytest.mark.parametrize("stage", [0, 1, 2])
def test_native_errors_are_classified_and_preserve_summary(monkeypatch, number, cause, stage):
    replies = [_config(), {"path": "trace.trc", "status": 1}, []]
    replies[stage] = pymssql.OperationalError(number, b"native summary")
    connector, conn = _connector(monkeypatch, replies=replies)
    with pytest.raises(AuditUnavailable) as error:
        connector.ddl_events(SINCE, UNTIL)
    assert error.value.cause == cause and "native summary" in str(error.value)
    assert conn.closed


def test_connection_error_is_an_audit_unavailable_error(monkeypatch):
    connector, _ = _connector(monkeypatch)

    def fail(**_):
        raise pymssql.OperationalError(20009, b"server unavailable")

    monkeypatch.setattr(connector, "_connect", fail)
    with pytest.raises(AuditUnavailable) as error:
        connector.ddl_events(SINCE, UNTIL)
    assert error.value.cause == "error" and "server unavailable" in str(error.value)


@pytest.mark.parametrize("changes", [{"at_local": None}, {"db_user": None}, {"object_name": None}])
def test_malformed_audit_record_is_incompatible(monkeypatch, changes):
    connector, conn = _connector(monkeypatch, [_row(**changes)])
    with pytest.raises(AuditUnavailable) as error:
        connector.ddl_events(SINCE, UNTIL)
    assert error.value.cause == "incompatible" and "StartTime / LoginName / ObjectName" in str(error.value)
    assert conn.closed


def test_empty_enabled_audit_is_a_successful_empty_read(monkeypatch):
    connector, conn = _connector(monkeypatch)
    assert connector.ddl_events(SINCE, UNTIL) == []
    assert conn.closed


@pytest.fixture
def audit_clock(monkeypatch):
    class Clock(datetime):
        age = 0
        elapsed = 0.0
        sleeps = []

        @classmethod
        def now(cls, tz=None):
            return (UNTIL + timedelta(seconds=cls.age + cls.elapsed)).replace(tzinfo=timezone.utc).astimezone(tz)

        @classmethod
        def sleep(cls, seconds):
            assert 0 < seconds <= 10
            cls.sleeps.append(seconds)
            cls.elapsed += seconds

    monkeypatch.setattr(attribution, "datetime", Clock)
    monkeypatch.setattr(attribution, "sleep", Clock.sleep)
    monkeypatch.setattr(attribution, "monotonic", lambda: Clock.elapsed)
    return Clock


@pytest.mark.parametrize("age,wait", [(0, 10), (5, 5), (10, 0), (11, 0)])
@pytest.mark.parametrize("column", [None, "amount"])
def test_trace_waits_from_snapshot_start_then_empty_result_is_unmatched(monkeypatch, audit_clock, age, wait, column):
    audit_clock.age = age
    connector, conn = _connector(monkeypatch)
    read = connector.ddl_events

    def read_after_wait(*args, **kwargs):
        assert audit_clock.elapsed == wait
        return read(*args, **kwargs)

    monkeypatch.setattr(connector, "ddl_events", read_after_wait)
    event = {"kind": "column_type_changed" if column else "comment_changed", "asset_id": "a1", "column_name": column}
    derived = {"kind": "derivation_changed", "asset_id": "a1"}
    result = attribute(
        _Session(),
        get_engine("sqlserver"),
        connector,
        [event, derived],
        Window(SINCE, SINCE + timedelta(seconds=1), UNTIL, UNTIL + timedelta(seconds=min(age, 8))),
    )
    assert result["status"] == "ok" and result["audit_events"] == 0 and conn.closed
    assert event["attribution_status"] == "unmatched" and event["actor_db_user"] is None
    assert event["attribution"]["window"]["audit_lag_seconds"] == connector.ddl_audit_lag_seconds == 10
    assert event["attribution"]["window"]["audit_wait_seconds"] == result["window"]["audit_wait_seconds"] == wait
    assert audit_clock.sleeps == ([wait] if wait else [])
    assert result["window"]["since"] == (SINCE - timedelta(seconds=10 + result["window"]["slack_seconds"])).isoformat()
    assert derived["attribution_status"] == "not_applicable"


def test_delayed_trace_event_in_lookback_is_only_a_candidate(monkeypatch):
    # late 的 StartTime 在上次快照之前：落盘时间不能冒充 DDL 生效时间。
    late = SINCE - timedelta(seconds=6)
    connector, _ = _connector(monkeypatch, [_row(at_local=late + timedelta(hours=8))])
    event = {"kind": "comment_changed", "asset_id": "a1"}
    result = attribute(
        _Session(),
        get_engine("sqlserver"),
        connector,
        [event],
        Window(SINCE, SINCE + timedelta(seconds=1), UNTIL - timedelta(seconds=1), UNTIL),
    )
    assert result["audit_events"] == 1
    assert event["attribution_status"] == "ambiguous" and event["actor_db_user"] is None
    assert event["attribution"]["statements"][0]["at"] == late.isoformat()
    assert event["attribution"]["statements"][0]["certain"] is False


@pytest.mark.parametrize("certain,expected", [(None, "matched"), (False, "ambiguous")])
def test_late_event_is_read_after_wait_and_attributed_normally(monkeypatch, audit_clock, certain, expected):
    connector, _ = _connector(monkeypatch)

    def delayed_read(*_args, **_kwargs):
        # 用有列级证据的离线事件验证共享归因；真实默认跟踪恢复的当前模式仍仅作边界候选。
        assert audit_clock.now(timezone.utc).replace(tzinfo=None) == UNTIL + timedelta(seconds=10)
        return [DdlEvent(AT, "alice", [("store", "sales")], "ALTER", "ALTER TABLE store.sales ADD amount INT", certain)]

    monkeypatch.setattr(connector, "ddl_events", delayed_read)
    event = {"kind": "column_added", "asset_id": "a1", "column_name": "amount"}
    attribute(
        _Session(),
        get_engine("sqlserver"),
        connector,
        [event],
        Window(SINCE, SINCE + timedelta(seconds=1), UNTIL, UNTIL),
    )
    assert event["attribution_status"] == expected
    assert event["actor_db_user"] == ("alice" if expected == "matched" else None)
    assert event["attribution"]["window"]["audit_wait_seconds"] == 10
    assert audit_clock.sleeps == [10]


def test_engine_without_audit_lag_does_not_wait(audit_clock):
    reads = []

    def read(*_args, **_kwargs):
        reads.append(audit_clock.elapsed)
        return []

    event = {"kind": "comment_changed", "asset_id": "a1"}
    result = attribute(
        _Session(),
        get_engine("postgres"),
        SimpleNamespace(ddl_events=read, ddl_audit_source="unit"),
        [event],
        Window(SINCE, SINCE + timedelta(seconds=1), UNTIL, UNTIL),
    )
    assert result["status"] == "ok" and event["attribution_status"] == "unmatched"
    assert result["window"]["audit_lag_seconds"] == result["window"]["audit_wait_seconds"] == 0
    assert reads == [0] and audit_clock.sleeps == []
