"""从 SQL 脚本、Python、Shell 中抽取语句与数据操作片段（确定性，带行号）。

- SQL：按分号切分（识别引号与注释），多语句按顺序处理；
- Python：用 ast 找 spark.sql / cursor.execute / text / read_sql 等调用的字符串参数
  （普通字符串、三引号、f-string、字面量拼接、.format），f-string 与 .format 的占位符记为待补参数；
  同一函数中不含 SQL、但有 DataFrame 读写（spark.table / saveAsTable / insertInto）的语句块作为“未识别片段”；
- Shell：heredoc（<<EOF … EOF）与 -e "…" 参数中的 SQL，${var} 记为待补参数。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from enum import Enum

SQL_CALLS = {"sql", "execute", "executemany", "text", "read_sql", "read_sql_query"}
DATAFRAME_MARKERS = ("spark.table(", "read.table(", "saveAsTable(", "insertInto(", ".join(")
PLACEHOLDER = re.compile(r"\$\{(\w+)\}|\{(\w+)\}|%\((\w+)\)s")
SHELL_SQL_COMMANDS = re.compile(r"\b(mysql|starrocks|beeline|hive|spark-sql|psql)\b")


@dataclass
class Segment:
    line_start: int
    line_end: int
    method: str  # sql | python_ast | shell | unrecognized
    text: str
    placeholders: list[str] = field(default_factory=list)


def placeholders_of(text: str) -> list[str]:
    names = []
    for m in PLACEHOLDER.finditer(text):
        name = next(g for g in m.groups() if g)
        if name not in names:
            names.append(name)
    return names


def substitute(text: str, params: dict[str, str]) -> tuple[str, list[str]]:
    missing: list[str] = []

    def rep(m: re.Match) -> str:
        name = next(g for g in m.groups() if g)
        if name in params:
            return str(params[name])
        if name not in missing:
            missing.append(name)
        return m.group(0)

    return PLACEHOLDER.sub(rep, text), missing


def split_sql(text: str, line_offset: int = 0) -> list[Segment]:
    """按分号切分 SQL，忽略引号与注释中的分号；返回每条语句的行号范围。"""
    segments, buf = [], []
    line, start_line = 1, None
    i, n = 0, len(text)
    quote = None
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if quote:
            buf.append(ch)
            if ch == "\\" and quote != "`":
                buf.append(nxt)
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch == "-" and nxt == "-" or ch == "#":
            end = text.find("\n", i)
            end = n if end < 0 else end
            i = end
            continue
        elif ch == "/" and nxt == "*":
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            line += text.count("\n", i, end)
            i = end
            continue
        elif ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            start_line = start_line or line
        elif ch == ";":
            statement = "".join(buf).strip()
            if statement:
                segments.append(Segment(start_line + line_offset, line + line_offset, "sql", statement,
                                        placeholders_of(statement)))
            buf, start_line = [], None
        else:
            if not ch.isspace() and start_line is None:
                start_line = line
            buf.append(ch)
        if ch == "\n":
            line += 1
        i += 1
    statement = "".join(buf).strip()
    if statement:
        segments.append(Segment(start_line + line_offset, line + line_offset, "sql", statement,
                                placeholders_of(statement)))
    return segments


# --- Python -----------------------------------------------------------------------

class _Unknown(Enum):
    VALUE = "uncertain"


Bindings = dict[str, str | None | _Unknown]


def _string_value(node: ast.AST, constants: Bindings) -> str | None:
    """把字符串表达式还原为 SQL 文本；动态部分记为 {name} 占位符。无法还原返回 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        value = constants.get(node.id)
        return value if isinstance(value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                if any(isinstance(n, ast.Name) and constants.get(n.id) is _Unknown.VALUE
                       for n in ast.walk(v.value)):
                    return None
                parts.append("{" + re.sub(r"\W+", "_", ast.unparse(v.value)).strip("_") + "}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _string_value(node.left, constants), _string_value(node.right, constants)
        if left is not None and right is not None:
            return left + right
        return None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        if any(isinstance(n, ast.Name) and constants.get(n.id) is _Unknown.VALUE
               for value in [*node.args, *(kw.value for kw in node.keywords)] for n in ast.walk(value)):
            return None
        return _string_value(node.func.value, constants)
    return None


def _contains_sql_call(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in SQL_CALLS
               for n in ast.walk(node))


def _assigned_names(nodes: list[ast.stmt]) -> set[str]:
    """只收集当前作用域的绑定；嵌套函数、类和推导式有自己的作用域。"""
    names: set[str] = set()

    class Bindings(ast.NodeVisitor):
        def visit_Name(self, node):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                names.add(node.id)

        def visit_FunctionDef(self, node):
            names.add(node.name)

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_ClassDef = visit_FunctionDef

        def visit_Lambda(self, node):
            pass

        visit_ListComp = visit_Lambda
        visit_SetComp = visit_Lambda
        visit_DictComp = visit_Lambda
        visit_GeneratorExp = visit_Lambda

        def visit_Import(self, node):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)

        def visit_ImportFrom(self, node):
            names.update(alias.asname or alias.name for alias in node.names)

        def visit_ExceptHandler(self, node):
            if node.name:
                names.add(node.name)
            self.generic_visit(node)

    visitor = Bindings()
    for node in nodes:
        visitor.visit(node)
    return names


def extract_python(source: str) -> tuple[list[Segment], list[str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [], [f"Python 语法错误（第 {exc.lineno} 行）：{exc.msg}"]
    lines = source.splitlines()
    segments: list[Segment] = []
    notes: list[str] = []

    def binding_value(node: ast.AST, bindings: Bindings) -> str | None | _Unknown:
        value = _string_value(node, bindings)
        if value is None and any(isinstance(n, ast.Name) and bindings.get(n.id) is _Unknown.VALUE
                                 for n in ast.walk(node)):
            return _Unknown.VALUE
        return value

    def expression(node: ast.AST | None, bindings: Bindings) -> None:
        if node is None:
            return
        if isinstance(node, ast.Lambda):
            local = dict(bindings)
            args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                    node.args.vararg, node.args.kwarg]
            local.update({arg.arg: None for arg in args if arg is not None})
            expression(node.body, local)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            local = dict(bindings)
            for generator in node.generators:
                expression(generator.iter, local)
                bind(generator.target, None, local)
                for condition in generator.ifs:
                    expression(condition, local)
            if isinstance(node, ast.DictComp):
                expression(node.key, local)
                expression(node.value, local)
            else:
                expression(node.elt, local)
            return
        if isinstance(node, ast.NamedExpr):
            expression(node.value, bindings)
            bind(node.target, binding_value(node.value, bindings), bindings)
            return
        if isinstance(node, ast.IfExp):
            expression(node.test, bindings)
            paths = [dict(bindings), dict(bindings)]
            expression(node.body, paths[0])
            expression(node.orelse, paths[1])
            merge(bindings, paths)
            return
        if isinstance(node, ast.BoolOp):
            expression(node.values[0], bindings)
            for value in node.values[1:]:
                optional = dict(bindings)
                expression(value, optional)
                merge(bindings, [dict(bindings), optional])
            return
        for child in ast.iter_child_nodes(node):
            expression(child, bindings)
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in SQL_CALLS and node.args):
            return
        text = _string_value(node.args[0], bindings)
        if text is None:
            notes.append(f"第 {node.lineno} 行的 {node.func.attr}() 参数无法确定，已交给模型候选流程")
            segments.append(Segment(node.lineno, node.end_lineno or node.lineno, "unrecognized",
                                    "\n".join(lines[node.lineno - 1:(node.end_lineno or node.lineno)])))
        else:
            for seg in split_sql(text):
                segments.append(Segment(node.lineno, node.end_lineno or node.lineno, "python_ast", seg.text,
                                        seg.placeholders))

    def bind(target: ast.AST, value: str | None | _Unknown, bindings: Bindings) -> None:
        if isinstance(target, ast.Name):
            bindings[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                bind(item, _Unknown.VALUE if value is _Unknown.VALUE else None, bindings)

    def merge(bindings: Bindings, paths: list[Bindings]) -> None:
        for name in set().union(*(path.keys() for path in paths)):
            values = [path.get(name) for path in paths]
            bindings[name] = values[0] if all(value == values[0] for value in values) else _Unknown.VALUE

    def statements(body: list[ast.stmt], bindings: Bindings) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for decorator in stmt.decorator_list:
                    expression(decorator, bindings)
                local = dict(bindings)
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # 外层绑定在调用时可能变化，函数和闭包不能固定为定义点的快照。
                    local = dict.fromkeys(bindings, _Unknown.VALUE)
                    local.update(dict.fromkeys(_assigned_names(stmt.body), _Unknown.VALUE))
                    for default in [*stmt.args.defaults, *stmt.args.kw_defaults]:
                        expression(default, bindings)
                    args = [*stmt.args.posonlyargs, *stmt.args.args, *stmt.args.kwonlyargs,
                            stmt.args.vararg, stmt.args.kwarg]
                    local.update({arg.arg: None for arg in args if arg is not None})
                statements(stmt.body, local)
                bindings[stmt.name] = None
            elif isinstance(stmt, ast.If):
                expression(stmt.test, bindings)
                paths = [dict(bindings), dict(bindings)]
                statements(stmt.body, paths[0])
                statements(stmt.orelse, paths[1])
                merge(bindings, paths)
            elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
                loop = dict(bindings)
                loop.update(dict.fromkeys(_assigned_names(stmt.body), _Unknown.VALUE))
                if isinstance(stmt, (ast.For, ast.AsyncFor)):
                    expression(stmt.iter, bindings)
                    bind(stmt.target, None, loop)
                else:
                    expression(stmt.test, loop)
                statements(stmt.body, loop)
                merge(bindings, [dict(bindings), loop])
                # 循环可以执行零次、继续或提前退出，退出点的赋值不一定是最后一条。
                bindings.update(dict.fromkeys(_assigned_names(stmt.body), _Unknown.VALUE))
                alternate = dict(bindings)
                statements(stmt.orelse, alternate)
                merge(bindings, [dict(bindings), alternate])
            elif isinstance(stmt, (ast.Try, ast.TryStar)):
                normal = dict(bindings)
                statements(stmt.body, normal)
                statements(stmt.orelse, normal)
                paths = [normal]
                for handler in stmt.handlers:
                    failed = dict(bindings)
                    # 异常可能在 try 中任意语句抛出，不能沿用被覆盖前的值。
                    failed.update(dict.fromkeys(_assigned_names(stmt.body), _Unknown.VALUE))
                    if handler.name:
                        failed[handler.name] = None
                    expression(handler.type, failed)
                    statements(handler.body, failed)
                    if handler.name:
                        failed[handler.name] = None
                    paths.append(failed)
                merge(bindings, paths)
                if stmt.finalbody:
                    # finally 也会在未匹配的异常路径上执行。
                    changed = [*stmt.body, *stmt.orelse]
                    for handler in stmt.handlers:
                        changed.extend(handler.body)
                    bindings.update(dict.fromkeys(_assigned_names(changed), _Unknown.VALUE))
                    statements(stmt.finalbody, bindings)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                for item in stmt.items:
                    expression(item.context_expr, bindings)
                    if item.optional_vars:
                        bind(item.optional_vars, None, bindings)
                statements(stmt.body, bindings)
            elif isinstance(stmt, ast.Match):
                expression(stmt.subject, bindings)
                paths = [dict(bindings)]
                for case in stmt.cases:
                    branch = dict(bindings)
                    for pattern in ast.walk(case.pattern):
                        name = getattr(pattern, "name", None)
                        if name:
                            branch[name] = None
                    expression(case.guard, branch)
                    statements(case.body, branch)
                    paths.append(branch)
                merge(bindings, paths)
            elif isinstance(stmt, ast.Assign):
                expression(stmt.value, bindings)
                value = binding_value(stmt.value, bindings)
                for target in stmt.targets:
                    bind(target, value, bindings)
            elif isinstance(stmt, ast.AnnAssign):
                expression(stmt.value, bindings)
                bind(stmt.target, binding_value(stmt.value, bindings) if stmt.value else None, bindings)
            elif isinstance(stmt, ast.AugAssign):
                expression(stmt.value, bindings)
                value = _string_value(stmt.target, bindings)
                right = _string_value(stmt.value, bindings)
                bind(stmt.target, value + right if isinstance(stmt.op, ast.Add)
                     and value is not None and right is not None else None, bindings)
            else:
                expression(stmt, bindings)
                for name in _assigned_names([stmt]):
                    bindings[name] = None

    statements(tree.body, {})

    def blocks(body: list[ast.stmt]) -> None:
        current: list[ast.stmt] = []

        def flush() -> None:
            if current:
                start, end = current[0].lineno, current[-1].end_lineno or current[-1].lineno
                text = "\n".join(lines[start - 1:end])
                if any(marker in text for marker in DATAFRAME_MARKERS):
                    segments.append(Segment(start, end, "unrecognized", text))
            current.clear()

        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                flush()
                blocks(stmt.body)
            elif _contains_sql_call(stmt) or isinstance(stmt, (ast.Import, ast.ImportFrom)):
                flush()
            else:
                current.append(stmt)
        flush()

    blocks(tree.body)
    segments.sort(key=lambda s: (s.line_start, s.method != "python_ast"))
    return segments, notes


# --- Shell ------------------------------------------------------------------------

HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")
DASH_E = re.compile(r"""-e\s+(["'])(.*?)(?<!\\)\1""", re.S)


def extract_shell(source: str) -> tuple[list[Segment], list[str]]:
    lines = source.splitlines()
    segments: list[Segment] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = HEREDOC.search(line)
        if m and SHELL_SQL_COMMANDS.search(line):
            delimiter, body, j = m.group(1), [], i + 1
            while j < len(lines) and lines[j].strip() != delimiter:
                body.append(lines[j])
                j += 1
            for seg in split_sql("\n".join(body), line_offset=i + 1):
                seg.method = "shell"
                segments.append(seg)
            i = j + 1
            continue
        if SHELL_SQL_COMMANDS.search(line):
            for em in DASH_E.finditer(line):
                for seg in split_sql(em.group(2), line_offset=i):
                    seg.method = "shell"
                    segments.append(seg)
        i += 1
    return segments, []
