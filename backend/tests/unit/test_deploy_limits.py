"""配置在预算、问数、数据源和导入入口的离线生效检查。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field

from avelis.analysis import agent as analysis_agent
from avelis.analysis import tools
from avelis.api import datasources, imports
from avelis.ask import agent as ask_agent
from avelis.config import build_settings


@pytest.fixture
def config_value():
    return build_settings({"AVELIS_SECRET": "offline-unit-test-secret-0123456789"})


def test_analysis_budget_defaults_read_current_settings_and_keep_explicit_values(monkeypatch, config_value):
    configured = replace(config_value, analysis_tool_calls=11, analysis_seconds=22, analysis_explore_limit=3,
                         analysis_model_requests=4, analysis_input_tokens=None, analysis_output_tokens=None)
    monkeypatch.setattr(tools, "get_settings", lambda: configured)
    budget = tools.Budget()
    assert (budget.tool_calls, budget.seconds, budget.explore, budget.model_requests,
            budget.input_tokens, budget.output_tokens) == (11, 22, 3, 4, None, None)
    explicit = tools.Budget(tool_calls=12, seconds=23, explore=5, model_requests=6, input_tokens=7, output_tokens=8)
    assert (explicit.tool_calls, explicit.seconds, explicit.explore, explicit.model_requests,
            explicit.input_tokens, explicit.output_tokens) == (12, 23, 5, 6, 7, 8)


@pytest.mark.parametrize(("remaining", "override", "expected"), [(100, 60, 9), (4, 60, 4), (100, 12, 12)])
def test_model_factory_timeout_uses_config_budget_and_experiment_override(
        monkeypatch, config_value, remaining, override, expected):
    monkeypatch.setattr(analysis_agent, "get_settings", lambda: replace(config_value, model_timeout_seconds=9))
    monkeypatch.setattr(analysis_agent, "MODEL_TIMEOUT_SECONDS", override)
    factory = Mock(return_value="offline-model")
    budget = SimpleNamespace(remaining=lambda: remaining, remaining_output_tokens=lambda: None)
    assert analysis_agent._model_for(SimpleNamespace(factory=factory), budget) == ("offline-model", False)
    factory.assert_called_once_with(timeout=expected, max_retries=0)


@pytest.mark.parametrize(("remaining", "retry"), [(6, False), (7, True)])
def test_model_retry_time_boundary_uses_configured_timeout(monkeypatch, config_value, remaining, retry):
    monkeypatch.setattr(analysis_agent, "get_settings", lambda: replace(config_value, model_timeout_seconds=3))
    monkeypatch.setattr(analysis_agent, "MODEL_TIMEOUT_SECONDS", 60)
    monkeypatch.setattr(analysis_agent, "_prepare_context", lambda *args, **kwargs: None)
    budget = tools.Budget(input_tokens=None, output_tokens=None)
    monkeypatch.setattr(budget, "remaining", lambda: remaining)
    box = SimpleNamespace(budget=budget, stopped=False, check_executor=Mock(), _step=Mock())
    response = AIMessage(content="离线响应")
    model = SimpleNamespace(invoke=Mock(side_effect=[TimeoutError("离线超时"), response]))
    if retry:
        assert analysis_agent._invoke(model, [], box) is response
    else:
        with pytest.raises(TimeoutError, match="离线超时"):
            analysis_agent._invoke(model, [], box)
    assert model.invoke.call_count == (2 if retry else 1)
    assert budget.used_model_requests == model.invoke.call_count
    assert box._step.call_args_list[0].args[4]["retry"] is retry


@pytest.mark.parametrize("rounds", [2, 4])
def test_ask_understand_stops_at_configured_tool_rounds(monkeypatch, config_value, rounds):
    monkeypatch.setattr(ask_agent, "get_settings", lambda: replace(config_value, ask_max_tool_rounds=rounds))
    model = Mock()
    model.bind_tools.return_value = model
    model.invoke.side_effect = lambda *_: AIMessage(
        content="", tool_calls=[{"name": "unknown", "args": {}, "id": f"call-{model.invoke.call_count}"}])
    monkeypatch.setattr(ask_agent.modelcfg, "resolve", lambda *args: SimpleNamespace(model=model, adapter="offline"))
    monkeypatch.setattr(ask_agent.modelcfg, "policy", lambda *_: {"send_results": False})
    monkeypatch.setattr(ask_agent.service, "visible_catalog", lambda *args, **kwargs: SimpleNamespace(cards=[]))
    box = SimpleNamespace(intent=None, clarification=None, tools=lambda: [])
    monkeypatch.setattr(ask_agent, "Toolbox", lambda *args: box)
    answer = SimpleNamespace(evidence={})
    result = ask_agent.understand({}, {"configurable": {"session": object(), "user": object(), "answer": answer}})
    assert model.invoke.call_count == rounds
    assert len(result["steps"][0]["tool_calls"]) == rounds
    assert len(result["messages"]) == 2 * rounds
    assert result["intent"] is None and result["clarification"]


@pytest.mark.parametrize(("explicit", "expected"), [(None, 37), (15, 15), (72, 72)])
def test_datasource_omitted_interval_uses_config_without_changing_explicit_values(
        monkeypatch, config_value, explicit, expected):
    monkeypatch.setattr(datasources, "get_settings", lambda: replace(config_value, collect_interval_minutes=37))
    engine = SimpleNamespace(options=[], is_test_adapter=False)
    monkeypatch.setattr(datasources, "get_engine", lambda *_: engine)
    monkeypatch.setattr(datasources.access, "require_role", Mock())
    monkeypatch.setattr(datasources.audit, "record", Mock())
    monkeypatch.setattr(datasources, "datasource_view", lambda session, ds: {"interval": ds.collect_interval_minutes})
    payload = {"name": "离线源", "kind": "postgresql", "host": "offline", "port": 5432}
    if explicit is not None:
        payload["collect_interval_minutes"] = explicit
    body = datasources.DatasourceCreate(**payload)
    session = Mock()
    session.scalar.return_value = None
    result = datasources.create_datasource(body, user=SimpleNamespace(id="offline-user"), session=session)
    assert result == {"interval": expected}
    field = datasources.DatasourceCreate.model_json_schema()["properties"]["collect_interval_minutes"]
    assert (field["default"], field["minimum"], field["maximum"]) == (15, 1, 1440)


def test_import_json_dynamic_character_limit_keeps_422_shape_and_removes_schema_maximum(monkeypatch, config_value):
    limits = SimpleNamespace(import_max_bytes=3)
    monkeypatch.setattr(imports, "get_settings", lambda: limits)
    monkeypatch.setattr(imports, "_created",
                        lambda session, user, **kwargs: JSONResponse({"content": kwargs["content"]}))
    app = FastAPI()
    app.include_router(imports.router)
    app.dependency_overrides[imports.current_user] = lambda: object()
    app.dependency_overrides[imports.get_session] = lambda: object()

    class LegacyImportBody(BaseModel):
        content: str = Field(min_length=1, max_length=limits.import_max_bytes)

    def legacy_create(body):
        return {"content": body.content}

    # 局部模型直接写入注解，避免延迟注解只能从模块全局解析的问题。
    legacy_create.__annotations__["body"] = LegacyImportBody
    app.post("/legacy-import")(legacy_create)
    with TestClient(app) as client:
        assert client.post("/api/imports", json={"datasource_id": "offline", "content": "三汉字"}).status_code == 200
        response = client.post("/api/imports", json={"datasource_id": "offline", "content": "四个汉字"})
        assert response.status_code == 422
        assert response.json() == {"detail": [{
            "type": "string_too_long", "loc": ["body", "content"],
            "msg": "String should have at most 3 characters", "input": "四个汉字", "ctx": {"max_length": 3},
        }]}
        legacy = client.post("/legacy-import", json={"content": "四个汉字"})
        assert (response.status_code, response.json()) == (legacy.status_code, legacy.json())
        limits.import_max_bytes = 4
        assert client.post("/api/imports", json={"datasource_id": "offline", "content": "四个汉字"}).status_code == 200
        content = client.get("/openapi.json").json()["components"]["schemas"]["ImportBody"]["properties"]["content"]
        assert content["minLength"] == 1 and "maxLength" not in content


def invoke_upload(raw):
    file = UploadFile(BytesIO(raw), filename="offline.sql")
    return asyncio.run(imports.upload(datasource_id="offline", file=file, default_db=None,
                                     user=object(), session=object()))


def test_import_upload_dynamic_byte_limit_and_utf8_error(monkeypatch, config_value):
    limits = SimpleNamespace(import_max_bytes=3)
    monkeypatch.setattr(imports, "get_settings", lambda: limits)
    monkeypatch.setattr(imports, "_created", lambda session, user, **kwargs: kwargs)
    assert invoke_upload("字".encode())["content"] == "字"
    with pytest.raises(HTTPException) as oversized:
        invoke_upload("字x".encode())
    assert oversized.value.status_code == 413
    limits.import_max_bytes = 4
    assert invoke_upload("字x".encode())["content"] == "字x"
    with pytest.raises(HTTPException) as invalid:
        invoke_upload(b"\xff")
    assert (invalid.value.status_code, invalid.value.detail) == (422, "文件不是 UTF-8 文本")
    limits.import_max_bytes = config_value.import_max_bytes
    with pytest.raises(HTTPException) as default_limit:
        invoke_upload(b"x" * (config_value.import_max_bytes + 1))
    assert (default_limit.value.status_code, default_limit.value.detail) == (413, "文件超过 2 MB")
