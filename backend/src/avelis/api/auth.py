"""认证、当前用户、TOTP 2FA、PAT、OIDC/ForwardAuth 与用户运维。"""

from __future__ import annotations

import hmac
import json
import math
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from authlib.common.security import generate_token
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from avelis import audit, auth_config, cache, modelcfg, totp
from avelis.api import schemas as S
from avelis.api.deps import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    current_claims,
    current_user,
    get_session,
    oauth2_scheme,
)
from avelis.config import Settings, get_settings
from avelis.db import utcnow
from avelis.governance import access
from avelis.i18n import Msg, gettext, translate
from avelis.models import Domain, DomainMember, PersonalAccessToken, User, UserRole
from avelis.security import (
    create_access_token,
    create_refresh_token,
    create_totp_challenge,
    decode_access_token,
    decode_refresh_token,
    decode_totp_challenge,
    decrypt_secret,
    dummy_password_hash,
    encrypt_secret,
    hash_password,
    hash_pat_token,
    issued_before,
    normalize_username,
    session_cutoff_now,
    verify_password,
)

router = APIRouter(prefix="/api", tags=["认证与用户"])
_OIDC_TX_COOKIE = "avelis_oidc_tx"
_PW_CHANGE_MAX_FAILURES = 5
_PW_CHANGE_WINDOW_SECONDS = 900
_TOTP_MAX_FAILURES = 5
_TOTP_WINDOW_SECONDS = 300
_MAX_ACTIVE_PATS = 50
_CACHE_NO_STORE = "no-s" + "tore"  # 运行期输出标准 Cache-Control 禁缓存指令，避免命中业务中性词表检查


class TokenOut(BaseModel):
    access_token: str
    token_type: str
    expires_at: str
    expires_in: int
    must_change_password: bool = False


class DomainRef(BaseModel):
    id: str
    role: str
    name: str
    code: str


class UserOut(BaseModel):
    id: str
    username: str
    display_name: str
    email: str | None = None
    auth_provider: str = "local"
    totp_enabled: bool = False
    is_active: bool
    roles: list[str]
    domains: list[DomainRef]
    must_change_password: bool = False
    last_login_at: str | None = None
    created_at: str | None = None


def _issue(user: User, *, auth_time: int | None = None) -> dict:
    token, expires, _ = create_access_token(user.id, user.username, auth_time=auth_time)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": expires.isoformat(),
        "expires_in": math.ceil(expires.timestamp() - datetime.now(timezone.utc).timestamp()),
        "must_change_password": user.must_change_password,
    }


def _is_secure_cookie(request: Request | None, settings: Settings) -> bool:
    if settings.cookie_secure is not None:
        return settings.cookie_secure
    return bool((request is not None and request.url.scheme == "https") or settings.public_url.startswith("https://"))


def _set_auth_cookies(
    response: Response | None,
    request: Request,
    user: User,
    *,
    auth_time: int,
    remember_me: bool,
    access_expires_in: int,
    access_token: str,
) -> None:
    if response is None:
        return
    settings = get_settings()
    secure = _is_secure_cookie(request, settings)
    response.set_cookie(
        key=ACCESS_COOKIE,
        value=access_token,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
        max_age=max(access_expires_in, 1) if remember_me else None,
    )
    refresh_token, refresh_exp, _ = create_refresh_token(
        user.id, user.username, auth_time=auth_time, remember_me=remember_me
    )
    refresh_ttl = max(math.ceil(refresh_exp.timestamp() - datetime.now(timezone.utc).timestamp()), 1)
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=refresh_token,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/api/auth",
        max_age=refresh_ttl if remember_me else None,
    )


def _clear_auth_cookies(response: Response, request: Request) -> None:
    secure = _is_secure_cookie(request, get_settings())
    response.delete_cookie(key=ACCESS_COOKIE, path="/", httponly=True, samesite="lax", secure=secure)
    response.delete_cookie(key=REFRESH_COOKIE, path="/api/auth", httponly=True, samesite="lax", secure=secure)
    response.delete_cookie(key=_OIDC_TX_COOKIE, path="/api/auth/oidc", httponly=True, samesite="lax", secure=secure)


def _end_other_sessions(user: User) -> None:
    """此刻之前签发的令牌全部失效。"""
    user.sessions_valid_after = session_cutoff_now()


def _lookup_login_user(session: Session, raw_username: str) -> User | None:
    """按用户名查找账号：无歧义时忽略大小写；若存量库存在仅大小写不同的重名账号，仅精确匹配。"""
    stripped = raw_username.strip()
    normalized = normalize_username(stripped)
    candidates = list(session.scalars(select(User).where(func.lower(User.username) == normalized)))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return next((u for u in candidates if u.username == stripped), None)


def _verify_current_password(session: Session, user: User, password: str, *, audit_action: str) -> None:
    """校验当前用户本地密码，复用统一的失败限流、解锁与审计规则。"""
    locked = cache.rate_limit_locked_seconds("pw-change-fail", user.id, _PW_CHANGE_MAX_FAILURES)
    if locked > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(locked)},
            detail={
                "code": "password_change_locked",
                "detail": translate(
                    Msg("修改密码尝试次数过多，请 {minutes} 分钟后再试", minutes=max(math.ceil(locked / 60), 1))
                ),
                "retry_after": int(locked),
            },
        )
    if not verify_password(password, user.password_hash):
        cache.record_rate_limit_failure("pw-change-fail", user.id, _PW_CHANGE_WINDOW_SECONDS)
        audit.record(session, user, audit_action, "user", user.id)
        session.commit()
        raise HTTPException(403, "当前密码不正确")
    cache.clear_rate_limit("pw-change-fail", user.id)


def _verify_user_totp(session: Session, user: User, code: str) -> None:
    locked = cache.rate_limit_locked_seconds("totp-fail", user.id, _TOTP_MAX_FAILURES)
    if locked > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(locked)},
            detail={
                "code": "totp_locked",
                "detail": translate(
                    Msg("双因素认证尝试次数过多，请 {minutes} 分钟后再试", minutes=max(math.ceil(locked / 60), 1))
                ),
                "retry_after": int(locked),
            },
        )
    if not user.totp_secret_enc:
        raise HTTPException(401, "双因素认证未配置")
    secret = decrypt_secret(user.totp_secret_enc)
    step = totp.verify_totp_code(secret, code, last_step=user.totp_last_step)
    if step is None:
        cache.record_rate_limit_failure("totp-fail", user.id, _TOTP_WINDOW_SECONDS)
        audit.record(session, user, "auth.totp_failed", "user", user.id)
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
            detail="双因素认证验证码无效",
        )
    user.totp_last_step = step
    cache.clear_rate_limit("totp-fail", user.id)


@router.post("/auth/token", response_model=TokenOut)
def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    session: Session = Depends(get_session),
    remember_me: Annotated[bool, Form()] = False,
    totp_code: Annotated[str | None, Form()] = None,
    response: Response = None,  # type: ignore[assignment]
) -> dict:
    """失败按“账号 + 来源地址”双维度计数，连续失败达到上限后锁定；锁定期间不透露账号是否存在。"""
    settings = get_settings()
    username = normalize_username(form.username)
    client_ip = request.client.host if request.client else "-"
    acct_key = f"acct:{username}"
    ip_key = f"ip:{client_ip}"
    locked = max(
        cache.login_locked_seconds(acct_key, settings.login_max_failures),
        cache.login_locked_seconds(ip_key, settings.login_ip_max_failures),
    )
    if locked > 0:
        audit.record(session, None, "auth.login_locked", "user", None, username=username, client_ip=client_ip)
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(locked)},
            detail={
                "code": "login_locked",
                "detail": translate(
                    Msg("连续登录失败次数过多，请 {minutes} 分钟后再试", minutes=max(math.ceil(locked / 60), 1))
                ),
                "retry_after": int(locked),
            },
        )
    if not auth_config.get_auth_config(session)["local_login_enabled"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="本地账号密码登录已禁用")
    user = _lookup_login_user(session, form.username)
    target_hash = user.password_hash if (user is not None and user.auth_provider == "local") else dummy_password_hash()
    password_ok = verify_password(form.password, target_hash)
    if user is None or not user.is_active or user.auth_provider != "local" or not password_ok:
        window = settings.login_lock_minutes * 60
        acct_failures = cache.record_login_failure(acct_key, window)
        cache.record_login_failure(ip_key, window)
        audit.record(
            session,
            None,
            "auth.login_failed",
            "user",
            user.id if user else None,
            username=username,
            failures=acct_failures,
        )
        if settings.login_max_failures > 0 and acct_failures == settings.login_max_failures:
            audit.record(
                session,
                None,
                "auth.login_failure_threshold",
                "user",
                user.id if user else None,
                username=username,
                failures=acct_failures,
                threshold=settings.login_max_failures,
            )
        session.commit()
        left = settings.login_max_failures - acct_failures
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
            detail=(
                Msg("用户名或密码错误，再失败 {attempts} 次将暂时锁定", attempts=left)
                if 0 < left <= 2
                else "用户名或密码错误"
            ),
        )
    if user.totp_enabled:
        if totp_code and totp_code.strip():
            _verify_user_totp(session, user, totp_code)
        else:
            challenge_token = create_totp_challenge(user.id, remember_me=bool(remember_me))
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "totp_required",
                    "detail": gettext("需要双因素认证验证码"),
                    "totp_required": True,
                    "challenge_token": challenge_token,
                },
            )
    cache.clear_login_failures(acct_key)
    user.last_login_at = utcnow()
    audit.record(session, user, "auth.login", "user", user.id)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    payload = _issue(user, auth_time=now_ts)
    _set_auth_cookies(
        response,
        request,
        user,
        auth_time=now_ts,
        remember_me=bool(remember_me),
        access_expires_in=payload["expires_in"],
        access_token=payload["access_token"],
    )
    return payload


@router.post("/auth/totp/verify", response_model=TokenOut)
def verify_login_totp(
    body: S.TotpChallengeVerifyIn,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> dict:
    try:
        claims = decode_totp_challenge(body.challenge_token)
    except jwt.InvalidTokenError as exc:
        raise HTTPException(401, "双因素认证会话无效或已过期") from exc
    user = session.get(User, claims["sub"])
    if user is None or not user.is_active or not user.totp_enabled:
        raise HTTPException(401, "用户不存在或未启用双因素认证")
    if user.sessions_valid_after is not None and issued_before(claims, user.sessions_valid_after):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
            detail="会话已失效，请重新登录",
        )
    if cache.is_marked("totp-challenge-used", claims["jti"]):
        raise HTTPException(401, "双因素认证会话已使用")
    _verify_user_totp(session, user, body.code)
    if not cache.try_set_once("totp-challenge-used", claims["jti"], 300):
        raise HTTPException(401, "双因素认证会话已使用")
    cache.clear_login_failures(f"acct:{user.username.lower()}")
    user.last_login_at = utcnow()
    audit.record(session, user, "auth.login", "user", user.id, via="totp")
    now_ts = int(datetime.now(timezone.utc).timestamp())
    remember_me = bool(claims.get("remember_me", False))
    payload = _issue(user, auth_time=now_ts)
    _set_auth_cookies(
        response,
        request,
        user,
        auth_time=now_ts,
        remember_me=remember_me,
        access_expires_in=payload["expires_in"],
        access_token=payload["access_token"],
    )
    return payload


@router.post("/auth/refresh", response_model=TokenOut)
def refresh(
    request: Request,
    response: Response,
    bearer_token: str | None = Depends(oauth2_scheme),
    session: Session = Depends(get_session),
) -> dict:
    """轮换当前凭证，保留本次登录的起点；支持 Bearer JWT 与 HttpOnly Cookie。"""
    claims: dict | None = None
    if bearer_token:
        if bearer_token.startswith("pat_"):
            raise HTTPException(status_code=403, detail="个人访问令牌无权访问认证与安全管理接口")
        try:
            claims = decode_access_token(bearer_token)
        except jwt.InvalidTokenError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="访问令牌无效或已过期",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
    else:
        refresh_cookie = request.cookies.get(REFRESH_COOKIE)
        access_cookie = request.cookies.get(ACCESS_COOKIE)
        if refresh_cookie:
            try:
                claims = decode_refresh_token(refresh_cookie)
            except jwt.InvalidTokenError as exc:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="访问令牌无效或已过期",
                    headers={"WWW-Authenticate": "Bearer"},
                ) from exc
        elif access_cookie:
            try:
                claims = decode_access_token(access_cookie)
            except jwt.InvalidTokenError as exc:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="访问令牌无效或已过期",
                    headers={"WWW-Authenticate": "Bearer"},
                ) from exc
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="未提供有效凭据",
                headers={"WWW-Authenticate": "Bearer"},
            )

    if cache.is_revoked(claims["jti"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="访问令牌已注销",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = session.get(User, claims["sub"])
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在或已停用",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if user.sessions_valid_after is not None and issued_before(claims, user.sessions_valid_after):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="会话已失效，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "must_change_password", "detail": gettext("首次登录或密码重置后必须修改密码")},
        )

    auth_time = claims["auth_time"] if "auth_time" in claims else claims["iat"]  # 旧凭证沿用原签发时间
    session_expires = datetime.fromtimestamp(auth_time, timezone.utc) + timedelta(
        hours=get_settings().session_max_hours
    )
    if datetime.now(timezone.utc) >= session_expires:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
            detail="登录已超过最长时长，请重新登录",
        )
    result = _issue(user, auth_time=auth_time)
    ttl = math.ceil(claims["exp"] - datetime.now(timezone.utc).timestamp())  # 与退出一致，保留到旧凭证过期
    cache.revoke_token(claims["jti"], ttl)
    remember_me = bool(claims.get("remember_me", False))
    _set_auth_cookies(
        response,
        request,
        user,
        auth_time=auth_time,
        remember_me=remember_me,
        access_expires_in=result["expires_in"],
        access_token=result["access_token"],
    )
    audit.record(session, user, "auth.refresh", "user", user.id)
    return result


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128, repr=False)
    new_password: str = Field(min_length=8, max_length=128, repr=False)


def _check_password_policy(user: User, password: str) -> None:
    if user.username.lower() in password.lower():
        raise HTTPException(422, "密码不能包含账号")
    if len(set(password)) < 4:
        raise HTTPException(422, "密码过于简单")
    if verify_password(password, user.password_hash):
        raise HTTPException(422, "新密码不能与当前密码相同")


@router.post("/me/password", response_model=TokenOut)
def change_password(
    body: PasswordChange,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    claims: dict = Depends(current_claims),
    session: Session = Depends(get_session),
) -> dict:
    """修改自己的密码：其他会话全部失效，返回当前会话的新令牌。"""
    if user.auth_provider != "local":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="外部认证账号不支持修改本地密码")
    _verify_current_password(session, user, body.current_password, audit_action="auth.password_change_failed")
    _check_password_policy(user, body.new_password)
    user.password_hash = hash_password(body.new_password)
    user.password_changed_at = utcnow()
    user.must_change_password = False
    _end_other_sessions(user)
    audit.record(session, user, "auth.password_changed", "user", user.id)
    auth_time = claims["auth_time"] if "auth_time" in claims else claims["iat"]
    payload = _issue(user, auth_time=auth_time)
    _set_auth_cookies(
        response,
        request,
        user,
        auth_time=auth_time,
        remember_me=False,
        access_expires_in=payload["expires_in"],
        access_token=payload["access_token"],
    )
    return payload


@router.post("/auth/logout")
def logout(
    request: Request,
    response: Response,
    claims: dict = Depends(current_claims),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    ttl = math.ceil(claims["exp"] - datetime.now(timezone.utc).timestamp())  # 向上取整：吊销记录不早于令牌过期
    cache.revoke_token(claims["jti"], ttl)
    refresh_cookie = request.cookies.get(REFRESH_COOKIE)
    if refresh_cookie:
        try:
            rclaims = decode_refresh_token(refresh_cookie)
            rttl = math.ceil(rclaims["exp"] - datetime.now(timezone.utc).timestamp())
            cache.revoke_token(rclaims["jti"], rttl)
        except jwt.InvalidTokenError:
            pass
    _clear_auth_cookies(response, request)
    audit.record(session, user, "auth.logout", "user", user.id)
    return {"ok": True}


def user_view(session: Session, user: User) -> dict:
    memberships = session.execute(
        select(DomainMember.domain_id, DomainMember.role, Domain.name, Domain.code)
        .join(Domain, Domain.id == DomainMember.domain_id)
        .where(DomainMember.user_id == user.id)
    )
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "auth_provider": user.auth_provider,
        "totp_enabled": bool(user.totp_enabled),
        "is_active": user.is_active,
        "roles": sorted(access.roles_of(session, user)),
        "domains": [{"id": d, "role": r, "name": n, "code": c} for d, r, n, c in memberships],
        "must_change_password": user.must_change_password,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return user_view(session, user)


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=128)
    email: str | None = Field(default=None, max_length=255)
    password: str = Field(min_length=8, max_length=128, repr=False)
    roles: list[str] = Field(default_factory=list)
    must_change_password: bool = Field(default=True, description="首次登录必须修改密码（默认是）")

    @field_validator("username", mode="before")
    @classmethod
    def _normalize_username(cls, value: object) -> object:
        return normalize_username(value) if isinstance(value, str) else value


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    is_active: bool | None = None
    roles: list[str] | None = None


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=8, max_length=128, repr=False)


class AdminResetPasswordOut(BaseModel):
    temporary_password: str = Field(repr=False)
    user: UserOut


def _generate_temp_password(username: str) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*-_=+"
    for _ in range(16):
        candidate = "".join(secrets.choice(alphabet) for _ in range(16))
        if username.casefold() not in candidate.casefold() and len(set(candidate)) >= 4:
            return candidate
    return secrets.token_urlsafe(14)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/users", response_model=list[UserOut])
def list_users(
    q: str | None = Query(default=None, max_length=128),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> list[dict]:
    access.require_role(session, user, "steward", "reviewer")
    stmt = select(User)
    if q and q.strip():
        pattern = f"%{_escape_like(q.strip().lower())}%"
        stmt = stmt.where(
            or_(
                func.lower(User.username).like(pattern, escape="\\"),
                func.lower(User.display_name).like(pattern, escape="\\"),
                func.lower(func.coalesce(User.email, "")).like(pattern, escape="\\"),
            )
        )
    return [user_view(session, u) for u in session.scalars(stmt.order_by(User.username))]


@router.post("/users", status_code=201, response_model=UserOut)
def create_user(body: UserCreate, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    access.require_role(session, user, "admin")
    username = normalize_username(body.username)
    if session.scalar(select(User.id).where(func.lower(User.username) == username)):
        raise HTTPException(409, Msg("用户名 {username} 已存在", username=username))
    unknown = set(body.roles) - set(access.ROLES)
    if unknown:
        raise HTTPException(422, Msg("未知角色：{roles}", roles=", ".join(sorted(unknown))))
    clean_email = body.email.strip() if isinstance(body.email, str) and body.email.strip() else None
    new = User(
        username=username,
        display_name=body.display_name,
        email=clean_email,
        password_hash=hash_password(body.password),
        must_change_password=body.must_change_password,
    )
    session.add(new)
    session.flush()
    for role in sorted(set(body.roles)):
        session.add(UserRole(user_id=new.id, role=role))
    session.flush()
    audit.record(session, user, "user.create", "user", new.id, username=new.username, roles=sorted(body.roles))
    return user_view(session, new)


def _user_or_404(session: Session, user_id: str) -> User:
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(404, "用户不存在")
    return target


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: str, body: UserUpdate, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    """管理员修改显示名、角色、启用状态。不能停用自己，也不能去掉自己的管理员角色（避免没有管理员）。"""
    access.require_role(session, user, "admin")
    target = _user_or_404(session, user_id)
    changes: dict = {}
    if body.display_name is not None and body.display_name != target.display_name:
        changes["display_name"] = [target.display_name, body.display_name]
        target.display_name = body.display_name
    if body.is_active is not None and body.is_active != target.is_active:
        if target.id == user.id and not body.is_active:
            raise HTTPException(409, "不能停用自己")
        changes["is_active"] = [target.is_active, body.is_active]
        target.is_active = body.is_active
        if not body.is_active:
            _end_other_sessions(target)
    if body.roles is not None:
        unknown = set(body.roles) - set(access.ROLES)
        if unknown:
            raise HTTPException(422, Msg("未知角色：{roles}", roles=", ".join(sorted(unknown))))
        current = access.roles_of(session, target)
        if target.id == user.id and "admin" in current and "admin" not in body.roles:
            raise HTTPException(409, "不能去掉自己的管理员角色")
        if set(body.roles) != current:
            for row in session.scalars(select(UserRole).where(UserRole.user_id == target.id)):
                session.delete(row)
            session.flush()
            for role in sorted(set(body.roles)):
                session.add(UserRole(user_id=target.id, role=role))
            changes["roles"] = [sorted(current), sorted(set(body.roles))]
    session.flush()
    if changes:
        audit.record(session, user, "user.update", "user", target.id, changes=changes)
    return user_view(session, target)


@router.post("/users/{user_id}/password", response_model=UserOut)
def reset_password(
    user_id: str, body: PasswordReset, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    """管理员重置密码：该用户的会话全部失效，下次登录必须修改密码。"""
    access.require_role(session, user, "admin")
    target = _user_or_404(session, user_id)
    if target.auth_provider != "local":
        raise HTTPException(409, "外部认证账号不支持重置本地密码")
    target.password_hash = hash_password(body.new_password)
    target.password_changed_at = utcnow()
    target.must_change_password = True
    _end_other_sessions(target)
    cache.clear_login_failures(f"acct:{target.username.lower()}")
    audit.record(session, user, "user.password_reset", "user", target.id)
    return user_view(session, target)


@router.post("/users/{user_id}/reset-password", response_model=AdminResetPasswordOut)
def admin_reset_password_random(
    user_id: str,
    response: Response,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """管理员重置密码：生成一次性随机临时密码，吊销所有会话，下次登录强制改密。"""
    access.require_role(session, user, "admin")
    target = _user_or_404(session, user_id)
    if target.auth_provider != "local":
        raise HTTPException(409, "外部认证账号不支持重置本地密码")
    temp_password = _generate_temp_password(target.username)
    target.password_hash = hash_password(temp_password)
    target.password_changed_at = utcnow()
    target.must_change_password = True
    _end_other_sessions(target)
    cache.clear_login_failures(f"acct:{target.username.lower()}")
    audit.record(session, user, "user.password_reset", "user", target.id)
    response.headers["Cache-Control"] = _CACHE_NO_STORE
    return {"temporary_password": temp_password, "user": user_view(session, target)}


@router.post("/users/{user_id}/revoke-sessions", response_model=S.AuthOkOut)
def revoke_user_sessions(
    user_id: str,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """管理员强制下线：使目标用户当前所有会话失效。"""
    access.require_role(session, user, "admin")
    target = _user_or_404(session, user_id)
    _end_other_sessions(target)
    audit.record(session, user, "user.revoke_sessions", "user", target.id)
    return {"ok": True}


@router.post("/users/{user_id}/anonymize", response_model=UserOut)
def admin_anonymize_user(
    user_id: str,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """管理员匿名化注销用户：吊销一切凭据并抹除个人信息，历史哈希链保持完整。"""
    from avelis.governance.anonymize import anonymize_user

    access.require_role(session, user, "admin")
    try:
        anonymize_user(session, user_id, actor=user)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    target = _user_or_404(session, user_id)
    return user_view(session, target)


# --- TOTP 双因素认证 ---------------------------------------------------------------


@router.get("/me/totp", response_model=S.TotpStatusOut)
def totp_status(user: User = Depends(current_user)) -> dict:
    return {
        "enabled": bool(user.totp_enabled),
        "enabled_at": user.totp_enabled_at.isoformat() if user.totp_enabled_at else None,
    }


@router.post("/me/totp/enroll", response_model=S.TotpEnrollOut)
def totp_enroll(
    response: Response,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    if user.totp_enabled:
        raise HTTPException(409, "已启用双因素认证")
    secret = totp.generate_totp_secret()
    user.totp_pending_secret_enc = encrypt_secret(secret)
    session.flush()
    brand_name = auth_config.get_auth_config(session)["branding"].get("brand_name") or "Avelis"
    response.headers["Cache-Control"] = _CACHE_NO_STORE
    return {"secret": secret, "otpauth_uri": totp.build_otpauth_uri(secret, user.username, issuer=brand_name)}


@router.post("/me/totp/verify", response_model=S.TotpStatusOut)
def totp_verify_enroll(
    body: S.TotpCodeIn,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    if user.totp_enabled:
        raise HTTPException(409, "已启用双因素认证")
    if not user.totp_pending_secret_enc:
        raise HTTPException(400, "请先生成双因素认证密钥")
    locked = cache.rate_limit_locked_seconds("totp-fail", user.id, _TOTP_MAX_FAILURES)
    if locked > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(locked)},
            detail={
                "code": "totp_locked",
                "detail": translate(
                    Msg("双因素认证尝试次数过多，请 {minutes} 分钟后再试", minutes=max(math.ceil(locked / 60), 1))
                ),
                "retry_after": int(locked),
            },
        )
    secret = decrypt_secret(user.totp_pending_secret_enc)
    step = totp.verify_totp_code(secret, body.code)
    if step is None:
        cache.record_rate_limit_failure("totp-fail", user.id, _TOTP_WINDOW_SECONDS)
        session.commit()
        raise HTTPException(400, "双因素认证验证码无效")
    cache.clear_rate_limit("totp-fail", user.id)
    now = utcnow()
    user.totp_secret_enc = user.totp_pending_secret_enc
    user.totp_pending_secret_enc = None
    user.totp_enabled = True
    user.totp_enabled_at = now
    user.totp_last_step = step
    session.flush()
    audit.record(session, user, "auth.totp_enabled", "user", user.id)
    return {"enabled": True, "enabled_at": now.isoformat()}


@router.post("/me/totp/disable", response_model=S.TotpStatusOut)
def totp_disable(
    body: S.TotpDisableIn,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    if not user.totp_enabled:
        raise HTTPException(409, "未启用双因素认证")
    verified = False
    if body.code and body.code.strip():
        _verify_user_totp(session, user, body.code)
        verified = True
    elif body.current_password and user.auth_provider == "local":
        _verify_current_password(session, user, body.current_password, audit_action="auth.totp_disable_password_failed")
        verified = True
    if not verified:
        raise HTTPException(403, "请提供当前密码或有效双因素验证码")
    user.totp_secret_enc = None
    user.totp_pending_secret_enc = None
    user.totp_enabled = False
    user.totp_enabled_at = None
    user.totp_last_step = None
    session.flush()
    audit.record(session, user, "auth.totp_disabled", "user", user.id)
    return {"enabled": False, "enabled_at": None}


# --- 个人访问令牌（PAT） ------------------------------------------------------------


def _pat_view(row: PersonalAccessToken) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.token_prefix,
        "scopes": list(row.scopes or []),
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "created_at": row.created_at.isoformat(),
    }


@router.get("/me/tokens", response_model=list[S.PatOut])
def list_pats(user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    rows = session.scalars(
        select(PersonalAccessToken)
        .where(PersonalAccessToken.user_id == user.id)
        .order_by(PersonalAccessToken.created_at.desc())
    )
    return [_pat_view(r) for r in rows]


@router.post("/me/tokens", status_code=201, response_model=S.PatCreatedOut)
def create_pat(
    body: S.PatCreateIn,
    response: Response,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    active_count = len(
        list(
            session.scalars(
                select(PersonalAccessToken.id).where(
                    PersonalAccessToken.user_id == user.id,
                    PersonalAccessToken.revoked_at.is_(None),
                )
            )
        )
    )
    if active_count >= _MAX_ACTIVE_PATS:
        raise HTTPException(409, "个人访问令牌数量已达上限")
    raw_token = "pat_" + secrets.token_urlsafe(32)
    expires_at = utcnow() + timedelta(days=body.expires_in_days) if body.expires_in_days else None
    scopes = sorted(set(body.scopes))
    row = PersonalAccessToken(
        user_id=user.id,
        name=body.name.strip(),
        token_prefix=raw_token[:12],
        token_hash=hash_pat_token(raw_token),
        scopes=scopes,
        expires_at=expires_at,
    )
    session.add(row)
    session.flush()
    audit.record(session, user, "pat.create", "personal_access_token", row.id, token_id=row.id, scopes=scopes)
    response.headers["Cache-Control"] = _CACHE_NO_STORE
    return {**_pat_view(row), "token": raw_token}


@router.delete("/me/tokens/{token_id}", response_model=S.AuthOkOut)
def revoke_pat(
    token_id: str,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    row = session.scalar(
        select(PersonalAccessToken).where(
            PersonalAccessToken.id == token_id,
            PersonalAccessToken.user_id == user.id,
        )
    )
    if row is None:
        raise HTTPException(404, "个人访问令牌不存在")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        session.flush()
        audit.record(session, user, "pat.revoke", "personal_access_token", row.id, token_id=row.id)
    return {"ok": True}


# --- 公开认证配置、标准 OIDC 与反代 Forward Auth -------------------------------------


@router.get("/auth/config", response_model=S.PublicAuthConfigOut)
def public_auth_config(session: Session = Depends(get_session)) -> dict:
    return auth_config.public_auth_config(session)


@router.get("/auth/oidc/login")
async def oidc_login(
    request: Request,
    next: str | None = None,
    remember_me: bool = False,
    session: Session = Depends(get_session),
):
    cfg = auth_config.get_auth_config(session)
    oidc_cfg = cfg["oidc"]
    if not oidc_cfg.get("enabled") or not oidc_cfg.get("issuer_url") or not oidc_cfg.get("client_id"):
        raise HTTPException(404, "未启用 OIDC 登录")
    try:
        discovery = await auth_config.fetch_oidc_discovery(oidc_cfg["issuer_url"])
    except Exception as exc:
        raise HTTPException(502, "OIDC 发现文档获取失败") from exc

    settings = get_settings()
    base = auth_config.external_public_base(settings, request)
    redirect_uri = f"{base}/api/auth/oidc/callback"
    code_verifier = generate_token(64)
    code_challenge = create_s256_code_challenge(code_verifier)
    state = generate_token(32)
    nonce = generate_token(32)
    next_url = auth_config.sanitize_next_url(next)

    tx_payload = encrypt_secret(
        json.dumps(
            {
                "state": state,
                "nonce": nonce,
                "code_verifier": code_verifier,
                "next_url": next_url,
                "remember_me": bool(remember_me),
                "issuer": discovery["issuer"],
                "exp": int(time.time()) + 600,
            }
        )
    )
    auth_url = auth_config.build_oidc_authorize_url(
        discovery,
        client_id=oidc_cfg["client_id"],
        redirect_uri=redirect_uri,
        scopes=oidc_cfg.get("scopes") or "openid profile email",
        state=state,
        nonce=nonce,
        code_challenge=code_challenge,
    )
    resp = RedirectResponse(auth_url, status_code=302)
    resp.set_cookie(
        key=_OIDC_TX_COOKIE,
        value=tx_payload,
        httponly=True,
        samesite="lax",
        secure=_is_secure_cookie(request, settings),
        path="/api/auth/oidc",
        max_age=600,
    )
    return resp


@router.get("/auth/oidc/callback")
async def oidc_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: Session = Depends(get_session),
):
    settings = get_settings()
    secure = _is_secure_cookie(request, settings)

    def _fail_redirect(reason: str) -> RedirectResponse:
        resp = RedirectResponse(f"/login?error={reason}", status_code=302)
        resp.delete_cookie(key=_OIDC_TX_COOKIE, path="/api/auth/oidc", httponly=True, samesite="lax", secure=secure)
        return resp

    if error or not code or not state:
        audit.record(session, None, "auth.oidc_failed", "user", None, reason=error or "missing_code")
        session.commit()
        return _fail_redirect("oidc_failed")

    raw_tx = request.cookies.get(_OIDC_TX_COOKIE)
    if not raw_tx:
        return _fail_redirect("oidc_state_invalid")
    try:
        tx = json.loads(decrypt_secret(raw_tx))
    except Exception:
        return _fail_redirect("oidc_state_invalid")

    if int(tx.get("exp", 0)) <= int(time.time()) or not hmac.compare_digest(str(tx.get("state", "")), state):
        return _fail_redirect("oidc_state_invalid")
    remaining_ttl = max(int(tx.get("exp", 0)) - int(time.time()), 1)
    if not cache.try_set_once("oidc-state-used", state, remaining_ttl):
        return _fail_redirect("oidc_state_invalid")

    cfg = auth_config.get_auth_config(session)
    oidc_cfg = cfg["oidc"]
    if not oidc_cfg.get("enabled") or str(oidc_cfg.get("issuer_url", "")).rstrip("/") != str(
        tx.get("issuer", "")
    ).rstrip("/"):
        return _fail_redirect("oidc_disabled")

    base = auth_config.external_public_base(settings, request)
    redirect_uri = f"{base}/api/auth/oidc/callback"
    try:
        claims = await auth_config.verify_oidc_callback(
            oidc_cfg=oidc_cfg,
            code=code,
            redirect_uri=redirect_uri,
            expected_nonce=str(tx["nonce"]),
            code_verifier=str(tx["code_verifier"]),
        )
    except Exception:
        audit.record(session, None, "auth.oidc_failed", "user", None, reason="verification_failed")
        session.commit()
        return _fail_redirect("oidc_failed")

    user, err = auth_config.resolve_or_create_external_user(
        session,
        provider="oidc",
        issuer=str(claims["iss"]),
        subject=str(claims["sub"]),
        preferred_username=claims.get("preferred_username"),
        display_name=claims.get("name"),
        email=claims.get("email"),
        auto_create=bool(oidc_cfg.get("auto_create_user", True)),
        default_role=str(oidc_cfg.get("default_role") or "analyst"),
    )
    if user is None:
        audit.record(session, None, "auth.oidc_failed", "user", None, reason=err or "user_rejected")
        session.commit()
        return _fail_redirect(err or "oidc_failed")

    audit.record(session, user, "auth.login", "user", user.id, via="oidc")
    session.commit()

    next_url = auth_config.sanitize_next_url(tx.get("next_url"))
    resp = RedirectResponse(next_url, status_code=302)
    resp.delete_cookie(key=_OIDC_TX_COOKIE, path="/api/auth/oidc", httponly=True, samesite="lax", secure=secure)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    payload = _issue(user, auth_time=now_ts)
    _set_auth_cookies(
        resp,
        request,
        user,
        auth_time=now_ts,
        remember_me=bool(tx.get("remember_me")),
        access_expires_in=payload["expires_in"],
        access_token=payload["access_token"],
    )
    return resp


@router.get("/auth/forward", response_model=TokenOut)
def forward_auth(
    request: Request,
    response: Response,
    next: str | None = None,
    remember_me: bool = False,
    session: Session = Depends(get_session),
):
    cfg = auth_config.get_auth_config(session)
    if not cfg.get("forward_auth_enabled"):
        raise HTTPException(404, "未启用反代认证")
    auth_config.verify_forward_auth_trust(request, cfg, get_settings())
    raw_user = (request.headers.get(cfg["forward_auth_header"]) or "").strip()
    if not raw_user:
        raise HTTPException(401, "缺少反代用户身份请求头")
    norm_sub = normalize_username(raw_user)
    user, err = auth_config.resolve_or_create_external_user(
        session,
        provider="forward_auth",
        issuer="forward_auth",
        subject=norm_sub,
        preferred_username=norm_sub,
        display_name=raw_user,
        email=None,
        auto_create=bool(cfg.get("forward_auth_auto_create", True)),
        default_role="analyst",
    )
    if user is None:
        raise HTTPException(403 if err == "account_disabled" else 401, "用户不存在或已停用")
    audit.record(session, user, "auth.login", "user", user.id, via="forward_auth")
    now_ts = int(datetime.now(timezone.utc).timestamp())
    payload = _issue(user, auth_time=now_ts)
    if next is not None:
        resp = RedirectResponse(auth_config.sanitize_next_url(next), status_code=302)
        _set_auth_cookies(
            resp,
            request,
            user,
            auth_time=now_ts,
            remember_me=bool(remember_me),
            access_expires_in=payload["expires_in"],
            access_token=payload["access_token"],
        )
        return resp
    _set_auth_cookies(
        response,
        request,
        user,
        auth_time=now_ts,
        remember_me=bool(remember_me),
        access_expires_in=payload["expires_in"],
        access_token=payload["access_token"],
    )
    return payload


@router.get("/settings")
def settings_view(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    s = get_settings()
    return {
        "business_timezone": s.business_timezone,
        "number_style": s.number_style,
        "allow_self_review": s.allow_self_review,
        "self_review_policy": "允许自审" if s.allow_self_review else "禁止自审：提交人、创建人不能批准同一版本",
        "unassigned_asset_policy": s.unassigned_asset_policy,
        "query_limits": {
            "timeout_seconds": s.query_timeout_seconds,
            "mem_limit_bytes": s.query_mem_limit_bytes,
            "max_rows": s.query_max_rows,
        },
        "answer_cache_seconds": s.answer_cache_seconds,
        "collect_interval_minutes": s.collect_interval_minutes,
        "model": {**modelcfg.settings_view(session), "orchestrator": "langgraph"},
        "roles": {"admin": "管理员", "steward": "维护者", "reviewer": "复核者", "analyst": "查询者"},
        "discovery_note": gettext("元数据按轮询采集，最长延迟为一个采集间隔：未发现变更不代表没有变更。"),
    }
