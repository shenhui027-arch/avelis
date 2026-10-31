"""个人经验：显式保存、版本留底与有界召回；正文始终是数据，不进入工具分发。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis import audit
from avelis.db import new_id, utcnow
from avelis.governance import access
from avelis.governance.errors import Conflict, Forbidden, GovernanceError, Invalid, NotFound
from avelis.models import AnalysisRun, Asset, AssetVersion, Experience, ExperienceVersion, Metric, MetricVersion, User

CONTENT_CHARS = 4000
MAX_REFS = 50
RECALL_SCAN = 200


def _require_user(session: Session, actor: User) -> None:
    access.require_role(session, actor, "analyst", "steward", "reviewer")


def candidates_from_feedback(*, run_id: str, proposal: dict, feedback: dict, refs: list[dict],
                             checkpoint: str = "assumptions") -> list[dict]:
    """只整理用户补充、质疑和对推断的明确决策，不把已知来源重复列为经验。"""
    items = {str(item.get("id")): item for item in proposal.get("items", []) if isinstance(item, dict)}
    candidates: list[dict] = []
    for field in ("supplement", "challenge"):
        text = str(feedback.get(field) or "").strip()
        if text:
            candidates.append({"kind": "rule", "content": text[:CONTENT_CHARS], "refs": _merge_refs(refs),
                               "source": {"run_id": run_id, "checkpoint": checkpoint,
                                          "user_text": text[:CONTENT_CHARS]}})
    seen: set[str] = set()
    for choice in ("rejected", "confirmed"):
        for decision in feedback.get("decisions", []):
            item_id = str(decision.get("item_id", ""))
            item = items.get(item_id)
            if (item is None or item.get("source") != "inferred" or item_id in seen
                    or decision.get("decision") != choice):
                continue
            seen.add(item_id)
            text = str(item.get("content", "")).strip()
            if not text:
                continue
            rejected = choice == "rejected"
            content = (("否决：" if rejected else "") + text)[:CONTENT_CHARS]
            kind = "negative" if rejected else ("join" if item.get("kind") == "join" else "rule")
            candidates.append({"kind": kind, "content": content,
                               "refs": _merge_refs(item.get("refs") or [], refs),
                               "source": {"run_id": run_id, "checkpoint": checkpoint,
                                          "user_text": content}})
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


def _resolve_refs(session: Session, actor: User, refs: list[dict], *,
                  allowed: set[str] | None = None) -> tuple[list[dict], set[str], set[str]]:
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
            version = (session.scalar(select(AssetVersion).where(AssetVersion.asset_id == asset.id,
                                                               AssetVersion.version_no == number))
                       if number is not None else session.get(AssetVersion, asset.current_version_id))
            if version is None or version.asset_id != asset.id:
                raise Invalid("经验引用的资产版本不存在")
            if not access.can_read_asset(session, actor, asset, allowed):
                raise Forbidden("无权访问经验引用的资产")
            pinned.append({"object_type": "asset", "object_id": asset.id, "version": version.version_no})
            asset_keys.update(_asset_keys(asset))
        else:
            metric = session.get(Metric, object_id) if object_type == "metric" else None
            version = (session.get(MetricVersion, object_id) if object_type == "metric_version" else
                       (session.scalar(select(MetricVersion).where(MetricVersion.metric_id == object_id,
                                                                  MetricVersion.version_no == number))
                        if number is not None else (latest_governed_version(session, metric) if metric else None)))
            if version is None or (number is not None and version.version_no != number):
                raise Invalid("经验引用的口径版本不存在")
            metric = metric or session.get(Metric, version.metric_id)
            if metric is None or version.metric_id != metric.id:
                raise Invalid("经验引用的口径不存在")
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
    return {"id": experience.id, "owner_id": experience.owner_id, "scope": experience.scope,
            "current_version": experience.current_version, "status": experience.status,
            "created_at": experience.created_at.isoformat(), "version_id": version.id, "version": version.version,
            "kind": version.kind, "content": version.content, "refs": version.refs, "source": version.source,
            "confirmed_by": version.confirmed_by, "confirmed_at": version.confirmed_at.isoformat(),
            "superseded_by": version.superseded_by}


def _current(session: Session, experience: Experience) -> ExperienceVersion:
    version = session.scalar(select(ExperienceVersion).where(ExperienceVersion.experience_id == experience.id,
                                                             ExperienceVersion.version == experience.current_version))
    if version is None:
        raise Conflict("经验当前版本缺失")
    return version


def _owned(session: Session, actor: User, experience_id: str, *, lock: bool = False) -> Experience:
    experience = session.get(Experience, experience_id, with_for_update=lock, populate_existing=lock)
    if experience is None or experience.owner_id != actor.id or experience.scope != "personal":
        raise NotFound("个人经验不存在")
    return experience


def list_experiences(session: Session, actor: User) -> list[dict]:
    _require_user(session, actor)
    rows = session.scalars(select(Experience).where(Experience.owner_id == actor.id, Experience.scope == "personal")
                           .order_by(Experience.created_at.desc(), Experience.id).limit(500))
    return [_view(row, _current(session, row)) for row in rows]


def save_experience(session: Session, actor: User, *, kind: str, content: str, refs: list[dict], source: dict) -> dict:
    """只有显式调用此写入操作才会保存；确认反馈本身只生成候选。"""
    _require_user(session, actor)
    if len(refs) > MAX_REFS:
        raise Invalid(f"经验引用最多 {MAX_REFS} 项")
    pinned, _, _ = _resolve_refs(session, actor, refs)
    origin = _source(session, actor, source)
    now = utcnow()
    experience = Experience(id=new_id(), owner_id=actor.id, scope="personal", current_version=1,
                            status="active", created_at=now)
    version = ExperienceVersion(id=new_id(), experience_id=experience.id, version=1, kind=kind,
                                content=content.strip(), refs=pinned, source=origin,
                                confirmed_by=actor.id, confirmed_at=now)
    session.add(experience)
    session.flush()
    session.add(version)
    session.flush()
    audit.record(session, actor, "experience.save", "experience", experience.id,
                 version_id=version.id, version=1, kind=kind, refs=pinned, source=origin, content=version.content)
    return _view(experience, version)


def revoke_experience(session: Session, actor: User, experience_id: str, *, reason: str = "") -> dict:
    _require_user(session, actor)
    experience = _owned(session, actor, experience_id, lock=True)
    if experience.status != "active":
        raise Conflict("只有有效经验可以撤销")
    version = _current(session, experience)
    experience.status = "revoked"
    audit.record(session, actor, "experience.revoke", "experience", experience.id,
                 version_id=version.id, from_status="active", to_status="revoked", reason=reason)
    session.flush()
    return _view(experience, version)


def replace_experience(session: Session, actor: User, experience_id: str, *, kind: str, content: str,
                       refs: list[dict], source: dict) -> dict:
    _require_user(session, actor)
    experience = _owned(session, actor, experience_id, lock=True)
    if experience.status != "active":
        raise Conflict("只有有效经验可以替代")
    old = _current(session, experience)
    replacement = save_experience(session, actor, kind=kind, content=content, refs=refs, source=source)
    experience.status = "superseded"
    old.superseded_by = replacement["version_id"]
    audit.record(session, actor, "experience.replace", "experience", experience.id,
                 version_id=old.id, superseded_by=old.superseded_by, replacement_id=replacement["id"],
                 from_status="active", to_status="superseded")
    session.flush()
    return replacement


def _keywords(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text[:1000].casefold())
    tokens = []
    for word in words:
        tokens.extend([word] if not re.fullmatch(r"[\u4e00-\u9fff]+", word) else
                      [word[i:i + 2] for i in range(max(1, len(word) - 1))])
    return set(tokens[:64])


def recall(session: Session, actor: User, *, metric_codes: Iterable[str] = (), asset_keys: Iterable[str] = (),
           question: str = "", k: int = 6, char_limit: int = 3000) -> list[dict]:
    """先过滤本人、状态、全部引用权限和精确结构化键，再做有界关键词匹配。"""
    _require_user(session, actor)
    wanted_metrics, wanted_assets = set(metric_codes), set(asset_keys)
    if not k or not char_limit or not (wanted_metrics or wanted_assets):
        return []
    allowed = access.domain_ids(session, actor)
    rows = session.scalars(select(Experience).where(Experience.owner_id == actor.id, Experience.scope == "personal",
                                                    Experience.status == "active")
                           .order_by(Experience.created_at.desc(), Experience.id).limit(RECALL_SCAN))
    keywords = _keywords(question)
    matches = []
    for experience in rows:
        version = _current(session, experience)
        try:
            _, metrics, assets = _resolve_refs(session, actor, version.refs, allowed=allowed)
            if not (wanted_metrics & metrics or wanted_assets & assets):
                continue
        except GovernanceError:
            continue
        score = len(keywords & _keywords(version.content + " " + " ".join(sorted(metrics | assets))))
        if keywords and not score:
            continue
        item = _view(experience, version)
        matches.append((score, experience.id, item))
    matches.sort(key=lambda match: (-match[0], match[1]))
    selected, used = [], 0
    for _, _, item in matches:
        cost = len(json.dumps(item, ensure_ascii=False, default=str, separators=(",", ":")))
        if used + cost > char_limit:
            continue
        selected.append(item)
        used += cost
        if len(selected) >= k:
            break
    return selected
