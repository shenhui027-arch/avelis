"""请求语言、异常出口与消息目录契约；所有依赖均使用离线替身。"""

from __future__ import annotations

import ast
import asyncio
import re
import tomllib
from pathlib import Path
from string import Formatter

import httpx
import pytest
from babel.messages.pofile import read_po
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from avelis.api import app as api_app
from avelis.cache import LockBusy
from avelis.config import build_settings
from avelis.engines import CapabilityMissing, CredentialMissing, UnsupportedEngine, get_engine
from avelis.governance.errors import Conflict, Invalid, NotFound
from avelis.i18n import Msg, current_locale, gettext, locale_context, negotiate_locale, translate

SOURCE = Path(__file__).resolve().parents[2] / "src" / "avelis"


@pytest.mark.parametrize("header,expected", [
    ("zh-TW", "zh-TW"), ("zh-HK", "zh-TW"), ("zh-Hant", "zh-TW"), ("zh-Hant-HK", "zh-TW"),
    ("zh-Hant-CN", "zh-TW"), ("zh", "zh-CN"), ("zh-SG", "zh-CN"), ("zh-Hans", "zh-CN"),
    ("en-US", "en"), ("fr", "zh-CN"), ("", "zh-CN"), (None, "zh-CN"),
    ("fr;q=1,en-US;q=0.8,zh;q=0.4", "en"), ("en;q=0.2,zh-HK;q=0.9", "zh-TW"),
    ("en;q=0,zh-TW;q=0.1", "zh-TW"), ("en;q=oops", "zh-CN"), ("*", "zh-CN"),
])
def test_language_negotiation(header, expected):
    assert negotiate_locale(header) == expected


@pytest.fixture
def application(monkeypatch):
    settings = build_settings({"AVELIS_SECRET": "offline-unit-test-secret-0123456789"})
    monkeypatch.setattr(api_app, "get_settings", lambda: settings)
    app = api_app.create_app()

    @app.get("/i18n/governance/{kind}")
    def governance(kind: str):
        if kind == "constant":
            raise NotFound("数据源不存在")
        raise Invalid(Msg("时间窗口无效：{error}", error="模型名稱 / SQL 原文"))

    @app.get("/i18n/http/{kind}")
    def http_error(kind: str):
        if kind == "object":
            raise HTTPException(422, {"message": "用户原文"}, headers={"X-Error": "original"})
        detail = "数据源不存在" if kind == "constant" else Msg("时间窗口无效：{error}", error="模型名稱 / SQL 原文")
        raise HTTPException(409, detail, headers={"Retry-After": "3", "WWW-Authenticate": "Bearer"})

    @app.get("/i18n/special/{kind}")
    def special_error(kind: str):
        if kind == "credential":
            raise CredentialMissing(Msg("时间窗口无效：{error}", error="原始参数"))
        if kind == "engine":
            raise UnsupportedEngine("数据源不存在")
        if kind == "capability":
            raise CapabilityMissing(get_engine("fake"), "query", "資料名稱")
        raise LockBusy("resource-key")

    @app.get("/i18n/starlette")
    def starlette_error():
        raise StarletteHTTPException(403, "数据源不存在", headers={"X-Error": "original"})

    @app.get("/i18n/validation")
    def validation(count: int):
        return count

    return app


@pytest.mark.parametrize("locale,constant,template", [
    ("zh-CN", "数据源不存在", "时间窗口无效：模型名稱 / SQL 原文"),
    ("zh-TW", "資料來源不存在", "時間範圍無效：模型名稱 / SQL 原文"),
    ("en", "Data source not found", "Invalid time window: 模型名稱 / SQL 原文"),
])
def test_request_error_translation(application, locale, constant, template):
    with TestClient(application) as client:
        headers = {"Accept-Language": locale}
        assert client.get("/i18n/governance/constant", headers=headers).json()["detail"] == constant
        response = client.get("/i18n/governance/template", headers=headers)
        assert response.status_code == 422 and response.json()["detail"] == template
        for kind, expected in (("constant", constant), ("template", template)):
            response = client.get(f"/i18n/http/{kind}", headers=headers)
            assert response.json()["detail"] == expected
            assert response.status_code == 409
            assert response.headers["retry-after"] == "3" and response.headers["www-authenticate"] == "Bearer"
        assert client.get("/i18n/starlette", headers=headers).json()["detail"] == constant
        assert client.get("/i18n/http/object", headers=headers).json()["detail"] == {"message": "用户原文"}
        assert client.get("/i18n/http/constant").json()["detail"] == "数据源不存在"
    assert current_locale() == "zh-CN"


def test_special_errors_and_validation(application):
    with TestClient(application) as client:
        for locale in ("zh-CN", "zh-TW", "en"):
            with locale_context(locale):
                expected_lock = gettext("资源正被其他任务占用")
                expected_capability = translate(CapabilityMissing(get_engine("fake"), "query", "資料名稱").args[0])
                expected_credential = translate(Msg("时间窗口无效：{error}", error="原始参数"))
                expected_engine = gettext("数据源不存在")
            headers = {"Accept-Language": locale}
            assert client.get("/i18n/special/credential", headers=headers).json()["detail"] == expected_credential
            assert client.get("/i18n/special/engine", headers=headers).json()["detail"] == expected_engine
            capability = client.get("/i18n/special/capability", headers=headers).json()
            assert capability == {"detail": expected_capability, "capability": "query"}
            assert client.get("/i18n/special/lock", headers=headers).json() == {
                "detail": expected_lock, "lock": "resource-key"}
            validation = client.get("/i18n/validation?count=wrong", headers=headers)
            assert validation.status_code == 422
            assert validation.json()["detail"][0]["type"] == "int_parsing"


def test_msg_preserves_original_string_and_untranslated_parameters():
    error = ValueError("SQL 原始錯誤")
    message = Msg("时间窗口无效：{error}", error=error)
    assert isinstance(message, str) and str(message) == "时间窗口无效：SQL 原始錯誤"
    assert message.template == "时间窗口无效：{error}" and message.params["error"] is error
    assert str(Invalid(message)) == str(message)
    with locale_context("en"):
        assert translate(message) == "Invalid time window: SQL 原始錯誤"
        assert translate("用户 / 模型撰写的内容") == "用户 / 模型撰写的内容"
        assert gettext("") == ""
        assert gettext("缺少译文：{value}", value="原文") == "缺少译文：原文"
        assert translate({"detail": "数据源不存在"}) == {"detail": "数据源不存在"}
    assert current_locale() == "zh-CN"


def test_only_marked_fixed_parameters_are_translated():
    message = Msg("{engine}不支持{capability}", engine="问数执行", capability=Msg("问数执行"))
    assert str(message) == "问数执行不支持问数执行"
    with locale_context("en"):
        rendered = translate(message)
    assert rendered.startswith("问数执行")
    assert rendered.count("问数执行") == 1


@pytest.mark.parametrize("invalid_json", [False, True])
def test_redacted_model_errors_keep_original_truncation(monkeypatch, invalid_json):
    from types import SimpleNamespace

    from avelis import modelcfg

    key = "offline-test-api-key"
    problem = (key + " upstream failure ") * 40
    provider = SimpleNamespace(api_key_enc="offline-placeholder", base_url="https://offline.invalid", kind="openai")
    monkeypatch.setattr(modelcfg, "_get", lambda *_: provider)
    monkeypatch.setattr(modelcfg, "decrypt_secret", lambda *_: key)
    error = ValueError(problem) if invalid_json else httpx.ConnectError(problem)

    def fail(*_, **__):
        raise error

    monkeypatch.setattr(modelcfg.httpx, "get", (lambda *_, **__: SimpleNamespace(status_code=200, json=fail))
                        if invalid_json else fail)
    prefix = "获取模型列表失败：" if invalid_json else "无法连接 https://offline.invalid/models："
    expected = modelcfg.redact_error(f"{prefix}{type(error).__name__}: {error}", key)
    with pytest.raises(Conflict) as caught:
        modelcfg.fetch_models(None, None, "offline-provider")
    assert str(caught.value) == expected
    assert key not in str(caught.value)
    with locale_context("en"):
        rendered = translate(caught.value.message)
    assert key not in rendered
    assert not rendered.startswith(("无法连接", "获取模型列表失败"))


def test_simultaneous_requests_keep_locale_isolated(application):
    arrived = 0
    both = asyncio.Event()

    @application.get("/i18n/concurrent")
    async def concurrent():
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both.set()
        await both.wait()
        return {"locale": current_locale(), "message": gettext("数据源不存在")}

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
            responses = await asyncio.gather(*[
                client.get("/i18n/concurrent", headers={"Accept-Language": locale}) for locale in ("en", "zh-TW")])
        return [response.json() for response in responses]

    assert asyncio.run(request()) == [
        {"locale": "en", "message": "Data source not found"},
        {"locale": "zh-TW", "message": "資料來源不存在"},
    ]
    assert current_locale() == "zh-CN"


def _literal(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _source_messages():
    """使用 AST 收集多行常量和位置参数，避免目录检查依赖代码排版。"""
    messages = set()
    dictionaries = {"TYPE_LABEL", "DATA_SOURCE_LABEL", "REPORT_STATUS", "MEASURE_LABEL", "CALC_COLUMN_LABELS",
                    "CALC_PERIOD_LABELS", "FIELD_LABEL", "CONCLUSION", "CAPABILITY_LABEL", "SOURCE_COLUMNS",
                    "PURPOSES", "EFFORT_LABELS", "BRIEF_SOURCE_NOTE", "BRIEF_EXPLORE_NOTE", "EXPLORE_MARK",
                    "RESTRICTED_NOTE"}
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            candidates = []
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else ""
                if name in {"NotFound", "Forbidden", "Conflict", "Invalid", "GovernanceError", "Msg", "gettext",
                            "_404", "_unauthorized", "_answer_text"} and node.args:
                    candidates.append(node.args[0])
                if name == "HTTPException":
                    candidates.extend(k.value for k in node.keywords if k.arg == "detail")
                    if len(node.args) > 1:
                        candidates.append(node.args[1])
                # 引擎说明、配置项名与提示在 Engine.describe() 里按请求语言取译文
                if name == "OptionField" and len(node.args) > 1:
                    candidates.append(node.args[1])
                if name in {"OptionField", "Engine"}:
                    candidates.extend(k.value for k in node.keywords if k.arg in {"label", "hint", "note"})
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in dictionaries
                                                    for t in node.targets):
                candidates.extend(n for n in ast.walk(node.value) if isinstance(n, ast.Constant))
            for candidate in candidates:
                # 条件表达式中的两个固定分支也都必须有译文。
                values = [candidate.body, candidate.orelse] if isinstance(candidate, ast.IfExp) else [candidate]
                for literal in values:
                    value = _literal(literal)
                    if value and re.search(r"[\u4e00-\u9fff]", value):
                        messages.add(value)
    return messages


def _placeholders(text):
    return {name for _, name, _, _ in Formatter().parse(text) if name is not None}


@pytest.mark.parametrize("locale", ["zh_TW", "en"])
def test_message_catalog_is_complete(locale):
    path = SOURCE / "locales" / locale / "LC_MESSAGES" / "messages.po"
    with path.open("rb") as stream:
        catalog = read_po(stream)
    expected = _source_messages()
    missing = [msgid for msgid in sorted(expected) if msgid not in catalog or not catalog[msgid].string]
    assert not missing, f"{locale} 缺少消息：\n" + "\n".join(missing)
    for message in catalog:
        if not message.id:
            continue
        assert isinstance(message.id, str) and isinstance(message.string, str)
        assert message.string and "fuzzy" not in message.flags, message.id
        assert _placeholders(message.id) == _placeholders(message.string), message.id


def test_catalogs_are_packaged_explicitly():
    project = tomllib.loads((SOURCE.parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    includes = project["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert includes["src/avelis/locales"] == "avelis/locales"


def test_generated_grain_notes_and_event_labels_translate_at_api_exit():
    """粒度说明与工作台事件类型是请求时生成的：留底仍是简体原文，接口出口按请求语言翻译，参数原样。"""
    from avelis.i18n import Msg, gettext, locale_context, translate

    note = Msg("透传自 {table}", table="db_a.table_a")
    assert note == "透传自 db_a.table_a"
    with locale_context("en"):
        assert translate(note) == "Passed through from db_a.table_a"
        assert translate(Msg("经身份映射继承他库表的键")).startswith("Key inherited")
        assert gettext("新增列") == "Column added"
    with locale_context("zh-TW"):
        assert translate(note) == "透傳自 db_a.table_a"


@pytest.mark.parametrize("locale,label,invalid,missing", [
    ("zh-CN", "认证方式", "Hive 数据源配置取值不合法：认证方式（可选 NONE、NOSASL、LDAP）", "Hive 数据源缺少配置：采集的库"),
    ("zh-TW", "認證方式", "Hive 資料來源設定值不合法：認證方式（可選 NONE、NOSASL、LDAP）", None),
    ("en", "Authentication",
     "The Hive data source has invalid settings: Authentication (choose from NONE, NOSASL, LDAP)",
     "The Hive data source is missing settings: Databases to collect"),
])
def test_engine_options_and_option_errors_follow_request_language(monkeypatch, locale, label, invalid, missing):
    from avelis.api import datasources as api

    class NoDuplicate:
        def scalar(self, *_):
            return None

    monkeypatch.setattr(api.access, "require_role", lambda *args, **kwargs: None)
    with locale_context(locale):
        hive = get_engine("hive").describe()
        auth = next(o for o in hive["options"] if o["key"] == "auth")
        assert auth["label"] == label and auth["choices"] == ["NONE", "NOSASL", "LDAP"]
        assert auth["hint"] == gettext("NONE 不校验口令，账号可不填；LDAP 需账号与口令；Kerberos 未支持")
        assert hive["note"] == gettext(get_engine("hive").note) and (locale == "zh-CN") == ("只读" in hive["note"])
        errors = []
        for options in ({"databases": ["db"], "auth": "KERBEROS"}, {"auth": "NONE"}):
            body = api.DatasourceCreate(name="hive", kind="hive", host="hive", port=10000, options=options)
            with pytest.raises(HTTPException) as caught:
                api.create_datasource(body, user=None, session=NoDuplicate())
            errors.append(translate(caught.value.detail))
    assert errors[0] == invalid
    assert missing is None or errors[1] == missing
