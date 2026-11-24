"""M4B 数据安全策略引擎：列级 PII 脱敏（含外发 LLM 强制脱敏）与确定性 SQL AST 行级权限（RLS）谓词注入。"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import sqlglot
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlglot import exp
from sqlglot.errors import ErrorLevel, ParseError, TokenError

from avelis.config import get_settings
from avelis.db import table_exists
from avelis.governance import access
from avelis.models import AssetColumnPolicy, AssetRlsPolicy, User

CLASSIFICATIONS = ("pii", "sensitive", "confidential")
MASKING_STRATEGIES = ("none", "mask", "hash", "redact")
RLS_SCOPE_TYPES = ("all", "role", "domain", "user")
STRATEGY_RANK = {"none": 0, "mask": 1, "hash": 2, "redact": 3}
ALLOWED_RLS_PARAMS = {"current_user_id", "current_username"}

_RLS_ALLOWED_NODES = (
    exp.And,
    exp.Or,
    exp.Not,
    exp.Paren,
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.In,
    exp.Is,
    exp.Like,
    exp.Between,
    exp.Column,
    exp.Identifier,
    exp.Literal,
    exp.Null,
    exp.Boolean,
    exp.Neg,
    exp.Parameter,
    exp.Placeholder,
)


def _pii_hmac_key() -> bytes:
    secret = get_settings().secret
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"avelis/pii-hash").derive(secret.encode())


def mask_value(value: Any, strategy: str, *, column_name: str = "") -> Any:
    """按脱敏策略处理单个单元格值；hash 使用与 AVELIS_SECRET 绑定的密钥化 HMAC-SHA256。"""
    if value is None or strategy == "none":
        return value
    if strategy == "redact":
        return "[REDACTED]"
    if strategy == "hash":
        raw = f"{column_name}:{value}".encode("utf-8") if column_name else str(value).encode("utf-8")
        digest = hmac.new(_pii_hmac_key(), raw, hashlib.sha256).hexdigest()
        return f"sha256:{digest[:12]}"
    if strategy == "mask":
        s = str(value)
        if "@" in s:
            local, domain = s.split("@", 1)
            prefix = local[:1] if local else ""
            return f"{prefix}***@{domain}"
        if s.isdigit() and len(s) >= 11:
            return f"{s[:3]}****{s[-4:]}"
        if len(s) <= 2:
            return "***"
        if len(s) <= 6:
            return f"{s[0]}***{s[-1]}"
        return f"{s[:2]}****{s[-2:]}"
    raise ValueError(f"不支持的脱敏策略：{strategy}")


def _param_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Parameter):
        inner = node.this
        return (inner.name if isinstance(inner, exp.Identifier) else str(inner or "")).strip()
    if isinstance(node, exp.Placeholder):
        return (node.name or "").strip()
    return ""


def _parse_predicate(predicate_sql: str) -> exp.Expression:
    text = (predicate_sql or "").strip()
    if not text:
        raise ValueError("RLS 谓词表达式不能为空")
    if ";" in text or "--" in text or "/*" in text or "*/" in text:
        raise ValueError("RLS 谓词禁止包含分号或注释")
    try:
        parsed = sqlglot.parse_one(f"SELECT 1 FROM _t WHERE ({text})", read="postgres", error_level=ErrorLevel.RAISE)
    except (ParseError, TokenError) as exc:
        raise ValueError(f"RLS 谓词语法无效：{str(exc).splitlines()[0]}") from exc
    if not isinstance(parsed, exp.Select):
        raise ValueError("RLS 谓词必须为合法布尔条件表达式")
    where = parsed.args.get("where")
    if where is None or where.this is None:
        raise ValueError("RLS 谓词解析失败")
    for n in parsed.walk():
        if getattr(n, "comments", None):
            raise ValueError("RLS 谓词禁止包含 SQL 注释")
    expr = where.this
    while isinstance(expr, exp.Paren):
        expr = expr.this
    return expr


def validate_rls_predicate(predicate_sql: str, allowed_columns: set[str]) -> exp.Expression:
    """用 sqlglot 允许清单校验 RLS 谓词 AST，拒绝子查询、函数、注释与未采集列。"""
    expr = _parse_predicate(predicate_sql)
    canonical_map = {c.casefold(): c for c in allowed_columns}
    has_column = False
    for node in expr.walk():
        if not isinstance(node, _RLS_ALLOWED_NODES):
            raise ValueError(f"RLS 谓词包含不支持的 SQL 语法节点：{type(node).__name__}")
        if isinstance(node, (exp.Parameter, exp.Placeholder)):
            pname = _param_name(node)
            if pname not in ALLOWED_RLS_PARAMS:
                raise ValueError(f"RLS 谓词参数仅支持 :current_user_id 或 :current_username，不支持：{pname or '空'}")
        elif isinstance(node, exp.Column):
            if node.table or node.db or node.catalog:
                raise ValueError(f"RLS 谓词中的列 {node.sql(dialect='postgres')} 不能带表名或库名限定")
            col_lower = node.name.casefold()
            if col_lower not in canonical_map:
                raise ValueError(f"RLS 谓词引用了未采集或不存在的列：{node.name}")
            has_column = True
    if not has_column:
        raise ValueError("RLS 谓词必须至少引用一个当前资产的列")
    return expr


def bind_rls_predicate(
    predicate_sql: str, *, table_alias: str, user_id: str, username: str, quoted: bool = False
) -> exp.Expression:
    """将 RLS 谓词绑定到指定表别名并替换当前用户参数，返回带外层括号的 AST 条件节点。"""
    expr = _parse_predicate(predicate_sql)

    def _transform(node: exp.Expression) -> exp.Expression:
        if isinstance(node, (exp.Parameter, exp.Placeholder)):
            pname = _param_name(node)
            if pname == "current_user_id":
                return exp.Literal.string(user_id)
            if pname == "current_username":
                return exp.Literal.string(username)
            raise ValueError(f"未授权的 RLS 参数：{pname}")
        if isinstance(node, exp.Column):
            return exp.column(node.name, table=table_alias or None, quoted=quoted)
        return node

    bound = expr.transform(_transform)
    return exp.Paren(this=bound)


def active_rls_predicates_for_user(session: Session, user: User, asset_id: str) -> list[str]:
    """返回当前用户在指定资产上生效的所有已启用 RLS 谓词 SQL，按策略 ID 确定性排序。"""
    if not table_exists(session, "asset_rls_policy"):
        return []
    policies = list(
        session.scalars(
            select(AssetRlsPolicy)
            .where(AssetRlsPolicy.asset_id == asset_id, AssetRlsPolicy.enabled.is_(True))
            .order_by(AssetRlsPolicy.id)
        )
    )
    if not policies:
        return []
    roles = access.roles_of(session, user)
    domains = access.domain_ids(session, user)
    matched: list[str] = []
    for p in policies:
        if p.scope_type == "all":
            matched.append(p.predicate_sql)
        elif p.scope_type == "role" and p.scope_value in roles:
            matched.append(p.predicate_sql)
        elif p.scope_type == "domain" and p.scope_value in domains:
            matched.append(p.predicate_sql)
        elif p.scope_type == "user" and (
            p.scope_value == user.id
            or (p.scope_value or "").casefold() == (getattr(user, "username", "") or "").casefold()
        ):
            matched.append(p.predicate_sql)
    return matched


def rls_signature(
    predicates_by_asset: dict[str, list[str]],
    *,
    user: object | None = None,
) -> str:
    """生成稳定的 RLS 摘要签名用于缓存键隔离。"""
    payload: list[object] = sorted((k, sorted(v)) for k, v in predicates_by_asset.items() if v)
    if payload and user is not None:
        flat_cf = " ".join(pred for _, preds in payload for pred in preds).casefold()
        user_tokens: list[str] = []
        if ":current_user_id" in flat_cf:
            user_tokens.append(f"uid:{getattr(user, 'id', '') or ''}")
        if ":current_username" in flat_cf:
            user_tokens.append(f"uname:{getattr(user, 'username', '') or ''}")
        if user_tokens:
            payload.append(tuple(user_tokens))
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def column_policies_for_assets(session: Session, asset_ids: list[str]) -> dict[tuple[str, str], AssetColumnPolicy]:
    """批量加载资产列级策略，按 (asset_id, column_name.casefold()) 索引。"""
    if not asset_ids or not table_exists(session, "asset_column_policy"):
        return {}
    rows = session.scalars(select(AssetColumnPolicy).where(AssetColumnPolicy.asset_id.in_(asset_ids)))
    return {(r.asset_id, r.column_name.casefold()): r for r in rows}


def pii_signature(policies: dict[tuple[str, str], AssetColumnPolicy], user_roles: set[str]) -> str:
    """生成包含豁免命中状态的列级 PII 缓存签名，防止豁免与非豁免用户串用缓存。"""
    entries = []
    for (asset_id, col_name), pol in sorted(policies.items()):
        if pol.masking_strategy == "none":
            continue
        exempt = bool(user_roles & set(pol.exempt_roles or []))
        entries.append([asset_id, col_name, pol.masking_strategy, exempt])
    return hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def apply_pii_masking(
    columns: list[str],
    rows: list[list[Any]] | list[tuple[Any, ...]],
    col_policy_map: dict[int, tuple[str, str, tuple[str, ...]]],
    *,
    user_roles: set[str],
    for_llm: bool = False,
) -> tuple[list[list[Any]], list[dict[str, Any]]]:
    """按输出列索引映射对结果行应用脱敏。

    col_policy_map: {col_index: (classification, masking_strategy, exempt_roles)}
    for_llm=True 时强制忽略 exempt_roles，确保原始 PII 绝不发送给外部大模型。
    """
    active: dict[int, tuple[str, str]] = {}
    applied: list[dict[str, Any]] = []
    for idx, (classification, strategy, exempt_roles) in sorted(col_policy_map.items()):
        if idx < 0 or idx >= len(columns) or strategy == "none":
            continue
        if not for_llm and (user_roles & set(exempt_roles)):
            continue
        active[idx] = (classification, strategy)
        applied.append(
            {
                "column": columns[idx],
                "classification": classification,
                "strategy": strategy,
                "for_llm": for_llm,
            }
        )
    if not active:
        return [list(r) for r in rows], []
    out_rows: list[list[Any]] = []
    for row in rows:
        out_row = list(row)
        for idx, (_cls, strategy) in active.items():
            if idx < len(out_row):
                out_row[idx] = mask_value(out_row[idx], strategy, column_name=columns[idx])
        out_rows.append(out_row)
    return out_rows, applied
