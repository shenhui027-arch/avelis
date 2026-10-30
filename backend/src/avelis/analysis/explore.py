"""探索查询的校验（ADR 0009 第 6 点）：允许清单，凡不确定的一律拒绝；执行的是由校验后的 AST 重新生成的 SQL。

现有的 ask.compiler.validate_sql 是禁止清单、列检查有跳过路径，这里不复用它。本模块是纯函数，不连数据库：
- 模型一律用 Avelis 标准 SQL 书写：PostgreSQL 写法的受限子集，按 sqlglot 的 postgres 方言解析（STANDARD_DIALECT），
  标准函数只有 STANDARD_FUNCTIONS 这张小清单；不让模型凭自己的知识猜目标引擎的方言。标识符按 PostgreSQL 规则：
  不加引号的折成小写，加双引号的按原样匹配（有大写字母的列名要加双引号，写法与 describe_asset 返回的一致）；
- 引擎能力档案（ExploreProfile）：目标方言、表源规则、执行参数，以及标准函数转换到本引擎后的节点（复核用）；
  没有档案的引擎不提供 query_sql。第一版只有 StarRocks、PostgreSQL；
- table_refs()：解析标准 SQL，列出物理表引用（数据源由调用方按资产确定，见 exploration.route）；
- validate()：解析（标准方言）→ 语法子集 → 静态规模 → 表源绑定（交给 resolver，按资产可见性与物理类型判断）
  → 按查询块作用域绑定列 → 关联条件 → 输出列的证据资格与量纲 → 生成目标方言的 SQL，按目标方言严格复核。
拒绝时抛 ExploreRejected，带稳定的错误码与拒绝阶段；这时还没有任何东西发到数据库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, OptimizeError, ParseError, TokenError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope
from sqlglot.schema import MappingSchema

SQLGLOT_VERSION = sqlglot.__version__
MAX_SQL_LENGTH = 5000
MAX_NODES = 600
MAX_DEPTH = 40
MAX_IN_LIST = 200
MAX_ORDER_KEYS = 10
MAX_JOINS = 4
ALLOWED_CONSTANTS = {Decimal(1), Decimal(100), Decimal(1000), Decimal(10000), Decimal(100000000)}
# 原来禁止清单里的函数：作为额外一道防线，名字出现在任何函数节点上都拒绝
BANNED_FUNCTIONS = {"sleep", "pg_sleep", "pg_sleep_for", "pg_sleep_until", "benchmark", "load_file", "pg_read_file",
                    "pg_read_binary_file", "pg_ls_dir", "lo_import", "lo_export", "dblink", "url", "file", "files", "s3",
                    "hdfs", "remote", "mysql", "jdbc", "generate_series", "copy", "pg_terminate_backend",
                    "pg_cancel_backend", "set_config", "current_setting"}

AGGREGATES = {exp.Sum: "SUM", exp.Count: "COUNT", exp.Avg: "AVG", exp.Min: "MIN", exp.Max: "MAX"}
# Avelis 标准 SQL 的源方言：sqlglot 上游要求明确源方言，默认方言 "" 不在支持范围
# （https://github.com/tobymao/sqlglot/issues/8845）。标准函数本来就是 PostgreSQL 的写法，sqlglot 对它的支持与测试最完善。
# 本模块里所有的解析、qualify 与 .sql() 都显式传方言（tests/unit/test_sqlglot_usage.py 防回流）。
STANDARD_DIALECT = "postgres"
# 标准函数（模型能用的全部函数）：类别与参数个数。每个都在 StarRocks、PostgreSQL 上有转换测试
# （tests/unit/test_analysis_explore.py::test_standard_functions_translate），没有测试的函数不进这张清单。
# 算术、CASE、CAST 是结构节点。日期加减只有一种写法：列 ± INTERVAL 'n' 单位（DATE_ADD 等函数不进清单）：
# 规范形式唯一。
STANDARD_FUNCTIONS: dict[type, tuple[str, int, int]] = {
    exp.Sum: ("聚合", 1, 1), exp.Count: ("聚合", 0, 1), exp.Avg: ("聚合", 1, 1), exp.Min: ("聚合", 1, 1),
    exp.Max: ("聚合", 1, 1),
    exp.Coalesce: ("条件", 2, 4), exp.Nullif: ("条件", 2, 2),
    exp.TimestampTrunc: ("日期", 2, 2),  # DATE_TRUNC('month', 列)：postgres 方言解析为 TimestampTrunc
}
DATE_UNITS = {"DAY", "WEEK", "MONTH", "QUARTER", "YEAR"}  # 日期截断与 INTERVAL 允许的单位
# 结构节点的允许清单（函数另外按档案核对）
STRUCTURE = {
    exp.Select, exp.From, exp.Join, exp.Where, exp.Group, exp.Having, exp.Order, exp.Ordered, exp.Limit, exp.Column,
    exp.Identifier, exp.Table, exp.TableAlias, exp.Alias, exp.Literal, exp.Null, exp.Boolean, exp.Paren, exp.And,
    exp.Or, exp.Not, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Is, exp.In, exp.Between, exp.Like,
    exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Neg, exp.Case, exp.Cast, exp.DataType, exp.DataTypeParam, exp.CTE,
    exp.With, exp.Subquery, exp.Distinct, exp.Var, exp.Interval,
}
SELECT_ARGS = {"expressions", "from_", "joins", "where", "group", "having", "order", "limit", "distinct", "with_"}
CAST_TYPES = {exp.DataType.Type.INT, exp.DataType.Type.BIGINT, exp.DataType.Type.DECIMAL, exp.DataType.Type.DOUBLE,
              exp.DataType.Type.VARCHAR, exp.DataType.Type.DATE, exp.DataType.Type.TIMESTAMP,
              exp.DataType.Type.DATETIME}  # DATETIME：标准 TIMESTAMP 在 StarRocks 上的写法（复核时出现）
NUMERIC_TYPES = ("int", "decimal", "numeric", "double", "float", "real", "bigint", "smallint", "tinyint", "largeint",
                 "number", "money")


class ExploreRejected(Exception):
    """探索查询被拒绝：stage 为拒绝阶段（parse / syntax / size / binding / column / join / evidence / regenerate），
    code 为稳定的错误码，hints 为给模型的改正提示（例如当前用户可见的可用列）。没有任何东西发到数据库。"""

    def __init__(self, stage: str, code: str, message: str, hints: list[str] | None = None):
        super().__init__(message)
        self.stage, self.code, self.message, self.hints = stage, code, message, hints or []

    def view(self) -> dict[str, Any]:
        return {"error": self.message, "stage": self.stage, "code": self.code, "sent_to_database": False,
                **({"hints": self.hints} if self.hints else {})}


@dataclass(frozen=True)
class BoundColumn:
    name: str
    data_type: str
    comment: str | None = None


@dataclass(frozen=True)
class BoundTable:
    """表源绑定：数据源 + 资产 + 资产版本 + 物理身份；生成 SQL 用系统给出的精确限定名。"""
    asset_id: str
    asset_version_id: str
    physical_id: str | None
    catalog: str
    db: str
    name: str
    kind: str
    columns: tuple[BoundColumn, ...]
    use_only: bool = False  # PostgreSQL：FROM ONLY，不扫描继承子表
    definition: str | None = None  # 视图：绑定版本的定义原文（执行前与当前定义比较）
    upstream: tuple[tuple[str, str | None], ...] = ()  # 视图：上游闭包（资产编号，绑定的版本编号），执行前、交付前一起复查

    @property
    def qualified(self) -> str:
        return f"{self.catalog}.{self.db}.{self.name}"


Resolver = Callable[[str | None, str | None, str], BoundTable]


@dataclass(frozen=True)
class ExploreProfile:
    """一个引擎（按版本）的探索能力档案：目标方言（标准 SQL 转换成它再执行）、表源规则、执行参数；
    functions 是标准函数转换到本方言后、按本方言解析出的节点（只用于复核重新生成的 SQL）。"""
    engine: str
    version: str
    dialect: str
    catalog_in_sql: bool
    functions: dict[type, tuple[str, int, int]] = field(default_factory=lambda: dict(STANDARD_FUNCTIONS))
    settings: dict[str, Any] = field(default_factory=dict)  # 执行参数，见连接器 execute_explore


# 标准函数转换到目标方言后，按目标方言解析出的节点与标准 SQL 相同
_TRANSLATED = dict(STANDARD_FUNCTIONS)
PROFILES: dict[str, ExploreProfile] = {
    "starrocks": ExploreProfile("starrocks", "2", "starrocks", True, functions=dict(_TRANSLATED),
                                settings={"query_timeout": "按统一截止时间", "query_mem_limit": 2 * 1024 ** 3,
                                          "pipeline_dop": 1}),
    "postgres": ExploreProfile("postgres", "2", "postgres", True, functions=dict(_TRANSLATED),
                               settings={"statement_timeout": "按统一截止时间", "lock_timeout": "2s", "work_mem": "16MB",
                                         "max_parallel_workers_per_gather": 0}),
}


def profile_for(engine_kind: str) -> ExploreProfile | None:
    return PROFILES.get(engine_kind)


@dataclass
class OutputColumn:
    name: str
    expression: str
    deps: list[str]
    kind: str  # level | count | ratio | unknown | text
    eligible: bool  # 可作为事实、计算类数字的来源
    label_ok: bool  # LabelRef 只能取维度列的直接投影
    assumption: bool


@dataclass
class Validated:
    sql: str  # 由标准 SQL 生成的目标方言 SQL，按目标方言严格复核过（实际执行的版本）
    tables: list[BoundTable]
    columns: list[OutputColumn]
    limit: int
    dialect: str  # 目标方言
    profile: str
    sqlglot: str = SQLGLOT_VERSION
    source_dialect: str = "avelis-standard"  # 模型书写的方言


# --- 主流程 ----------------------------------------------------------------------------

def validate(sql: str, profile: ExploreProfile, resolver: Resolver, *, max_rows: int) -> Validated:
    if not sql or not sql.strip():
        raise ExploreRejected("parse", "empty", "SQL 为空")
    if len(sql) > MAX_SQL_LENGTH:
        raise ExploreRejected("size", "sql_too_long", f"SQL 超过 {MAX_SQL_LENGTH} 个字符")
    ast = _parse(sql)
    _check_syntax(ast, STANDARD_FUNCTIONS, "标准 SQL")
    _check_size(ast)
    ctes = _cte_names(ast)
    bound = _bind_tables(ast, ctes, resolver)
    qualified = _qualify(ast, bound, profile)
    _check_aliases(qualified)
    _check_joins(qualified)
    columns = _outputs(qualified)
    final, limit = _regenerate(qualified, bound, profile, max_rows)
    return Validated(sql=final, tables=list({t.asset_id: t for t in bound.values()}.values()), columns=columns,
                     limit=limit, dialect=profile.dialect, profile=f"{profile.engine}/{profile.version}")


def table_refs(sql: str) -> list[tuple[str | None, str | None, str]]:
    """标准 SQL 里的物理表引用（catalog、库、表），不含 CTE 引用；数据源由调用方按这些资产确定。"""
    if not sql or not sql.strip():
        raise ExploreRejected("parse", "empty", "SQL 为空")
    if len(sql) > MAX_SQL_LENGTH:
        raise ExploreRejected("size", "sql_too_long", f"SQL 超过 {MAX_SQL_LENGTH} 个字符")
    out = []
    for table in _parse(sql).find_all(exp.Table):
        if isinstance(table.this, exp.Identifier) and not _is_cte_ref(table):
            catalog, db = table.args.get("catalog"), table.args.get("db")
            out.append((catalog.name if catalog else None, db.name if db else None, table.name))
    return list(dict.fromkeys(out))


def _parse(sql: str, dialect: str = STANDARD_DIALECT) -> exp.Expression:
    try:
        statements = sqlglot.parse(sql, read=dialect, error_level=ErrorLevel.RAISE)
    except (ParseError, TokenError) as e:
        raise ExploreRejected("parse", "parse_error", f"SQL 无法解析：{str(e).splitlines()[0][:160]}") from None
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise ExploreRejected("parse", "multiple_statements", "只允许一条语句")
    ast = statements[0]
    if not isinstance(ast, exp.Select):
        raise ExploreRejected("syntax", "not_select", f"只允许 SELECT 或 WITH … SELECT（收到 {type(ast).__name__}）")
    if dialect == STANDARD_DIALECT:
        # 标识符按 PostgreSQL 规则规范化：未加引号的折成小写，加了引号的原样保留。路由（table_refs）与绑定都用规范化后的名字
        ast = normalize_identifiers(ast, dialect=STANDARD_DIALECT)
        # 标准 SQL 的除法一律是数值除法（不按 PostgreSQL 的整数相除取整），各引擎结果一致：目标是 PostgreSQL 时
        # 生成 CAST(… AS DOUBLE PRECISION)，StarRocks 的 / 本来就是数值除法
        for div in ast.find_all(exp.Div):
            div.set("typed", False)
    return ast


def _check_syntax(ast: exp.Expression, functions: dict[type, tuple[str, int, int]], label: str) -> None:
    for node in ast.walk():
        if node.comments:
            raise ExploreRejected("syntax", "comment", "不允许注释（包括 hint 与执行注释）")
        if isinstance(node, exp.Star) and isinstance(node.parent, exp.Count):  # COUNT(*)
            continue
        name = (node.sql_name() if isinstance(node, exp.Func) else "").lower()
        if name in BANNED_FUNCTIONS or (isinstance(node, exp.Anonymous) and str(node.this).lower() in BANNED_FUNCTIONS):
            raise ExploreRejected("syntax", "banned_function", f"函数 {name or node.this} 不允许使用")
        kind = type(node)
        if isinstance(node, exp.If) and isinstance(node.parent, exp.Case):  # CASE 的 WHEN 分支
            continue
        if isinstance(node, exp.Func) and kind not in STRUCTURE:
            rule = functions.get(kind)
            if rule is None:
                name = node.this if isinstance(node, exp.Anonymous) else node.sql_name()
                raise ExploreRejected("syntax", "function_not_allowed",
                                      f"函数 {name} 不在{label}的允许清单里（标准函数：聚合、COALESCE、NULLIF、"
                                      f"DATE_TRUNC；日期加减写成 列 ± INTERVAL 'n' DAY）")
            _check_arity(node, rule)
            if isinstance(node, (exp.DateTrunc, exp.TimestampTrunc)) and _unit(node) not in DATE_UNITS:
                raise ExploreRejected("syntax", "date_unit", f"日期单位只能是 {'、'.join(sorted(DATE_UNITS))}")
            continue
        if kind not in STRUCTURE:
            raise ExploreRejected("syntax", "node_not_allowed", f"不支持的写法：{kind.__name__}")
        if isinstance(node, exp.Select):
            if node.args.get("from_") is None:
                raise ExploreRejected("syntax", "no_source", "每个查询块都必须有表源（FROM 已采集的表）")
            extra = [k for k, v in node.args.items() if v not in (None, [], False) and k not in SELECT_ARGS]
            if extra:
                raise ExploreRejected("syntax", "select_clause_not_allowed", f"不支持的子句：{'、'.join(extra)}")
            distinct = node.args.get("distinct")
            if distinct is not None and distinct.args.get("on"):
                raise ExploreRejected("syntax", "distinct_on", "不支持 DISTINCT ON")
            if any(isinstance(p, exp.Star) for p in node.expressions):
                raise ExploreRejected("syntax", "star", "不允许 * ，请写出需要的列")
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise ExploreRejected("syntax", "recursive", "不允许递归 CTE")
        if isinstance(node, exp.Subquery) and not isinstance(node.parent, (exp.From, exp.Join)):
            raise ExploreRejected("syntax", "scalar_subquery", "只允许在 FROM / JOIN 中使用派生表，不允许标量子查询与相关子查询")
        if isinstance(node, exp.In) and node.args.get("query"):
            raise ExploreRejected("syntax", "in_subquery", "不允许 IN (子查询)")
        if isinstance(node, exp.Table) and not isinstance(node.this, exp.Identifier):
            raise ExploreRejected("syntax", "table_function", "表源只能是普通表引用，不允许表函数")
        if isinstance(node, exp.Table) and (node.args.get("joins") or node.args.get("pivots") or node.args.get("laterals")
                                            or node.args.get("version") or node.args.get("sample")):
            raise ExploreRejected("syntax", "table_modifier", "表引用不允许附加子句")
        if isinstance(node, exp.Join):
            _check_join_shape(node)
        if isinstance(node, exp.Cast):
            to = node.args.get("to")
            if not isinstance(to, exp.DataType) or to.this not in CAST_TYPES:
                raise ExploreRejected("syntax", "cast_type",
                                      f"不支持转换成 {to.sql(dialect=STANDARD_DIALECT) if to else '未知类型'}")
        if isinstance(node, exp.Var) and not isinstance(node.parent, (exp.Interval, exp.DateTrunc, exp.TimestampTrunc)):
            raise ExploreRejected("syntax", "variable", "不允许变量")
        if isinstance(node, exp.Interval) and (_unit(node) not in DATE_UNITS
                                               or not isinstance(node.parent, (exp.Add, exp.Sub))):
            raise ExploreRejected("syntax", "date_unit",
                                  f"INTERVAL 只用于日期加减（列 ± INTERVAL 'n' 单位），单位只能是 {'、'.join(sorted(DATE_UNITS))}")
        if isinstance(node, exp.Limit) and not (isinstance(node.expression, exp.Literal)
                                                and node.expression.is_int):
            raise ExploreRejected("syntax", "limit", "LIMIT 只能是整数")


def _unit(node: exp.Expression) -> str:
    unit = node.args.get("unit")
    if isinstance(node, exp.Interval) and unit is None and isinstance(node.this, exp.Literal):  # INTERVAL '7 DAY'
        parts = str(node.this.this).split()
        return parts[1].upper().rstrip("S") if len(parts) == 2 else ""
    return (unit.name if isinstance(unit, (exp.Var, exp.Literal)) else str(unit or "")).upper().rstrip("S")


def _check_arity(node: exp.Expression, rule: tuple[str, int, int]) -> None:
    _, low, high = rule
    count = 0
    for value in node.args.values():
        if isinstance(value, exp.Expression):
            count += 1
        elif isinstance(value, list):
            count += len(value)
    if isinstance(node, exp.Count) and isinstance(node.this, exp.Star):
        count = 0
    if not low <= count <= high:
        raise ExploreRejected("syntax", "function_arity", f"函数 {node.sql_name()} 的参数个数不对（{count} 个）")


def _check_join_shape(join: exp.Join) -> None:
    if join.args.get("using") or join.args.get("method") or (join.args.get("kind") or "").upper() in ("CROSS", "NATURAL"):
        raise ExploreRejected("syntax", "join_form", "不允许 USING、NATURAL、CROSS JOIN")
    if join.args.get("on") is None:
        raise ExploreRejected("join", "cartesian", "JOIN 必须带等值 ON 条件（不允许笛卡尔积）")


def _check_size(ast: exp.Expression) -> None:
    nodes = list(ast.walk())
    if len(nodes) > MAX_NODES:
        raise ExploreRejected("size", "too_many_nodes", f"SQL 太复杂（超过 {MAX_NODES} 个语法节点）")
    if max(_depth(n) for n in nodes) > MAX_DEPTH:
        raise ExploreRejected("size", "too_deep", f"嵌套超过 {MAX_DEPTH} 层")
    for node in nodes:
        if isinstance(node, exp.In) and len(node.expressions) > MAX_IN_LIST:
            raise ExploreRejected("size", "in_list_too_long", f"IN 列表超过 {MAX_IN_LIST} 项")
        if isinstance(node, exp.Order) and len(node.expressions) > MAX_ORDER_KEYS:
            raise ExploreRejected("size", "too_many_order_keys", f"排序键超过 {MAX_ORDER_KEYS} 个")
    joins = sum(len(s.args.get("joins") or []) for s in ast.find_all(exp.Select))
    if joins > MAX_JOINS:
        raise ExploreRejected("size", "too_many_joins", f"关联超过 {MAX_JOINS} 个")


def _depth(node: exp.Expression) -> int:
    depth = 0
    while node.parent is not None:
        depth, node = depth + 1, node.parent
    return depth


def _cte_names(ast: exp.Expression) -> set[str]:
    names = [cte.alias_or_name for cte in ast.find_all(exp.CTE)]
    if len(names) != len(set(names)):
        raise ExploreRejected("syntax", "duplicate_cte", "CTE 名称重复")
    return set(names)


def _visible_ctes(table: exp.Table) -> set[str]:
    """按查询块作用域：表引用处可见的 CTE 名。非递归 CTE 只对同一 WITH 里排在它后面的 CTE 与主查询可见，
    在自己的定义里同名的引用指物理表（WITH t AS (SELECT … FROM t) 里的 t 是物理表）。"""
    visible: set[str] = set()
    child, node = table, table.parent
    while node is not None:
        if isinstance(node, exp.Select) and node.args.get("with_") is not None:
            ctes = node.args["with_"].expressions
            inside = next((i for i, cte in enumerate(ctes) if child is cte), None)
            visible |= {c.alias_or_name for c in (ctes[:inside] if inside is not None else ctes)}
        if isinstance(node, exp.With):  # 在某个 CTE 的定义里：保留这个 CTE，交给上面的 Select 按位置截取
            node = node.parent
            continue
        child, node = node, node.parent
    return visible


def _is_cte_ref(table: exp.Table) -> bool:
    return not table.args.get("db") and not table.args.get("catalog") and table.name in _visible_ctes(table)


def _bind_tables(ast: exp.Expression, ctes: set[str], resolver: Resolver) -> dict[int, BoundTable]:
    """每个物理表引用都经过 resolver 绑定到资产；只有在引用处可见的 CTE 名（按查询块作用域）才算 CTE 引用。"""
    bound: dict[int, BoundTable] = {}
    for table in ast.find_all(exp.Table):
        if _is_cte_ref(table):
            continue
        catalog = table.args.get("catalog")
        db = table.args.get("db")
        bound[id(table)] = resolver(catalog.name if catalog else None, db.name if db else None, table.name)
    return bound


def _is_numeric(data_type: str) -> bool:
    return any(t in (data_type or "").lower() for t in NUMERIC_TYPES)


def _qualify(ast: exp.Expression, bound: dict[int, BoundTable], profile: ExploreProfile) -> exp.Expression:
    """把表引用换成系统给出的精确限定名，再按查询块作用域限定、校验每一列（未采集的列、有歧义的列都拒绝）。"""
    tree = ast.copy()
    originals = list(ast.find_all(exp.Table))
    copies = list(tree.find_all(exp.Table))
    mapping: dict[str, dict] = {}
    for original, table in zip(originals, copies):
        target = bound.get(id(original))
        if target is None:
            continue
        table.set("catalog", exp.to_identifier(target.catalog, quoted=True))
        table.set("db", exp.to_identifier(target.db, quoted=True))
        table.set("this", exp.to_identifier(target.name, quoted=True))
        table.meta["avelis_bound"] = target
        mapping.setdefault(target.catalog, {}).setdefault(target.db, {})[target.name] = {
            c.name: ("DOUBLE" if _is_numeric(c.data_type) else "TEXT") for c in target.columns}
    try:
        schema = MappingSchema(mapping, dialect=STANDARD_DIALECT, normalize=False)
        return qualify(tree, dialect=STANDARD_DIALECT, schema=schema, validate_qualify_columns=True,
                       quote_identifiers=True, expand_stars=False, identify=True)
    except OptimizeError as e:
        tables = {b.asset_id: b for b in bound.values()}.values()
        hints = [f"{t.qualified}：" + "、".join(f"{c.name}" + (f"（{c.comment}）" if c.comment else "")
                                              for c in t.columns[:60]) for t in tables]
        # 只差大小写：标识符按 PostgreSQL 规则，不加引号的折成小写，有大写字母的列名要加双引号
        written = {i.name for i in ast.find_all(exp.Identifier) if not i.quoted}
        cased = sorted({c.name for t in tables for c in t.columns if c.name != c.name.lower()
                        and c.name.lower() in {w.lower() for w in written}})
        hints += [f'列名有大写字母时加双引号：写成 "{name}"' for name in cased]
        raise ExploreRejected("column", "column_unresolved", f"列无法唯一解析：{str(e).splitlines()[0][:160]}",
                              hints) from None


def _check_aliases(tree: exp.Expression) -> None:
    for select in tree.find_all(exp.Select):
        sources = [s.alias_or_name for s in [select.args.get("from_") and select.args["from_"].this,
                                             *[j.this for j in select.args.get("joins") or []]] if s is not None]
        if len(sources) != len(set(sources)):
            raise ExploreRejected("column", "duplicate_alias", "同一查询块里的表别名重复")
        names = [p.alias_or_name for p in select.expressions]
        if len(names) != len(set(names)):
            raise ExploreRejected("column", "duplicate_output", f"输出列名重复：{'、'.join(names)}")


def _check_joins(tree: exp.Expression) -> None:
    """ON 只能是等值条件，且每个条件都把本次 JOIN 引入的表和前面已有的表连起来（否则实际是笛卡尔积）。"""
    for select in tree.find_all(exp.Select):
        source = select.args.get("from_")
        joined = {source.this.alias_or_name} if source is not None else set()
        for join in select.args.get("joins") or []:
            alias = join.this.alias_or_name
            for part in _conjuncts(join.args.get("on")):
                if not (isinstance(part, exp.EQ) and isinstance(part.this, exp.Column)
                        and isinstance(part.expression, exp.Column) and part.this.table != part.expression.table):
                    raise ExploreRejected("join", "non_equi_join", "JOIN 的 ON 只能是两侧列的等值条件（可用 AND 连接）")
                if {part.this.table, part.expression.table} - joined != {alias}:
                    raise ExploreRejected("join", "join_not_linked",
                                          f"JOIN {alias} 的 ON 条件必须把 {alias} 与前面已有的表连起来（否则是笛卡尔积）")
            joined.add(alias)


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


# --- 证据资格与量纲 ---------------------------------------------------------------------

@dataclass
class _Info:
    kind: str  # column | agg_level | count | ratio | arith | const | text | other
    deps: set[str] = field(default_factory=set)
    constants: set[Decimal] = field(default_factory=set)
    numeric: bool = True
    bad_constant: bool = False


def _outputs(tree: exp.Expression) -> list[OutputColumn]:
    root = build_scope(tree)
    out = []
    for projection in tree.expressions:
        info = _analyze(projection.unalias() if isinstance(projection, exp.Alias) else projection, root)
        has_data = bool(info.deps)
        assumption = not has_data or info.bad_constant or info.kind in ("const", "other")
        kind = {"column": "level" if info.numeric else "text", "agg_level": "level", "count": "count",
                "ratio": "ratio"}.get(info.kind, "unknown" if info.numeric else "text")
        direct = info.kind == "column"
        out.append(OutputColumn(
            name=projection.alias_or_name, expression=projection.sql(dialect=STANDARD_DIALECT), deps=sorted(info.deps),
            kind=kind,
            eligible=not assumption and info.numeric and info.kind in ("column", "agg_level", "count", "ratio", "arith"),
            label_ok=direct and not info.numeric, assumption=assumption and info.numeric))
    return out


def _analyze(node: exp.Expression, scope: Scope) -> _Info:
    if isinstance(node, exp.Paren):
        return _analyze(node.this, scope)
    if isinstance(node, exp.Column):
        return _column(node, scope)
    if isinstance(node, exp.Literal):
        if node.is_string:
            return _Info("const", numeric=False)
        value = _decimal(node.this)
        return _Info("const", constants={value} if value is not None else set(),
                     bad_constant=value is None or value not in ALLOWED_CONSTANTS)
    if isinstance(node, exp.Neg):
        inner = _analyze(node.this, scope)
        return _Info("arith" if inner.deps else "const", inner.deps, inner.constants, inner.numeric, inner.bad_constant)
    if isinstance(node, exp.Count):
        arg = node.this.this if isinstance(node.this, exp.Distinct) else node.this
        deps = _scope_inputs(scope) if arg is None or isinstance(arg, exp.Star) else _deps(arg, scope)
        return _Info("count", deps)
    if isinstance(node, (exp.Sum, exp.Avg, exp.Min, exp.Max)):
        arg = node.this
        if isinstance(node, exp.Sum) and _conditional_count(arg):
            return _Info("count", _deps(arg, scope))
        inner = _analyze(arg, scope)
        if inner.kind == "column" and inner.numeric:
            return _Info("agg_level", inner.deps)
        if inner.kind == "arith" and not inner.bad_constant:
            return _Info("agg_level", inner.deps, inner.constants)
        return _Info("other", inner.deps, inner.constants, inner.numeric, True)
    if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
        left, right = _analyze(node.this, scope), _analyze(node.expression, scope)
        bad = left.bad_constant or right.bad_constant or not (left.numeric and right.numeric)
        aggs = ("agg_level", "count", "ratio")
        if isinstance(node, exp.Div) and left.kind in aggs and right.kind in aggs:
            kind = "ratio"
        else:
            kind = "arith"
        return _Info(kind, left.deps | right.deps, left.constants | right.constants, True, bad)
    if isinstance(node, exp.Cast):
        inner = _analyze(node.this, scope)
        return _Info("other" if inner.kind != "column" else "arith", inner.deps, inner.constants,
                     inner.numeric and node.args["to"].is_type(*exp.DataType.NUMERIC_TYPES), inner.bad_constant)
    deps = _deps(node, scope)
    return _Info("other", deps, numeric=not isinstance(node, (exp.Lower, exp.Upper, exp.Trim, exp.Substring)),
                 bad_constant=True)


def _conditional_count(node: exp.Expression) -> bool:
    """SUM(CASE WHEN 条件 THEN 1 ELSE 0 END)：条件里的字面量只用作过滤，不进入数值。"""
    if not isinstance(node, exp.Case) or node.this is not None:
        return False
    ones = [w.args.get("true") for w in node.args.get("ifs") or []]
    default = node.args.get("default")
    return bool(ones) and all(isinstance(v, exp.Literal) and v.this == "1" for v in ones) and (
        default is None or (isinstance(default, exp.Literal) and default.this == "0"))


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _column(node: exp.Column, scope: Scope) -> _Info:
    source = scope.sources.get(node.table)
    if isinstance(source, exp.Table):
        bound: BoundTable | None = source.meta.get("avelis_bound")
        col = next((c for c in bound.columns if c.name == node.name), None) if bound else None
        return _Info("column", {f"{bound.qualified}.{node.name}" if bound else node.sql(dialect=STANDARD_DIALECT)},
                     numeric=bool(col and _is_numeric(col.data_type)))
    if isinstance(source, Scope):
        for projection in source.expression.expressions:
            if projection.alias_or_name == node.name:
                inner = projection.unalias() if isinstance(projection, exp.Alias) else projection
                return _analyze(inner, source)
    return _Info("other", {node.sql(dialect=STANDARD_DIALECT)}, bad_constant=True)


def _scope_inputs(scope: Scope) -> set[str]:
    """COUNT(*) 的依赖：当前作用域的真实输入（绑定的表，或派生表 / CTE 的输入）；没有真实表源时为空。"""
    out: set[str] = set()
    for source in scope.sources.values():
        if isinstance(source, exp.Table) and source.meta.get("avelis_bound") is not None:
            out.add(f"{source.meta['avelis_bound'].qualified}.*")
        elif isinstance(source, Scope):
            out |= _scope_inputs(source)
    return out


def _deps(node: exp.Expression, scope: Scope) -> set[str]:
    out: set[str] = set()
    for column in node.find_all(exp.Column):
        out |= _column(column, scope).deps
    return out


# --- 重新生成 ---------------------------------------------------------------------------

def _regenerate(tree: exp.Expression, bound: dict[int, BoundTable], profile: ExploreProfile,
                max_rows: int) -> tuple[str, int]:
    final = tree.copy()
    for table in final.find_all(exp.Table):
        target: BoundTable | None = table.meta.get("avelis_bound")
        if target is None and not _is_cte_ref(table):
            raise ExploreRejected("binding", "unbound_table",
                                  f"表 {table.sql(dialect=STANDARD_DIALECT)} 没有绑定到已采集的资产")
        if target is not None:
            if not profile.catalog_in_sql:
                table.set("catalog", None)
            if target.use_only:
                table.set("only", True)
    for node in final.walk():
        node.comments = None
    requested = final.args.get("limit")
    wanted = int(requested.expression.this) if requested is not None else None
    limit = wanted if wanted is not None and wanted <= max_rows else max_rows + 1  # 多取 1 行以准确标记截断
    final.set("limit", exp.Limit(expression=exp.Literal.number(limit)))
    sql = final.sql(dialect=profile.dialect)
    try:  # 生成后再严格解析、复核一次：结构必须一致
        again = sqlglot.parse(sql, read=profile.dialect, error_level=ErrorLevel.RAISE)
    except (ParseError, TokenError):
        raise ExploreRejected("regenerate", "regenerate_unstable", "重新生成的 SQL 无法再次解析") from None
    if len(again) != 1 or again[0].sql(dialect=profile.dialect) != sql:
        raise ExploreRejected("regenerate", "regenerate_unstable", "重新生成的 SQL 与校验结果不一致")
    _check_syntax(again[0], profile.functions, f" {profile.engine} 方言")
    return sql, limit
