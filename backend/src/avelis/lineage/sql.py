"""SQL 解析：规范化指纹、列级血缘、JOIN 与分组信息。基于 SQLGlot。

本模块与具体数据库无关：只认 SqlDialect（SQLGlot 方言名、默认 Catalog、引擎特有写法的规整函数），
由引擎适配器（avelis.engines）提供。新增数据库时不改这里。

v0.1 支持的 SQL 子集（视图定义、导入的 SELECT）：
  SELECT 投影与别名、聚合、WHERE / HAVING 过滤、GROUP BY、等值 JOIN（INNER / LEFT / RIGHT）、
  FROM 子查询、CTE、视图套视图（逐层解析）、DISTINCT、窗口函数（记为计算）。
不支持（明确报出范围与原因）：UNION 等集合运算的列级血缘、多语句、非 SELECT 语句、标量子查询投影、
  LATERAL / UNNEST 投影的列级来源。

列引用分两类：
  取值 value_direct / value_computed / value_aggregated —— 输出值由它计算而来；
  影响 filter / join_key / group_by —— 决定哪些行参与、如何分组，但值不会复制到输出。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope, traverse_scope
from sqlglot.schema import MappingSchema

from avelis.connectors.base import sha256_text

PARSER_VERSION = f"sqlglot-{sqlglot.__version__}/avelis-1"

VALUE_RANK = {"value_direct": 0, "value_computed": 1, "value_aggregated": 2}


@dataclass(frozen=True)
class SqlDialect:
    """解析所需的全部引擎差异。"""
    name: str  # SQLGlot 方言名
    default_catalog: str  # 未写 Catalog 的表补全为该值（库.表 两级命名的引擎也补一个固定 Catalog）
    normalize: Callable[[exp.Expression], exp.Expression] | None = None  # 引擎改写过的定义写法，解析前规整
    # 生成的 SQL 是否写 Catalog 段：库.表 两级命名、查询中不认 Catalog 的引擎（ClickHouse 等）为 False，
    # 表名写成 库.表，校验时再按 default_catalog 补齐
    catalog_in_sql: bool = True
    # 生成的 SQL 中字符串字面量写成 N'...'（SQL Server：NVARCHAR 列在非 UTF-8 排序规则下才能正确比较中文）
    national_strings: bool = False
    # DATE 类型带时间部分（Oracle）：按日期过滤时用 [当日 0 点, 次日 0 点) 区间，而不是 BETWEEN 两个日期
    date_has_time: bool = False

    def prepare(self, expression: exp.Expression) -> exp.Expression:
        return self.normalize(expression) if self.normalize else expression


@dataclass(frozen=True)
class ColumnRef:
    table: str  # catalog.db.name
    column: str


@dataclass
class ValueSource:
    ref: ColumnRef
    relation: str
    expression: str


@dataclass
class Influence:
    ref: ColumnRef
    relation: str  # filter | join_key | group_by
    expression: str


@dataclass
class JoinInfo:
    kind: str
    left: list[ColumnRef]
    right: list[ColumnRef]
    right_table: str | None
    left_tables: list[str]
    condition: str


@dataclass
class OutputColumn:
    name: str
    expression: str
    relation: str | None
    sources: list[ValueSource]


@dataclass
class ParsedQuery:
    status: str  # ok | partial | unsupported | failed
    reason: str | None
    canonical_sql: str | None
    canonical_hash: str
    tables: list[str] = field(default_factory=list)
    columns: list[OutputColumn] = field(default_factory=list)
    influences: list[Influence] = field(default_factory=list)
    joins: list[JoinInfo] = field(default_factory=list)
    group_outputs: list[str] | None = None
    distinct: bool = False
    passthrough: dict[str, Any] | None = None
    unresolved: list[str] = field(default_factory=list)

    def details(self) -> dict[str, Any]:
        return {
            "tables": self.tables,
            "joins": [{"kind": j.kind, "left": [[r.table, r.column] for r in j.left],
                       "right": [[r.table, r.column] for r in j.right], "right_table": j.right_table,
                       "left_tables": j.left_tables, "condition": j.condition} for j in self.joins],
            "group_outputs": self.group_outputs,
            "distinct": self.distinct,
            "passthrough": self.passthrough,
            "unresolved": self.unresolved,
            "columns": [{"name": c.name, "expression": c.expression, "relation": c.relation} for c in self.columns],
        }


class SqlNotSupported(Exception):
    pass


# --- 规范化 -----------------------------------------------------------------------

def strip_alias_prefixes(expression: exp.Expression) -> exp.Expression:
    """供引擎选用的规整函数。StarRocks 把别名列写成 `db`.`alias`.`col`（Catalog 表为 `catalog`.`db`.`alias`.`col`），
    SQLGlot 会把别名当表名。凡是表部分能在本层来源中找到的列，去掉库与 Catalog 前缀。"""
    for scope in traverse_scope(expression):
        for column in scope.columns:
            if column.table and column.table in scope.sources:
                column.set("db", None)
                column.set("catalog", None)
    return expression


def fold_identifiers(dialect_name: str) -> Callable[[exp.Expression], exp.Expression]:
    """供引擎选用的规整函数：按该引擎的规则折叠未加引号的标识符（Oracle 存为大写）。
    视图原文里写的 store.sales 在数据字典中是 STORE.SALES，表名与列名都要先折叠，才能对上已采集的资产。"""
    def fold(expression: exp.Expression) -> exp.Expression:
        return normalize_identifiers(expression, dialect=dialect_name)
    return fold


def _select_star(expression: exp.Expression) -> bool:
    """投影中残留的 * 或 t.*（未能按表结构展开）。COUNT(*) 等聚合参数里的 * 不算。"""
    for select in expression.find_all(exp.Select):
        for projection in select.expressions:
            if isinstance(projection, exp.Star) or (isinstance(projection, exp.Column)
                                                    and isinstance(projection.this, exp.Star)):
                return True
    return False


def default_tables(expression: exp.Expression, default_catalog: str, default_db: str | None) -> list[str]:
    """补全表的 Catalog 与库，返回引用到的实体表（不含 CTE 名）。"""
    cte_names = {cte.alias_or_name for cte in expression.find_all(exp.CTE)}
    tables = []
    for table in expression.find_all(exp.Table):
        if not table.args.get("db") and table.name in cte_names:
            continue
        if not table.args.get("db"):
            if default_db is None:
                continue
            table.set("db", exp.to_identifier(default_db))
        if not table.args.get("catalog"):
            table.set("catalog", exp.to_identifier(default_catalog))
        tables.append(qualified_name(table))
    return sorted(set(tables))


def qualified_name(table: exp.Table) -> str:
    return ".".join(p for p in (table.catalog, table.db, table.name) if p)


def canonical_hash_of(sql: str, dialect: SqlDialect) -> tuple[str | None, str]:
    """只改格式（空白、大小写、引号、别名前缀写法）不改变指纹；改了表达式、过滤、关联就会变。"""
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect.name) if s is not None]
    except ParseError:
        return None, sha256_text("raw:" + " ".join(sql.split()))
    parts = []
    for statement in statements:
        dialect.prepare(statement)
        parts.append(statement.sql(dialect=dialect.name, normalize=True, identify=True))
    canonical = ";\n".join(parts)
    return canonical, sha256_text(canonical)


# --- 解析 -------------------------------------------------------------------------

def parse_query(sql: str, *, dialect: SqlDialect, schema: dict[str, dict[str, dict[str, dict[str, str]]]],
                default_catalog: str | None = None, default_db: str | None = None) -> ParsedQuery:
    d = dialect.name
    canonical, digest = canonical_hash_of(sql, dialect)
    try:
        statements = [s for s in sqlglot.parse(sql, read=d) if s is not None]
    except ParseError as exc:
        return ParsedQuery("failed", f"SQL 无法解析：{_first_line(exc)}", canonical, digest)
    if len(statements) != 1:
        return ParsedQuery("unsupported", f"包含 {len(statements)} 条语句；视图定义只接受一条 SELECT",
                           canonical, digest)
    root = statements[0]
    if not isinstance(root, exp.Query):
        return ParsedQuery("unsupported", f"不是查询语句（{root.key.upper()}）", canonical, digest)

    dialect.prepare(root)
    tables = default_tables(root, default_catalog or dialect.default_catalog, default_db)
    result = ParsedQuery("ok", None, canonical, digest, tables=tables)

    if isinstance(root, exp.SetOperation):
        result.status = "unsupported"
        result.reason = f"{root.key.upper()} 的列级血缘不在 v0.1 范围；已记录表级来源"
        return result

    try:
        qualified = qualify(root, schema=MappingSchema(schema, dialect=d), dialect=d,
                            validate_qualify_columns=True, quote_identifiers=False)
    except (OptimizeError, ValueError, KeyError) as exc:
        result.status = "failed"
        result.reason = f"列无法对应到已采集的表结构：{_first_line(exc)}"
        result.unresolved = [t for t in tables if not _in_schema(schema, t)]
        return result

    scope = build_scope(qualified)
    if scope is None:
        result.status = "failed"
        result.reason = "无法建立查询作用域"
        return result

    problems: list[str] = []
    result.unresolved = [t for t in tables if not _in_schema(schema, t)]
    if result.unresolved:
        problems.append("未采集到的表：" + "、".join(result.unresolved))
    if _select_star(qualified):
        problems.append("SELECT * 未能按已采集的表结构展开")
    select = scope.expression
    for projection in select.selects:
        name = projection.alias_or_name
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if inner.find(exp.Subquery):
            problems.append(f"输出列 {name} 含标量子查询，来源未展开")
        sources = _trace(inner, scope, problems, d)
        relation = max((s.relation for s in sources), key=VALUE_RANK.get, default=None)
        result.columns.append(OutputColumn(name=name, expression=inner.sql(dialect=d), relation=relation,
                                           sources=sources))

    for sub in traverse_scope(qualified):
        _collect_influences(sub, result, problems, d)

    result.distinct = bool(select.args.get("distinct"))
    result.group_outputs = _group_outputs(select, d)
    result.passthrough = _passthrough(scope, result)
    if problems:
        result.status = "partial"
        result.reason = "；".join(dict.fromkeys(problems))
    return result


def _in_schema(schema: dict, qualified: str) -> bool:
    parts = qualified.split(".")
    node: Any = schema
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _first_line(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0][:300] if str(exc).strip() else type(exc).__name__


def _level_relation(expression: exp.Expression) -> str:
    if expression.find(exp.AggFunc) and not expression.find(exp.Window):
        return "value_aggregated"
    if isinstance(expression, exp.Column):
        return "value_direct"
    return "value_computed"


def _trace(expression: exp.Expression, scope: Scope, problems: list[str], d: str,
           inherited: str = "value_direct", depth: int = 0) -> list[ValueSource]:
    """把表达式中的列追到实体表的列，沿途合成取值类型（直接 < 计算 < 聚合）。"""
    if depth > 32:
        problems.append("嵌套层数超过 32，来源未展开")
        return []
    level = _level_relation(expression)
    relation = max(inherited, level, key=VALUE_RANK.get)
    out: list[ValueSource] = []
    for column in expression.find_all(exp.Column):
        if _inside_subquery(column, expression):
            continue  # 标量子查询内的列属于另一个作用域，已在投影处报告“未展开”
        source = scope.sources.get(column.table)
        if isinstance(source, exp.Table):
            out.append(ValueSource(ColumnRef(qualified_name(source), column.name), relation,
                                   expression.sql(dialect=d)))
        elif isinstance(source, Scope):
            inner = _projection(source, column.name)
            if inner is None:
                problems.append(f"子查询 {column.table} 中找不到列 {column.name}")
                continue
            out.extend(_trace(inner, source, problems, d, relation, depth + 1))
        else:
            problems.append(f"列 {column.sql(dialect=d)} 的来源无法确定")
    return out


def _inside_subquery(node: exp.Expression, root: exp.Expression) -> bool:
    """node 是否位于 root 内部的子查询中（root 本身是子查询也算）。"""
    if isinstance(root, exp.Subquery):
        return True
    current = node
    while current is not None and current is not root:
        if isinstance(current, exp.Subquery):
            return True
        current = current.parent
    return False


def _projection(scope: Scope, name: str) -> exp.Expression | None:
    for projection in scope.expression.selects:
        if projection.alias_or_name == name:
            return projection.this if isinstance(projection, exp.Alias) else projection
    return None


def _resolve_refs(expression: exp.Expression, scope: Scope, problems: list[str], d: str) -> list[ColumnRef]:
    return [s.ref for s in _trace(expression, scope, problems, d)]


def _collect_influences(scope: Scope, result: ParsedQuery, problems: list[str], d: str) -> None:
    select = scope.expression
    if not isinstance(select, exp.Select):
        return
    seen = {(i.ref, i.relation) for i in result.influences}

    def add(expression: exp.Expression | None, relation: str) -> None:
        if expression is None:
            return
        text = expression.sql(dialect=d)
        for ref in _resolve_refs(expression, scope, problems, d):
            if (ref, relation) not in seen:
                seen.add((ref, relation))
                result.influences.append(Influence(ref, relation, text))

    where = select.args.get("where")
    add(where.this if where else None, "filter")
    having = select.args.get("having")
    add(having.this if having else None, "filter")
    group = select.args.get("group")
    if group:
        for key in group.expressions:
            add(key, "group_by")

    from_ = select.args.get("from_") or select.args.get("from")
    left_tables = []
    if from_ is not None:
        left_tables.extend(_source_tables(from_.this, scope))
    for join in select.args.get("joins") or []:
        on = join.args.get("on")
        right_alias = join.this.alias_or_name
        right_tables = _source_tables(join.this, scope)
        if join.args.get("using"):
            problems.append(f"JOIN USING（{right_alias}）未展开为列级关联")
        if on is None:
            if not join.args.get("using"):
                problems.append(f"与 {right_alias} 的关联没有 ON 条件（笛卡尔积或 LATERAL）")
            left_tables.extend(right_tables)
            continue
        add(on, "join_key")
        left_refs: list[ColumnRef] = []
        right_refs: list[ColumnRef] = []
        # 只有顶层 AND 连接的等值项才必然成立，才能用来推导唯一性；含 OR / NOT 时不提取关联键（粒度不传播）
        conjuncts = list(on.flatten()) if isinstance(on, exp.And) else [on]
        if any(c.find(exp.Or, exp.Not) for c in conjuncts):
            problems.append(f"与 {right_alias} 的关联条件含 OR / NOT，未用于推导唯一性")
            conjuncts = []
        for eq in (c for c in conjuncts if isinstance(c, exp.EQ)):
            a, b = eq.this, eq.expression
            if not (isinstance(a, exp.Column) and isinstance(b, exp.Column)):
                continue
            if a.table == right_alias and b.table != right_alias:
                a, b = b, a
            if b.table == right_alias and a.table != right_alias:
                left_refs.extend(_resolve_refs(a, scope, problems, d))
                right_refs.extend(_resolve_refs(b, scope, problems, d))
        side = join.args.get("side") or ""
        result.joins.append(JoinInfo(
            kind=(side or join.args.get("kind") or "inner").lower(), left=left_refs, right=right_refs,
            right_table=right_tables[0] if len(right_tables) == 1 else None,
            left_tables=list(left_tables), condition=on.sql(dialect=d)))
        left_tables.extend(right_tables)


def _source_tables(node: exp.Expression, scope: Scope) -> list[str]:
    source = scope.sources.get(node.alias_or_name)
    if isinstance(source, exp.Table):
        return [qualified_name(source)]
    return []


def _group_outputs(select: exp.Select, d: str) -> list[str] | None:
    """GROUP BY 的键都出现在输出中时，返回对应的输出列名（即视图的粒度）。"""
    group = select.args.get("group")
    if not group:
        return None
    keys = [k.sql(dialect=d) for k in group.expressions]
    outputs = {}
    for projection in select.selects:
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        outputs[inner.sql(dialect=d)] = projection.alias_or_name
    names = [outputs.get(k) for k in keys]
    return names if all(names) else []


def _passthrough(scope: Scope, result: ParsedQuery) -> dict[str, Any] | None:
    """无分组、单一来源表时，记录“实体列 → 输出列”的直接映射，供粒度传播使用。"""
    select = scope.expression
    if select.args.get("group") or result.distinct:
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None:
        return None
    base = _source_tables(from_.this, scope)
    if len(base) != 1:
        return None
    mapping = {}
    for column in result.columns:
        if column.relation == "value_direct" and len(column.sources) == 1 and column.sources[0].ref.table == base[0]:
            mapping[column.sources[0].ref.column] = column.name
    return {"base": base[0], "mapping": mapping, "joined": [j.right_table for j in result.joins]}
