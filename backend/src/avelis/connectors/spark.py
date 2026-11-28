"""Spark SQL 连接器（Spark Thrift Server，HiveServer2 协议，驱动 impyla）。

按 2026-11-28 在 Spark 4.0.1 上的实机核验实现。

继承 Hive 连接器（连接、认证、TLS、异步执行与到期取消、执行前核对与新鲜度的流程都相同），只覆盖 Spark 不同的地方：

- 资产名两级：库.表，固定 Catalog 段 spark_catalog（Spark 的会话 Catalog）；生成的 SQL 不写 Catalog 段。
- DESCRIBE FORMATTED 的格式与 Hive 不同：
  - “键 / 值”两列（键后没有冒号）；
  - 普通列里也列出分区列；
  - Type 为 MANAGED / EXTERNAL / VIEW；
  - Table Properties、Partition Parameters 写成 “[k=v, k2=v2]”。
- 视图定义：Spark 保存原文（View Text），不展开库名，也不含 CREATE VIEW v (列, …) 里声明的列名。不写库名的表按
  “View Catalog and Namespace”里建视图时的当前库解析，不一定是视图所在的库。适配器因此像 Hive 的展开文本那样补全：
  按建视图时的当前库补全表的库名（CTE 不动），并按位置把输出列名对齐到视图的列；记下的定义因此自足，
  建视图时的当前库或声明的列名变化都会体现在定义的指纹与执行前核对里。补全失败（SQLGlot 解析不了）时保留原文。
  建视图时的 Catalog 与库另记在资产属性 view_context 里，只作说明。
- 问数执行：会话参数经 execute_async 的 configuration 传入无效，改为先执行 SET：
  - spark.sql.session.timeZone 设业务时区；
  - spark.sql.thriftServer.queryTimeout 设服务端兜底超时。
  服务端超时报 TIMEDOUT_STATE（8），impyla 不认识（KeyError(8)），所以照旧由 Avelis 到期取消；
  Spark 的状态查询每次最多阻塞约 5 秒，取消最多晚约 5 秒。
- 数据新鲜度：SHOW PARTITIONS 不支持 ORDER BY / LIMIT，取全部分区说明里最大的一条（按字符串，日期分区值需零填充），
  读它的 transient_lastDdlTime；非分区的数据源表（USING parquet 等）没有这个参数，不报告新鲜度。
- 错误：Spark 4 的报错带错误类，如 [TABLE_OR_VIEW_NOT_FOUND]、[UNRESOLVED_COLUMN.WITH_SUGGESTION]，按错误类归类。
- 只读：Spark Thrift Server 没有只读会话；只读依赖 Avelis 的单条 SELECT 校验与只有 SELECT 权限的查询账号。
  TRANSFORM … USING 与 reflect / java_method 在 Spark 里同样可用，已由 avelis.sql_guard 拒绝。
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError
from sqlglot.optimizer.qualify_tables import qualify_tables

from avelis.connectors.base import ObservedAsset, QueryError
from avelis.connectors.hive import Described, HiveConnector, _cell, _first_line, _quote, observed

CATALOG = "spark_catalog"
# Spark 的表类型 → 与 Hive 共用的表类型名（KINDS 按 Hive 的名字区分表 / 视图）
TABLE_TYPES = {"MANAGED": "MANAGED_TABLE", "EXTERNAL": "EXTERNAL_TABLE", "VIEW": "VIRTUAL_VIEW"}
ERROR_CLASS = re.compile(r"\[([A-Z_]+(?:\.[A-Z_]+)?)\]")
# 错误类（点号前的部分）→ Avelis 的错误类别
INVALID_OBJECT_CLASSES = {"TABLE_OR_VIEW_NOT_FOUND", "UNRESOLVED_COLUMN", "SCHEMA_NOT_FOUND", "UNRESOLVED_ROUTINE"}
PERMISSION_CLASSES = {"INSUFFICIENT_PERMISSIONS"}


def bracket_properties(text: str | None) -> dict[str, str]:
    """“[k=v, k2=v2]” → {"k": "v", "k2": "v2"}；空或不是方括号包着的文本返回 {}。"""
    text = (text or "").strip()
    if not (text.startswith("[") and text.endswith("]")):
        return {}
    properties: dict[str, str] = {}
    # Spark 的属性文本按逗号加空格分隔，假定值本身不含这个分隔符。
    for item in text[1:-1].split(", "):
        key, sep, value = item.partition("=")
        if sep:
            properties[key.strip()] = value.strip()
    return properties


def parse_spark_described(rows: list[tuple]) -> Described:
    """解析 Spark 的 DESCRIBE FORMATTED（三列：col_name, data_type, comment；空值可能是 None 或空串）。

    返回与 Hive 相同的 Described：
    - columns：开头的普通列，去掉分区列；
    - partition_columns：“# Partition Information” 小节（跳过 “# col_name” 表头）；
    - info：
      - Table Type 为 TABLE_TYPES 映射后的名字（未知原样）；
      - Location、Owner、Provider 原样；
      - CreateTime 取 “Created Time”；
      - View Catalog and Namespace 原样；
    - parameters：Table Properties（或分区的 Partition Parameters）解析后的键值，另加 “comment”（来自 Comment 行）；
    - expanded_query：View Text（多行文本可能跨多行输出：续行第一列为空、文本在第二列）。
    """
    out = Described()
    section = "columns"
    metadata_sections = ("# Detailed Table Information", "# Detailed Partition Information", "# Storage Information")
    view_text: list[str] = []
    reading_view_text = False
    table_comment = None
    for raw_name, raw_value, raw_comment in rows:
        name = _cell(raw_name)
        if name.startswith("#"):
            if section == "# Partition Information" and name == "# col_name":
                continue
            section = name
            reading_view_text = False
            continue
        if section in ("columns", "# Partition Information"):
            if name:
                columns = out.columns if section == "columns" else out.partition_columns
                columns.append((name, _cell(raw_value), _cell(raw_comment) or None))
            else:
                section = ""
            continue
        if section not in metadata_sections:
            continue

        value = "" if raw_value is None else str(raw_value)
        if not name:
            if reading_view_text:
                view_text.append(value)
            continue
        reading_view_text = name == "View Text"
        if reading_view_text:
            view_text = [value]
        elif name == "Type":
            out.info["Table Type"] = TABLE_TYPES.get(value, value)
        elif name == "Created Time":
            out.info["CreateTime"] = value
        elif name in ("Location", "Owner", "Provider", "View Catalog and Namespace"):
            out.info[name] = value
        elif name in ("Table Properties", "Partition Parameters"):
            out.parameters.update(bracket_properties(value))
        elif name == "Comment":
            table_comment = value

    partition_names = {name for name, _, _ in out.partition_columns}
    out.columns = [column for column in out.columns if column[0] not in partition_names]
    if table_comment is not None:
        out.parameters["comment"] = table_comment
    out.expanded_query = "\n".join(view_text).strip() or None
    return out


def spark_view_definition(view_text: str | None, namespace: str | None, columns: list[str]) -> str | None:
    """把 Spark 保存的视图原文补全成自足的定义：表补上建视图时的当前库，输出列名按位置对齐视图的列。

    namespace 取 “View Catalog and Namespace”（如 spark_catalog.default），最后一段是库；columns 是视图的列名。
    投影含 * 或列数对不上时不改列名；解析失败时返回原文。
    """
    if not view_text:
        return view_text
    try:
        tree = sqlglot.parse_one(view_text, read="spark")
    except (ParseError, TokenError):
        return view_text
    db = (namespace or "").strip().rpartition(".")[2]
    if db:
        tree = qualify_tables(tree, db=db, dialect="spark")
    head = tree
    while isinstance(head, exp.SetOperation):
        head = head.this
    if isinstance(head, exp.Select) and columns and len(head.expressions) == len(columns):
        projections = head.expressions
        if not any(
            isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star)) for p in projections
        ):
            for projection, name in zip(projections, columns, strict=True):
                if projection.alias_or_name != name:
                    projection.replace(exp.alias_(projection.unalias().copy(), exp.to_identifier(name)))
    return tree.sql(dialect="spark")


class SparkConnector(HiveConnector):
    kind = "spark"
    dialect = "spark"
    is_test_adapter = False
    catalog = CATALOG

    def _session(self, cur, *, backstop: int, timezone: str | None) -> dict[str, str]:
        """先 SET 会话参数（configuration 对 Spark 无效），返回空的 configuration。"""
        cur.execute(f"SET spark.sql.thriftServer.queryTimeout={int(backstop)}s")
        if timezone:
            cur.execute(f"SET spark.sql.session.timeZone={timezone}")
        return {}

    def _table_names(self, cur, db: str) -> list[str]:
        """Spark 的 SHOW TABLES 有三列（namespace、tableName、isTemporary）：取表名，跳过临时视图（不属于这个库）。"""
        rows = self._rows(cur, f"SHOW TABLES IN {_quote(db)}")
        return [str(row[1]) for row in rows if len(row) >= 3 and not row[2]]

    @staticmethod
    def _describe(rows: list[tuple]) -> Described:
        return parse_spark_described(rows)

    def _definition(self, cur, db: str, name: str, kind: str | None, described: Described) -> str | None:
        """视图定义取 View Text，按建视图时的当前库与视图的列补全（见模块说明）；表没有定义。"""
        if kind != "view":
            return None
        return spark_view_definition(
            described.expanded_query,
            described.info.get("View Catalog and Namespace"),
            [name for name, _, _ in described.columns],
        )

    def _observed(self, db: str, name: str, described: Described, definition: str | None) -> ObservedAsset:
        """在 Hive 的观察结果上补 Spark 的属性：provider，以及视图的 view_context（建视图时的当前 Catalog 与库）。"""
        asset = observed(db, name, described, definition, catalog=self.catalog)
        asset.properties["provider"] = described.info.get("Provider") or None
        namespace = described.info.get("View Catalog and Namespace", "").strip()
        if asset.kind == "view" and namespace:
            catalog, _, view_db = namespace.rpartition(".")
            asset.properties["view_context"] = {"catalog": catalog, "db": view_db.lower()}
        return asset

    def _latest_partition(self, cur, table: str, described: Described) -> str | None:
        """SHOW PARTITIONS 不支持 ORDER BY / LIMIT：取全部分区说明里按字符串最大的一条。"""
        cur.execute(f"SHOW PARTITIONS {table}")
        return max((str(row[0]) for row in cur.fetchall()), default=None)

    @staticmethod
    def _missing_object(error: QueryError) -> bool:
        return error.db_errno == "TABLE_OR_VIEW_NOT_FOUND"

    @staticmethod
    def _query_error(exc: Exception) -> QueryError:
        """按 Spark 错误类归类：db_errno 为错误类全名（如 UNRESOLVED_COLUMN.WITH_SUGGESTION），没有时为 None。"""
        message = _first_line(exc)
        match = ERROR_CLASS.search(message)
        errno = match.group(1) if match else None
        error_class = errno.partition(".")[0] if errno else None
        text = message.lower()
        if error_class in INVALID_OBJECT_CLASSES:
            code = "invalid_object"
        elif error_class in PERMISSION_CLASSES or "accesscontrolexception" in text or "permission denied" in text:
            code = "permission"
        elif "timed out" in text or "timeout" in text:
            code = "timeout"
        else:
            code = "other"
        return QueryError(code, errno, message)


__all__ = ["CATALOG", "SparkConnector", "bracket_properties", "parse_spark_described", "spark_view_definition"]
