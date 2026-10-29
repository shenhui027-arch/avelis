"""把一条抽取出的 SQL 语句解析为“目标表 ← 来源列”的列级数据流。

支持：INSERT INTO / INSERT OVERWRITE … SELECT（含列清单）、CREATE [TEMPORARY] TABLE / VIEW … AS SELECT、
USE（切换默认库）。脚本中前面语句建出的表（含临时表）会加入后续语句的表结构，从而按顺序串联。
目标列的对应：有列清单按位置；否则用已知的目标表结构按位置；都没有时按 SELECT 输出列名，并注明。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from avelis.lineage.sql import DIALECT, parse_query


@dataclass
class StatementEdge:
    source_ref: str
    source_column: str | None
    target_column: str | None
    relation: str
    expression: str | None


@dataclass
class ParsedStatement:
    status: str  # parsed | partial | unsupported | failed | skipped
    statement_type: str | None
    target_ref: str | None = None
    temporary: bool = False
    edges: list[StatementEdge] = field(default_factory=list)
    reason: str | None = None
    target_columns: list[str] = field(default_factory=list)
    use_db: str | None = None


def _qualify(table: exp.Table, default_catalog: str, default_db: str | None) -> str | None:
    db = table.db or default_db
    if not db:
        return None
    return f"{table.catalog or default_catalog}.{db}.{table.name}"


def _schema_columns(schema: dict, qualified: str) -> list[str] | None:
    catalog, db, name = qualified.split(".")
    cols = schema.get(catalog, {}).get(db, {}).get(name)
    return list(cols) if cols else None


def parse_statement(sql: str, *, schema: dict, default_catalog: str = "default_catalog",
                    default_db: str | None = None) -> ParsedStatement:
    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except ParseError as exc:
        return ParsedStatement("failed", None, reason=f"SQL 无法解析：{str(exc).splitlines()[0][:200]}")
    if tree is None:
        return ParsedStatement("skipped", None, reason="空语句")
    kind = tree.key.upper()
    if isinstance(tree, exp.Use):
        return ParsedStatement("skipped", "USE", use_db=tree.this.name if tree.this else None)
    if isinstance(tree, exp.Insert):
        target_node, query = tree.this, tree.expression
        columns = []
        if isinstance(target_node, exp.Schema):
            columns = [c.name for c in target_node.expressions]
            target_node = target_node.this
        statement_type = "INSERT OVERWRITE" if tree.args.get("overwrite") else "INSERT"
        temporary = False
    elif isinstance(tree, exp.Create) and tree.args.get("kind", "").upper() in ("TABLE", "VIEW") \
            and isinstance(tree.expression, exp.Query):
        target_node, query, columns = tree.this, tree.expression, []
        if isinstance(target_node, exp.Schema):
            columns = [c.name for c in target_node.expressions]
            target_node = target_node.this
        statement_type = f"CREATE {tree.args['kind'].upper()} AS"
        props = tree.args.get("properties")
        temporary = bool(props and any(isinstance(p, exp.TemporaryProperty) for p in props.expressions))
    elif isinstance(tree, exp.Query):
        return ParsedStatement("skipped", "SELECT", reason="只读查询，不产生数据流")
    else:
        return ParsedStatement("unsupported", kind, reason=f"{kind} 语句不产生可解析的数据流（v0.1 只解析写入类语句）")

    if not isinstance(target_node, exp.Table):
        return ParsedStatement("unsupported", statement_type, reason="目标不是表")
    target = _qualify(target_node, default_catalog, default_db)
    if target is None:
        return ParsedStatement("failed", statement_type, reason=f"目标表 {target_node.name} 缺少库名，且没有默认库")

    parsed = parse_query(query.sql(dialect=DIALECT), schema=schema, default_catalog=default_catalog,
                         default_db=default_db)
    result = ParsedStatement("parsed" if parsed.status == "ok" else parsed.status, statement_type, target,
                             temporary=temporary, reason=parsed.reason)
    if parsed.status in ("failed", "unsupported"):
        # 列级失败时仍记录表级来源
        result.edges = [StatementEdge(t, None, None, "table", None) for t in parsed.tables]
        return result
    outputs = [c.name for c in parsed.columns]
    if not columns:
        known = _schema_columns(schema, target)
        if known and len(known) == len(outputs):
            columns = known
        else:
            columns = outputs
            if known is None:
                note = "目标表未采集，按 SELECT 输出列名对应目标列"
                result.reason = f"{result.reason}；{note}" if result.reason else note
    if len(columns) != len(outputs):
        result.status = "partial"
        result.reason = f"目标列 {len(columns)} 个，SELECT 输出 {len(outputs)} 个，无法逐列对应"
        result.edges = [StatementEdge(t, None, None, "table", None) for t in parsed.tables]
        return result
    result.target_columns = columns
    for target_column, column in zip(columns, parsed.columns):
        for src in column.sources:
            result.edges.append(StatementEdge(src.ref.table, src.ref.column, target_column, src.relation,
                                              column.expression))
    for inf in parsed.influences:
        result.edges.append(StatementEdge(inf.ref.table, inf.ref.column, None, inf.relation, inf.expression))
    return result
