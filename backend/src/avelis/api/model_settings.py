"""模型配置接口：提供方、模型白名单、连接检测、按用途的默认模型、数据外发策略。只有管理员可以修改。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis import modelcfg
from avelis.api.deps import current_user, get_session
from avelis.governance import access
from avelis.models import ModelProvider, User

router = APIRouter(prefix="/api", tags=["模型配置"])


class ModelEntry(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    tool_calling: bool | None = None


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


class Target(BaseModel):
    provider_id: str
    model_id: str
    reasoning_effort: Literal["off", "low", "medium", "high", "max"] | None = Field(
        default=None, description="思考强度；不写时按该用途的默认档位")


class EffortOut(BaseModel):
    value: str
    label: str
    effective: str = Field(description="实际生效的档位（提供方会把某些档位映射成另一档）")
    effective_label: str


class ModelOut(BaseModel):
    id: str
    enabled: bool
    tool_calling: bool | None = None
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


class PolicyOut(BaseModel):
    send_results: bool
    send_code: bool


class SettingsOut(BaseModel):
    assignments: dict[str, AssignmentOut]
    policy: PolicyOut
    mode: Literal["configured", "demo"]
    note: str | None


class FetchedOut(BaseModel):
    models: list[str]


class SettingsBody(BaseModel):
    assignments: dict[str, Target | None] = Field(default_factory=dict)
    policy: dict[str, bool] | None = None


@router.get("/model-providers/presets", response_model=PresetsOut)
def presets(user: User = Depends(current_user)) -> dict:
    return {"presets": modelcfg.PRESETS, "purposes": modelcfg.PURPOSES}


@router.get("/model-providers", response_model=list[ProviderOut])
def list_providers(user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    access.require_role(session, user, "steward")
    return [modelcfg.provider_view(p) for p in session.scalars(select(ModelProvider).order_by(ModelProvider.name))]


@router.post("/model-providers", status_code=201, response_model=ProviderOut)
def create_provider(body: ProviderCreate, user: User = Depends(current_user),
                    session: Session = Depends(get_session)) -> dict:
    access.require_role(session, user, "admin")
    p = modelcfg.create_provider(session, user, code=body.code, preset=body.preset, name=body.name, kind=body.kind,
                                 base_url=body.base_url, api_key=body.api_key,
                                 models=[m.model_dump() for m in body.models])
    return modelcfg.provider_view(p)


@router.patch("/model-providers/{provider_id}", response_model=ProviderOut)
def update_provider(provider_id: str, body: ProviderUpdate, user: User = Depends(current_user),
                    session: Session = Depends(get_session)) -> dict:
    access.require_role(session, user, "admin")
    changes = body.model_dump(exclude_none=True)
    if body.models is not None:
        changes["models"] = [m.model_dump() for m in body.models]
    return modelcfg.provider_view(modelcfg.update_provider(session, user, provider_id, changes))


@router.delete("/model-providers/{provider_id}", status_code=204)
def delete_provider(provider_id: str, user: User = Depends(current_user),
                    session: Session = Depends(get_session)) -> None:
    access.require_role(session, user, "admin")
    modelcfg.delete_provider(session, user, provider_id)


@router.post("/model-providers/{provider_id}/fetch-models", response_model=FetchedOut)
def fetch_models(provider_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    access.require_role(session, user, "admin")
    return {"models": modelcfg.fetch_models(session, user, provider_id)}


@router.post("/model-providers/{provider_id}/check", response_model=CheckOut)
def check(provider_id: str, body: CheckBody, user: User = Depends(current_user),
          session: Session = Depends(get_session)) -> dict:
    access.require_role(session, user, "admin")
    return modelcfg.check(session, user, provider_id, body.model_id)


@router.get("/model-settings", response_model=SettingsOut)
def get_settings_view(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return modelcfg.settings_view(session)


@router.put("/model-settings", response_model=SettingsOut)
def save_settings(body: SettingsBody, user: User = Depends(current_user),
                  session: Session = Depends(get_session)) -> dict:
    access.require_role(session, user, "admin")
    assignments = {k: (v.model_dump() if v else None) for k, v in body.assignments.items()}
    return modelcfg.save_settings(session, user, assignments, body.policy)
