"""PostgreSQL 连接器（真实数据库）。

- 资产名三段：库（catalog 段）. 模式. 表。只采配置的库与模式（options.database / options.schemas）。
- 采集账号只读系统目录 pg_catalog（不需要任何表的 SELECT 权限）：pg_class、pg_attribute、pg_constraint、pg_get_viewdef。
- 物理身份用 pg_class.oid：改名不变，删除后重建会变（与 StarRocks TABLE_ID 的用法一致）。
- 会话 search_path 置空，pg_get_viewdef 输出完全限定的表名，解析时不需要猜模式。
- 问数执行：只读事务 + statement_timeout；SQLSTATE 57014 归为超时，42703 / 42P01 归为对象无法解析。
"""

from __future__ import annotations

import re
from typing import Any

import psycopg
from psycopg import sql

from avelis.connectors.base import ObservedAsset, ObservedColumn, QueryError, QueryResult, Snapshot

IDENT = re.compile(r"^[A-Za-z0-9_]+$")
KINDS = {"r": "table", "p": "table", "v": "view", "m": "materialized_view", "f": "external_table"}


def _checked(names: list[str], label: str) -> list[str]:
    for name in names:
        if not IDENT.match(name):
            raise ValueError(f"{label} 名称不合法：{name!r}")
    return names


class PostgresConnector:
    kind = "postgres"
    dialect = "postgres"
    is_test_adapter = False

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
        return Snapshot(server_identity=identity, assets=assets, warnings=warnings)

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


def _message(exc: psycopg.Error) -> str:
    return (exc.diag.message_primary if exc.diag and exc.diag.message_primary else str(exc)).strip()[:2000]
