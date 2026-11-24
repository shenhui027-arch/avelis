"""模型调用 Token/费用账本、定价目录、配额校验与频次限流。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import HTTPException, Request, status
from sqlalchemy import case, func, or_, select, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from avelis import audit, cache
from avelis.config import get_settings
from avelis.db import utcnow
from avelis.governance import access
from avelis.governance.errors import Invalid, NotFound
from avelis.i18n import Msg, gettext, translate
from avelis.models import LlmPricing, LlmQuota, LlmUsageLedger, Metric, MetricVersion, User
from avelis.observability import METRICS

_COST_QUANT = Decimal("0.000001")
_MILLION = Decimal("1000000")

# 内置参考单价（USD / 1M tokens）；管理员可通过 llm_pricing 表覆盖任意模型的单价与币种。
BUILTIN_PRICING: dict[str, dict[str, Any]] = {
    "deepseek-chat": {
        "input_per_1m": Decimal("0.27"),
        "output_per_1m": Decimal("1.10"),
        "cache_hit_per_1m": Decimal("0.07"),
        "currency": "USD",
    },
    "deepseek-reasoner": {
        "input_per_1m": Decimal("0.55"),
        "output_per_1m": Decimal("2.19"),
        "cache_hit_per_1m": Decimal("0.14"),
        "currency": "USD",
    },
    "deepseek-flash": {
        "input_per_1m": Decimal("0.14"),
        "output_per_1m": Decimal("0.55"),
        "cache_hit_per_1m": Decimal("0.035"),
        "currency": "USD",
    },
    "gpt-4o": {
        "input_per_1m": Decimal("2.50"),
        "output_per_1m": Decimal("10.00"),
        "cache_hit_per_1m": Decimal("1.25"),
        "currency": "USD",
    },
    "gpt-4o-mini": {
        "input_per_1m": Decimal("0.15"),
        "output_per_1m": Decimal("0.60"),
        "cache_hit_per_1m": Decimal("0.075"),
        "currency": "USD",
    },
    "gpt-4.1": {
        "input_per_1m": Decimal("2.00"),
        "output_per_1m": Decimal("8.00"),
        "cache_hit_per_1m": Decimal("0.50"),
        "currency": "USD",
    },
    "gpt-4.1-mini": {
        "input_per_1m": Decimal("0.40"),
        "output_per_1m": Decimal("1.60"),
        "cache_hit_per_1m": Decimal("0.10"),
        "currency": "USD",
    },
    "gpt-4.1-nano": {
        "input_per_1m": Decimal("0.10"),
        "output_per_1m": Decimal("0.40"),
        "cache_hit_per_1m": Decimal("0.025"),
        "currency": "USD",
    },
    "gpt-5": {
        "input_per_1m": Decimal("1.25"),
        "output_per_1m": Decimal("10.00"),
        "cache_hit_per_1m": Decimal("0.125"),
        "currency": "USD",
    },
    "gpt-5-mini": {
        "input_per_1m": Decimal("0.25"),
        "output_per_1m": Decimal("2.00"),
        "cache_hit_per_1m": Decimal("0.025"),
        "currency": "USD",
    },
    "o1": {
        "input_per_1m": Decimal("15.00"),
        "output_per_1m": Decimal("60.00"),
        "cache_hit_per_1m": Decimal("7.50"),
        "currency": "USD",
    },
    "o3": {
        "input_per_1m": Decimal("2.00"),
        "output_per_1m": Decimal("8.00"),
        "cache_hit_per_1m": Decimal("0.50"),
        "currency": "USD",
    },
    "o3-mini": {
        "input_per_1m": Decimal("1.10"),
        "output_per_1m": Decimal("4.40"),
        "cache_hit_per_1m": Decimal("0.55"),
        "currency": "USD",
    },
    "o4-mini": {
        "input_per_1m": Decimal("1.10"),
        "output_per_1m": Decimal("4.40"),
        "cache_hit_per_1m": Decimal("0.275"),
        "currency": "USD",
    },
    "claude-sonnet-4": {
        "input_per_1m": Decimal("3.00"),
        "output_per_1m": Decimal("15.00"),
        "cache_hit_per_1m": Decimal("0.30"),
        "currency": "USD",
    },
    "claude-opus-4": {
        "input_per_1m": Decimal("15.00"),
        "output_per_1m": Decimal("75.00"),
        "cache_hit_per_1m": Decimal("1.50"),
        "currency": "USD",
    },
    "claude-haiku-4": {
        "input_per_1m": Decimal("0.80"),
        "output_per_1m": Decimal("4.00"),
        "cache_hit_per_1m": Decimal("0.08"),
        "currency": "USD",
    },
    "claude-3-7-sonnet": {
        "input_per_1m": Decimal("3.00"),
        "output_per_1m": Decimal("15.00"),
        "cache_hit_per_1m": Decimal("0.30"),
        "currency": "USD",
    },
    "claude-3-5-sonnet": {
        "input_per_1m": Decimal("3.00"),
        "output_per_1m": Decimal("15.00"),
        "cache_hit_per_1m": Decimal("0.30"),
        "currency": "USD",
    },
    "claude-3-5-haiku": {
        "input_per_1m": Decimal("0.80"),
        "output_per_1m": Decimal("4.00"),
        "cache_hit_per_1m": Decimal("0.08"),
        "currency": "USD",
    },
    "qwen-plus": {
        "input_per_1m": Decimal("0.40"),
        "output_per_1m": Decimal("1.20"),
        "cache_hit_per_1m": Decimal("0.16"),
        "currency": "USD",
    },
    "qwen-max": {
        "input_per_1m": Decimal("1.60"),
        "output_per_1m": Decimal("6.40"),
        "cache_hit_per_1m": Decimal("0.64"),
        "currency": "USD",
    },
    "qwen-turbo": {
        "input_per_1m": Decimal("0.05"),
        "output_per_1m": Decimal("0.20"),
        "cache_hit_per_1m": Decimal("0.02"),
        "currency": "USD",
    },
    "qwen-long": {
        "input_per_1m": Decimal("0.07"),
        "output_per_1m": Decimal("0.28"),
        "cache_hit_per_1m": Decimal("0.03"),
        "currency": "USD",
    },
    "qwen3": {
        "input_per_1m": Decimal("0.40"),
        "output_per_1m": Decimal("1.20"),
        "cache_hit_per_1m": Decimal("0.16"),
        "currency": "USD",
    },
    "qwq": {
        "input_per_1m": Decimal("0.40"),
        "output_per_1m": Decimal("1.20"),
        "cache_hit_per_1m": Decimal("0.16"),
        "currency": "USD",
    },
}


def _is_memory_session(session: Session | None) -> bool:
    return not isinstance(session, Session)


def _table_exists(session: Session | None, table_name: str) -> bool:
    if _is_memory_session(session):
        return False
    assert session is not None
    bind = session.get_bind()
    if bind.dialect.name != "sqlite":
        return True
    return sa_inspect(session.connection()).has_table(table_name)


def _builtin_pricing_for(model_id: str) -> dict[str, Any] | None:
    mid = (model_id or "").strip().lower()
    if not mid:
        return None
    if mid in BUILTIN_PRICING:
        return BUILTIN_PRICING[mid]
    for prefix in sorted(BUILTIN_PRICING, key=len, reverse=True):
        if mid.startswith(prefix):
            return BUILTIN_PRICING[prefix]
    return None


def lookup_pricing(
    session: Session | None, provider_code: str | None, model_id: str
) -> tuple[Decimal, Decimal, Decimal | None, str, str]:
    """返回 (input_per_1m, output_per_1m, cache_hit_per_1m, currency, source)。"""
    if _table_exists(session, "llm_pricing"):
        assert session is not None
        if provider_code:
            exact = session.scalar(
                select(LlmPricing).where(LlmPricing.provider_code == provider_code, LlmPricing.model_id == model_id)
            )
            if exact is not None:
                return (
                    Decimal(exact.input_per_1m),
                    Decimal(exact.output_per_1m),
                    Decimal(exact.cache_hit_per_1m) if exact.cache_hit_per_1m is not None else None,
                    exact.currency,
                    "configured",
                )
        wild = session.scalar(
            select(LlmPricing).where(LlmPricing.provider_code == "*", LlmPricing.model_id == model_id)
        )
        if wild is not None:
            return (
                Decimal(wild.input_per_1m),
                Decimal(wild.output_per_1m),
                Decimal(wild.cache_hit_per_1m) if wild.cache_hit_per_1m is not None else None,
                wild.currency,
                "configured",
            )
    builtin = _builtin_pricing_for(model_id)
    if builtin is not None:
        return (
            builtin["input_per_1m"],
            builtin["output_per_1m"],
            builtin.get("cache_hit_per_1m"),
            builtin.get("currency", "USD"),
            "builtin",
        )
    return Decimal("0"), Decimal("0"), None, "USD", "none"


def compute_cost(
    input_tokens: int,
    output_tokens: int,
    cache_hit_tokens: int,
    input_per_1m: Decimal,
    output_per_1m: Decimal,
    cache_hit_per_1m: Decimal | None,
) -> Decimal:
    in_tok = max(0, int(input_tokens or 0))
    out_tok = max(0, int(output_tokens or 0))
    cache_tok = max(0, int(cache_hit_tokens or 0))
    non_cached = max(0, in_tok - cache_tok)
    eff_cache = cache_hit_per_1m if cache_hit_per_1m is not None else input_per_1m
    total = (
        Decimal(non_cached) * input_per_1m + Decimal(cache_tok) * eff_cache + Decimal(out_tok) * output_per_1m
    ) / _MILLION
    return total.quantize(_COST_QUANT)


def resolve_answer_domain_id(session: Session | None, metric_version_ids: list[str] | None) -> str | None:
    if _is_memory_session(session) or not metric_version_ids:
        return None
    if not _table_exists(session, "metric_version") or not _table_exists(session, "metric"):
        return None
    assert session is not None
    mv = session.get(MetricVersion, metric_version_ids[0])
    if mv is None:
        return None
    metric = session.get(Metric, mv.metric_id)
    return metric.domain_id if metric is not None else None


def resolve_metric_codes_domain_id(session: Session | None, metric_codes: list[str] | None) -> str | None:
    if _is_memory_session(session) or not metric_codes:
        return None
    if not _table_exists(session, "metric"):
        return None
    assert session is not None
    domain_ids = {
        d for d in session.scalars(select(Metric.domain_id).where(Metric.code.in_(list(metric_codes)))) if d is not None
    }
    return next(iter(domain_ids)) if len(domain_ids) == 1 else None


def record_usage(
    session: Session | None,
    *,
    user_id: str | None,
    purpose: str,
    model_id: str,
    usage: dict[str, int | None] | None = None,
    domain_id: str | None = None,
    phase: str | None = None,
    provider_id: str | None = None,
    provider_code: str | None = None,
    adapter: str | None = None,
    thread_id: str | None = None,
    answer_id: str | None = None,
    run_id: str | None = None,
    latency_ms: int | None = None,
    status: str = "ok",
    failover_from: str | None = None,
    routing_tier: str | None = None,
    error_code: str | None = None,
) -> LlmUsageLedger | None:
    """将一次模型调用写入 llm_usage_ledger。MemorySession 或未建表的 SQLite 局部测试夹具跳过。"""
    usage = usage or {}
    in_tok = max(0, int(usage.get("input_tokens") or 0))
    out_tok = max(0, int(usage.get("output_tokens") or 0))
    cache_tok = max(0, int(usage.get("cache_hit_tokens") or 0))
    METRICS.observe_llm(
        purpose or "unknown",
        status or "ok",
        duration_seconds=(latency_ms / 1000.0) if latency_ms is not None else None,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_hit_tokens=cache_tok,
    )
    if not _table_exists(session, "llm_usage_ledger"):
        return None
    assert session is not None
    reason_tok = max(0, int(usage.get("reasoning_tokens") or 0))
    total_tok = in_tok + out_tok
    in_p, out_p, cache_p, currency, _ = lookup_pricing(session, provider_code, model_id)
    cost = compute_cost(in_tok, out_tok, cache_tok, in_p, out_p, cache_p)
    row = LlmUsageLedger(
        user_id=user_id,
        domain_id=domain_id,
        purpose=purpose,
        phase=phase,
        provider_id=provider_id,
        provider_code=provider_code,
        model_id=model_id,
        adapter=adapter,
        thread_id=thread_id,
        answer_id=answer_id,
        run_id=run_id,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_hit_tokens=cache_tok,
        reasoning_tokens=reason_tok,
        total_tokens=total_tok,
        estimated_cost=cost,
        currency=currency,
        latency_ms=latency_ms,
        status=status,
        failover_from=failover_from,
        routing_tier=routing_tier,
        error_code=error_code,
    )
    session.add(row)
    session.flush()
    return row


def backfill_answer_domain(session: Session | None, answer_id: str | None, domain_id: str | None) -> None:
    if not answer_id or not domain_id or not _table_exists(session, "llm_usage_ledger"):
        return
    assert session is not None
    session.execute(
        update(LlmUsageLedger)
        .where(LlmUsageLedger.answer_id == answer_id, LlmUsageLedger.domain_id.is_(None))
        .values(domain_id=domain_id)
    )
    session.flush()


def _utc_windows(now: datetime | None = None) -> tuple[datetime, datetime]:
    cur = now or utcnow()
    day_start = cur.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = cur.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return day_start, month_start


def _scope_usage(session: Session, scope_type: str, scope_id: str, *, since: datetime) -> tuple[int, Decimal]:
    stmt = select(
        func.coalesce(func.sum(LlmUsageLedger.total_tokens), 0),
        func.coalesce(func.sum(LlmUsageLedger.estimated_cost), 0),
    ).where(LlmUsageLedger.created_at >= since)
    if scope_type == "user":
        stmt = stmt.where(LlmUsageLedger.user_id == scope_id)
    elif scope_type == "domain":
        stmt = stmt.where(LlmUsageLedger.domain_id == scope_id)
    tokens, cost = session.execute(stmt).one()
    return int(tokens or 0), Decimal(str(cost or 0))


def _quota_exceeded_error(
    quota: LlmQuota, period: str, metric: str, limit: Decimal | int, used: Decimal | int
) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        headers={"Retry-After": "60"},
        detail={
            "code": "quota_exceeded",
            "scope_type": quota.scope_type,
            "scope_id": quota.scope_id,
            "period": period,
            "metric": metric,
            "limit": float(limit),
            "used": float(used),
            "detail": gettext("已达到模型用量或费用配额上限，请联系管理员调整"),
        },
    )


def _check_single_quota(session: Session, quota: LlmQuota, day_start: datetime, month_start: datetime) -> None:
    if quota.daily_token_limit is not None or quota.daily_cost_limit is not None:
        used_tok, used_cost = _scope_usage(session, quota.scope_type, quota.scope_id, since=day_start)
        if quota.daily_token_limit is not None and used_tok >= quota.daily_token_limit:
            raise _quota_exceeded_error(quota, "daily", "tokens", quota.daily_token_limit, used_tok)
        if quota.daily_cost_limit is not None and used_cost >= Decimal(quota.daily_cost_limit):
            raise _quota_exceeded_error(quota, "daily", "cost", quota.daily_cost_limit, used_cost)
    if quota.monthly_token_limit is not None or quota.monthly_cost_limit is not None:
        used_tok, used_cost = _scope_usage(session, quota.scope_type, quota.scope_id, since=month_start)
        if quota.monthly_token_limit is not None and used_tok >= quota.monthly_token_limit:
            raise _quota_exceeded_error(quota, "monthly", "tokens", quota.monthly_token_limit, used_tok)
        if quota.monthly_cost_limit is not None and used_cost >= Decimal(quota.monthly_cost_limit):
            raise _quota_exceeded_error(quota, "monthly", "cost", quota.monthly_cost_limit, used_cost)


def check_quota(
    session: Session | None,
    user: User,
    *,
    domain_ids: list[str] | set[str] | None = None,
) -> None:
    if not _table_exists(session, "llm_quota") or not _table_exists(session, "llm_usage_ledger"):
        return
    assert session is not None
    day_start, month_start = _utc_windows()
    for scope_type, scope_id in (("global", "*"), ("user", user.id)):
        quota = session.scalar(
            select(LlmQuota).where(
                LlmQuota.scope_type == scope_type, LlmQuota.scope_id == scope_id, LlmQuota.enabled.is_(True)
            )
        )
        if quota is not None:
            _check_single_quota(session, quota, day_start, month_start)
    if domain_ids is None and _table_exists(session, "domain_member"):
        domain_ids = sorted(access.domain_ids(session, user))
    dids = sorted(set(domain_ids or ()))
    if not dids:
        return
    domain_quotas = list(
        session.scalars(
            select(LlmQuota).where(
                LlmQuota.scope_type == "domain", LlmQuota.scope_id.in_(dids), LlmQuota.enabled.is_(True)
            )
        )
    )
    for dq in domain_quotas:
        _check_single_quota(session, dq, day_start, month_start)


def _effective_rpm_limit(
    session: Session | None, user: User, action: Literal["ask", "analysis"], domain_ids: list[str] | set[str] | None
) -> tuple[int, bool]:
    settings = get_settings()
    default_rpm = settings.ask_rpm_limit if action == "ask" else settings.analysis_rpm_limit
    if not _table_exists(session, "llm_quota"):
        return default_rpm, False
    assert session is not None
    user_q = session.scalar(
        select(LlmQuota).where(LlmQuota.scope_type == "user", LlmQuota.scope_id == user.id, LlmQuota.enabled.is_(True))
    )
    if user_q is not None and user_q.rpm_limit is not None:
        return user_q.rpm_limit, True
    dids = sorted(set(domain_ids or ()))
    if dids:
        dom_rpms = [
            q.rpm_limit
            for q in session.scalars(
                select(LlmQuota).where(
                    LlmQuota.scope_type == "domain", LlmQuota.scope_id.in_(dids), LlmQuota.enabled.is_(True)
                )
            )
            if q.rpm_limit is not None
        ]
        if dom_rpms:
            return min(dom_rpms), True
    global_q = session.scalar(
        select(LlmQuota).where(LlmQuota.scope_type == "global", LlmQuota.scope_id == "*", LlmQuota.enabled.is_(True))
    )
    if global_q is not None and global_q.rpm_limit is not None:
        return global_q.rpm_limit, True
    return default_rpm, False


def enforce_llm_preflight(
    request: Request | None,
    session: Session | None,
    user: User,
    *,
    action: Literal["ask", "analysis"],
    domain_ids: list[str] | set[str] | None = None,
) -> None:
    """在发起模型对话或打开 SSE 流前校验 RPM 限流与配额，超限返回 HTTP 429。"""
    if domain_ids is None and session is not None and _table_exists(session, "domain_member"):
        domain_ids = sorted(access.domain_ids(session, user))
    rpm_limit, is_explicit = _effective_rpm_limit(session, user, action, domain_ids)
    if rpm_limit == 0 and is_explicit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": "60"},
            detail={
                "code": "rate_limited",
                "retry_after": 60,
                "detail": translate(Msg("请求过于频繁，请 {seconds} 秒后再试", seconds=60)),
            },
        )
    if rpm_limit > 0:
        allowed, _, retry_after = cache.fixed_window_rate_limit(f"llm:{action}", f"user:{user.id}", rpm_limit)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(retry_after)},
                detail={
                    "code": "rate_limited",
                    "retry_after": int(retry_after),
                    "detail": translate(Msg("请求过于频繁，请 {seconds} 秒后再试", seconds=retry_after)),
                },
            )
    check_quota(session, user, domain_ids=domain_ids)


# --- 定价、配额、账本查询与管理 -------------------------------------------------------


def pricing_view(row: LlmPricing) -> dict[str, Any]:
    return {
        "id": row.id,
        "provider_code": row.provider_code,
        "model_id": row.model_id,
        "input_per_1m": float(row.input_per_1m),
        "output_per_1m": float(row.output_per_1m),
        "cache_hit_per_1m": float(row.cache_hit_per_1m) if row.cache_hit_per_1m is not None else None,
        "currency": row.currency,
        "source": "configured",
        "updated_at": row.updated_at.isoformat(),
    }


def list_pricing(session: Session) -> dict[str, Any]:
    configured = [
        pricing_view(r)
        for r in session.scalars(select(LlmPricing).order_by(LlmPricing.provider_code, LlmPricing.model_id))
    ]
    defaults = [
        {
            "provider_code": "*",
            "model_id": mid,
            "input_per_1m": float(vals["input_per_1m"]),
            "output_per_1m": float(vals["output_per_1m"]),
            "cache_hit_per_1m": float(vals["cache_hit_per_1m"]) if vals.get("cache_hit_per_1m") is not None else None,
            "currency": vals.get("currency", "USD"),
            "source": "builtin",
        }
        for mid, vals in sorted(BUILTIN_PRICING.items())
    ]
    return {"configured": configured, "defaults": defaults}


def upsert_pricing(session: Session, actor: User, items: list[dict[str, Any]]) -> dict[str, Any]:
    for item in items:
        provider_code = (item.get("provider_code") or "*").strip() or "*"
        model_id = (item.get("model_id") or "").strip()
        if not model_id:
            raise Invalid("model_id 不能为空")
        existing = session.scalar(
            select(LlmPricing).where(LlmPricing.provider_code == provider_code, LlmPricing.model_id == model_id)
        )
        if item.get("delete"):
            if existing is not None:
                session.delete(existing)
            continue
        in_p = Decimal(str(item["input_per_1m"]))
        out_p = Decimal(str(item["output_per_1m"]))
        cache_raw = item.get("cache_hit_per_1m")
        cache_p = Decimal(str(cache_raw)) if cache_raw is not None else None
        if in_p < 0 or out_p < 0 or (cache_p is not None and cache_p < 0):
            raise Invalid("单价不能为负数")
        currency = (item.get("currency") or "USD").strip().upper()[:8] or "USD"
        if existing is None:
            existing = LlmPricing(
                provider_code=provider_code,
                model_id=model_id,
                input_per_1m=in_p,
                output_per_1m=out_p,
                cache_hit_per_1m=cache_p,
                currency=currency,
                updated_by=actor.id,
            )
            session.add(existing)
        else:
            existing.input_per_1m = in_p
            existing.output_per_1m = out_p
            existing.cache_hit_per_1m = cache_p
            existing.currency = currency
            existing.updated_by = actor.id
            existing.updated_at = utcnow()
    session.flush()
    audit.record(session, actor, "llm_pricing.update", "llm_pricing", None, count=len(items))
    return list_pricing(session)


def quota_view(row: LlmQuota) -> dict[str, Any]:
    return {
        "id": row.id,
        "scope_type": row.scope_type,
        "scope_id": row.scope_id,
        "daily_token_limit": row.daily_token_limit,
        "monthly_token_limit": row.monthly_token_limit,
        "daily_cost_limit": float(row.daily_cost_limit) if row.daily_cost_limit is not None else None,
        "monthly_cost_limit": float(row.monthly_cost_limit) if row.monthly_cost_limit is not None else None,
        "rpm_limit": row.rpm_limit,
        "enabled": row.enabled,
        "updated_at": row.updated_at.isoformat(),
    }


def list_quotas(session: Session) -> list[dict[str, Any]]:
    return [quota_view(q) for q in session.scalars(select(LlmQuota).order_by(LlmQuota.scope_type, LlmQuota.scope_id))]


def upsert_quota(session: Session, actor: User, body: dict[str, Any]) -> dict[str, Any]:
    scope_type = body["scope_type"]
    if scope_type not in ("global", "user", "domain"):
        raise Invalid("scope_type 必须是 global、user 或 domain")
    scope_id = "*" if scope_type == "global" else (body.get("scope_id") or "").strip()
    if not scope_id:
        raise Invalid("scope_id 不能为空")
    row = session.scalar(select(LlmQuota).where(LlmQuota.scope_type == scope_type, LlmQuota.scope_id == scope_id))
    daily_cost = Decimal(str(body["daily_cost_limit"])) if body.get("daily_cost_limit") is not None else None
    monthly_cost = Decimal(str(body["monthly_cost_limit"])) if body.get("monthly_cost_limit") is not None else None
    if row is None:
        row = LlmQuota(
            scope_type=scope_type,
            scope_id=scope_id,
            daily_token_limit=body.get("daily_token_limit"),
            monthly_token_limit=body.get("monthly_token_limit"),
            daily_cost_limit=daily_cost,
            monthly_cost_limit=monthly_cost,
            rpm_limit=body.get("rpm_limit"),
            enabled=bool(body.get("enabled", True)),
            updated_by=actor.id,
        )
        session.add(row)
    else:
        row.daily_token_limit = body.get("daily_token_limit")
        row.monthly_token_limit = body.get("monthly_token_limit")
        row.daily_cost_limit = daily_cost
        row.monthly_cost_limit = monthly_cost
        row.rpm_limit = body.get("rpm_limit")
        row.enabled = bool(body.get("enabled", True))
        row.updated_by = actor.id
        row.updated_at = utcnow()
    session.flush()
    audit.record(session, actor, "llm_quota.upsert", "llm_quota", row.id, scope_type=scope_type, scope_id=scope_id)
    return quota_view(row)


def delete_quota(session: Session, actor: User, quota_id: str) -> None:
    row = session.get(LlmQuota, quota_id)
    if row is None:
        raise NotFound("配额规则不存在")
    audit.record(
        session, actor, "llm_quota.delete", "llm_quota", row.id, scope_type=row.scope_type, scope_id=row.scope_id
    )
    session.delete(row)
    session.flush()


def usage_summary(
    session: Session,
    *,
    user_id: str | None = None,
    domain_id: str | None = None,
    days: int = 30,
) -> dict[str, Any]:
    since = utcnow() - timedelta(days=max(1, min(days, 365)))
    filters = [LlmUsageLedger.created_at >= since]
    if user_id:
        filters.append(LlmUsageLedger.user_id == user_id)
    if domain_id:
        filters.append(LlmUsageLedger.domain_id == domain_id)

    totals_row = session.execute(
        select(
            func.count(LlmUsageLedger.id),
            func.coalesce(
                func.sum(
                    case(
                        (or_(LlmUsageLedger.failover_from.is_not(None), LlmUsageLedger.status == "failover"), 1),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(func.sum(case((LlmUsageLedger.status == "error", 1), else_=0)), 0),
            func.coalesce(func.sum(LlmUsageLedger.input_tokens), 0),
            func.coalesce(func.sum(LlmUsageLedger.output_tokens), 0),
            func.coalesce(func.sum(LlmUsageLedger.cache_hit_tokens), 0),
            func.coalesce(func.sum(LlmUsageLedger.reasoning_tokens), 0),
            func.coalesce(func.sum(LlmUsageLedger.total_tokens), 0),
            func.coalesce(func.sum(LlmUsageLedger.estimated_cost), 0),
        ).where(*filters)
    ).one()

    total_calls = int(totals_row[0] or 0)
    failover_count = int(totals_row[1] or 0)
    error_count = int(totals_row[2] or 0)
    in_tok = int(totals_row[3] or 0)
    out_tok = int(totals_row[4] or 0)
    cache_tok = int(totals_row[5] or 0)
    reason_tok = int(totals_row[6] or 0)
    tot_tok = int(totals_row[7] or 0)
    tot_cost = Decimal(str(totals_row[8] or 0))

    currency_rows = session.execute(
        select(LlmUsageLedger.currency, func.coalesce(func.sum(LlmUsageLedger.estimated_cost), 0))
        .where(*filters)
        .group_by(LlmUsageLedger.currency)
    ).all()
    cost_by_currency: dict[str, float] = {}
    for cur, amt in currency_rows:
        code = (cur or "USD").upper()
        cost_by_currency[code] = float(Decimal(str(amt or 0)).quantize(_COST_QUANT))
    if not cost_by_currency:
        currency_label = "USD"
    elif len(cost_by_currency) == 1:
        currency_label = next(iter(cost_by_currency))
    else:
        currency_label = "MIXED"

    model_rows = session.execute(
        select(
            LlmUsageLedger.provider_code,
            LlmUsageLedger.model_id,
            func.count(LlmUsageLedger.id),
            func.coalesce(func.sum(LlmUsageLedger.total_tokens), 0),
            func.coalesce(func.sum(LlmUsageLedger.estimated_cost), 0),
        )
        .where(*filters)
        .group_by(LlmUsageLedger.provider_code, LlmUsageLedger.model_id)
    ).all()
    by_model = [
        {
            "provider_code": pcode,
            "model_id": mid,
            "calls": int(calls or 0),
            "total_tokens": int(tokens or 0),
            "estimated_cost": float(Decimal(str(cost or 0)).quantize(_COST_QUANT)),
        }
        for pcode, mid, calls, tokens, cost in model_rows
    ]
    by_model.sort(key=lambda x: x["total_tokens"], reverse=True)

    purpose_rows = session.execute(
        select(
            LlmUsageLedger.purpose,
            func.count(LlmUsageLedger.id),
            func.coalesce(func.sum(LlmUsageLedger.total_tokens), 0),
            func.coalesce(func.sum(LlmUsageLedger.estimated_cost), 0),
        )
        .where(*filters)
        .group_by(LlmUsageLedger.purpose)
    ).all()
    by_purpose = [
        {
            "purpose": purpose,
            "calls": int(calls or 0),
            "total_tokens": int(tokens or 0),
            "estimated_cost": float(Decimal(str(cost or 0)).quantize(_COST_QUANT)),
        }
        for purpose, calls, tokens, cost in purpose_rows
    ]
    by_purpose.sort(key=lambda x: x["total_tokens"], reverse=True)

    day_expr = func.date(LlmUsageLedger.created_at)
    daily_rows = session.execute(
        select(
            day_expr,
            func.count(LlmUsageLedger.id),
            func.coalesce(func.sum(LlmUsageLedger.total_tokens), 0),
            func.coalesce(func.sum(LlmUsageLedger.estimated_cost), 0),
            func.coalesce(
                func.sum(case((LlmUsageLedger.purpose.in_(("ask", "explain")), LlmUsageLedger.total_tokens), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case((LlmUsageLedger.purpose == "analysis", LlmUsageLedger.total_tokens), else_=0)), 0
            ),
        )
        .where(*filters)
        .group_by(day_expr)
    ).all()
    daily = [
        {
            "date": day_val.isoformat() if hasattr(day_val, "isoformat") else str(day_val)[:10],
            "calls": int(calls or 0),
            "total_tokens": int(tokens or 0),
            "estimated_cost": float(Decimal(str(cost or 0)).quantize(_COST_QUANT)),
            "ask_tokens": int(ask_tok or 0),
            "analysis_tokens": int(ana_tok or 0),
        }
        for day_val, calls, tokens, cost, ask_tok, ana_tok in daily_rows
    ]
    daily.sort(key=lambda x: x["date"])

    return {
        "days": days,
        "total_calls": total_calls,
        "failover_count": failover_count,
        "error_count": error_count,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cache_hit_tokens": cache_tok,
        "reasoning_tokens": reason_tok,
        "total_tokens": tot_tok,
        "estimated_cost": float(tot_cost.quantize(_COST_QUANT)),
        "currency": currency_label,
        "cost_by_currency": cost_by_currency,
        "by_model": by_model,
        "by_purpose": by_purpose,
        "daily": daily,
    }


def ledger_item_view(row: LlmUsageLedger) -> dict[str, Any]:
    return {
        "id": row.id,
        "created_at": row.created_at.isoformat(),
        "user_id": row.user_id,
        "domain_id": row.domain_id,
        "purpose": row.purpose,
        "phase": row.phase,
        "provider_id": row.provider_id,
        "provider_code": row.provider_code,
        "model_id": row.model_id,
        "adapter": row.adapter,
        "thread_id": row.thread_id,
        "answer_id": row.answer_id,
        "run_id": row.run_id,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "cache_hit_tokens": row.cache_hit_tokens,
        "reasoning_tokens": row.reasoning_tokens,
        "total_tokens": row.total_tokens,
        "estimated_cost": float(Decimal(row.estimated_cost).quantize(_COST_QUANT)),
        "currency": row.currency,
        "latency_ms": row.latency_ms,
        "status": row.status,
        "failover_from": row.failover_from,
        "routing_tier": row.routing_tier,
        "error_code": row.error_code,
    }


def usage_ledger(
    session: Session,
    *,
    user_id: str | None = None,
    domain_id: str | None = None,
    purpose: str | None = None,
    status_filter: str | None = None,
    model_id: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    days: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    filters = []
    if user_id:
        filters.append(LlmUsageLedger.user_id == user_id)
    if domain_id:
        filters.append(LlmUsageLedger.domain_id == domain_id)
    if purpose:
        filters.append(LlmUsageLedger.purpose == purpose)
    if status_filter:
        filters.append(LlmUsageLedger.status == status_filter)
    if model_id:
        filters.append(LlmUsageLedger.model_id == model_id)
    if start_date is not None:
        filters.append(LlmUsageLedger.created_at >= datetime.combine(start_date, time.min))
    elif days is not None:
        filters.append(LlmUsageLedger.created_at >= utcnow() - timedelta(days=max(1, min(days, 365))))
    if end_date is not None:
        filters.append(LlmUsageLedger.created_at <= datetime.combine(end_date, time.max))

    total = int(session.scalar(select(func.count()).select_from(LlmUsageLedger).where(*filters)) or 0)
    clamped_limit = max(1, min(limit, 200))
    clamped_offset = max(0, offset)
    items = [
        ledger_item_view(r)
        for r in session.scalars(
            select(LlmUsageLedger)
            .where(*filters)
            .order_by(LlmUsageLedger.created_at.desc(), LlmUsageLedger.id.desc())
            .limit(clamped_limit)
            .offset(clamped_offset)
        )
    ]
    return {"items": items, "total": total, "limit": clamped_limit, "offset": clamped_offset}


def user_quota_status(session: Session, user: User) -> dict[str, Any]:
    day_start, month_start = _utc_windows()
    summary = usage_summary(session, user_id=user.id, days=30)
    daily_tok, daily_cost = _scope_usage(session, "user", user.id, since=day_start)
    monthly_tok, monthly_cost = _scope_usage(session, "user", user.id, since=month_start)
    domain_ids = sorted(access.domain_ids(session, user))
    quotas: list[dict[str, Any]] = []
    for scope_type, scope_id in [("global", "*"), ("user", user.id), *[("domain", d) for d in domain_ids]]:
        q = session.scalar(
            select(LlmQuota).where(
                LlmQuota.scope_type == scope_type, LlmQuota.scope_id == scope_id, LlmQuota.enabled.is_(True)
            )
        )
        if q is None:
            continue
        q_daily_tok, q_daily_cost = _scope_usage(session, scope_type, scope_id, since=day_start)
        q_monthly_tok, q_monthly_cost = _scope_usage(session, scope_type, scope_id, since=month_start)
        quotas.append(
            {
                **quota_view(q),
                "used_daily_tokens": q_daily_tok,
                "used_monthly_tokens": q_monthly_tok,
                "used_daily_cost": float(q_daily_cost.quantize(_COST_QUANT)),
                "used_monthly_cost": float(q_monthly_cost.quantize(_COST_QUANT)),
            }
        )
    return {
        "summary": summary,
        "today_tokens": daily_tok,
        "today_cost": float(daily_cost.quantize(_COST_QUANT)),
        "month_tokens": monthly_tok,
        "month_cost": float(monthly_cost.quantize(_COST_QUANT)),
        "quotas": quotas,
    }
