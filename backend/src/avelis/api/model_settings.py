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


class SettingsBody(BaseModel):
    assignments: dict[str, Target | None] = Field(default_factory=dict)
    policy: dict[str, bool] | None = None


@router.get("/model-providers/presets")
def presets(user: User = Depends(current_user)) -> dict:
    return {"presets": modelcfg.PRESETS, "purposes": modelcfg.PURPOSES}


@router.get("/model-providers")
def list_providers(user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    access.require_role(session, user, "steward")
    return [modelcfg.provider_view(p) for p in session.scalars(select(ModelProvider).order_by(ModelProvider.name))]


@router.post("/model-providers", status_code=201)
def create_provider(body: ProviderCreate, user: User = Depends(current_user),
                    session: Session = Depends(get_session)) -> dict:
    access.require_role(session, user, "admin")
    p = modelcfg.create_provider(session, user, code=body.code, preset=body.preset, name=body.name, kind=body.kind,
                                 base_url=body.base_url, api_key=body.api_key,
                                 models=[m.model_dump() for m in body.models])
    return modelcfg.provider_view(p)


@router.patch("/model-providers/{provider_id}")
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


@router.post("/model-providers/{provider_id}/fetch-models")
def fetch_models(provider_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    access.require_role(session, user, "admin")
    return {"models": modelcfg.fetch_models(session, user, provider_id)}


@router.post("/model-providers/{provider_id}/check")
def check(provider_id: str, body: CheckBody, user: User = Depends(current_user),
          session: Session = Depends(get_session)) -> dict:
    access.require_role(session, user, "admin")
    return modelcfg.check(session, user, provider_id, body.model_id)


@router.get("/model-settings")
def get_settings_view(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return modelcfg.settings_view(session)


@router.put("/model-settings")
def save_settings(body: SettingsBody, user: User = Depends(current_user),
                  session: Session = Depends(get_session)) -> dict:
    access.require_role(session, user, "admin")
    assignments = {k: (v.model_dump() if v else None) for k, v in body.assignments.items()}
    return modelcfg.save_settings(session, user, assignments, body.policy)
