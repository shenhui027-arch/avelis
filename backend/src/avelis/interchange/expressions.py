"""Keep fixed metric filters executable in consumers without Avelis extensions."""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.normalize import normalize
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from avelis.i18n import gettext

_AGGREGATES = (exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Count)
_UNSAFE = (exp.Query, exp.Window, exp.WithinGroup, exp.Order, exp.Filter, exp.IgnoreNulls, exp.RespectNulls)


def physical_column_name(identifier: exp.Expression, names: set[str], dialect: str) -> str | None:
    """Resolve SQL identifiers against physical names using the source dialect's case rules."""
    sql_dialect = sqlglot.Dialect.get_or_raise(dialect)
    normalized = sql_dialect.normalize_identifier(identifier.copy()).name
    return next(
        (
            name
            for name in names
            if sql_dialect.normalize_identifier(exp.to_identifier(name, quoted=True)).name == normalized
        ),
        None,
    )


def _parse(text: str, dialect: str) -> exp.Expression:
    statements = sqlglot.parse(text, read=dialect)
    if len(statements) != 1 or not isinstance(statements[0], exp.Condition):
        raise ValueError("Expected one SQL expression")
    return statements[0]


def fold_filters(expression: str, filters: str | None, dialect: str) -> str:
    """Fold a row predicate into supported aggregate arguments, or refuse it."""
    if not filters:
        return expression
    try:
        node, predicate = _parse(expression, dialect), _parse(filters, dialect)
    except (SqlglotError, ValueError) as error:
        raise ValueError(gettext("固定过滤条件无法安全折入核心表达式：SQL 表达式无法解析")) from error
    if any(isinstance(part, exp.Anonymous) for tree in (node, predicate) for part in tree.walk()):
        raise ValueError(gettext("固定过滤条件无法安全折入核心表达式：无法确认自定义函数是否为聚合"))
    if any(isinstance(part, _UNSAFE) for part in node.walk()) or any(
        isinstance(part, (*_UNSAFE, exp.AggFunc)) for part in predicate.walk()
    ):
        raise ValueError(gettext("固定过滤条件无法安全折入核心表达式：不支持窗口、有序聚合、聚合过滤或子查询"))
    aggregates = list(node.find_all(exp.AggFunc))
    if not aggregates or any(type(aggregate) not in _AGGREGATES for aggregate in aggregates):
        raise ValueError(gettext("固定过滤条件无法安全折入核心表达式：仅支持 SUM、AVG、MIN、MAX、COUNT 聚合"))
    if any(aggregate.find_ancestor(exp.AggFunc) is not None for aggregate in aggregates):
        raise ValueError(gettext("固定过滤条件无法安全折入核心表达式：不支持嵌套聚合"))
    if any(column.find_ancestor(exp.AggFunc) is None for column in node.find_all(exp.Column)):
        raise ValueError(gettext("固定过滤条件无法安全折入核心表达式：聚合外引用了源列"))
    for aggregate in aggregates:
        argument = aggregate.this
        if (
            argument is None
            or any(value for key, value in aggregate.args.items() if key not in {"this", "big_int"})
            or (isinstance(argument, exp.Distinct) and len(argument.expressions) != 1)
        ):
            raise ValueError(gettext("固定过滤条件无法安全折入核心表达式：聚合必须只有一个参数"))
        value = argument.expressions[0] if isinstance(argument, exp.Distinct) else argument
        if any(isinstance(part, exp.Star) for part in value.walk()):
            if not isinstance(aggregate, exp.Count) or not isinstance(argument, exp.Star):
                raise ValueError(gettext("固定过滤条件无法安全折入核心表达式：仅支持 COUNT(*) 的星号参数"))
            value = exp.Literal.number(1)
        conditional = exp.Case(ifs=[exp.If(this=predicate.copy(), true=value.copy())])
        if isinstance(argument, exp.Distinct):
            aggregate.set("this", exp.Distinct(expressions=[conditional]))
        else:
            aggregate.set("this", conditional)
    return node.sql(dialect=dialect, unsupported_level=sqlglot.ErrorLevel.RAISE)


def equivalent_expressions(left: str, right: str, dialect: str) -> bool:
    """Compare parsed SQL after identifier and redundant-parenthesis normalization."""

    def canonical(text: str) -> exp.Expression:
        node = normalize(normalize_identifiers(_parse(text, dialect), dialect=dialect))
        for column in node.find_all(exp.Column):
            if not column.table and not column.is_star:
                column.set("table", exp.to_identifier("b"))
        for identifier in node.find_all(exp.Identifier):
            identifier.set("quoted", True)
        for parenthesis in reversed(list(node.find_all(exp.Paren))):
            if parenthesis is node:
                node = parenthesis.this
            else:
                parenthesis.replace(parenthesis.this)
        return node

    try:
        return canonical(left) == canonical(right)
    except (SqlglotError, ValueError):
        return False
