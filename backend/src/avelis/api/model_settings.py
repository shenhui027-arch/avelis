"""模型配置接口：提供方、模型白名单、连接检测、按用途的默认模型、数据外发策略。只有管理员可以修改。"""

from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis import modelcfg, usage
from avelis.api.deps import current_user, get_session
from avelis.governance import access
from avelis.models import ModelProvider, User

router = APIRouter(prefix="/api", tags=["模型配置"])


class ModelEntry(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    tool_calling: bool | None = None
    context_window: int | None = Field(default=None, strict=True)


class ProviderCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    preset: Literal["deepseek", "openai", "anthropic", "dashscope", "custom"] = "custom"
    name: str | None = Field(default=None, max_length=128)
    kind: Literal["openai_compatible", "anthropic"] | None = None
    base_url: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=512, repr=False)
    models: list[ModelEntry] = Field(default_factory=list)


class ProviderUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    base_url: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=512, repr=False)
    clear_api_key: bool = False
    enabled: bool | None = None
    models: list[ModelEntry] | None = None


class CheckBody(BaseModel):
    model_id: str = Field(min_length=1, max_length=128)


class BackupTarget(BaseModel):
    provider_id: str
    model_id: str
    reasoning_effort: Literal["off", "low", "medium", "high", "max"] | None = None


class Target(BaseModel):
    provider_id: str
    model_id: str
    reasoning_effort: Literal["off", "low", "medium", "high", "max"] | None = Field(
        default=None, description="思考强度；不写时按该用途的默认档位"
    )
    backups: list[BackupTarget] | None = Field(default=None, max_length=5, description="备选容灾模型（最多 5 个）")


class RoutingConfig(BaseModel):
    enabled: bool = False
    simple_target: BackupTarget | None = None
    complex_target: BackupTarget | None = None


class EffortOut(BaseModel):
    value: str
    label: str
    effective: str = Field(description="实际生效的档位（提供方会把某些档位映射成另一档）")
    effective_label: str


class ModelOut(BaseModel):
    id: str
    enabled: bool
    tool_calling: bool | None = None
    context_window: int | None = None
    efforts: list[EffortOut] = Field(default_factory=list, description="该模型支持的思考档位；为空时不支持设置")


class CheckOut(BaseModel):
    model_id: str | None = None
    ok: bool | None = None
    tool_calling: bool | None = None
    latency_ms: int | None = None
    error: str | None = None
    checked_at: str | None = None


class ProviderOut(BaseModel):
    id: str
    code: str
    name: str
    kind: Literal["openai_compatible", "anthropic"]
    preset: str | None
    base_url: str
    endpoint: str
    api_key: str | None = Field(description="掩码，只显示末四位；密钥本身永不回传")
    has_api_key: bool
    enabled: bool
    models: list[ModelOut]
    last_check: CheckOut
    updated_at: str


class PresetOut(BaseModel):
    name: str
    kind: str
    base_url: str


class PurposeOut(BaseModel):
    label: str
    needs_tools: bool
    hint: str


class PresetsOut(BaseModel):
    presets: dict[str, PresetOut]
    purposes: dict[str, PurposeOut]


class AssignmentOut(PurposeOut):
    provider_id: str | None
    model_id: str | None
    updated_at: str | None
    reasoning_effort: str
    default_effort: str
    effective_effort: str | None
    backups: list[BackupTarget] = Field(default_factory=list)


class PolicyOut(BaseModel):
    send_results: bool
    send_code: bool


class SettingsOut(BaseModel):
    assignments: dict[str, AssignmentOut]
    policy: PolicyOut
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    mode: Literal["configured", "demo"]
    note: str | None


class FetchedOut(BaseModel):
    models: list[str]


class SettingsBody(BaseModel):
    assignments: dict[str, Target | None] = Field(default_factory=dict)
    policy: dict[str, bool] | None = None
    routing: RoutingConfig | None = None


class PricingItemIn(BaseModel):
    provider_code: str = Field(default="*", max_length=64)
    model_id: str = Field(min_length=1, max_length=128)
    input_per_1m: float = Field(default=0.0, ge=0.0)
    output_per_1m: float = Field(default=0.0, ge=0.0)
    cache_hit_per_1m: float | None = Field(default=None, ge=0.0)
    currency: str = Field(default="USD", min_length=1, max_length=8)
    delete: bool = False


class PricingUpdateBody(BaseModel):
    items: list[PricingItemIn] = Field(min_length=1, max_length=200)


class PricingDefaultOut(BaseModel):
    provider_code: str
    model_id: str
    input_per_1m: float
    output_per_1m: float
    cache_hit_per_1m: float | None = None
    currency: str
    source: str


class PricingOut(PricingDefaultOut):
    id: str
    updated_at: str


class PricingCatalogOut(BaseModel):
    configured: list[PricingOut]
    defaults: list[PricingDefaultOut]


class QuotaUpsertIn(BaseModel):
    scope_type: Literal["global", "user", "domain"]
    scope_id: str = Field(default="*", max_length=64)
    daily_token_limit: int | None = Field(default=None, ge=0)
    monthly_token_limit: int | None = Field(default=None, ge=0)
    daily_cost_limit: float | None = Field(default=None, ge=0.0)
    monthly_cost_limit: float | None = Field(default=None, ge=0.0)
    rpm_limit: int | None = Field(default=None, ge=0)
    enabled: bool = True


class QuotaOut(BaseModel):
    id: str
    scope_type: Literal["global", "user", "domain"]
    scope_id: str
    daily_token_limit: int | None = None
    monthly_token_limit: int | None = None
    daily_cost_limit: float | None = None
    monthly_cost_limit: float | None = None
    rpm_limit: int | None = None
    enabled: bool
    updated_at: str


class QuotaStatusOut(QuotaOut):
    used_daily_tokens: int
    used_monthly_tokens: int
    used_daily_cost: float
    used_monthly_cost: float


class UsageByModelOut(BaseModel):
    provider_code: str | None = None
    model_id: str
    calls: int
    total_tokens: int
    estimated_cost: float


class UsageByPurposeOut(BaseModel):
    purpose: str
    calls: int
    total_tokens: int
    estimated_cost: float


class UsageDailyOut(BaseModel):
    date: str
    calls: int
    total_tokens: int
    estimated_cost: float
    ask_tokens: int
    analysis_tokens: int


class UsageSummaryOut(BaseModel):
    days: int
    total_calls: int
    failover_count: int
    error_count: int
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    reasoning_tokens: int
    total_tokens: int
    estimated_cost: float
    currency: str
    cost_by_currency: dict[str, float] = Field(default_factory=dict)
    by_model: list[UsageByModelOut]
    by_purpose: list[UsageByPurposeOut]
    daily: list[UsageDailyOut]


class UsageLedgerItemOut(BaseModel):
    id: str
    created_at: str
    user_id: str | None = None
    domain_id: str | None = None
    purpose: str
    phase: str | None = None
    provider_id: str | None = None
    provider_code: str | None = None
    model_id: str
    adapter: str | None = None
    thread_id: str | None = None
    answer_id: str | None = None
    run_id: str | None = None
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    reasoning_tokens: int
    total_tokens: int
    estimated_cost: float
    currency: str
    latency_ms: int | None = None
    status: str
    failover_from: str | None = None
    routing_tier: str | None = None
    error_code: str | None = None


class UsageLedgerPageOut(BaseModel):
    items: list[UsageLedgerItemOut]
    total: int
    limit: int
    offset: int


class MyUsageOut(BaseModel):
    summary: UsageSummaryOut
    today_tokens: int
    today_cost: float
    month_tokens: int
    month_cost: float
    quotas: list[QuotaStatusOut]


@router.get("/model-providers/presets", response_model=PresetsOut)
def presets(user: User = Depends(current_user)) -> dict:
    return {"presets": modelcfg.PRESETS, "purposes": modelcfg.PURPOSES}


@router.get("/model-providers", response_model=list[ProviderOut])
def list_providers(user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    access.require_role(session, user, "steward")
    return [modelcfg.provider_view(p) for p in session.scalars(select(ModelProvider).order_by(ModelProvider.name))]


@router.post("/model-providers", status_code=201, response_model=ProviderOut)
def create_provider(
    body: ProviderCreate, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    access.require_role(session, user, "admin")
    p = modelcfg.create_provider(
        session,
        user,
        code=body.code,
        preset=body.preset,
        name=body.name,
        kind=body.kind,
        base_url=body.base_url,
        api_key=body.api_key,
        models=[m.model_dump() for m in body.models],
    )
    return modelcfg.provider_view(p)


@router.patch("/model-providers/{provider_id}", response_model=ProviderOut)
def update_provider(
    provider_id: str, body: ProviderUpdate, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    access.require_role(session, user, "admin")
    changes = body.model_dump(exclude_none=True)
    if body.models is not None:
        changes["models"] = [m.model_dump() for m in body.models]
    return modelcfg.provider_view(modelcfg.update_provider(session, user, provider_id, changes))


@router.delete("/model-providers/{provider_id}", status_code=204)
def delete_provider(
    provider_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> None:
    access.require_role(session, user, "admin")
    modelcfg.delete_provider(session, user, provider_id)


@router.post("/model-providers/{provider_id}/fetch-models", response_model=FetchedOut)
def fetch_models(provider_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    access.require_role(session, user, "admin")
    return {"models": modelcfg.fetch_models(session, user, provider_id)}


@router.post("/model-providers/{provider_id}/check", response_model=CheckOut)
def check(
    provider_id: str, body: CheckBody, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    access.require_role(session, user, "admin")
    return modelcfg.check(session, user, provider_id, body.model_id)


@router.get("/model-settings", response_model=SettingsOut)
def get_settings_view(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return modelcfg.settings_view(session)


@router.put("/model-settings", response_model=SettingsOut)
def save_settings(
    body: SettingsBody, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    access.require_role(session, user, "admin")
    assignments = {k: (v.model_dump(exclude_unset=True) if v else None) for k, v in body.assignments.items()}
    routing = body.routing.model_dump() if body.routing is not None else None
    return modelcfg.save_settings(session, user, assignments, body.policy, new_routing=routing)


@router.get("/model-pricing", response_model=PricingCatalogOut)
def get_model_pricing(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    access.require_role(session, user, "steward")
    return usage.list_pricing(session)


@router.put("/model-pricing", response_model=PricingCatalogOut)
def put_model_pricing(
    body: PricingUpdateBody, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    access.require_role(session, user, "admin")
    return usage.upsert_pricing(session, user, [item.model_dump() for item in body.items])


@router.get("/llm-quotas", response_model=list[QuotaOut])
def get_llm_quotas(user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    access.require_role(session, user, "admin")
    return usage.list_quotas(session)


@router.put("/llm-quotas", response_model=QuotaOut)
def put_llm_quota(
    body: QuotaUpsertIn, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    access.require_role(session, user, "admin")
    return usage.upsert_quota(session, user, body.model_dump())


@router.delete("/llm-quotas/{quota_id}", status_code=204)
def delete_llm_quota(
    quota_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> Response:
    access.require_role(session, user, "admin")
    usage.delete_quota(session, user, quota_id)
    return Response(status_code=204)


@router.get("/llm-usage/summary", response_model=UsageSummaryOut)
def get_llm_usage_summary(
    days: int = Query(30, ge=1, le=365),
    user_id: str | None = None,
    domain_id: str | None = None,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    is_admin = access.is_admin(session, user)
    target_user = user_id if is_admin else user.id
    target_domain = domain_id if is_admin else None
    return usage.usage_summary(session, user_id=target_user, domain_id=target_domain, days=days)


@router.get("/llm-usage/ledger", response_model=UsageLedgerPageOut)
def get_llm_usage_ledger(
    days: int | None = Query(None, ge=1, le=365),
    user_id: str | None = None,
    domain_id: str | None = None,
    purpose: str | None = None,
    status: str | None = None,
    model_id: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    is_admin = access.is_admin(session, user)
    target_user = user_id if is_admin else user.id
    target_domain = domain_id if is_admin else None
    return usage.usage_ledger(
        session,
        user_id=target_user,
        domain_id=target_domain,
        purpose=purpose,
        status_filter=status,
        model_id=model_id,
        start_date=start_date,
        end_date=end_date,
        days=days,
        limit=limit,
        offset=offset,
    )


@router.get("/llm-usage/me", response_model=MyUsageOut)
def get_my_llm_usage(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return usage.user_quota_status(session, user)
