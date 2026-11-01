"""StarRocks AuditLoader 归因契约；只用离线数据库替身，不读取真实审计或业务数据。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pymysql
import pytest

from avelis.collect.attribution import Window, attribute
from avelis.connectors.ddl import AuditUnavailable
from avelis.connectors.starrocks import StarRocksConnector
from avelis.engines import get_engine
from avelis.engines.base import DDL_AUDIT
from avelis.i18n import locale_context

AUDIT_TABLE = "starrocks_audit_db__.starrocks_audit_tbl__"
T0 = datetime(2026, 10, 30, 8)
WINDOW = Window(T0 - timedelta(minutes=10), T0 - timedelta(minutes=10) + timedelta(seconds=2),
                T0 + timedelta(minutes=1), T0 + timedelta(minutes=1, seconds=2))


def _connector(audit_table=AUDIT_TABLE, **options):
    return StarRocksConnector("db", 9030, "collector", "offline-test-password",
                              {"databases": ["store"], "ddl_audit_table": audit_table, **options})


class _Session:
    def get(self, _model, _key):
        return SimpleNamespace(catalog_name="default_catalog", db_name="store", name="sales")


def _change(kind="column_added", column="channel"):
    return {"kind": kind, "asset_id": "sales", "column_name": column, "before": {}, "after": {}}


def _attribute(connector, events=None):
    events = events if events is not None else [_change()]
    result = attribute(_Session(), get_engine("starrocks"), connector, events, WINDOW)
    return result, events


def test_engine_declares_optional_audit_table_and_translated_setup_hint():
    engine = get_engine("starrocks")
    assert engine.supports(DDL_AUDIT)
    option = next(o for o in engine.options if o.key == "ddl_audit_table")
    assert (option.label, option.type, option.required, option.default) == ("DDL 审计表", "text", False, "")
    assert AUDIT_TABLE in option.hint and "AuditLoader" in option.hint and "SELECT" in option.hint
    assert engine.option({}, "ddl_audit_table") is None
    hints = []
    for locale in ("zh-CN", "zh-TW", "en"):
        with locale_context(locale):
            description = engine.describe()
        field = next(o for o in description["options"] if o["key"] == "ddl_audit_table")
        assert AUDIT_TABLE in field["hint"] and "AuditLoader" in field["hint"] and "SELECT" in field["hint"]
        hints.append(field["hint"])
    assert len(set(hints)) == 3


@pytest.mark.parametrize("binding", [None, "", "   "])
def test_unbound_audit_is_explicit_and_does_not_connect(monkeypatch, binding):
    connector = _connector(binding)

    def forbidden(**_kwargs):
        pytest.fail("未绑定审计表时不能连接数据库")

    monkeypatch.setattr(connector, "_connect", forbidden)
    assert connector.ddl_audit_source is None
    result, changes = _attribute(connector, [_change(), _change("derivation_changed")])
    assert result["status"] == "unsupported"
    assert changes[0]["attribution_status"] == "unsupported" and changes[0]["actor_db_user"] is None
    assert "未绑定审计表" in changes[0]["attribution"]["reason"]
    assert changes[1]["attribution_status"] == "not_applicable"


def test_unbound_reason_translates_at_request_time():
    connector = _connector(None)
    reasons = []
    for locale, needle in (("zh-CN", "未绑定审计表"), ("zh-TW", "未綁定稽核表"), ("en", "audit")):
        with locale_context(locale):
            result, changes = _attribute(connector)
        assert result["status"] == "unsupported"
        reason = changes[0]["attribution"]["reason"]
        assert needle in reason
        reasons.append(reason)
    assert len(set(reasons)) == 3


def _columns(classifier="isQuery", **changes):
    fields = {"timestamp": "datetime", "user": "varchar(64)", "db": "varchar(96)",
              "stmt": "varchar(1048576)", "state": "varchar(8)"}
    if classifier:
        fields[classifier] = "tinyint(1)" if classifier.lower() == "isquery" else "varchar(32)"
    fields.update(changes)
    return [{"Field": name, "Type": kind} for name, kind in fields.items() if kind is not None]


def _row(stmt="ALTER TABLE store.sales ADD COLUMN channel VARCHAR(16)", *, at=T0, **extra):
    return {"ts": at.replace(tzinfo=timezone.utc).timestamp(), "actor": "alice@10.0.0.1", "db": "store",
            "stmt": stmt, "state": "EOF", "is_query": 0, "query_type": "query", "stmt_type": "ALTER", **extra}


class _Cursor:
    def __init__(self, rows, columns, zone, error=None, error_stage="read"):
        self.rows, self.columns, self.zone = rows, columns, zone
        self.error, self.error_stage = error, error_stage
        self.calls, self.result = [], []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql.startswith("SHOW COLUMNS"):
            stage, self.result = "columns", self.columns
        elif "@@system_time_zone" in sql:
            stage, self.result = "zone", [{"zone": self.zone}]
        elif sql.startswith("SET time_zone"):
            stage, self.result = "set_zone", []
        elif "UNIX_TIMESTAMP" in sql:
            stage, self.result = "read", self.rows
        else:
            pytest.fail(f"意外的审计数据库操作：{sql}")
        if self.error is not None and stage == self.error_stage:
            raise self.error

    def fetchall(self):
        return self.result

    def fetchone(self):
        return self.result[0] if self.result else None


class _Connection:
    def __init__(self, cursor):
        self.cur = cursor
        self.closed = False

    def cursor(self):
        assert not self.closed
        return self.cur

    def close(self):
        self.closed = True


def _bind(monkeypatch, connector=None, rows=None, columns=None, zone="Asia/Shanghai", **kwargs):
    connector = connector or _connector()
    cursor = _Cursor(rows if rows is not None else [_row()], columns if columns is not None else _columns(),
                     zone, **kwargs)
    connection = _Connection(cursor)
    monkeypatch.setattr(connector, "_connect", lambda **_kwargs: connection)
    return connector, connection, cursor


@pytest.mark.parametrize("classifier", ["isQuery", "queryType", "stmtType", "stmt_type"])
def test_auditloader_schema_versions_and_utc_event_contract(monkeypatch, classifier):
    connector, connection, cursor = _bind(monkeypatch, columns=_columns(classifier))
    events = connector.ddl_events(T0 - timedelta(minutes=1), T0 + timedelta(minutes=1))
    assert connector.ddl_audit_source == f"starrocks:{AUDIT_TABLE}"
    assert connector.ddl_identifier_fold == "insensitive"
    assert len(events) == 1
    event = events[0]
    assert (event.at, event.db_user, event.objects, event.statement_type) == \
        (T0, "alice@10.0.0.1", [("store", "sales")], "ALTER")
    assert event.statement == _row()["stmt"] and event.may_lag and event.certain is None
    assert connection.closed
    assert any(params == ("Asia/Shanghai",) for sql, params in cursor.calls if sql.startswith("SET time_zone"))


def test_schema_field_names_are_case_insensitive(monkeypatch):
    columns = [{"Field": r["Field"].upper(), "Type": r["Type"].upper()} for r in _columns()]
    connector, connection, _ = _bind(monkeypatch, columns=columns)
    assert len(connector.ddl_events(T0, T0)) == 1
    assert connection.closed


@pytest.mark.parametrize("classifier", ["isQuery", "queryType", "stmtType", "stmt_type"])
def test_only_successful_nonquery_ddl_is_accepted(monkeypatch, classifier):
    rows = [_row(), _row("CREATE TABLE store.new_table (id INT)", state="OK", stmt_type="CREATE"),
            _row(state="ERR"), _row(state="RUNNING"), _row(state=None),
            _row("SELECT * FROM store.sales", is_query=1, stmt_type="SELECT"),
            _row("SELECT * FROM store.sales", is_query=0, stmt_type="SELECT"),
            _row("INSERT INTO store.sales VALUES (1)", query_type="insert", stmt_type="INSERT"),
            _row("UPDATE store.sales SET channel = 'ALTER TABLE store.sales DROP COLUMN channel'",
                 stmt_type="UPDATE"),
            _row("-- ALTER TABLE store.sales ADD COLUMN secret INT\nSELECT 1", is_query=1, stmt_type="SELECT")]
    connector, connection, _ = _bind(monkeypatch, rows=rows, columns=_columns(classifier))
    events = connector.ddl_events(T0, T0)
    assert [(e.statement_type, e.objects) for e in events] == \
        [("ALTER", [("store", "sales")]), ("CREATE", [("store", "new_table")])]
    assert all(e.may_lag for e in events) and connection.closed


def test_query_classifier_cannot_disagree_with_ddl_text(monkeypatch):
    for columns, bad in ((_columns("isQuery"), {"is_query": 1}),
                         (_columns("queryType"), {"query_type": "insert"}),
                         (_columns("queryType"), {"query_type": "unknown"})):
        connector, _, _ = _bind(monkeypatch, rows=[_row(**bad)], columns=columns)
        assert connector.ddl_events(T0, T0) == []


def test_objects_rename_truncate_and_scope_are_not_guessed(monkeypatch):
    rows = [_row("RENAME TABLE store.old TO store.sales", stmt_type="RENAME"),
            _row("TRUNCATE TABLE store.sales", stmt_type="TRUNCATE"),
            _row("ALTER TABLE sales DROP COLUMN channel"),
            _row("ALTER TABLE `default_catalog`.`store`.`sales` ADD COLUMN x INT"),
            _row("ALTER TABLE other.sales ADD COLUMN x INT"),
            _row("ALTER TABLE `external`.`store`.`sales` ADD COLUMN x INT"),
            _row("CREATE USER 'someone'@'%' IDENTIFIED BY 'secret'", stmt_type="CREATE"),
            _row("ALTER TABLE sales ADD COLUMN c INT", db=""),
            _row("ALTER TABLE store.sales ADD COLUMN c INT; ALTER TABLE store.private ADD COLUMN c INT")]
    connector, connection, _ = _bind(monkeypatch, rows=rows)
    events = connector.ddl_events(T0, T0)
    assert [(e.statement_type, e.objects) for e in events] == [
        ("RENAME", [("store", "old"), ("store", "sales")]),
        ("TRUNCATE", [("store", "sales")]), ("ALTER", [("store", "sales")]),
        ("ALTER", [("store", "sales")]),
    ]
    assert connection.closed


def test_time_window_is_parameterized_in_utc_and_fe_timezone_is_explicit(monkeypatch):
    connector, connection, cursor = _bind(monkeypatch, zone="Asia/Taipei")
    since = datetime(2026, 10, 30, 16, tzinfo=timezone(timedelta(hours=8)))
    until = T0 + timedelta(minutes=1)
    events = connector.ddl_events(since, until, cursors=({"ignored": 1}, {"ignored": 2}))
    assert events[0].at == T0 and events[0].at.tzinfo is None
    sql, params = next((sql, params) for sql, params in cursor.calls if "UNIX_TIMESTAMP" in sql)
    assert params == (T0.replace(tzinfo=timezone.utc).timestamp(), until.replace(tzinfo=timezone.utc).timestamp())
    assert sql.count("%s") == 2 and "2026-10-30" not in sql
    assert "`starrocks_audit_db__`.`starrocks_audit_tbl__`" in sql
    assert any(params == ("Asia/Taipei",) for sql, params in cursor.calls if sql.startswith("SET time_zone"))
    assert connection.closed


def test_quoted_binding_is_escaped_and_timezone_is_not_interpolated(monkeypatch):
    binding = "`audit db`.`event``name; DROP TABLE x`"
    zone = "UTC'; SELECT secret; --"
    connector, connection, cursor = _bind(monkeypatch, connector=_connector(binding), zone=zone)
    assert len(connector.ddl_events(T0, T0)) == 1
    assert cursor.calls[0] == (f"SHOW COLUMNS FROM {binding}", None)
    assert any(params == (zone,) and zone not in sql for sql, params in cursor.calls if sql.startswith("SET time_zone"))
    assert connection.closed


@pytest.mark.parametrize("binding", ["audit", "a.b.c", "audit.table; DROP TABLE secret", "a.`b", "a.b c", 123])
def test_invalid_audit_binding_is_incompatible_without_connecting(monkeypatch, binding):
    connector = _connector(binding)

    def forbidden(**_kwargs):
        pytest.fail("非法审计表配置不能触发数据库操作")

    monkeypatch.setattr(connector, "_connect", forbidden)
    with pytest.raises(AuditUnavailable) as captured:
        connector.ddl_events(T0, T0)
    assert captured.value.cause == "incompatible"


@pytest.mark.parametrize("changes", [{"timestamp": None}, {"user": None}, {"db": None}, {"stmt": None},
                                     {"state": None}, {"timestamp": "varchar(32)"}, {"stmt": "bigint"}])
def test_missing_or_wrong_schema_is_incompatible_and_closes_connection(monkeypatch, changes):
    connector, connection, cursor = _bind(monkeypatch, columns=_columns(**changes))
    with pytest.raises(AuditUnavailable) as captured:
        connector.ddl_events(T0, T0)
    assert captured.value.cause == "incompatible"
    assert connection.closed and not any("UNIX_TIMESTAMP" in sql for sql, _ in cursor.calls)


def test_missing_all_statement_classifiers_is_incompatible(monkeypatch):
    connector, connection, _ = _bind(monkeypatch, columns=_columns(classifier=None))
    with pytest.raises(AuditUnavailable) as captured:
        connector.ddl_events(T0, T0)
    assert captured.value.cause == "incompatible" and connection.closed


@pytest.mark.parametrize("classifier,kind", [("isQuery", "datetime"), ("queryType", "bigint"),
                                            ("stmtType", "bigint"), ("stmt_type", "datetime")])
def test_classifier_field_types_are_validated(monkeypatch, classifier, kind):
    connector, connection, _ = _bind(monkeypatch, columns=_columns(classifier, **{classifier: kind}))
    with pytest.raises(AuditUnavailable) as captured:
        connector.ddl_events(T0, T0)
    assert captured.value.cause == "incompatible" and connection.closed


@pytest.mark.parametrize("zone", [None, "", "   ", 8])
def test_unknown_fe_timezone_is_incompatible_instead_of_assuming_local_time(monkeypatch, zone):
    connector, connection, _ = _bind(monkeypatch, zone=zone)
    with pytest.raises(AuditUnavailable) as captured:
        connector.ddl_events(T0, T0)
    assert captured.value.cause == "incompatible" and connection.closed


@pytest.mark.parametrize("errno,message,cause", [
    (1146, "Table 'audit.events' doesn't exist", "not_enabled"),
    (1049, "Unknown database 'audit'", "not_enabled"),
    (1142, "SELECT command denied to user 'collector' for table 'events'", "no_permission"),
    (1044, "Access denied for user 'collector' to database 'audit'", "no_permission"),
    (1054, "Unknown column 'timestamp' in 'field list'", "incompatible"),
    (1105, "Column 'timestamp' does not exist in table audit.events", "incompatible"),
    (1105, "Table [audit.events] is not found", "not_enabled"),
    (1298, "Unknown or incorrect time zone: 'unconfigured'", "incompatible"),
    (2013, "Lost connection to MySQL server during query", "error"),
])
@pytest.mark.parametrize("stage", ["columns", "zone", "set_zone", "read"])
def test_native_errors_have_honest_causes_and_close(monkeypatch, errno, message, cause, stage):
    connector, connection, _ = _bind(monkeypatch, error=pymysql.err.OperationalError(errno, message), error_stage=stage)
    with pytest.raises(AuditUnavailable) as captured:
        connector.ddl_events(T0, T0)
    assert captured.value.cause == cause and message in str(captured.value)
    if cause == "not_enabled":
        assert "AuditLoader" in str(captured.value)
    assert connection.closed


@pytest.mark.parametrize("error,cause", [(pymysql.err.OperationalError(1045, "Access denied"), "no_permission"),
                                        (pymysql.err.OperationalError(2003, "Can't connect"), "error"),
                                        (OSError("network offline"), "error")])
def test_connect_failures_are_audit_unavailable(monkeypatch, error, cause):
    connector = _connector()

    def fail(**_kwargs):
        raise error

    monkeypatch.setattr(connector, "_connect", fail)
    with pytest.raises(AuditUnavailable) as captured:
        connector.ddl_events(T0, T0)
    assert captured.value.cause == cause and str(error.args[-1]) in str(captured.value)


@pytest.mark.parametrize("cause,label", [("not_enabled", "未开启"), ("no_permission", "无权读取"),
                                        ("incompatible", "结构"), ("error", "读取 DDL 审计失败")])
def test_read_failures_are_reported_as_unavailable_not_unmatched(monkeypatch, cause, label):
    connector = _connector()

    def fail(*_args, **_kwargs):
        raise AuditUnavailable("offline error detail", cause)

    monkeypatch.setattr(connector, "ddl_events", fail)
    result, changes = _attribute(connector)
    assert result["status"] == changes[0]["attribution_status"] == "unavailable"
    assert changes[0]["actor_db_user"] is None
    assert changes[0]["attribution"]["cause"] == cause
    assert label in changes[0]["attribution"]["reason"] and "offline error detail" in changes[0]["attribution"]["reason"]


@pytest.mark.parametrize("stmt", ["ALTER TABLE store.sales ADD COLUMN channel VARCHAR(16) ...",
                                  "ALTER TABLE store.sales ADD COLUMN channel VARCHAR(16",
                                  "ALTER TABLE store.sales ADD COLUMN channel VARCHAR(16))",
                                  "ALTER TABLE store.sales COMMENT 'unfinished"])
def test_incomplete_or_visibly_truncated_sql_is_incompatible(monkeypatch, stmt):
    connector, connection, _ = _bind(monkeypatch, rows=[_row(stmt)])
    with pytest.raises(AuditUnavailable) as captured:
        connector.ddl_events(T0, T0)
    assert captured.value.cause == "incompatible" and connection.closed


def test_stmt_column_length_limit_is_not_accepted_as_complete_evidence(monkeypatch):
    stmt = "ALTER TABLE store.sales ADD COLUMN channel INT".ljust(80)
    connector, connection, _ = _bind(monkeypatch, rows=[_row(stmt)], columns=_columns(stmt="varchar(80)"))
    result, changes = _attribute(connector)
    assert result["cause"] == "incompatible" and changes[0]["attribution_status"] == "unavailable"
    assert connection.closed


def test_stmt_byte_limit_counts_utf8_bytes(monkeypatch):
    stmt = "ALTER TABLE store.sales ADD COLUMN channel VARCHAR(16) COMMENT '渠道渠道渠道渠道渠道'"
    assert len(stmt) < 80 <= len(stmt.encode("utf-8"))
    connector, connection, _ = _bind(monkeypatch, rows=[_row(stmt)], columns=_columns(stmt="varchar(80)"))
    with pytest.raises(AuditUnavailable) as captured:
        connector.ddl_events(T0, T0)
    assert captured.value.cause == "incompatible" and connection.closed


@pytest.mark.parametrize("row_changes", [{"actor": None}, {"actor": ""}, {"ts": None}, {"ts": "bad"}, {"stmt": None}])
def test_invalid_audit_rows_never_become_named_candidates(monkeypatch, row_changes):
    connector, connection, _ = _bind(monkeypatch, rows=[_row(**row_changes)])
    result, changes = _attribute(connector)
    assert result["cause"] == "incompatible" and changes[0]["attribution_status"] == "unavailable"
    assert changes[0]["actor_db_user"] is None and connection.closed


def test_empty_async_source_is_unmatched_with_log_delay_explanation(monkeypatch):
    connector, connection, _ = _bind(monkeypatch, rows=[])
    result, changes = _attribute(connector)
    assert result["status"] == "ok" and changes[0]["attribution_status"] == "unmatched"
    assert changes[0]["actor_db_user"] is None and "尚未落盘" in changes[0]["attribution"]["reason"]
    assert connection.closed


def test_async_audit_events_match_only_inside_the_certain_window(monkeypatch):
    connector, _, _ = _bind(monkeypatch)
    result, changes = _attribute(connector)
    assert result["status"] == "ok"
    assert (changes[0]["attribution_status"], changes[0]["actor_db_user"]) == ("matched", "alice@10.0.0.1")
    assert changes[0]["attribution"]["statements"][0]["certain"] is True
    assert changes[0]["attribution"]["window"]["effect_lag_seconds"] == connector.ddl_effect_lag_seconds
    assert "ALTER TABLE" not in repr(changes[0]["attribution"])
    boundary = WINDOW.previous_start - timedelta(seconds=connector.ddl_effect_lag_seconds / 2)
    connector, _, _ = _bind(monkeypatch, rows=[_row(at=boundary)])
    _, changes = _attribute(connector)
    assert changes[0]["attribution_status"] == "ambiguous" and changes[0]["actor_db_user"] is None
    assert changes[0]["attribution"]["statements"][0]["certain"] is False


def test_column_attribution_uses_sql_positions_and_keeps_competing_users(monkeypatch):
    rows = [_row(), _row("ALTER TABLE store.sales ADD COLUMN unrelated INT COMMENT 'channel'", actor="bob@10.0.0.2")]
    connector, _, _ = _bind(monkeypatch, rows=rows)
    _, changes = _attribute(connector)
    assert (changes[0]["attribution_status"], changes[0]["actor_db_user"]) == ("matched", "alice@10.0.0.1")
    rows.append(_row("ALTER TABLE store.sales MODIFY COLUMN channel VARCHAR(32)", actor="bob@10.0.0.2"))
    connector, _, _ = _bind(monkeypatch, rows=rows)
    _, changes = _attribute(connector)
    assert changes[0]["attribution_status"] == "ambiguous"
    assert changes[0]["attribution"]["candidates"] == ["alice@10.0.0.1", "bob@10.0.0.2"]


def test_escaped_column_does_not_hide_an_actor_on_the_same_table(monkeypatch):
    rows = [_row(), _row("ALTER TABLE store.sales ADD COLUMN `channel``extra` INT", actor="bob")]
    connector, _, _ = _bind(monkeypatch, rows=rows)
    _, changes = _attribute(connector, [_change("comment_changed", column=None)])
    assert changes[0]["attribution_status"] == "ambiguous"
    assert changes[0]["attribution"]["candidates"] == ["alice@10.0.0.1", "bob"]


@pytest.mark.parametrize("suffix", ["RENAME TO `sales``archive`", 'RENAME TO "sales""archive"',
                                   "RENAME TO external.store.sales", "RENAME `sales``archive`"])
def test_alter_rename_never_fabricates_a_prefix_or_other_catalog_target(monkeypatch, suffix):
    connector, _, _ = _bind(monkeypatch, rows=[_row(f"ALTER TABLE store.old {suffix}")])
    _, changes = _attribute(connector, [_change("comment_changed", column=None)])
    assert changes[0]["attribution_status"] == ("unmatched" if "external" in suffix else "unavailable")
    assert changes[0]["actor_db_user"] is None


def test_internal_audit_never_attributes_same_named_external_catalog_asset(monkeypatch):
    class ExternalSession:
        def get(self, _model, _key):
            return SimpleNamespace(catalog_name="jdbc_catalog", db_name="store", name="sales")

    connector, _, _ = _bind(monkeypatch)
    changes = [_change()]
    result = attribute(ExternalSession(), get_engine("starrocks"), connector, changes, WINDOW)
    assert result["status"] == "ok"
    assert changes[0]["attribution_status"] == "unsupported" and changes[0]["actor_db_user"] is None
    assert "Catalog" in changes[0]["attribution"]["reason"]


@pytest.mark.parametrize("object_name", ["`sales``archive`", '"sales""archive"'])
def test_escaped_identifier_never_attributes_its_prefix_object(monkeypatch, object_name):
    connector, connection, _ = _bind(monkeypatch, rows=[_row(f"ALTER TABLE store.{object_name} ADD COLUMN channel INT")])
    result, changes = _attribute(connector, [_change("comment_changed", None)])
    assert result["status"] == "unavailable" and result["cause"] == "incompatible"
    assert changes[0]["attribution_status"] == "unavailable" and changes[0]["actor_db_user"] is None
    assert connection.closed


def test_unparseable_rename_does_not_hide_a_competing_user_on_the_old_name(monkeypatch):
    rows = [_row(), _row("ALTER TABLE store.sales RENAME TO `archived``sales`", actor="bob")]
    connector, _, _ = _bind(monkeypatch, rows=rows)
    result, changes = _attribute(connector, [_change("comment_changed", None)])
    assert result["cause"] == "incompatible"
    assert changes[0]["attribution_status"] == "unavailable" and changes[0]["actor_db_user"] is None


@pytest.mark.parametrize("target", ["COLUMN", "INDEX", "PARTITION", "ROLLUP"])
def test_renaming_table_members_does_not_invent_another_table(monkeypatch, target):
    connector, connection, _ = _bind(monkeypatch, rows=[_row(f"ALTER TABLE store.sales RENAME {target} a TO b")])
    events = connector.ddl_events(T0, T0)
    assert len(events) == 1 and events[0].objects == [("store", "sales")]
    assert connection.closed


@pytest.mark.parametrize("stmt", ["RENAME TABLE store.sales TO store.archived_sales",
                                  "ALTER TABLE store.sales RENAME TO archived_sales"])
def test_renaming_a_table_keeps_both_real_object_names(monkeypatch, stmt):
    connector, connection, _ = _bind(monkeypatch, rows=[_row(stmt)])
    events = connector.ddl_events(T0, T0)
    assert len(events) == 1 and events[0].objects == [("store", "sales"), ("store", "archived_sales")]
    assert connection.closed


@pytest.mark.parametrize("prefix", ["/* migration: release 42 */\n", "-- migration: release 42\n"])
def test_ddl_with_leading_comments_can_match_its_column(monkeypatch, prefix):
    connector, connection, _ = _bind(monkeypatch, rows=[_row(prefix + _row()["stmt"])])
    result, changes = _attribute(connector)
    assert result["status"] == "ok" and result["audit_events"] == 1
    assert (changes[0]["attribution_status"], changes[0]["actor_db_user"]) == ("matched", "alice@10.0.0.1")
    assert changes[0]["attribution"]["column_match"] and connection.closed


@pytest.mark.parametrize("prefix", ["/* migration */\n", "-- migration\n"])
def test_commented_ddl_keeps_a_competing_user_in_column_attribution(monkeypatch, prefix):
    rows = [_row(), _row(prefix + "ALTER TABLE store.sales MODIFY COLUMN channel VARCHAR(32)", actor="bob@10.0.0.2")]
    connector, connection, _ = _bind(monkeypatch, rows=rows)
    result, changes = _attribute(connector)
    assert result["status"] == "ok" and result["audit_events"] == 2
    assert changes[0]["attribution_status"] == "ambiguous" and changes[0]["actor_db_user"] is None
    assert changes[0]["attribution"]["candidates"] == ["alice@10.0.0.1", "bob@10.0.0.2"]
    assert connection.closed


@pytest.mark.parametrize("prefix", ["/* ALTER TABLE store.sales ADD COLUMN channel INT */\n",
                                    "-- ALTER TABLE store.sales ADD COLUMN channel INT\n"])
def test_ddl_inside_a_comment_does_not_turn_a_select_into_audit_evidence(monkeypatch, prefix):
    connector, connection, _ = _bind(monkeypatch, rows=[_row(prefix + "SELECT * FROM store.sales", is_query=0)])
    assert connector.ddl_events(T0, T0) == []
    assert connection.closed
