"""技能市场查询、权限校验与状态转换。"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import case, func, select, true
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from avelis import audit
from avelis.config import get_settings
from avelis.db import new_id, utcnow
from avelis.governance import access
from avelis.governance.errors import Conflict, Forbidden, GovernanceError, Invalid, NotFound
from avelis.i18n import Msg, translate
from avelis.models import Skill, SkillEnablement, SkillVersion, User
from avelis.skills import storage
from avelis.skills.package import _NAME, _path_parts, validate_zip

log = logging.getLogger(__name__)


def _user_ref(user: User) -> dict:
    return {"id": user.id, "display_name": user.display_name}


def _iso(value: datetime) -> str:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def _version_view(skill: Skill, version: SkillVersion, users: dict[str, User]) -> dict:
    return {
        "id": version.id,
        "skill_id": skill.id,
        "name": skill.name,
        "version": version.version,
        "status": version.status,
        "description": version.description,
        "frontmatter": version.frontmatter,
        "note": version.note,
        "files": version.files,
        "has_scripts": version.has_scripts,
        "line_count": version.line_count,
        "zip_bytes": version.zip_bytes,
        "uploaded_by": _user_ref(users[version.uploaded_by]),
        "uploaded_at": _iso(version.uploaded_at),
        "reviewed_by": _user_ref(users[version.reviewed_by]) if version.reviewed_by else None,
        "reviewed_at": _iso(version.reviewed_at) if version.reviewed_at else None,
        "review_note": version.review_note,
    }


def _version_views(session: Session, skill: Skill, versions: list[SkillVersion]) -> list[dict]:
    ids = {
        user_id for version in versions for user_id in (version.uploaded_by, version.reviewed_by) if user_id is not None
    }
    users = {user.id: user for user in session.scalars(select(User).where(User.id.in_(ids)))}
    return [_version_view(skill, version, users) for version in versions]


def _skill(session: Session, skill_id: str, *, lock: bool = False) -> Skill:
    if "\x00" in skill_id:
        raise NotFound(Msg("技能不存在"))
    query = select(Skill).where(Skill.id == skill_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    skill = session.scalar(query)
    if skill is None:
        raise NotFound(Msg("技能不存在"))
    return skill


def _visible(skill: Skill, actor: User, admin: bool) -> bool:
    can_manage = admin or skill.owner_id == actor.id
    if skill.status != "listed" and not can_manage:
        raise NotFound(Msg("技能不存在"))
    return can_manage


def _enabled(actor: User):
    return (
        select(SkillEnablement.skill_id)
        .where(SkillEnablement.skill_id == Skill.id, SkillEnablement.user_id == actor.id)
        .exists()
    )


def _pending():
    return select(SkillVersion.id).where(SkillVersion.skill_id == Skill.id, SkillVersion.status == "pending").exists()


def summary(session: Session, actor: User) -> dict:
    listed = Skill.status == "listed"
    admin = access.is_admin(session, actor)
    row = session.execute(
        select(
            func.count().filter(listed),
            func.count().filter(listed, _enabled(actor)),
            func.count().filter(Skill.owner_id == actor.id),
            func.count().filter(_pending()),
        ).select_from(Skill)
    ).one()
    return {"market": row[0], "enabled": row[1], "mine": row[2], "review": row[3] if admin else None}


def _latest_number():
    return (
        select(func.max(SkillVersion.version))
        .where(SkillVersion.skill_id == Skill.id)
        .correlate(Skill)
        .scalar_subquery()
    )


def _skill_views(session: Session, actor: User, admin: bool, condition, q: str = "") -> list[dict]:
    latest = aliased(SkillVersion)
    published = aliased(SkillVersion)
    uploader = aliased(User)
    can_manage = true() if admin else Skill.owner_id == actor.id
    description = func.coalesce(published.description, latest.description)
    query = (
        select(
            Skill,
            User,
            uploader,
            can_manage.label("can_manage"),
            case((can_manage, latest.version), else_=published.version).label("latest_version"),
            case((can_manage, latest.status), else_=published.status).label("latest_status"),
            case((can_manage, latest.id), else_=published.id).label("latest_version_id"),
            case((can_manage, latest.note), else_=published.note).label("latest_note"),
            case((can_manage, latest.has_scripts), else_=published.has_scripts).label("latest_has_scripts"),
            case((can_manage & (latest.status == "rejected"), latest.review_note)).label("review_note"),
            description.label("description"),
            func.coalesce(published.has_scripts, latest.has_scripts).label("has_scripts"),
            func.jsonb_array_length(func.coalesce(published.files, latest.files)).label("file_count"),
            _enabled(actor).label("enabled"),
        )
        .select_from(Skill)
        .join(User, User.id == Skill.owner_id)
        .join(latest, (latest.skill_id == Skill.id) & (latest.version == _latest_number()))
        .outerjoin(published, (published.skill_id == Skill.id) & (published.version == Skill.published_version))
        .join(uploader, uploader.id == case((can_manage, latest.uploaded_by), else_=published.uploaded_by))
        .where(condition)
        .order_by(Skill.name)
    )
    if q:
        query = query.where(Skill.name.icontains(q, autoescape=True) | description.icontains(q, autoescape=True))
    rows = session.execute(query).all()
    if not rows:
        return []
    ids = [skill.id for skill, *_ in rows]
    counts = dict(
        session.execute(
            select(SkillEnablement.skill_id, func.count())
            .where(SkillEnablement.skill_id.in_(ids))
            .group_by(SkillEnablement.skill_id)
        ).all()
    )
    views = []
    for row in rows:
        skill, owner, submitter = row[:3]
        views.append(
            {
                "id": skill.id,
                "name": skill.name,
                "description": row.description,
                "status": skill.status,
                "owner": _user_ref(owner),
                "version": skill.published_version,
                "latest_version": row.latest_version,
                "latest_status": row.latest_status,
                "latest_version_id": row.latest_version_id,
                "latest_note": row.latest_note,
                "latest_uploaded_by": _user_ref(submitter),
                "latest_has_scripts": row.latest_has_scripts,
                "review_note": row.review_note,
                "enabled": row.enabled,
                "enabled_users": counts.get(skill.id, 0),
                "has_scripts": row.has_scripts,
                "file_count": row.file_count,
                "can_manage": row.can_manage,
                "updated_at": _iso(skill.updated_at),
            }
        )
    return views


def list_skills(session: Session, actor: User, view: str, q: str = "") -> list[dict]:
    admin = access.is_admin(session, actor)
    if view == "review" and not admin:
        raise Forbidden(Msg("只有管理员可以审核技能"))
    conditions = {
        "market": Skill.status == "listed",
        "enabled": (Skill.status == "listed") & _enabled(actor),
        "mine": Skill.owner_id == actor.id,
        "review": _pending(),
    }
    if view not in conditions or "\x00" in q:
        raise Invalid(Msg("技能请求参数无效"))
    return _skill_views(session, actor, admin, conditions[view], q)


def _detail(session: Session, actor: User, skill: Skill, admin: bool, *, body: str | None = None) -> dict:
    can_manage = _visible(skill, actor, admin)
    query = select(SkillVersion).where(SkillVersion.skill_id == skill.id).order_by(SkillVersion.version.desc())
    if not can_manage:
        query = query.where(SkillVersion.status.in_(("approved", "superseded")))
    versions = _version_views(session, skill, list(session.scalars(query)))
    if not versions:
        raise NotFound(Msg("技能版本不存在"))
    shown = next((version for version in versions if version["version"] == skill.published_version), versions[0])
    if body is None:
        body = storage.skill_md_body(skill.name, shown["version"])
    view = _skill_views(session, actor, admin, Skill.id == skill.id)[0]
    return {**view, "versions": versions, "shown_version": shown, "body": body}


def detail(session: Session, actor: User, skill_id: str) -> dict:
    return _detail(session, actor, _skill(session, skill_id), access.is_admin(session, actor))


def _visible_version(session: Session, actor: User, skill_id: str, number: int) -> tuple[Skill, SkillVersion]:
    skill = _skill(session, skill_id)
    can_manage = _visible(skill, actor, access.is_admin(session, actor))
    if not can_manage and number != skill.published_version:
        raise NotFound(Msg("技能版本不存在"))
    version = session.scalar(
        select(SkillVersion).where(SkillVersion.skill_id == skill.id, SkillVersion.version == number)
    )
    if version is None or (not can_manage and version.status != "approved"):
        raise NotFound(Msg("技能版本不存在"))
    return skill, version


def file_content(session: Session, actor: User, skill_id: str, version: int, path: str) -> dict:
    skill, item = _visible_version(session, actor, skill_id, version)
    path = "/".join(_path_parts(path))
    file = next((file for file in item.files if file["path"] == path), None)
    if file is None:
        raise NotFound(Msg("技能文件不可用：{name}", name=skill.name))
    content, size, truncated, binary = storage.read_file(skill.name, version, path, 256 * 1024)
    return {**file, "size": size, "content": content, "truncated": truncated, "binary": binary}


def download_path(session: Session, actor: User, skill_id: str, version: int) -> tuple[Path, str]:
    skill, _ = _visible_version(session, actor, skill_id, version)
    return storage.zip_path(skill.name, version), f"{skill.name}-v{version}.zip"


def validate(session: Session, actor: User, data: bytes) -> dict:
    result = validate_zip(data)
    conflict = None
    next_version = 1
    if result.name is not None:
        row = session.execute(
            select(Skill, User, _latest_number(), _pending())
            .select_from(Skill)
            .join(User, User.id == Skill.owner_id)
            .where(Skill.name == result.name)
        ).one_or_none()
        if row is not None:
            skill, owner, latest, pending = row
            next_version = (latest or 0) + 1
            conflict = {
                "skill_id": skill.id,
                "name": skill.name,
                "version": skill.published_version,
                "status": skill.status,
                "owner": _user_ref(owner),
                "enabled_users": session.scalar(
                    select(func.count()).select_from(SkillEnablement).where(SkillEnablement.skill_id == skill.id)
                ),
                "can_overwrite": actor.id == owner.id or access.is_admin(session, actor),
                "has_pending": pending,
            }
    return {
        "ok": result.ok,
        "name": result.name,
        "description": result.description,
        "files": result.files,
        "has_scripts": result.has_scripts,
        "line_count": result.line_count,
        "zip_bytes": result.zip_bytes,
        "checks": [{**check, "message": translate(check["message"])} for check in result.checks],
        "conflict": conflict,
        "next_version": next_version,
    }


def _name_conflict(name: str) -> Conflict:
    return Conflict(Msg("已存在同名技能 {name}，请修改名称后重新上传，或选择覆盖", name=name))


def _lock_upload_name(session: Session, name: str) -> None:
    session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"skills:{name}", 0))))


def discard_failed_upload(session: Session, name: str, version: int) -> None:
    """失败后重新锁定名称，避免删除另一次重传已经提交的版本。"""
    try:
        session.rollback()
        with session.begin():
            _lock_upload_name(session, name)
            exists = session.scalar(
                select(SkillVersion.id)
                .join(Skill, Skill.id == SkillVersion.skill_id)
                .where(Skill.name == name, SkillVersion.version == version)
            )
            if exists is None:
                storage.discard_version(name, version)
    except storage.SkillStorageError:
        pass  # 下一次上传可重试清理，存储边界已写日志。
    except SQLAlchemyError:
        log.exception("技能上传失败后无法核对孤立版本：%s v%s，存储目录：%s", name, version, get_settings().skills_dir)


def upload(session: Session, actor: User, data: bytes, mode: str = "new", note: str | None = None) -> dict:
    result = validate_zip(data)
    if not result.ok:
        raise Invalid(next(check["message"] for check in result.checks if check["level"] == "error"))
    if mode not in ("new", "overwrite") or (note is not None and (len(note) > 500 or "\x00" in note)):
        raise Invalid(Msg("技能请求参数无效"))

    name = result.name
    assert name is not None and result.description is not None and result.line_count is not None
    # 同名上传和失败清理共用名称锁，覆盖另锁技能行以协调审核操作。
    _lock_upload_name(session, name)
    skill = session.scalar(select(Skill).where(Skill.name == name).with_for_update())
    creating = skill is None
    now = utcnow()
    if creating:
        if mode == "overwrite":
            raise Invalid(Msg("不存在可覆盖的同名技能"))
        skill = Skill(id=new_id(), name=name, owner_id=actor.id, status="pending", created_at=now, updated_at=now)
        number = 1
    else:
        if mode == "new":
            raise _name_conflict(name)
        if skill.owner_id != actor.id and not access.is_admin(session, actor):
            raise Forbidden(Msg("只有技能上传人或管理员可以覆盖技能"))
        if session.scalar(select(_pending()).where(Skill.id == skill.id)):
            raise Conflict(Msg("该技能已有待审核的版本"))
        number = (session.scalar(select(_latest_number()).where(Skill.id == skill.id)) or 0) + 1

    version = SkillVersion(
        id=new_id(),
        skill_id=skill.id,
        version=number,
        status="pending",
        description=result.description,
        frontmatter=result.frontmatter,
        files=result.files,
        has_scripts=result.has_scripts,
        line_count=result.line_count,
        zip_sha256=hashlib.sha256(data).hexdigest(),
        zip_bytes=result.zip_bytes,
        note=note,
        uploaded_by=actor.id,
        uploaded_at=now,
    )
    # 锁内确定的下一版本号在数据库中尚不存在。
    storage.write_version(name, number, data, result, replace_orphan=True)
    try:
        if creating:
            session.add(skill)
            session.flush()
        skill.updated_at = now
        session.add(version)
        audit.record(session, actor, "skill.upload", "skill_version", version.id, skill_id=skill.id, version=number)
        session.flush()
    except Exception as exc:
        discard_failed_upload(session, name, number)
        if isinstance(exc, IntegrityError):
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            if creating and constraint in ("uq_skill_name", "skill_name_key"):
                raise _name_conflict(name) from exc
            if constraint == "uq_skill_version_pending":
                raise Conflict(Msg("该技能已有待审核的版本")) from exc
        raise
    return _version_view(skill, version, {actor.id: actor})


def set_enabled(session: Session, actor: User, skill_id: str, enabled: bool) -> dict:
    skill = _skill(session, skill_id, lock=True)
    if skill.status != "listed":
        raise Conflict(Msg("只能设置已上架技能的启用状态"))
    item = session.get(SkillEnablement, (actor.id, skill.id))
    if enabled and item is None:
        session.add(SkillEnablement(user_id=actor.id, skill_id=skill.id))
    elif not enabled and item is not None:
        session.delete(item)
    session.flush()
    return _skill_views(session, actor, access.is_admin(session, actor), Skill.id == skill.id)[0]


def _lock_version(session: Session, version_id: str) -> tuple[Skill, SkillVersion]:
    if "\x00" in version_id:
        raise NotFound(Msg("技能版本不存在"))
    skill_id = session.scalar(select(SkillVersion.skill_id).where(SkillVersion.id == version_id))
    if skill_id is None:
        raise NotFound(Msg("技能版本不存在"))
    skill = _skill(session, skill_id, lock=True)
    version = session.get(SkillVersion, version_id, populate_existing=True)
    return skill, version


def _require_pending(version: SkillVersion) -> None:
    if version.status != "pending":
        raise Conflict(Msg("只有待审核版本可以执行此操作"))


def _require_admin(session: Session, actor: User) -> None:
    if not access.is_admin(session, actor):
        raise Forbidden(Msg("只有管理员可以审核技能"))


def _sync_publication(session: Session, skill_id: str, name: str, version: int | None) -> None:
    try:
        # 提交后重新取得行锁，避免较早的请求覆盖较新的上架状态。
        query = select(Skill.id).where(Skill.id == skill_id)
        if version is None:
            query = query.where(Skill.status == "delisted")
        else:
            query = query.where(Skill.status == "listed", Skill.published_version == version)
        if session.scalar(query.with_for_update()) is None:
            return
        if version is None:
            storage.unpublish(name)
        else:
            storage.publish(name, version)
    except storage.SkillStorageError:
        pass  # 存储边界已写日志。
    except (OSError, GovernanceError, SQLAlchemyError) as exc:
        log.exception("技能规范目录同步失败：%s，存储目录：%s", name, get_settings().skills_dir)
        if isinstance(exc, SQLAlchemyError):
            session.rollback()


def approve(session: Session, actor: User, version_id: str) -> dict:
    _require_admin(session, actor)
    skill, version = _lock_version(session, version_id)
    _require_pending(version)
    skill_id, name, number = skill.id, skill.name, version.version
    body = storage.skill_md_body(name, number)
    storage.zip_path(name, number)
    if skill.published_version is not None:
        previous = session.scalar(
            select(SkillVersion).where(
                SkillVersion.skill_id == skill.id,
                SkillVersion.version == skill.published_version,
                SkillVersion.status == "approved",
            )
        )
        if previous is not None:
            previous.status = "superseded"
    now = utcnow()
    version.status, version.reviewed_by, version.reviewed_at = "approved", actor.id, now
    skill.status, skill.published_version, skill.updated_at = "listed", version.version, now
    audit.record(
        session, actor, "skill.approve", "skill_version", version.id, skill_id=skill.id, version=version.version
    )
    session.flush()
    response = _detail(session, actor, skill, True, body=body)
    session.commit()
    _sync_publication(session, skill_id, name, number)
    return response


def reject(session: Session, actor: User, version_id: str, reason: str) -> dict:
    _require_admin(session, actor)
    if not 1 <= len(reason) <= 500 or "\x00" in reason:
        raise Invalid(Msg("技能请求参数无效"))
    skill, version = _lock_version(session, version_id)
    _require_pending(version)
    now = utcnow()
    version.status, version.reviewed_by, version.reviewed_at = "rejected", actor.id, now
    version.review_note = reason
    skill.updated_at = now
    audit.record(
        session, actor, "skill.reject", "skill_version", version.id, skill_id=skill.id, version=version.version
    )
    session.flush()
    return _version_views(session, skill, [version])[0]


def withdraw(session: Session, actor: User, version_id: str) -> dict:
    skill, version = _lock_version(session, version_id)
    if version.uploaded_by != actor.id:
        raise Forbidden(Msg("只有该版本的上传人可以撤回"))
    _require_pending(version)
    version.status, skill.updated_at = "withdrawn", utcnow()
    audit.record(
        session, actor, "skill.withdraw", "skill_version", version.id, skill_id=skill.id, version=version.version
    )
    session.flush()
    return _version_view(skill, version, {actor.id: actor})


def delist(session: Session, actor: User, skill_id: str) -> dict:
    skill = _skill(session, skill_id, lock=True)
    admin = access.is_admin(session, actor)
    if not admin and actor.id != skill.owner_id:
        raise Forbidden(Msg("只有技能上传人或管理员可以下架技能"))
    if skill.status != "listed":
        raise Conflict(Msg("只有已上架技能可以下架"))
    skill.status, skill.updated_at = "delisted", utcnow()
    audit.record(session, actor, "skill.delist", "skill", skill.id, version=skill.published_version)
    session.flush()
    response = _skill_views(session, actor, admin, Skill.id == skill.id)[0]
    name = skill.name
    session.commit()
    _sync_publication(session, skill_id, name, None)
    return response


def enabled_catalog(session: Session, user: User, *, names: list[str] | None = None) -> list[dict]:
    """取用户启用的上架版本，可按一批技能名筛选。"""
    query = (
        select(
            Skill.id.label("skill_id"),
            Skill.name,
            SkillVersion.description,
            SkillVersion.version,
            SkillVersion.id.label("version_id"),
        )
        .select_from(Skill)
        .join(SkillVersion, (SkillVersion.skill_id == Skill.id) & (SkillVersion.version == Skill.published_version))
        .join(SkillEnablement, SkillEnablement.skill_id == Skill.id)
        .where(SkillEnablement.user_id == user.id, Skill.status == "listed", SkillVersion.status == "approved")
        .order_by(Skill.name)
    )
    if names is not None:
        names = [name for name in names if len(name) <= 64 and _NAME.fullmatch(name)]
        if not names:
            return []
        query = query.where(Skill.name.in_(names))
    return [dict(row) for row in session.execute(query).mappings()]
