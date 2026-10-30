"""模型配置的凭据脱敏与服务器检测约束；全部使用内存对象，不连接数据库或上游。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from avelis import modelcfg
from avelis.governance.errors import Conflict
from avelis.models import AppSetting, ModelAssignment, ModelProvider, User

KEY = "sk-unit-secret-0123456789"
MODEL_ID = "unit-model"


class MemorySession:
    def __init__(self, provider, assignment=None):
        self.rows = {(ModelProvider, provider.id): provider}
        if assignment is not None:
            self.rows[ModelAssignment, assignment.purpose] = assignment

    def get(self, kind, key):
        return self.rows.get((kind, key))

    def scalar(self, statement):
        return None

    def scalars(self, statement):
        return [row for (kind, _), row in self.rows.items() if kind is ModelAssignment]

    def add(self, row):
        key = row.purpose if isinstance(row, ModelAssignment) else row.key
        self.rows[type(row), key] = row

    def flush(self):
        pass


@pytest.fixture
def provider():
    return ModelProvider(id="provider-id", code="unit", name="测试模型", kind="openai_compatible",
                         base_url="https://unit.invalid/v1", api_key_enc="encrypted", enabled=True,
                         models=[{"id": MODEL_ID, "enabled": True, "tool_calling": True}], last_check={})


@pytest.fixture(autouse=True)
def isolate_credentials(monkeypatch):
    monkeypatch.setattr(modelcfg, "decrypt_secret", lambda token: KEY)
    monkeypatch.setattr(modelcfg, "encrypt_secret", lambda text: "new-encrypted")
    monkeypatch.setattr(modelcfg.audit, "record", lambda *args, **kwargs: None)


def assignment():
    return ModelAssignment(purpose="ask", provider_id="provider-id", model_id=MODEL_ID, updated_by="actor",
                           updated_at=modelcfg.utcnow())


def successful_check(model_id=MODEL_ID):
    return {"model_id": model_id, "ok": True, "tool_calling": True}


@pytest.mark.parametrize("text,secrets", [
    (f"request failed with {KEY}", [KEY]),
    ('headers={"Authorization": "Bearer header-secret"}', ["header-secret"]),
    ("headers={'Authorization': 'Basic basic-secret'}", ["basic-secret"]),
    ("Authorization =\n Bearer multiline-secret\nHTTP 401", ["multiline-secret"]),
    ("Authorization:\n Bearer\n multiline-bearer-secret", ["multiline-bearer-secret"]),
    ('"Authorization": "Bearer\n quoted-header-secret"', ["quoted-header-secret"]),
    ("Authorization:\n Basic\n multiline-basic-secret", ["multiline-basic-secret"]),
    ("proxy-authorization: Basic proxy-secret", ["proxy-secret"]),
    ("Bearer standalone-secret", ["standalone-secret"]),
    ("x-api-key: anthropic-secret", ["anthropic-secret"]),
    ("https://url-user:url-pass@unit.invalid/path", ["url-user", "url-pass"]),
    ("postgresql://pg-user:pg-pass@unit.invalid/db", ["pg-user", "pg-pass"]),
    ("socks5://proxy-user:proxy-pass@unit.invalid", ["proxy-user", "proxy-pass"]),
])
def test_upstream_error_redacts_credentials(text, secrets):
    safe = modelcfg.redact_error(text, KEY)
    assert "[REDACTED]" in safe
    assert all(secret not in safe for secret in secrets)


def test_upstream_error_redacts_before_truncating():
    secret = "very-long-secret-" * 40
    safe = modelcfg.redact_error(f"prefix {secret} suffix " + "x" * 400, secret)
    assert "very-long-secret" not in safe
    assert "[REDACTED] suffix" in safe
    assert len(safe) == 300


def test_check_error_is_redacted_in_result_and_saved_record(monkeypatch, provider):
    provider.last_check = successful_check()

    def fail(*args, **kwargs):
        raise RuntimeError(f"{KEY}; Authorization: Bearer auth-secret\nhttps://url-user:url-pass@unit.invalid")

    monkeypatch.setattr(modelcfg, "build_chat_model", fail)
    result = modelcfg.check(MemorySession(provider), User(id="actor"), provider.id, MODEL_ID)
    saved = json.dumps({"returned": result, "saved": provider.last_check})
    assert all(secret not in saved for secret in (KEY, "auth-secret", "url-user", "url-pass"))
    assert result["ok"] is False and len(result["error"]) <= 300
    assert provider.models[0]["tool_calling"] is None
    assert modelcfg._model_check(provider, MODEL_ID)["ok"] is False


def test_fetch_models_redacts_response_body(monkeypatch, provider):
    body = f'{KEY}\n{{"Authorization": "Bearer body-secret"}} https://url-user:url-pass@unit.invalid'
    monkeypatch.setattr(modelcfg.httpx, "get", lambda *args, **kwargs: httpx.Response(401, text=body))
    with pytest.raises(Conflict) as error:
        modelcfg.fetch_models(MemorySession(provider), User(id="actor"), provider.id)
    safe = str(error.value) + json.dumps(error.value.detail)
    assert all(secret not in safe for secret in (KEY, "body-secret", "url-user", "url-pass"))


def test_fetch_models_redacts_connection_error_and_endpoint(monkeypatch, provider):
    provider.base_url = "https://url-user:url-pass@unit.invalid"

    def fail(*args, **kwargs):
        raise httpx.ConnectError(f"{KEY}\nAuthorization: Bearer connection-secret")

    monkeypatch.setattr(modelcfg.httpx, "get", fail)
    with pytest.raises(Conflict) as error:
        modelcfg.fetch_models(MemorySession(provider), User(id="actor"), provider.id)
    assert all(secret not in str(error.value) for secret in (KEY, "connection-secret", "url-user", "url-pass"))


def test_fetch_models_redacts_invalid_json_error(monkeypatch, provider):
    def invalid_json():
        raise ValueError(f"{KEY} Bearer json-secret https://url-user:url-pass@unit.invalid")

    response = SimpleNamespace(status_code=200, json=invalid_json)
    monkeypatch.setattr(modelcfg.httpx, "get", lambda *args, **kwargs: response)
    with pytest.raises(Conflict) as error:
        modelcfg.fetch_models(MemorySession(provider), User(id="actor"), provider.id)
    assert all(secret not in str(error.value) for secret in (KEY, "json-secret", "url-user", "url-pass"))


@pytest.mark.parametrize("last_check", [
    {},
    {"model_id": MODEL_ID, "ok": False, "tool_calling": True},
    {"model_id": MODEL_ID, "ok": True, "tool_calling": None},
    {"model_id": MODEL_ID, "ok": True, "tool_calling": False},
    {"model_id": MODEL_ID, "ok": True, "tool_calling": "true"},
    {"model_id": MODEL_ID, "ok": True, "tool_calling": 1},
    {"model_id": "other-model", "ok": True, "tool_calling": True},
])
def test_ask_assignment_requires_successful_server_check(provider, last_check):
    provider.last_check = last_check
    with pytest.raises(Conflict, match="最近一次服务器检测成功"):
        modelcfg.save_settings(MemorySession(provider), User(id="actor"),
                               {"ask": {"provider_id": provider.id, "model_id": MODEL_ID}}, None)


def test_ask_assignment_accepts_successful_server_check(provider):
    provider.last_check = successful_check()
    session = MemorySession(provider)
    result = modelcfg.save_settings(session, User(id="actor"),
                                    {"ask": {"provider_id": provider.id, "model_id": MODEL_ID}}, None)
    assert session.get(ModelAssignment, "ask").model_id == MODEL_ID
    assert result["mode"] == "configured" and result["note"] is None


def test_check_keeps_each_models_latest_server_result(monkeypatch, provider):
    provider.last_check = successful_check()
    provider.models.append({"id": "other-model", "enabled": True, "tool_calling": None})
    reply = SimpleNamespace(tool_calls=[{"name": "echo"}])
    model = SimpleNamespace(bind_tools=lambda tools: SimpleNamespace(invoke=lambda messages: reply))
    monkeypatch.setattr(modelcfg, "build_chat_model", lambda *args, **kwargs: model)
    modelcfg.check(MemorySession(provider), User(id="actor"), provider.id, "other-model")
    assert modelcfg._enabled_model(provider, MODEL_ID, needs_tools=True) is not None
    assert modelcfg._enabled_model(provider, "other-model", needs_tools=True) is not None
    reply.tool_calls = []
    modelcfg.check(MemorySession(provider), User(id="actor"), provider.id, MODEL_ID)
    assert modelcfg._enabled_model(provider, MODEL_ID, needs_tools=True) is None
    assert modelcfg._enabled_model(provider, "other-model", needs_tools=True) is not None


@pytest.mark.parametrize("change", [
    {"base_url": "https://changed.invalid/v1"},
    {"api_key": "new-key"},
    {"clear_api_key": True},
])
def test_connection_changes_invalidate_server_checks(provider, change):
    provider.last_check = successful_check()
    modelcfg.update_provider(MemorySession(provider), User(id="actor"), provider.id, change)
    assert provider.last_check == {}
    assert provider.models[0]["tool_calling"] is None


def test_unchanged_connection_keeps_server_check(provider):
    provider.last_check = successful_check()
    modelcfg.update_provider(MemorySession(provider), User(id="actor"), provider.id,
                             {"name": "改名", "base_url": provider.base_url})
    assert provider.last_check == successful_check()


@pytest.mark.parametrize("invalid", ["removed", "disabled", "provider_disabled", "missing_key", "stale_check"])
def test_resolve_falls_back_when_assignment_is_no_longer_valid(monkeypatch, provider, invalid):
    provider.last_check = successful_check()
    if invalid == "removed":
        provider.models = []
    elif invalid == "disabled":
        provider.models[0]["enabled"] = False
    elif invalid == "provider_disabled":
        provider.enabled = False
    elif invalid == "missing_key":
        provider.api_key_enc = None
    else:
        provider.last_check = {}
    session = MemorySession(provider, assignment())
    monkeypatch.setattr(modelcfg, "build_chat_model", lambda *args, **kwargs: pytest.fail("无效指派不得调用模型"))
    assert modelcfg.resolve(session, "ask") is None
    assert modelcfg.resolve(session, "explain") is None
    view = modelcfg.settings_view(session)
    assert view["mode"] == "demo" and "演示模型" in view["note"]


def test_resolve_uses_enabled_checked_model(monkeypatch, provider):
    provider.last_check = successful_check()
    model = object()
    monkeypatch.setattr(modelcfg, "build_chat_model", lambda *args, **kwargs: model)
    result = modelcfg.resolve(MemorySession(provider, assignment()), "ask")
    assert result.model is model and result.adapter == f"unit:{MODEL_ID}"
    assert KEY not in repr(result)
    assert KEY not in result.safe_error(f"provider returned {KEY}")


def test_explain_assignment_does_not_require_tool_check(monkeypatch, provider):
    explain = assignment()
    explain.purpose = "explain"
    model = object()
    monkeypatch.setattr(modelcfg, "build_chat_model", lambda *args, **kwargs: model)
    session = MemorySession(provider, explain)
    assert session.get(AppSetting, modelcfg.POLICY_KEY) is None
    assert modelcfg.resolve(session, "explain").model is model


def deepseek():
    return ModelProvider(id="ds-id", code="ds", name="DeepSeek", kind="openai_compatible", preset="deepseek",
                         base_url="https://api.deepseek.com", api_key_enc="encrypted", enabled=True,
                         models=[{"id": "deepseek-flash", "enabled": True, "tool_calling": True}],
                         last_check={"models": {"deepseek-flash": successful_check("deepseek-flash")}})


def test_reasoning_effort_request_parameters_are_adapted_per_provider(provider):
    """ADR 0012：思考强度的请求参数集中在 build_chat_model。DeepSeek 关闭思考用 thinking.type=disabled，
    其余档位传 reasoning_effort，medium 按官方文档换成 high；没有核实过的提供方不传思考参数。"""
    ds = deepseek()
    off = modelcfg.build_chat_model(ds, "deepseek-flash", effort="off", max_tokens=1000)
    assert off.extra_body == {"thinking": {"type": "disabled"}} and off.reasoning_effort is None and off.max_tokens == 1000
    assert modelcfg.build_chat_model(ds, "deepseek-flash", effort="medium").reasoning_effort == "high"
    assert modelcfg.build_chat_model(ds, "deepseek-flash", effort="low").reasoning_effort == "low"
    assert modelcfg.build_chat_model(ds, "deepseek-flash", effort="max").reasoning_effort == "max"
    other = modelcfg.build_chat_model(provider, MODEL_ID, effort="high")
    assert other.reasoning_effort is None and other.extra_body is None and other.max_tokens is None
    assert [o["value"] for o in modelcfg.effort_options(ds, "deepseek-flash")] == list(modelcfg.EFFORTS)
    medium = next(o for o in modelcfg.effort_options(ds, "deepseek-flash") if o["value"] == "medium")
    assert medium["effective"] == "high" and medium["effective_label"] == "高"  # 页面注明实际生效的档位
    assert modelcfg.effort_options(provider, MODEL_ID) == []


def test_assignment_effort_is_saved_validated_and_resolved(monkeypatch):
    """思考强度存在用途指派上；不支持的档位拒绝；没设置时按默认档位；resolve 记下实际生效的档位，阶段可另定。"""
    ds = deepseek()
    session = MemorySession(ds)
    actor = User(id="actor", username="admin")
    modelcfg.save_settings(session, actor, {"analysis": {"provider_id": "ds-id", "model_id": "deepseek-flash",
                                                         "reasoning_effort": "low"}}, None)
    view = modelcfg.settings_view(session)["assignments"]["analysis"]
    assert view["reasoning_effort"] == "low" and view["effective_effort"] == "low"
    resolved = modelcfg.resolve(session, "analysis")
    assert resolved.effort == "low" and resolved.effective_effort == "low" and resolved.model.reasoning_effort == "low"
    understand = resolved.factory(timeout=30, max_retries=0, effort="off", max_tokens=1000)
    assert understand.extra_body == {"thinking": {"type": "disabled"}} and understand.max_tokens == 1000
    modelcfg.save_settings(session, actor, {"analysis": {"provider_id": "ds-id", "model_id": "deepseek-flash"}}, None)
    view = modelcfg.settings_view(session)["assignments"]["analysis"]
    assert view["reasoning_effort"] == modelcfg.DEFAULT_EFFORT["analysis"] == "medium" and view["effective_effort"] == "high"
    plain = ModelProvider(id="provider-id", code="unit", name="测试模型", kind="openai_compatible",
                          base_url="https://unit.invalid/v1", api_key_enc="encrypted", enabled=True,
                          models=[{"id": MODEL_ID, "enabled": True, "tool_calling": True}],
                          last_check={"models": {MODEL_ID: successful_check()}})
    session.rows[ModelProvider, "provider-id"] = plain
    with pytest.raises(modelcfg.Invalid):
        modelcfg.save_settings(session, actor, {"ask": {"provider_id": "provider-id", "model_id": MODEL_ID,
                                                        "reasoning_effort": "high"}}, None)
