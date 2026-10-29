"""认证、当前用户、用户管理、运行设置。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis import audit, cache
from avelis.api.deps import current_claims, current_user, get_session
from avelis.ask.intent import get_model
from avelis.config import get_settings
from avelis.governance import access
from avelis.models import Domain, DomainMember, User, UserRole
from avelis.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api", tags=["认证与用户"])


@router.post("/auth/token")
def login(form: OAuth2PasswordRequestForm = Depends(), session: Session = Depends(get_session)) -> dict:
    user = session.scalar(select(User).where(User.username == form.username))
    if user is None or not user.is_active or not verify_password(form.password, user.password_hash):
        audit.record(session, None, "auth.login_failed", "user", user.id if user else None, username=form.username)
        session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或口令错误",
                            headers={"WWW-Authenticate": "Bearer"})
    token, expires, _ = create_access_token(user.id, user.username)
    audit.record(session, user, "auth.login", "user", user.id)
    return {"access_token": token, "token_type": "bearer", "expires_at": expires.isoformat()}


@router.post("/auth/logout")
def logout(claims: dict = Depends(current_claims), user: User = Depends(current_user),
           session: Session = Depends(get_session)) -> dict:
    ttl = int(claims["exp"] - datetime.now(timezone.utc).timestamp())
    cache.revoke_token(claims["jti"], ttl)
    audit.record(session, user, "auth.logout", "user", user.id)
    return {"ok": True}


def user_view(session: Session, user: User) -> dict:
    memberships = session.execute(select(DomainMember.domain_id, DomainMember.role, Domain.name, Domain.code)
                                  .join(Domain, Domain.id == DomainMember.domain_id)
                                  .where(DomainMember.user_id == user.id))
    return {"id": user.id, "username": user.username, "display_name": user.display_name,
            "is_active": user.is_active, "roles": sorted(access.roles_of(session, user)),
            "domains": [{"id": d, "role": r, "name": n, "code": c} for d, r, n, c in memberships]}


@router.get("/me")
def me(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return user_view(session, user)


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=128)
    roles: list[str] = Field(default_factory=list)


@router.get("/users")
def list_users(user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    access.require_role(session, user, "steward", "reviewer")
    return [user_view(session, u) for u in session.scalars(select(User).order_by(User.username))]


@router.post("/users", status_code=201)
def create_user(body: UserCreate, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    access.require_role(session, user, "admin")
    if session.scalar(select(User.id).where(User.username == body.username)):
        raise HTTPException(409, f"用户名 {body.username} 已存在")
    unknown = set(body.roles) - set(access.ROLES)
    if unknown:
        raise HTTPException(422, f"未知角色：{', '.join(sorted(unknown))}")
    new = User(username=body.username, display_name=body.display_name, password_hash=hash_password(body.password))
    session.add(new)
    session.flush()
    for role in sorted(set(body.roles)):
        session.add(UserRole(user_id=new.id, role=role))
    session.flush()
    audit.record(session, user, "user.create", "user", new.id, username=new.username, roles=sorted(body.roles))
    return user_view(session, new)


@router.get("/settings")
def settings_view(user: User = Depends(current_user)) -> dict:
    s = get_settings()
    model = get_model()
    return {
        "business_timezone": s.business_timezone,
        "allow_self_review": s.allow_self_review,
        "self_review_policy": "允许自审" if s.allow_self_review else "禁止自审：提交人、创建人不能批准同一版本",
        "meta_backend": s.meta_backend,
        "query_limits": {"timeout_seconds": s.query_timeout_seconds, "mem_limit_bytes": s.query_mem_limit_bytes,
                         "max_rows": s.query_max_rows},
        "answer_cache_seconds": s.answer_cache_seconds,
        "collect_interval_minutes": s.collect_interval_minutes,
        "model": {"adapter": model.name, "is_demo": model.is_demo,
                  "status": "演示模型；真实模型端到端未验证" if model.is_demo else "已配置"},
        "roles": {"admin": "管理员", "steward": "维护者", "reviewer": "复核者", "analyst": "查询者"},
        "discovery_note": "元数据按轮询采集，最长延迟为一个采集间隔：未发现变更不代表没有变更。",
    }
