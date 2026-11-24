"""请求依赖：数据库会话、当前用户（Cookie / Bearer JWT / 个人访问令牌 PAT）。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis import cache
from avelis.db import session_factory, utcnow
from avelis.i18n import gettext
from avelis.models import PersonalAccessToken, User
from avelis.observability.context import bind_request_context
from avelis.security import decode_access_token, hash_pat_token, issued_before

ACCESS_COOKIE = "avelis_access"
REFRESH_COOKIE = "avelis_refresh"
MUST_CHANGE_PASSWORD_ALLOWLIST = frozenset({"/api/me", "/api/me/password", "/api/auth/logout"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token", auto_error=False)


def normalized_path(request: Request) -> str:
    path = request.scope.get("path", request.url.path)
    root = request.scope.get("root_path", "")
    if root and (path == root or path.startswith(root + "/")):
        path = path[len(root) :]
    return path


def get_session() -> Iterator[Session]:
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail=detail, headers={"WWW-Authenticate": "Bearer"}
    )


def _is_pat_disallowed_path(path: str) -> bool:
    return (
        path.startswith("/api/auth/")
        or path == "/api/me/password"
        or path.startswith("/api/me/totp")
        or path.startswith("/api/me/tokens")
        or path.startswith("/api/oauth/consent")
    )


def _aware_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _resolve_pat_claims(request: Request, raw_token: str, session: Session) -> dict:
    pat = session.scalar(select(PersonalAccessToken).where(PersonalAccessToken.token_hash == hash_pat_token(raw_token)))
    if pat is None or pat.revoked_at is not None:
        raise _unauthorized("个人访问令牌无效或已吊销")
    now = datetime.now(timezone.utc)
    if pat.expires_at is not None and _aware_utc(pat.expires_at) <= now:
        raise _unauthorized("个人访问令牌已过期")
    path = normalized_path(request)
    if _is_pat_disallowed_path(path):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="个人访问令牌无权访问认证与安全管理接口")
    scopes = set(pat.scopes or ())
    if request.method.upper() in _SAFE_METHODS:
        allowed = bool(scopes & {"read", "write"})
    else:
        allowed = "write" in scopes
    if not allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="个人访问令牌权限不足")
    if pat.last_used_at is None or (now - _aware_utc(pat.last_used_at)).total_seconds() >= 60:
        pat.last_used_at = utcnow()
        session.flush()
    return {
        "sub": pat.user_id,
        "jti": f"pat:{pat.id}",
        "token_type": "pat",
        "pat_id": pat.id,
        "scopes": list(pat.scopes or []),
        "iat": int(pat.created_at.timestamp()),
    }


def current_claims(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    session: Session = Depends(get_session),
) -> dict:
    if token and token.startswith("pat_"):
        return _resolve_pat_claims(request, token, session)
    jwt_token = token or request.cookies.get(ACCESS_COOKIE)
    if not jwt_token:
        raise _unauthorized("未提供有效凭据")
    try:
        claims = decode_access_token(jwt_token)
    except jwt.InvalidTokenError as exc:
        raise _unauthorized("访问令牌无效或已过期") from exc
    if cache.is_revoked(claims["jti"]):
        raise _unauthorized("访问令牌已注销")
    return claims


def current_user(
    request: Request,
    claims: dict = Depends(current_claims),
    session: Session = Depends(get_session),
) -> User:
    user = session.get(User, claims["sub"])
    if user is None or not user.is_active:
        raise _unauthorized("用户不存在或已停用")
    # 改密码、重置密码后，之前签发的令牌失效
    if (
        claims.get("token_type") != "pat"
        and user.sessions_valid_after is not None
        and issued_before(claims, user.sessions_valid_after)
    ):
        raise _unauthorized("会话已失效，请重新登录")
    if user.must_change_password:
        if claims.get("token_type") == "pat" or normalized_path(request) not in MUST_CHANGE_PASSWORD_ALLOWLIST:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "must_change_password", "detail": gettext("首次登录或密码重置后必须修改密码")},
            )
    bind_request_context(user_id=user.id)
    return user


def try_authenticated_user(request: Request, session: Session) -> User | None:
    """尽最大努力从 Bearer JWT 或 Cookie 解析当前有效用户；失败返回 None（用于 SPA 页面路由守卫）。"""
    auth_header = request.headers.get("authorization", "")
    token: str | None = None
    if auth_header.lower().startswith("bearer "):
        candidate = auth_header[7:].strip()
        if candidate and not candidate.startswith("pat_"):
            token = candidate
    if not token:
        token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        return None
    try:
        claims = decode_access_token(token)
    except jwt.InvalidTokenError:
        return None
    if cache.is_revoked(claims["jti"]):
        return None
    user = session.get(User, claims["sub"])
    if user is None or not user.is_active:
        return None
    if user.sessions_valid_after is not None and issued_before(claims, user.sessions_valid_after):
        return None
    bind_request_context(user_id=user.id)
    return user
