"""SQL Server 连接器（真实数据库，TDS / pymssql）。

- 资产名三段：库（catalog 段）. 模式. 表。只采配置的库与模式（options.database / options.schemas）。
- 采集账号只需模式上的 VIEW DEFINITION：读 sys.objects / sys.columns / sys.indexes / sys.sql_modules 与
  MS_Description 扩展属性（列注释），读不到业务数据。
- 物理身份用 object_id：sp_rename 不变，删除后重建会变；CREATE OR ALTER VIEW 不变。
- 视图定义：sys.sql_modules 存的是整条 CREATE VIEW 语句，用 SQLGlot（tsql）取出其中的查询；取不出时原样保留，解析会如实报错。
- 唯一键：主键、唯一约束、非筛选的唯一索引。
- 执行前核对用查询账号读视图定义：查询账号除 SELECT 外还需要模式上的 VIEW DEFINITION，否则读到的定义为空，
  会被当作定义已变化而拦截（宁可拦截，不放过）。
- 问数执行：只读账号（只有 SELECT）在事务内执行并回滚；查询超时由驱动设置（DB-Lib 20003 归为超时，连接随即关闭）。
  错误号 208 / 207 / 4104 归为对象无法解析，229 / 230 / 262 / 297 归为权限。
- 不报告数据新鲜度：最近写入时间需要 VIEW SERVER STATE（sys.dm_db_index_usage_stats），默认不授予。
- DDL 审计只读默认跟踪中的对象 Commit 事件；没有语句文本，列级变化只能作为不确定候选。
  StartTime 按服务器本地时间、读取时 SYSDATETIMEOFFSET() 的偏移转 UTC；假设窗口内偏移未变化（不跨 DST 切换）。
  默认跟踪不含模式；object_id、名称、类型和创建时间吻合当前目录也不能证明历史模式（对象可能转移过模式），
  仅恢复候选模式并强制归因为 ambiguous；删除对象不猜模式，无法恢复候选模式时为 unmatched。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import pymssql
import sqlglot
from sqlglot import exp

from avelis.connectors.base import ObservedAsset, ObservedColumn, QueryError, QueryResult, Snapshot
from avelis.connectors.ddl import AuditUnavailable, DdlEvent
from avelis.i18n import gettext

IDENT = re.compile(r"^[A-Za-z0-9_]+$")
KINDS = {"U": "table", "V": "view"}
SIZED = {"char", "varchar", "binary", "varbinary"}
NSIZED = {"nchar", "nvarchar"}
SCALED = {"decimal", "numeric"}
PRECISION = {"datetime2", "datetimeoffset", "time"}


def _checked(names: list[str], label: str) -> list[str]:
    for name in names:
        if not IDENT.match(name):
            raise ValueError(f"{label} 名称不合法：{name!r}")
    return names


def _in_list(names: list[str]) -> str:
    return ",".join("N'" + n + "'" for n in names)


def type_name(name: str, max_length: int, precision: int, scale: int) -> str:
    """sys.columns 的类型信息 → 常见写法（nvarchar(32)、decimal(18,2)、varchar(max)）。"""
    if name in SIZED | NSIZED:
        if max_length == -1:
            return f"{name}(max)"
        return f"{name}({max_length // 2 if name in NSIZED else max_length})"
    if name in SCALED:
        return f"{name}({precision},{scale})"
    if name in PRECISION:
        return f"{name}({scale})"
    return name


def view_query(definition: str | None) -> str | None:
    """CREATE VIEW ... AS <查询> → <查询>（tsql 方言重新生成）。解析不了时返回原文。"""
    if not definition:
        return None
    try:
        statement = sqlglot.parse_one(definition, read="tsql")
    except sqlglot.errors.ParseError:
        return definition.strip()
    if isinstance(statement, exp.Create) and isinstance(statement.expression, exp.Query):
        return statement.expression.sql(dialect="tsql")
    return definition.strip()


class SqlServerConnector:
    kind = "sqlserver"
    dialect = "tsql"
    is_test_adapter = False
    ddl_audit_source = "sqlserver:default_trace"
    ddl_identifier_fold = "exact"

    def __init__(self, host: str, port: int, user: str, password: str, options: dict[str, Any] | None = None):
        self.options = options or {}
        database = self.options.get("database")
        if not database or not IDENT.match(database):
            raise ValueError("SQL Server 数据源需要配置要采集的库（options.database）")
        self.database = database
        self._conn_args = dict(server=host, port=str(port), user=user, password=password, database=database)

    def _connect(self, timeout: int = 60) -> pymssql.Connection:
        return pymssql.connect(**self._conn_args, login_timeout=5, timeout=timeout, charset="UTF-8", autocommit=True)

    # --- 采集 ---------------------------------------------------------------------

    def server_identity(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            return self._server_identity(conn.cursor(as_dict=True))
        finally:
            conn.close()

    def _server_identity(self, cur) -> dict[str, Any]:
        cur.execute(
            "SELECT CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(64)) AS version, "
            "CAST(SERVERPROPERTY('Edition') AS NVARCHAR(128)) AS edition, "
            "CAST(d.service_broker_guid AS NVARCHAR(64)) AS db_guid "
            "FROM sys.databases d WHERE d.name = DB_NAME()"
        )
        row = cur.fetchone() or {}
        identity = {
            "version": row.get("version"),
            "edition": row.get("edition"),
            "endpoint": f"{self._conn_args['server']}:{self._conn_args['port']}",
            "database": self.database,
            "database_guid": row.get("db_guid"),
        }
        if identity["database_guid"]:
            identity["instance_id"] = f"mssql:{identity['database_guid']}"
        return identity

    def snapshot(self) -> Snapshot:
        schemas = _checked(list(self.options.get("schemas", [])), "模式")
        if not schemas:
            raise ValueError("数据源未配置要采集的模式（options.schemas）")
        warnings: list[str] = []
        conn = self._connect(timeout=120)
        try:
            cur = conn.cursor(as_dict=True)
            identity = self._server_identity(cur)
            cur.execute(
                "SELECT o.object_id, s.name AS schema_name, o.name, o.type, "
                "CAST(ep.value AS NVARCHAR(4000)) AS comment "
                "FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id "
                "LEFT JOIN sys.extended_properties ep ON ep.class = 1 AND ep.major_id = o.object_id "
                "AND ep.minor_id = 0 AND ep.name = N'MS_Description' "
                f"WHERE s.name IN ({_in_list(schemas)}) AND o.type IN ('U', 'V') ORDER BY s.name, o.name"
            )
            objects = cur.fetchall()
            ids = [r["object_id"] for r in objects]
            columns = self._columns(cur, ids, with_comments=True)
            keys = self._keys(cur, ids)
            definitions = self._definitions(cur, [r["object_id"] for r in objects if r["type"].strip() == "V"])
        finally:
            conn.close()
        assets = []
        for r in objects:
            oid, kind = r["object_id"], KINDS[r["type"].strip()]
            unique = keys.get(oid, {"pk": [], "unique": []})
            pk = unique["pk"][0] if unique["pk"] else []
            definition = definitions.get(oid)
            if kind == "view" and definition is None:
                warnings.append(f"视图 {r['schema_name']}.{r['name']} 未读到定义（需要 VIEW DEFINITION）")
            pk_set = set(pk)
            assets.append(
                ObservedAsset(
                    catalog=self.database,
                    db=r["schema_name"],
                    name=r["name"],
                    kind=kind,
                    columns=[
                        ObservedColumn(
                            name=c[0], data_type=c[1], ordinal=c[2], nullable=c[3], comment=c[4], is_key=c[0] in pk_set
                        )
                        for c in columns.get(oid, [])
                    ],
                    physical_id=str(oid),
                    key_columns=pk,
                    unique_keys=sorted(unique["pk"] + unique["unique"]),
                    comment=r["comment"] or None,
                    definition=definition,
                )
            )
        return Snapshot(server_identity=identity, assets=assets, warnings=warnings)

    @staticmethod
    def _columns(cur, ids: list[int], with_comments: bool = False) -> dict[int, list[tuple]]:
        out: dict[int, list[tuple]] = {}
        if not ids:
            return out
        comment = "CAST(ep.value AS NVARCHAR(4000))" if with_comments else "NULL"
        cur.execute(
            f"SELECT c.object_id, c.name, t.name AS type_name, c.max_length, c.precision, c.scale, c.column_id, "
            f"c.is_nullable, {comment} AS comment FROM sys.columns c "
            "JOIN sys.types t ON t.user_type_id = c.user_type_id "
            "LEFT JOIN sys.extended_properties ep ON ep.class = 1 AND ep.major_id = c.object_id "
            "AND ep.minor_id = c.column_id AND ep.name = N'MS_Description' "
            f"WHERE c.object_id IN ({','.join(str(int(i)) for i in ids)}) ORDER BY c.object_id, c.column_id"
        )
        for r in cur.fetchall():
            out.setdefault(r["object_id"], []).append(
                (
                    r["name"],
                    type_name(r["type_name"], r["max_length"], r["precision"], r["scale"]),
                    int(r["column_id"]),
                    bool(r["is_nullable"]),
                    r["comment"] or None,
                )
            )
        return out

    @staticmethod
    def _keys(cur, ids: list[int]) -> dict[int, dict[str, list[list[str]]]]:
        out: dict[int, dict[str, list[list[str]]]] = {}
        if not ids:
            return out
        cur.execute(
            "SELECT i.object_id, i.index_id, i.is_primary_key, c.name, ic.key_ordinal FROM sys.indexes i "
            "JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.key_ordinal > 0 "
            "JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
            f"WHERE i.object_id IN ({','.join(str(int(i)) for i in ids)}) AND i.is_unique = 1 AND i.has_filter = 0 "
            "AND i.is_disabled = 0 ORDER BY i.object_id, i.index_id, ic.key_ordinal"
        )
        grouped: dict[tuple[int, int], tuple[bool, list[str]]] = {}
        for r in cur.fetchall():
            grouped.setdefault((r["object_id"], r["index_id"]), (bool(r["is_primary_key"]), []))[1].append(r["name"])
        for (oid, _), (is_pk, names) in grouped.items():
            out.setdefault(oid, {"pk": [], "unique": []})["pk" if is_pk else "unique"].append(names)
        return out

    @staticmethod
    def _definitions(cur, ids: list[int]) -> dict[int, str]:
        if not ids:
            return {}
        cur.execute(
            "SELECT object_id, definition FROM sys.sql_modules "
            f"WHERE object_id IN ({','.join(str(int(i)) for i in ids)})"
        )
        return {r["object_id"]: view_query(r["definition"]) for r in cur.fetchall() if r["definition"]}

    def ddl_events(self, since: datetime, until: datetime, cursors=None) -> list[DdlEvent]:
        """默认跟踪的 46 / 47 / 164 只取 EventSubClass=1（Commit），不把 Begin / Rollback 当证据。

        对象事件没有 SQL 文本，statement 留空：沿用 column_refs 的 unknown，列归因为 ambiguous。
        当前目录只能恢复候选模式，不能确认对象在事件发生时属于该模式（ALTER SCHEMA TRANSFER 不换对象 ID）。
        因此有候选对象时也不能确认归因；尤其 DROP、改名或 ID 已复用时不映射对象，归因会 unmatched。
        """
        schemas = set(_checked(list(self.options.get("schemas", [])), "模式"))
        conn = None
        try:
            conn = self._connect(timeout=60)
            cur = conn.cursor(as_dict=True)
            cur.execute(
                "SELECT CAST(value_in_use AS INT) AS enabled, "
                "DATEPART(TZOFFSET, SYSDATETIMEOFFSET()) AS offset_minutes, "
                "HAS_PERMS_BY_NAME(NULL, NULL, 'ALTER TRACE') AS can_read "
                "FROM sys.configurations WHERE name = 'default trace enabled'"
            )
            config = cur.fetchone() or {}
            if config.get("enabled") == 0:
                raise AuditUnavailable(gettext("SQL Server 默认跟踪未开启（default trace enabled = 0）"), "not_enabled")
            offset_minutes = config.get("offset_minutes")
            if config.get("enabled") != 1 or not isinstance(offset_minutes, int) or not -840 <= offset_minutes <= 840:
                raise AuditUnavailable(gettext("SQL Server 默认跟踪配置或服务器时区偏移无法读取"), "incompatible")
            if config.get("can_read") is None:
                raise AuditUnavailable(
                    gettext(
                        "无法判断采集账号是否具有 SQL Server 默认跟踪读取权限"
                        "（ALTER TRACE 权限检查返回 NULL）；请确认相关授权"
                    ),
                    "no_permission",
                )
            if config.get("can_read") != 1:
                raise AuditUnavailable(
                    gettext(
                        "采集账号缺少 SQL Server 默认跟踪读取权限（需要 ALTER TRACE；"
                        "相关读取可能还需要 VIEW SERVER STATE）"
                    ),
                    "no_permission",
                )
            offset = timedelta(minutes=offset_minutes)
            cur.execute("SELECT TOP (1) path, status FROM sys.traces WHERE is_default = 1 ORDER BY id")
            trace = cur.fetchone() or {}
            if trace.get("status") != 1 or not trace.get("path"):
                raise AuditUnavailable(gettext("SQL Server 默认跟踪未运行或没有可读取的跟踪文件"), "not_enabled")
            cur.execute(
                "SELECT t.StartTime AS at_local, t.LoginName AS db_user, t.DatabaseName AS database_name, "
                "t.ObjectName AS object_name, t.EventClass AS event_class, t.EventSubClass AS event_subclass, "
                "t.ObjectType AS object_type, s.name AS schema_name "
                "FROM sys.fn_trace_gettable(%s, DEFAULT) AS t "
                "LEFT JOIN sys.objects AS o ON t.EventClass <> 47 AND o.object_id = t.ObjectID "
                "AND o.name = t.ObjectName AND o.create_date <= t.StartTime "
                "AND ((t.ObjectType = 8277 AND o.type = 'U') OR (t.ObjectType = 8278 AND o.type = 'V')) "
                "LEFT JOIN sys.schemas AS s ON s.schema_id = o.schema_id "
                "WHERE t.EventClass IN (46, 47, 164) AND t.EventSubClass = 1 "
                "AND t.ObjectType IN (8277, 8278) AND t.DatabaseName = %s "
                "AND t.StartTime >= %s AND t.StartTime <= %s ORDER BY t.StartTime",
                (trace["path"], self.database, since + offset, until + offset),
            )
            rows = cur.fetchall()
        except pymssql.Error as exc:
            error = _query_error(exc)
            raise AuditUnavailable(
                gettext("SQL Server 默认跟踪读取失败：{error}", error=error.message), _audit_cause(error)
            ) from exc
        finally:
            if conn is not None:
                conn.close()
        events = []
        try:
            for row in rows:
                # SQL 已过滤；再次防守，避免任何非 Commit、其他库或非表/视图事件进入归因。
                if (
                    row["event_subclass"] != 1
                    or row["event_class"] not in (46, 47, 164)
                    or row["object_type"] not in (8277, 8278)
                    or row["database_name"] != self.database
                ):
                    continue
                at, user, name = row["at_local"], row["db_user"], row["object_name"]
                if not isinstance(at, datetime) or at.tzinfo is not None or not user or not name:
                    raise ValueError("StartTime / LoginName / ObjectName")
                at -= offset
                if not since <= at <= until:
                    continue
                schema = row["schema_name"] if row["event_class"] != 47 else None
                objects = [(schema, name)] if schema and schema in schemas else []
                # Commit 只证明 DDL 已生效，不能证明历史模式成员关系。当前目录恢复的模式仅作候选：
                # 沿用 certain=False 的保守路径阻止对象级 matched，at 仍保留来源事件的真实 UTC 时间。
                # 此处的不确定性来自历史对象身份，不能把它解释为事件一定发生在时间窗口边界。
                events.append(
                    DdlEvent(
                        at=at,
                        db_user=user,
                        objects=objects,
                        statement_type={46: "CREATE", 47: "DROP", 164: "ALTER"}[row["event_class"]],
                        statement="",
                        certain=False,
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise AuditUnavailable(
                gettext("SQL Server 默认跟踪记录结构不符：{error}", error=str(exc)[:2000]), "incompatible"
            ) from exc
        return events

    # --- 只读执行 ------------------------------------------------------------------

    def execute_select(
        self, sql_text: str, *, timeout_seconds: int, mem_limit_bytes: int, max_rows: int, timezone: str | None = None
    ) -> QueryResult:
        """只读账号在事务内执行后回滚；SQL Server 没有单查询内存上限，靠超时与行数上限约束。"""
        # SQL Server 无会话时区；datetimeoffset 的边界偏移由编译器写入 SQL。
        try:
            conn = self._connect(timeout=int(timeout_seconds))
        except pymssql.Error as exc:
            raise _query_error(exc) from exc
        try:
            conn.autocommit(False)
            cur = conn.cursor()
            cur.execute(sql_text)
            rows = cur.fetchmany(max_rows + 1)
            columns = [d[0] for d in cur.description or []]
            conn.rollback()
        except pymssql.Error as exc:
            raise _query_error(exc) from exc
        finally:
            try:
                conn.close()
            except pymssql.Error:
                pass  # 超时后连接已被驱动断开
        return QueryResult(columns=columns, rows=[list(r) for r in rows[:max_rows]], truncated=len(rows) > max_rows)

    def freshness(self, tables: list[tuple[str, str]]) -> dict[str, Any]:
        """需要 VIEW SERVER STATE 才能读最近写入时间；不授予，不报告新鲜度（不以统计信息冒充）。"""
        return {}

    def describe_objects(self, objects: list[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
        """执行前核对：给定 模式.表 当前的列（名称、类型，按位置）、视图定义与 object_id；对象不存在时列为空。"""
        names = [(s, n) for s, n in objects if IDENT.match(s) and IDENT.match(n)]
        out: dict[tuple[str, str], dict[str, Any]] = {
            k: {"columns": [], "definition": None, "physical_id": None} for k in names
        }
        if not names:
            return out
        try:
            conn = self._connect(timeout=30)
            try:
                cur = conn.cursor(as_dict=True)
                cur.execute(
                    "SELECT o.object_id, s.name AS schema_name, o.name, o.type FROM sys.objects o "
                    "JOIN sys.schemas s ON s.schema_id = o.schema_id WHERE o.type IN ('U', 'V') AND ("
                    + " OR ".join(f"(s.name = N'{s}' AND o.name = N'{n}')" for s, n in names)
                    + ")"
                )
                rows = cur.fetchall()
                by_id = {r["object_id"]: (r["schema_name"], r["name"]) for r in rows}
                for oid, key in by_id.items():
                    out[key]["physical_id"] = str(oid)
                for oid, cols in self._columns(cur, list(by_id)).items():
                    out[by_id[oid]]["columns"] = [[c[0], c[1]] for c in cols]
                for oid, definition in self._definitions(
                    cur, [r["object_id"] for r in rows if r["type"].strip() == "V"]
                ).items():
                    out[by_id[oid]]["definition"] = definition
            finally:
                conn.close()
        except pymssql.Error as exc:
            raise _query_error(exc) from exc
        return out


DBLIB = re.compile(r"DB-Lib error message (\d+), severity \d+:\s*")


def _query_error(exc: pymssql.Error) -> QueryError:
    number = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    raw = exc.args[1] if len(exc.args) > 1 else str(exc)
    message = (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)).strip()
    # 服务器报错在前、驱动附加的 DB-Lib 说明在后：保留服务器原文；只有驱动层错误（如 20003 超时）才取 DB-Lib 的编号
    parts = DBLIB.split(message)
    if len(parts) >= 3:
        if parts[0].strip():
            message = parts[0].strip()
        else:
            number, message = int(parts[1]), parts[2].strip()
    if number == 20003 or "timed out" in message.lower():
        kind = "timeout"
    elif number in (229, 230, 262, 297, 916, 18456):
        kind = "permission"
    elif number in (207, 208, 4104):
        kind = "invalid_object"
    else:
        kind = "other"
    return QueryError(kind, number, message[:2000])


def _audit_cause(error: QueryError) -> str:
    if error.code == "permission" or error.db_errno in (300, 15151, 15247):
        return "no_permission"
    if error.db_errno in (207, 208, 195, 4104, 4121):  # 默认来源缺列、缺视图或函数不受该版本支持
        return "incompatible"
    return "error"
