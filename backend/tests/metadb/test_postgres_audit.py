"""PostgreSQL DDL 审计来源（ADR 0008；# 编号对应 PR #9 审计报告）。源库是开发者自己的 <库名>_src，部署了事件触发器。

不同数据库用户用“在执行 DDL 的事务里改写本事务审计记录的 db_user”模拟（避免在共享实例上建角色）。
"""

from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest

from avelis.collect.attribution import Window, attribute
from avelis.connectors.ddl import AuditUnavailable
from avelis.connectors.postgres import PostgresConnector
from avelis.db import utcnow
from avelis.engines import get_engine

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def src(source_db):
    args, name = source_db
    connector = PostgresConnector(args["host"], args["port"], args["user"], args["password"],
                                  {"database": name, "schemas": ["store"]})

    def connect(autocommit=True):
        return psycopg.connect(**args, dbname=name, autocommit=autocommit)

    return connector, connect


def _as(conn, user: str) -> None:
    conn.execute("UPDATE avelis_audit.ddl_event SET db_user = %s WHERE xid = pg_current_xact_id()", (user,))


def _ddl(connect, user: str, *statements: str) -> None:
    with connect(autocommit=False) as conn:
        for s in statements:
            conn.execute(s)
        _as(conn, user)
        conn.commit()


def _window(p, c) -> Window:
    now = utcnow()
    return Window(now - timedelta(hours=1), now - timedelta(hours=1), now, now, p, c)


class _Session:
    def __init__(self, assets):
        self.assets = assets

    def get(self, _model, key):
        return self.assets.get(key)


def _attribute(connector, events, window):
    from types import SimpleNamespace

    return attribute(_Session({"s": SimpleNamespace(db_name="store", name="sales")}), get_engine("postgres"),
                     connector, events, window)


def test_long_transaction_belongs_to_the_window_it_committed_in(src):
    # 5：bob 的事务在上次快照前开始、快照后提交；alice 随后在同一窗口改同一列
    connector, connect = src
    bob = connect(autocommit=False)
    try:
        bob.execute("COMMENT ON COLUMN store.sales.amount IS 'bob'")
        _as(bob, "bob")
        previous = connector.snapshot().audit_cursor
        bob.commit()
    finally:
        bob.close()
    middle = connector.snapshot().audit_cursor
    got = connector.ddl_events(utcnow(), utcnow(), cursors=(previous, middle))  # 时间参数在按事务取时不用
    assert [(e.db_user, e.certain) for e in got] == [("bob", True)]
    event = {"kind": "column_comment_changed", "asset_id": "s", "column_name": "amount", "before": {}, "after": {}}
    _attribute(connector, [event], _window(previous, middle))
    assert (event["attribution_status"], event["actor_db_user"]) == ("matched", "bob")

    _ddl(connect, "alice", "COMMENT ON COLUMN store.sales.amount IS 'alice'")
    current = connector.snapshot().audit_cursor
    both = {"kind": "column_comment_changed", "asset_id": "s", "column_name": "amount", "before": {}, "after": {}}
    _attribute(connector, [both], _window(previous, current))
    assert both["attribution_status"] == "ambiguous" and both["attribution"]["candidates"] == ["alice", "bob"]
    # 下一个窗口只有 alice：bob 的事务已在上个窗口
    assert [e.db_user for e in connector.ddl_events(utcnow(), utcnow(), cursors=(middle, current))] == ["alice"]


def test_disabled_or_missing_event_trigger_means_not_enabled(src):
    # 4：触发器关闭期间的改动不会留痕；残留的旧记录不能再当作确定依据
    connector, connect = src
    _ddl(connect, "alice", "ALTER TABLE store.sales ALTER COLUMN notes SET DEFAULT 'x'")
    with connect() as conn:
        conn.execute("ALTER EVENT TRIGGER avelis_ddl_end DISABLE")
    try:
        _ddl(connect, "bob", "ALTER TABLE store.sales ALTER COLUMN notes TYPE varchar(200)")
        with pytest.raises(AuditUnavailable) as info:
            connector.ddl_events(utcnow() - timedelta(hours=1), utcnow())
        assert info.value.cause == "not_enabled" and "avelis_ddl_end" in str(info.value)
        event = {"kind": "column_type_changed", "asset_id": "s", "column_name": "notes", "before": {}, "after": {}}
        now = utcnow()
        _attribute(connector, [event], Window(now - timedelta(hours=1), now - timedelta(hours=1), now, now))
        assert event["attribution_status"] == "unavailable" and event["actor_db_user"] is None
        assert event["attribution"]["cause"] == "not_enabled" and "未开启" in event["attribution"]["reason"]
    finally:
        with connect() as conn:
            conn.execute("ALTER EVENT TRIGGER avelis_ddl_end ENABLE")


def test_source_structure_errors_are_not_reported_as_disabled(src):
    # 18：审计表缺列 → incompatible；审计表不存在 → not_enabled
    connector, connect = src
    with connect() as conn:
        conn.execute("ALTER TABLE avelis_audit.ddl_event RENAME COLUMN statement TO stmt")
    try:
        with pytest.raises(AuditUnavailable) as info:
            connector.ddl_events(utcnow() - timedelta(hours=1), utcnow())
        assert info.value.cause == "incompatible"
    finally:
        with connect() as conn:
            conn.execute("ALTER TABLE avelis_audit.ddl_event RENAME COLUMN stmt TO statement")
    with connect() as conn:
        conn.execute("ALTER TABLE avelis_audit.ddl_event RENAME TO ddl_event_moved")
    try:
        with pytest.raises(AuditUnavailable) as info:
            connector.ddl_events(utcnow() - timedelta(hours=1), utcnow())
        assert info.value.cause == "not_enabled"
    finally:
        with connect() as conn:
            conn.execute("ALTER TABLE avelis_audit.ddl_event_moved RENAME TO ddl_event")


def test_batched_request_keeps_no_statement_text(src):
    # 1：一个请求里多条语句（含口令一样的敏感值、另一个对象）：可以按对象与列归因，但摘要为空
    connector, connect = src
    previous = connector.snapshot().audit_cursor
    with connect(autocommit=False) as conn:
        conn.execute("CREATE TABLE store.hidden (a int); COMMENT ON TABLE store.hidden IS $$s3cr3t$$; "
                     "ALTER TABLE store.sales ADD COLUMN channel text")
        _as(conn, "carol")
        conn.commit()
    current = connector.snapshot().audit_cursor
    event = {"kind": "column_added", "asset_id": "s", "column_name": "channel", "before": {}, "after": {}}
    _attribute(connector, [event], _window(previous, current))
    assert event["actor_db_user"] == "carol"
    assert all("summary" not in s and set(s) <= {"at", "db_user", "statement_type", "certain", "xid"}
               for s in event["attribution"]["statements"])
    assert all(s.get("xid") for s in event["attribution"]["statements"])  # 结构化信息里带事务号
    assert "s3cr3t" not in repr(event["attribution"]) and "hidden" not in repr(event["attribution"]["statements"])


def test_audit_table_has_time_and_transaction_indexes(src):
    # 15：按 (at, id) 与 xid 读取都有索引；模式与对象类型过滤在库里完成
    _, connect = src
    with connect() as conn:
        names = {r[0] for r in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'avelis_audit' AND tablename = 'ddl_event'")}
    assert {"ddl_event_at_id", "ddl_event_xid"} <= names


def test_audit_connection_must_be_the_snapshot_instance(src):
    # 第三轮复核 4：没有可信实例 ID 时，实例键是（端点、postmaster 启动时间、库 oid）；审计连接也要核对一次，
    # 对不上或无法确认都按实例已切换处理
    connector, _ = src
    cursor = connector.snapshot().audit_cursor
    assert cursor["instance"] and cursor["pg_snapshot"]
    # 读得到 system_identifier 与时间线（PG 18 默认 PUBLIC 可执行）：键里带时间线与启动时间（第四、五轮复核）
    assert cursor["instance"].startswith("pg:") and "/tl" in cursor["instance"]
    assert connector.ddl_events(utcnow(), utcnow(), cursors=(cursor, cursor)) == []
    for other in ({**cursor, "instance": "127.0.0.1:1|2000-01-01 00:00:00+00|1"}, {**cursor, "instance": None}):
        with pytest.raises(AuditUnavailable) as info:
            connector.ddl_events(utcnow(), utcnow(), cursors=(cursor, other))
        assert info.value.cause == "instance_changed"
