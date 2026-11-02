"""MySQL 连接器：元数据、视图血缘、只读问数、执行前核对和可选 DDL 审计。

视图定义保留 information_schema.views 的原文，包括 MySQL 补写的库名和反引号，交现有 SQLGlot 解析。
实例身份用 @@server_uuid 确认；MySQL 不提供可用于普通表重建检测的稳定物理 ID。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pymysql

from avelis.connectors.base import ObservedAsset, ObservedColumn, QueryError, QueryResult, Snapshot
from avelis.connectors.ddl import DdlEvent
from avelis.connectors.mysql_audit import audit_source, ddl_events
from avelis.connectors.starrocks import _checked, _in_list, _set_timezone
from avelis.i18n import gettext


class MySQLMetaConnector:
    kind = "mysql"
    dialect = "mysql"
    is_test_adapter = False
    # MySQL 列名不区分大小写；归因模块仍按目录原样精确匹配库名和对象名。
    ddl_identifier_fold = "insensitive"

    def __init__(self, host: str, port: int, user: str, password: str, options: dict[str, Any] | None = None):
        self._conn_args = dict(host=host, port=port, user=user, password=password)
        self.options = options or {}

    def _connect(self, read_timeout: int = 60) -> pymysql.Connection:
        return pymysql.connect(
            **self._conn_args,
            autocommit=True,
            charset="utf8mb4",
            connect_timeout=5,
            read_timeout=read_timeout,
            write_timeout=30,
            cursorclass=pymysql.cursors.DictCursor,
        )

    @property
    def ddl_audit_source(self) -> str | None:
        return audit_source(self.options)

    @property
    def ddl_audit_unbound_reason(self) -> str | None:
        value = self.options.get("ddl_audit_table")
        if value is None or isinstance(value, str) and not value.strip():
            return gettext("未绑定审计表：请在数据源选项 ddl_audit_table 中绑定 MySQL DDL 审计表")
        return None

    def ddl_events(self, since: datetime, until: datetime, cursors=None) -> list[DdlEvent]:
        return ddl_events(self, since, until, cursors)

    def execute_select(
        self, sql: str, *, timeout_seconds: int, mem_limit_bytes: int, max_rows: int, timezone: str | None = None
    ) -> QueryResult:
        """执行调用层已校验的一条 SELECT：query 只读账号、只读事务、服务端超时与行数上限。

        调用层 validate_sql 拒绝写语法并限制显式 LIMIT；SQL_SELECT_LIMIT 也约束未写 LIMIT 的 SELECT。
        MySQL 没有通用的每查询内存限制，不把 mem_limit_bytes 映射成并不等价的会话内存参数。
        """
        if timeout_seconds <= 0:
            raise QueryError("timeout", None, gettext("超时值必须为正数，未执行"))
        if max_rows < 0:
            raise QueryError("other", None, gettext("查询行数上限不能为负数，未执行"))
        conn = None
        transaction_started = False
        try:
            conn = self._connect(read_timeout=timeout_seconds + 15)
            cur = conn.cursor(pymysql.cursors.Cursor)
            warnings = _set_timezone(cur, timezone)
            cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {int(timeout_seconds) * 1000}")
            cur.execute(f"SET SESSION SQL_SELECT_LIMIT = {int(max_rows) + 1}")
            cur.execute("START TRANSACTION READ ONLY")
            transaction_started = True
            cur.execute(sql)
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(max_rows + 1)
            return QueryResult(
                columns=columns,
                rows=[list(r) for r in rows[:max_rows]],
                truncated=len(rows) > max_rows,
                warnings=warnings,
            )
        except pymysql.MySQLError as exc:
            raise _query_error(exc) from exc
        finally:
            if conn is not None:
                try:
                    if transaction_started:
                        conn.rollback()
                except pymysql.MySQLError:
                    pass  # 超时或断连后回滚可能失败；关闭连接，并保留原始查询错误。
                finally:
                    conn.close()

    def describe_objects(
        self, objects: list[tuple[str, str]], *, timeout_seconds: int | None = None
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """执行前核对当前列和视图定义；不存在的对象保留空列，不虚构物理身份。

        使用参数绑定查询精确的库表组合，带反引号、空格等名称不会进入 SQL 文本。
        """
        names = sorted({(db, name) for db, name in objects if db and name})
        out: dict[tuple[str, str], dict[str, Any]] = {
            key: {"columns": [], "definition": None, "physical_id": None} for key in names
        }
        if not names:
            return out
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise QueryError("timeout", None, gettext("超时值必须为正数，未执行"))
        where = "(TABLE_SCHEMA, TABLE_NAME) IN (" + ",".join("(%s,%s)" for _ in names) + ")"
        params = tuple(part for key in names for part in key)
        conn = None
        try:
            conn = self._connect(read_timeout=timeout_seconds or 30)
            cur = conn.cursor()
            cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {int(timeout_seconds or 30) * 1000}")
            cur.execute(
                "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, COLUMN_TYPE "
                f"FROM information_schema.columns WHERE {where}",
                params,
            )
            for row in sorted(cur.fetchall(), key=lambda row: int(row["ORDINAL_POSITION"])):
                key = (row["TABLE_SCHEMA"], row["TABLE_NAME"])
                if key in out:
                    out[key]["columns"].append([row["COLUMN_NAME"], row["COLUMN_TYPE"]])
            cur.execute(
                f"SELECT TABLE_SCHEMA, TABLE_NAME, VIEW_DEFINITION FROM information_schema.views WHERE {where}", params
            )
            for row in cur.fetchall():
                key = (row["TABLE_SCHEMA"], row["TABLE_NAME"])
                if key in out:
                    out[key]["definition"] = row["VIEW_DEFINITION"] or None
        except pymysql.MySQLError as exc:
            raise _query_error(exc) from exc
        finally:
            if conn is not None:
                conn.close()
        return out

    def freshness(self, tables: list[tuple[str, str]]) -> dict[str, Any]:
        """UPDATE_TIME 受存储引擎、分区和缓存影响，不能可靠表示数据新鲜度。"""
        return {}

    def server_identity(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            return self._server_identity(conn.cursor())
        finally:
            conn.close()

    @staticmethod
    def _server_identity(cur) -> dict[str, Any]:
        cur.execute("SELECT @@server_uuid AS server_uuid, @@hostname AS hostname, @@port AS port, VERSION() AS version")
        row = cur.fetchone()
        return {
            "instance_id": row["server_uuid"],
            "server_uuid": row["server_uuid"],
            "hostname": row["hostname"],
            "port": int(row["port"]),
            "version": row["version"],
        }

    def snapshot(self) -> Snapshot:
        schemas = _checked(list(self.options.get("databases", [])), "库")
        if not schemas:
            raise ValueError("数据源未配置要采集的库（options.databases）")
        in_dbs = _in_list(schemas)
        conn = self._connect()
        try:
            cur = conn.cursor()
            identity = self._server_identity(cur)
            cur.execute(
                "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, TABLE_COMMENT, CREATE_TIME "
                f"FROM information_schema.tables WHERE TABLE_SCHEMA IN ({in_dbs})"
            )
            tables = cur.fetchall()
            cur.execute(
                "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, COLUMN_TYPE, IS_NULLABLE, "
                "COLUMN_COMMENT, COLUMN_KEY FROM information_schema.columns "
                f"WHERE TABLE_SCHEMA IN ({in_dbs})"
            )
            columns: dict[tuple[str, str], list[ObservedColumn]] = {}
            for r in cur.fetchall():
                columns.setdefault((r["TABLE_SCHEMA"], r["TABLE_NAME"]), []).append(
                    ObservedColumn(
                        name=r["COLUMN_NAME"],
                        data_type=r["COLUMN_TYPE"],
                        ordinal=int(r["ORDINAL_POSITION"]),
                        nullable=r["IS_NULLABLE"] == "YES",
                        comment=r["COLUMN_COMMENT"] or None,
                        is_key=r["COLUMN_KEY"] in ("PRI", "UNI"),
                    )
                )
            cur.execute(
                "SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME "
                "FROM information_schema.statistics "
                f"WHERE TABLE_SCHEMA IN ({in_dbs}) AND NON_UNIQUE = 0 ORDER BY INDEX_NAME, SEQ_IN_INDEX"
            )
            unique: dict[tuple[str, str], dict[str, list[str]]] = {}
            for r in cur.fetchall():
                unique.setdefault((r["TABLE_SCHEMA"], r["TABLE_NAME"]), {}).setdefault(r["INDEX_NAME"], []).append(
                    r["COLUMN_NAME"]
                )
            cur.execute(
                "SELECT TABLE_SCHEMA, TABLE_NAME, VIEW_DEFINITION FROM information_schema.views "
                f"WHERE TABLE_SCHEMA IN ({in_dbs})"
            )
            views = {(r["TABLE_SCHEMA"], r["TABLE_NAME"]): r["VIEW_DEFINITION"] for r in cur.fetchall()}
        finally:
            conn.close()

        assets = []
        warnings: list[str] = []
        for t in tables:
            key = (t["TABLE_SCHEMA"], t["TABLE_NAME"])
            indexes = unique.get(key, {})
            pk = indexes.get("PRIMARY", [])
            is_view = t["TABLE_TYPE"] == "VIEW"
            definition = (views.get(key) or None) if is_view else None
            if is_view and definition is None:
                warnings.append(
                    gettext("MySQL 视图 {name} 未读取到定义（需要 SHOW VIEW 权限）；列级血缘不可用", name=".".join(key))
                )
            assets.append(
                ObservedAsset(
                    catalog="def",
                    db=key[0],
                    name=key[1],
                    kind="view" if is_view else "table",
                    columns=sorted(columns.get(key, []), key=lambda c: c.ordinal),
                    key_columns=pk,
                    unique_keys=[cols for _, cols in sorted(indexes.items())],
                    comment=t["TABLE_COMMENT"] or None,
                    definition=definition,
                    properties={"create_time": str(t["CREATE_TIME"]) if t["CREATE_TIME"] else None},
                )
            )
        return Snapshot(server_identity=identity, assets=assets, warnings=warnings)


def _query_error(exc: pymysql.MySQLError) -> QueryError:
    errno = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    message = str(exc.args[-1]) if exc.args else str(exc)
    text = message.lower()
    if errno in (1205, 1317, 3024) or "timeout" in text or "timed out" in text:
        kind = "timeout"
    elif errno in (1044, 1045, 1142, 1143, 1290, 1792) or "access denied" in text or "command denied" in text:
        kind = "permission"
    elif errno in (1049, 1051, 1054, 1146):
        kind = "invalid_object"
    else:
        kind = "other"
    return QueryError(kind, errno, message.strip()[:2000])
