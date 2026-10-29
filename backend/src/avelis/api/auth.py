"""认证、当前用户、用户管理、运行设置。"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis import audit, cache, modelcfg
from avelis.api.deps import current_claims, current_user, get_session
from avelis.config import get_settings
from avelis.db import utcnow
from avelis.governance import access
from avelis.models import Domain, DomainMember, User, UserRole
from avelis.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api", tags=["认证与用户"])


class TokenOut(BaseModel):
    access_token: str
    token_type: str
    expires_at: str
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
    is_active: bool
    roles: list[str]
    domains: list[DomainRef]
    must_change_password: bool = False
    last_login_at: str | None = None
    created_at: str | None = None


def _issue(user: User) -> dict:
    token, expires, _ = create_access_token(user.id, user.username)
    return {"access_token": token, "token_type": "bearer", "expires_at": expires.isoformat(),
            "must_change_password": user.must_change_password}


def _end_other_sessions(user: User) -> None:
    """此刻之前签发的令牌全部失效（令牌的 iat 精确到秒，取整到秒，保证随后签发的新令牌有效）。"""
    user.sessions_valid_after = datetime.now(timezone.utc).replace(microsecond=0)


@router.post("/auth/token", response_model=TokenOut)
def login(request: Request, form: OAuth2PasswordRequestForm = Depends(),
          session: Session = Depends(get_session)) -> dict:
    """失败按“用户名 + 来源地址”计数，连续失败达到上限后锁定一段时间（AVELIS_LOGIN_MAX_FAILURES / _LOCK_MINUTES）。
    锁定期间即使口令正确也拒绝，且不透露账号是否存在。"""
    settings = get_settings()
    key = f"{form.username.lower()}|{request.client.host if request.client else '-'}"
    locked = cache.login_locked_seconds(key, settings.login_max_failures)
    if locked:
        audit.record(session, None, "auth.login_locked", "user", None, username=form.username)
        session.commit()
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, headers={"Retry-After": str(locked)},
                            detail=f"连续登录失败次数过多，请 {max(locked // 60, 1)} 分钟后再试")
    user = session.scalar(select(User).where(User.username == form.username))
    if user is None or not user.is_active or not verify_password(form.password, user.password_hash):
        failures = cache.record_login_failure(key, settings.login_lock_minutes * 60)
        audit.record(session, None, "auth.login_failed", "user", user.id if user else None, username=form.username,
                     failures=failures)
        session.commit()
        left = settings.login_max_failures - failures
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"},
                            detail="用户名或口令错误" + (f"，再失败 {left} 次将暂时锁定" if 0 < left <= 2 else ""))
    cache.clear_login_failures(key)
    user.last_login_at = utcnow()
    audit.record(session, user, "auth.login", "user", user.id)
    return _issue(user)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128, repr=False)
    new_password: str = Field(min_length=8, max_length=128, repr=False)


def _check_password_policy(user: User, password: str) -> None:
    if password.lower() == user.username.lower():
        raise HTTPException(422, "口令不能与用户名相同")
    if len(set(password)) < 4:
        raise HTTPException(422, "口令过于简单")
    if verify_password(password, user.password_hash):
        raise HTTPException(422, "新口令不能与当前口令相同")


@router.post("/me/password", response_model=TokenOut)
def change_password(body: PasswordChange, user: User = Depends(current_user),
                    session: Session = Depends(get_session)) -> dict:
    """修改自己的口令：其他会话全部失效，返回当前会话的新令牌。"""
    if not verify_password(body.current_password, user.password_hash):
        audit.record(session, user, "auth.password_change_failed", "user", user.id)
        session.commit()
        raise HTTPException(403, "当前口令不正确")
    _check_password_policy(user, body.new_password)
    user.password_hash = hash_password(body.new_password)
    user.password_changed_at = utcnow()
    user.must_change_password = False
    _end_other_sessions(user)
    audit.record(session, user, "auth.password_changed", "user", user.id)
    return _issue(user)


@router.post("/auth/logout")
def logout(claims: dict = Depends(current_claims), user: User = Depends(current_user),
           session: Session = Depends(get_session)) -> dict:
    ttl = math.ceil(claims["exp"] - datetime.now(timezone.utc).timestamp())  # 向上取整：吊销记录不早于令牌过期
    cache.revoke_token(claims["jti"], ttl)
    audit.record(session, user, "auth.logout", "user", user.id)
    return {"ok": True}


def user_view(session: Session, user: User) -> dict:
    memberships = session.execute(select(DomainMember.domain_id, DomainMember.role, Domain.name, Domain.code)
                                  .join(Domain, Domain.id == DomainMember.domain_id)
                                  .where(DomainMember.user_id == user.id))
    return {"id": user.id, "username": user.username, "display_name": user.display_name,
            "is_active": user.is_active, "roles": sorted(access.roles_of(session, user)),
            "domains": [{"id": d, "role": r, "name": n, "code": c} for d, r, n, c in memberships],
            "must_change_password": user.must_change_password,
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
            "created_at": user.created_at.isoformat() if user.created_at else None}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return user_view(session, user)


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=128, repr=False)
    roles: list[str] = Field(default_factory=list)
    must_change_password: bool = Field(default=True, description="首次登录必须修改口令（默认是）")


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    is_active: bool | None = None
    roles: list[str] | None = None


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=8, max_length=128, repr=False)


@router.get("/users", response_model=list[UserOut])
def list_users(user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    access.require_role(session, user, "steward", "reviewer")
    return [user_view(session, u) for u in session.scalars(select(User).order_by(User.username))]


@router.post("/users", status_code=201, response_model=UserOut)
def create_user(body: UserCreate, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    access.require_role(session, user, "admin")
    if session.scalar(select(User.id).where(User.username == body.username)):
        raise HTTPException(409, f"用户名 {body.username} 已存在")
    unknown = set(body.roles) - set(access.ROLES)
    if unknown:
        raise HTTPException(422, f"未知角色：{', '.join(sorted(unknown))}")
    new = User(username=body.username, display_name=body.display_name, password_hash=hash_password(body.password),
               must_change_password=body.must_change_password)
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
def update_user(user_id: str, body: UserUpdate, user: User = Depends(current_user),
                session: Session = Depends(get_session)) -> dict:
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
            raise HTTPException(422, f"未知角色：{', '.join(sorted(unknown))}")
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
def reset_password(user_id: str, body: PasswordReset, user: User = Depends(current_user),
                   session: Session = Depends(get_session)) -> dict:
    """管理员重置口令：该用户的会话全部失效，下次登录必须修改口令。"""
    access.require_role(session, user, "admin")
    target = _user_or_404(session, user_id)
    target.password_hash = hash_password(body.new_password)
    target.password_changed_at = utcnow()
    target.must_change_password = True
    _end_other_sessions(target)
    audit.record(session, user, "user.password_reset", "user", target.id)
    return user_view(session, target)


@router.get("/settings")
def settings_view(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    s = get_settings()
    return {
        "business_timezone": s.business_timezone,
        "allow_self_review": s.allow_self_review,
        "self_review_policy": "允许自审" if s.allow_self_review else "禁止自审：提交人、创建人不能批准同一版本",
        "unassigned_asset_policy": s.unassigned_asset_policy,
        "query_limits": {"timeout_seconds": s.query_timeout_seconds, "mem_limit_bytes": s.query_mem_limit_bytes,
                         "max_rows": s.query_max_rows},
        "answer_cache_seconds": s.answer_cache_seconds,
        "collect_interval_minutes": s.collect_interval_minutes,
        "model": {**modelcfg.settings_view(session), "orchestrator": "langgraph"},
        "roles": {"admin": "管理员", "steward": "维护者", "reviewer": "复核者", "analyst": "查询者"},
        "discovery_note": "元数据按轮询采集，最长延迟为一个采集间隔：未发现变更不代表没有变更。",
    }
