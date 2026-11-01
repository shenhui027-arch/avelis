"""把 SQL 语句转为有序写入事实，列级数据流统一交给查询解析器。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from avelis.lineage.sql import SqlDialect, parse_query


@dataclass
class StatementEdge:
    source_ref: str
    source_column: str | None
    target_column: str | None
    relation: str
    expression: str | None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class Write:
    target_ref: str
    target_columns: list[str] = field(default_factory=list)
    edges: list[StatementEdge] = field(default_factory=list)
    temporary: bool = False
    created: bool = False
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "parsed"
    reason: str | None = None


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
    writes: list[Write] = field(default_factory=list)
    dropped_refs: list[str] = field(default_factory=list)
    removed_refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 单目标调用方仍可直接读取这些字段；多目标调用方遍历 writes。
        if self.writes:
            first = self.writes[0]
            self.target_ref = first.target_ref
            self.target_columns = first.target_columns
            self.edges = first.edges
            self.temporary = first.temporary


def _qualify(table: exp.Table, default_catalog: str, default_db: str | None) -> str | None:
    db = table.db or default_db
    return f"{table.catalog or default_catalog}.{db}.{table.name}" if db else None


def _schema_columns(schema: dict, qualified: str) -> list[str] | None:
    catalog, db, name = qualified.split(".")
    cols = schema.get(catalog, {}).get(db, {}).get(name)
    return list(cols) if cols else None


def _target(node: exp.Expression) -> tuple[exp.Expression, list[str]]:
    if isinstance(node, exp.Schema):
        return node.this, [c.name for c in node.expressions if isinstance(c, (exp.Identifier, exp.ColumnDef))]
    return node, []


def _result(kind: str, writes: list[Write], **kwargs: Any) -> ParsedStatement:
    statuses = {w.status for w in writes}
    status = next((s for s in ("failed", "unsupported", "partial") if s in statuses), "parsed")
    reasons = list(dict.fromkeys(w.reason for w in writes if w.reason))
    return ParsedStatement(status, kind, writes=writes, reason="；".join(reasons) or None, **kwargs)


def _with(query: exp.Query, owner: exp.Expression) -> exp.Query:
    query = query.copy()
    outer = owner.args.get("with_")
    if outer:
        inner = query.args.get("with_")
        merged = outer.copy()
        if inner:
            merged.set("expressions", [*merged.expressions, *(c.copy() for c in inner.expressions)])
        query.set("with_", merged)
    return query


def _query_write(target_node: exp.Expression, query: exp.Query, columns: list[str], *,
                 dialect: SqlDialect, schema: dict, default_db: str | None,
                 temporary: bool = False, created: bool = False) -> Write:
    if not isinstance(target_node, exp.Table):
        raise ValueError("目标不是表")
    target = _qualify(target_node, dialect.default_catalog, default_db)
    if target is None:
        raise ValueError(f"目标表 {target_node.name} 缺少库名，且没有默认库")
    parsed = parse_query(query.sql(dialect=dialect.name), dialect=dialect, schema=schema,
                         default_catalog=dialect.default_catalog, default_db=default_db)
    write = Write(target, temporary=temporary, created=created,
                  status="parsed" if parsed.status == "ok" else parsed.status, reason=parsed.reason)
    if parsed.status in ("failed", "unsupported"):
        write.edges = [StatementEdge(t, None, None, "table", None) for t in parsed.tables if t != target]
        return write
    outputs = [c.name for c in parsed.columns]
    if not columns:
        known = _schema_columns(schema, target)
        if known and len(known) == len(outputs):
            columns = known
        else:
            columns = outputs
            if known is None:
                note = "目标表未采集，按 SELECT 输出列名对应目标列"
                write.reason = f"{write.reason}；{note}" if write.reason else note
    if len(columns) != len(outputs):
        write.status = "partial"
        write.reason = f"目标列 {len(columns)} 个，SELECT 输出 {len(outputs)} 个，无法逐列对应"
        write.edges = [StatementEdge(t, None, None, "table", None) for t in parsed.tables if t != target]
        return write
    write.target_columns = list(columns)
    for target_column, column in zip(columns, parsed.columns):
        for src in column.sources:
            if src.ref.table == target and src.ref.column == target_column:
                continue
            write.edges.append(StatementEdge(src.ref.table, src.ref.column, target_column, src.relation,
                                              column.expression))
    for inf in parsed.influences:
        write.edges.append(StatementEdge(inf.ref.table, inf.ref.column, None, inf.relation, inf.expression))
    # 标量子查询的列级展开超出 parse_query 当前能力，至少保留其表级来源。
    if any(p.find(exp.Subquery) for p in query.selects):
        known_sources = {e.source_ref for e in write.edges}
        write.edges.extend(StatementEdge(t, None, None, "table", None)
                           for t in parsed.tables if t != target and t not in known_sources)
    return write


def _select(expressions: list[exp.Expression]) -> exp.Select:
    # 固定别名只用于按位置取回投影，目标列仍来自 SET / INSERT 的列清单。
    return exp.Select(expressions=[exp.alias_(item.copy(), f"_import_{i}")
                                   for i, item in enumerate(expressions)])


def _set_parts(update: exp.Update) -> tuple[list[str], list[exp.Expression]]:
    columns, values = [], []
    for assignment in update.expressions:
        if not isinstance(assignment, exp.EQ) or not isinstance(assignment.this, exp.Column):
            raise ValueError("SET 赋值形式尚不支持逐列对应")
        columns.append(assignment.this.name)
        values.append(assignment.expression)
    return columns, values


def _from(query: exp.Select, source: exp.Expression) -> None:
    source = source.copy()
    joins = source.args.get("joins") or []
    source.set("joins", None)
    query.set("from_", exp.From(this=source))
    if joins:
        query.set("joins", joins)


def _update_query(tree: exp.Update) -> tuple[exp.Expression, list[str], exp.Select]:
    columns, values = _set_parts(tree)
    query = _select(values)
    target = tree.this
    from_ = tree.args.get("from_")
    # UPDATE alias ... FROM 实表 alias：目标必须取 FROM 中的实体表。
    bound_target = next((t for t in from_.find_all(exp.Table)
                         if isinstance(target, exp.Table) and not target.db
                         and t.alias_or_name == target.name), None) if from_ else None
    if bound_target is not None:
        target = bound_target.copy()
        _from(query, from_.this)
    else:
        _from(query, target)
        if from_:
            source = from_.this.copy()
            joins = source.args.get("joins") or []
            source.set("joins", None)
            query.append("joins", exp.Join(this=source, on=exp.true()))
            for join in joins:
                query.append("joins", join)
    if tree.args.get("where"):
        query.set("where", tree.args["where"].copy())
    return target, columns, _with(query, tree)


def _insert_write(tree: exp.Insert, *, dialect: SqlDialect, schema: dict,
                  default_db: str | None, shared: exp.Expression | None = None,
                  condition: exp.Expression | None = None) -> Write | None:
    target, columns = _target(tree.this)
    query = tree.expression
    if isinstance(query, exp.Values):
        if not isinstance(shared, exp.Query) or len(query.expressions) != 1:
            return None
        query = _select(query.expressions[0].expressions)
        _from(query, shared.copy().subquery(alias="_import_source"))
    elif isinstance(query, exp.Query):
        query = query.copy()
        if shared is not None and isinstance(query, exp.Select) and not query.args.get("from_"):
            _from(query, shared)
    else:
        raise ValueError("INSERT 的来源尚不支持")
    if condition is not None and isinstance(query, exp.Select):
        query = query.where(condition.copy())
    return _query_write(target, _with(query, tree), columns, dialect=dialect, schema=schema, default_db=default_db)


def _merge_writes(tree: exp.Merge, *, dialect: SqlDialect, schema: dict, default_db: str | None) -> list[Write]:
    writes = []
    whens = tree.args.get("whens")
    for when in whens.expressions if whens else []:
        action = when.args.get("then")
        if isinstance(action, exp.Update):
            columns, values = _set_parts(action)
        elif isinstance(action, exp.Insert) and isinstance(action.expression, exp.Tuple):
            columns = [c.name for c in action.this.expressions] if isinstance(action.this, exp.Tuple) else []
            values = action.expression.expressions
        else:
            continue  # DELETE 分支不产生写入数据流。
        query = _select(values)
        _from(query, tree.this)
        query.append("joins", exp.Join(this=tree.args["using"].copy(), on=tree.args["on"].copy()))
        if when.args.get("condition") is not None:
            query = query.where(when.args["condition"].copy())
        writes.append(_query_write(tree.this, _with(query, tree), columns,
                                   dialect=dialect, schema=schema, default_db=default_db))
    return writes


def _rename_writes(tree: exp.Alter, *, dialect: SqlDialect, schema: dict,
                   default_db: str | None) -> tuple[list[Write], list[str]]:
    if not isinstance(tree.this, exp.Table):
        raise ValueError("改名的目标不是表")
    old = _qualify(tree.this, dialect.default_catalog, default_db)
    if old is None:
        raise ValueError("改名的来源表缺少库名，且没有默认库")
    actions = tree.args.get("actions") or []
    if len(actions) != 1 or not isinstance(actions[0], (exp.AlterRename, exp.SwapTable)):
        raise ValueError("ALTER TABLE 操作尚不支持数据流解析")
    action = actions[0]
    new_node = action.this
    if not isinstance(new_node, exp.Table):
        raise ValueError("改名后的目标不是表")
    new = _qualify(new_node, tree.this.catalog or dialect.default_catalog, tree.this.db or default_db)
    if new is None:
        raise ValueError("改名后的表缺少库名")
    op = "swap" if isinstance(action, exp.SwapTable) else "rename"
    pairs = [(old, new), (new, old)] if op == "swap" else [(old, new)]
    writes = []
    for source, target in pairs:
        columns = _schema_columns(schema, source) or []
        edges = ([StatementEdge(source, c, c, "value_direct", None, {"op": op}) for c in columns]
                 if columns else [StatementEdge(source, None, None, "table", None, {"op": op})])
        writes.append(Write(target, columns, edges, attributes={"op": op}))
    return writes, [source for source, _ in pairs]


def _code_block(sql: str, dialect: SqlDialect) -> bool:
    try:
        tokens = sqlglot.tokenize(sql, read=dialect.name)
    except TokenError:
        return False
    if not tokens:
        return False
    first = tokens[0].text.upper()
    if first == "DO":
        return True
    if first in ("BEGIN", "DECLARE"):
        return len(tokens) > 1 and tokens[1].text.upper() not in (";", "TRANSACTION", "TRAN", "WORK")
    if first == "CREATE":
        for token in tokens[1:5]:
            kind = token.text.upper()
            if kind in ("TABLE", "VIEW", "INDEX", "DATABASE", "SCHEMA", "TYPE", "SEQUENCE"):
                return False
            if kind in ("PROCEDURE", "FUNCTION", "TRIGGER"):
                return True
    return False


def parse_statement(sql: str, *, dialect: SqlDialect, schema: dict,
                    default_db: str | None = None, line_start: int = 1) -> ParsedStatement:
    if _code_block(sql, dialect):
        return ParsedStatement("unsupported", "BLOCK", reason="存储过程 / 代码块，本期不展开内部语句")
    try:
        trees = [tree for tree in sqlglot.parse(sql, read=dialect.name) if tree is not None]
    except (ParseError, TokenError) as exc:
        error = exc.errors[0] if isinstance(exc, ParseError) and exc.errors else None
        if error and all(error.get(key) is not None for key in ("description", "line", "col", "highlight")):
            description = error["description"].split("but got <Token", 1)[0].rstrip()
            reason = (f"SQL 无法解析：第 {line_start + error['line'] - 1} 行第 {error['col']} 列附近"
                      f"（{error['highlight']}）：{description}")
        else:
            reason = "SQL 无法解析：" + (str(exc).splitlines() or [""])[0][:200]
        return ParsedStatement("failed", None, reason=reason)
    if not trees:
        return ParsedStatement("skipped", None, reason="空语句")
    if len(trees) != 1:
        return ParsedStatement("unsupported", None, reason="包含多条语句，请先按脚本切分")
    tree = dialect.prepare(trees[0])
    kind = tree.key.upper()
    if isinstance(tree, exp.Use):
        return ParsedStatement("skipped", "USE", use_db=tree.this.name if tree.this else None,
                               reason="切换默认库，不产生数据流")
    kwargs = {"dialect": dialect, "schema": schema, "default_db": default_db}
    try:
        if isinstance(tree, exp.Insert):
            kind = "INSERT OVERWRITE" if tree.args.get("overwrite") else "INSERT"
            write = _insert_write(tree, **kwargs)
            return (_result(kind, [write]) if write else
                    ParsedStatement("skipped", kind, reason="没有来源表"))
        if isinstance(tree, exp.MultitableInserts):
            writes = []
            conditions: list[exp.Expression] = []
            for branch in tree.expressions:
                insert = branch.this if isinstance(branch, exp.ConditionalInsert) else branch
                condition = branch.expression if isinstance(branch, exp.ConditionalInsert) else None
                if isinstance(branch, exp.ConditionalInsert) and branch.args.get("else_") and conditions:
                    condition = exp.Not(this=exp.Paren(this=exp.or_(*(c.copy() for c in conditions))))
                if condition is not None:
                    conditions.append(condition)
                if not isinstance(insert, exp.Insert):
                    raise ValueError("多路 INSERT 分支尚不支持")
                write = _insert_write(insert, shared=tree.args.get("source"), condition=condition, **kwargs)
                if write:
                    writes.append(write)
            return (_result("MULTITABLE INSERT", writes) if writes else
                    ParsedStatement("skipped", "MULTITABLE INSERT", reason="没有来源表"))
        if isinstance(tree, exp.Update):
            target, columns, query = _update_query(tree)
            return _result("UPDATE", [_query_write(target, query, columns, **kwargs)])
        if isinstance(tree, exp.Merge):
            writes = _merge_writes(tree, **kwargs)
            return (_result("MERGE", writes) if writes else
                    ParsedStatement("skipped", "MERGE", reason="没有产生数据流的写入分支"))
        if isinstance(tree, exp.RenameTable):
            # RENAME TABLE a TO b[, c TO d]：每一对与 ALTER TABLE … RENAME 相同，来源都取语句执行前的状态
            writes, removed = [], []
            for alter in tree.expressions:
                pair_writes, pair_removed = _rename_writes(alter, **kwargs)
                writes.extend(pair_writes)
                removed.extend(pair_removed)
            return _result("RENAME TABLE", writes, removed_refs=removed)
        if isinstance(tree, exp.Alter):
            writes, removed = _rename_writes(tree, **kwargs)
            return _result("ALTER TABLE SWAP" if writes[0].attributes["op"] == "swap" else "ALTER TABLE RENAME",
                           writes, removed_refs=removed)
        if isinstance(tree, exp.Create) and tree.args.get("kind", "").upper() in ("TABLE", "VIEW"):
            target, columns = _target(tree.this)
            if not isinstance(target, exp.Table):
                raise ValueError("目标不是表")
            props = tree.args.get("properties")
            temporary = bool(props and any(isinstance(p, exp.TemporaryProperty) for p in props.expressions))
            materialized = bool(props and any(isinstance(p, exp.MaterializedProperty) for p in props.expressions))
            kind = "CREATE MATERIALIZED VIEW" if materialized else f"CREATE {tree.args['kind'].upper()}"
            if isinstance(tree.expression, exp.Query):
                kind = kind if materialized else kind + " AS"
                return _result(kind, [_query_write(target, _with(tree.expression, tree), columns,
                                                   temporary=temporary, created=True, **kwargs)])
            ref = _qualify(target, dialect.default_catalog, default_db)
            if ref is None:
                raise ValueError(f"目标表 {target.name} 缺少库名，且没有默认库")
            like = next(tree.find_all(exp.LikeProperty), None)
            if like and isinstance(like.this, exp.Table):
                source = _qualify(like.this, dialect.default_catalog, default_db)
                columns = (_schema_columns(schema, source) or []) if source else []
                write = Write(ref, columns, temporary=temporary, created=True,
                              reason="只复制表结构，不产生数据流" if columns else "来源表结构未知，未登记目标列")
                return _result("CREATE TABLE LIKE", [write])
            return _result(kind, [Write(ref, columns, temporary=temporary, created=True,
                                        reason="只登记表结构，不产生数据流")])
        if isinstance(tree, exp.Drop) and tree.args.get("kind", "").upper() == "TABLE":
            refs = [ref for t in tree.find_all(exp.Table)
                    if (ref := _qualify(t, dialect.default_catalog, default_db))]
            return ParsedStatement("skipped", "DROP TABLE", dropped_refs=refs, reason="删除表，不产生数据流")
        if isinstance(tree, (exp.Delete, exp.TruncateTable, exp.Set, exp.Transaction, exp.Commit, exp.Rollback)):
            return ParsedStatement("skipped", kind, reason="不产生写入来源的数据流")
        if isinstance(tree, exp.Query):
            return ParsedStatement("skipped", "SELECT", reason="只读查询，不产生数据流")
    except ValueError as exc:
        return ParsedStatement("unsupported", kind, reason=str(exc))
    return ParsedStatement("unsupported", kind, reason=f"{kind} 写法尚不受 SQL 语法树解析支持")
