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

def _string_value(node: ast.AST, constants: dict[str, str]) -> str | None:
    """把字符串表达式还原为 SQL 文本；动态部分记为 {name} 占位符。无法还原返回 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                parts.append("{" + re.sub(r"\W+", "_", ast.unparse(v.value)).strip("_") + "}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _string_value(node.left, constants), _string_value(node.right, constants)
        if left is not None and right is not None:
            return left + right
        return None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return _string_value(node.func.value, constants)
    return None


def _contains_sql_call(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in SQL_CALLS
               for n in ast.walk(node))


def extract_python(source: str) -> tuple[list[Segment], list[str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [], [f"Python 语法错误（第 {exc.lineno} 行）：{exc.msg}"]
    lines = source.splitlines()
    constants = {t.id: n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                 for t in n.targets if isinstance(t, ast.Name)
                 if isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)}
    segments: list[Segment] = []
    notes: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in SQL_CALLS):
            continue
        if not node.args:
            continue
        text = _string_value(node.args[0], constants)
        if text is None:
            notes.append(f"第 {node.lineno} 行的 {node.func.attr}() 参数无法静态还原为 SQL")
            segments.append(Segment(node.lineno, node.end_lineno or node.lineno, "unrecognized",
                                    "\n".join(lines[node.lineno - 1:(node.end_lineno or node.lineno)])))
            continue
        for seg in split_sql(text):
            segments.append(Segment(node.lineno, node.end_lineno or node.lineno, "python_ast", seg.text,
                                    seg.placeholders))

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
