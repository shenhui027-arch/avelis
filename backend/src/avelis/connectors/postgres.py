"""PostgreSQL 连接器（真实数据库）。

- 资产名三段：库（catalog 段）. 模式. 表。只采配置的库与模式（options.database / options.schemas）。
- 采集账号只读系统目录 pg_catalog（不需要任何表的 SELECT 权限）：pg_class、pg_attribute、pg_constraint、pg_get_viewdef。
- 物理身份用 pg_class.oid：改名不变，删除后重建会变（与 StarRocks TABLE_ID 的用法一致）。
- 会话 search_path 置空，pg_get_viewdef 输出完全限定的表名，解析时不需要猜模式。
- 问数执行：只读事务 + statement_timeout；SQLSTATE 57014 归为超时，42703 / 42P01 归为对象无法解析。
- DDL 审计（ADR 0008）：事件触发器（ddl_command_end + sql_drop）写入 avelis_audit.ddl_event，部署脚本见
  deploy/demo/shop-pg/init/03-ddl-audit.sql；采集账号只需该表的 SELECT。db_user 为 session_user（登录用户）。
  快照在可重复读事务里读，并记下 pg_current_snapshot()；归因按审计记录的事务号（xid）在两次快照中的可见性取事件，
  跨采集提交的长事务也能归到正确的窗口。读取前核对两个事件触发器存在、已启用且指向预期函数。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg import sql

from avelis.connectors.base import ObservedAsset, ObservedColumn, QueryError, QueryResult, Snapshot
from avelis.connectors.ddl import AuditUnavailable, DdlEvent

IDENT = re.compile(r"^[A-Za-z0-9_]+$")
KINDS = {"r": "table", "p": "table", "v": "view", "m": "materialized_view", "f": "external_table"}
AUDIT_TABLE = "avelis_audit.ddl_event"
AUDIT_OBJECT_TYPES = ("table", "view", "materialized view", "foreign table", "table column")
AUDIT_TRIGGERS = {"avelis_ddl_end": "avelis_audit.on_ddl_end()", "avelis_sql_drop": "avelis_audit.on_sql_drop()"}


def _checked(names: list[str], label: str) -> list[str]:
    for name in names:
        if not IDENT.match(name):
            raise ValueError(f"{label} 名称不合法：{name!r}")
    return names


class PostgresConnector:
    kind = "postgres"
    dialect = "postgres"
    is_test_adapter = False
    ddl_audit_source = f"postgres:{AUDIT_TABLE}"
    ddl_identifier_fold = "lower"  # 不带引号的标识符折成小写

    def __init__(self, host: str, port: int, user: str, password: str, options: dict[str, Any] | None = None):
        self.options = options or {}
        database = self.options.get("database")
        if not database or not IDENT.match(database):
            raise ValueError("PostgreSQL 数据源需要配置要采集的库（options.database）")
        self.database = database
        self._conn_args = dict(host=host, port=port, user=user, password=password, dbname=database)

    def _connect(self) -> psycopg.Connection:
        # 空 search_path：系统函数输出的对象名全部带模式
        return psycopg.connect(**self._conn_args, connect_timeout=5, autocommit=True,
                               options="-c search_path= -c lock_timeout=5000")

    # --- 采集 ---------------------------------------------------------------------

    def server_identity(self) -> dict[str, Any]:
        with self._connect() as conn:
            return self._server_identity(conn)

    def _server_identity(self, conn: psycopg.Connection) -> dict[str, Any]:
        identity: dict[str, Any] = {
            "version": conn.execute("SELECT current_setting('server_version')").fetchone()[0],
            "endpoint": f"{self._conn_args['host']}:{self._conn_args['port']}", "database": self.database}
        try:
            identity["system_identifier"] = str(conn.execute(
                "SELECT system_identifier FROM pg_catalog.pg_control_system()").fetchone()[0])
        except psycopg.Error:
            identity["system_identifier"] = None  # 需要 pg_read_all_settings 或超级用户；默认不授予
        if identity["system_identifier"]:
            identity["instance_id"] = f"pg:{identity['system_identifier']}/{self.database}"
        return identity

    def snapshot(self) -> Snapshot:
        schemas = _checked(list(self.options.get("schemas", [])), "模式")
        if not schemas:
            raise ValueError("数据源未配置要采集的模式（options.schemas）")
        warnings: list[str] = []
        with self._connect() as conn:
            identity = self._server_identity(conn)
            instance = self._instance_key(conn, identity)  # 在自动提交下读：出错不会中止后面的只读事务
            # 元数据在同一个可重复读只读事务里读，并记下这个事务快照：归因据此判断 DDL 事务在不在两次快照之间
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                cursor = {"pg_snapshot": conn.execute("SELECT pg_catalog.pg_current_snapshot()::text").fetchone()[0],
                          "instance": instance}
                relations = conn.execute(
                    "SELECT c.oid, n.nspname, c.relname, c.relkind, pg_catalog.obj_description(c.oid, 'pg_class') "
                    "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = ANY(%s) AND c.relkind = ANY(%s) ORDER BY n.nspname, c.relname",
                    (schemas, list(KINDS))).fetchall()
                oids = [r[0] for r in relations]
                columns = self._columns(conn, oids, with_comments=True)
                keys: dict[int, dict[str, list[list[str]]]] = {}
                for oid, contype, names in conn.execute(
                        "SELECT con.conrelid, con.contype, array_agg(a.attname ORDER BY k.ord) "
                        "FROM pg_catalog.pg_constraint con "
                        "CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) "
                        "JOIN pg_catalog.pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum "
                        "WHERE con.conrelid = ANY(%s) AND con.contype IN ('p', 'u') "
                        "GROUP BY con.oid, con.conrelid, con.contype", (oids,)).fetchall():
                    keys.setdefault(oid, {"p": [], "u": []})[contype].append(list(names))
                definitions = self._definitions(conn, [r[0] for r in relations if r[3] in ("v", "m")])
        assets = []
        for oid, schema, name, relkind, comment in relations:
            kind = KINDS[relkind]
            pk = (keys.get(oid, {}).get("p") or [[]])[0]
            unique = keys.get(oid, {}).get("p", []) + keys.get(oid, {}).get("u", [])
            definition = definitions.get(oid)
            if kind in ("view", "materialized_view") and definition is None:
                warnings.append(f"视图 {schema}.{name} 未读到定义")
            pk_set = set(pk)
            assets.append(ObservedAsset(
                catalog=self.database, db=schema, name=name, kind=kind,
                columns=[ObservedColumn(name=c[0], data_type=c[1], ordinal=c[2], nullable=c[3], comment=c[4],
                                        is_key=c[0] in pk_set) for c in columns.get(oid, [])],
                physical_id=str(oid), key_columns=pk,
                unique_keys=sorted(unique), comment=comment, definition=definition,
                properties={"relkind": relkind},
            ))
        return Snapshot(server_identity=identity, assets=assets, warnings=warnings, audit_cursor=cursor)

    @staticmethod
    def _columns(conn: psycopg.Connection, oids: list[int], with_comments: bool = False) -> dict[int, list[tuple]]:
        out: dict[int, list[tuple]] = {}
        if not oids:
            return out
        comment = "pg_catalog.col_description(a.attrelid, a.attnum)" if with_comments else "NULL"
        for oid, name, data_type, ordinal, nullable, col_comment in conn.execute(
                "SELECT a.attrelid, a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnum, "
                f"NOT a.attnotnull, {comment} FROM pg_catalog.pg_attribute a "
                "WHERE a.attrelid = ANY(%s) AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attrelid, a.attnum",
                (oids,)).fetchall():
            out.setdefault(oid, []).append((name, data_type, int(ordinal), bool(nullable), col_comment))
        return out

    @staticmethod
    def _definitions(conn: psycopg.Connection, oids: list[int]) -> dict[int, str]:
        if not oids:
            return {}
        rows = conn.execute("SELECT c.oid, pg_catalog.pg_get_viewdef(c.oid, true) FROM pg_catalog.pg_class c "
                            "WHERE c.oid = ANY(%s)", (oids,)).fetchall()
        return {oid: definition.strip().rstrip(";") for oid, definition in rows if definition}

    # --- 只读执行 ------------------------------------------------------------------

    def execute_select(self, sql_text: str, *, timeout_seconds: int, mem_limit_bytes: int,
                       max_rows: int) -> QueryResult:
        """只读事务内执行；PostgreSQL 没有单查询内存上限，靠 statement_timeout 与行数上限约束。"""
        try:
            with self._connect() as conn:
                conn.autocommit = False
                with conn.transaction():
                    conn.execute("SET TRANSACTION READ ONLY")
                    conn.execute(sql.SQL("SET LOCAL statement_timeout = {}").format(
                        sql.Literal(f"{int(timeout_seconds)}s")))
                    cur = conn.execute(sql_text)
                    rows = cur.fetchmany(max_rows + 1)
                    columns = [d.name for d in cur.description or []]
        except psycopg.Error as exc:
            raise QueryError(_classify(exc), exc.sqlstate, _message(exc)) from exc
        return QueryResult(columns=columns, rows=[list(r) for r in rows[:max_rows]], truncated=len(rows) > max_rows)

    def ddl_events(self, since: datetime, until: datetime, cursors=None) -> list[DdlEvent]:
        """读 DDL 审计。两次快照都记了事务快照时，按事务可见性取“上次快照看不到、本次看得到”的事务，结果确定；
        否则（升级后第一次、上次快照没有游标）按执行时间取窗口内的记录，一律标为不确定（长事务可能跨窗口）。
        事件触发器不存在、已停用或指向别的函数时，审计视为未开启；审计连接的实例与本次快照不同（或无法确认）时，
        视为实例已切换。"""
        schemas = list(self.options.get("schemas", []))
        previous, current = cursors or (None, None)
        p_snap = (previous or {}).get("pg_snapshot")
        c_snap = (current or {}).get("pg_snapshot")
        select = ("SELECT extract(epoch FROM at), db_user, command_tag, object_names, statement, xid::text "
                  f"FROM {AUDIT_TABLE} WHERE object_type = ANY(%s) AND object_names[1] = ANY(%s) "
                  "AND array_length(object_names, 1) >= 2 AND ")
        try:
            with self._connect() as conn:
                self._check_audit_triggers(conn)
                expected = (current or {}).get("instance")
                if current and (expected is None or self._instance_key(conn) != expected):
                    # 审计连接与本次快照连接不是同一个实例（或无法确认）：不能用快照游标解释这里的事务号
                    raise AuditUnavailable("审计连接所在的实例与本次快照不一致或无法确认", "instance_changed")
                if p_snap and c_snap:
                    rows = conn.execute(
                        select + "xid >= pg_catalog.pg_snapshot_xmin(%s::pg_snapshot) "
                        "AND pg_catalog.pg_visible_in_snapshot(xid, %s::pg_snapshot) "
                        "AND NOT pg_catalog.pg_visible_in_snapshot(xid, %s::pg_snapshot) ORDER BY at, id",
                        (list(AUDIT_OBJECT_TYPES), schemas, p_snap, c_snap, p_snap)).fetchall()
                else:
                    rows = conn.execute(select + "at >= %s AND at <= %s ORDER BY at, id",
                                        (list(AUDIT_OBJECT_TYPES), schemas, since.replace(tzinfo=timezone.utc),
                                         until.replace(tzinfo=timezone.utc))).fetchall()
        except psycopg.Error as exc:
            raise AuditUnavailable(_message(exc), _audit_cause(exc)) from exc
        by_transaction = bool(p_snap and c_snap)
        return [DdlEvent(at=datetime.fromtimestamp(float(ts), tz=timezone.utc).replace(tzinfo=None), db_user=user,
                         objects=[(names[0], names[1])], statement_type=tag, statement=statement or "",
                         certain=by_transaction, xid=xid)  # 没有两次游标时只能按时间取，结果一律不确定
                for ts, user, tag, names, statement, xid in rows]

    def _instance_key(self, conn: psycopg.Connection, identity: dict[str, Any] | None = None) -> str | None:
        """事务号只在同一个实例内可比：有 system_identifier 时用它；没有时用（端点、postmaster 启动时间、库 oid），
        实例重启或切换都会让键变化（归因随之重建基线）。都读不到时返回 None，按实例已切换处理。"""
        identity = identity if identity is not None else self._server_identity(conn)
        try:
            start, db_oid = conn.execute(
                "SELECT pg_catalog.pg_postmaster_start_time()::text, d.oid::text FROM pg_catalog.pg_database d "
                "WHERE d.datname = pg_catalog.current_database()").fetchone()
        except psycopg.Error:
            return identity.get("instance_id")
        return identity.get("instance_id") or f"{identity['endpoint']}|{start}|{db_oid}"

    @staticmethod
    def _check_audit_triggers(conn: psycopg.Connection) -> None:
        found = {name: (enabled, func) for name, enabled, func in conn.execute(
            "SELECT e.evtname, e.evtenabled, e.evtfoid::regprocedure::text FROM pg_catalog.pg_event_trigger e "
            "WHERE e.evtname = ANY(%s)", (list(AUDIT_TRIGGERS),)).fetchall()}
        for name, func in AUDIT_TRIGGERS.items():
            if name not in found:
                raise AuditUnavailable(f"事件触发器 {name} 不存在", "not_enabled")
            enabled, actual = found[name]
            if enabled not in ("O", "A"):  # D 停用；R 只在复制会话里触发
                raise AuditUnavailable(f"事件触发器 {name} 未启用（evtenabled = {enabled}）", "not_enabled")
            if actual != func:
                raise AuditUnavailable(f"事件触发器 {name} 指向 {actual}，不是 {func}", "not_enabled")

    def freshness(self, tables: list[tuple[str, str]]) -> dict[str, Any]:
        """PostgreSQL 不记录表的最近修改时间，不报告新鲜度（不以统计信息冒充）。"""
        return {}

    def describe_objects(self, objects: list[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
        """执行前核对：给定 模式.表 当前的列（名称、类型，按位置）与视图定义；对象不存在时列为空。"""
        names = [(s, n) for s, n in objects if IDENT.match(s) and IDENT.match(n)]
        out: dict[tuple[str, str], dict[str, Any]] = {k: {"columns": [], "definition": None, "physical_id": None}
                                                      for k in names}
        if not names:
            return out
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT c.oid, n.nspname, c.relname, c.relkind FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE (n.nspname, c.relname) IN (SELECT * FROM unnest(%s::text[], %s::text[]))",
                    ([s for s, _ in names], [n for _, n in names])).fetchall()
                by_oid = {oid: (schema, name) for oid, schema, name, _ in rows}
                for oid, key in by_oid.items():
                    out[key]["physical_id"] = str(oid)
                for oid, cols in self._columns(conn, list(by_oid)).items():
                    out[by_oid[oid]]["columns"] = [[c[0], c[1]] for c in cols]
                for oid, definition in self._definitions(
                        conn, [oid for oid, _, _, kind in rows if kind in ("v", "m")]).items():
                    out[by_oid[oid]]["definition"] = definition
        except psycopg.Error as exc:
            raise QueryError(_classify(exc), exc.sqlstate, _message(exc)) from exc
        return out


def _classify(exc: psycopg.Error) -> str:
    state = exc.sqlstate or ""
    if state == "57014":
        return "timeout"
    if state in ("42501", "28000", "28P01", "25006"):  # 25006：只读事务中的写操作
        return "permission"
    if state in ("42703", "42P01", "42883", "3F000"):
        return "invalid_object"
    return "other"


def _audit_cause(exc: psycopg.Error) -> str:
    state = exc.sqlstate or ""
    if state == "42501":
        return "no_permission"
    if state in ("42P01", "3F000", "42883"):  # 审计表、模式或函数不存在
        return "not_enabled"
    if state == "42703":  # 审计表缺列
        return "incompatible"
    return "error"


def _message(exc: psycopg.Error) -> str:
    return (exc.diag.message_primary if exc.diag and exc.diag.message_primary else str(exc)).strip()[:2000]
