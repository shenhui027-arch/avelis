"""确定性编译与校验：SQL 只由已发布指标版本 + 已校验的意图生成，再独立解析校验一遍才执行。

校验不靠提示词、不靠正则：重新解析实际 SQL 的语法树，检查语句类型、引用的表与列、关联路径、LIMIT。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from avelis.governance.metric_sql import BASE_ALIAS, column_ref, literal, parse_snippet, table_expr

FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command, exp.Into,
             exp.Merge, exp.TruncateTable, exp.Set, exp.Use)


@dataclass
class CompiledQuery:
    sql: str
    tables: list[str]
    columns: list[str]
    plan: dict[str, Any] = field(default_factory=dict)


class ValidationFailed(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("；".join(reasons))
        self.reasons = reasons


def compile_metric_query(*, version, aliases: dict[str, Any], steps: list[dict[str, Any]], intent, max_rows: int,
                         time_type: str, dialect: str) -> CompiledQuery:
    """dialect：执行该查询的数据源引擎的 SQLGlot 方言。"""
    names = set(aliases)

    def col(text: str) -> exp.Expression:
        return parse_snippet(text, dialect=dialect, label="列", aliases=names, require_aggregate=False).expression

    dims = {d["name"]: d for d in version.dimensions or []}
    select = exp.Select()
    group_cols = []
    for name in intent.group_by:
        select = select.select(exp.alias_(col(dims[name]["column"]), name))
        group_cols.append(col(dims[name]["column"]))
    measure = parse_snippet(version.expression, dialect=dialect, label="表达式", aliases=names,
                            require_aggregate=True).expression
    select = select.select(exp.alias_(measure, "value"))

    base = aliases[BASE_ALIAS]
    select = select.from_(table_expr(_qualified(base), BASE_ALIAS))
    for step in steps:
        path = step["path"]
        right = step["right"]
        if step["pre_aggregate"]:
            inner = exp.Select()
            for c in path.right_columns:
                inner = inner.select(exp.column(c))
            inner = inner.select(exp.alias_(exp.Count(this=exp.Star()), "row_count"))
            keys = [exp.column(c) for c in path.right_columns]
            inner = inner.from_(table_expr(_qualified(right), "x")).group_by(*keys)
            source = exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(step["alias"])))
        else:
            source = table_expr(_qualified(right), step["alias"])
        condition = exp.and_(*[
            exp.EQ(this=exp.column(lc, table=step["from"]), expression=exp.column(rc, table=step["alias"]))
            for lc, rc in zip(path.left_columns, path.right_columns)])
        select = select.join(source, on=condition, join_type="inner")

    t_alias, t_col = column_ref(version.time_column, dialect=dialect, label="时间字段", aliases=names)
    time_column = exp.column(t_col, table=t_alias)
    conditions: list[exp.Expression] = []
    if time_type.startswith("date") and not time_type.startswith("datetime"):
        conditions.append(exp.Between(this=time_column, low=literal(intent.start_date), high=literal(intent.end_date)))
    else:
        conditions.append(exp.GTE(this=time_column, expression=literal(f"{intent.start_date} 00:00:00")))
        conditions.append(exp.LT(this=time_column,
                                 expression=literal(f"{intent.end_date + timedelta(days=1)} 00:00:00")))
    if version.filters:
        conditions.append(exp.Paren(this=parse_snippet(version.filters, dialect=dialect, label="过滤条件",
                                                       aliases=names, require_aggregate=False).expression))
    for f in intent.filters:
        target = col(dims[f.dimension]["column"])
        if len(f.values) == 1:
            conditions.append(exp.EQ(this=target, expression=literal(f.values[0])))
        else:
            conditions.append(exp.In(this=target, expressions=[literal(v) for v in f.values]))
    select = select.where(exp.and_(*conditions))
    if group_cols:
        select = select.group_by(*group_cols)
        if getattr(intent, "order", None):  # 排名：按度量排序，维度作为次序键保证结果稳定
            select = select.order_by(exp.Ordered(this=exp.column("value"), desc=intent.order == "desc"),
                                     *group_cols)
        else:
            select = select.order_by(*group_cols)
    limit = getattr(intent, "limit", None)
    select = select.limit(min(limit, max_rows) if limit else max_rows)
    sql = select.sql(dialect=dialect, comments=False)
    tables = sorted({_qualified(a) for a in aliases.values()})
    return CompiledQuery(sql=sql, tables=tables, columns=[*intent.group_by, "value"],
                         plan={"base": _qualified(base), "joins": [
                             {"alias": s["alias"], "path": s["path"].code, "pre_aggregate": s["pre_aggregate"]}
                             for s in steps], "group_by": intent.group_by,
                             "time_range": [str(intent.start_date), str(intent.end_date)],
                             "filters": [f.model_dump() for f in intent.filters]})


def distinct_values_sql(qualified: str, column: str, *, limit: int, dialect: str) -> str:
    """维度可选值：SELECT DISTINCT 列 FROM 表 WHERE 列 IS NOT NULL LIMIT n。"""
    col = exp.column(column, table="t")
    return (exp.select(col).distinct().from_(table_expr(qualified, "t")).where(exp.Not(this=exp.Is(
        this=col.copy(), expression=exp.Null()))).limit(limit).sql(dialect=dialect, identify=True))


def duplicate_key_sql(qualified: str, columns: list[str], *, dialect: str) -> str:
    """粒度核验：按键分组找重复，取一条样例。"""
    keys = [exp.column(c, table="t") for c in columns]
    return (exp.select(*keys, exp.alias_(exp.Count(this=exp.Star()), "n")).from_(table_expr(qualified, "t"))
            .group_by(*[k.copy() for k in keys]).having(exp.GT(this=exp.Count(this=exp.Star()),
                                                                 expression=exp.Literal.number(1)))
            .limit(1).sql(dialect=dialect, identify=True))


def _qualified(asset) -> str:
    return f"{asset.catalog_name}.{asset.db_name}.{asset.name}"


def validate_sql(sql: str, *, dialect: str, allowed_tables: set[str], schema: dict[str, set[str]],
                 max_rows: int) -> None:
    """对实际要执行的 SQL 做确定性校验。schema：表 → 允许的列。"""
    reasons = []
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except ParseError as exc:
        raise ValidationFailed([f"SQL 无法解析，拒绝执行：{str(exc).splitlines()[0][:200]}"]) from exc
    if len(statements) != 1:
        raise ValidationFailed([f"只允许一条语句，实际 {len(statements)} 条"])
    root = statements[0]
    if not isinstance(root, exp.Select):
        raise ValidationFailed([f"只允许 SELECT，实际为 {root.key.upper()}"])
    for node in root.walk():
        if isinstance(node, FORBIDDEN):
            reasons.append(f"包含禁止的语法 {node.key.upper()}")
    limit = root.args.get("limit")
    if limit is None:
        reasons.append("缺少 LIMIT")
    else:
        try:
            if int(limit.expression.name) > max_rows:
                reasons.append(f"LIMIT 超过上限 {max_rows}")
        except (ValueError, AttributeError):
            reasons.append("LIMIT 不是常数")
    aliases: dict[str, str] = {}
    for table in root.find_all(exp.Table):
        name = ".".join(p for p in (table.catalog, table.db, table.name) if p)
        if name not in allowed_tables:
            reasons.append(f"引用了未批准的表 {name}")
        aliases[table.alias_or_name] = name
    derived = {s.alias_or_name: s for s in root.find_all(exp.Subquery)}
    for column in root.find_all(exp.Column):
        table = column.table
        if table in derived:
            continue
        target = aliases.get(table)
        if target is None:
            if table:
                reasons.append(f"列 {column.sql(dialect=dialect)} 的来源未知")
            continue
        if column.name not in schema.get(target, set()):
            reasons.append(f"列 {target}.{column.name} 不在已采集的表结构中")
    if reasons:
        raise ValidationFailed(list(dict.fromkeys(reasons)))
