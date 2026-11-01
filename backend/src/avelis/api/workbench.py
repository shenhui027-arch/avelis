"""工作台汇总：闭环各阶段的计数、当前用户的待办、负责的指标、被拦截的提问、数据源新鲜度、采集节奏。"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from avelis.api import schemas as S
from avelis.api.ask import answer_view
from avelis.api.deps import current_user, get_session
from avelis.config import get_settings
from avelis.db import utcnow
from avelis.governance import access
from avelis.i18n import gettext
from avelis.models import (
    Answer,
    Asset,
    CandidateEdge,
    ChangeEvent,
    ChangeSet,
    CollectionRun,
    Datasource,
    Domain,
    ImportJob,
    JoinPath,
    JoinPathApproval,
    LineageEdge,
    Metric,
    MetricVersion,
    ReviewTask,
    User,
)

router = APIRouter(prefix="/api", tags=["工作台"])

EVENT_LABELS = {
    "definition_changed": "定义变化", "derivation_changed": "字段推导变化", "upstream_broken": "上游缺失",
    "column_added": "新增列", "column_removed": "删除列", "column_type_changed": "类型变化",
    "column_rename_candidate": "改名候选", "added": "新增", "deleted": "删除", "renamed": "改名",
    "rename_candidate": "改名候选", "grain_changed": "粒度变化", "identity_link_changed": "同一张表匹配变化",
    "external_ref_changed": "外部地址变化", "comment_changed": "注释变化", "server_identity_changed": "实例变化",
}


def _iso(value) -> str:
    return value.isoformat() if value else ""


@router.get("/workbench", response_model=S.WorkbenchOut)
def workbench(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    settings = get_settings()
    tz = ZoneInfo(settings.business_timezone)
    now = utcnow()
    local_midnight = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start = local_midnight.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    allowed = access.domain_ids(session, user)
    roles = access.roles_of(session, user)
    is_admin = "admin" in roles

    # --- 数据源 ---
    datasources = []
    healthy = 0
    for ds in session.scalars(select(Datasource).order_by(Datasource.name)):
        last = session.scalar(select(CollectionRun).where(CollectionRun.datasource_id == ds.id)
                              .order_by(CollectionRun.started_at.desc()).limit(1))
        ok = last is not None and last.status == "succeeded"
        healthy += ok
        runs = session.scalar(select(func.count()).select_from(CollectionRun)
                              .where(CollectionRun.datasource_id == ds.id)) or 0
        datasources.append({"id": ds.id, "name": ds.name, "kind": ds.kind, "is_test_adapter": ds.is_test_adapter,
                            "status": "ok" if ok else ("failed" if last else "never"),
                            "last_run_at": _iso(last.finished_at or last.started_at) if last else None,
                            "run_no": runs, "interval_minutes": ds.collect_interval_minutes})

    # --- 采集节奏：今天每小时 ---
    cadence = []
    runs_today = list(session.scalars(select(CollectionRun).where(CollectionRun.started_at >= today_start)))
    change_runs = set(session.scalars(select(ChangeSet.run_id).where(ChangeSet.created_at >= today_start)))
    local_hour_now = now.astimezone(tz).hour
    for hour in range(24):
        in_hour = [r for r in runs_today if r.started_at.astimezone(tz).hour == hour]
        cadence.append({"hour": hour, "runs": len(in_hour), "changed": any(r.id in change_runs for r in in_hour),
                        "failed": any(r.status == "failed" for r in in_hour), "future": hour > local_hour_now})

    # --- 计数 ---
    edges = session.scalar(select(func.count()).select_from(LineageEdge).where(LineageEdge.status == "active")) or 0
    pending_candidates = session.scalar(select(func.count()).select_from(CandidateEdge)
                                        .where(CandidateEdge.status == "pending")) or 0
    open_sets = list(session.scalars(select(ChangeSet).where(ChangeSet.status == "open")))
    semantic = sum(1 for cs in open_sets if (cs.summary or {}).get("impacts"))
    metrics = list(session.scalars(select(Metric).where(Metric.domain_id.in_(allowed)))) if allowed else []
    statuses = {}
    for m in metrics:
        v = session.scalar(select(MetricVersion).where(MetricVersion.metric_id == m.id)
                           .order_by(MetricVersion.version_no.desc()).limit(1))
        statuses[m.id] = v
    published = sum(1 for v in statuses.values() if v and v.status == "published")
    affected = sum(1 for v in statuses.values() if v and v.status == "affected")
    answers_today = session.scalar(select(func.count()).select_from(Answer).where(Answer.asked_at >= today_start)) or 0
    blocked_today = session.scalar(select(func.count()).select_from(Answer).where(
        Answer.asked_at >= today_start, Answer.status.in_(("blocked", "denied")))) or 0
    last_run = max((d["last_run_at"] or "" for d in datasources), default="")
    stages = [
        {"key": "collect", "label": gettext("采集"), "value": str(sum(d["run_no"] for d in datasources)), "unit": gettext("批次"),
         "status": "ok" if healthy == len(datasources) and datasources else "warn",
         "note": gettext("{healthy} / {total} 数据源正常", healthy=healthy, total=len(datasources)), "at": last_run},
        {"key": "lineage", "label": gettext("血缘"), "value": str(edges), "unit": gettext("列级边"),
         "status": "warn" if pending_candidates else "ok",
         "note": gettext("{count} 条候选待确认", count=pending_candidates) if pending_candidates else gettext("无待确认候选")},
        {"key": "change", "label": gettext("变更"), "value": str(len(open_sets)), "unit": gettext("待处理"),
         "status": "danger" if semantic else "ok",
         "note": gettext("{count} 项影响口径", count=semantic) if semantic else gettext("无影响口径的变更")},
        {"key": "metric", "label": gettext("口径"), "value": f"{published} / {len(metrics)}", "unit": gettext("已发布"),
         "status": "warn" if affected else "ok", "note": gettext("{count} 个受影响", count=affected) if affected else gettext("全部可用")},
        {"key": "ask", "label": gettext("问数"), "value": str(answers_today), "unit": gettext("今日"),
         "status": "danger" if blocked_today else "ok",
         "note": gettext("{count} 次拦截", count=blocked_today) if blocked_today else gettext("无拦截")},
    ]

    # --- 待办 ---
    todos = []
    for t in session.scalars(select(ReviewTask).where(ReviewTask.status == "open", ReviewTask.assignee_id == user.id)
                             .order_by(ReviewTask.created_at.desc())):
        title, ref = "复核", t.object_id
        if t.object_type == "metric_version":
            v = session.get(MetricVersion, t.object_id)
            m = session.get(Metric, v.metric_id)
            title, ref = f"{m.name} V{v.version_no} · 上游定义变化", m.code
        elif t.object_type == "join_path":
            jp = session.get(JoinPath, t.object_id)
            title, ref = f"关联路径 {jp.name} · 关联键变化", jp.code
        todos.append({"kind": "review", "status": "danger", "title": title, "meta": "复核 · 指派给你", "ref": ref,
                      "link": f"/changes/{t.change_set_id}", "at": _iso(t.created_at)})
    if "reviewer" in roles or is_admin:
        for v in session.scalars(select(MetricVersion).where(MetricVersion.status == "pending_review")):
            m = session.get(Metric, v.metric_id)
            if m.domain_id not in allowed or v.submitted_by == user.id:
                continue
            submitter = session.get(User, v.submitted_by) if v.submitted_by else None
            todos.append({"kind": "approve", "status": "info", "title": f"{m.name} V{v.version_no}",
                          "meta": f"{submitter.display_name if submitter else ''} 提交，待批准", "ref": m.code,
                          "link": f"/catalog/metrics/{m.id}", "at": _iso(v.submitted_at)})
    owned = [d.id for d in session.scalars(select(Domain).where(Domain.owner_id == user.id))]
    for jp in session.scalars(select(JoinPath).where(JoinPath.status == "pending")):
        domains = {a.domain_id for a in (session.get(Asset, jp.left_asset_id), session.get(Asset, jp.right_asset_id))}
        decided = set(session.scalars(select(JoinPathApproval.domain_id).where(JoinPathApproval.join_path_id == jp.id)))
        if any(d in owned and d not in decided for d in domains):
            todos.append({"kind": "join_path", "status": "info", "title": f"关联路径 {jp.name}",
                          "meta": f"基数 {jp.cardinality.upper().replace('N1', 'N:1').replace('1N', '1:N')} · 待你批准",
                          "ref": jp.code, "link": "/catalog/joins", "at": _iso(jp.created_at)})
    if "steward" in roles or is_admin:
        for job in session.scalars(select(ImportJob).where(ImportJob.status.in_(("needs_review", "needs_params")))):
            n = session.scalar(select(func.count()).select_from(CandidateEdge).where(
                CandidateEdge.job_id == job.id, CandidateEdge.status == "pending")) or 0
            title = f"{n} 条模型候选血缘" if job.status == "needs_review" else "动态 SQL 待补参数"
            todos.append({"kind": "import", "status": "warn", "title": title, "meta": f"导入 {job.filename}",
                          "ref": job.id[:8], "link": f"/lineage/import/{job.id}", "at": _iso(job.updated_at)})

    # --- 我负责的指标 ---
    my_metrics = []
    for m in metrics:
        if m.owner_id != user.id and not is_admin:
            continue
        v = statuses.get(m.id)
        week = session.scalar(select(func.count()).select_from(Answer).where(
            Answer.asked_at >= now - timedelta(days=7), Answer.metric_version_ids.contains([v.id]))) if v else 0
        my_metrics.append({"id": m.id, "name": m.name, "code": m.code, "version_no": v.version_no if v else None,
                           "status": v.status if v else "none", "grain": v.grain if v else [],
                           "asks_week": week or 0})

    blocked_stmt = select(Answer).where(Answer.status.in_(("blocked", "denied", "clarify")))
    if not access.is_admin(session, user):
        blocked_stmt = blocked_stmt.where(Answer.asked_by == user.id)  # 答案只给提问人（管理员除外）
    blocked = list(session.scalars(blocked_stmt.order_by(Answer.asked_at.desc()).limit(5)))
    if not (is_admin or "steward" in roles):
        blocked = [a for a in blocked if a.asked_by == user.id]

    recent = []
    for cs in session.scalars(select(ChangeSet).order_by(ChangeSet.created_at.desc()).limit(6)):
        events = list(session.scalars(select(ChangeEvent).where(ChangeEvent.change_set_id == cs.id)))
        primary = next((e for e in events if e.kind not in ("derivation_changed", "identity_link_changed")),
                       events[0] if events else None)
        asset = session.get(Asset, primary.asset_id) if primary and primary.asset_id else None
        recent.append({"id": cs.id, "asset": f"{asset.db_name}.{asset.name}" if asset else "（数据源）",
                       "kind": gettext(EVENT_LABELS.get(primary.kind, primary.kind)) if primary else "",
                       "events": len(events), "impacts": (cs.summary or {}).get("impacts", 0), "status": cs.status,
                       "at": _iso(cs.created_at)})

    return {"stages": stages, "todos": todos, "my_metrics": my_metrics,
            "blocked_answers": [answer_view(a) for a in blocked], "datasources": datasources,
            "cadence": cadence, "recent_changes": recent}
