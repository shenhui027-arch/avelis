"""个人经验：显式保存、版本留底与有界召回；正文始终是数据，不进入工具分发。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from avelis import audit
from avelis.db import new_id, utcnow
from avelis.governance import access, joins
from avelis.governance.errors import Conflict, Forbidden, GovernanceError, Invalid, NotFound
from avelis.i18n import Msg
from avelis.models import (
    AnalysisRun,
    Asset,
    AssetVersion,
    Domain,
    Experience,
    ExperienceVersion,
    JoinPath,
    Metric,
    MetricVersion,
    User,
)
from avelis.retrieval_hybrid import SearchDocument, hybrid_rank

CONTENT_CHARS = 4000
MAX_REFS = 50
RECALL_SCAN = 200


def _require_user(session: Session, actor: User) -> None:
    access.require_role(session, actor, "analyst", "steward", "reviewer")


def candidates_from_feedback(
    *, run_id: str, proposal: dict, feedback: dict, refs: list[dict], checkpoint: str = "assumptions"
) -> list[dict]:
    """保留用户补充（含插话）、质疑与否决的推断。

    否决的推断作为否定经验；确认的推断只用于本次分析，关联另走治理提议。
    已知来源不重复列为经验。
    """
    items = {str(item.get("id")): item for item in proposal.get("items", []) if isinstance(item, dict)}
    candidates: list[dict] = []
    for field in ("supplement", "challenge"):
        text = str(feedback.get(field) or "").strip()
        if text:
            candidates.append(
                {
                    "kind": "rule",
                    "content": text[:CONTENT_CHARS],
                    "refs": _merge_refs(refs),
                    "source": {"run_id": run_id, "checkpoint": checkpoint, "user_text": text[:CONTENT_CHARS]},
                }
            )
    seen: set[str] = set()
    for decision in feedback.get("decisions", []):
        item_id = str(decision.get("item_id", ""))
        item = items.get(item_id)
        if (
            item is None
            or item.get("source") != "inferred"
            or item_id in seen
            or decision.get("decision") != "rejected"
        ):
            continue
        seen.add(item_id)
        text = str(item.get("content", "")).strip()
        if not text:
            continue
        content = ("否决：" + text)[:CONTENT_CHARS]
        candidates.append(
            {
                "kind": "negative",
                "content": content,
                "refs": _merge_refs(item.get("refs") or [], refs),
                "source": {"run_id": run_id, "checkpoint": checkpoint, "user_text": content},
            }
        )
    return candidates


def _merge_refs(*groups: list[dict]) -> list[dict]:
    refs, seen = [], set()
    for group in groups:
        for ref in group:
            key = json.dumps(ref, sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                refs.append(dict(ref))
    # 超出界限时保留完整引用，保存时拒绝；不能截掉不可见引用后让经验生效。
    return refs


def _asset_keys(asset: Asset) -> set[str]:
    return {asset.id, f"{asset.db_name}.{asset.name}", f"{asset.catalog_name}.{asset.db_name}.{asset.name}"}


def _resolve_refs(
    session: Session,
    actor: User,
    refs: list[dict],
    *,
    allowed: set[str] | None = None,
    domain_id: str | None = None,
) -> tuple[list[dict], set[str], set[str]]:
    """规范化到具体版本，并返回用于结构化召回的口径键与资产键。"""
    from avelis.ask.service import visible_metric
    from avelis.governance.metrics import latest_governed_version

    allowed = access.domain_ids(session, actor) if allowed is None else allowed
    pinned, metric_keys, asset_keys = [], set(), set()
    for ref in refs:
        object_type, object_id, number = ref.get("object_type"), ref.get("object_id"), ref.get("version")
        if object_type == "asset":
            asset = session.get(Asset, object_id)
            if asset is None:
                raise Invalid("经验引用的资产不存在")
            version = (
                session.scalar(
                    select(AssetVersion).where(AssetVersion.asset_id == asset.id, AssetVersion.version_no == number)
                )
                if number is not None
                else session.get(AssetVersion, asset.current_version_id)
            )
            if version is None or version.asset_id != asset.id:
                raise Invalid("经验引用的资产版本不存在")
            if domain_id is not None and asset.domain_id != domain_id:
                raise Forbidden("经验引用的资产不属于目标业务域")
            if not access.can_read_asset(session, actor, asset, allowed):
                raise Forbidden("无权访问经验引用的资产")
            pinned.append({"object_type": "asset", "object_id": asset.id, "version": version.version_no})
            asset_keys.update(_asset_keys(asset))
        else:
            metric = session.get(Metric, object_id) if object_type == "metric" else None
            version = (
                session.get(MetricVersion, object_id)
                if object_type == "metric_version"
                else (
                    session.scalar(
                        select(MetricVersion).where(
                            MetricVersion.metric_id == object_id, MetricVersion.version_no == number
                        )
                    )
                    if number is not None
                    else (latest_governed_version(session, metric) if metric else None)
                )
            )
            if version is None or (number is not None and version.version_no != number):
                raise Invalid("经验引用的口径版本不存在")
            metric = metric or session.get(Metric, version.metric_id)
            if metric is None or version.metric_id != metric.id:
                raise Invalid("经验引用的口径不存在")
            if domain_id is not None and metric.domain_id != domain_id:
                raise Forbidden("经验引用的口径不属于目标业务域")
            if visible_metric(session, metric.code, allowed)[1] is None:
                raise Forbidden("无权访问经验引用的口径")
            pinned.append({"object_type": "metric_version", "object_id": version.id, "version": version.version_no})
            metric_keys.update((metric.id, metric.code, version.id))
    return _merge_refs(pinned), metric_keys, asset_keys


def _source(session: Session, actor: User, source: dict) -> dict:
    run_id, checkpoint, user_text = source.get("run_id"), source.get("checkpoint"), source.get("user_text", "")
    run = session.get(AnalysisRun, run_id)
    if run is None or run.user_id != actor.id:
        raise NotFound("分析运行不存在")
    return {"run_id": run_id, "checkpoint": checkpoint, "user_text": user_text}


def _view(experience: Experience, version: ExperienceVersion) -> dict[str, Any]:
    submitted_at = getattr(experience, "submitted_at", None)
    reviewed_at = getattr(experience, "reviewed_at", None)
    return {
        "id": experience.id,
        "owner_id": experience.owner_id,
        "scope": experience.scope,
        "domain_id": getattr(experience, "domain_id", None),
        "review_status": getattr(experience, "review_status", None) or "none",
        "submitted_by": getattr(experience, "submitted_by", None),
        "submitted_at": submitted_at.isoformat() if submitted_at else None,
        "reviewed_by": getattr(experience, "reviewed_by", None),
        "reviewed_at": reviewed_at.isoformat() if reviewed_at else None,
        "review_note": getattr(experience, "review_note", None),
        "current_version": experience.current_version,
        "status": experience.status,
        "created_at": experience.created_at.isoformat(),
        "version_id": version.id,
        "version": version.version,
        "kind": version.kind,
        "content": version.content,
        "refs": version.refs,
        "source": version.source,
        "confirmed_by": version.confirmed_by,
        "confirmed_at": version.confirmed_at.isoformat(),
        "superseded_by": version.superseded_by,
    }


def _current(session: Session, experience: Experience) -> ExperienceVersion:
    version = session.scalar(
        select(ExperienceVersion).where(
            ExperienceVersion.experience_id == experience.id, ExperienceVersion.version == experience.current_version
        )
    )
    if version is None:
        raise Conflict("经验当前版本缺失")
    return version


def _owned(session: Session, actor: User, experience_id: str, *, lock: bool = False) -> Experience:
    experience = session.get(Experience, experience_id, with_for_update=lock, populate_existing=lock)
    if experience is None or experience.owner_id != actor.id or experience.scope not in ("personal", "domain"):
        raise NotFound("个人经验不存在")
    return experience


def _dedup_key(kind: str, content: str, refs: list[dict]) -> tuple:
    return kind, content.strip(), tuple(sorted({(ref["object_type"], ref["object_id"]) for ref in refs}))


def list_experiences(
    session: Session,
    actor: User,
    *,
    scope: str = "personal",
    domain_id: str | None = None,
    review_status: str | None = None,
) -> list[dict]:
    _require_user(session, actor)
    if scope not in ("personal", "domain", "all"):
        raise Invalid("无效的经验范围")
    allowed = access.domain_ids(session, actor)
    admin = access.is_admin(session, actor)
    rows: list[Experience] = []

    if scope in ("personal", "all"):
        personal_rows = list(
            session.scalars(
                select(Experience)
                .where(Experience.owner_id == actor.id, Experience.scope == "personal")
                .order_by(Experience.created_at.desc(), Experience.id)
                .limit(500)
            )
        )
        rows.extend(personal_rows)

    if scope in ("domain", "all"):
        target_domains = (
            [domain_id] if domain_id and (domain_id in allowed or admin) else ([] if domain_id else sorted(allowed))
        )
        if target_domains:
            stmt = (
                select(Experience)
                .where(Experience.scope == "domain", Experience.domain_id.in_(target_domains))
                .order_by(Experience.created_at.desc(), Experience.id)
                .limit(500)
            )
            if review_status:
                stmt = stmt.where(Experience.review_status == review_status)
            for row in session.scalars(stmt):
                if (
                    admin
                    or row.owner_id == actor.id
                    or row.review_status == "approved"
                    or (row.domain_id and access.domain_role(session, actor, row.domain_id) in ("owner", "maintainer"))
                ):
                    rows.append(row)

    result, seen_keys, seen_ids = [], set(), set()
    for row in rows:
        if row.id in seen_ids:
            continue
        seen_ids.add(row.id)
        if review_status and getattr(row, "review_status", "none") != review_status:
            continue
        version = _current(session, row)
        if row.scope == "domain" and not admin and row.owner_id != actor.id:
            try:
                _resolve_refs(session, actor, version.refs, allowed=allowed)
            except GovernanceError:
                continue
        key = (row.scope, row.domain_id, _dedup_key(version.kind, version.content, version.refs))
        if row.status == "active" and row.scope == "personal":
            if key in seen_keys:
                continue
            seen_keys.add(key)
        result.append(_view(row, version))
    return result


def _matching_active(session: Session, actor: User, kind: str, content: str, refs: list[dict]) -> dict | None:
    key = _dedup_key(kind, content, refs)
    rows = session.scalars(
        select(Experience)
        .where(Experience.owner_id == actor.id, Experience.scope == "personal", Experience.status == "active")
        .order_by(Experience.created_at.desc(), Experience.id)
    )
    for row in rows:
        version = _current(session, row)
        if _dedup_key(version.kind, version.content, version.refs) == key:
            return _view(row, version)
    return None


def save_experience(session: Session, actor: User, *, kind: str, content: str, refs: list[dict], source: dict) -> dict:
    """只有显式调用此写入操作才会保存；确认反馈本身只生成候选。"""
    _require_user(session, actor)
    if len(refs) > MAX_REFS:
        raise Invalid(Msg("经验引用最多 {limit} 项", limit=MAX_REFS))
    pinned, _, _ = _resolve_refs(session, actor, refs)
    origin = _source(session, actor, source)
    content = content.strip()
    existing = _matching_active(session, actor, kind, content, pinned)
    if existing is not None:
        return existing
    now = utcnow()
    experience = Experience(
        id=new_id(),
        owner_id=actor.id,
        scope="personal",
        review_status="none",
        current_version=1,
        status="active",
        created_at=now,
    )
    version = ExperienceVersion(
        id=new_id(),
        experience_id=experience.id,
        version=1,
        kind=kind,
        content=content,
        refs=pinned,
        source=origin,
        confirmed_by=actor.id,
        confirmed_at=now,
    )
    session.add(experience)
    session.flush()
    session.add(version)
    session.flush()
    audit.record(
        session,
        actor,
        "experience.save",
        "experience",
        experience.id,
        version_id=version.id,
        version=1,
        kind=kind,
        refs=pinned,
        source=origin,
        content=version.content,
    )
    return _view(experience, version)


def revoke_experience(session: Session, actor: User, experience_id: str, *, reason: str = "") -> dict:
    _require_user(session, actor)
    experience = session.get(Experience, experience_id, with_for_update=True, populate_existing=True)
    if experience is None or experience.scope not in ("personal", "domain"):
        raise NotFound("个人经验不存在")
    if experience.scope == "personal" and experience.owner_id != actor.id:
        raise NotFound("个人经验不存在")
    if experience.scope == "domain" and experience.owner_id != actor.id:
        if not experience.domain_id:
            raise NotFound("经验不存在")
        access.require_domain_maintainer(session, actor, experience.domain_id)
    if experience.status != "active":
        raise Conflict("只有有效经验可以撤销")
    version = _current(session, experience)
    experience.status = "revoked"
    audit.record(
        session,
        actor,
        "experience.revoke",
        "experience",
        experience.id,
        version_id=version.id,
        from_status="active",
        to_status="revoked",
        reason=reason,
    )
    session.flush()
    return _view(experience, version)


def submit_experience(session: Session, actor: User, experience_id: str, *, domain_id: str) -> dict:
    """将个人有效经验提交到指定业务域共享复核。"""
    _require_user(session, actor)
    experience = _owned(session, actor, experience_id, lock=True)
    if experience.status != "active":
        raise Conflict("只有有效经验可以共享到业务域")
    if (getattr(experience, "review_status", None) or "none") not in ("none", "rejected"):
        raise Conflict("该经验当前已提交或已通过业务域审核")
    domain = session.get(Domain, domain_id)
    if domain is None:
        raise NotFound("业务域不存在")
    if domain_id not in access.domain_ids(session, actor):
        raise Forbidden("只能共享到自己所属的业务域")
    version = _current(session, experience)
    if not version.refs:
        raise Invalid("共享到业务域的经验至少需要引用一个资产或口径")
    _resolve_refs(session, actor, version.refs, domain_id=domain_id)
    now = utcnow()
    experience.scope = "domain"
    experience.domain_id = domain_id
    experience.review_status = "pending"
    experience.submitted_by = actor.id
    experience.submitted_at = now
    experience.reviewed_by = None
    experience.reviewed_at = None
    experience.review_note = None
    audit.record(
        session,
        actor,
        "experience.submit",
        "experience",
        experience.id,
        domain_id=domain_id,
        version=experience.current_version,
    )
    session.flush()
    return _view(experience, version)


def approve_experience(session: Session, actor: User, experience_id: str, *, note: str | None = None) -> dict:
    """批准共享到业务域的经验；严格禁止所有者或提交人自审。"""
    _require_user(session, actor)
    experience = session.get(Experience, experience_id, with_for_update=True, populate_existing=True)
    if experience is None or experience.scope != "domain" or not experience.domain_id:
        raise NotFound("业务域待审经验不存在")
    access.require_domain_maintainer(session, actor, experience.domain_id)
    if actor.id in (experience.owner_id, experience.submitted_by):
        raise Forbidden("禁止自审：经验所有者或提交人不能审批同一经验")
    if experience.status != "active" or experience.review_status != "pending":
        raise Conflict("只有有效且待审核的业务域经验可以批准")
    version = _current(session, experience)
    _resolve_refs(session, actor, version.refs, domain_id=experience.domain_id)
    target_key = _dedup_key(version.kind, version.content, version.refs)
    for other in session.scalars(
        select(Experience).where(
            Experience.scope == "domain",
            Experience.domain_id == experience.domain_id,
            Experience.status == "active",
            Experience.review_status == "approved",
        )
    ):
        if other.id == experience.id:
            continue
        other_ver = _current(session, other)
        if _dedup_key(other_ver.kind, other_ver.content, other_ver.refs) == target_key:
            raise Conflict("该业务域已存在相同的已共享经验")
    experience.review_status = "approved"
    experience.reviewed_by = actor.id
    experience.reviewed_at = utcnow()
    experience.review_note = (note or "").strip() or None
    audit.record(
        session,
        actor,
        "experience.approve",
        "experience",
        experience.id,
        domain_id=experience.domain_id,
        note=experience.review_note,
    )
    session.flush()
    return _view(experience, version)


def reject_experience(session: Session, actor: User, experience_id: str, *, note: str) -> dict:
    """驳回共享到业务域的经验；必须填写原因且严格禁止自审。"""
    reason = (note or "").strip()
    if not reason:
        raise Invalid("驳回必须填写原因")
    _require_user(session, actor)
    experience = session.get(Experience, experience_id, with_for_update=True, populate_existing=True)
    if experience is None or experience.scope != "domain" or not experience.domain_id:
        raise NotFound("业务域待审经验不存在")
    access.require_domain_maintainer(session, actor, experience.domain_id)
    if actor.id in (experience.owner_id, experience.submitted_by):
        raise Forbidden("禁止自审：经验所有者或提交人不能驳回同一经验")
    if experience.status != "active" or experience.review_status != "pending":
        raise Conflict("只有有效且待审核的业务域经验可以驳回")
    version = _current(session, experience)
    experience.review_status = "rejected"
    experience.reviewed_by = actor.id
    experience.reviewed_at = utcnow()
    experience.review_note = reason
    audit.record(
        session,
        actor,
        "experience.reject",
        "experience",
        experience.id,
        domain_id=experience.domain_id,
        note=reason,
    )
    session.flush()
    return _view(experience, version)


def replace_experience(
    session: Session, actor: User, experience_id: str, *, kind: str, content: str, refs: list[dict], source: dict
) -> dict:
    _require_user(session, actor)
    experience = _owned(session, actor, experience_id, lock=True)
    if experience.status != "active":
        raise Conflict("只有有效经验可以替代")
    old = _current(session, experience)
    replacement = save_experience(session, actor, kind=kind, content=content, refs=refs, source=source)
    if replacement["id"] == experience.id:
        return replacement
    experience.status = "superseded"
    old.superseded_by = replacement["version_id"]
    audit.record(
        session,
        actor,
        "experience.replace",
        "experience",
        experience.id,
        version_id=old.id,
        superseded_by=old.superseded_by,
        replacement_id=replacement["id"],
        from_status="active",
        to_status="superseded",
    )
    session.flush()
    return replacement


_IDENTIFIER = r"(?:`[^`]+`|[\w$]+)"
_QUALIFIED_COLUMN = rf"{_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER}){{1,3}}"
_JOIN_EQUALITY = re.compile(rf"({_QUALIFIED_COLUMN})\s*=\s*({_QUALIFIED_COLUMN})")
_JOIN_EXPRESSION = re.compile(
    rf"(?P<expression>{_JOIN_EQUALITY.pattern}(?:\s+AND\s+{_JOIN_EQUALITY.pattern})*)"
    r"(?:\s*(?:\([^()]*\)|（[^（）]*）))?",
    re.IGNORECASE,
)


def _column_ref(text: str) -> tuple[str, str]:
    parts = [part.strip().strip("`") for part in text.split(".")]
    return ".".join(parts[:-1]), parts[-1]


def _join_asset(session: Session, actor: User, name: str, side: str, allowed: set[str]) -> Asset:
    matches = [
        asset
        for asset in session.scalars(select(Asset).where(Asset.status == "active"))
        if name in {asset.name, f"{asset.db_name}.{asset.name}", f"{asset.catalog_name}.{asset.db_name}.{asset.name}"}
        and access.can_read_asset(session, actor, asset, allowed)
    ]
    if not matches:
        raise Invalid(Msg("{side}资产 {name} 无法匹配当前用户可见的资产", side=Msg(side), name=name))
    if len(matches) != 1:
        raise Invalid(Msg("{side}资产 {name} 匹配到多个可见资产，请使用更完整的名称", side=Msg(side), name=name))
    return matches[0]


def to_join_path(session: Session, actor: User, experience_id: str) -> JoinPath:
    """把已有经验的等值关联提议到治理，成功后保留撤销记录。"""
    _require_user(session, actor)
    experience = _owned(session, actor, experience_id, lock=True)
    if experience.status != "active":
        raise Conflict("只有有效经验可以转为关联路径")
    version = _current(session, experience)
    if version.kind == "negative":
        raise Invalid("否定经验不能转为关联路径")
    expression = _JOIN_EXPRESSION.fullmatch(version.content.strip())
    if expression is None:
        raise Invalid("经验内容需要是 x.col = y.col，多对关联键用 AND 连接，可在末尾加括号说明")
    matches = list(_JOIN_EQUALITY.finditer(expression["expression"]))
    allowed = access.domain_ids(session, actor)
    left_columns, right_columns = [], []
    left_asset = right_asset = None
    for match in matches:
        left_name, left_column = _column_ref(match[1])
        right_name, right_column = _column_ref(match[2])
        left = _join_asset(session, actor, left_name, "左侧", allowed)
        right = _join_asset(session, actor, right_name, "右侧", allowed)
        if left_asset is None:
            left_asset, right_asset = left, right
        if (left.id, right.id) != (left_asset.id, right_asset.id) and (left.id, right.id) == (
            right_asset.id,
            left_asset.id,
        ):
            left, right, left_column, right_column = right, left, right_column, left_column
        if (left.id, right.id) != (left_asset.id, right_asset.id):
            raise Invalid("各对关联键必须属于相同的左侧和右侧资产")
        left_columns.append(left_column)
        right_columns.append(right_column)
    path = joins.propose(
        session,
        actor,
        left_asset_id=left_asset.id,
        left_columns=left_columns,
        right_asset_id=right_asset.id,
        right_columns=right_columns,
        source={
            "run_id": version.source.get("run_id"),
            "checkpoint": version.source.get("checkpoint"),
            "user_text": version.content,
        },
    )
    revoke_experience(session, actor, experience.id, reason=f"已转为关联路径 {path.code}")
    return path


def _keywords(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text[:1000].casefold())
    tokens = []
    for word in words:
        tokens.extend(
            [word]
            if not re.fullmatch(r"[\u4e00-\u9fff]+", word)
            else [word[i : i + 2] for i in range(max(1, len(word) - 1))]
        )
    return set(tokens[:64])


def recall(
    session: Session,
    actor: User,
    *,
    metric_codes: Iterable[str] = (),
    asset_keys: Iterable[str] = (),
    question: str = "",
    k: int = 6,
    char_limit: int = 3000,
) -> list[dict]:
    """合并召回本人有效经验与所属业务域已批准经验，按全部引用权限、结构化键与混合检索过滤排序。"""
    _require_user(session, actor)
    wanted_metrics, wanted_assets = set(metric_codes), set(asset_keys)
    if not k or not char_limit or not (wanted_metrics or wanted_assets):
        return []
    allowed = access.domain_ids(session, actor)
    clauses = [
        and_(Experience.owner_id == actor.id, Experience.scope == "personal", Experience.status == "active"),
        and_(
            Experience.owner_id == actor.id,
            Experience.scope == "domain",
            Experience.status == "active",
            Experience.review_status == "pending",
        ),
    ]
    if allowed:
        clauses.append(
            and_(
                Experience.scope == "domain",
                Experience.domain_id.in_(sorted(allowed)),
                Experience.status == "active",
                Experience.review_status == "approved",
            )
        )
    rows = session.scalars(
        select(Experience)
        .where(or_(*clauses) if len(clauses) > 1 else clauses[0])
        .order_by(Experience.created_at.desc(), Experience.id)
        .limit(RECALL_SCAN)
    )
    candidates: list[tuple[Experience, ExperienceVersion, set[str], set[str]]] = []
    for experience in rows:
        version = _current(session, experience)
        try:
            _, metrics, assets = _resolve_refs(session, actor, version.refs, allowed=allowed)
            if not (wanted_metrics & metrics or wanted_assets & assets):
                continue
        except GovernanceError:
            continue
        candidates.append((experience, version, metrics, assets))

    if not candidates:
        return []

    query = (question or "").strip()
    docs = [
        SearchDocument(
            key=exp.id,
            fields=[(3.0, ver.content), (1.5, " ".join(sorted(metrics | assets)))],
            semantic_text=f"{ver.content} {' '.join(sorted(metrics | assets))}",
        )
        for exp, ver, metrics, assets in candidates
    ]
    score_map = dict(hybrid_rank(query, docs)) if query else {exp.id: 1.0 for exp, *_ in candidates}
    keywords = _keywords(query)

    matches: list[tuple[float, int, str, tuple, dict]] = []
    for exp, ver, metrics, assets in candidates:
        score = score_map.get(exp.id, 0.0)
        if (
            query
            and score <= 0.0
            and not (keywords & _keywords(ver.content + " " + " ".join(sorted(metrics | assets))))
        ):
            continue
        item = _view(exp, ver)
        dedup = _dedup_key(ver.kind, ver.content, ver.refs)
        matches.append((score, 0 if exp.scope == "personal" else 1, exp.id, dedup, item))

    matches.sort(key=lambda m: (-round(m[0], 9), m[1], m[2]))
    selected, used, seen_keys = [], 0, set()
    for _, _, _, dedup, item in matches:
        if dedup in seen_keys:
            continue
        cost = len(json.dumps(item, ensure_ascii=False, default=str, separators=(",", ":")))
        if used + cost > char_limit:
            continue
        seen_keys.add(dedup)
        selected.append(item)
        used += cost
        if len(selected) >= k:
            break
    return selected
