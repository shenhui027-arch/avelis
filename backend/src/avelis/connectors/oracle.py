"""Oracle 连接器（真实数据库，python-oracledb thin 模式，不需要 Oracle 客户端）。

- 资产名三段：服务名（catalog 段，如 FREEPDB1）. 模式（用户）. 表。Oracle 的查询不认服务名这一段，生成的 SQL 只写 模式.表。
- 未加引号的标识符按大写保存（SCHEMA_A.TABLE_A.COL_A），资产名与列名保持数据库目录中的写法。
- 采集账号只需 CREATE SESSION + SELECT_CATALOG_ROLE：读 DBA_OBJECTS / DBA_TAB_COLUMNS / DBA_VIEWS / DBA_CONSTRAINTS
  与 DBA_TAB_COMMENTS / DBA_COL_COMMENTS，读不到业务数据（对无权限的表报 ORA-00942“不存在”）。
- 物理身份用 OBJECT_ID：RENAME 与 CREATE OR REPLACE VIEW 都不变，删除后重建会变。
- 视图定义取 DBA_VIEWS.TEXT（LONG 列，按建视图时的原文保存）。
- 唯一键：启用的主键与唯一约束。
- DATE 类型带时间部分：方言声明 date_has_time，按日期过滤时用 [当日 0 点, 次日 0 点) 区间；
  会话固定 NLS_DATE_FORMAT / NLS_TIMESTAMP_FORMAT，SQL 中的日期字符串不依赖库的默认设置。
- 问数执行：只读事务（SET TRANSACTION READ ONLY）+ 调用超时（DPY-4024 归为超时）；
  ORA-00904 / ORA-00942 归为对象无法解析，ORA-01031 / ORA-01456 / ORA-01017 / ORA-41900（23ai 起的“缺少某项权限”）归为权限。
- 查询账号的执行前核对读 ALL_* 视图（只看得到有权限的对象），不需要额外授权。
- 不报告数据新鲜度：DBA_TAB_MODIFICATIONS 依赖统计信息刷新，不以它冒充写入时间。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import oracledb

from avelis.connectors.base import ObservedAsset, ObservedColumn, QueryError, QueryResult, Snapshot, checked_timezone
from avelis.connectors.ddl import AuditUnavailable, DdlEvent
from avelis.i18n import Msg

IDENT = re.compile(r"^[A-Za-z0-9_$#]+$")
SESSION = ("ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS' "
           "NLS_TIMESTAMP_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF' NLS_NUMERIC_CHARACTERS = '.,'")
ORA = re.compile(r"\b(ORA|DPY)-(\d+)")
DDL_ACTIONS = ("CREATE TABLE", "ALTER TABLE", "DROP TABLE", "CREATE VIEW", "ALTER VIEW", "DROP VIEW",
               "RENAME", "COMMENT", "TRUNCATE TABLE")
DDL_PRIVILEGES = {"CREATE ANY TABLE", "ALTER ANY TABLE", "DROP ANY TABLE", "CREATE ANY VIEW", "ALTER ANY VIEW",
                  "DROP ANY VIEW", "COMMENT ANY TABLE"}


def _checked(names: list[str], label: str) -> list[str]:
    for name in names:
        if not IDENT.match(name):
            raise ValueError(f"{label} 名称不合法：{name!r}")
    return names


def _binds(names: list[str], prefix: str) -> tuple[str, dict[str, str]]:
    keys = {f"{prefix}{i}": n for i, n in enumerate(names)}
    return ",".join(f":{k}" for k in keys), keys


def type_name(data_type: str, length: int | None, precision: int | None, scale: int | None,
              char_length: int | None) -> str:
    """ALL_TAB_COLUMNS 的类型信息 → 常见写法（VARCHAR2(32)、NUMBER(18,2)、DATE）。"""
    if data_type in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR"):
        return f"{data_type}({char_length or length})"
    if data_type == "NUMBER" and precision is not None:
        return f"NUMBER({precision},{scale or 0})"
    return data_type


class OracleConnector:
    kind = "oracle"
    dialect = "oracle"
    is_test_adapter = False
    ddl_audit_source = "oracle:UNIFIED_AUDIT_TRAIL"
    ddl_identifier_fold = "upper"

    def __init__(self, host: str, port: int, user: str, password: str, options: dict[str, Any] | None = None):
        self.options = options or {}
        service = self.options.get("service")
        if not service or not IDENT.match(service):
            raise ValueError("Oracle 数据源需要配置服务名（options.service）")
        self.service = service
        self._conn_args = dict(user=user, password=password, host=host, port=port, service_name=service)

    def _connect(self, call_timeout_ms: int = 60_000) -> oracledb.Connection:
        conn = oracledb.connect(**self._conn_args, tcp_connect_timeout=5)
        conn.call_timeout = call_timeout_ms
        conn.outputtypehandler = _decimal_numbers
        with conn.cursor() as cur:
            cur.execute(SESSION)
        return conn

    # --- 采集 ---------------------------------------------------------------------

    def server_identity(self) -> dict[str, Any]:
        with self._connect() as conn:
            return self._server_identity(conn)

    def _server_identity(self, conn: oracledb.Connection) -> dict[str, Any]:
        identity: dict[str, Any] = {"version": conn.version, "service": self.service,
                                    "endpoint": f"{self._conn_args['host']}:{self._conn_args['port']}"}
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT dbid, SYS_CONTEXT('USERENV', 'CON_NAME') FROM v$database")
                dbid, container = cur.fetchone()
                identity.update(dbid=str(dbid), container=container, instance_id=f"ora:{dbid}/{container}")
            except oracledb.DatabaseError:
                identity["dbid"] = None  # 需要 SELECT_CATALOG_ROLE；查询账号默认没有
        return identity

    def snapshot(self) -> Snapshot:
        schemas = _checked([s.upper() for s in self.options.get("schemas", [])], "模式")
        if not schemas:
            raise ValueError("数据源未配置要采集的模式（options.schemas）")
        warnings: list[str] = []
        with self._connect(call_timeout_ms=120_000) as conn:
            identity = self._server_identity(conn)
            marks, binds = _binds(schemas, "s")
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT o.object_id, o.owner, o.object_name, o.object_type, c.comments "
                    "FROM dba_objects o LEFT JOIN dba_tab_comments c ON c.owner = o.owner "
                    "AND c.table_name = o.object_name "
                    f"WHERE o.owner IN ({marks}) AND o.object_type IN ('TABLE', 'VIEW') "
                    "AND o.object_name NOT LIKE 'BIN$%' ORDER BY o.owner, o.object_name", binds)
                objects = cur.fetchall()
            columns = self._columns(conn, "dba", schemas, with_comments=True)
            keys = self._keys(conn, schemas)
            definitions = self._definitions(conn, "dba", schemas)
        assets = []
        for oid, owner, name, object_type, comment in objects:
            kind = "table" if object_type == "TABLE" else "view"
            unique = keys.get((owner, name), {"P": [], "U": []})
            pk = unique["P"][0] if unique["P"] else []
            definition = definitions.get((owner, name)) if kind == "view" else None
            if kind == "view" and definition is None:
                warnings.append(f"视图 {owner}.{name} 未读到定义")
            pk_set = set(pk)
            assets.append(ObservedAsset(
                catalog=self.service, db=owner, name=name, kind=kind,
                columns=[ObservedColumn(name=c[0], data_type=c[1], ordinal=c[2], nullable=c[3], comment=c[4],
                                        is_key=c[0] in pk_set) for c in columns.get((owner, name), [])],
                physical_id=str(oid), key_columns=pk, unique_keys=sorted(unique["P"] + unique["U"]),
                comment=comment or None, definition=definition,
            ))
        return Snapshot(server_identity=identity, assets=assets, warnings=warnings)

    @staticmethod
    def _columns(conn, scope: str, schemas: list[str], names: list[str] | None = None,
                 with_comments: bool = False) -> dict[tuple[str, str], list[tuple]]:
        marks, binds = _binds(schemas, "s")
        sql = ("SELECT c.owner, c.table_name, c.column_name, c.data_type, c.data_length, c.data_precision, "
               "c.data_scale, c.char_length, c.column_id, c.nullable, "
               + ("m.comments " if with_comments else "NULL ")
               + f"FROM {scope}_tab_columns c "
               + (f"LEFT JOIN {scope}_col_comments m ON m.owner = c.owner AND m.table_name = c.table_name "
                  "AND m.column_name = c.column_name " if with_comments else "")
               + f"WHERE c.owner IN ({marks})")
        if names is not None:
            tmarks, tbinds = _binds(names, "t")
            sql += f" AND c.table_name IN ({tmarks})"
            binds = {**binds, **tbinds}
        out: dict[tuple[str, str], list[tuple]] = {}
        with conn.cursor() as cur:
            cur.execute(sql + " ORDER BY c.owner, c.table_name, c.column_id", binds)
            for owner, table, name, dtype, length, precision, scale, char_length, column_id, nullable, comment \
                    in cur.fetchall():
                out.setdefault((owner, table), []).append((
                    name, type_name(dtype, length, precision, scale, char_length), int(column_id), nullable == "Y",
                    comment or None))
        return out

    @staticmethod
    def _keys(conn, schemas: list[str]) -> dict[tuple[str, str], dict[str, list[list[str]]]]:
        marks, binds = _binds(schemas, "s")
        grouped: dict[tuple[str, str, str], tuple[str, list[str]]] = {}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.owner, c.table_name, c.constraint_name, c.constraint_type, k.column_name "
                "FROM dba_constraints c JOIN dba_cons_columns k ON k.owner = c.owner "
                "AND k.constraint_name = c.constraint_name "
                f"WHERE c.owner IN ({marks}) AND c.constraint_type IN ('P', 'U') AND c.status = 'ENABLED' "
                "ORDER BY c.owner, c.table_name, c.constraint_name, k.position", binds)
            for owner, table, cname, ctype, column in cur.fetchall():
                grouped.setdefault((owner, table, cname), (ctype, []))[1].append(column)
        out: dict[tuple[str, str], dict[str, list[list[str]]]] = {}
        for (owner, table, _), (ctype, columns) in grouped.items():
            out.setdefault((owner, table), {"P": [], "U": []})[ctype].append(columns)
        return out

    @staticmethod
    def _definitions(conn, scope: str, schemas: list[str], names: list[str] | None = None) -> dict[tuple[str, str], str]:
        marks, binds = _binds(schemas, "s")
        sql = f"SELECT owner, view_name, text FROM {scope}_views WHERE owner IN ({marks})"
        if names is not None:
            tmarks, tbinds = _binds(names, "t")
            sql += f" AND view_name IN ({tmarks})"
            binds = {**binds, **tbinds}
        with conn.cursor() as cur:
            cur.execute(sql, binds)
            return {(owner, name): text.strip().rstrip(";") for owner, name, text in cur.fetchall() if text}

    # --- 只读执行 ------------------------------------------------------------------

    def execute_select(self, sql_text: str, *, timeout_seconds: int, mem_limit_bytes: int,
                       max_rows: int, timezone: str | None = None) -> QueryResult:
        """只读事务内执行；Oracle 没有单查询内存上限可由会话设置，靠调用超时与行数上限约束。"""
        try:
            with self._connect(call_timeout_ms=int(timeout_seconds) * 1000) as conn:
                with conn.cursor() as cur:
                    if timezone is not None:
                        cur.execute(f"ALTER SESSION SET TIME_ZONE = '{checked_timezone(timezone)}'")
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute(sql_text)
                    columns = [d[0] for d in cur.description or []]
                    rows = cur.fetchmany(max_rows + 1)
                conn.rollback()
        except oracledb.Error as exc:
            raise _query_error(exc) from exc
        return QueryResult(columns=columns, rows=[list(r) for r in rows[:max_rows]], truncated=len(rows) > max_rows)

    def freshness(self, tables: list[tuple[str, str]]) -> dict[str, Any]:
        """DBA_TAB_MODIFICATIONS 依赖统计信息刷新，不能代表最近写入时间；不报告新鲜度。"""
        return {}

    def ddl_events(self, since: datetime, until: datetime, cursors=None) -> list[DdlEvent]:
        """只读统一审计中成功的表/视图 DDL；SQL_TEXT（CLOB）只在连接打开时读入内存。

        优先 EVENT_TIMESTAMP_UTC。只有它缺失时才回退 EVENT_TIMESTAMP，并将审计连接的 TIME_ZONE
        固定为 +00:00：按该视图的“会话本地时间”语义将无时区值视作 UTC，不使用 Avelis 主机时区。
        空窗口本身不意味着审计停用；只有全历史也没有相关成功记录，且策略未覆盖相关成功操作时才报未开启。
        策略对部分用户/对象启用只证明存在来源，不保证所有变更路径都被覆盖，缺记录仍由通用归因报未知。
        """
        schemas = _checked([s.upper() for s in self.options.get("schemas", [])], "模式")
        if not schemas:
            raise ValueError("数据源未配置要采集的模式（options.schemas）")
        marks, binds = _binds(schemas, "s")
        amarks, abinds = _binds(list(DDL_ACTIONS), "a")
        binds.update(abinds)
        where = (f"AUDIT_TYPE = 'Standard' AND RETURN_CODE = 0 AND ACTION_NAME IN ({amarks}) "
                 f"AND OBJECT_SCHEMA IN ({marks}) AND OBJECT_NAME IS NOT NULL")
        try:
            with self._connect() as conn, conn.cursor() as cur:
                def read(timestamp: str):
                    cur.execute(
                        f"SELECT {timestamp}, DBUSERNAME, ACTION_NAME, OBJECT_SCHEMA, OBJECT_NAME, SQL_TEXT "
                        f"FROM UNIFIED_AUDIT_TRAIL WHERE {where} "
                        f"AND {timestamp} >= :since AND {timestamp} <= :until ORDER BY {timestamp}",
                        {**binds, "since": _utc(since), "until": _utc(until)})
                    return cur.fetchall()

                try:
                    rows = read("EVENT_TIMESTAMP_UTC")
                except oracledb.Error as exc:
                    # ORA-00904 也可能指向其他必需列：不能把结构不符一概当旧版时钟列处理。
                    if _query_error(exc).db_errno != "ORA-00904" or '"EVENT_TIMESTAMP_UTC"' not in str(exc).upper():
                        raise
                    cur.execute("ALTER SESSION SET TIME_ZONE = '+00:00'")
                    rows = read("EVENT_TIMESTAMP")
                events = []
                for row in rows:
                    if not isinstance(row, (tuple, list)) or len(row) != 6:
                        raise AuditUnavailable(Msg("Oracle 统一审计记录缺少有效的时间、数据库用户、操作或对象信息"),
                                               "incompatible")
                    at, user, action, schema, name, statement = row
                    if not isinstance(at, datetime) or not all(isinstance(v, str) and v
                                                              for v in (user, action, schema, name)):
                        raise AuditUnavailable(Msg("Oracle 统一审计记录缺少有效的时间、数据库用户、操作或对象信息"),
                                               "incompatible")
                    if statement is not None and not isinstance(statement, str):
                        read_clob = getattr(statement, "read", None)
                        if not callable(read_clob):
                            raise AuditUnavailable(Msg("Oracle 统一审计的 SQL_TEXT 不是文本"), "incompatible")
                        statement = read_clob()  # python-oracledb 默认把 CLOB 返回为 LOB，不能等连接关闭后读取
                    if statement is not None and not isinstance(statement, str):
                        raise AuditUnavailable(Msg("Oracle 统一审计的 SQL_TEXT 不是文本"), "incompatible")
                    events.append(DdlEvent(at=_utc(at), db_user=user, objects=[(schema, name)],
                                           statement_type=action, statement=statement or ""))
                if not events:
                    cur.execute(f"SELECT 1 FROM UNIFIED_AUDIT_TRAIL WHERE {where} AND ROWNUM = 1", binds)
                    if cur.fetchone() is None:
                        self._check_ddl_policies(cur, schemas)
                return events
        except oracledb.Error as exc:
            raise _audit_error(exc) from exc

    @staticmethod
    def _check_ddl_policies(cur, schemas: list[str]) -> None:
        """只在没有历史证据时检查策略：保留按用户、排除用户和授予角色等有效启用方式。

        不拿采集账号去匹配 ENTITY_NAME：做 DDL 的用户可以是其他账号，也可以跨模式操作。
        未知的审计选项类型不能证明“没有启用”，因此保守保留为可能存在的审计来源。
        """
        cur.execute(
            "SELECT p.AUDIT_OPTION, p.AUDIT_OPTION_TYPE, p.OBJECT_SCHEMA, p.OBJECT_TYPE, "
            "e.ENABLED_OPTION, e.ENTITY_NAME, e.SUCCESS "
            "FROM AUDIT_UNIFIED_ENABLED_POLICIES e JOIN AUDIT_UNIFIED_POLICIES p "
            "ON p.POLICY_NAME = e.POLICY_NAME")
        for option, kind, schema, object_type, enabled, entity, success in cur.fetchall():
            if success != "YES" or enabled == "INVALID" or not entity:
                continue
            if schema is not None and schema not in schemas:
                continue
            if kind == "OBJECT ACTION" and object_type not in ("TABLE", "VIEW"):
                continue
            if kind in ("INVALID", "XS ACTION", "OLS_ACTION", "DATAPUMP ACTION", "DIRECT LOAD ACTION", "DV ACTION"):
                continue
            if kind not in ("STANDARD ACTION", "SYSTEM ACTION", "SYSTEM PRIVILEGE", "OBJECT ACTION"):
                return
            if option in (*DDL_ACTIONS, *DDL_PRIVILEGES, "ALL", "ALL ACTIONS", "ALL PRIVILEGES"):
                return
            if kind == "OBJECT ACTION" and object_type in ("TABLE", "VIEW") \
                    and option in ("ALTER", "COMMENT", "RENAME", "ALL"):
                return
        raise AuditUnavailable(Msg("Oracle 统一审计中没有相关的成功 DDL 记录，且未启用记录这些操作成功结果的审计策略"),
                               "not_enabled")

    def describe_objects(self, objects: list[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
        """执行前核对：给定 模式.表 当前的列（名称、类型，按位置）、视图定义与 OBJECT_ID；对象不存在时列为空。
        用 ALL_* 视图：查询账号只看得到有权限的对象，这正是它执行时能看到的。"""
        names = [(s, n) for s, n in objects if IDENT.match(s) and IDENT.match(n)]
        out: dict[tuple[str, str], dict[str, Any]] = {k: {"columns": [], "definition": None, "physical_id": None}
                                                      for k in names}
        if not names:
            return out
        schemas, tables = sorted({s for s, _ in names}), sorted({n for _, n in names})
        try:
            with self._connect(call_timeout_ms=30_000) as conn:
                smarks, sbinds = _binds(schemas, "s")
                tmarks, tbinds = _binds(tables, "t")
                with conn.cursor() as cur:
                    cur.execute("SELECT object_id, owner, object_name, object_type FROM all_objects "
                                f"WHERE owner IN ({smarks}) AND object_name IN ({tmarks}) "
                                "AND object_type IN ('TABLE', 'VIEW')", {**sbinds, **tbinds})
                    for oid, owner, name, _ in cur.fetchall():
                        if (owner, name) in out:
                            out[(owner, name)]["physical_id"] = str(oid)
                for key, cols in self._columns(conn, "all", schemas, tables).items():
                    if key in out:
                        out[key]["columns"] = [[c[0], c[1]] for c in cols]
                for key, definition in self._definitions(conn, "all", schemas, tables).items():
                    if key in out:
                        out[key]["definition"] = definition
        except oracledb.Error as exc:
            raise _query_error(exc) from exc
        return out


def _decimal_numbers(cursor, metadata):
    """NUMBER 取为 Decimal（默认是 float，金额会有精度误差）。"""
    if metadata.type_code is oracledb.DB_TYPE_NUMBER:
        return cursor.var(Decimal, arraysize=cursor.arraysize)
    return None


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo is not None else value


def _audit_error(exc: oracledb.Error) -> AuditUnavailable:
    error = _query_error(exc)
    if error.db_errno in ("ORA-00942", "ORA-01031", "ORA-01017", "ORA-01045", "ORA-28000", "ORA-41900"):
        # 系统审计视图的 ORA-00942 通常掩盖 SELECT 权限不足，不把它误报成客户没开策略。
        return AuditUnavailable(Msg("采集账号无权读取 Oracle 统一审计；请授予 AUDIT_VIEWER 或相应读取权限（{error}）",
                                    error=error.message), "no_permission")
    if error.db_errno in ("ORA-00904", "ORA-00932", "ORA-00902"):
        return AuditUnavailable(Msg("Oracle 统一审计来源结构与连接器要求不符（{error}）", error=error.message),
                                "incompatible")
    return AuditUnavailable(Msg("读取 Oracle 统一审计失败（{error}）", error=error.message), "error")


def _query_error(exc: oracledb.Error) -> QueryError:
    message = str(exc).strip()
    m = ORA.search(message)
    code = f"{m.group(1)}-{m.group(2)}" if m else None
    if code in ("DPY-4024", "DPY-4011", "ORA-01013", "ORA-03156"):
        kind = "timeout"
    elif code in ("ORA-01031", "ORA-01456", "ORA-01017", "ORA-01045", "ORA-28000", "ORA-41900"):
        kind = "permission"
    elif code in ("ORA-00904", "ORA-00942", "ORA-04043"):
        kind = "invalid_object"
    else:
        kind = "other"
    return QueryError(kind, code, message.splitlines()[0][:2000] if message else "")
