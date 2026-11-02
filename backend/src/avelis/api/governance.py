"""指标口径、关联路径、变更集、影响与复核。所有状态转换都在后端检查权限并记录真实操作者。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis import events
from avelis.api import schemas as S
from avelis.api.catalog import qualified
from avelis.api.deps import current_user, get_session
from avelis.governance import access, changes, joins, metrics
from avelis.i18n import gettext
from avelis.models import (
    Asset,
    ChangeEvent,
    ChangeSet,
    Impact,
    JoinPath,
    JoinPathApproval,
    Metric,
    MetricVersion,
    MetricVersionTransition,
    ReviewTask,
    User,
)

router = APIRouter(prefix="/api", tags=["口径与变更"])


# --- 指标 -------------------------------------------------------------------------


class MetricCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=1, max_length=128)
    domain_id: str
    owner_id: str | None = None
    aliases: list[str] = Field(default_factory=list)


class Dimension(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    column: str = Field(min_length=1, max_length=255)
    label: str | None = None


class JoinStep(BaseModel):
    alias: str = Field(min_length=1, max_length=32)
    join_path_id: str
    from_: str = Field(default="b", alias="from")
    pre_aggregate: bool = False

    def as_spec(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "join_path_id": self.join_path_id,
            "from": self.from_,
            "pre_aggregate": self.pre_aggregate,
        }


class VersionSpec(BaseModel):
    """基础指标：base_asset_id、expression（聚合表达式）、time_column 必填。
    派生指标：kind="derived"，expression 为引用其他指标编码的公式（如 metric_a / metric_b），其余不填。"""

    kind: str = Field(default="base", pattern=r"^(base|derived)$")
    description: str = Field(min_length=1)
    base_asset_id: str | None = None
    expression: str = Field(min_length=1)
    time_column: str = ""
    timezone: str | None = None
    time_format: Literal["yyyymmdd", "yyyy-mm-dd", "yyyymm"] | None = None
    data_grain: Literal["day", "week", "month", "quarter", "year"] = "day"
    time_aggregation: Literal["sum", "last", "avg"] = "sum"
    value_kind: Literal["level", "ratio", "count"] | None = None
    polarity: Literal["up_good", "down_good"] | None = None
    filters: str | None = None
    dimensions: list[Dimension] = Field(default_factory=list)
    joins: list[JoinStep] = Field(default_factory=list)
    grain: list[str] = Field(default_factory=list)
    dedupe_basis: str | None = None
    unit: str | None = Field(default=None, max_length=16, description="单位，如 元、单、人；比率类可不填")
    unit_scale: int = Field(default=1, ge=1, description="列本身的倍数：列已是“万元”时为 10000")
    material_change_ratio: float | None = Field(
        default=None, gt=0, lt=1, description="变化显著性阈值（相对对比期）：为空时用系统默认 1%"
    )

    @model_validator(mode="after")
    def _base_fields(self) -> "VersionSpec":
        if self.kind == "base" and (not self.base_asset_id or not self.time_column):
            raise ValueError(gettext("基础指标需要 base_asset_id 与 time_column"))
        return self

    def as_spec(self) -> dict[str, Any]:
        data = self.model_dump(exclude={"dimensions", "joins"})
        data["dimensions"] = [d.model_dump() for d in self.dimensions]
        data["joins"] = [j.as_spec() for j in self.joins]
        return data


class Note(BaseModel):
    note: str | None = None


def metric_view(session: Session, metric: Metric) -> dict:
    versions = session.scalars(
        select(MetricVersion).where(MetricVersion.metric_id == metric.id).order_by(MetricVersion.version_no.desc())
    )
    owner = session.get(User, metric.owner_id)
    return {
        "id": metric.id,
        "code": metric.code,
        "name": metric.name,
        "aliases": metric.aliases,
        "domain_id": metric.domain_id,
        "owner": {"id": owner.id, "display_name": owner.display_name},
        "versions": [
            {
                "id": v.id,
                "version_no": v.version_no,
                "status": v.status,
                "status_reason": v.status_reason,
                "unit": v.unit,
                "published_at": v.published_at.isoformat() if v.published_at else None,
            }
            for v in versions
        ],
    }


def version_view(session: Session, version: MetricVersion) -> dict:
    data = metrics.describe_version(session, version)
    data["transitions"] = [
        {"from": t.from_status, "to": t.to_status, "actor_id": t.actor_id, "reason": t.reason, "at": t.at.isoformat()}
        for t in session.scalars(
            select(MetricVersionTransition)
            .where(MetricVersionTransition.metric_version_id == version.id)
            .order_by(MetricVersionTransition.at)
        )
    ]
    data["impacts"] = [
        impact_view(i)
        for i in session.scalars(
            select(Impact).where(Impact.object_type == "metric_version", Impact.object_id == version.id)
        )
    ]
    data["drift"] = metrics.dependency_drift(session, version) if version.dependency_fingerprint else []
    return data


def _visible_metric(session: Session, user: User, metric_id: str) -> Metric:
    metric = metrics.metric_or_404(session, metric_id)
    if metric.domain_id not in access.domain_ids(session, user):
        raise HTTPException(404, "指标不存在或无权访问")
    return metric


@router.get("/metrics", response_model=list[S.MetricOut])
def list_metrics(user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    return [metric_view(session, m) for m in metrics.visible_metrics(session, user)]


@router.post("/metrics", status_code=201, response_model=S.MetricOut)
def create_metric(body: MetricCreate, user: User = Depends(current_user), session: Session = Depends(get_session)):
    m = metrics.create_metric(
        session,
        user,
        code=body.code,
        name=body.name,
        domain_id=body.domain_id,
        owner_id=body.owner_id,
        aliases=body.aliases,
    )
    return metric_view(session, m)


@router.get("/metrics/{metric_id}", response_model=S.MetricOut)
def get_metric(metric_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return metric_view(session, _visible_metric(session, user, metric_id))


@router.post("/metrics/{metric_id}/versions", status_code=201, response_model=S.MetricVersionOut)
def create_version(
    metric_id: str, body: VersionSpec, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    metric = _visible_metric(session, user, metric_id)
    return version_view(session, metrics.create_version(session, user, metric, body.as_spec()))


def _visible_version(session: Session, user: User, version_id: str) -> MetricVersion:
    version = metrics.version_or_404(session, version_id)
    _visible_metric(session, user, version.metric_id)
    return version


@router.get("/metric-versions/{version_id}", response_model=S.MetricVersionOut)
def get_version(version_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    return version_view(session, _visible_version(session, user, version_id))


@router.put("/metric-versions/{version_id}", response_model=S.MetricVersionOut)
def update_version(
    version_id: str, body: VersionSpec, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    version = _visible_version(session, user, version_id)
    return version_view(session, metrics.update_draft(session, user, version, body.as_spec()))


@router.post("/metric-versions/{version_id}/check", response_model=S.PublishCheckOut)
def check_version(version_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """发布前检查（不改变状态）。"""
    return metrics.publish_check(session, _visible_version(session, user, version_id), user)


@router.post("/metric-versions/{version_id}/submit", response_model=S.MetricVersionOut)
def submit_version(version_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    with events.failure_guard("metric_version.submitted", user, "metric_version", version_id, session=session):
        version = _visible_version(session, user, version_id)
        try:
            metrics.submit(session, user, version)
        except metrics.Conflict:
            session.commit()  # 保留现有的检查结果与 submit_blocked 审计；失败事件另行提交
            raise
        result = version_view(session, version)
        session.commit()
        return result


@router.post("/metric-versions/{version_id}/approve", response_model=S.MetricVersionOut)
def approve_version(
    version_id: str, body: Note, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    with events.failure_guard("metric_version.published", user, "metric_version", version_id, session=session):
        version = _visible_version(session, user, version_id)
        result = version_view(session, metrics.approve(session, user, version, body.note))
        session.commit()
        return result


@router.post("/metric-versions/{version_id}/reject", response_model=S.MetricVersionOut)
def reject_version(
    version_id: str, body: Note, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    with events.failure_guard("metric_version.rejected", user, "metric_version", version_id, session=session):
        if not body.note:
            raise HTTPException(422, "驳回必须填写理由")
        version = _visible_version(session, user, version_id)
        result = version_view(session, metrics.reject(session, user, version, body.note))
        session.commit()
        return result


@router.post("/metric-versions/{version_id}/archive", response_model=S.MetricVersionOut)
def archive_version(
    version_id: str, body: Note, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    version = _visible_version(session, user, version_id)
    return version_view(session, metrics.archive(session, user, version, body.note or "归档"))


# --- 关联路径 ---------------------------------------------------------------------


class JoinPathCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=1, max_length=128)
    left_asset_id: str
    left_columns: list[str] = Field(min_length=1)
    right_asset_id: str
    right_columns: list[str] = Field(min_length=1)


class Decision(BaseModel):
    domain_id: str
    decision: str = Field(pattern=r"^(approved|rejected)$")
    note: str | None = None


def join_path_view(session: Session, path: JoinPath) -> dict:
    left, right = session.get(Asset, path.left_asset_id), session.get(Asset, path.right_asset_id)
    creator = session.get(User, path.created_by)
    approvals = session.scalars(select(JoinPathApproval).where(JoinPathApproval.join_path_id == path.id))
    try:
        required = joins.required_domains(session, path)
    except metrics.Conflict as exc:
        required = [exc.message]
    return {
        "id": path.id,
        "code": path.code,
        "name": path.name,
        "status": path.status,
        "source": path.source,
        "created_by": creator.display_name if creator else None,
        "left": {"asset_id": left.id, "qualified": qualified(left), "columns": path.left_columns},
        "right": {"asset_id": right.id, "qualified": qualified(right), "columns": path.right_columns},
        "cardinality": path.cardinality,
        "cardinality_basis": path.cardinality_basis,
        "required_domains": required,
        "approvals": [
            {
                "domain_id": a.domain_id,
                "user_id": a.user_id,
                "decision": a.decision,
                "note": a.note,
                "at": a.at.isoformat(),
            }
            for a in approvals
        ],
    }


@router.get("/join-paths", response_model=list[S.JoinPathOut])
def list_join_paths(user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    allowed = access.domain_ids(session, user)
    return [
        join_path_view(session, p)
        for p in session.scalars(select(JoinPath).order_by(JoinPath.code))
        if access.can_see_object(session, user, "join_path", p.id, allowed)
    ]


@router.post("/join-paths", status_code=201, response_model=S.JoinPathOut)
def create_join_path(
    body: JoinPathCreate, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    path = joins.create(session, user, **body.model_dump())
    return join_path_view(session, path)


@router.post("/join-paths/{path_id}/decisions", response_model=S.JoinPathOut)
def decide_join_path(
    path_id: str, body: Decision, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    if body.decision != "approved":
        path = joins.path_or_404(session, path_id)
        return join_path_view(session, joins.decide(session, user, path, body.domain_id, body.decision, body.note))
    with events.failure_guard("join_path.approved", user, "join_path", path_id, session=session):
        path = joins.path_or_404(session, path_id)
        result = join_path_view(session, joins.decide(session, user, path, body.domain_id, body.decision, body.note))
        session.commit()
        return result


# --- 变更、影响、复核 ---------------------------------------------------------------


def impact_view(i: Impact) -> dict:
    return {
        "id": i.id,
        "change_set_id": i.change_set_id,
        "object_type": i.object_type,
        "object_id": i.object_id,
        "level": i.level,
        "reason": i.reason,
        "path": i.path,
        "created_at": i.created_at.isoformat(),
    }


def task_view(session: Session, t: ReviewTask) -> dict:
    target: dict[str, Any] = {"type": t.object_type, "id": t.object_id}
    if t.object_type == "metric_version":
        v = session.get(MetricVersion, t.object_id)
        m = session.get(Metric, v.metric_id)
        target.update({"metric_code": m.code, "metric_name": m.name, "version_no": v.version_no, "status": v.status})
    return {
        "id": t.id,
        "change_set_id": t.change_set_id,
        "target": target,
        "assignee_id": t.assignee_id,
        "status": t.status,
        "resolution": t.resolution,
        "note": t.note,
        "resolved_by": t.resolved_by,
        "linked_metric_version_id": t.linked_metric_version_id,
        "created_at": t.created_at.isoformat(),
        "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None,
    }


def change_set_view(session: Session, cs: ChangeSet, full: bool, user: User | None = None) -> dict:
    data = {
        "id": cs.id,
        "run_id": cs.run_id,
        "datasource_id": cs.datasource_id,
        "status": cs.status,
        "summary": cs.summary,
        "created_at": cs.created_at.isoformat(),
        "effective_window": {
            "after": cs.window_start.isoformat() if cs.window_start else None,
            "before": cs.window_end.isoformat(),
            "note": gettext("变化发生在上次成功采集之后、本次采集之前；更精确的时间无法确认"),
        },
    }
    if full:
        names = {}
        events = list(session.scalars(select(ChangeEvent).where(ChangeEvent.change_set_id == cs.id)))
        if user is not None:  # 只展示当前用户可见对象的事件、影响与任务
            allowed = access.domain_ids(session, user)
            events = [
                e
                for e in events
                if e.asset_id
                and access.can_see_object(session, user, "asset", e.asset_id, allowed)
                or (e.asset_id is None and access.is_admin(session, user))
            ]
        for e in events:
            if e.asset_id and e.asset_id not in names:
                a = session.get(Asset, e.asset_id)
                names[e.asset_id] = qualified(a) if a else None
        data["events"] = [
            {
                "id": e.id,
                "kind": e.kind,
                "asset_id": e.asset_id,
                "asset": names.get(e.asset_id),
                "column": e.column_name,
                "before": e.before,
                "after": e.after,
                "actor_db_user": e.actor_db_user,
                "attribution_status": e.attribution_status,
                "attribution": e.attribution,
            }
            for e in events
        ]
        data["impacts"] = [
            impact_view(i)
            for i in session.scalars(select(Impact).where(Impact.change_set_id == cs.id))
            if user is None or access.can_see_object(session, user, i.object_type, i.object_id)
        ]
        data["tasks"] = [
            task_view(session, t)
            for t in session.scalars(select(ReviewTask).where(ReviewTask.change_set_id == cs.id))
            if user is None
            or t.assignee_id == user.id
            or access.can_see_object(session, user, t.object_type, t.object_id)
        ]
    return data


@router.get("/change-sets", response_model=list[S.ChangeSetOut])
def list_change_sets(
    status: str | None = None, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> list[dict]:
    stmt = select(ChangeSet).order_by(ChangeSet.created_at.desc()).limit(100)
    if status:
        stmt = stmt.where(ChangeSet.status == status)
    out = []
    for cs in session.scalars(stmt):
        if not access.is_admin(session, user) and not change_set_view(session, cs, full=True, user=user)["events"]:
            continue  # 完全不涉及可见对象的变更集不列出
        out.append(change_set_view(session, cs, full=False))
    return out


@router.get("/change-sets/{change_set_id}", response_model=S.ChangeSetOut)
def get_change_set(change_set_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    cs = session.get(ChangeSet, change_set_id)
    if cs is None:
        raise HTTPException(404, "变更集不存在")
    data = change_set_view(session, cs, full=True, user=user)
    if not data["events"] and not access.is_admin(session, user):
        raise HTTPException(404, "变更集不存在")
    return data


@router.get("/review-tasks", response_model=list[S.TaskOut])
def list_tasks(
    mine: bool = True,
    status: str | None = "open",
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> list[dict]:
    stmt = select(ReviewTask).order_by(ReviewTask.created_at.desc())
    if mine:
        stmt = stmt.where(ReviewTask.assignee_id == user.id)
    if status:
        stmt = stmt.where(ReviewTask.status == status)
    return [
        task_view(session, t)
        for t in session.scalars(stmt)
        if t.assignee_id == user.id or access.can_see_object(session, user, t.object_type, t.object_id)
    ]


class Resolve(BaseModel):
    resolution: str = Field(pattern=r"^(restored|new_version|retired)$")
    note: str = Field(min_length=1)
    linked_metric_version_id: str | None = None


@router.post("/review-tasks/{task_id}/resolve", response_model=S.TaskOut)
def resolve_task(
    task_id: str, body: Resolve, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    task = changes.task_or_404(session, task_id)
    if task.object_type == "metric_version" and body.resolution in ("new_version", "retired"):
        with events.failure_guard(
            "metric_version.invalidated", user, "metric_version", task.object_id, session=session
        ):
            changes.resolve_task(session, user, task, body.resolution, body.note, body.linked_metric_version_id)
            result = task_view(session, task)
            session.commit()
            return result
    changes.resolve_task(session, user, task, body.resolution, body.note, body.linked_metric_version_id)
    return task_view(session, task)
