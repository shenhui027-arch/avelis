"""MySQL DDL 候选：只读绑定、开关/权限/结构错误、UTC 时间与现有保守归因规则。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pymysql
import pytest

from avelis.connectors import mysql_audit
from avelis.connectors.ddl import AuditUnavailable, column_refs
from avelis.connectors.mysql import MySQLMetaConnector
from avelis.connectors.mysql_audit import _objects, _table, audit_source
from avelis.i18n import locale_context
from tests.pymysql_contract import mogrify
from tests.unit.test_history import T0, WINDOW, _asset, _attribute, _event

SCHEMA = [
    {"Field": name, "Type": kind}
    for name, kind in (
        ("event_time", "timestamp(6)"),
        ("user_host", "mediumtext"),
        ("command_type", "varchar(64)"),
        ("argument", "mediumblob"),
    )
]


def _row(
    statement="ALTER TABLE `store`.`sales` ADD COLUMN `channel` varchar(16)",
    *,
    at=T0,
    user="alice[alice] @ localhost [127.0.0.1]",
    command="Query",
):
    return {"event_time": at, "user_host": user, "command_type": command, "argument": statement}


class _Cursor:
    def __init__(self, rows, *, schema=None, enabled=1, output="TABLE", offset=0, fail_on=None):
        self.rows = rows
        self.schema = SCHEMA if schema is None else schema
        self.settings = {"enabled": enabled, "output": output}
        self.offset = offset
        self.fail_on = fail_on
        self.calls = []
        self.mode = ""

    def execute(self, sql, params=None):
        mogrify(sql, params)
        self.calls.append((sql, params))
        if self.fail_on and self.fail_on[0] in sql:
            raise self.fail_on[1]
        if "@@GLOBAL.general_log" in sql:
            self.mode = "settings"
        elif sql.startswith("SHOW COLUMNS"):
            self.mode = "schema"
        elif sql == "SET SESSION time_zone = '+00:00'":
            self.offset = 0
        elif sql.startswith("SELECT event_time"):
            self.mode = "rows"

    def fetchone(self):
        return self.settings

    def fetchall(self):
        if self.mode == "schema":
            return self.schema
        assert self.mode == "rows"
        # 模拟 TIMESTAMP：驱动把服务端按会话时区转换后的值作为无时区 datetime 返回。
        return [
            {
                **row,
                "event_time": row["event_time"] + timedelta(hours=self.offset)
                if isinstance(row["event_time"], datetime)
                else row["event_time"],
            }
            for row in self.rows
        ]


class _Connection:
    def __init__(self, cursor):
        self.cur = cursor
        self.closed = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _connector(monkeypatch, rows=(), *, source="mysql.general_log", databases=("store",), **kw):
    connector = MySQLMetaConnector(
        "unused", 3306, "collector", "unused", {"databases": list(databases), "ddl_audit_table": source}
    )
    cur = _Cursor(list(rows), **kw)
    conn = _Connection(cur)

    def connect(**args):
        assert args == {"read_timeout": 60}
        return conn

    monkeypatch.setattr(connector, "_connect", connect)
    return connector, conn, cur


@pytest.mark.parametrize("source", [None, "", "   "])
def test_unbound_is_explicit_without_connecting(monkeypatch, source):
    connector, conn, cur = _connector(monkeypatch, source=source)
    assert connector.ddl_audit_source is None
    with pytest.raises(AuditUnavailable, match="未绑定 DDL 审计表") as caught:
        connector.ddl_events(T0, T0)
    assert caught.value.cause == "not_enabled"
    assert not conn.closed and not cur.calls


@pytest.mark.parametrize("source", [None, "", "   "])
def test_unbound_attribution_is_unsupported_without_connecting(monkeypatch, source):
    connector, _, cur = _connector(monkeypatch, source=source)

    def forbidden(**_kwargs):
        pytest.fail("未绑定审计表时不能连接数据库")

    monkeypatch.setattr(connector, "_connect", forbidden)
    changes = [_event("column_added", "channel"), _event("derivation_changed")]
    result = _attribute(changes, connector, engine="mysql")
    assert result["status"] == "unsupported"
    assert changes[0]["attribution_status"] == "unsupported" and changes[0]["actor_db_user"] is None
    assert "未绑定审计表" in changes[0]["attribution"]["reason"]
    assert changes[1]["attribution_status"] == "not_applicable"
    assert not cur.calls


def test_unbound_reason_translates_at_request_time():
    connector = MySQLMetaConnector("unused", 3306, "collector", "unused", {"databases": ["store"]})
    reasons = []
    for locale, needle in (("zh-CN", "未绑定审计表"), ("zh-TW", "未綁定稽核表"), ("en", "audit")):
        change = _event("column_added", "channel")
        with locale_context(locale):
            result = _attribute([change], connector, engine="mysql")
        assert result["status"] == "unsupported"
        reason = change["attribution"]["reason"]
        assert needle in reason and "MySQL" in reason and "ddl_audit_table" in reason
        reasons.append(reason)
    assert len(set(reasons)) == 3


@pytest.mark.parametrize("source", ["general_log", "mysql.general_log; DROP TABLE store.sales", "a.b.c", [], 1])
def test_invalid_binding_is_incompatible(monkeypatch, source):
    connector, _, cur = _connector(monkeypatch, source=source)
    with pytest.raises(AuditUnavailable, match="名称无效") as caught:
        connector.ddl_events(T0, T0)
    assert caught.value.cause == "incompatible" and not cur.calls


def test_quoted_binding_is_safely_quoted():
    assert _table({"ddl_audit_table": " `audit``db` . `ddl.log` "}) == ("audit`db", "ddl.log", "`audit``db`.`ddl.log`")
    assert audit_source({"ddl_audit_table": " audit.events "}) == "mysql:audit.events"


@pytest.mark.parametrize(
    ("enabled", "output"),
    [
        (0, "TABLE"),
        ("OFF", "TABLE"),
        (1, "FILE"),
        (1, "NONE"),
        (1, "NONE,TABLE"),
        (1, "NONE,FILE,TABLE"),
    ],
)
def test_general_log_requires_enabled_and_table_output(monkeypatch, enabled, output):
    connector, conn, cur = _connector(monkeypatch, enabled=enabled, output=output)
    with pytest.raises(AuditUnavailable, match="general_log=ON.*TABLE") as caught:
        connector.ddl_events(T0, T0)
    assert caught.value.cause == "not_enabled"
    assert conn.closed and len(cur.calls) == 1


def test_quoted_uppercase_general_log_does_not_bypass_enablement(monkeypatch):
    connector, conn, cur = _connector(monkeypatch, source="`MYSQL`.`GENERAL_LOG`", enabled=0)
    with pytest.raises(AuditUnavailable) as caught:
        connector.ddl_events(T0, T0)
    assert caught.value.cause == "not_enabled"
    assert conn.closed and "@@GLOBAL.general_log" in cur.calls[0][0]


@pytest.mark.parametrize("output", ["TABLE", "FILE,TABLE", " file, table "])
def test_general_log_accepts_table_among_outputs(monkeypatch, output):
    connector, conn, _ = _connector(monkeypatch, output=output)
    assert connector.ddl_events(T0, T0) == [] and conn.closed


def test_plugin_table_has_same_contract_but_independent_enablement(monkeypatch):
    connector, conn, cur = _connector(monkeypatch, [_row()], source="audit.ddl_requests", enabled=0, output="FILE")
    events = connector.ddl_events(T0, T0)
    assert len(events) == 1 and connector.ddl_audit_source == "mysql:audit.ddl_requests"
    assert cur.calls[0][0] == "SHOW COLUMNS FROM `audit`.`ddl_requests`"
    assert not any("general_log" in sql for sql, _ in cur.calls)
    assert conn.closed


@pytest.mark.parametrize(
    ("code", "message", "cause"),
    [
        (1142, "SELECT command denied to user collector for table general_log", "no_permission"),
        (1143, "SELECT command denied for column argument", "no_permission"),
        (1045, "Access denied", "no_permission"),
        (1146, "Table mysql.general_log doesn't exist", "not_enabled"),
        (1054, "Unknown column event_time", "incompatible"),
        (2013, "Lost connection to MySQL server during query", "error"),
    ],
)
def test_native_read_failures_keep_cause_and_summary(monkeypatch, code, message, cause):
    connector, conn, _ = _connector(monkeypatch, fail_on=("SELECT event_time", pymysql.OperationalError(code, message)))
    with pytest.raises(AuditUnavailable, match=message) as caught:
        connector.ddl_events(T0, T0)
    assert caught.value.cause == cause and conn.closed


def test_connection_failures_are_audit_unavailable(monkeypatch):
    connector, _, _ = _connector(monkeypatch)

    def fail(**kw):
        raise pymysql.OperationalError(2003, "Can't connect to MySQL server")

    monkeypatch.setattr(connector, "_connect", fail)
    with pytest.raises(AuditUnavailable, match="Can't connect") as caught:
        connector.ddl_events(T0, T0)
    assert caught.value.cause == "error"


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        (SCHEMA[:-1], "缺少列 argument"),
        ([{**r, "Type": "datetime(6)"} if r["Field"] == "event_time" else r for r in SCHEMA], "必须为 TIMESTAMP"),
        ([{**r, "Type": "int"} if r["Field"] == "user_host" else r for r in SCHEMA], "必须为文本或二进制列"),
    ],
)
def test_incompatible_structure_is_not_reported_as_empty(monkeypatch, schema, message):
    connector, conn, cur = _connector(monkeypatch, schema=schema)
    with pytest.raises(AuditUnavailable, match=message) as caught:
        connector.ddl_events(T0, T0)
    assert caught.value.cause == "incompatible" and conn.closed
    assert not any(sql.startswith("SELECT event_time") for sql, _ in cur.calls)


@pytest.mark.parametrize("offset", [-5, 0, 8])
def test_timestamp_is_read_in_utc_and_aware_window_converted(monkeypatch, offset):
    connector, conn, cur = _connector(monkeypatch, [_row()], offset=offset)
    local = timezone(timedelta(hours=8))
    since = T0.replace(tzinfo=timezone.utc).astimezone(local) - timedelta(seconds=1)
    until = since + timedelta(seconds=2)
    event = connector.ddl_events(since, until)[0]
    assert event.at == T0 and event.at.tzinfo is None
    assert cur.calls[-2] == ("SET SESSION time_zone = '+00:00'", None)
    assert cur.calls[-1][1] == (T0 - timedelta(seconds=1), T0 + timedelta(seconds=1))
    assert not any("log_timestamps" in sql for sql, _ in cur.calls)
    assert conn.closed


def test_query_and_execute_are_candidates_but_prepare_and_dml_are_not(monkeypatch):
    rows = [
        _row(command="Prepare"),
        _row(command="Execute", user="bob[bob] @ server [::1]"),
        _row(),
        _row("SELECT 'ALTER TABLE store.sales ADD COLUMN channel int'"),
        _row("ALTER TABLE private.sales ADD COLUMN channel int"),
    ]
    connector, _, cur = _connector(monkeypatch, rows)
    events = connector.ddl_events(T0, T0)
    assert [e.db_user for e in events] == ["bob", "alice"]
    assert all(e.objects == [("store", "sales")] and e.certain is None for e in events)
    assert "command_type IN ('Query', 'Execute')" in cur.calls[-1][0]
    for word in ("CREATE", "ALTER", "DROP", "RENAME", "TRUNCATE"):
        assert f"LIKE '%%{word}%%'" in cur.calls[-1][0]
        assert f"LIKE '%{word}%'" in mogrify(*cur.calls[-1])
    assert "CONVERT(argument USING utf8mb4)" in cur.calls[-1][0]


@pytest.mark.parametrize(
    ("statement", "kind", "objects"),
    [
        ("/* comment */ ALTER TABLE `store`.`sales` CHANGE COLUMN `old` `new` bigint", "ALTER", [("store", "sales")]),
        ("# MySQL comment\nALTER TABLE store.sales ADD COLUMN x int", "ALTER", [("store", "sales")]),
        ("ALTER TABLE store.sales ADD COLUMN x text COMMENT 'a\\'b'", "ALTER", [("store", "sales")]),
        ("CREATE TABLE store.copy AS SELECT * FROM private.original", "CREATE", [("store", "copy")]),
        (
            "CREATE ALGORITHM=MERGE DEFINER=`u`@`%` SQL SECURITY DEFINER VIEW store.v AS SELECT x FROM store.sales",
            "CREATE",
            [("store", "v")],
        ),
        ("ALTER VIEW store.v AS SELECT x FROM store.sales", "ALTER", [("store", "v")]),
        ("DROP TABLE IF EXISTS store.sales, store.shops", "DROP", [("store", "sales"), ("store", "shops")]),
        (
            "RENAME TABLE store.old TO store.new, store.x TO other.y",
            "RENAME",
            [("store", "old"), ("store", "new"), ("store", "x"), ("other", "y")],
        ),
        ("ALTER TABLE store.old RENAME TO store.new", "ALTER", [("store", "old"), ("store", "new")]),
        ("ALTER TABLE store.old RENAME AS store.new", "ALTER", [("store", "old"), ("store", "new")]),
        ("ALTER TABLE store.old RENAME store.new", "ALTER", [("store", "old"), ("store", "new")]),
        ("ALTER TABLE store.sales RENAME COLUMN old TO new", "ALTER", [("store", "sales")]),
        ("CREATE INDEX ix ON store.sales(channel)", "CREATE INDEX", [("store", "sales")]),
        ("CREATE UNIQUE INDEX ix ON store.sales(channel)", "CREATE INDEX", [("store", "sales")]),
        ("DROP INDEX ix ON store.sales", "DROP INDEX", [("store", "sales")]),
        ("CREATE TABLE `odd``db`.`odd.name` (id int)", "CREATE", [("odd`db", "odd.name")]),
        ("TRUNCATE TABLE store.sales", "TRUNCATE", [("store", "sales")]),
    ],
)
def test_only_explicit_ddl_targets_are_parsed(statement, kind, objects):
    assert _objects(statement) == (kind, objects)


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE DATABASE store",
        "CREATE USER 'u'@'%' IDENTIFIED BY 'unused'",
        "SELECT * FROM store.sales",
        "ALTER TABLE",
        "ALTER TABLE `store.sales ADD COLUMN x int",
    ],
)
def test_unresolved_objects_and_batches_do_not_guess(statement):
    assert _objects(statement)[1] == []


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE sales ADD COLUMN x int",
        "RENAME TABLE store.old TO new",
        "CREATE TABLE sales (x int)",
        "DROP TABLE private.a, sales",
        "RENAME TABLE private.a TO private.b, sales TO other",
        "CREATE UNIQUE INDEX ix ON sales(x)",
        "DROP INDEX ix ON sales",
    ],
)
def test_any_unqualified_ddl_target_is_incompatible(statement):
    with pytest.raises(AuditUnavailable, match="缺少当前库上下文") as caught:
        _objects(statement, {"store"})
    assert caught.value.cause == "incompatible"


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE store.sales ADD x int; DROP TABLE store.shops",
        "SELECT 1; ALTER TABLE store.sales ADD x int",
    ],
)
def test_real_multistatement_requests_with_relevant_ddl_are_incompatible(statement):
    with pytest.raises(AuditUnavailable, match="包含多条语句") as caught:
        _objects(statement, {"store"})
    assert caught.value.cause == "incompatible"


def test_fully_qualified_multistatement_outside_collected_databases_can_be_ignored():
    assert _objects("ALTER TABLE private.a ADD x int; DROP TABLE private.b", {"store"})[1] == []


@pytest.mark.parametrize("tail", ["-- trailing comment", "/* trailing comment */", "# trailing comment"])
def test_single_ddl_with_trailing_commented_semicolon_keeps_target(tail):
    assert _objects(f"ALTER TABLE store.sales ADD x int; {tail}") == ("ALTER", [("store", "sales")])


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TEMPORARY TABLE store.sales (x int)",
        "DROP TEMPORARY TABLE store.sales",
        "# MySQL comment\nCREATE TEMPORARY TABLE store.sales (x int)",
    ],
)
def test_explicit_temporary_tables_never_explain_persistent_table_changes(statement):
    assert _objects(statement)[1] == []


@pytest.mark.parametrize(
    "statement",
    [
        "/*!80000 ALTER TABLE store.sales ADD COLUMN channel int */",
        "ALTER /*!80000 TABLE */ store.sales ADD COLUMN channel int",
    ],
)
def test_executable_mysql_comments_are_incompatible(statement):
    with pytest.raises(AuditUnavailable, match="包含 MySQL 可执行注释") as caught:
        _objects(statement)
    assert caught.value.cause == "incompatible"


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT '/*!80000 ALTER TABLE store.sales ADD x INT */'",
        "SELECT 1 /* ALTER TABLE store.sales ADD x INT */",
        "# ALTER TABLE store.sales ADD x INT\nSELECT 1",
        "SELECT 1 -- ALTER TABLE store.sales ADD x INT\n",
        "/* /*!80000 ALTER TABLE store.sales ADD x INT */ SELECT 1",
    ],
)
def test_strings_and_nonexecutable_comments_are_not_mistaken_for_ddl(statement):
    assert _objects(statement)[1] == []


@pytest.mark.parametrize("action", ["DISCARD TABLESPACE", "IMPORT TABLESPACE", "CONVERT TO CHARACTER SET utf8mb4"])
def test_valid_mysql_actions_unsupported_by_sqlglot_preserve_target(action, caplog):
    statement = f"ALTER TABLE `store`.`sales` {action} /* private_business_comment */"
    assert _objects(statement) == ("ALTER", [("store", "sales")])
    assert column_refs(statement, "sales", "insensitive", schema="store").unknown
    assert "private_business_comment" not in caplog.text


def test_sqlglot_parse_error_preserves_only_confirmed_ddl_target(monkeypatch):
    def fail(*args, **kw):
        raise mysql_audit.SqlglotError("unsupported syntax")

    monkeypatch.setattr(mysql_audit.sqlglot, "parse", fail)
    assert _objects("CREATE TABLE store.copy AS SELECT x FROM private.source") == ("CREATE", [("store", "copy")])
    assert _objects("CREATE USER 'u'@'%' IDENTIFIED BY 'unused'")[1] == []
    with pytest.raises(AuditUnavailable, match="缺少当前库上下文"):
        _objects("ALTER TABLE sales ADD COLUMN x int")


def test_excess_candidates_are_unavailable_instead_of_partial_attribution(monkeypatch):
    monkeypatch.setattr(mysql_audit, "_AUDIT_ROW_LIMIT", 2)
    connector, conn, cur = _connector(monkeypatch, [_row(), _row(), _row()])
    with pytest.raises(AuditUnavailable, match="超过读取上限.*无法完整归因") as caught:
        connector.ddl_events(T0, T0)
    assert caught.value.cause == "error" and conn.closed
    assert cur.calls[-1][0].endswith("LIMIT 3")


@pytest.mark.parametrize(
    "user_host",
    [
        "",
        "alice",
        "[root] @ localhost [127.0.0.1]",
        "alice@localhost",
        "[someone[spoof] @ localhost [127.0.0.1]",
        "alice[someone[spoof] @ localhost [127.0.0.1]",
    ],
)
def test_unknown_user_is_incompatible_instead_of_empty_user_match(monkeypatch, user_host):
    connector, conn, _ = _connector(monkeypatch, [_row(user=user_host)])
    with pytest.raises(AuditUnavailable, match="无法解析数据库用户名") as caught:
        connector.ddl_events(T0, T0)
    assert caught.value.cause == "incompatible" and conn.closed


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("argument", b"\xff", "UTF-8"),
        ("user_host", None, "UTF-8"),
        ("event_time", "0000-00-00 00:00:00", "有效的时间值"),
    ],
)
def test_invalid_audit_values_fail_explicitly(monkeypatch, field, value, message):
    connector, _, _ = _connector(monkeypatch, [{**_row(), field: value}])
    with pytest.raises(AuditUnavailable, match=message) as caught:
        connector.ddl_events(T0, T0)
    assert caught.value.cause == "incompatible"


def test_general_log_binary_argument_and_column_evidence(monkeypatch):
    statement = "ALTER TABLE `store`.`sales` CHANGE COLUMN `old` `new` bigint COMMENT 'channel'"
    connector, _, _ = _connector(monkeypatch, [_row(statement.encode())])
    event = connector.ddl_events(T0, T0)[0]
    refs = column_refs(event.statement, "sales", "insensitive", schema="store")
    assert [t.text for t in refs.columns] == ["old", "new"]
    assert not refs.unknown


def test_user_host_preserves_authenticated_account_instead_of_submitted_login(monkeypatch):
    connector, _, _ = _connector(monkeypatch, [_row(user="auth_account[submitted_login] @ host [127.0.0.1]")])
    assert connector.ddl_events(T0, T0)[0].db_user == "auth_account"


def test_attribution_requires_structural_object_column_and_window_corroboration(monkeypatch):
    connector, _, _ = _connector(monkeypatch, [_row()])
    added, different_column, different_object = (
        _event("column_added", "channel"),
        _event("column_added", "elsewhere"),
        _event("column_added", "channel", asset_id="a2"),
    )
    _attribute([added, different_column, different_object], connector, engine="mysql")
    assert (added["attribution_status"], added["actor_db_user"]) == ("matched", "alice")
    assert different_column["attribution_status"] == different_object["attribution_status"] == "unmatched"
    # 日志没有成功标志；只有结构差异参与现有规则的交叉核验，语句原文不会进入归因留底。
    assert "varchar" not in repr(added["attribution"])


def test_attribution_keeps_boundary_multiuser_and_unknown_column_ambiguous(monkeypatch):
    for rows, expected in [
        ([_row(at=WINDOW.previous_done)], "ambiguous"),
        ([_row(), _row(user="bob[bob] @ host [::1]")], "ambiguous"),
        ([_row("TRUNCATE TABLE store.sales")], "ambiguous"),
        ([_row("ALTER TABLE store.sales SOMETHING_UNRECOGNIZED")], "ambiguous"),
    ]:
        connector, _, _ = _connector(monkeypatch, rows)
        event = _event("column_added", "channel")
        _attribute([event], connector, engine="mysql")
        assert event["attribution_status"] == expected
        assert event["actor_db_user"] is None


def test_unrecognized_action_from_another_user_prevents_false_single_user_match(monkeypatch):
    connector, _, _ = _connector(
        monkeypatch,
        [
            _row(),
            _row("ALTER TABLE store.sales CONVERT TO CHARACTER SET utf8mb4", user="bob[bob] @ host [::1]"),
        ],
    )
    event = _event("column_added", "channel")
    _attribute([event], connector, engine="mysql")
    assert event["attribution_status"] == "ambiguous" and event["actor_db_user"] is None
    assert event["attribution"]["candidates"] == ["alice", "bob"]


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE sales ADD COLUMN channel int",
        "ALTER TABLE store.sales ADD COLUMN channel int; ALTER TABLE store.shops ADD COLUMN x int",
        "SELECT 1; ALTER TABLE store.sales ADD COLUMN channel int",
        "/*!80000 ALTER TABLE store.sales ADD COLUMN channel int */",
    ],
)
def test_uncertain_ddl_request_from_other_user_cannot_leave_a_false_single_user_match(monkeypatch, statement):
    connector, _, _ = _connector(monkeypatch, [_row(), _row(statement, user="bob[bob] @ host [::1]")])
    event = _event("column_added", "channel")
    _attribute([event], connector, engine="mysql")
    assert event["attribution_status"] == "unavailable" and event["actor_db_user"] is None
    assert event["attribution"]["cause"] == "incompatible"


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE UNIQUE INDEX ix ON store.sales(channel)",
        "CREATE INDEX ix ON store.sales(channel)",
        "DROP INDEX ix ON store.sales",
    ],
)
def test_index_ddl_targets_table_but_cannot_falsely_match_arbitrary_column_changes(monkeypatch, statement):
    connector, _, _ = _connector(monkeypatch, [_row(statement)])
    event = connector.ddl_events(T0, T0)[0]
    assert event.objects == [("store", "sales")] and event.certain is False
    changed = _event("column_key_changed", "channel")
    _attribute([changed], connector, engine="mysql")
    assert changed["attribution_status"] == "ambiguous" and changed["actor_db_user"] is None


def test_temporary_table_ddl_is_excluded_even_when_persistent_table_name_matches(monkeypatch):
    connector, _, _ = _connector(
        monkeypatch,
        [
            _row("CREATE TEMPORARY TABLE store.sales (channel int)", user="bob[bob] @ host [::1]"),
            _row("DROP TEMPORARY TABLE store.sales", user="bob[bob] @ host [::1]"),
        ],
    )
    changed = _event("comment_changed")
    _attribute([changed], connector, engine="mysql")
    assert changed["attribution_status"] == "unmatched" and changed["actor_db_user"] is None


def test_attribution_rename_and_view_redefinition(monkeypatch):
    connector, _, _ = _connector(
        monkeypatch,
        [
            _row("RENAME TABLE store.old TO store.new"),
            _row("CREATE OR REPLACE VIEW store.v AS SELECT channel FROM store.sales"),
        ],
    )
    renamed = _event("renamed", asset_id="renamed", before={"name": "def.store.old"}, after={"name": "def.store.new"})
    view_column = _event("column_added", "channel", asset_id="view")
    _attribute(
        [renamed, view_column],
        connector,
        engine="mysql",
        assets={"renamed": _asset(name="new"), "view": _asset(name="v")},
    )
    assert renamed["actor_db_user"] == view_column["actor_db_user"] == "alice"


def test_attribution_explains_unbound_instead_of_claiming_no_ddl(monkeypatch):
    connector, _, _ = _connector(monkeypatch, source=None)
    event = _event("column_added", "channel")
    result = _attribute([event], connector, engine="mysql")
    assert result["status"] == event["attribution_status"] == "unsupported"
    assert event["actor_db_user"] is None
    assert event["attribution"]["reason"] == connector.ddl_audit_unbound_reason
    assert "未绑定审计表" in event["attribution"]["reason"]
