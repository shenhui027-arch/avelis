"""变更 → 影响 → 复核。

影响按列判断：只有依赖字段推导指纹发生变化的指标、关联路径才受影响；未受影响、证据完整的指标照常可查。
证据不全（指纹状态 unknown）也按受影响处理。受影响的指标版本立即被后端拦截（问数前核对状态与指纹），
不会因为尚未复核就继续当作旧定义使用。
"""

from __future__ import annotations

from collections import deque
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis import audit, cache
from avelis.db import utcnow
from avelis.governance import access
from avelis.governance.errors import Conflict, Forbidden, Invalid, NotFound
from avelis.governance.metrics import dependency_drift, mark_affected, status_before_affected, transition
from avelis.i18n import Msg
from avelis.models import (
    Answer,
    Asset,
    ChangeEvent,
    ChangeSet,
    Domain,
    Impact,
    JoinPath,
    LineageEdge,
    Metric,
    MetricVersion,
    ReviewTask,
    User,
)

RESOLUTIONS = ("restored", "new_version", "retired")


def analyze_change_set(session: Session, change_set: ChangeSet, fp_changes: list[dict[str, Any]]) -> None:
    changed = {(c["asset_id"], c["column"]): c for c in fp_changes}
    impacts = 0

    # 1. 下游资产：自身没有直接变化、但字段推导因上游而变化的资产，以及沿血缘继续传播到的资产
    direct = {e.asset_id for e in session.scalars(select(ChangeEvent).where(
        ChangeEvent.change_set_id == change_set.id,
        ChangeEvent.kind.not_in(("derivation_changed", "upstream_broken"))))}
    affected_assets: dict[str, list[dict[str, Any]]] = {}
    for (asset_id, column), change in changed.items():
        if asset_id not in direct:
            affected_assets.setdefault(asset_id, [{"asset_id": asset_id, "column": column,
                                                   "status": change["after"]["status"]}])
    for asset_id, path in downstream_assets(session, set(changed)).items():
        if asset_id not in direct:
            affected_assets.setdefault(asset_id, path)
    for asset_id, path in affected_assets.items():
        session.add(Impact(change_set_id=change_set.id, object_type="asset", object_id=asset_id, level="affected",
                           reason="上游字段推导发生变化", path=path))
        impacts += 1

    # 2. 指标版本：依赖指纹不一致即受影响
    affected_versions: list[MetricVersion] = []
    versions = session.scalars(select(MetricVersion).where(
        MetricVersion.status.in_(("published", "pending_review", "affected"))))
    for version in versions:
        hits = [d for d in version.dependency or [] if (d["asset_id"], d["column"]) in changed]
        if not hits:
            continue
        drift = dependency_drift(session, version)
        if not drift and version.status != "affected":
            continue
        metric = session.get(Metric, version.metric_id)
        cols = "、".join(f"{d['qualified'].split('.', 1)[1]}.{d['column']}" for d in hits)
        if not drift:
            level, reason = "affected", f"依赖字段 {cols} 的推导已恢复为与该版本一致，仍需负责人复核确认"
        elif any(d["current_status"] in ("ok", "broken") for d in drift):
            level, reason = "affected", f"依赖字段 {cols} 的推导已变化"
        else:
            level, reason = "possibly_affected", f"依赖字段 {cols} 的推导证据不全，按受影响处理"
        newly = mark_affected(session, version, reason)
        session.add(Impact(change_set_id=change_set.id, object_type="metric_version", object_id=version.id,
                           level=level, reason=reason, path=[{"asset_id": d["asset_id"], "column": d["column"],
                                                              "before": d["fingerprint"],
                                                              "after": d["current_fingerprint"],
                                                              "status": d["current_status"]} for d in drift]))
        impacts += 1
        open_task = session.scalar(select(ReviewTask).where(ReviewTask.object_type == "metric_version",
                                                            ReviewTask.object_id == version.id,
                                                            ReviewTask.status == "open"))
        if newly or open_task is None:
            session.add(ReviewTask(change_set_id=change_set.id, object_type="metric_version", object_id=version.id,
                                   assignee_id=metric.owner_id, status="open", note=reason))
        cache.invalidate_tag(f"metric_version:{version.id}")
        for answer in session.scalars(select(Answer).where(Answer.status == "answered")):
            if version.id in (answer.metric_version_ids or []) and answer.recheck_status is None:
                answer.recheck_status = "needs_recheck"
        audit.record(session, None, "metric_version.affected", "metric_version", version.id, reason=reason,
                     change_set_id=change_set.id)
        affected_versions.append(version)

    # 2b. 派生指标：锁定的组成版本受影响，派生版本随之受影响并生成复核任务
    affected_ids = {v.id for v in affected_versions}
    for derived in session.scalars(select(MetricVersion).where(
            MetricVersion.kind == "derived", MetricVersion.status.in_(("published", "pending_review")))):
        hit = [c for c in derived.components or [] if c["version_id"] in affected_ids]
        if not hit:
            continue
        reason = "组成指标受影响：" + "、".join(f"{c['metric_name']} V{c['version_no']}" for c in hit)
        if not mark_affected(session, derived, reason):
            continue
        metric = session.get(Metric, derived.metric_id)
        session.add(Impact(change_set_id=change_set.id, object_type="metric_version", object_id=derived.id,
                           level="affected", reason=reason,
                           path=[{"component": c["metric_code"], "version_id": c["version_id"]} for c in hit]))
        session.add(ReviewTask(change_set_id=change_set.id, object_type="metric_version", object_id=derived.id,
                               assignee_id=metric.owner_id, status="open", note=reason))
        cache.invalidate_tag(f"metric_version:{derived.id}")
        audit.record(session, None, "metric_version.affected", "metric_version", derived.id, reason=reason,
                     change_set_id=change_set.id)
        impacts += 1

    # 3. 关联路径
    for path in session.scalars(select(JoinPath).where(JoinPath.status == "approved")):
        hits = [d for d in path.dependency or [] if (d["asset_id"], d["column"]) in changed]
        if not hits:
            continue
        reason = "关联键的推导已变化：" + "、".join(d["column"] for d in hits)
        from avelis.governance.joins import reset_approvals

        reset_approvals(session, path, "affected", reason)
        session.add(Impact(change_set_id=change_set.id, object_type="join_path", object_id=path.id,
                           level="affected", reason=reason, path=hits))
        left = session.get(Asset, path.left_asset_id)
        owner = session.get(Domain, left.domain_id).owner_id if left.domain_id else path.created_by
        session.add(ReviewTask(change_set_id=change_set.id, object_type="join_path", object_id=path.id,
                               assignee_id=owner, status="open", note=reason))
        impacts += 1

    change_set.summary = {**(change_set.summary or {}), "impacts": impacts}
    if impacts == 0:
        change_set.status = "resolved"
        change_set.resolved_at = utcnow()


def downstream_assets(session: Session, start: set[tuple[str, str]]) -> dict[str, list[dict[str, str]]]:
    """从变化的字段出发，沿已确认血缘（解析、人工）找下游资产，返回每个资产的一条传播路径。"""
    edges = list(session.scalars(select(LineageEdge).where(LineageEdge.status == "active",
                                                           LineageEdge.origin.in_(("parsed", "manual")))))
    by_source: dict[tuple[str, str], list[LineageEdge]] = {}
    for e in edges:
        if e.source_asset_id:
            by_source.setdefault((e.source_asset_id, e.source_column or ""), []).append(e)
    out: dict[str, list[dict[str, str]]] = {}
    queue = deque((key, [{"asset_id": key[0], "column": key[1]}]) for key in start)
    seen = set(start)
    while queue:
        key, path = queue.popleft()
        for e in by_source.get(key, []):
            targets = [e.target_column] if e.target_column else _all_columns(session, e.target_asset_id)
            for col in targets:
                nxt = (e.target_asset_id, col)
                if nxt in seen:
                    continue
                seen.add(nxt)
                new_path = path + [{"asset_id": e.target_asset_id, "column": col, "relation": e.relation}]
                out.setdefault(e.target_asset_id, new_path)
                queue.append((nxt, new_path))
    for asset_id, _ in start:
        out.pop(asset_id, None)
    return out


def _all_columns(session: Session, asset_id: str) -> list[str]:
    from avelis.models import ColumnVersion

    asset = session.get(Asset, asset_id)
    if asset is None or not asset.current_version_id:
        return []
    return list(session.scalars(select(ColumnVersion.name).where(
        ColumnVersion.asset_version_id == asset.current_version_id)))


# --- 复核 -------------------------------------------------------------------------

def resolve_task(session: Session, actor: User, task: ReviewTask, resolution: str, note: str,
                 linked_metric_version_id: str | None = None) -> ReviewTask:
    if task.status != "open":
        raise Conflict("复核任务已处理")
    if resolution not in RESOLUTIONS:
        raise Invalid(Msg("处理方式只能是：{resolutions}", resolutions="、".join(RESOLUTIONS)))
    if task.assignee_id != actor.id and not access.is_admin(session, actor):
        raise Forbidden("只有任务负责人可以处理")
    if not note:
        raise Invalid("必须填写处理理由")
    if task.object_type == "metric_version":
        _resolve_metric(session, actor, task, resolution, note, linked_metric_version_id)
    elif task.object_type == "join_path":
        _resolve_join_path(session, task, resolution)
    task.status = "resolved"
    task.resolution = resolution
    task.note = note
    task.resolved_by = actor.id
    task.resolved_at = utcnow()
    task.linked_metric_version_id = linked_metric_version_id
    session.flush()
    audit.record(session, actor, "review_task.resolve", "review_task", task.id, resolution=resolution, note=note,
                 target_type=task.object_type, target_id=task.object_id, linked=linked_metric_version_id)
    _maybe_close_change_set(session, task.change_set_id)
    return task


def _resolve_metric(session: Session, actor: User, task: ReviewTask, resolution: str, note: str,
                    linked_id: str | None) -> None:
    version = session.get(MetricVersion, task.object_id)
    if version.status != "affected":
        raise Conflict(Msg("指标版本当前为 {status}，不是受影响状态", status=version.status))
    if resolution == "restored":
        drift = dependency_drift(session, version)
        if drift:
            raise Conflict("当前定义仍与该版本不一致，不能按“已恢复”处理。请先在数据库恢复原定义并重新采集",
                           drift=drift)
        if status_before_affected(session, version) != "published":
            # 受影响前尚未批准：只能回到待批准，由复核者按正式流程批准（含禁止自审与发布检查）
            transition(session, version, "pending_review", actor, f"复核：依赖已恢复，回到待批准。{note}")
            return
        transition(session, version, "published", actor, f"复核：已恢复原定义。{note}")
        for answer in session.scalars(select(Answer).where(Answer.recheck_status == "needs_recheck")):
            if version.id in (answer.metric_version_ids or []):
                answer.recheck_status = "reconfirmed"
        return
    if resolution == "new_version":
        if not linked_id:
            raise Invalid("选择“发布新口径”时必须指定已发布的新指标版本")
        linked = session.get(MetricVersion, linked_id)
        if linked is None or linked.status != "published":
            raise Conflict("指定的新口径必须是已发布的指标版本")
        if linked.id == version.id:
            raise Invalid("新口径不能是受影响的版本本身")
    action = "改用新口径" if resolution == "new_version" else "停用"
    transition(session, version, "retired", actor, f"复核：{action}。{note}")


def _resolve_join_path(session: Session, task: ReviewTask, resolution: str) -> None:
    path = session.get(JoinPath, task.object_id)
    if resolution == "restored":
        from avelis.governance.joins import drift

        if drift(session, path):
            raise Conflict("关联键当前推导仍与批准时不一致")
        path.status = "pending"  # 依据已恢复：仍须涉及的各业务域负责人重新批准
    else:
        path.status = "rejected"


def _maybe_close_change_set(session: Session, change_set_id: str) -> None:
    """关闭没有未决复核的变更集：本组的任务都已处理，且本组影响的指标版本不再处于“受影响”。"""
    for cs in session.scalars(select(ChangeSet).where(ChangeSet.status == "open")):
        if session.scalar(select(ReviewTask.id).where(ReviewTask.change_set_id == cs.id,
                                                      ReviewTask.status == "open").limit(1)):
            continue
        affected = [i for i in session.scalars(select(Impact).where(
            Impact.change_set_id == cs.id, Impact.object_type == "metric_version"))
            if (v := session.get(MetricVersion, i.object_id)) is not None and v.status == "affected"]
        if cs.id == change_set_id or not affected:
            cs.status = "resolved"
            cs.resolved_at = utcnow()


def task_or_404(session: Session, task_id: str) -> ReviewTask:
    task = session.get(ReviewTask, task_id)
    if task is None:
        raise NotFound("复核任务不存在")
    return task
