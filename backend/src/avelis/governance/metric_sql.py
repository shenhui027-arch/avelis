"""指标定义的 SQL 片段：校验与确定性编译共用。

指标版本的表达式、过滤条件只能引用别名列：b 为基础资产，关联资产用关联步骤的别名；
未写别名的列视为基础资产的列。只允许白名单内的函数与运算，不允许子查询、窗口、星号（COUNT(*) 除外）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from avelis.governance.errors import Invalid

BASE_ALIAS = "b"
ADDITIVE = (exp.Sum, exp.Count, exp.Avg)
ALLOWED_FUNCS = (exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max, exp.Coalesce, exp.If, exp.Case, exp.Abs,
                 exp.Round, exp.Cast, exp.Distinct, exp.Nullif)
ALLOWED_NODES = (exp.Column, exp.Identifier, exp.Literal, exp.Paren, exp.Binary, exp.Neg, exp.Not, exp.Null,
                 exp.Boolean, exp.DataType, exp.In, exp.Between, exp.Is, exp.Tuple, exp.Star, exp.Unary,
                 exp.When, exp.Interval, exp.Var, exp.DataTypeParam, exp.Like, exp.ILike, exp.Bracket)


@dataclass
class ParsedSnippet:
    expression: exp.Expression
    columns: list[tuple[str, str]] = field(default_factory=list)  # (alias, column)


def parse_snippet(text: str, *, dialect: str, label: str, aliases: set[str],
                  require_aggregate: bool | None) -> ParsedSnippet:
    try:
        wrapper = sqlglot.parse_one(f"SELECT {text}", read=dialect)
    except ParseError as exc:
        raise Invalid(f"{label}无法解析：{text}", error=str(exc)[:300]) from exc
    extra = [k for k, v in wrapper.args.items() if v and k != "expressions"]
    if not isinstance(wrapper, exp.Select) or len(wrapper.selects) != 1 or extra:
        raise Invalid(f"{label}只能是单个表达式：{text}")
    node = wrapper.selects[0]
    if isinstance(node, exp.Alias):
        raise Invalid(f"{label}不能带别名：{text}")
    for sub in node.walk():
        if isinstance(sub, (exp.Subquery, exp.Select, exp.Window)):
            raise Invalid(f"{label}不允许子查询或窗口函数：{text}")
        if isinstance(sub, exp.Func) and not isinstance(sub, ALLOWED_FUNCS):
            raise Invalid(f"{label}使用了不允许的函数 {sub.sql_name()}：{text}")
        if not isinstance(sub, ALLOWED_FUNCS + ALLOWED_NODES) and not isinstance(sub, exp.Func):
            raise Invalid(f"{label}包含不支持的语法 {type(sub).__name__}：{text}")
        if isinstance(sub, exp.Star) and not isinstance(sub.parent, exp.Count):
            raise Invalid(f"{label}只允许在 COUNT(*) 中使用星号：{text}")
    has_agg = node.find(exp.AggFunc) is not None
    if require_aggregate is True and not has_agg:
        raise Invalid(f"{label}必须是聚合表达式（SUM / COUNT / AVG / MIN / MAX）：{text}")
    if require_aggregate is False and has_agg:
        raise Invalid(f"{label}不能包含聚合：{text}")
    columns = []
    for column in node.find_all(exp.Column):
        alias = column.table or BASE_ALIAS
        if alias not in aliases:
            raise Invalid(f"{label}引用了未定义的别名 {alias}：{text}")
        if column.args.get("db") or column.args.get("catalog"):
            raise Invalid(f"{label}只能用“别名.列”引用：{text}")
        column.set("table", exp.to_identifier(alias))
        columns.append((alias, column.name))
    return ParsedSnippet(node, columns)


def column_ref(text: str, *, dialect: str, label: str, aliases: set[str]) -> tuple[str, str]:
    snippet = parse_snippet(text, dialect=dialect, label=label, aliases=aliases, require_aggregate=False)
    if not isinstance(snippet.expression, exp.Column):
        raise Invalid(f"{label}必须是单个列：{text}")
    return snippet.columns[0]


def is_additive(expression: exp.Expression) -> bool:
    """可加度量：SUM、COUNT（非 DISTINCT）、AVG 在放大的关联之后都会算错。"""
    for agg in expression.find_all(exp.AggFunc):
        if isinstance(agg, exp.Count) and isinstance(agg.this, exp.Distinct):
            continue
        if isinstance(agg, ADDITIVE):
            return True
    return False


def table_expr(qualified: str, alias: str, *, with_catalog: bool = True) -> exp.Table:
    catalog, db, name = qualified.split(".")
    return exp.Table(this=exp.to_identifier(name), db=exp.to_identifier(db),
                     catalog=exp.to_identifier(catalog) if with_catalog else None,
                     alias=exp.TableAlias(this=exp.to_identifier(alias)))


def literal(value) -> exp.Expression:
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)):
        return exp.Literal.number(value)
    if isinstance(value, date):
        return exp.Literal.string(value.isoformat())
    return exp.Literal.string(str(value))
