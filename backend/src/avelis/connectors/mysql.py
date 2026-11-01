"""MySQL 元数据连接器（真实数据库，只读 information_schema）。

v0.1 只做元数据：用于把 StarRocks 外部表 / Catalog 表追到 MySQL 真实表，并提供主键、唯一键作为粒度。
实例身份用 @@server_uuid 确认（同一个库在不同网络位置的地址不同，见 endpoint_alias）。
"""

from __future__ import annotations

from typing import Any

import pymysql

from avelis.connectors.base import ObservedAsset, ObservedColumn, QueryError, QueryResult, Snapshot
from avelis.connectors.starrocks import _checked, _classify, _in_list, _set_timezone


class MySQLMetaConnector:
    kind = "mysql"
    dialect = "mysql"
    is_test_adapter = False

    def __init__(self, host: str, port: int, user: str, password: str, options: dict[str, Any] | None = None):
        self._conn_args = dict(host=host, port=port, user=user, password=password)
        self.options = options or {}

    def _connect(self, read_timeout: int = 60) -> pymysql.Connection:
        return pymysql.connect(**self._conn_args, autocommit=True, charset="utf8mb4", connect_timeout=5,
                               read_timeout=read_timeout, cursorclass=pymysql.cursors.DictCursor)

    def execute_select(self, sql: str, *, timeout_seconds: int, mem_limit_bytes: int, max_rows: int,
                       timezone: str | None = None) -> QueryResult:
        """只读事务执行；保留元数据适配器的能力声明，不据此开放未经联调的问数。"""
        conn = self._connect(read_timeout=timeout_seconds + 15)
        try:
            cur = conn.cursor(pymysql.cursors.Cursor)
            warnings = _set_timezone(cur, timezone)
            cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {int(timeout_seconds) * 1000}")
            cur.execute("START TRANSACTION READ ONLY")
            cur.execute(sql)
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(max_rows + 1)
            conn.rollback()
            return QueryResult(columns=columns, rows=[list(r) for r in rows[:max_rows]], truncated=len(rows) > max_rows,
                               warnings=warnings)
        except pymysql.MySQLError as exc:
            errno = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
            message = str(exc.args[-1]) if exc.args else str(exc)
            raise QueryError(_classify(errno, message), errno, message) from exc
        finally:
            conn.close()

    def server_identity(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            return self._server_identity(conn.cursor())
        finally:
            conn.close()

    @staticmethod
    def _server_identity(cur) -> dict[str, Any]:
        cur.execute("SELECT @@server_uuid AS server_uuid, @@hostname AS hostname, @@port AS port, "
                    "VERSION() AS version")
        row = cur.fetchone()
        return {"instance_id": row["server_uuid"], "server_uuid": row["server_uuid"], "hostname": row["hostname"],
                "port": int(row["port"]), "version": row["version"]}

    def snapshot(self) -> Snapshot:
        schemas = _checked(list(self.options.get("databases", [])), "库")
        if not schemas:
            raise ValueError("数据源未配置要采集的库（options.databases）")
        in_dbs = _in_list(schemas)
        conn = self._connect()
        try:
            cur = conn.cursor()
            identity = self._server_identity(cur)
            cur.execute("SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, TABLE_COMMENT, CREATE_TIME "
                        f"FROM information_schema.tables WHERE TABLE_SCHEMA IN ({in_dbs})")
            tables = cur.fetchall()
            cur.execute("SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, COLUMN_TYPE, IS_NULLABLE, "
                        "COLUMN_COMMENT, COLUMN_KEY FROM information_schema.columns "
                        f"WHERE TABLE_SCHEMA IN ({in_dbs})")
            columns: dict[tuple[str, str], list[ObservedColumn]] = {}
            for r in cur.fetchall():
                columns.setdefault((r["TABLE_SCHEMA"], r["TABLE_NAME"]), []).append(ObservedColumn(
                    name=r["COLUMN_NAME"], data_type=r["COLUMN_TYPE"], ordinal=int(r["ORDINAL_POSITION"]),
                    nullable=r["IS_NULLABLE"] == "YES", comment=r["COLUMN_COMMENT"] or None,
                    is_key=r["COLUMN_KEY"] in ("PRI", "UNI"),
                ))
            cur.execute("SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME "
                        "FROM information_schema.statistics "
                        f"WHERE TABLE_SCHEMA IN ({in_dbs}) AND NON_UNIQUE = 0 ORDER BY INDEX_NAME, SEQ_IN_INDEX")
            unique: dict[tuple[str, str], dict[str, list[str]]] = {}
            for r in cur.fetchall():
                unique.setdefault((r["TABLE_SCHEMA"], r["TABLE_NAME"]), {}).setdefault(
                    r["INDEX_NAME"], []).append(r["COLUMN_NAME"])
            cur.execute("SELECT TABLE_SCHEMA, TABLE_NAME, VIEW_DEFINITION FROM information_schema.views "
                        f"WHERE TABLE_SCHEMA IN ({in_dbs})")
            views = {(r["TABLE_SCHEMA"], r["TABLE_NAME"]): r["VIEW_DEFINITION"] for r in cur.fetchall()}
        finally:
            conn.close()

        assets = []
        for t in tables:
            key = (t["TABLE_SCHEMA"], t["TABLE_NAME"])
            indexes = unique.get(key, {})
            pk = indexes.get("PRIMARY", [])
            is_view = t["TABLE_TYPE"] == "VIEW"
            assets.append(ObservedAsset(
                catalog="def", db=key[0], name=key[1], kind="view" if is_view else "table",
                columns=sorted(columns.get(key, []), key=lambda c: c.ordinal),
                key_columns=pk, unique_keys=[cols for _, cols in sorted(indexes.items())],
                comment=t["TABLE_COMMENT"] or None, definition=views.get(key) if is_view else None,
                properties={"create_time": str(t["CREATE_TIME"]) if t["CREATE_TIME"] else None},
            ))
        return Snapshot(server_identity=identity, assets=assets)
