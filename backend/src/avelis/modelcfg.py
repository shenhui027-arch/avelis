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

import re
import time
from dataclasses import dataclass, field
from datetime import timezone
from collections.abc import Callable
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
    "analysis": {"label": "分析", "needs_tools": True,
                 "hint": "多步分析：规划、调用上下文与查询工具、撰写报告；必须支持工具调用；未指定时沿用问数模型"},
}

PRESETS = {
    "deepseek": {"name": "DeepSeek", "kind": "openai_compatible", "base_url": "https://api.deepseek.com"},
    "openai": {"name": "OpenAI", "kind": "openai_compatible", "base_url": "https://api.openai.com/v1"},
    "anthropic": {"name": "Anthropic", "kind": "anthropic", "base_url": "https://api.anthropic.com"},
    "dashscope": {"name": "通义千问（兼容模式）", "kind": "openai_compatible",
                  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    "custom": {"name": "自定义（OpenAI 兼容）", "kind": "openai_compatible", "base_url": ""},
}

# 思考强度（ADR 0012）：页面按提供方实际支持的档位显示；DEFAULT_EFFORT 是各用途的默认档位，改默认值只改这里。
EFFORTS = ("off", "low", "medium", "high", "max")
EFFORT_LABELS = {"off": "关闭", "low": "低", "medium": "中", "high": "高", "max": "最高"}
# 默认“低”（用户 2026-10-30 确认）：DeepSeek 的“中”会按“高”执行
DEFAULT_EFFORT = {"ask": "low", "explain": "low", "import": "low", "analysis": "low"}
# 提供方支持的档位 → 实际发送的档位。DeepSeek API 只有 low / high / max 三档，其他名称按官方映射表归入这三档
# （medium 归入 high；https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/，对所有模型通用），关闭思考用
# thinking.type=disabled，所以页面只列 关闭 / 低 / 高 / 最高。以前保存的“中”按映射表发送 high。
# 实测（deepseek-flash，2026-10-30）：关闭时没有推理 token；low、high 推理量相近，max 明显更多。
# 多轮工具调用按官方协议原样回传 reasoning_content（同一文档：携带 tools 的请求，后续所有请求必须完整回传，否则 400），
# 由 llm_protocol.ReasoningRoundTripChatOpenAI 处理；“几次没报 400”不作为兼容的依据。其他提供方未核实，不传思考参数。
DEEPSEEK_EFFORTS = {"off": "off", "low": "low", "high": "high", "max": "max"}
DEEPSEEK_ALIASES = {"medium": "high"}

POLICY_KEY = "model_policy"
# 默认允许把查询结果与脱敏代码片段发给模型（用户决定，2026-10-29，ADR 0007）：有保密需求时接入本地模型，
# 而不是由产品限制。管理员仍可在模型配置页关闭；关闭后结果解释用模板，分析模式不可用。
DEFAULT_POLICY = {"send_results": True, "send_code": True}


def redact_error(text: str, api_key: str | None = None, *, limit: int = 300) -> str:
    """先移除上游可能回显的凭据，再截断，避免长凭据只被截掉一半。"""
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    text = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/]+@", r"\1[REDACTED]@", text)
    text = re.sub(r'''(?i)\bbearer\s+[^\s"',;}\]]+''', "Bearer [REDACTED]", text)
    text = re.sub(
        r'''(?ix)(["']?(?:authorization|proxy-authorization|x-api-key)["']?\s*[:=]\s*)
        (?:"[^"]*"|'[^']*'|Basic\s+[^\s"',;}\]]+|[^\r\n,;}]+)''',
        r"\1[REDACTED]", text,
    )
    return text[:limit]


def _model_check(p: ModelProvider, model_id: str) -> dict[str, Any]:
    """服务器检测按模型保存在现有 JSON 字段；兼容此前单条检测记录。"""
    checks = p.last_check or {}
    if model_id in checks.get("models", {}):
        return checks["models"][model_id]
    return checks if checks.get("model_id") == model_id else {}


def effort_map(p: ModelProvider | None, model_id: str) -> dict[str, str] | None:
    """该提供方、模型支持的思考档位 → 实际生效的档位；没有核实过的返回 None（不显示选项，也不传思考参数）。"""
    if p is None or p.kind != "openai_compatible":
        return None
    if p.preset == "deepseek" or "api.deepseek.com" in (p.base_url or ""):
        return dict(DEEPSEEK_EFFORTS)
    return None


def effort_options(p: ModelProvider | None, model_id: str) -> list[dict[str, str]]:
    """页面上的档位选项：值、名称、实际生效的档位（与所选不同时页面注明）。"""
    mapping = effort_map(p, model_id) or {}
    return [{"value": e, "label": EFFORT_LABELS[e], "effective": mapping[e], "effective_label": EFFORT_LABELS[mapping[e]]}
            for e in EFFORTS if e in mapping]


def effective_effort(p: ModelProvider | None, model_id: str, effort: str | None) -> str | None:
    """实际生效的档位；提供方没有核实过的档位时为 None（不传思考参数，按提供方默认）。"""
    mapping = effort_map(p, model_id)
    if not mapping:
        return None
    effort = effort or ""
    return mapping.get(effort) or mapping.get(DEEPSEEK_ALIASES.get(effort, ""))


def _effort_kwargs(p: ModelProvider, model_id: str, effort: str | None) -> dict[str, Any]:
    """思考强度的请求参数集中在这里（ADR 0012），调用方不分散判断。"""
    effective = effective_effort(p, model_id, effort)
    if effective is None:
        return {}
    if effective == "off":
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {"reasoning_effort": effective}


def _enabled_model(p: ModelProvider | None, model_id: str, *, needs_tools: bool = False) -> dict | None:
    if p is None or not p.enabled or not p.api_key_enc:
        return None
    model = next((m for m in p.models if m["id"] == model_id and m["enabled"]), None)
    if model is not None and needs_tools:
        check = _model_check(p, model_id)
        if check.get("ok") is not True or check.get("tool_calling") is not True:
            return None
    return model


# --- 视图 -------------------------------------------------------------------------

def mask(hint: str | None) -> str | None:
    return f"••••••••{hint}" if hint else None


def endpoint_preview(provider: ModelProvider) -> str:
    base = provider.base_url.rstrip("/")
    return f"{base}/v1/messages" if provider.kind == "anthropic" else f"{base}/chat/completions"


def provider_view(p: ModelProvider) -> dict[str, Any]:
    return {"id": p.id, "code": p.code, "name": p.name, "kind": p.kind, "preset": p.preset, "base_url": p.base_url,
            "endpoint": endpoint_preview(p), "api_key": mask(p.api_key_hint), "has_api_key": bool(p.api_key_enc),
            "enabled": p.enabled, "models": [{**m, "efforts": effort_options(p, m["id"])} for m in p.models],
            "last_check": p.last_check,
            "updated_at": p.updated_at.isoformat()}  # UTCDateTime 读出时带 UTC 时区


def policy(session: Session) -> dict[str, bool]:
    row = session.get(AppSetting, POLICY_KEY)
    return {**DEFAULT_POLICY, **(row.value if row else {})}


def settings_view(session: Session) -> dict[str, Any]:
    assignments = {a.purpose: a for a in session.scalars(select(ModelAssignment))}
    out = {}
    for purpose, meta in PURPOSES.items():
        a = assignments.get(purpose)
        p = session.get(ModelProvider, a.provider_id) if a else None
        effort = (a.reasoning_effort if a else None) or DEFAULT_EFFORT[purpose]
        effective = effective_effort(p, a.model_id, effort) if a else None
        out[purpose] = {**meta, "provider_id": a.provider_id if a else None, "model_id": a.model_id if a else None,
                        "updated_at": a.updated_at.isoformat() if a else None,
                        "reasoning_effort": effort, "default_effort": DEFAULT_EFFORT[purpose],
                        "effective_effort": effective}
    ask = assignments.get("ask")
    provider = session.get(ModelProvider, ask.provider_id) if ask else None
    configured = ask is not None and _enabled_model(provider, ask.model_id, needs_tools=True) is not None
    return {"assignments": out, "policy": policy(session),
            "mode": "configured" if configured else "demo",
            "note": None if configured else "未配置有效的问数理解模型：使用演示模型（规则匹配）"}


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
    connection_changed = changes.get("base_url") is not None and changes["base_url"] != p.base_url
    for key in ("name", "base_url", "enabled"):
        if changes.get(key) is not None:
            setattr(p, key, changes[key])
            fields.append(key)
    if changes.get("models") is not None:
        p.models = _normalise_models(changes["models"])
        fields.append("models")
    if changes.get("api_key"):
        _set_key(p, changes["api_key"])
        connection_changed = True
        fields.append("api_key")
    if changes.get("clear_api_key"):
        p.api_key_enc, p.api_key_hint = None, None
        connection_changed = True
        fields.append("api_key_cleared")
    if connection_changed:
        p.last_check = {}
        p.models = [{**m, "tool_calling": None} for m in p.models]
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
        if PURPOSES[purpose]["needs_tools"] and _enabled_model(p, model["id"], needs_tools=True) is None:
            raise Conflict(f"{PURPOSES[purpose]['label']}需要该模型最近一次服务器检测成功且支持工具调用")
        effort = target.get("reasoning_effort")
        if effort is not None and effort not in (effort_map(p, model["id"]) or {}):
            raise Invalid(f"{p.name} · {model['id']} 不支持思考强度“{EFFORT_LABELS.get(effort, effort)}”")
        if current is None:
            current = ModelAssignment(purpose=purpose, provider_id=p.id, model_id=model["id"], updated_by=actor.id)
            session.add(current)
        current.provider_id, current.model_id = p.id, model["id"]
        current.reasoning_effort = effort
        current.updated_by, current.updated_at = actor.id, utcnow()
    if new_policy is not None:
        row = session.get(AppSetting, POLICY_KEY)
        # 以当前已保存的策略为基础合并：部分更新不能把管理员明确关闭的设置重新打开（默认值只用于从未保存过的项）
        value = {**policy(session), **{k: bool(v) for k, v in new_policy.items() if k in DEFAULT_POLICY}}
        if row is None:
            session.add(AppSetting(key=POLICY_KEY, value=value, updated_by=actor.id))
        else:
            row.value, row.updated_by, row.updated_at = value, actor.id, utcnow()
    session.flush()
    audit.record(session, actor, "model_settings.update", "model_settings", None,
                 assignments={k: (v or {}).get("model_id") for k, v in assignments.items()}, policy=new_policy)
    return settings_view(session)


# --- 调用 -------------------------------------------------------------------------

def build_chat_model(p: ModelProvider, model_id: str, *, temperature: float = 0.0, timeout: float = 60,
                     max_retries: int = 2, effort: str | None = None, max_tokens: int | None = None) -> BaseChatModel:
    """effort：思考强度（按提供方适配，见 _effort_kwargs；思考开启时 temperature 不生效，不依赖它）；
    max_tokens：输出上限，阶段需要时由系统设定（如理解阶段）。"""
    if not p.api_key_enc:
        raise Conflict(f"提供方 {p.name} 未设置密钥")
    key = decrypt_secret(p.api_key_enc)
    if p.kind == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model_id, api_key=key, base_url=p.base_url, temperature=temperature,
                             timeout=timeout, max_retries=max_retries,
                             max_tokens=max_tokens if max_tokens is not None else 4096)
    from langchain_openai import ChatOpenAI

    from avelis.llm_protocol import ReasoningRoundTripChatOpenAI

    extra = {"max_tokens": max_tokens} if max_tokens else {}
    # 核实过思考协议的提供方（目前只有 DeepSeek）用带 reasoning_content 往返的实现；其余按 OpenAI 规范
    cls = ReasoningRoundTripChatOpenAI if effort_map(p, model_id) else ChatOpenAI
    return cls(model=model_id, api_key=key, base_url=p.base_url, temperature=temperature, timeout=timeout,
               max_retries=max_retries, **extra, **_effort_kwargs(p, model_id, effort))


@dataclass
class Resolved:
    model: BaseChatModel
    adapter: str
    _api_key: str | None = field(default=None, repr=False)
    # 按剩余时间重建模型（请求超时、重试次数）：分析的统一截止时间要覆盖每一次模型请求
    factory: Callable[..., BaseChatModel] | None = field(default=None, repr=False)
    effort: str | None = None  # 指派的思考强度
    effective_effort: str | None = None  # 实际生效的档位（提供方没有核实过时为 None）

    def safe_error(self, error: Exception | str, *, limit: int = 300) -> str:
        return redact_error(str(error), self._api_key, limit=limit)


def resolve(session: Session, purpose: str, *, temperature: float = 0.0) -> Resolved | None:
    """指派无效、模型不再启用或问数检测失效时返回 None，调用方标明并使用演示模型。"""
    a = session.get(ModelAssignment, purpose)
    if a is None and purpose in ("explain", "analysis"):
        a = session.get(ModelAssignment, "ask")
    if a is None:
        return None
    p = session.get(ModelProvider, a.provider_id)
    if _enabled_model(p, a.model_id, needs_tools=a.purpose in ("ask", "analysis")) is None:
        return None
    model_id = a.model_id
    effort = a.reasoning_effort or DEFAULT_EFFORT.get(purpose, "medium")

    def factory(timeout: float, max_retries: int, *, effort: str = effort, max_tokens: int | None = None) -> BaseChatModel:
        """按剩余时间重建模型；阶段可以另定思考强度与输出上限（如理解阶段关闭思考）。"""
        return build_chat_model(p, model_id, temperature=temperature, timeout=timeout, max_retries=max_retries,
                                effort=effort, max_tokens=max_tokens)
    return Resolved(build_chat_model(p, model_id, temperature=temperature, effort=effort), f"{p.code}:{model_id}",
                    decrypt_secret(p.api_key_enc), factory, effort, effective_effort(p, model_id, effort))


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
        message = redact_error(f"无法连接 {url}：{type(exc).__name__}: {exc}", key)
        raise Conflict(message) from None
    if resp.status_code != 200:
        raise Conflict(f"获取模型列表失败：HTTP {resp.status_code}", body=redact_error(resp.text, key))
    try:
        ids = sorted({m.get("id") for m in resp.json().get("data", []) if m.get("id")})
    except (ValueError, TypeError, AttributeError) as exc:
        message = redact_error(f"获取模型列表失败：{type(exc).__name__}: {exc}", key)
        raise Conflict(message) from None
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
    key = None
    try:
        key = decrypt_secret(p.api_key_enc) if p.api_key_enc else None
        model = build_chat_model(p, model_id)
        tool = StructuredTool.from_function(lambda value: value, name="echo", args_schema=_PingArgs,
                                            description="原样返回 value")
        reply = model.bind_tools([tool]).invoke(
            [HumanMessage("这是连通性检测。请调用 echo 工具，参数 value 为 ok。")])
        result.update(ok=True, tool_calling=bool(reply.tool_calls))
    except Exception as exc:  # 上游可能回显请求头或地址，只保存脱敏后的错误
        result.update(ok=False, tool_calling=None, error=redact_error(f"{type(exc).__name__}: {exc}", key))
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    checks = dict((p.last_check or {}).get("models", {}))
    previous = p.last_check or {}
    if previous.get("model_id") and previous["model_id"] not in checks:
        checks[previous["model_id"]] = {k: v for k, v in previous.items() if k != "models"}
    checks[model_id] = dict(result)
    p.last_check = {**result, "models": checks}
    # 复制后整体赋值：原地修改 JSONB 中的字典不会被识别为变更
    p.models = [{**m, "tool_calling": result["tool_calling"]} if m["id"] == model_id else dict(m)
                for m in p.models]
    audit.record(session, actor, "model_provider.check", "model_provider", p.id, model_id=model_id,
                 ok=result["ok"], tool_calling=result.get("tool_calling"))
    return result
