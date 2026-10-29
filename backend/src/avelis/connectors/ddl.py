"""DDL 审计事件（ADR 0008）：连接器从数据库的审计来源读出“谁在什么时候对哪个对象执行了什么 DDL”。

连接器只读审计来源，不开启、不修改它。读不到时抛 AuditUnavailable，并按数据库原生错误给出原因（cause）：
no_permission 采集账号无权读取、not_enabled 未开启（审计来源不存在或已停用）、
incompatible 审计来源结构与连接器要求不符、
error 其他。不返回空列表冒充“没人改”。是否开启、是否授权由数据源方负责，这里只如实说明。

落库只保存脱敏后的单条语句摘要：一条审计记录里有多条语句（如 PostgreSQL 的 current_query() 是整个请求），
或语句里可能有口令等敏感值时，摘要为空，只保存命令类型和对象。原文只在内存里用于匹配列名。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

SUMMARY_LIMIT = 300
CAUSES = ("no_permission", "not_enabled", "incompatible", "error")
# 可能带敏感值的写法：口令、密钥、凭据，以及无法可靠识别边界的引用（美元引用、E'' 转义串）
_SENSITIVE = re.compile(r"\b(PASSWORD|IDENTIFIED|SECRET|CREDENTIALS?|TOKEN|ACCESS_KEY|SECRET_KEY)\b|\$[A-Za-z_]*\$|"
                        r"(?<![\w'])[Ee]'", re.IGNORECASE)
_SPACE = re.compile(r"\s+")


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
    kind: str  # ident | dot | semi | other
    text: str = ""
    quoted: bool = False


def tokens(statement: str) -> list[Token]:
    """SQL 词法（够用即可）：跳过注释与字符串字面量（含 '' 转义、E'' 反斜杠转义、美元引用），
    识别带引号的标识符（"" ` [] ）与点号。"""
    s, i, n, out = statement or "", 0, len(statement or ""), []
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
        elif s.startswith("--", i):
            j = s.find("\n", i)
            i = n if j < 0 else j + 1
        elif s.startswith("/*", i):
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c == "'" or (c in "Ee" and i + 1 < n and s[i + 1] == "'" and (i == 0 or not (s[i - 1].isalnum()
                                                                                         or s[i - 1] == "_"))):
            backslash = c in "Ee"
            i += 2 if backslash else 1
            while i < n:
                if backslash and s[i] == "\\":
                    i += 2
                    continue
                if s[i] == "'":
                    if i + 1 < n and s[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(Token("other"))
        elif c == "$" and (m := re.match(r"\$([A-Za-z_]\w*)?\$", s[i:])):
            tag = m.group(0)
            j = s.find(tag, i + len(tag))
            i = n if j < 0 else j + len(tag)
            out.append(Token("other"))
        elif c in "\"`[":
            close = {"\"": "\"", "`": "`", "[": "]"}[c]
            j, buf = i + 1, []
            while j < n:
                if s[j] == close:
                    if j + 1 < n and s[j + 1] == close and close != "]":
                        buf.append(close)
                        j += 2
                        continue
                    break
                buf.append(s[j])
                j += 1
            out.append(Token("ident", "".join(buf), quoted=True))
            i = j + 1
        elif c.isalpha() or c == "_":
            m = re.match(r"[A-Za-z_][\w$]*", s[i:])
            out.append(Token("ident", m.group(0)))
            i += len(m.group(0))
        elif c == ".":
            out.append(Token("dot"))
            i += 1
        elif c == ";":
            out.append(Token("semi"))
            i += 1
        else:
            out.append(Token("other"))
            i += 1
    return out


def _chains(toks: list[Token]) -> list[list[Token]]:
    """把 a.b.c 这样的限定名合成一串。"""
    chains, cur, expect_ident = [], [], True
    for t in toks:
        if t.kind == "ident" and expect_ident:
            cur.append(t)
            expect_ident = False
        elif t.kind == "dot" and cur and not expect_ident:
            expect_ident = True
        else:
            if cur:
                chains.append(cur)
            cur, expect_ident = ([t], False) if t.kind == "ident" else ([], True)
    if cur:
        chains.append(cur)
    return chains


# DDL 里常见的关键字：不带引号出现时不当作列名
_KEYWORDS = {
    "add", "alter", "column", "table", "view", "drop", "create", "replace", "or", "if", "exists", "not", "null",
    "type", "set", "default", "comment", "on", "is", "rename", "to", "modify", "change", "using", "as", "select",
    "from", "where", "group", "by", "order", "join", "inner", "left", "right", "outer", "and", "constraint",
    "primary", "key", "unique", "index", "materialized", "external", "temporary", "after", "first", "cascade",
    "restrict", "data", "collate", "with", "without", "time", "zone", "varchar", "text", "int", "integer",
    "bigint", "numeric", "decimal", "string", "nullable", "engine", "settings", "properties", "distributed",
    "buckets", "hash", "partition", "values", "in", "distinct", "case", "when", "then", "else", "end", "sum",
    "count", "max", "min", "avg", "schema", "database", "owner", "grant", "revoke", "force", "sync",
}


def _same(token: Token, name: str, fold: str) -> bool:
    if token.quoted or fold == "exact":
        return token.text == name
    if fold == "lower":  # PostgreSQL：不带引号的标识符折成小写
        return token.text.lower() == name
    return token.text.lower() == name.lower()  # 不区分大小写的引擎


def mentions_column(statement: str, column: str, object_name: str | None = None, fold: str = "insensitive") -> bool:
    """语句是否点名了某列：只看标识符，排除字符串、注释；限定名 <表>.<列> 取最后一段，对象自身的限定名不算。
    无法确定时返回 False（由调用方记为未知）。"""
    for chain in _chains(tokens(statement)):
        last = chain[-1]
        if len(chain) >= 2:
            if object_name is not None and _same(chain[-2], object_name, fold) and _same(last, column, fold):
                return True
            continue  # 其他限定名（库.表、模式.表）不是列
        if object_name is not None and _same(last, object_name, fold):
            continue  # 对象自身
        if not last.quoted and last.text.lower() in _KEYWORDS:
            continue
        if _same(last, column, fold):
            return True
    return False


def is_batch(statement: str) -> bool:
    """一条审计记录里是否有多条语句（分号后还有内容）。字符串、注释里的分号不算。"""
    toks = tokens(statement)
    while toks and toks[-1].kind == "semi":
        toks.pop()
    return any(t.kind == "semi" for t in toks)


def summarize(statement: str) -> str | None:
    """可落库的摘要：单条语句、看不出敏感值时压缩空白并截断；否则返回 None（只保存命令类型和对象）。"""
    if not statement or is_batch(statement) or _SENSITIVE.search(statement):
        return None
    s = _SPACE.sub(" ", statement).strip()
    return s if len(s) <= SUMMARY_LIMIT else s[:SUMMARY_LIMIT - 1] + "…"


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
