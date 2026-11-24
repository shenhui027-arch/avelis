"""M4C 用户账号匿名化注销：吊销一切凭据并抹除个人身份标识，同时完整保留历史哈希链与审计不可变性。"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from avelis import audit
from avelis.db import utcnow
from avelis.models import OAuthToken, PersonalAccessToken, User, UserRole
from avelis.security import normalize_username, session_cutoff_now


def anonymize_user(session: Session, target_id_or_username: str, *, actor: User | None) -> dict[str, Any]:
    """匿名化停用目标用户，不修改或删除任何 Chained 历史表行。"""
    key = (target_id_or_username or "").strip()
    if not key:
        raise ValueError("必须指定要匿名化的用户编号或用户名")
    user = session.scalar(select(User).where(or_(User.id == key, func.lower(User.username) == normalize_username(key))))
    if user is None:
        raise LookupError(f"用户不存在：{key}")
    if actor is not None and actor.id == user.id:
        raise ValueError("不能匿名化当前登录账号")
    if user.username.startswith("anonymized_") and not user.is_active:
        raise ValueError("该用户已完成匿名化注销")

    is_target_admin = (
        session.scalar(select(UserRole.id).where(UserRole.user_id == user.id, UserRole.role == "admin")) is not None
    )
    if is_target_admin and user.is_active:
        other_active_admins = (
            session.scalar(
                select(func.count())
                .select_from(UserRole)
                .join(User, User.id == UserRole.user_id)
                .where(UserRole.role == "admin", User.is_active.is_(True), User.id != user.id)
            )
            or 0
        )
        if other_active_admins == 0:
            raise ValueError("不能匿名化系统最后一个活跃管理员")

    now = utcnow()
    short_hash = hashlib.sha256(f"{user.id}:anonymized".encode("utf-8")).hexdigest()[:12]
    anon_username = f"anonymized_{short_hash}"
    anon_display = f"已匿名用户 ({short_hash})"

    revoked_pats = 0
    for pat in session.scalars(select(PersonalAccessToken).where(PersonalAccessToken.user_id == user.id)):
        if pat.revoked_at is None:
            pat.revoked_at = now
            revoked_pats += 1

    revoked_oauth = 0
    for tok in session.scalars(select(OAuthToken).where(OAuthToken.user_id == user.id)):
        changed = False
        if tok.access_revoked_at is None:
            tok.access_revoked_at = now
            changed = True
        if tok.refresh_revoked_at is None:
            tok.refresh_revoked_at = now
            changed = True
        if changed:
            tok.revocation_reason = "user_anonymized"
            revoked_oauth += 1

    user.is_active = False
    user.must_change_password = False
    user.password_hash = "!"
    user.sessions_valid_after = session_cutoff_now()
    user.totp_secret_enc = None
    user.totp_pending_secret_enc = None
    user.totp_enabled = False
    user.totp_enabled_at = None
    user.totp_last_step = None
    user.external_issuer = None
    user.external_sub = None
    user.email = None
    user.username = anon_username
    user.display_name = anon_display
    session.flush()

    audit.record(
        session,
        actor,
        "user.anonymized",
        "user",
        user.id,
        anonymized_username=anon_username,
        revoked_pats=revoked_pats,
        revoked_oauth_tokens=revoked_oauth,
    )
    return {
        "user_id": user.id,
        "anonymized_username": anon_username,
        "display_name": anon_display,
        "revoked_pats": revoked_pats,
        "revoked_oauth_tokens": revoked_oauth,
    }
