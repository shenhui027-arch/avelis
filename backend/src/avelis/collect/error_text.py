"""采集 / 测试连接的错误说明：把驱动返回的原始报错归类，给出按请求语言翻译的说明；原文保留供排查。

原始报错来自各引擎驱动（英文为主、格式各异），直接显示对使用者不友好。这里只做“认得出就说清楚”：
认不出的归为 unknown，不给说明，界面照旧显示原文。规则按顺序匹配，先匹配到的为准。
"""

from __future__ import annotations

from avelis.i18n import gettext

ERROR_KINDS = ("unreachable", "timeout", "auth", "permission", "tls", "unknown")

_RULES = (
    ("tls", ("ssl", "tls", "certificate", "x509")),
    (
        "auth",
        (
            "access denied for user",
            "password authentication failed",
            "login failed",
            "authentication failed",
            "ora-01017",
            "invalid username/password",
            "wrong password",
            "code: 516",
        ),
    ),
    (
        "permission",
        (
            "permission denied",
            "command denied",
            "insufficient privilege",
            "ora-01031",
            "not authorized",
            "access denied",
        ),
    ),
    ("timeout", ("timed out", "timeout", "连接超时")),
    (
        "unreachable",
        (
            "can't connect",
            "could not connect",
            "connection refused",
            "network is unreachable",
            "no route to host",
            "name or service not known",
            "nodename nor servname",
            "getaddrinfo",
            "unknown host",
            "could not translate host name",
            "ora-12541",
            "ora-12514",
            "tns:",
            "connection reset",
            "errno 101",
            "errno 111",
        ),
    ),
)


def classify(raw: str | None) -> str | None:
    """原始报错 → 类别；raw 为空（None 或只有空白）时返回 None。不区分大小写，按以下顺序匹配，先中先得：
    1. tls：含 "ssl"、"tls"、"certificate"、"x509"
    2. auth：含 "access denied for user"、"password authentication failed"、"login failed"、"authentication failed"、
       "ora-01017"、"invalid username/password"、"wrong password"、"code: 516"
    3. permission：含 "permission denied"、"command denied"、"insufficient privilege"、"ora-01031"、
       "not authorized"、"access denied"（排在 auth 之后，避免把“用户口令错误”的 access denied 归为权限）
    4. timeout：含 "timed out"、"timeout"、"连接超时"
    5. unreachable：含 "can't connect"、"could not connect"、"connection refused"、"network is unreachable"、
       "no route to host"、"name or service not known"、"nodename nor servname"、"getaddrinfo"、"unknown host"、
       "could not translate host name"、"ora-12541"、"ora-12514"、"tns:"、"connection reset"、"errno 101"、"errno 111"
    6. 以上都不中：unknown
    """
    if raw is None or not raw.strip():
        return None
    normalized = raw.lower()
    for kind, keywords in _RULES:
        if any(keyword in normalized for keyword in keywords):
            return kind
    return "unknown"


def explain(raw: str | None) -> str | None:
    """按 classify 的类别返回已翻译的说明（gettext，按当前请求语言）；unknown 或 raw 为空返回 None。说明原文：
    - unreachable：无法连接到数据库：请检查地址、端口和网络是否可达
    - timeout：连接数据库超时：请检查网络，或稍后重试
    - auth：账号或口令错误：请检查采集账号
    - permission：权限不足：采集账号缺少读取元数据的权限
    - tls：TLS 连接失败：请检查证书与 TLS 配置
    """
    match classify(raw):
        case "unreachable":
            return gettext("无法连接到数据库：请检查地址、端口和网络是否可达")
        case "timeout":
            return gettext("连接数据库超时：请检查网络，或稍后重试")
        case "auth":
            return gettext("账号或口令错误：请检查采集账号")
        case "permission":
            return gettext("权限不足：采集账号缺少读取元数据的权限")
        case "tls":
            return gettext("TLS 连接失败：请检查证书与 TLS 配置")
    return None
