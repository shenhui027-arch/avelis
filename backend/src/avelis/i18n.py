"""请求语言与固定文案：简体原文作为消息键，业务内容和参数始终保留原样。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from importlib.resources import files
from typing import Any

from babel import Locale, parse_locale
from babel.messages.pofile import read_po

DEFAULT_LOCALE = "zh-CN"
SUPPORTED_LOCALES = ("zh-CN", "zh-TW", "en")
_request_locale: ContextVar[str] = ContextVar("avelis_request_locale", default=DEFAULT_LOCALE)


def negotiate_locale(accept_language: str | None) -> str:
    """按权重选择受支持语言；中文地区与字形统一映射到两份中文界面。"""
    preferred = []
    for index, item in enumerate((accept_language or "").split(",")):
        tag, *options = item.strip().split(";")
        quality = 1.0
        try:
            for option in options:
                key, value = option.strip().split("=", 1)
                if key.lower() == "q":
                    quality = float(value)
            if not 0 < quality <= 1:
                continue
            language, territory, script, *_ = parse_locale(tag.replace("_", "-"), sep="-")
        except (ValueError, TypeError):
            continue
        language = language.lower()
        if language == "zh":
            candidate = "zh_TW" if territory in {"TW", "HK"} or script == "Hant" else "zh_CN"
        elif language == "en":
            candidate = "en"
        else:
            continue
        preferred.append((-quality, index, candidate))
    match = Locale.negotiate([item[2] for item in sorted(preferred)], ("zh_CN", "zh_TW", "en"), aliases={})
    if match is None:
        return DEFAULT_LOCALE
    if match.language == "en":
        return "en"
    return "zh-TW" if match.territory == "TW" else DEFAULT_LOCALE


def current_locale() -> str:
    return _request_locale.get()


@contextmanager
def locale_context(locale: str):
    """后台或离线导出可显式恢复已保存的语言，请求结束后不会污染下一次调用。"""
    token = _request_locale.set(locale if locale in SUPPORTED_LOCALES else DEFAULT_LOCALE)
    try:
        yield
    finally:
        _request_locale.reset(token)


class LocaleMiddleware:
    """纯 ASGI 中间件，让同步端点、异步端点和异常处理器共享同一请求语言。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = ",".join(
            value.decode("latin-1") for key, value in scope.get("headers", ()) if key.lower() == b"accept-language"
        )
        with locale_context(negotiate_locale(header)):
            await self.app(scope, receive, send)


class Msg(str):
    """仍是格式化好的简体字符串，同时保留出口翻译所需的模板和原始参数。"""

    def __new__(cls, template: str, **params: Any):
        instance = super().__new__(cls, template.format(**params))
        instance.template = template
        instance.params = params
        return instance


@lru_cache(maxsize=2)
def _messages(locale: str) -> dict[str, str]:
    resource = files("avelis").joinpath("locales", locale.replace("-", "_"), "LC_MESSAGES", "messages.po")
    try:
        with resource.open("rb") as stream:
            catalog = read_po(stream)
    except FileNotFoundError:
        return {}
    return {
        message.id: message.string
        for message in catalog
        if isinstance(message.id, str)
        and message.id
        and isinstance(message.string, str)
        and message.string
        and "fuzzy" not in message.flags
    }


def gettext(msgid: str, **params: Any) -> str:
    locale = current_locale()
    text = _messages(locale).get(msgid, msgid) if locale != DEFAULT_LOCALE else msgid
    return text.format(**params) if params else text


def translate(message: Any) -> Any:
    if isinstance(message, Msg):
        # 只有显式标记的固定子标签参与翻译，普通字符串参数仍是用户或模型的原文。
        params = {name: translate(value) if isinstance(value, Msg) else value for name, value in message.params.items()}
        return gettext(message.template, **params)
    if isinstance(message, str):
        return gettext(message)
    return message


def reply_language_instruction(locale: str) -> str:
    """只追加到每次运行的用户上下文，系统提示保持稳定以复用前缀缓存。"""
    if locale == "en":
        return (
            "Write user-facing text in English. Keep metric names, dimension members, field names, data values, "
            "and quoted metadata in their original language. Do not translate them. "
            "Verification and citation rules remain unchanged."
        )
    if locale == "zh-TW":
        return (
            "請用繁體中文（台灣）撰寫給使用者看的文字；指標名稱、維度成員、欄位名稱、資料值及引用的中繼資料"
            "保持原文，不要翻譯；核驗與引用規則不變。"
        )
    # 简体也给出明确指令：固定 reply_language = "zh-CN" 或从其他语言切回简体时，模型不会沿用上一轮的语言
    return (
        "请用简体中文撰写给用户看的文字；指标名称、维度成员、字段名称、数据取值及引用的元数据"
        "保持原文，不要翻译；核验与引用规则不变。"
    )
