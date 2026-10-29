"""DDL 审计事件（ADR 0008）：连接器从数据库的审计来源读出“谁在什么时候对哪个对象执行了什么 DDL”。

连接器只读审计来源，不开启、不修改它。读不到（未开启、无权限、表不存在）时抛 AuditUnavailable，
不返回空列表冒充“没人改”。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

SUMMARY_LIMIT = 300
_SECRET = re.compile(r"(IDENTIFIED\s+(?:WITH\s+\w+\s+)?(?:BY|AS)|PASSWORD)\s*(=\s*)?'[^']*'", re.IGNORECASE)
_SPACE = re.compile(r"\s+")


@dataclass
class DdlEvent:
    at: datetime  # UTC，不带时区
    db_user: str
    objects: list[tuple[str, str]]  # (库或模式, 对象名)，按数据库返回的写法
    statement_type: str
    statement: str = field(repr=False)  # 原文只用于匹配列名，不落库；落库的是 summary

    @property
    def summary(self) -> str:
        return summarize(self.statement)


class AuditUnavailable(Exception):
    """审计来源读取失败。message 保留数据库原始报错。"""


def summarize(statement: str) -> str:
    """压缩空白、遮蔽口令字面量、截断。"""
    s = _SECRET.sub(lambda m: f"{m.group(1)} {m.group(2) or ''}'***'", statement or "")
    s = _SPACE.sub(" ", s).strip()
    return s if len(s) <= SUMMARY_LIMIT else s[:SUMMARY_LIMIT - 1] + "…"


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
    """从 DDL 语句中解析对象名（用于不在审计记录里给出对象的来源，如 Doris）。
    只认 CREATE / ALTER / DROP TABLE|VIEW 与 RENAME TABLE；ALTER … RENAME 时新旧名都返回。"""
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
    """语句是否整体重定义对象（CREATE …、ALTER VIEW … AS）：这类语句不一定逐列点名。"""
    return bool(_REDEFINE.match(statement or ""))


def mentions_column(statement: str, column: str) -> bool:
    return bool(re.search(rf"(?<![\w$]){re.escape(column)}(?![\w$])", statement or "", re.IGNORECASE))
