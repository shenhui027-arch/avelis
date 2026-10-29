"""模型配置：提供方、按用途指定模型、数据外发策略、连接检测。

页面与流程参考开源项目的模型配置页：
- LobeChat / Cherry Studio：左侧提供方列表与启用开关，右侧密钥、接口地址（显示实际请求端点）、
  “检测”、“获取模型列表”后勾选启用；
- Open WebUI：用 /models 验证连接、模型 ID 白名单；
- Dify：按用途设置默认模型（这里是 问数理解 / 结果解释 / 导入血缘推断）。
Avelis 额外要求：检测会实际发起一次带工具定义的对话，确认模型支持工具调用（问数智能体依赖它）；
数据外发策略（是否发送查询结果、是否发送脱敏后的代码片段）由管理员显式开启。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timezone
from typing import Any

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

import avelis.ask.llm  # noqa: F401 - 先关闭外部追踪
from avelis import audit
from avelis.db import utcnow
from avelis.governance.errors import Conflict, Invalid, NotFound
from avelis.models import AppSetting, ModelAssignment, ModelProvider, User
from avelis.security import decrypt_secret, encrypt_secret

PURPOSES = {
    "ask": {"label": "问数理解", "needs_tools": True,
            "hint": "理解问题、检索口径、追问；必须支持工具调用"},
    "explain": {"label": "结果解释", "needs_tools": False,
                "hint": "用自然语言解释查询结果；仅在允许发送查询结果时使用"},
    "import": {"label": "导入血缘推断", "needs_tools": False,
               "hint": "对确定性解析覆盖不到的代码片段提出候选血缘"},
}

PRESETS = {
    "deepseek": {"name": "DeepSeek", "kind": "openai_compatible", "base_url": "https://api.deepseek.com"},
    "openai": {"name": "OpenAI", "kind": "openai_compatible", "base_url": "https://api.openai.com/v1"},
    "anthropic": {"name": "Anthropic", "kind": "anthropic", "base_url": "https://api.anthropic.com"},
    "dashscope": {"name": "通义千问（兼容模式）", "kind": "openai_compatible",
                  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    "custom": {"name": "自定义（OpenAI 兼容）", "kind": "openai_compatible", "base_url": ""},
}

POLICY_KEY = "model_policy"
DEFAULT_POLICY = {"send_results": False, "send_code": False}


# --- 视图 -------------------------------------------------------------------------

def mask(hint: str | None) -> str | None:
    return f"••••••••{hint}" if hint else None


def endpoint_preview(provider: ModelProvider) -> str:
    base = provider.base_url.rstrip("/")
    return f"{base}/v1/messages" if provider.kind == "anthropic" else f"{base}/chat/completions"


def provider_view(p: ModelProvider) -> dict[str, Any]:
    return {"id": p.id, "code": p.code, "name": p.name, "kind": p.kind, "preset": p.preset, "base_url": p.base_url,
            "endpoint": endpoint_preview(p), "api_key": mask(p.api_key_hint), "has_api_key": bool(p.api_key_enc),
            "enabled": p.enabled, "models": p.models, "last_check": p.last_check,
            "updated_at": p.updated_at.isoformat()}  # UTCDateTime 读出时带 UTC 时区


def policy(session: Session) -> dict[str, bool]:
    row = session.get(AppSetting, POLICY_KEY)
    return {**DEFAULT_POLICY, **(row.value if row else {})}


def settings_view(session: Session) -> dict[str, Any]:
    assignments = {a.purpose: a for a in session.scalars(select(ModelAssignment))}
    out = {}
    for purpose, meta in PURPOSES.items():
        a = assignments.get(purpose)
        out[purpose] = {**meta, "provider_id": a.provider_id if a else None, "model_id": a.model_id if a else None,
                        "updated_at": a.updated_at.isoformat() if a else None}
    return {"assignments": out, "policy": policy(session),
            "mode": "configured" if "ask" in assignments else "demo",
            "note": None if "ask" in assignments else "未指定问数理解模型：使用演示模型（规则匹配）"}


# --- 维护 -------------------------------------------------------------------------

def _get(session: Session, provider_id: str) -> ModelProvider:
    p = session.get(ModelProvider, provider_id)
    if p is None:
        raise NotFound("模型提供方不存在")
    return p


def _set_key(p: ModelProvider, api_key: str | None) -> None:
    if api_key:
        p.api_key_enc = encrypt_secret(api_key)
        p.api_key_hint = api_key[-4:]


def _normalise_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for m in models:
        mid = str(m.get("id", "")).strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append({"id": mid, "enabled": bool(m.get("enabled", True)), "tool_calling": m.get("tool_calling")})
    return out


def create_provider(session: Session, actor: User, *, code: str, preset: str | None, name: str | None,
                    kind: str | None, base_url: str | None, api_key: str | None,
                    models: list[dict[str, Any]]) -> ModelProvider:
    base = PRESETS.get(preset or "custom")
    if base is None:
        raise Invalid(f"未知预设：{preset}")
    kind = kind or base["kind"]
    if kind not in ("openai_compatible", "anthropic"):
        raise Invalid("kind 只能是 openai_compatible 或 anthropic")
    url = (base_url or base["base_url"]).strip()
    if not url.startswith(("https://", "http://")):
        raise Invalid("接口地址必须以 http:// 或 https:// 开头")
    if session.scalar(select(ModelProvider.id).where(ModelProvider.code == code)):
        raise Conflict(f"提供方编码 {code} 已存在")
    p = ModelProvider(code=code, name=name or base["name"], kind=kind, preset=preset, base_url=url,
                      models=_normalise_models(models), created_by=actor.id, enabled=True)
    _set_key(p, api_key)
    session.add(p)
    session.flush()
    audit.record(session, actor, "model_provider.create", "model_provider", p.id, code=code, kind=kind,
                 base_url=url, api_key_set=bool(api_key))
    return p


def update_provider(session: Session, actor: User, provider_id: str, changes: dict[str, Any]) -> ModelProvider:
    p = _get(session, provider_id)
    fields = []
    for key in ("name", "base_url", "enabled"):
        if changes.get(key) is not None:
            setattr(p, key, changes[key])
            fields.append(key)
    if changes.get("models") is not None:
        p.models = _normalise_models(changes["models"])
        fields.append("models")
    if changes.get("api_key"):
        _set_key(p, changes["api_key"])
        p.last_check = {}
        fields.append("api_key")
    if changes.get("clear_api_key"):
        p.api_key_enc, p.api_key_hint = None, None
        fields.append("api_key_cleared")
    p.updated_at = utcnow()
    if p.enabled is False and session.scalar(select(ModelAssignment.purpose).where(
            ModelAssignment.provider_id == p.id).limit(1)):
        raise Conflict("该提供方正被用途使用，先在“默认模型”中改用其他模型再停用")
    audit.record(session, actor, "model_provider.update", "model_provider", p.id, fields=fields)
    return p


def delete_provider(session: Session, actor: User, provider_id: str) -> None:
    p = _get(session, provider_id)
    if session.scalar(select(ModelAssignment.purpose).where(ModelAssignment.provider_id == p.id).limit(1)):
        raise Conflict("该提供方正被用途使用，不能删除")
    audit.record(session, actor, "model_provider.delete", "model_provider", p.id, code=p.code)
    session.delete(p)


def save_settings(session: Session, actor: User, assignments: dict[str, dict | None],
                  new_policy: dict[str, bool] | None) -> dict[str, Any]:
    for purpose, target in assignments.items():
        if purpose not in PURPOSES:
            raise Invalid(f"未知用途：{purpose}")
        current = session.get(ModelAssignment, purpose)
        if not target:
            if current is not None:
                session.delete(current)
            continue
        p = _get(session, target["provider_id"])
        if not p.enabled or not p.api_key_enc:
            raise Conflict(f"提供方 {p.name} 未启用或未设置密钥")
        model = next((m for m in p.models if m["id"] == target["model_id"] and m["enabled"]), None)
        if model is None:
            raise Conflict(f"模型 {target['model_id']} 不在 {p.name} 的启用列表中")
        if PURPOSES[purpose]["needs_tools"] and model.get("tool_calling") is False:
            raise Conflict(f"{PURPOSES[purpose]['label']}需要工具调用，检测显示 {model['id']} 不支持")
        if current is None:
            current = ModelAssignment(purpose=purpose, provider_id=p.id, model_id=model["id"], updated_by=actor.id)
            session.add(current)
        current.provider_id, current.model_id = p.id, model["id"]
        current.updated_by, current.updated_at = actor.id, utcnow()
    if new_policy is not None:
        row = session.get(AppSetting, POLICY_KEY)
        value = {**DEFAULT_POLICY, **{k: bool(v) for k, v in new_policy.items() if k in DEFAULT_POLICY}}
        if row is None:
            session.add(AppSetting(key=POLICY_KEY, value=value, updated_by=actor.id))
        else:
            row.value, row.updated_by, row.updated_at = value, actor.id, utcnow()
    session.flush()
    audit.record(session, actor, "model_settings.update", "model_settings", None,
                 assignments={k: (v or {}).get("model_id") for k, v in assignments.items()}, policy=new_policy)
    return settings_view(session)


# --- 调用 -------------------------------------------------------------------------

def build_chat_model(p: ModelProvider, model_id: str, *, temperature: float = 0.0) -> BaseChatModel:
    if not p.api_key_enc:
        raise Conflict(f"提供方 {p.name} 未设置密钥")
    key = decrypt_secret(p.api_key_enc)
    if p.kind == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model_id, api_key=key, base_url=p.base_url, temperature=temperature, timeout=60,
                             max_retries=2, max_tokens=2048)
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=model_id, api_key=key, base_url=p.base_url, temperature=temperature, timeout=60,
                      max_retries=2)


@dataclass
class Resolved:
    model: BaseChatModel
    adapter: str


def resolve(session: Session, purpose: str, *, temperature: float = 0.0) -> Resolved | None:
    """按用途取模型；未指定（或提供方已停用）返回 None，调用方使用演示模型。"""
    a = session.get(ModelAssignment, purpose)
    if a is None and purpose == "explain":
        a = session.get(ModelAssignment, "ask")
    if a is None:
        return None
    p = session.get(ModelProvider, a.provider_id)
    if p is None or not p.enabled:
        return None
    return Resolved(build_chat_model(p, a.model_id, temperature=temperature), f"{p.code}:{a.model_id}")


def fetch_models(session: Session, actor: User, provider_id: str) -> list[str]:
    """调用提供方的模型列表接口（OpenAI 兼容：GET /models；Anthropic：GET /v1/models）。不自动启用。"""
    p = _get(session, provider_id)
    if not p.api_key_enc:
        raise Conflict("未设置密钥")
    key = decrypt_secret(p.api_key_enc)
    base = p.base_url.rstrip("/")
    if p.kind == "anthropic":
        url, headers = f"{base}/v1/models", {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        url, headers = f"{base}/models", {"Authorization": f"Bearer {key}"}
    try:
        resp = httpx.get(url, headers=headers, timeout=20)
    except httpx.HTTPError as exc:
        raise Conflict(f"无法连接 {url}：{type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise Conflict(f"获取模型列表失败：HTTP {resp.status_code}", body=resp.text[:300])
    ids = sorted({m.get("id") for m in resp.json().get("data", []) if m.get("id")})
    audit.record(session, actor, "model_provider.fetch_models", "model_provider", p.id, count=len(ids))
    return ids


class _PingArgs(BaseModel):
    value: str


def check(session: Session, actor: User, provider_id: str, model_id: str) -> dict[str, Any]:
    """检测：发起一次带工具定义的极小对话，记录延迟与是否发生工具调用。不发送任何业务数据。"""
    p = _get(session, provider_id)
    started = time.monotonic()
    result: dict[str, Any] = {"model_id": model_id,
                              "checked_at": utcnow().replace(tzinfo=timezone.utc).isoformat()}
    try:
        model = build_chat_model(p, model_id)
        tool = StructuredTool.from_function(lambda value: value, name="echo", args_schema=_PingArgs,
                                            description="原样返回 value")
        reply = model.bind_tools([tool]).invoke(
            [HumanMessage("这是连通性检测。请调用 echo 工具，参数 value 为 ok。")])
        result.update(ok=True, tool_calling=bool(reply.tool_calls))
    except Exception as exc:  # 失败原因如实返回（不含密钥）
        result.update(ok=False, tool_calling=None, error=f"{type(exc).__name__}: {str(exc)[:300]}")
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    p.last_check = result
    # 复制后整体赋值：原地修改 JSONB 中的字典不会被识别为变更
    p.models = [{**m, "tool_calling": result["tool_calling"]} if m["id"] == model_id and result["ok"] else dict(m)
                for m in p.models]
    audit.record(session, actor, "model_provider.check", "model_provider", p.id, model_id=model_id,
                 ok=result["ok"], tool_calling=result.get("tool_calling"))
    return result
