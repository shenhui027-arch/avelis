"""导入的模型辅助：逐片段记录、校验与复用，结果始终只是候选。"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
import shlex
import textwrap
from dataclasses import asdict, dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from avelis import modelcfg
from avelis.importer.candidates import (
    Candidate,
    ModelOperations,
    candidate_prompt,
    demo_candidates,
    model_candidates,
    model_operations,
    operation_prompt,
    qualify_ref,
    redact,
    validate_candidate,
)
from avelis.importer.extract import placeholders_of, substitute
from avelis.importer.statements import parse_statement
from avelis.lineage.sql import SqlDialect


@dataclass
class AssistBudget:
    """由执行层为整批创建一个实例，所有文件共用。"""

    fragments: int = 0
    characters: int = 0

    def reserve(self, size: int) -> bool:
        if self.fragments >= 200 or self.characters + size > 2_000_000:
            return False
        self.fragments += 1
        self.characters += size
        return True


@dataclass
class CodeBlock:
    line_start: int
    line_end: int
    text: str
    header: str = ""

    @property
    def snippet(self) -> str:
        return f"文件头部：\n{self.header}\n代码块：\n{self.text}" if self.header else self.text


@dataclass
class AssistanceResult:
    candidates: list[dict[str, Any]] = field(default_factory=list)
    payloads: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    suggested_sql_functions: list[str] = field(default_factory=list)
    statements: list[dict[str, Any]] = field(default_factory=list)
    statement_updates: dict[str, dict[str, str]] = field(default_factory=dict)
    cache: dict[str, dict[str, Any]] = field(default_factory=dict)


def _value(item: Any, key: str, default=None):
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _shell_static_line(line: str) -> bool:
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return False
    if words and words[0] == "export":
        words = words[1:]
    if not all(re.fullmatch(r"[A-Za-z_]\w*=.*", word, re.S) for word in words):
        return False
    quote = None
    escaped = False
    for i, character in enumerate(line):
        if escaped:
            escaped = False
        elif character == "\\" and quote != "'":
            escaped = True
        elif character in ("'", '"') and quote in (None, character):
            quote = None if quote else character
        elif character == "#" and quote is None and (i == 0 or line[i - 1].isspace()):
            break
        elif quote != "'" and (character == "`" or line.startswith("$(", i)):
            return False
    return True


def code_blocks(source: str, language: str) -> list[CodeBlock]:
    """按顶层函数或类分块，静态文件头附到每块，不执行原文。"""
    lines = source.splitlines()
    if not lines:
        return []
    if language == "python":
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return [CodeBlock(1, len(lines), source)]
        definitions = [
            node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        header_nodes = [
            node
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign))
            and not any(isinstance(child, (ast.Call, ast.Await)) for child in ast.walk(node))
        ]
        header = "\n".join("\n".join(lines[n.lineno - 1 : n.end_lineno]) for n in header_nodes)
        blocks: list[CodeBlock] = []

        cursor = 1
        for node in definitions:
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            if cursor < start and any(n not in header_nodes for n in tree.body if cursor <= n.lineno < start):
                blocks.append(CodeBlock(cursor, start - 1, "\n".join(lines[cursor - 1 : start - 1]), header))
            blocks.append(
                CodeBlock(start, node.end_lineno or node.lineno, "\n".join(lines[start - 1 : node.end_lineno]), header)
            )
            cursor = (node.end_lineno or node.lineno) + 1
        if cursor <= len(lines):
            blocks.append(CodeBlock(cursor, len(lines), "\n".join(lines[cursor - 1 :]), header))
        return blocks or [CodeBlock(1, len(lines), source, header)]
    if language == "shell":
        starts = [
            i
            for i, line in enumerate(lines, 1)
            if re.match(r"^\s*(?:function\s+)?[A-Za-z_]\w*\s*(?:\(\s*\))?\s*\{\s*(?:#.*)?$", line)
        ]
        if starts:
            preamble = list(enumerate(lines[: starts[0] - 1], 1))
            static_lines = {i for i, line in preamble if _shell_static_line(line)}
            header = "\n".join(line for i, line in preamble if i in static_lines)
            executable = [i for i, _ in preamble if i not in static_lines]
            blocks = []
            if executable:
                first, last = executable[0], executable[-1]
                blocks.append(CodeBlock(first, last, "\n".join(lines[first - 1 : last]), header))
            ends = [*starts[1:], len(lines) + 1]
            blocks.extend(
                CodeBlock(start, end - 1, "\n".join(lines[start - 1 : end - 1]), header)
                for start, end in zip(starts, ends)
            )
            return blocks
    return [CodeBlock(1, len(lines), source)]


def _constant_values(source: str) -> set[str]:
    """只求值字面量、字典索引和字符串拼接，为表名提供可复核的依据。"""
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return _shell_constant_values(source)
    bindings: dict[str, Any] = {}
    strings: set[str] = set()

    def evaluate(node: ast.AST):
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float)):
            return node.value
        if isinstance(node, ast.Name):
            return bindings.get(node.id)
        if isinstance(node, ast.Dict):
            return {evaluate(k): evaluate(v) for k, v in zip(node.keys, node.values) if k is not None}
        if isinstance(node, (ast.List, ast.Tuple)):
            return [evaluate(n) for n in node.elts]
        if isinstance(node, ast.Subscript):
            value, key = evaluate(node.value), evaluate(node.slice)
            if isinstance(value, dict):
                return value.get(key)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(left, str) and isinstance(right, str):
                return left + right
        if isinstance(node, ast.JoinedStr):
            values = [evaluate(n.value) if isinstance(n, ast.FormattedValue) else evaluate(n) for n in node.values]
            if all(isinstance(v, (str, int, float)) for v in values):
                return "".join(str(v) for v in values)
        return None

    def collect(value):
        if isinstance(value, str):
            strings.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    def visit(nodes):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                snapshot = dict(bindings)
                visit(node.body)
                bindings.clear()
                bindings.update(snapshot)
            else:
                for child in ast.walk(node):
                    collect(evaluate(child))
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    value = evaluate(node.value) if node.value else None
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            bindings[target.id] = value
                    collect(value)

    visit(tree.body)
    return strings


def _shell_constant_values(source: str) -> set[str]:
    """Shell 常量仅用于核对表名，不执行命令、插值表达式或还原后的 SQL。"""
    bindings: dict[str, str] = {}
    strings: set[str] = set()
    variable = re.compile(r"\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)")
    for line in source.splitlines():
        if "$(" in line or "`" in line:
            continue
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            continue
        if words and words[0] == "export":
            words = words[1:]
        for word in words:
            unresolved = False

            def expand(match):
                nonlocal unresolved
                name = match.group(1) or match.group(2)
                if name not in bindings:
                    unresolved = True
                    return match.group(0)
                return bindings[name]

            value = variable.sub(expand, word)
            if unresolved:
                continue
            assignment = re.fullmatch(r"([A-Za-z_]\w*)=(.*)", value, re.S)
            if assignment:
                bindings[assignment.group(1)] = assignment.group(2)
                strings.add(assignment.group(2))
            else:
                strings.add(value)
    return strings


def normalized_sql(sql: str, dialect_name: str) -> str | None:
    try:
        tree = sqlglot.parse_one(sql, read=dialect_name)
        if tree is None:
            return None
        normalize_identifiers(tree, dialect=dialect_name)
        return tree.sql(dialect=dialect_name, comments=False)
    except (sqlglot.errors.SqlglotError, ValueError):
        return None


def _grounded_tables(
    sql: str, dialect: SqlDialect, block: CodeBlock, known: dict[str, set[str]], default_db: str | None
) -> str | None:
    try:
        tree = sqlglot.parse_one(sql, read=dialect.name)
    except sqlglot.errors.SqlglotError:
        return None  # 由确定性解析器如实报告不支持的语法。
    if tree is None:
        return None
    tree = dialect.prepare(tree)
    normalize_identifiers(tree, dialect=dialect.name)
    visible = "\n".join([block.header, block.text])
    constants = _constant_values(block.header + "\n" + textwrap.dedent(block.text))
    ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    targets = set()
    for write in tree.walk():
        if isinstance(write, (exp.Insert, exp.Create, exp.Update, exp.Merge)):
            target = write.this.this if isinstance(write.this, exp.Schema) else write.this
            if isinstance(target, exp.Table):
                targets.add(id(target))
    for table in tree.find_all(exp.Table):
        if not table.db and table.name in ctes:
            continue
        names = [".".join(filter(None, [table.db, table.name])), table.sql(dialect=dialect.name)]
        if not any(
            re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I)
            for name in names
            for text in [visible, *constants]
        ):
            return f"还原 SQL 的表 {names[0]} 未出现在代码块中，也不能由块内常量拼出"
        if id(table) not in targets:
            qualified = qualify_ref(
                ".".join(filter(None, [table.catalog, table.db, table.name])), default_db, dialect.default_catalog
            )
            if qualified not in known:
                return f"来源表 {names[0]} 未采集，也不是本批次产生的表"
    return None


def _known_tables(schema: dict) -> dict[str, set[str]]:
    return {
        f"{c}.{d}.{t}": set(cols)
        for c, dbs in schema.items()
        for d, tables in dbs.items()
        for t, cols in tables.items()
    }


def _fragment_hash(
    mode: str,
    text: str,
    reason: str | None,
    dialect: SqlDialect,
    default_db: str | None,
    execution: dict[str, Any],
    database_context: list[dict],
) -> str:
    value = [mode, text, reason, dialect.name, dialect.default_catalog, default_db, execution, database_context]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def _default_db_at(contexts: list[dict], line: int, default_db: str | None, seq: int | None = None) -> str | None:
    if seq is not None:
        match = next((c for c in contexts if c["seq"] == seq), None)
        if match is not None:
            return match["default_db"]
    for context in contexts:
        if context["line_start"] > line:
            break
        if context["line_end"] >= line:
            return context["default_db"]
        default_db = context["default_db_after"]
    return default_db


def run_assistance(
    *,
    language: str,
    content: str,
    rule_statements: list[Any],
    dialect: SqlDialect,
    schema: dict,
    default_db: str | None,
    options: dict,
    policy: dict,
    resolved=None,
    budget: AssistBudget | None = None,
    cached: dict[str, dict[str, Any]] | None = None,
    statement_contexts: list[dict[str, Any]] | None = None,
) -> AssistanceResult:
    """不访问数据库；调用方在确定性提交后写入返回的语句、候选与逐片段载荷。"""
    result = AssistanceResult()
    budget = budget if budget is not None else AssistBudget()
    cached = cached or {}
    # 模型还原的结构只供本文件后续候选使用，不能污染确定性解析的 schema。
    model_schema = copy.deepcopy(schema)
    known = _known_tables(model_schema)
    model = resolved if policy.get("send_code") else None
    adapter = model.adapter if model else "demo-rules"
    model_id = (
        (
            getattr(model.model, "model_name", None)
            or getattr(model.model, "model", None)
            or adapter.partition(":")[2]
            or adapter
        )
        if model
        else None
    )
    execution = {
        "is_demo": model is None,
        "adapter": adapter,
        "model_id": model_id if isinstance(model_id, str) else adapter,
    }
    contexts = sorted(statement_contexts or [], key=lambda c: (c["line_start"], c["seq"]))
    file_end = max(1, len(content.splitlines()))
    assist = options.get("model_assist") or {}
    fragments: list[tuple[str, CodeBlock, Any, str | None]] = []
    for stmt in rule_statements:
        block = CodeBlock(_value(stmt, "line_start", 1), _value(stmt, "line_end", 1), _value(stmt, "raw_text", ""))
        if _value(stmt, "method") == "unrecognized":
            fragments.append(("non_sql", block, stmt, None))
        elif assist.get("failed_sql") and _value(stmt, "status") in ("failed", "unsupported"):
            fragments.append(("failed_sql", block, stmt, _value(stmt, "reason")))
    if assist.get("code") and language in ("python", "shell"):
        fragments.extend(("code", block, None, None) for block in code_blocks(content, language))

    def add_candidate(values: dict[str, Any]) -> None:
        signature = tuple(
            values.get(k)
            for k in (
                "statement_id",
                "origin",
                "source_ref",
                "source_column",
                "target_ref",
                "target_column",
                "relation",
            )
        )
        if not any(
            tuple(
                c.get(k)
                for k in (
                    "statement_id",
                    "origin",
                    "source_ref",
                    "source_column",
                    "target_ref",
                    "target_column",
                    "relation",
                )
            )
            == signature
            for c in result.candidates
        ):
            result.candidates.append(values)

    for mode, block, stmt, failure_reason in fragments:
        fragment_db = _default_db_at(contexts, block.line_start, default_db, _value(stmt, "seq"))
        database_context = [
            {
                "line_start": c["line_start"] - block.line_start,
                "line_end": c["line_end"] - block.line_start,
                "default_db": c["default_db"],
                "default_db_after": c["default_db_after"],
            }
            for c in contexts
            if block.line_start <= c["line_start"] <= block.line_end
        ]
        context_prompt = "\n".join(
            f"第 {c['line_end'] + block.line_start} 行之后默认数据库为 "
            f"{c['default_db_after'] if c['default_db_after'] is not None else '（未设置）'}"
            for c in database_context
            if c["default_db"] != c["default_db_after"]
        )
        text, redactions = redact(block.snippet)
        reason = redact(failure_reason)[0] if failure_reason else None
        digest = _fragment_hash(mode, block.snippet, failure_reason, dialect, fragment_db, execution, database_context)
        statement_id = _value(stmt, "id")
        payload: dict[str, Any] = {
            "statement_id": statement_id,
            "lines": [block.line_start, block.line_end],
            "kind": mode,
            "text": text,
            "redactions": redactions,
            "fragment_hash": digest,
            "adapter": adapter,
            "sent_to": adapter if model else "未发送（演示规则）",
            "is_demo": model is None,
            "model_id": execution["model_id"],
            "default_db": fragment_db,
        }
        if reason:
            payload["failure_reason"] = reason
        visible_known = {t: c for t, c in known.items() if t.split(".")[-1] in text}
        payload["prompt"] = (
            operation_prompt(
                text, block.line_start, block.line_end, default_db=fragment_db, database_context=context_prompt
            )
            if mode == "code"
            else candidate_prompt(
                text, block.line_start, visible_known, reason, default_db=fragment_db, database_context=context_prompt
            )
        )
        result.payloads.append(payload)
        entry = cached.get(digest)
        if entry and entry.get("kind") == mode and not entry.get("error") and entry.get("execution") == execution:
            raw = copy.deepcopy(entry.get("result", {}))
            # 缓存的行号相对于片段复用，前面的无关代码增删不会使旧行号越界。
            offset = block.line_start - entry.get("line_start", block.line_start)
            if mode == "code":
                for operation in raw.get("operations", []):
                    operation["line_start"] += offset
                    operation["line_end"] += offset
            else:
                for edge in raw.get("edges", []):
                    edge["evidence_lines"] = [n + offset for n in edge.get("evidence_lines", [])]
            payload.update(
                reused=True,
                sent_to="未发送（复用片段结果）",
                adapter=entry.get("adapter", adapter),
                is_demo=entry.get("is_demo", True),
            )
        else:
            if not budget.reserve(len(text) + len(reason or "")):
                payload.pop("text", None)
                payload.pop("prompt", None)
                payload.update(
                    sent_to="未发送（超过批次上限）", skipped=True, note="模型辅助超过批次上限（200 片段、200 万字符）"
                )
                result.notes.append(f"第 {block.line_start} 至 {block.line_end} 行：{payload['note']}")
                continue
            if model is None:
                payload["note"] = (
                    "模型配置未允许发送代码片段，改用演示规则"
                    if not policy.get("send_code")
                    else "未配置有效模型，使用演示规则"
                )
            try:
                if mode == "code":
                    operations = (
                        model_operations(
                            model.model,
                            text,
                            block.line_start,
                            block.line_end,
                            default_db=fragment_db,
                            database_context=context_prompt,
                        )
                        if model
                        else ModelOperations()
                    )
                    raw = operations.model_dump()
                    if operations.validation_errors:
                        raw["dropped_operations"] = operations.validation_errors
                else:

                    def columns_of(ref):
                        return known.get(qualify_ref(ref, fragment_db, dialect.default_catalog), set())

                    edges = (
                        model_candidates(
                            model.model,
                            text,
                            block.line_start,
                            visible_known,
                            failure_reason=reason,
                            default_db=fragment_db,
                            database_context=context_prompt,
                        )
                        if model
                        else demo_candidates(text, block.line_start, columns_of)
                    )
                    raw = {"edges": [asdict(c) for c in edges]}
            except Exception as exc:  # 片段失败只写说明，不影响已经提交的确定性结果。
                error = f"{type(exc).__name__}: {exc}"
                error = model.safe_error(error, limit=200) if model else modelcfg.redact_error(error, limit=200)
                payload["error"] = f"模型推断失败：{error}"
                if statement_id:
                    result.statement_updates[statement_id] = {"reason": payload["error"]}
                result.notes.append(f"第 {block.line_start} 至 {block.line_end} 行：{payload['error']}")
                continue
        payload["result"] = raw
        result.cache[digest] = {
            "kind": mode,
            "result": copy.deepcopy(raw),
            "line_start": block.line_start,
            "adapter": payload["adapter"],
            "is_demo": payload["is_demo"],
            "execution": execution,
        }
        dropped: list[str] = []
        if mode != "code":
            for values in raw.get("edges", []):
                try:
                    candidate = Candidate(**values)
                    problem = validate_candidate(
                        candidate,
                        known,
                        fragment_db,
                        dialect.default_catalog,
                        text,
                        line_start=block.line_start,
                        line_end=min(block.line_end, file_end),
                    )
                except (TypeError, ValueError) as exc:
                    dropped.append(f"候选结构不合法：{exc}")
                    continue
                if problem:
                    dropped.append(problem)
                    continue
                add_candidate(
                    {
                        "statement_id": statement_id,
                        "statement_seq": _value(stmt, "seq"),
                        "origin": "model",
                        "source_ref": qualify_ref(candidate.source_ref, fragment_db, dialect.default_catalog),
                        "source_column": candidate.source_column,
                        "target_ref": qualify_ref(candidate.target_ref, fragment_db, dialect.default_catalog),
                        "target_column": candidate.target_column,
                        "relation": candidate.relation,
                        "expression": None,
                        "confidence": candidate.confidence,
                        "status": "pending",
                        "evidence": {
                            "lines": candidate.evidence_lines,
                            "rationale": candidate.rationale,
                            "adapter": payload["adapter"],
                            "is_demo": payload["is_demo"],
                            "fragment_hash": digest,
                        },
                    }
                )
        else:
            operations = ModelOperations.model_validate(raw)
            dropped.extend(raw.get("dropped_operations", []))
            # 原文行号决定执行顺序；同一行内保留模型给出的操作顺序。
            for operation in sorted(operations.operations, key=lambda op: op.line_start):
                if not block.line_start <= operation.line_start <= operation.line_end <= min(block.line_end, file_end):
                    dropped.append("操作行号不在代码块或文件范围内")
                    continue
                names = [operation.sql_function] if operation.sql_function else []
                names.extend(operations.suggested_sql_functions)
                for name in names:
                    if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", name) and re.search(
                        r"(?<!\w)" + re.escape(name) + r"\s*\(", block.text
                    ):
                        if name not in result.suggested_sql_functions:
                            result.suggested_sql_functions.append(name)
                if not operation.sql:
                    continue
                operation_db = _default_db_at(contexts, operation.line_start, default_db)
                operation_databases = {operation_db}
                for context in contexts:
                    if operation.line_start <= context["line_start"] <= operation.line_end:
                        operation_databases.add(context["default_db"])
                        operation_databases.add(context["default_db_after"])
                if len(operation_databases) > 1:
                    dropped.append("操作行范围包含默认数据库切换，无法确定还原 SQL 的默认库")
                    continue
                problem = _grounded_tables(operation.sql, dialect, block, known, operation_db)
                if problem:
                    dropped.append(problem)
                    continue
                sql, missing = substitute(operation.sql, options.get("params") or {})
                overlapping = [
                    s
                    for s in rule_statements
                    if _value(s, "method") != "unrecognized"
                    and _value(s, "line_start", 0) <= operation.line_end
                    and _value(s, "line_end", 0) >= operation.line_start
                    and _value(s, "sql_text")
                ]
                canonical = normalized_sql(sql, dialect.name) if not missing else None
                if canonical and any(
                    normalized_sql(_value(s, "sql_text"), dialect.name) == canonical for s in overlapping
                ):
                    payload.setdefault("comparisons", []).append(
                        {
                            "lines": [operation.line_start, operation.line_end],
                            "status": "consistent",
                            "note": "规则与模型一致",
                        }
                    )
                    continue
                comparison = "conflict" if overlapping else "new"
                code = "\n".join(content.splitlines()[operation.line_start - 1 : operation.line_end])
                key = f"assist:{len(result.statements)}"
                parsed = (
                    None
                    if missing
                    else parse_statement(sql, dialect=dialect, schema=model_schema, default_db=operation_db)
                )
                statement = {
                    "id": key,
                    "seq": len(rule_statements) + len(result.statements) + 1,
                    "line_start": operation.line_start,
                    "line_end": operation.line_end,
                    "raw_text": code,
                    "sql_text": operation.sql if missing else sql,
                    "method": "model_sql",
                    "status": "needs_params" if missing else parsed.status,
                    "statement_type": None if parsed is None else parsed.statement_type,
                    "reason": f"还原 SQL 需要补充参数：{', '.join(missing)}" if missing else parsed.reason,
                    "placeholders": placeholders_of(operation.sql),
                }
                result.statements.append(statement)
                if parsed is None or parsed.status in ("failed", "unsupported", "skipped"):
                    continue
                writes = getattr(parsed, "writes", None)
                if writes is None:
                    writes = [parsed] if parsed.target_ref else []
                values: list[dict[str, Any]] = []
                for write in writes:
                    for edge in write.edges:
                        if edge.source_ref not in known:
                            dropped.append(f"来源表 {edge.source_ref} 未采集，也不是本批次产生的表")
                            continue
                        values.append(
                            {
                                "statement_id": key,
                                "statement_seq": statement["seq"],
                                "origin": "model_sql",
                                "source_ref": edge.source_ref,
                                "source_column": edge.source_column,
                                "target_ref": write.target_ref,
                                "target_column": edge.target_column,
                                "relation": edge.relation,
                                "expression": edge.expression,
                                "confidence": operation.confidence,
                                "status": "pending",
                                "evidence": {
                                    "lines": [operation.line_start, operation.line_end],
                                    "original_code": code,
                                    "restored_sql": operation.sql,
                                    "rationale": operation.rationale,
                                    "comparison": comparison,
                                    "adapter": payload["adapter"],
                                    "is_demo": payload["is_demo"],
                                    "fragment_hash": digest,
                                    "note": "与规则结果不一致，优先复核" if overlapping else "规则未抽取到的新候选",
                                },
                            }
                        )
                for value in values:
                    add_candidate(value)
                for write in writes:
                    if write.status in ("failed", "unsupported", "skipped"):
                        continue
                    catalog, db, name = write.target_ref.split(".")
                    tables = model_schema.setdefault(catalog, {}).setdefault(db, {})
                    if write.created or name not in tables:
                        tables[name] = {column: "VARCHAR" for column in write.target_columns}
                    else:
                        tables[name].update(
                            {column: tables[name].get(column, "VARCHAR") for column in write.target_columns}
                        )
                    known[write.target_ref] = set(tables[name])
        if dropped:
            payload["dropped"] = dropped
            result.notes.extend(f"第 {block.line_start} 至 {block.line_end} 行：{problem}" for problem in dropped)
    return result
