"""DDL 审计事件（ADR 0008）：连接器从数据库的审计来源读出“谁在什么时候对哪个对象执行了什么 DDL”。

连接器只读审计来源，不开启、不修改它。读不到时抛 AuditUnavailable，并按数据库原生错误给出原因（cause）：
no_permission 采集账号无权读取、not_enabled 未开启（审计来源不存在或已停用）、
incompatible 审计来源结构与连接器要求不符、error 其他。不返回空列表冒充“没人改”。
是否开启、是否授权由数据源方负责，这里只如实说明。

留底不保存任何语句原文或摘要，只保存审计来源给出的结构化信息：数据库用户、时间、命令类型、对象、事务号。
原文只在内存里用于列匹配，用完即丢；这里的词法器只服务于列匹配，不承担“能否安全落库”的判断。

列级证据只来自能识别语法位置的写法（ALTER TABLE / VIEW … ADD / DROP / ALTER / MODIFY / CHANGE / RENAME [COLUMN]、
COMMENT ON COLUMN、ClickHouse 的 COMMENT / CLEAR / MATERIALIZE COLUMN）；约束名、索引名不算。针对同一对象、
但识别不出涉及哪些列的语句（ColumnRefs.unknown）视为可能涉及任何一列：它仍是候选，但只能得到 ambiguous。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

CAUSES = ("no_permission", "not_enabled", "incompatible", "instance_changed", "error")
_WORD = re.compile(r"[^\W\d][\w$]*")  # 标识符：Unicode 字母或下划线开头（含中文）
_DOLLAR = re.compile(r"\$(?:[^\W\d]\w*)?\$")


@dataclass
class DdlEvent:
    at: datetime  # UTC，不带时区：语句执行（或生效）时间
    db_user: str
    objects: list[tuple[str, str]]  # (库或模式, 对象名)，按数据库目录中的真实写法（区分大小写）
    statement_type: str
    statement: str = field(repr=False)  # 原文只在内存里用于匹配列，用完即丢，不落库
    # 是否确定发生在两次快照之间：True / False 由连接器按事务可见性给出；None 由归因按时间与窗口边界判断
    certain: bool | None = None
    # 是否可能晚于语句时间才生效（如 Doris 的异步 schema change 作业）：上次快照之前 lag 秒内的这类语句也算边界候选
    may_lag: bool = False
    xid: str | None = None  # 审计来源记下的事务号（PostgreSQL xid8），随结构化信息落库



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
    $tag$ 美元引用），识别带引号的标识符（"" ` ）、Unicode 标识符与标点。遇到未闭合的结构时标记为不确定。"""
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
        elif c in "\"`":  # 方括号不当作引用：PostgreSQL 的 ARRAY[…]、下标都用它
            close = c
            j, buf, closed = i + 1, [], False
            while j < n:
                if s[j] == close:
                    if j + 1 < n and s[j + 1] == close:
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


# --- 列的语法位置 ------------------------------------------------------------------

def _kw(t: Token | None, *words: str) -> bool:
    return t is not None and t.kind == "ident" and not t.quoted and t.text.upper() in words


def _same(token: Token, name: str, fold: str) -> bool:
    if token.quoted or fold == "exact":
        return token.text == name
    if fold == "lower":  # PostgreSQL：不带引号的标识符折成小写
        return token.text.lower() == name
    return token.text.lower() == name.lower()  # 不区分大小写的引擎


same_identifier = _same  # 归因按引擎的标识符规则比较列名


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


_NO_COLUMN_ADD = {"CHECK", "UNIQUE", "FOREIGN", "EXCLUDE", "INDEX", "KEY", "PARTITION", "PARTITIONS", "ROLLUP",
                  "PROJECTION", "STATISTICS", "TEMPORARY"}
_NO_COLUMN_MODIFY = {"COMMENT", "ORDER", "TTL", "SETTING", "SETTINGS", "ENGINE", "PROPERTIES", "DISTRIBUTION",
                     "SAMPLE", "PARTITION"}
UNKNOWN = None  # 识别不了：可能涉及任何一列
# 确定不改变采集字段（列名、类型、可空性、注释、键）的 ALTER TABLE 动作（PostgreSQL、MySQL 系、ClickHouse 的写法）
_NO_COLUMN_VERBS = {"OWNER", "SET", "RESET", "CLUSTER", "REPLICA", "INHERIT", "OF", "ATTACH", "DETACH", "DEPENDS",
                    "ALGORITHM", "LOCK", "ENGINE", "AUTO_INCREMENT", "CHARSET", "COLLATE", "ROW_FORMAT",
                    "KEY_BLOCK_SIZE"}
_NO_COLUMN_TOGGLES = {"TRIGGER", "RULE", "ROW", "REPLICA", "ALWAYS", "FORCE", "KEYS", "INHERIT"}


def _paren_idents(a: list[Token], i: int) -> list[Token]:
    """括号里每个逗号分隔项的第一个标识符（如 PRIMARY KEY (a, b)、Doris 的 ADD COLUMN (a INT, b INT)）。"""
    cols, depth, expect = [], 0, True
    for t in a[i:]:
        if t.kind == "lparen":
            depth += 1
            continue
        if t.kind == "rparen":
            depth -= 1
            if depth == 0:
                break
            continue
        if depth == 1 and t.kind == "comma":
            expect = True
            continue
        if depth == 1 and expect and t.kind == "ident":
            cols.append(t)
        if depth == 1:
            expect = False
    return cols


def _action_columns(a: list[Token]) -> list[Token] | None:
    """一个 ALTER TABLE / VIEW 动作涉及的列：列表（可能为空：确定不改列结构）；UNKNOWN：识别不了，可能涉及任何一列。"""
    if not a:
        return []
    verb = a[0].text.upper() if a[0].kind == "ident" and not a[0].quoted else ""
    nxt = a[1] if len(a) > 1 else None
    if verb in _NO_COLUMN_VERBS or (verb in ("ENABLE", "DISABLE", "FORCE", "NO") and _kw(nxt, *_NO_COLUMN_TOGGLES)):
        return []  # 属主、存储参数、表空间、模式、日志属性、聚簇、复制标识、触发器、规则、行级安全：不改采集的字段
    if verb == "ALTER" and _kw(nxt, "CONSTRAINT"):
        return []  # 只改约束的可延迟属性
    i = 1
    if verb in ("ADD", "DROP") and _kw(nxt, "CONSTRAINT"):
        if verb == "DROP":
            return UNKNOWN  # 删的可能是 NOT NULL 或主键约束，影响哪些列看不出
        i = 3  # ADD CONSTRAINT <名字> …
        nxt = a[i] if len(a) > i else None
    if verb == "ADD" and _kw(nxt, "PRIMARY"):
        return _paren_idents(a, i) or UNKNOWN  # 主键会让列变为非空
    if verb == "ADD" and _kw(nxt, "NOT"):  # PostgreSQL 18：ADD [CONSTRAINT n] NOT NULL col
        j = _skip(a, i, "NOT", "NULL")
        return [a[j]] if j < len(a) and a[j].kind == "ident" else UNKNOWN
    if verb in ("ADD", "DROP") and _kw(nxt, *_NO_COLUMN_ADD):
        return []
    if verb == "MODIFY" and _kw(nxt, *_NO_COLUMN_MODIFY):
        return []
    if verb in ("ADD", "DROP", "ALTER", "MODIFY", "CLEAR", "MATERIALIZE"):
        i = _skip(a, 1, "COLUMN")
        if verb in ("CLEAR", "MATERIALIZE") and i == 1:
            return UNKNOWN
        i = _skip(a, i, "IF", "NOT", "EXISTS")
        i = _skip(a, i, "IF", "EXISTS")
        if i < len(a) and a[i].kind == "lparen" and verb == "ADD":  # Doris：ADD COLUMN (a INT, b INT)
            return _paren_idents(a, i) or UNKNOWN
        return [a[i]] if i < len(a) and a[i].kind == "ident" else UNKNOWN
    if verb == "CHANGE":  # MySQL：CHANGE [COLUMN] old new type
        i = _skip(a, 1, "COLUMN")
        cols = [t for t in a[i:i + 2] if t.kind == "ident"]
        return cols if len(cols) == 2 else UNKNOWN
    if verb == "RENAME":
        if _kw(nxt, "TO", "AS") or _kw(nxt, "CONSTRAINT", "INDEX", "PARTITION", "ROLLUP"):
            return []  # 改表名、改约束名等
        i = _skip(a, 1, "COLUMN")  # PostgreSQL 可以省略 COLUMN：RENAME a TO b
        old = a[i] if i < len(a) and a[i].kind == "ident" else None
        j = _skip(a, i + 1, "TO")
        new = a[j] if j < len(a) and a[j].kind == "ident" else None
        return [old, new] if old is not None and new is not None else UNKNOWN
    if verb == "COMMENT":
        if _kw(nxt, "COLUMN"):  # ClickHouse：COMMENT COLUMN c '…'
            i = _skip(a, 2, "IF", "EXISTS")
            return [a[i]] if i < len(a) and a[i].kind == "ident" else UNKNOWN
        return []  # 表注释（ClickHouse / Doris：COMMENT '…'）
    return UNKNOWN


@dataclass
class ColumnRefs:
    columns: list[Token]  # 在语法位置上点名的列
    unknown: bool  # 有可能涉及该对象、但识别不出涉及哪些列的部分（可能涉及任何一列）；与 columns 独立累积


_ALTERABLE = (("TABLE",), ("VIEW",), ("MATERIALIZED", "VIEW"), ("FOREIGN", "TABLE"))
# 确定不改变表结构的语句（事务控制、数据读写、授权、维护）：不针对对象的列
_HARMLESS = {"BEGIN", "START", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE", "SET", "RESET", "SHOW", "GRANT",
             "REVOKE", "INSERT", "UPDATE", "DELETE", "COPY", "VACUUM", "ANALYZE", "LOCK", "TRUNCATE", "REFRESH",
             "NOTIFY", "LISTEN", "UNLISTEN", "DISCARD", "CLUSTER", "REINDEX", "CHECKPOINT", "SECURITY", "USE"}
# 可能在内部执行任意 DDL 的语句：动态 SQL、过程调用、可能调用函数的查询
_OPAQUE = {"DO", "EXECUTE", "CALL", "SELECT", "WITH", "VALUES", "PERFORM", "PREPARE", "TABLE"}


def _targets(parts: list[Token], schema: str | None, name: str, fold: str) -> bool:
    """限定名是否指向该对象：末段比对名称；带了库或模式段时也要比对（只写名称时按名称算，可能经 search_path）。"""
    if not parts or not _same(parts[-1], name, fold):
        return False
    return len(parts) < 2 or schema is None or _same(parts[-2], schema, fold)


def column_refs(statement: str, object_name: str, fold: str = "insensitive", schema: str | None = None) -> ColumnRefs:
    """语句（可能是多条语句的请求）对 schema.object_name 涉及的列。逐条子语句判断，unknown 独立累积：
    - ALTER TABLE / VIEW / MATERIALIZED VIEW / FOREIGN TABLE <该对象> 的各个动作：认得的列动作给出列，
      确定不改列结构的动作
      （改属主、存储参数、表空间、触发器、行级安全等）不算，其余动作算 unknown；
    - COMMENT ON COLUMN <该对象>.<列> 给出列；COMMENT ON TABLE / VIEW <该对象> 不涉及列；
    - CREATE / DROP 该对象、DO / EXECUTE / CALL / SELECT 等可能在内部执行任意 DDL 的语句、认不出的语句：unknown；
    - 针对别的对象的 DDL、事务控制、数据读写、授权等：不涉及该对象的列。
    审计来源说这条记录针对该对象、却没有一条子语句认得出是针对它的，也算 unknown；词法上无法确定时同样。"""
    lexed = lex(statement)
    found: list[Token] = []
    unknown = not lexed.certain
    targeted = False
    for st in _statements(lexed.tokens):
        head = st[0].text.upper() if st and st[0].kind == "ident" and not st[0].quoted else ""
        if head == "ALTER":
            kind = next((k for k in _ALTERABLE if all(_kw(st[1 + n] if len(st) > 1 + n else None, w)
                                                       for n, w in enumerate(k))), None)
            if kind is None:
                continue  # ALTER INDEX / ROLE / SEQUENCE … 不改表的列
            i = _skip(st, 1 + len(kind), "IF", "EXISTS")
            i = _skip(st, i, "ONLY")
            parts, i = _name(st, i)
            if not _targets(parts, schema, object_name, fold):
                continue
            targeted = True
            for action in _actions(st[i:]):
                cols = _action_columns(action)
                if cols is UNKNOWN:
                    unknown = True
                else:
                    found += cols
        elif head == "COMMENT" and _kw(st[1] if len(st) > 1 else None, "ON"):
            what = st[2] if len(st) > 2 else None
            if _kw(what, "COLUMN"):
                parts, _ = _name(st, 3)
                if len(parts) >= 2 and _targets(parts[:-1], schema, object_name, fold):
                    targeted = True
                    found.append(parts[-1])
            elif _kw(what, "TABLE", "VIEW"):
                parts, _ = _name(st, 3)
                targeted = targeted or _targets(parts, schema, object_name, fold)
            elif _kw(what, "MATERIALIZED", "FOREIGN"):
                parts, _ = _name(st, 4)
                targeted = targeted or _targets(parts, schema, object_name, fold)
        elif head in ("CREATE", "DROP"):
            i = _skip(st, 1, "OR", "REPLACE")
            i = _skip(st, i, "TEMPORARY")
            i = _skip(st, i, "TEMP")
            kind = next((k for k in _ALTERABLE if all(_kw(st[i + n] if len(st) > i + n else None, w)
                                                       for n, w in enumerate(k))), None)
            if kind is not None:
                j = _skip(st, i + len(kind), "IF", "NOT", "EXISTS")
                j = _skip(st, j, "IF", "EXISTS")
                parts, _ = _name(st, j)
                if _targets(parts, schema, object_name, fold):
                    targeted, unknown = True, True  # 重建了这个对象：任何一列都可能变
        elif head in _HARMLESS:
            # 不改结构的语句：点名了该对象（如 GRANT … ON 该表）就算认得出、且不涉及任何列
            targeted = targeted or any(_targets(_name(st, k)[0], schema, object_name, fold)
                                       for k in range(len(st)) if st[k].kind == "ident")
        else:
            unknown = True  # DO / EXECUTE / CALL / SELECT …，以及认不出的语句：内部可能执行任意 DDL
    return ColumnRefs(found, unknown or not targeted)


def mentions_column(statement: str, column: str, object_name: str | None = None, fold: str = "insensitive",
                    schema: str | None = None) -> bool:
    """语句是否在列的语法位置上点名了该列。"""
    if object_name is None:
        return False
    return any(_same(t, column, fold) for t in column_refs(statement, object_name, fold, schema).columns)


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
