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
- 不支持问数执行：Hive 查询要起计算作业，超时、资源限制与错误归类没有联调，不声明 query 能力。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from impala.dbapi import connect
from impala.error import Error as ImpylaError
from sqlglot import exp

from avelis.connectors.base import ObservedAsset, ObservedColumn, Snapshot

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
    parameters: dict[str, str] = field(default_factory=dict)  # Table Parameters
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
    ('', 参数名, 参数值) 行；“# Primary Key”、“# Not Null Constraints” 等约束小节的 “Column Name:” 行给出列名。
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
        elif section in ("detailed table information", "storage information"):
            if name:
                key = name.rstrip(":").strip()
                if second is None and third is None:
                    group = key
                else:
                    out.info[key] = _cell(second)
                    group = None
            elif group == "Table Parameters" and _cell(second):
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


def observed(db: str, name: str, described: Described, definition: str | None) -> ObservedAsset:
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
        catalog=CATALOG,
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
    kind = "hive"
    dialect = "hive"
    is_test_adapter = False

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

    def _connect(self, timeout: int = 60):
        return connect(**self._conn_args, timeout=timeout)

    # --- 采集 ---------------------------------------------------------------------

    def server_identity(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            return self._server_identity(conn.cursor())
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
            cur = conn.cursor()
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
                for (name,) in self._rows(cur, f"SHOW TABLES IN {_quote(db)}"):
                    name = str(name)
                    # 读不到某张表的结构时整次采集失败：跳过会被当成“已删除”，产生错误的变更
                    described = parse_described(self._rows(cur, f"DESCRIBE FORMATTED {_quote(db)}.{_quote(name)}"))
                    kind = KINDS.get(described.table_type)
                    definition = None
                    if kind == "view":
                        lines = self._rows(cur, f"SHOW CREATE TABLE {_quote(db)}.{_quote(name)}")
                        definition = view_query("\n".join("" if r[0] is None else str(r[0]) for r in lines))
                    elif kind == "materialized_view":
                        definition = described.expanded_query
                    if kind in ("view", "materialized_view") and definition is None:
                        warnings.append(f"视图 {db}.{name} 未读到定义")
                    if kind is None:
                        warnings.append(f"{db}.{name} 的类型 {described.table_type or '未知'} 按普通表采集")
                    assets.append(observed(db, name, described, definition))
        except ImpylaError as exc:
            raise RuntimeError(_first_line(exc)) from exc
        finally:
            conn.close()
        return Snapshot(server_identity=identity, assets=assets, warnings=warnings)

    @staticmethod
    def _rows(cur, sql: str) -> list[tuple]:
        cur.execute(sql)
        return list(cur.fetchall())


def _first_line(exc: Exception) -> str:
    # HiveServer2 的报错带 “; Query ID: …” 后缀与驱动说明，只保留数据库原始报错
    text = str(exc).strip()
    return re.sub(r";\s*Query ID:.*$", "", text, flags=re.S)[:2000]
