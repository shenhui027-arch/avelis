"""确定性编译与校验：SQL 只由已发布指标版本 + 已校验的意图生成，再独立解析校验一遍才执行。

校验不靠提示词、不靠正则：重新解析实际 SQL 的语法树，检查语句类型、引用的表与列、关联路径、LIMIT。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, ParseError, UnsupportedError

from avelis.ask.intent import period_column
from avelis.engines.time_sql import period_expression, time_conditions
from avelis.governance.errors import Invalid
from avelis.governance.metric_sql import BASE_ALIAS, column_ref, literal, parse_snippet, table_expr
from avelis.governance.metrics import encoded_time_type
from avelis.i18n import Msg
from avelis.lineage.sql import SqlDialect

FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command, exp.Into,
             exp.Merge, exp.TruncateTable, exp.Set, exp.Use)
TIME_GRAINS = ("day", "week", "month", "quarter", "year")


@dataclass
class CompiledQuery:
    sql: str
    tables: list[str]
    columns: list[str]
    plan: dict[str, Any] = field(default_factory=dict)


def _dialect(dialect: SqlDialect | str) -> SqlDialect:
    """接受数据源的解析方言；单元测试里也可以直接给 SQLGlot 方言名（表名照写 Catalog 段）。"""
    return dialect if isinstance(dialect, SqlDialect) else SqlDialect(name=dialect, default_catalog="")


class ValidationFailed(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("；".join(reasons))
        self.reasons = reasons


def compile_metric_query(*, version, aliases: dict[str, Any], steps: list[dict[str, Any]], intent, max_rows: int,
                         time_type: str, dialect: SqlDialect | str, reserved_names: tuple[str, ...] = (),
                         bucket_grain: str | None = None) -> CompiledQuery:
    """dialect：执行该查询的数据源的解析方言（SQLGlot 方言名 + 表名是否写 Catalog 段）。
    reserved_names：结果中还会出现的列名（如指标显示名），时间段列避开它们。
    bucket_grain：跨时间合成的内部取数粒度；内部桶与用户期间共同分组，避免跨月的周桶误归期间。"""
    sql_dialect = _dialect(dialect)
    dialect = sql_dialect.name
    names = set(aliases)

    def table(qualified: str, alias: str) -> exp.Table:
        return table_expr(qualified, alias, with_catalog=sql_dialect.catalog_in_sql)

    def col(text: str) -> exp.Expression:
        return parse_snippet(text, dialect=dialect, label="列", aliases=names, require_aggregate=False).expression

    dims = {d["name"]: d for d in version.dimensions or []}
    select = exp.Select()
    group_cols = []
    time_grain = getattr(intent, "time_grain", None)
    data_grain = getattr(version, "data_grain", None) or "day"
    if data_grain not in TIME_GRAINS:
        raise Invalid(Msg("未知数据粒度 {data_grain}", data_grain=data_grain))
    for grain in (time_grain, bucket_grain):
        if grain and (grain not in TIME_GRAINS or TIME_GRAINS.index(grain) < TIME_GRAINS.index(data_grain)):
            raise Invalid(Msg("时间分组粒度 {grain} 不能比数据粒度 {data_grain} 更细",
                              grain=grain, data_grain=data_grain))
    t_alias, t_col = column_ref(version.time_column, dialect=dialect, label="时间字段", aliases=names)
    time_column = exp.column(t_col, table=t_alias)
    if time_grain:  # 时间段列：日期按方言格式化；编码键只做确定性的整数除法或字符串截取
        period = _period_expression(version, time_column.copy(), time_grain, time_type, dialect)
        select = select.select(exp.alias_(period, period_column(intent.group_by, reserved_names)))
        group_cols.append(period.copy())
    for name in intent.group_by:
        select = select.select(exp.alias_(col(dims[name]["column"]), name))
        group_cols.append(col(dims[name]["column"]))
    bucket_column = None
    if bucket_grain:
        taken = {*intent.group_by, *reserved_names, "value", period_column(intent.group_by, reserved_names)}
        bucket_column = "_time_bucket"
        i = 1
        while bucket_column in taken:
            bucket_column = f"_time_bucket_{i}"
            i += 1
        bucket = _period_expression(version, time_column.copy(), bucket_grain, time_type, dialect)
        select = select.select(exp.alias_(bucket, bucket_column))
        group_cols.append(bucket.copy())
    measure = parse_snippet(version.expression, dialect=dialect, label="表达式", aliases=names,
                            require_aggregate=True).expression
    select = select.select(exp.alias_(measure, "value"))

    base = aliases[BASE_ALIAS]
    select = select.from_(table(_qualified(base), BASE_ALIAS))
    for step in steps:
        path = step["path"]
        right = step["right"]
        if step["pre_aggregate"]:
            inner = exp.Select()
            for c in path.right_columns:
                inner = inner.select(exp.column(c))
            inner = inner.select(exp.alias_(exp.Count(this=exp.Star()), "row_count"))
            keys = [exp.column(c) for c in path.right_columns]
            inner = inner.from_(table(_qualified(right), "x")).group_by(*keys)
            source = exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(step["alias"])))
        else:
            source = table(_qualified(right), step["alias"])
        condition = exp.and_(*[
            exp.EQ(this=exp.column(lc, table=step["from"]), expression=exp.column(rc, table=step["alias"]))
            for lc, rc in zip(path.left_columns, path.right_columns)])
        select = select.join(source, on=condition, join_type="inner")

    conditions: list[exp.Expression] = []
    time_type = time_type.lower()
    time_format = getattr(version, "time_format", None)
    if time_format:
        kind = encoded_time_type(time_type)
        if kind is None or (time_format == "yyyy-mm-dd" and kind != "string"):
            raise Invalid("编码日期键的列类型与 time_format 不匹配")
        if time_format == "yyyymm" and (intent.start_date.day != 1
                                        or (intent.end_date + timedelta(days=1)).day != 1):
            raise Invalid("数据按月编码，窗口必须是整月")
        formats = {"yyyymmdd": "%Y%m%d", "yyyy-mm-dd": "%Y-%m-%d", "yyyymm": "%Y%m"}
        if time_format not in formats:
            raise Invalid(Msg("未知时间编码格式 {time_format}", time_format=time_format))
        low, high = (d.strftime(formats[time_format]) for d in (intent.start_date, intent.end_date))
        bounds = [exp.Literal.number(v) if kind == "integer" else exp.Literal.string(v) for v in (low, high)]
        conditions.append(exp.Between(this=time_column, low=bounds[0], high=bounds[1]))
    else:
        conditions.extend(time_conditions(time_column, intent.start_date, intent.end_date, data_type=time_type,
                                           business_timezone=getattr(version, "timezone", None), dialect=sql_dialect))
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
        if getattr(intent, "order", None) and not bucket_grain:  # 内部桶须完整取回，合成后才能排名
            select = select.order_by(exp.Ordered(this=exp.column("value"), desc=intent.order == "desc"),
                                     *group_cols)
        else:
            select = select.order_by(*group_cols)
    limit = None if bucket_grain else getattr(intent, "limit", None)
    # 不限行数、或请求的行数超过系统上限时多取 1 行：连接器截到 max_rows 并据此标记截断，“刚好超出上限”也能识别。
    # 请求的行数不超过上限时按请求取（结果是用户要的前 N 行，不是被系统截断）
    select = select.limit(limit if limit and limit <= max_rows else max_rows + 1)
    if sql_dialect.national_strings:
        select = select.transform(_national)
    try:
        sql = select.sql(dialect=dialect, comments=False, unsupported_level=ErrorLevel.RAISE)
    except UnsupportedError as exc:
        raise Invalid(Msg("当前方言 {dialect} 不支持该时间分组", dialect=dialect)) from exc
    tables = sorted({_qualified(a) for a in aliases.values()})
    return CompiledQuery(sql=sql, tables=tables, columns=[*([period_column(intent.group_by, reserved_names)] if time_grain else []), *intent.group_by,
                                                  *([bucket_column] if bucket_column else []), "value"],
                         plan={"base": _qualified(base), "joins": [
                             {"alias": s["alias"], "path": s["path"].code, "pre_aggregate": s["pre_aggregate"]}
                             for s in steps], "group_by": intent.group_by, "time_grain": time_grain,
                             **({"bucket_column": bucket_column, "bucket_grain": bucket_grain} if bucket_column else {}),
                             "time_range": [str(intent.start_date), str(intent.end_date)],
                             "filters": [f.model_dump() for f in intent.filters]})


def _period_expression(version, column: exp.Expression, grain: str, time_type: str,
                       dialect: str) -> exp.Expression:
    """期间格式：日/周/季为 YYYY-MM-DD（周一/季初），月为 YYYY-MM，年为 YYYY。"""
    time_format = getattr(version, "time_format", None)
    if time_format:
        if grain not in ("day", "month") or (time_format == "yyyymm" and grain != "month"):
            raise Invalid("编码日期键只支持 day 与 month 分组，按月编码不能按日分组")
        kind = encoded_time_type(time_type)
        if kind is None or (time_format == "yyyy-mm-dd" and kind != "string"):
            raise Invalid("编码日期键的列类型与 time_format 不匹配")
        if time_format == "yyyymmdd" and grain == "month" and kind == "integer":
            column = exp.IntDiv(this=column, expression=exp.Literal.number(100))
        text = exp.Cast(this=column, to=exp.DataType.build("VARCHAR")) if kind == "integer" else column
        if time_format == "yyyy-mm-dd":
            return text if grain == "day" else exp.Substring(this=text, start=exp.Literal.number(1),
                                                              length=exp.Literal.number(7))
        if time_format not in ("yyyymm", "yyyymmdd"):
            raise Invalid(Msg("未知时间编码格式 {time_format}", time_format=time_format))

        def part(start: int, length: int) -> exp.Expression:
            return exp.Substring(this=text.copy(), start=exp.Literal.number(start), length=exp.Literal.number(length))

        pieces = [part(1, 4), exp.Literal.string("-"), part(5, 2)]
        if grain == "day":
            pieces += [exp.Literal.string("-"), part(7, 2)]
        return exp.Concat(expressions=pieces, safe=True, coalesce=False)
    return period_expression(column, grain, data_type=time_type,
                             business_timezone=getattr(version, "timezone", None), dialect=dialect)


def _national(node: exp.Expression) -> exp.Expression:
    if isinstance(node, exp.Literal) and node.is_string:
        return exp.National(this=node.this)
    return node


def distinct_values_sql(qualified: str, column: str, *, limit: int, dialect: SqlDialect | str) -> str:
    """维度可选值：SELECT DISTINCT 列 FROM 表 WHERE 列 IS NOT NULL LIMIT n。"""
    d = _dialect(dialect)
    col = exp.column(column, table="t")
    return (exp.select(col).distinct().from_(table_expr(qualified, "t", with_catalog=d.catalog_in_sql))
            .where(exp.Not(this=exp.Is(this=col.copy(), expression=exp.Null()))).limit(limit)
            .sql(dialect=d.name, identify=True))


def duplicate_key_sql(qualified: str, columns: list[str], *, dialect: SqlDialect | str) -> str:
    """粒度核验：按键分组找重复，取一条样例。"""
    d = _dialect(dialect)
    dialect = d.name
    keys = [exp.column(c, table="t") for c in columns]
    return (exp.select(*keys, exp.alias_(exp.Count(this=exp.Star()), "n"))
            .from_(table_expr(qualified, "t", with_catalog=d.catalog_in_sql))
            .group_by(*[k.copy() for k in keys]).having(exp.GT(this=exp.Count(this=exp.Star()),
                                                                 expression=exp.Literal.number(1)))
            .limit(1).sql(dialect=dialect, identify=True))


def _qualified(asset) -> str:
    return f"{asset.catalog_name}.{asset.db_name}.{asset.name}"


def validate_sql(sql: str, *, dialect: SqlDialect | str, allowed_tables: set[str], schema: dict[str, set[str]],
                 max_rows: int) -> None:
    """对实际要执行的 SQL 做确定性校验。schema：表 → 允许的列（表名均为 catalog.db.name 三段）。"""
    sql_dialect = _dialect(dialect)
    dialect = sql_dialect.name
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
    limit = root.args.get("limit")  # LIMIT n、TOP n 或 FETCH FIRST n ROWS ONLY（Oracle）
    if limit is None:
        reasons.append("缺少 LIMIT")
    else:
        count = limit.args.get("count") if isinstance(limit, exp.Fetch) else limit.expression
        try:
            if int(count.name) > max_rows + 1:  # 多出的 1 行只用于判断截断，不返回
                reasons.append(f"LIMIT 超过上限 {max_rows}")
        except (ValueError, AttributeError):
            reasons.append("LIMIT 不是常数")
    aliases: dict[str, str] = {}
    for table in root.find_all(exp.Table):
        catalog = table.catalog or ("" if sql_dialect.catalog_in_sql else sql_dialect.default_catalog)
        name = ".".join(p for p in (catalog, table.db, table.name) if p)
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
