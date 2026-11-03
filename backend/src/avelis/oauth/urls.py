"""OAuth/MCP 共用的请求级对外地址；代理信任只由 uvicorn 处理。"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import replace
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import HTTPException
from mcp.server.auth.routes import build_resource_metadata_url
from pydantic import AnyHttpUrl, TypeAdapter, UrlConstraints
from starlette.requests import Request

from avelis.config import Settings

_HTTP_URL = TypeAdapter(Annotated[AnyHttpUrl, UrlConstraints(preserve_empty_path=True)])
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def request_settings(settings: Settings, request: Request) -> Settings:
    """冻结本次请求的 issuer/resource，不读任何 X-Forwarded-* 头。"""
    hosts = request.headers.getlist("host")
    try:
        if len(hosts) != 1:
            raise ValueError
        host = hosts[0]
        if not host or re.search(r"[^A-Za-z0-9.\-:\[\]]", host):
            raise ValueError
        parsed = urlsplit("//" + host)
        hostname = parsed.hostname
        if not hostname or parsed.port is not None and not 0 <= parsed.port <= 65535:
            raise ValueError
        if host.startswith("["):
            ipaddress.IPv6Address(hostname)
            if not re.fullmatch(r"\[[0-9A-Fa-f:.]+\](?::[0-9]+)?", host):
                raise ValueError
        else:
            if not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", host):
                raise ValueError
            try:
                ipaddress.IPv4Address(hostname)
            except ValueError:
                name = hostname.removesuffix(".")
                if (
                    len(name) > 253
                    or not all(_LABEL.fullmatch(label) for label in name.split("."))
                    or "." in name
                    and all(c in "0123456789." for c in name)
                ):
                    raise ValueError from None
        if request.scope["scheme"] not in ("http", "https"):
            raise ValueError
        if not settings.public_url:
            # 统一规范化一次，避免 PRM 的 URL 模型补尾斜杠或规范化端口后与 iss 不一致。
            issuer = _HTTP_URL.validate_python(
                request.scope["scheme"] + "://" + host + request.scope.get("root_path", "").rstrip("/")
            )
            if not host.startswith("[") and urlsplit(str(issuer)).hostname != hostname.lower():
                # 不接受把数字或十六进制主机别名暗中改写成另一 IP 的 URL 拼写。
                raise ValueError
    except ValueError:
        raise HTTPException(400, "Invalid Host header") from None
    if settings.public_url:
        return settings
    return replace(settings, public_url=str(issuer))


def resource_metadata_url(settings: Settings, request: Request) -> AnyHttpUrl:
    if settings.public_url:
        # 保留显式 public_url 原有的 RFC 9728 路由位置。
        return build_resource_metadata_url(AnyHttpUrl(settings.mcp_resource_url))
    return AnyHttpUrl(request_settings(settings, request).public_url + "/.well-known/oauth-protected-resource/mcp")
