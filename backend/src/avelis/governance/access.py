"""最小角色模型与后端权限检查。

全局角色：admin 管理员；steward 维护者（建数据源、资产归属、指标草稿、关联路径）；
reviewer 复核者（批准 / 驳回指标版本）；analyst 查询者（问数）。
业务域成员（owner / maintainer / viewer）决定能检索、查询哪个域的资产和口径。
自审策略：默认禁止（提交人、创建人不能批准同一版本），由 AVELIS_ALLOW_SELF_REVIEW 控制并在 /api/settings 可见。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis.governance.errors import Forbidden
from avelis.models import Asset, Domain, DomainMember, User, UserRole

ROLES = ("admin", "steward", "reviewer", "analyst")


def roles_of(session: Session, user: User) -> set[str]:
    return set(session.scalars(select(UserRole.role).where(UserRole.user_id == user.id)))


def is_admin(session: Session, user: User) -> bool:
    return "admin" in roles_of(session, user)


def require_role(session: Session, user: User, *roles: str) -> None:
    have = roles_of(session, user)
    if "admin" in have or have & set(roles):
        return
    raise Forbidden(f"需要角色：{' / '.join(roles)}", required=list(roles))


def domain_ids(session: Session, user: User) -> set[str]:
    """用户可检索、可查询的业务域。管理员可见全部。"""
    if is_admin(session, user):
        return set(session.scalars(select(Domain.id)))
    return set(session.scalars(select(DomainMember.domain_id).where(DomainMember.user_id == user.id)))


def domain_role(session: Session, user: User, domain_id: str) -> str | None:
    return session.scalar(select(DomainMember.role).where(DomainMember.domain_id == domain_id,
                                                          DomainMember.user_id == user.id))


def require_domain_maintainer(session: Session, user: User, domain_id: str) -> None:
    if is_admin(session, user):
        return
    if domain_role(session, user, domain_id) in ("owner", "maintainer"):
        return
    raise Forbidden("需要该业务域的负责人或维护者身份", domain_id=domain_id)


def require_domain_owner(session: Session, user: User, domain_id: str) -> None:
    domain = session.get(Domain, domain_id)
    if domain is not None and (domain.owner_id == user.id or is_admin(session, user)):
        return
    raise Forbidden("需要该业务域负责人", domain_id=domain_id)


def can_read_asset(session: Session, user: User, asset: Asset, allowed: set[str] | None = None) -> bool:
    """未归属业务域的资产不做域限制（v0.1）；归属了业务域的资产只对该域成员可见。"""
    if asset.domain_id is None:
        return True
    allowed = domain_ids(session, user) if allowed is None else allowed
    return asset.domain_id in allowed
