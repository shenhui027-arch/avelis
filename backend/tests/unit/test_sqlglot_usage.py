"""sqlglot 的接入用法（防回流）：上游要求明确源方言，默认方言不在支持范围（https://github.com/tobymao/sqlglot/issues/8845）。

用 ast 扫描 backend/src，下面几种写法出现就失败（能静态判断的才判断，判断不了的不报）：
- sqlglot.parse / parse_one / transpile 没传 read=（或 dialect=）；
- qualify(...) 没传 dialect=；
- 在导入了 sqlglot 的模块里，对表达式调用 .sql() 时没有任何参数（会按默认方言生成）。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "avelis"
PARSERS = {"parse", "parse_one", "transpile"}


def _keywords(call: ast.Call) -> set[str | None]:
    return {k.arg for k in call.keywords}  # None 表示 **kwargs：判断不了，不报


def problems(source: str, filename: str = "<src>") -> list[str]:
    tree = ast.parse(source, filename)
    imported_parsers: set[str] = set()  # from sqlglot import parse_one 之类导入的名字
    imported_qualify: set[str] = set()
    uses_sqlglot = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "sqlglot":
            uses_sqlglot = True
            for alias in node.names:
                if node.module == "sqlglot" and alias.name in PARSERS:
                    imported_parsers.add(alias.asname or alias.name)
                if alias.name == "qualify" and node.module.startswith("sqlglot.optimizer"):
                    imported_qualify.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import) and any(a.name.split(".")[0] == "sqlglot" for a in node.names):
            uses_sqlglot = True
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func, kw = node.func, _keywords(node)
        where = f"{filename}:{node.lineno}"
        if None in kw:
            continue
        is_parser = (isinstance(func, ast.Attribute) and func.attr in PARSERS and isinstance(func.value, ast.Name)
                     and func.value.id == "sqlglot") or (isinstance(func, ast.Name) and func.id in imported_parsers)
        if is_parser and not kw & {"read", "dialect"}:
            out.append(f"{where} sqlglot 解析没有传 read= / dialect=")
        if isinstance(func, ast.Name) and func.id in imported_qualify and "dialect" not in kw:
            out.append(f"{where} qualify 没有传 dialect=")
        if (uses_sqlglot and isinstance(func, ast.Attribute) and func.attr == "sql" and not node.args
                and not node.keywords):
            out.append(f"{where} .sql() 没有传方言")
    return out


def test_backend_sources_always_name_the_sqlglot_dialect():
    found = [p for path in sorted(SRC.rglob("*.py"))
             for p in problems(path.read_text(encoding="utf-8"), str(path.relative_to(SRC)))]
    assert found == []


def test_the_scanner_flags_each_pattern_and_skips_what_it_cannot_judge():
    bad = (
        "import sqlglot\n"
        "from sqlglot import parse_one as p1\n"
        "from sqlglot.optimizer.qualify import qualify\n"
        "sqlglot.parse_one(sql)\n"
        "sqlglot.transpile(sql, write='postgres')\n"
        "p1(sql)\n"
        "qualify(tree, schema=s)\n"
        "node.sql()\n")
    assert [p.split(" ", 1)[1] for p in problems(bad)] == [
        "sqlglot 解析没有传 read= / dialect=", "sqlglot 解析没有传 read= / dialect=", "sqlglot 解析没有传 read= / dialect=",
        "qualify 没有传 dialect=", ".sql() 没有传方言"]
    good = (
        "import sqlglot\n"
        "from sqlglot.optimizer.qualify import qualify\n"
        "sqlglot.parse_one(sql, read='postgres')\n"
        "sqlglot.parse(sql, dialect=d)\n"
        "sqlglot.transpile(sql, read='postgres', write='starrocks')\n"
        "qualify(tree, dialect='postgres')\n"
        "node.sql(dialect='postgres')\n"
        "node.sql('postgres')\n"
        "sqlglot.parse_one(sql, **options)\n")  # **kwargs：判断不了，不报
    assert problems(good) == []
    assert problems("thing.sql()\n") == []  # 没有导入 sqlglot 的模块：.sql() 不一定是 sqlglot 表达式
