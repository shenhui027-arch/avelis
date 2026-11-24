"""认证配置、轻白标品牌、标准 OIDC（Authorization Code + PKCE + JIT）与反代 Forward Auth。"""

from __future__ import annotations

import copy
import hmac
import ipaddress
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from fastapi import HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from avelis import audit
from avelis.config import Settings
from avelis.db import utcnow
from avelis.i18n import Msg
from avelis.models import AppSetting, User, UserRole
from avelis.oauth.urls import request_settings
from avelis.security import decrypt_secret, encrypt_secret, normalize_username

logger = logging.getLogger(__name__)

AUTH_CONFIG_KEY = "auth_config"
ALLOWED_OIDC_ALGS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA")
ALLOWED_OIDC_ROLES = frozenset({"analyst", "steward", "reviewer"})
FORBIDDEN_FORWARD_HEADERS = frozenset(
    {"host", "cookie", "authorization", "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "content-length"}
)
_DISCOVERY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_DISCOVERY_TTL_SECONDS = 300

DEFAULT_AUTH_CONFIG: dict[str, Any] = {
    "local_login_enabled": True,
    "forward_auth_enabled": False,
    "forward_auth_header": "X-Forwarded-User",
    "forward_auth_secret_enc": None,
    "forward_auth_secret_hint": None,
    "forward_auth_auto_create": True,
    "oidc": {
        "enabled": False,
        "provider_name": "SSO",
        "issuer_url": "",
        "client_id": "",
        "client_secret_enc": None,
        "client_secret_hint": None,
        "scopes": "openid profile email",
        "auto_create_user": True,
        "default_role": "analyst",
    },
    "branding": {
        "brand_name": "Avelis",
        "brand_logo_url": None,
        "login_subtitle": None,
        "terms_url": None,
        "privacy_url": None,
    },
}


def _merge_defaults(stored: dict[str, Any] | None) -> dict[str, Any]:
    merged = copy.deepcopy(DEFAULT_AUTH_CONFIG)
    if not isinstance(stored, dict):
        return merged
    for key in (
        "local_login_enabled",
        "forward_auth_enabled",
        "forward_auth_header",
        "forward_auth_secret_enc",
        "forward_auth_secret_hint",
        "forward_auth_auto_create",
    ):
        if key in stored:
            merged[key] = stored[key]
    if isinstance(stored.get("oidc"), dict):
        merged["oidc"].update(stored["oidc"])
    if isinstance(stored.get("branding"), dict):
        merged["branding"].update(stored["branding"])
    return merged


def get_auth_config(session: Session) -> dict[str, Any]:
    try:
        row = session.get(AppSetting, AUTH_CONFIG_KEY)
    except OperationalError:
        return copy.deepcopy(DEFAULT_AUTH_CONFIG)
    return _merge_defaults(row.value if row is not None else None)


def _secret_hint(secret: str) -> str:
    return secret[-4:] if len(secret) >= 4 else secret


def _masked_hint(hint: str | None) -> str | None:
    return f"••••••••{hint}" if hint else None


def public_auth_config(session: Session) -> dict[str, Any]:
    cfg = get_auth_config(session)
    oidc = cfg["oidc"]
    branding = cfg["branding"]
    return {
        "local_login_enabled": bool(cfg["local_login_enabled"]),
        "forward_auth_enabled": bool(cfg["forward_auth_enabled"]),
        "oidc": (
            {
                "enabled": True,
                "provider_name": oidc["provider_name"] or "SSO",
                "authorize_url": "/api/auth/oidc/login",
            }
            if oidc.get("enabled") and oidc.get("issuer_url") and oidc.get("client_id")
            else None
        ),
        "branding": {
            "brand_name": branding.get("brand_name") or "Avelis",
            "brand_logo_url": branding.get("brand_logo_url"),
            "login_subtitle": branding.get("login_subtitle"),
            "terms_url": branding.get("terms_url"),
            "privacy_url": branding.get("privacy_url"),
        },
    }


def admin_auth_settings_view(session: Session) -> dict[str, Any]:
    cfg = get_auth_config(session)
    oidc = cfg["oidc"]
    branding = cfg["branding"]
    return {
        "local_login_enabled": bool(cfg["local_login_enabled"]),
        "forward_auth_enabled": bool(cfg["forward_auth_enabled"]),
        "forward_auth_header": cfg["forward_auth_header"],
        "forward_auth_auto_create": bool(cfg["forward_auth_auto_create"]),
        "has_forward_auth_secret": bool(cfg.get("forward_auth_secret_enc")),
        "forward_auth_secret_hint": _masked_hint(cfg.get("forward_auth_secret_hint")),
        "oidc": {
            "enabled": bool(oidc.get("enabled")),
            "provider_name": oidc.get("provider_name") or "SSO",
            "issuer_url": oidc.get("issuer_url") or "",
            "client_id": oidc.get("client_id") or "",
            "has_client_secret": bool(oidc.get("client_secret_enc")),
            "client_secret_hint": _masked_hint(oidc.get("client_secret_hint")),
            "scopes": oidc.get("scopes") or "openid profile email",
            "auto_create_user": bool(oidc.get("auto_create_user", True)),
            "default_role": oidc.get("default_role") or "analyst",
        },
        "branding": {
            "brand_name": branding.get("brand_name") or "Avelis",
            "brand_logo_url": branding.get("brand_logo_url"),
            "login_subtitle": branding.get("login_subtitle"),
            "terms_url": branding.get("terms_url"),
            "privacy_url": branding.get("privacy_url"),
        },
    }


def validate_safe_ui_url(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if "\\" in cleaned or any(ord(c) < 32 or ord(c) == 127 or c.isspace() for c in cleaned):
        raise HTTPException(422, Msg("{field} 包含非法空白或控制字符", field=field_name))
    lowered = cleaned.lower()
    if lowered.startswith(("javascript:", "data:", "vbscript:", "file:")):
        raise HTTPException(422, Msg("{field} 不支持不安全的协议", field=field_name))
    if cleaned.startswith("/"):
        if cleaned.startswith("//"):
            raise HTTPException(422, Msg("{field} 不能以 // 开头", field=field_name))
        return cleaned
    parsed = urlsplit(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(422, Msg("{field} 必须为 http(s) 地址或站内相对路径", field=field_name))
    return cleaned


def validate_http_url(value: str, field_name: str) -> str:
    cleaned = value.strip().rstrip("/")
    if not cleaned or "\\" in cleaned or any(ord(c) < 32 or ord(c) == 127 or c.isspace() for c in cleaned):
        raise HTTPException(422, Msg("{field} 必须为有效的 HTTP(S) 地址", field=field_name))
    parsed = urlsplit(cleaned)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(422, Msg("{field} 必须为无凭据、查询参数或片段的 HTTP(S) 地址", field=field_name))
    return cleaned


def _normalize_scopes(raw: str) -> str:
    tokens = [t.strip() for t in raw.split() if t.strip()]
    if "openid" not in tokens:
        tokens.insert(0, "openid")
    deduped = list(dict.fromkeys(tokens))
    return " ".join(deduped)


def save_auth_settings(session: Session, actor: User, payload: dict[str, Any]) -> dict[str, Any]:
    current = get_auth_config(session)
    oidc_in = payload["oidc"]
    branding_in = payload["branding"]

    local_login_enabled = bool(payload["local_login_enabled"])
    forward_auth_enabled = bool(payload["forward_auth_enabled"])
    oidc_enabled = bool(oidc_in["enabled"])

    if not local_login_enabled and not oidc_enabled and not forward_auth_enabled:
        raise HTTPException(422, "不能同时禁用本地登录和所有外部认证方式")

    forward_header = payload["forward_auth_header"].strip()
    if not re.fullmatch(r"[A-Za-z0-9-]+", forward_header) or forward_header.lower() in FORBIDDEN_FORWARD_HEADERS:
        raise HTTPException(422, "反代认证请求头名称不合法")

    issuer_url = oidc_in["issuer_url"].strip().rstrip("/")
    client_id = oidc_in["client_id"].strip()
    if oidc_enabled:
        issuer_url = validate_http_url(issuer_url, "issuer_url")
        if not client_id:
            raise HTTPException(422, "启用 OIDC 时必须填写 client_id")
    elif issuer_url:
        issuer_url = validate_http_url(issuer_url, "issuer_url")

    default_role = oidc_in["default_role"].strip()
    if default_role not in ALLOWED_OIDC_ROLES:
        raise HTTPException(422, "OIDC 默认角色不合法")

    brand_name = branding_in["brand_name"].strip()
    if not brand_name:
        raise HTTPException(422, "品牌名称不能为空")
    subtitle = branding_in.get("login_subtitle")
    subtitle_clean = subtitle.strip() if isinstance(subtitle, str) and subtitle.strip() else None

    if payload.get("clear_forward_auth_secret"):
        fw_secret_enc = None
        fw_secret_hint = None
    elif payload.get("forward_auth_secret"):
        raw_fw = payload["forward_auth_secret"]
        fw_secret_enc = encrypt_secret(raw_fw)
        fw_secret_hint = _secret_hint(raw_fw)
    else:
        fw_secret_enc = current.get("forward_auth_secret_enc")
        fw_secret_hint = current.get("forward_auth_secret_hint")

    if oidc_in.get("clear_client_secret"):
        oidc_secret_enc = None
        oidc_secret_hint = None
    elif oidc_in.get("client_secret"):
        raw_cs = oidc_in["client_secret"]
        oidc_secret_enc = encrypt_secret(raw_cs)
        oidc_secret_hint = _secret_hint(raw_cs)
    else:
        oidc_secret_enc = current["oidc"].get("client_secret_enc")
        oidc_secret_hint = current["oidc"].get("client_secret_hint")

    new_value = {
        "local_login_enabled": local_login_enabled,
        "forward_auth_enabled": forward_auth_enabled,
        "forward_auth_header": forward_header,
        "forward_auth_secret_enc": fw_secret_enc,
        "forward_auth_secret_hint": fw_secret_hint,
        "forward_auth_auto_create": bool(payload["forward_auth_auto_create"]),
        "oidc": {
            "enabled": oidc_enabled,
            "provider_name": (oidc_in["provider_name"] or "SSO").strip() or "SSO",
            "issuer_url": issuer_url,
            "client_id": client_id,
            "client_secret_enc": oidc_secret_enc,
            "client_secret_hint": oidc_secret_hint,
            "scopes": _normalize_scopes(oidc_in.get("scopes") or "openid profile email"),
            "auto_create_user": bool(oidc_in["auto_create_user"]),
            "default_role": default_role,
        },
        "branding": {
            "brand_name": brand_name,
            "brand_logo_url": validate_safe_ui_url(branding_in.get("brand_logo_url"), "brand_logo_url"),
            "login_subtitle": subtitle_clean,
            "terms_url": validate_safe_ui_url(branding_in.get("terms_url"), "terms_url"),
            "privacy_url": validate_safe_ui_url(branding_in.get("privacy_url"), "privacy_url"),
        },
    }

    row = session.get(AppSetting, AUTH_CONFIG_KEY)
    if row is None:
        session.add(AppSetting(key=AUTH_CONFIG_KEY, value=new_value, updated_by=actor.id, updated_at=utcnow()))
    else:
        row.value = new_value
        row.updated_by = actor.id
        row.updated_at = utcnow()
    session.flush()
    audit.record(
        session,
        actor,
        "auth_settings.update",
        "app_setting",
        AUTH_CONFIG_KEY,
        local_login_enabled=local_login_enabled,
        oidc_enabled=oidc_enabled,
        forward_auth_enabled=forward_auth_enabled,
        brand_name=brand_name,
    )
    return admin_auth_settings_view(session)


def sanitize_next_url(next_url: str | None) -> str:
    if (
        isinstance(next_url, str)
        and next_url.startswith("/")
        and not next_url.startswith("//")
        and "\\" not in next_url
        and not any(ord(c) < 32 or ord(c) == 127 for c in next_url)
    ):
        return next_url
    return "/"


def external_public_base(settings: Settings, request: Request) -> str:
    if settings.public_url:
        return settings.public_url.rstrip("/")
    return request_settings(settings, request).public_url.rstrip("/")


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10.0)


async def fetch_oidc_discovery(issuer_url: str) -> dict[str, Any]:
    normalized = issuer_url.rstrip("/")
    now = time.monotonic()
    cached = _DISCOVERY_CACHE.get(normalized)
    if cached and now - cached[0] < _DISCOVERY_TTL_SECONDS:
        return cached[1]
    url = f"{normalized}/.well-known/openid-configuration"
    async with _http_client() as client:
        resp = await client.get(url)
        resp.raise_for_status()
        doc = resp.json()
    doc_issuer = str(doc.get("issuer", "")).rstrip("/")
    if doc_issuer != normalized:
        raise ValueError("OIDC discovery issuer mismatch")
    for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        endpoint = str(doc.get(field, ""))
        parsed = urlsplit(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"Invalid OIDC {field}")
    _DISCOVERY_CACHE[normalized] = (now, doc)
    return doc


_JIT_CREATE_MAX_ATTEMPTS = 8


def _sanitize_jit_username_base(candidate: str) -> str:
    sanitized = re.sub(r"[^a-z0-9_.-]+", "_", normalize_username(candidate)).strip("._-")
    if len(sanitized) < 2:
        sanitized = f"sso_{secrets.token_hex(4)}"
    return sanitized[:56]


def _allocate_jit_username(session: Session, candidate: str) -> str:
    base = _sanitize_jit_username_base(candidate)
    attempt = base
    for _ in range(10):
        exists = session.scalar(select(User.id).where(func.lower(User.username) == attempt))
        if exists is None:
            return attempt
        attempt = f"{base[:50]}_{secrets.token_hex(2)}"
    return f"sso_{secrets.token_hex(6)}"


def _allocate_retry_jit_username(session: Session, candidate: str, tried: set[str]) -> str:
    base = _sanitize_jit_username_base(candidate)[:55]
    for _ in range(10):
        attempt = f"{base}_{secrets.token_hex(4)}"
        if attempt in tried:
            continue
        exists = session.scalar(select(User.id).where(func.lower(User.username) == attempt))
        if exists is None:
            return attempt
    while True:
        fallback = f"sso_{secrets.token_hex(6)}"
        if fallback not in tried:
            return fallback


def _refresh_existing_external_user(
    session: Session,
    user: User,
    display_name: str | None,
    email: str | None,
) -> tuple[User | None, str | None]:
    if not user.is_active:
        return None, "account_disabled"
    if display_name and display_name.strip():
        user.display_name = display_name.strip()[:128]
    if email and email.strip():
        user.email = email.strip()[:255]
    user.last_login_at = utcnow()
    session.flush()
    return user, None


def resolve_or_create_external_user(
    session: Session,
    *,
    provider: str,
    issuer: str,
    subject: str,
    preferred_username: str | None,
    display_name: str | None,
    email: str | None,
    auto_create: bool,
    default_role: str = "analyst",
) -> tuple[User | None, str | None]:
    """按 (external_issuer, external_sub) 解析外部用户，绝不自动合并已有本地同名/同邮箱账号。"""
    user = session.scalar(select(User).where(User.external_issuer == issuer, User.external_sub == subject))
    if user is not None:
        return _refresh_existing_external_user(session, user, display_name, email)

    if not auto_create:
        return None, "user_not_found"

    seed = (
        preferred_username
        or (email.split("@", 1)[0] if email and "@" in email else None)
        or f"{provider}_{subject[:12]}"
    )
    clean_email = email.strip()[:255] if isinstance(email, str) and email.strip() else None
    role = default_role if default_role in ALLOWED_OIDC_ROLES else "analyst"
    tried_usernames: set[str] = set()

    for attempt_idx in range(_JIT_CREATE_MAX_ATTEMPTS):
        username = (
            _allocate_jit_username(session, seed)
            if attempt_idx == 0
            else _allocate_retry_jit_username(session, seed, tried_usernames)
        )
        tried_usernames.add(username)
        clean_display = (display_name or preferred_username or username).strip()[:128] or username
        candidate = User(
            username=username,
            display_name=clean_display,
            email=clean_email,
            password_hash="!",
            auth_provider=provider,
            external_issuer=issuer,
            external_sub=subject,
            is_active=True,
            must_change_password=False,
            last_login_at=utcnow(),
        )
        try:
            with session.begin_nested():
                session.add(candidate)
                session.flush()
                session.add(UserRole(user_id=candidate.id, role=role))
                session.flush()
        except IntegrityError:
            existing = session.scalar(select(User).where(User.external_issuer == issuer, User.external_sub == subject))
            if existing is not None:
                return _refresh_existing_external_user(session, existing, display_name, email)
            if attempt_idx + 1 >= _JIT_CREATE_MAX_ATTEMPTS:
                raise
            continue

        audit.record(
            session,
            None,
            "user.create",
            "user",
            candidate.id,
            username=candidate.username,
            roles=[role],
            via=provider,
        )
        return candidate, None


async def verify_oidc_callback(
    *,
    oidc_cfg: dict[str, Any],
    code: str,
    redirect_uri: str,
    expected_nonce: str,
    code_verifier: str,
) -> dict[str, Any]:
    discovery = await fetch_oidc_discovery(oidc_cfg["issuer_url"])
    token_data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": oidc_cfg["client_id"],
        "code_verifier": code_verifier,
    }
    if oidc_cfg.get("client_secret_enc"):
        token_data["client_secret"] = decrypt_secret(oidc_cfg["client_secret_enc"])

    async with _http_client() as client:
        tok_resp = await client.post(discovery["token_endpoint"], data=token_data)
        tok_resp.raise_for_status()
        tokens = tok_resp.json()
        id_token = tokens.get("id_token")
        if not id_token:
            raise ValueError("Missing id_token in OIDC token response")
        jwks_resp = await client.get(discovery["jwks_uri"])
        jwks_resp.raise_for_status()
        jwks_data = jwks_resp.json()

        header = jwt.get_unverified_header(id_token)
        alg = header.get("alg")
        if alg not in ALLOWED_OIDC_ALGS:
            raise ValueError(f"Disallowed OIDC signing algorithm: {alg}")

        jwk_set = jwt.PyJWKSet.from_dict(jwks_data)
        kid = header.get("kid")
        if kid:
            signing_key = next((k for k in jwk_set.keys if k.key_id == kid), None)
        elif len(jwk_set.keys) == 1:
            signing_key = jwk_set.keys[0]
        else:
            signing_key = None
        if signing_key is None:
            raise ValueError("Matching JWKS signing key not found")

        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=[alg],
            audience=oidc_cfg["client_id"],
            issuer=discovery["issuer"],
            options={"require": ["sub", "iss", "aud", "exp", "iat"]},
        )
        if not hmac.compare_digest(str(claims.get("nonce", "")), expected_nonce):
            raise ValueError("OIDC nonce mismatch")

        if (
            discovery.get("userinfo_endpoint")
            and tokens.get("access_token")
            and (not claims.get("name") or not claims.get("email"))
        ):
            ui_resp = await client.get(
                discovery["userinfo_endpoint"],
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
            if ui_resp.status_code == 200:
                ui = ui_resp.json()
                if ui.get("sub") == claims["sub"]:
                    claims = {**ui, **claims}
        return claims


def build_oidc_authorize_url(
    discovery: dict[str, Any],
    *,
    client_id: str,
    redirect_uri: str,
    scopes: str,
    state: str,
    nonce: str,
    code_challenge: str,
) -> str:
    params = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    sep = "&" if "?" in discovery["authorization_endpoint"] else "?"
    return f"{discovery['authorization_endpoint']}{sep}{params}"


def is_trusted_proxy_ip(client_host: str | None, allowed_spec: str) -> bool:
    if not client_host:
        return False
    try:
        addr = ipaddress.ip_address(client_host.strip("[]"))
    except ValueError:
        return False
    for item in allowed_spec.split(","):
        token = item.strip()
        if not token or token == "*":
            continue
        try:
            if "/" in token:
                if addr in ipaddress.ip_network(token, strict=False):
                    return True
            elif addr == ipaddress.ip_address(token.strip("[]")):
                return True
        except ValueError:
            continue
    return False


def verify_forward_auth_trust(request: Request, cfg: dict[str, Any], settings: Settings) -> None:
    secret_enc = cfg.get("forward_auth_secret_enc")
    if secret_enc:
        expected = decrypt_secret(secret_enc)
        provided = request.headers.get("x-forwarded-auth-secret", "")
        if not provided or not hmac.compare_digest(provided, expected):
            raise HTTPException(401, "反代认证凭据无效")
        return
    if settings.forwarded_allow_ips.strip() == "*":
        raise HTTPException(403, "未配置共享密钥时不允许通配符代理信任")
    client_host = request.scope.get("avelis_direct_peer") or (request.client.host if request.client else None)
    if not is_trusted_proxy_ip(client_host, settings.forwarded_allow_ips):
        raise HTTPException(403, "请求来源不在受信任代理列表中")


def _invalidate_discovery_cache() -> None:
    _DISCOVERY_CACHE.clear()
