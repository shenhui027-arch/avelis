"""DDL 审计事件（ADR 0008）：连接器从数据库的审计来源读出“谁在什么时候对哪个对象执行了什么 DDL”。

连接器只读审计来源，不开启、不修改它。读不到时抛 AuditUnavailable，并按数据库原生错误给出原因（cause）：
no_permission 采集账号无权读取、not_enabled 未开启（审计来源不存在或已停用）、
incompatible 审计来源结构与连接器要求不符、error 其他。不返回空列表冒充“没人改”。
是否开启、是否授权由数据源方负责，这里只如实说明。

落库只保存脱敏后的单条语句摘要：一条审计记录里有多条语句（如 PostgreSQL 的 current_query() 是整个请求），
语句里可能有口令等敏感值，或词法上无法确定（未闭合的注释、字符串、引用）时，摘要为空，只保存命令类型和对象。
原文只在内存里用于匹配列名。

列级证据只来自能识别语法位置的写法（ALTER TABLE … ADD / DROP / ALTER / MODIFY / CHANGE / RENAME COLUMN、
COMMENT ON COLUMN、ClickHouse 的 COMMENT / CLEAR / MATERIALIZE COLUMN）；约束名、索引名不算，
识别不了的语句只作为对象级候选，不提供列级证据。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

SUMMARY_LIMIT = 300
CAUSES = ("no_permission", "not_enabled", "incompatible", "instance_changed", "error")
# 可能带敏感值的写法：口令、密钥、凭据，以及无法可靠识别边界的引用（任何美元引用 $…$、E'' 转义串）
_SENSITIVE = re.compile(r"\b(PASSWORD|IDENTIFIED|SECRET|CREDENTIALS?|TOKEN|ACCESS_KEY|SECRET_KEY)\b|"
                        r"\$(?:[^\W\d]\w*)?\$|(?<![\w'])[Ee]'", re.IGNORECASE)
_SPACE = re.compile(r"\s+")
_WORD = re.compile(r"[^\W\d][\w$]*")  # 标识符：Unicode 字母或下划线开头（含中文）
_DOLLAR = re.compile(r"\$(?:[^\W\d]\w*)?\$")


@dataclass
class DdlEvent:
    at: datetime  # UTC，不带时区：语句执行（或生效）时间
    db_user: str
    objects: list[tuple[str, str]]  # (库或模式, 对象名)，按数据库目录中的真实写法（区分大小写）
    statement_type: str
    statement: str = field(repr=False)  # 原文只用于匹配列名，不落库；落库的是 summary
    # 是否确定发生在两次快照之间：True / False 由连接器按事务可见性给出；None 由归因按时间与窗口边界判断
    certain: bool | None = None
    # 是否可能晚于语句时间才生效（如 Doris 的异步 schema change 作业）：上次快照之前 lag 秒内的这类语句也算边界候选
    may_lag: bool = False

    @property
    def summary(self) -> str | None:
        return summarize(self.statement)


class AuditUnavailable(Exception):
    """审计来源读取失败。message 保留数据库原始报错；cause 见模块说明。"""

    def __init__(self, message: str, cause: str = "error"):
        super().__init__(message)
        self.cause = cause if cause in CAUSES else "error"


# --- 词法 -------------------------------------------------------------------------

@dataclass(frozen=True)
class Token:
    kind: str  # ident | dot | semi | comma | lparen | rparen | other
    text: str = ""
    quoted: bool = False


@dataclass
class Lexed:
    tokens: list[Token]
    certain: bool  # False：有未闭合的注释、字符串或引用，词法结果不可靠


def lex(statement: str) -> Lexed:
    """SQL 词法（够用即可）：跳过注释（含 PostgreSQL 的嵌套块注释）与字符串字面量（'' 转义、E'' 反斜杠转义、
    $tag$ 美元引用），识别带引号的标识符（"" ` [] ）、Unicode 标识符与标点。遇到未闭合的结构时标记为不确定。"""
    s, i, n, out, certain = statement or "", 0, len(statement or ""), [], True
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
        elif s.startswith("--", i):
            j = s.find("\n", i)
            i = n if j < 0 else j + 1
        elif s.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if s.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif s.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            certain = certain and depth == 0
        elif c == "'" or (c in "Ee" and i + 1 < n and s[i + 1] == "'" and (i == 0 or not (s[i - 1].isalnum()
                                                                                         or s[i - 1] in "_$"))):
            backslash = c in "Ee"
            i += 2 if backslash else 1
            closed = False
            while i < n:
                if backslash and s[i] == "\\":
                    i += 2
                    continue
                if s[i] == "'":
                    if i + 1 < n and s[i + 1] == "'":
                        i += 2
                        continue
                    i, closed = i + 1, True
                    break
                i += 1
            certain = certain and closed
            out.append(Token("other"))
        elif c == "$" and (m := _DOLLAR.match(s, i)):
            tag = m.group(0)
            j = s.find(tag, m.end())
            certain = certain and j >= 0
            i = n if j < 0 else j + len(tag)
            out.append(Token("other"))
        elif c in "\"`[":
            close = {"\"": "\"", "`": "`", "[": "]"}[c]
            j, buf, closed = i + 1, [], False
            while j < n:
                if s[j] == close:
                    if j + 1 < n and s[j + 1] == close and close != "]":
                        buf.append(close)
                        j += 2
                        continue
                    closed = True
                    break
                buf.append(s[j])
                j += 1
            certain = certain and closed
            out.append(Token("ident", "".join(buf), quoted=True))
            i = j + 1
        elif m := _WORD.match(s, i):
            out.append(Token("ident", m.group(0)))
            i = m.end()
        else:
            out.append(Token({".": "dot", ";": "semi", ",": "comma", "(": "lparen", ")": "rparen"}.get(c, "other")))
            i += 1
    return Lexed(out, certain)


def tokens(statement: str) -> list[Token]:
    return lex(statement).tokens


def _statements(toks: list[Token]) -> list[list[Token]]:
    out, cur = [], []
    for t in toks:
        if t.kind == "semi":
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


def is_batch(statement: str) -> bool:
    """一条审计记录里是否有多条语句（字符串、注释里的分号不算）。词法上无法确定时按多语句处理。"""
    lexed = lex(statement)
    return not lexed.certain or len(_statements(lexed.tokens)) > 1


def summarize(statement: str) -> str | None:
    """可落库的摘要：单条语句、词法确定、看不出敏感值时压缩空白并截断；否则返回 None（只保存命令类型和对象）。"""
    if not statement or is_batch(statement) or _SENSITIVE.search(statement):
        return None
    s = _SPACE.sub(" ", statement).strip()
    return s if len(s) <= SUMMARY_LIMIT else s[:SUMMARY_LIMIT - 1] + "…"


# --- 列的语法位置 ------------------------------------------------------------------

def _kw(t: Token | None, *words: str) -> bool:
    return t is not None and t.kind == "ident" and not t.quoted and t.text.upper() in words


def _same(token: Token, name: str, fold: str) -> bool:
    if token.quoted or fold == "exact":
        return token.text == name
    if fold == "lower":  # PostgreSQL：不带引号的标识符折成小写
        return token.text.lower() == name
    return token.text.lower() == name.lower()  # 不区分大小写的引擎


def _name(toks: list[Token], i: int) -> tuple[list[Token], int]:
    """从 i 读一个限定名 a.b.c，返回 (各段, 下一个位置)。"""
    parts: list[Token] = []
    while i < len(toks) and toks[i].kind == "ident":
        parts.append(toks[i])
        i += 1
        if i < len(toks) and toks[i].kind == "dot":
            i += 1
        else:
            break
    return parts, i


def _skip(toks: list[Token], i: int, *seq: str) -> int:
    """跳过可选的关键字序列（如 IF NOT EXISTS）。"""
    j = i
    for w in seq:
        if not _kw(toks[j] if j < len(toks) else None, w):
            return i
        j += 1
    return j


def _actions(toks: list[Token]) -> list[list[Token]]:
    """ALTER TABLE 后的各个动作（括号外的逗号分隔）。"""
    out, cur, depth = [], [], 0
    for t in toks:
        if t.kind == "lparen":
            depth += 1
        elif t.kind == "rparen":
            depth -= 1
        if t.kind == "comma" and depth == 0:
            out.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


_NOT_COLUMN = {"CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "INDEX", "KEY", "PARTITION", "PARTITIONS",
               "ROLLUP", "PROJECTION", "STATISTICS", "TEMPORARY", "EXCLUDE", "OWNER", "DEFAULT", "COMMENT", "ORDER",
               "TTL", "SETTING", "SETTINGS", "QUERY", "ENGINE", "PROPERTIES", "DISTRIBUTION", "SAMPLE"}


def _action_columns(a: list[Token]) -> list[Token] | None:
    """一个 ALTER TABLE 动作点名的列；None 表示这个动作不涉及列（或识别不了）。"""
    if not a:
        return None
    verb = a[0].text.upper() if a[0].kind == "ident" and not a[0].quoted else ""
    i = 1
    if verb in ("ADD", "DROP", "ALTER", "MODIFY", "CLEAR", "MATERIALIZE"):
        if i < len(a) and _kw(a[i], *_NOT_COLUMN):
            return None
        i = _skip(a, i, "COLUMN")
        if verb in ("CLEAR", "MATERIALIZE") and i == 1:
            return None  # ClickHouse：CLEAR / MATERIALIZE 只认 … COLUMN 写法（另有 INDEX、PROJECTION）
        i = _skip(a, i, "IF", "NOT", "EXISTS")
        i = _skip(a, i, "IF", "EXISTS")
        if i < len(a) and a[i].kind == "lparen" and verb == "ADD":  # Doris：ADD COLUMN (a INT, b INT)
            cols, depth, expect = [], 0, True
            for t in a[i:]:
                if t.kind == "lparen":
                    depth += 1
                    continue
                if t.kind == "rparen":
                    depth -= 1
                    continue
                if depth == 1 and t.kind == "comma":
                    expect = True
                    continue
                if depth == 1 and expect and t.kind == "ident":
                    cols.append(t)
                    expect = False
                elif depth == 1:
                    expect = False
            return cols or None
        return [a[i]] if i < len(a) and a[i].kind == "ident" else None
    if verb == "CHANGE":  # MySQL：CHANGE [COLUMN] old new type
        i = _skip(a, i, "COLUMN")
        return [t for t in a[i:i + 2] if t.kind == "ident"] or None
    if verb == "RENAME":
        if not _kw(a[i] if i < len(a) else None, "COLUMN"):
            return None  # RENAME TO（改表名）、RENAME CONSTRAINT 等
        i += 1
        old = a[i] if i < len(a) and a[i].kind == "ident" else None
        j = _skip(a, i + 1, "TO")
        new = a[j] if j < len(a) and a[j].kind == "ident" else None
        return [t for t in (old, new) if t is not None] or None
    if verb == "COMMENT" and _kw(a[i] if i < len(a) else None, "COLUMN"):  # ClickHouse：COMMENT COLUMN c '…'
        i = _skip(a, i + 1, "IF", "EXISTS")
        return [a[i]] if i < len(a) and a[i].kind == "ident" else None
    return None


def column_refs(statement: str, object_name: str, fold: str = "insensitive") -> list[Token] | None:
    """语句中对 object_name 点名的列（按语法位置）。只认 ALTER TABLE 的列动作与 COMMENT ON COLUMN；
    其他写法返回 None（不提供列级证据）。多语句时只看目标是该对象的那几条。"""
    lexed = lex(statement)
    found: list[Token] = []
    recognized = False
    for st in _statements(lexed.tokens):
        if _kw(st[0] if st else None, "ALTER") and _kw(st[1] if len(st) > 1 else None, "TABLE"):
            i = _skip(st, 2, "IF", "EXISTS")
            i = _skip(st, i, "ONLY")
            parts, i = _name(st, i)
            if not parts or not _same(parts[-1], object_name, fold):
                continue
            recognized = True
            for action in _actions(st[i:]):
                found += _action_columns(action) or []
        elif (_kw(st[0] if st else None, "COMMENT") and _kw(st[1] if len(st) > 1 else None, "ON")
              and _kw(st[2] if len(st) > 2 else None, "COLUMN")):
            parts, _ = _name(st, 3)
            if len(parts) >= 2 and _same(parts[-2], object_name, fold):
                recognized = True
                found.append(parts[-1])
    return found if recognized else None


def mentions_column(statement: str, column: str, object_name: str | None = None, fold: str = "insensitive") -> bool:
    """语句是否在列的语法位置上点名了该列。识别不了的写法返回 False（由调用方按整体候选或未知处理）。"""
    if object_name is None:
        return False
    refs = column_refs(statement, object_name, fold)
    return bool(refs) and any(_same(t, column, fold) for t in refs)


# --- 对象 -------------------------------------------------------------------------

_IDENT = r"(?:`[^`]+`|\"[^\"]+\"|\[[^\]]+\]|[\w$]+)"
_NAME = rf"{_IDENT}(?:\s*\.\s*{_IDENT}){{0,2}}"
_OBJECT = re.compile(
    rf"^\s*(?:CREATE|ALTER|DROP|TRUNCATE)\s+(?:OR\s+REPLACE\s+)?(?:TEMPORARY\s+|EXTERNAL\s+)?"
    rf"(?:TABLE|VIEW|MATERIALIZED\s+VIEW)\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?(?P<name>{_NAME})"
    rf"(?P<rest>.*)$", re.IGNORECASE | re.DOTALL)
_RENAME = re.compile(rf"^\s*RENAME\s+(?:TO\s+)?(?P<name>{_NAME})", re.IGNORECASE)
_RENAME_TABLE = re.compile(rf"^\s*RENAME\s+TABLE\s+(?P<a>{_NAME})\s+TO\s+(?P<b>{_NAME})", re.IGNORECASE)


def _unquote(part: str) -> str:
    part = part.strip()
    if part[:1] in "`\"[" and len(part) > 1:
        return part[1:-1]
    return part


def _split(name: str, default_db: str | None) -> tuple[str, str] | None:
    parts = [_unquote(p) for p in re.findall(_IDENT, name)]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return (default_db, parts[0]) if default_db else None


def objects_in(statement: str, default_db: str | None = None) -> list[tuple[str, str]]:
    """从单条 DDL 语句中解析对象名（用于不在审计记录里给出对象的来源，如 Doris）。多条语句的记录不解析。
    只认 CREATE / ALTER / DROP TABLE|VIEW 与 RENAME TABLE；ALTER … RENAME 时新旧名都返回。"""
    if is_batch(statement or ""):
        return []
    out: list[tuple[str, str]] = []
    m = _RENAME_TABLE.match(statement or "")
    if m:
        for g in ("a", "b"):
            obj = _split(m.group(g), default_db)
            if obj:
                out.append(obj)
        return out
    m = _OBJECT.match(statement or "")
    if not m:
        return out
    obj = _split(m.group("name"), default_db)
    if obj is None:
        return out
    out.append(obj)
    rest = m.group("rest") or ""
    r = _RENAME.match(rest)
    if r:
        new = _split(r.group("name"), None)
        out.append((obj[0], new[1]) if new else (obj[0], _unquote(r.group("name"))))
    return out


_REDEFINE = re.compile(r"^\s*(?:CREATE\b|ALTER\s+(?:MATERIALIZED\s+)?VIEW\b.*\bAS\b)", re.IGNORECASE | re.DOTALL)


def redefines(statement: str) -> bool:
    """单条语句是否整体重定义对象（CREATE …、ALTER VIEW … AS）：这类语句不一定逐列点名。"""
    return not is_batch(statement or "") and bool(_REDEFINE.match(statement or ""))
