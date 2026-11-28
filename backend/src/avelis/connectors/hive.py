"""Apache Hive 连接器（真实数据库，HiveServer2 Thrift 接口，驱动 impyla）。按 Hive 4.1 实机核验结论实现。

- 资产名两级：库.表。Avelis 统一存为三段，Catalog 段用固定值 hive（Hive Metastore 的默认 Catalog 名）；
  生成的 SQL 不写 Catalog 段。Hive 的库、表、列名不区分大小写，元数据中存为小写。
- 采集只读元数据：SHOW TABLES IN <库> 列出表与视图，逐个 DESCRIBE FORMATTED 读列、分区列、注释、表类型与表参数。
  定义用 Hive 保存的展开文本（Expanded Query：库名、别名都已补全，与建视图时的当前库无关）：
  视图取 SHOW CREATE TABLE（输出 CREATE VIEW … AS <展开文本>）；物化视图的 SHOW CREATE TABLE 只给建表语句、不含查询，
  取 DESCRIBE FORMATTED 的 “# Materialized View Information” 小节。不读业务数据，不起 MapReduce / Tez 作业。
- 配置的库在 SHOW DATABASES 中不可见（授权插件会过滤）或读不到某张表时整次采集失败，不返回缺了对象的快照
  （否则采集服务会把它们记成删除）。
- 表类型：MANAGED_TABLE、EXTERNAL_TABLE 都是普通表（Hive 的外部表只是数据位置由用户管理，不指向另一个数据源），
  原类型记在 table_model；VIRTUAL_VIEW 为视图；MATERIALIZED_VIEW 为物化视图。
- 分区列排在普通列之后（与 SELECT * 的列序一致），列名记在 properties.partition_columns。
- 列类型保留 Hive 原样（小写，如 decimal(18,2)、array<string>、struct<a:int,b:string>）。
- 键：Hive 的主键 / 唯一约束只能是 DISABLE NOVALIDATE（不校验），不作为粒度依据，只记在 properties；
  NOT NULL 约束（Hive 3 起强制执行）记为列不可空，其余列可空。
- 物理身份：HiveServer2 不暴露表 ID，表、视图都按名称对应；实例身份只有版本与地址，不给 instance_id。
- 认证：NONE（SASL PLAIN，服务端不校验口令；未填账号时以 avelis 作会话用户）、NOSASL、LDAP（账号 + 口令）。
  Kerberos（GSSAPI）需要系统 Kerberos 库、KDC 与 keytab，未支持。只支持 binary 传输模式，不支持 http 模式。
- 问数执行（2026-11-28 在 Hive 4.1 + Tez 上联调）：
  - HiveServer2 没有只读会话或只读事务，匿名账号也能写入；只读依赖 Avelis 的单条 SELECT 校验，
    以及查询账号在 Hive 授权（SQL 标准授权或 Ranger）中只有 SELECT 权限。
  - 超时由 Avelis 控制：异步提交后轮询，到截止时间调用 cancel_operation 取消。服务端 hive.query.timeout.seconds
    只作兜底，设为截止时间再加 BACKSTOP_SECONDS：impyla 的 Thrift 定义来自 Impala，没有 Hive 的
    TIMEDOUT_STATE（8），服务端超时会以 KeyError(8) 抛出，所以不能依赖它先触发。
  - 行数上限用 fetchmany(max_rows + 1)；Hive 没有每查询内存上限，不映射 mem_limit_bytes。
  - 业务时区经会话参数 hive.local.time.zone 设置。
  - 关闭向量化执行（hive.vectorized.execution.enabled=false）：Hive 4.1 向量化执行下，窗口函数与日期列上的
    TRUNC 同在一条查询里时，TRUNC 结果错成 1970-01-01（2026-11-28 实测，关闭后正确）。宁可慢，不给错数。
- 执行前核对：对指定对象重跑采集时的 DESCRIBE FORMATTED / SHOW CREATE TABLE，列与定义的提取与采集完全一致。
- 数据新鲜度：分区表取按第一个分区列倒序的最新分区（SHOW PARTITIONS … ORDER BY … DESC LIMIT 1），
  读它的 transient_lastDdlTime；非分区表读表参数 transient_lastDdlTime。回填旧分区不计入。
"""

from __future__ import annotations

import re
import time
from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

import sqlglot
from impala.dbapi import connect
from impala.error import Error as ImpylaError
from sqlglot import exp

from avelis.connectors.base import (
    ObservedAsset,
    ObservedColumn,
    QueryError,
    QueryResult,
    Snapshot,
    checked_timezone,
)
from avelis.connectors.tls import impyla_ssl_kwargs, parse_tls_options
from avelis.i18n import gettext

CATALOG = "hive"
IDENT = re.compile(r"^[A-Za-z0-9_]+$")
AUTH_MODES = ("NONE", "NOSASL", "LDAP")
# 认证方式 → impyla 的 auth_mechanism。NONE 在 HiveServer2 上仍走 SASL 传输（PLAIN），只是不校验口令
MECHANISM = {"NONE": "PLAIN", "NOSASL": "NOSASL", "LDAP": "LDAP"}
DEFAULT_USER = "avelis"
KINDS = {
    "MANAGED_TABLE": "table",
    "EXTERNAL_TABLE": "table",
    "VIRTUAL_VIEW": "view",
    "MATERIALIZED_VIEW": "materialized_view",
}
# SHOW CREATE TABLE 对普通视图的输出：CREATE VIEW `库`.`视图` AS <展开文本>
CREATE_VIEW = re.compile(r"^\s*CREATE\s+VIEW\s+(?:`(?:[^`]|``)+`|\w+)(?:\s*\.\s*(?:`(?:[^`]|``)+`|\w+))?\s+AS\s+", re.I)
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "'": "'", "\\": "\\"}


BACKSTOP_SECONDS = 30  # 服务端兜底超时比 Avelis 的截止时间多出的秒数
POLL_SECONDS = 0.25  # 异步执行时轮询状态的间隔
# 每次问数都带的会话参数；关闭向量化执行的原因见模块说明“问数执行”
SESSION_SETTINGS = {"hive.vectorized.execution.enabled": "false"}


def _checked(names: list[str], label: str) -> list[str]:
    for name in names:
        if not IDENT.match(name):
            raise ValueError(f"{label} 名称不合法：{name!r}")
    return names


def _quote(name: str) -> str:
    return f"`{name}`"


def _unescape(text: str) -> str:
    """DESCRIBE FORMATTED 的表参数按 Java 字符串转义输出（换行写成 \\n、引号写成 \\"），还原成原文。"""
    return re.sub(r"\\(.)", lambda m: ESCAPES.get(m.group(1), m.group(0)), text)


def _cell(value: Any) -> str:
    return "" if value is None else str(value).strip()


@dataclass
class Described:
    """DESCRIBE FORMATTED 的结构化结果。"""

    columns: list[tuple[str, str, str | None]] = field(default_factory=list)  # 名称、类型、注释
    partition_columns: list[tuple[str, str, str | None]] = field(default_factory=list)
    info: dict[str, str] = field(default_factory=dict)  # Detailed Table Information、Storage Information 中的“键: 值”
    parameters: dict[str, str] = field(default_factory=dict)  # Table Parameters 或 Partition Parameters
    primary_key: list[str] = field(default_factory=list)
    not_null: set[str] = field(default_factory=set)
    expanded_query: str | None = None  # 视图 / 物化视图小节的展开文本（多行时续行在第三列）

    @property
    def table_type(self) -> str:
        return self.info.get("Table Type", "")

    @property
    def comment(self) -> str | None:
        return self.parameters.get("comment") or None


def parse_described(rows: list[tuple]) -> Described:
    """解析 DESCRIBE FORMATTED 的三列输出（col_name, data_type, comment）。

    按 “# 小节” 切分：开头（Hive 2/3 带 “# col_name” 表头，4.x 没有）是普通列；“# Partition Information” 是分区列；
    “# Detailed Table Information”、“# Storage Information” 是 “键: 值” 行，其中 “Table Parameters:” 后跟
    ('', 参数名, 参数值) 行；“# Detailed Partition Information” 的 “Partition Parameters:” 同理。
    “# Primary Key”、“# Not Null Constraints” 等约束小节的 “Column Name:” 行给出列名。
    """
    out = Described()
    section = "columns"
    group = None  # 当前的 “键:” 分组（如 Table Parameters）
    query: list[str] | None = None  # 正在收集的展开文本
    for row in rows:
        cells = list(row) + [None] * (3 - len(row))
        name, second, third = _cell(cells[0]), cells[1], cells[2]
        if name.startswith("#"):
            header = name.lstrip("#").strip().lower()
            if header != "col_name":
                section, group = header, None
            continue
        if not name and second is None and third is None:  # 空行：分组结束
            group = None
            continue
        if section in ("view information", "materialized view information"):
            # Hive 4：Expanded Query；Hive 2/3：View Expanded Text。值的第一行在第二列，续行在第三列（第二列是空白）
            key = name.rstrip(":").strip()
            if key:
                query = [_cell(second)] if key in ("Expanded Query", "View Expanded Text") else None
            elif query is not None and third is not None:
                query.append(str(third).rstrip())
            if query is not None:
                out.expanded_query = "\n".join(query).strip() or None
            continue
        if section in ("columns", "partition information"):
            if name:
                target = out.columns if section == "columns" else out.partition_columns
                target.append((name, _cell(second), _cell(third) or None))
        elif section in ("detailed table information", "detailed partition information", "storage information"):
            if name:
                key = name.rstrip(":").strip()
                if second is None and third is None:
                    group = key
                else:
                    out.info[key] = _cell(second)
                    group = None
            elif group in ("Table Parameters", "Partition Parameters") and _cell(second):
                out.parameters[_cell(second)] = _unescape(_cell(third))
        elif name.rstrip(":").strip() == "Column Name":
            if section == "primary key":
                out.primary_key.append(_cell(second))
            elif section == "not null constraints":
                out.not_null.add(_cell(second))
    return out


def view_query(create_statement: str | None) -> str | None:
    """SHOW CREATE TABLE 的输出 → 视图的查询文本。

    普通视图的输出固定为 CREATE VIEW `库`.`视图` AS <展开文本>，直接取 AS 之后的原文（保留 Hive 存的写法，
    定义指纹不随 SQLGlot 的生成规则变化）；其他写法（如物化视图带存储子句）用 SQLGlot（hive）取出其中的查询。
    都取不出时返回原文。"""
    if not create_statement or not create_statement.strip():
        return None
    text = create_statement.strip()
    m = CREATE_VIEW.match(text)
    if m:
        return text[m.end() :].strip()
    try:
        statement = sqlglot.parse_one(text, read="hive")
    except sqlglot.errors.ParseError:
        return text
    if isinstance(statement, exp.Create) and isinstance(statement.expression, exp.Query):
        return statement.expression.sql(dialect="hive")
    return text


def _definition(cur, db: str, name: str, kind: str | None, described: Described) -> str | None:
    if kind == "view":
        cur.execute(f"SHOW CREATE TABLE {_quote(db)}.{_quote(name)}")
        return view_query("\n".join("" if r[0] is None else str(r[0]) for r in cur.fetchall()))
    if kind == "materialized_view":
        return described.expanded_query
    return None


def observed(
    db: str, name: str, described: Described, definition: str | None, *, catalog: str = CATALOG
) -> ObservedAsset:
    """一张表 / 视图的观察结果。分区列排在普通列之后；只有 NOT NULL 约束的列不可空。"""
    table_type = described.table_type
    kind = KINDS.get(table_type, "table")
    seen: set[str] = set()
    columns: list[ObservedColumn] = []
    for col, data_type, comment in [*described.columns, *described.partition_columns]:
        if col in seen:  # 个别版本把分区列在普通列里也列一遍
            continue
        seen.add(col)
        columns.append(
            ObservedColumn(
                name=col,
                data_type=data_type,
                ordinal=len(columns) + 1,
                nullable=col not in described.not_null,
                comment=comment,
            )
        )
    info = described.info
    properties: dict[str, Any] = {
        "table_type": table_type or None,
        "partition_columns": [c[0] for c in described.partition_columns],
        "location": info.get("Location") or None,
        "input_format": info.get("InputFormat") or None,
        "create_time": info.get("CreateTime") or None,
        "owner": info.get("Owner") or None,
    }
    if described.primary_key:
        properties["declared_primary_key"] = described.primary_key  # DISABLE NOVALIDATE：不校验，不作粒度依据
    return ObservedAsset(
        catalog=catalog,
        db=db,
        name=name,
        kind=kind,
        columns=columns,
        physical_id=None,
        table_model=table_type or None,
        key_columns=[],
        unique_keys=[],
        comment=described.comment,
        definition=definition if kind != "table" else None,
        properties=properties,
    )


class HiveConnector:
    """HiveServer2 协议连接器。同协议的引擎（Spark Thrift Server）继承本类，只覆盖下面标明“子类可覆盖”的钩子。"""

    kind = "hive"
    dialect = "hive"
    is_test_adapter = False
    catalog = CATALOG  # 资产名的固定 Catalog 段

    def __init__(self, host: str, port: int, user: str, password: str, options: dict[str, Any] | None = None):
        self.options = options or {}
        auth = str(self.options.get("auth") or "NONE").upper()
        if auth not in AUTH_MODES:
            raise ValueError(f"Hive 认证方式不支持：{auth}（可选 {'、'.join(AUTH_MODES)}）")
        if auth == "LDAP" and not (user and password):
            raise ValueError("Hive 认证方式 LDAP 需要采集账号与口令")
        self.auth = auth
        self._conn_args = dict(
            host=host,
            port=port,
            auth_mechanism=MECHANISM[auth],
            user=user or DEFAULT_USER,
            # PLAIN 机制要求带口令；NONE 模式下服务端不校验，随便给一个
            password=password or ("-" if auth == "NONE" else None),
        )
        self._ssl_kwargs = impyla_ssl_kwargs(parse_tls_options(self.options))

    def _connect(self, timeout: int = 60):
        return connect(**self._conn_args, **self._ssl_kwargs, timeout=timeout)

    def _cursor(self, conn):
        """会话用户显式传给 cursor：impyla 的 cursor() 默认用操作系统用户名开会话，NOSASL 时服务端就以它为会话用户。"""
        return conn.cursor(user=self._conn_args["user"])

    # --- 采集 ---------------------------------------------------------------------

    def server_identity(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            return self._server_identity(self._cursor(conn))
        except ImpylaError as exc:
            raise RuntimeError(_first_line(exc)) from exc
        finally:
            conn.close()

    def _server_identity(self, cur) -> dict[str, Any]:
        cur.execute("SELECT version()")
        row = cur.fetchone()
        return {
            "version": str(row[0]) if row else None,
            "endpoint": f"{self._conn_args['host']}:{self._conn_args['port']}",
            "auth": self.auth,
        }

    def snapshot(self) -> Snapshot:
        databases = _checked([str(d).lower() for d in self.options.get("databases", [])], "库")
        if not databases:
            raise ValueError("数据源未配置要采集的库（options.databases）")
        warnings: list[str] = []
        assets: list[ObservedAsset] = []
        conn = self._connect(timeout=120)
        try:
            cur = self._cursor(conn)
            identity = self._server_identity(cur)
            existing = {str(r[0]).lower() for r in self._rows(cur, "SHOW DATABASES")}
            # 配置的库看不到时整次采集失败（与读不到表结构一致）：Hive 授权插件会按权限过滤 SHOW DATABASES，
            # 看不到不代表已删除；跳过会把这个库已采集的资产全部记成删除
            missing = [db for db in databases if db not in existing]
            if missing:
                raise RuntimeError(
                    f"配置的库 {'、'.join(missing)} 在 SHOW DATABASES 中不可见"
                    "（不存在或采集账号无权限），本次采集不更新目录"
                )
            for db in databases:
                for name in self._table_names(cur, db):
                    # 读不到某张表的结构时整次采集失败：跳过会被当成“已删除”，产生错误的变更
                    described = self._describe(self._rows(cur, f"DESCRIBE FORMATTED {_quote(db)}.{_quote(name)}"))
                    kind = KINDS.get(described.table_type)
                    definition = self._definition(cur, db, name, kind, described)
                    if kind in ("view", "materialized_view") and definition is None:
                        warnings.append(f"视图 {db}.{name} 未读到定义")
                    if kind is None:
                        warnings.append(f"{db}.{name} 的类型 {described.table_type or '未知'} 按普通表采集")
                    assets.append(self._observed(db, name, described, definition))
        except ImpylaError as exc:
            raise RuntimeError(_first_line(exc)) from exc
        finally:
            conn.close()
        return Snapshot(server_identity=identity, assets=assets, warnings=warnings)

    # --- 问数执行与执行前核对 -------------------------------------------------------

    def execute_select(
        self, sql: str, *, timeout_seconds: int, mem_limit_bytes: int, max_rows: int, timezone: str | None = None
    ) -> QueryResult:
        """执行调用层已校验的一条 SELECT：异步提交、到截止时间取消、fetchmany 截断。见模块说明“问数执行”。"""
        if timeout_seconds <= 0:
            raise QueryError("timeout", None, gettext("超时值必须为正数，未执行"))
        if max_rows < 0:
            raise QueryError("other", None, gettext("查询行数上限不能为负数，未执行"))
        backstop = timeout_seconds + BACKSTOP_SECONDS
        if timezone:
            checked_timezone(timezone)

        conn = cur = None
        try:
            conn = self._connect(timeout=backstop)
            cur = self._cursor(conn)
            # 截止时间从提交前算起：HiveServer2 关闭异步编译时，execute_async 会等编译完才返回
            deadline = time.monotonic() + timeout_seconds
            configuration = self._session(cur, backstop=backstop, timezone=timezone)
            cur.execute_async(sql, configuration=configuration)
            while cur.is_executing():
                if time.monotonic() >= deadline:
                    cur.cancel_operation()
                    raise QueryError("timeout", None, gettext("查询超过 {seconds} 秒，已取消", seconds=timeout_seconds))
                time.sleep(POLL_SECONDS)
            # is_executing() 对 ERROR_STATE 只返回 False、不带报错：用 impyla 同步执行同样的收尾取回数据库原始报错
            cur._wait_to_finish()
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(max_rows + 1)
            return QueryResult(columns=columns, rows=[list(r) for r in rows[:max_rows]], truncated=len(rows) > max_rows)
        except ImpylaError as exc:
            raise self._query_error(exc) from exc
        finally:
            for resource in (cur, conn):
                if resource is not None:
                    with suppress(Exception):
                        resource.close()

    def execute_explore(
        self,
        sql: str,
        *,
        timeout_seconds: int,
        max_rows: int,
        mem_limit_bytes: int = 0,
        timezone: str | None = None,
    ) -> QueryResult:
        """执行校验后重新生成的探索查询：与 execute_select 相同的取消、截断与时区处理。"""
        return self.execute_select(
            sql, timeout_seconds=timeout_seconds, mem_limit_bytes=mem_limit_bytes, max_rows=max_rows, timezone=timezone
        )

    def describe_objects(
        self, objects: list[tuple[str, str]], *, timeout_seconds: int | None = None
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """执行前核对当前列与视图定义：与 snapshot 同样的 DESCRIBE FORMATTED / SHOW CREATE TABLE 与提取函数。"""
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise QueryError("timeout", None, gettext("超时值必须为正数，未执行"))
        out: dict[tuple[str, str], dict[str, Any]] = {
            key: {"columns": [], "definition": None, "physical_id": None} for key in objects
        }
        if not out:
            return out
        try:
            with closing(self._connect(timeout=timeout_seconds or 60)) as conn, closing(self._cursor(conn)) as cur:
                for (input_db, input_name), result in out.items():
                    db, name = input_db.lower(), input_name.lower()
                    if not (IDENT.fullmatch(db) and IDENT.fullmatch(name)):
                        continue
                    try:
                        rows = self._rows(cur, f"DESCRIBE FORMATTED {_quote(db)}.{_quote(name)}")
                    except ImpylaError as exc:
                        error = self._query_error(exc)
                        if self._missing_object(error):
                            continue
                        raise error from exc
                    described = self._describe(rows)
                    definition = self._definition(cur, db, name, KINDS.get(described.table_type), described)
                    asset = self._observed(db, name, described, definition)
                    result["columns"] = [[c.name, c.data_type] for c in asset.columns]
                    result["definition"] = asset.definition
        except ImpylaError as exc:
            raise self._query_error(exc) from exc
        return out

    def freshness(self, tables: list[tuple[str, str]]) -> dict[str, Any]:
        """数据新鲜度：键为 “库.表”（按传入写法）。

        值为 UTC ISO 时间字符串（如 2026-11-28T09:07:44+00:00），读不到时为 None。
        """
        if not tables:
            return {}
        out: dict[str, Any] = {}
        with closing(self._connect()) as conn, closing(self._cursor(conn)) as cur:
            for input_db, input_name in tables:
                db, name = input_db.lower(), input_name.lower()
                if not (IDENT.fullmatch(db) and IDENT.fullmatch(name)):
                    continue
                key = f"{input_db}.{input_name}"
                out[key] = None
                table = f"{_quote(db)}.{_quote(name)}"
                describe_sql = f"DESCRIBE FORMATTED {table}"
                try:
                    described = self._describe(self._rows(cur, describe_sql))
                    if described.partition_columns:
                        partition = self._latest_partition(cur, table, described)
                        if not partition:
                            continue
                        spec = partition_spec_sql(partition)
                        described = self._describe(self._rows(cur, f"{describe_sql} PARTITION ({spec})"))
                except ImpylaError:
                    continue
                try:
                    seconds = int(described.parameters.get("transient_lastDdlTime", ""))
                except ValueError:
                    continue
                out[key] = datetime.fromtimestamp(seconds, timezone.utc).isoformat()
        return out

    # --- 子类可覆盖的钩子 -------------------------------------------------------------

    def _session(self, cur, *, backstop: int, timezone: str | None) -> dict[str, str]:
        """子类可覆盖：本次查询的会话参数（作为 execute_async 的 configuration）。时区已由调用方校验。"""
        configuration = {"hive.query.timeout.seconds": str(backstop), **SESSION_SETTINGS}
        if timezone:
            configuration["hive.local.time.zone"] = timezone
        return configuration

    def _table_names(self, cur, db: str) -> list[str]:
        """子类可覆盖：库里的表与视图名。Hive 的 SHOW TABLES 只有一列表名。"""
        return [str(row[0]) for row in self._rows(cur, f"SHOW TABLES IN {_quote(db)}")]

    @staticmethod
    def _describe(rows: list[tuple]) -> Described:
        """子类可覆盖：解析 DESCRIBE FORMATTED 的输出。"""
        return parse_described(rows)

    def _definition(self, cur, db: str, name: str, kind: str | None, described: Described) -> str | None:
        """子类可覆盖：视图 / 物化视图的定义。"""
        return _definition(cur, db, name, kind, described)

    def _observed(self, db: str, name: str, described: Described, definition: str | None) -> ObservedAsset:
        """子类可覆盖：一张表 / 视图的观察结果。"""
        return observed(db, name, described, definition, catalog=self.catalog)

    def _latest_partition(self, cur, table: str, described: Described) -> str | None:
        """子类可覆盖：按第一个分区列倒序的最新分区说明（如 dt=2026-11-02），没有分区时为 None。"""
        column = _quote(described.partition_columns[0][0].replace("`", "``"))
        cur.execute(f"SHOW PARTITIONS {table} ORDER BY {column} DESC LIMIT 1")
        row = cur.fetchone()
        return str(row[0]) if row else None

    @staticmethod
    def _missing_object(error: QueryError) -> bool:
        """子类可覆盖：执行前核对时，这个错误是否表示对象不存在。"""
        return error.db_errno == 10001 or "table not found" in error.message.lower()

    @staticmethod
    def _query_error(exc: Exception) -> QueryError:
        """把 HiveServer2 报错归一化。

        原文用 _first_line 去掉 Query ID 后缀；db_errno 取 [Error NNNNN] 中的数字，没有时为 None。
        """
        message = _first_line(exc)
        match = re.search(r"\[Error (\d+)\]", message, re.I)
        errno = int(match.group(1)) if match else None
        text = message.lower()
        if errno in (10001, 10002, 10004) or any(
            phrase in text for phrase in ("table not found", "invalid column reference", "invalid table alias")
        ):
            code = "invalid_object"
        elif any(
            phrase in text for phrase in ("hiveaccesscontrolexception", "permission denied", "authorization failed")
        ):
            code = "permission"
        elif "timed out" in text or "timeout" in text:
            code = "timeout"
        else:
            code = "other"
        return QueryError(code, errno, message)

    @staticmethod
    def _rows(cur, sql: str) -> list[tuple]:
        cur.execute(sql)
        return list(cur.fetchall())


def partition_spec_sql(spec: str) -> str:
    """SHOW PARTITIONS 的分区说明（a=1/b=x，值按 Hive 路径转义）→ PARTITION 子句内容 `a`='1', `b`='x'。"""
    parts = []
    for part in spec.split("/"):
        name, sep, value = part.partition("=")
        if not sep or not IDENT.fullmatch(name):
            raise ValueError(f"Hive 分区说明不合法：{part!r}")
        value = unquote(value).replace("\\", "\\\\").replace("'", "\\'")
        parts.append(f"{_quote(name)}='{value}'")
    return ", ".join(parts)


def _first_line(exc: Exception) -> str:
    # HiveServer2 的报错带 “; Query ID: …” 后缀与驱动说明，只保留数据库原始报错
    text = str(exc).strip()
    return re.sub(r";\s*Query ID:.*$", "", text, flags=re.S)[:2000]
