"""分析智能体接口（M8，ADR 0007）：发起分析、查看步骤与数据集、导出报告、分析任务与定时、站内通知。"""

from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis import jobs, modelcfg
from avelis.analysis import export
from avelis.analysis import service as analysis
from avelis.analysis.dataset_access import require_dataset_access
from avelis.analysis.rerun import rerun_dataset
from avelis.api import schemas as S
from avelis.api.deps import current_user, get_session
from avelis.config import get_settings
from avelis.db import utcnow
from avelis.governance import access, experiences
from avelis.governance.errors import NotFound
from avelis.models import (AnalysisDataset, AnalysisRun, AnalysisTask, Asset, ColumnVersion, Job,
                           Metric, MetricVersion, Notification, User)

router = APIRouter(prefix="/api", tags=["analysis"])


class Window(BaseModel):
    kind: str = Field(description="last_month（上个自然月对比再上个月）或 fixed")
    start: str | None = None
    end: str | None = None
    compare_start: str | None = None
    compare_end: str | None = None


class RunCreate(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    window: Window | None = None
    confirmation_mode: Literal["auto", "always"] = "auto"
    model: S.AnalysisModelIn | None = None
    reasoning_effort: str | None = None


class TaskCreate(BaseModel):
    title: str = ""
    question: str = Field(min_length=2, max_length=2000)
    window: Window | None = None
    schedule_cron: str | None = Field(None, description="5 段 cron（业务时区），如 0 9 1 * *；为空表示只手动运行")


class TaskPatch(BaseModel):
    title: str | None = None
    window: Window | None = None
    schedule_cron: str | None = None
    status: str | None = None
    reason: str | None = None


def _window(w: Window | None) -> dict | None:
    return w.model_dump(exclude_none=True) if w else None


SYNC_WAIT_SECONDS: int | None = None  # 同步入口最长等待；None = 分析时长预算（默认 300 秒）+ 120 秒余量


def _sync_wait_seconds() -> int:
    return SYNC_WAIT_SECONDS if SYNC_WAIT_SECONDS is not None else get_settings().analysis_seconds + 120


@router.get("/analysis/models", response_model=S.AnalysisModelsOut)
def list_analysis_models(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    access.require_role(session, user, "analyst", "steward", "reviewer")
    return modelcfg.analysis_models_view(session)


@router.post("/analysis/runs", status_code=201, response_model=S.AnalysisRunOut)
def create_run(body: RunCreate, wait: bool = False, user: User = Depends(current_user),
               session: Session = Depends(get_session)) -> dict:
    """发起一次分析。wait=true 时在本次请求内执行完再返回（演示与验收用）；否则交给 worker，前端轮询。"""
    analysis.warm_up()
    run = analysis.create_run(session, user, body.question, _window(body.window),
                              confirmation_mode=body.confirmation_mode,
                              model=body.model.model_dump() if body.model else None,
                              reasoning_effort=body.reasoning_effort)
    # 运行与 Job 同一事务提交，之后才执行或唤醒 worker：不会留下没有 Job、恢复器回收不到的分析
    job_id = jobs._enqueue(session, "analysis_run", {"run_id": run.id}, user.id)
    session.commit()
    if wait:  # 在本进程内领取并执行同一个 Job（被 worker 抢先领取时等它结束）；进程中断时由恢复器按 Job 回收
        jobs.run_and_wait(job_id, _sync_wait_seconds())
        session.expire_all()
    else:
        jobs._wake(job_id)
    return analysis.run_view(session, session.get(AnalysisRun, run.id), user=user)


@router.get("/analysis/runs", response_model=list[S.AnalysisRunOut])
def list_runs(user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    runs = session.scalars(select(AnalysisRun).where(AnalysisRun.user_id == user.id)
                           .order_by(AnalysisRun.created_at.desc()).limit(100))
    return [analysis.run_view(session, r, full=False, user=user) for r in runs]


@router.get("/analysis/runs/{run_id}", response_model=S.AnalysisRunOut)
def get_run(run_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return analysis.run_view(session, analysis.visible_run(session, user, run_id), user=user)


@router.post("/analysis/runs/{run_id}/resume", response_model=S.AnalysisRunOut)
def resume_run(run_id: str, body: S.AnalysisResumeIn, user: User = Depends(current_user),
               session: Session = Depends(get_session)) -> dict:
    """确认本次提议并原子领取，重复提交不会创建第二个 Job。"""
    run = analysis.resume_run(session, user, run_id, body.model_dump())
    job_id = run.budget["job_id"]
    session.commit()
    jobs._wake(job_id)
    return analysis.run_view(session, run, user=user)


@router.post("/analysis/runs/{run_id}/messages", status_code=201, response_model=S.AnalysisMessageOut)
def add_message(run_id: str, body: S.AnalysisMessageIn, user: User = Depends(current_user),
                session: Session = Depends(get_session)) -> dict:
    """记录插话；只在下一次模型调用前生效，等待确认期间不消费。"""
    message = analysis.add_message(session, user, run_id, body.text)
    session.commit()
    return analysis.message_view(message)


@router.post("/analysis/runs/{run_id}/stop", response_model=S.AnalysisRunOut)
def stop_run(run_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    """请求在下一个安全点停止；等待确认的运行立即结束。"""
    run = analysis.stop_run(session, user, run_id)
    session.commit()
    return analysis.run_view(session, run, user=user)


@router.post("/analysis/runs/{run_id}/compact", response_model=S.AnalysisRunOut)
def compact_run(run_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    """请求在下一次模型调用前压缩上下文；等待确认期间保留请求。"""
    run = analysis.compact_run(session, user, run_id)
    session.commit()
    return analysis.run_view(session, run, user=user)


@router.post("/analysis/runs/{run_id}/followup", status_code=201, response_model=S.AnalysisRunOut)
def followup_run(run_id: str, body: S.AnalysisFollowupIn, user: User = Depends(current_user),
                 session: Session = Depends(get_session)) -> dict:
    """对已完成报告发起新运行，继承已确认的范围与假设。"""
    analysis.warm_up()
    run = analysis.followup_run(session, user, run_id, body.text, confirmation_mode=body.confirmation_mode,
                                model=body.model.model_dump() if body.model else None,
                                reasoning_effort=body.reasoning_effort)
    job_id = jobs._enqueue(session, "analysis_run", {"run_id": run.id}, user.id)
    session.commit()
    jobs._wake(job_id)
    return analysis.run_view(session, run, user=user)


# --- 个人经验 -----------------------------------------------------------------------

@router.get("/analysis/experiences", response_model=list[S.ExperienceOut])
def list_experiences(user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    return experiences.list_experiences(session, user)


@router.post("/analysis/experiences", status_code=201, response_model=S.ExperienceOut)
def save_experience(body: S.ExperienceCandidate, user: User = Depends(current_user),
                    session: Session = Depends(get_session)) -> dict:
    """仅在用户明确提交保存时持久化。候选确认与长期保存分开。"""
    return experiences.save_experience(session, user, **body.model_dump(exclude_none=True))


@router.post("/analysis/experiences/{experience_id}/revoke", response_model=S.ExperienceOut)
def revoke_experience(experience_id: str, body: S.ExperienceRevokeIn, user: User = Depends(current_user),
                      session: Session = Depends(get_session)) -> dict:
    return experiences.revoke_experience(session, user, experience_id, reason=body.reason)


@router.post("/analysis/experiences/{experience_id}/replace", response_model=S.ExperienceOut)
def replace_experience(experience_id: str, body: S.ExperienceCandidate, user: User = Depends(current_user),
                       session: Session = Depends(get_session)) -> dict:
    return experiences.replace_experience(session, user, experience_id, **body.model_dump(exclude_none=True))


@router.post("/analysis/experiences/{experience_id}/to-join-path", response_model=S.JoinPathOut)
def experience_to_join_path(experience_id: str, user: User = Depends(current_user),
                            session: Session = Depends(get_session)) -> dict:
    from avelis.api.governance import join_path_view

    path = experiences.to_join_path(session, user, experience_id)
    session.commit()  # 先提交再返回：前端收到后立即刷新经验列表
    return join_path_view(session, path)


def _dataset(session: Session, run: AnalysisRun, seq: int) -> AnalysisDataset:
    d = session.scalar(select(AnalysisDataset).where(AnalysisDataset.run_id == run.id, AnalysisDataset.seq == seq))
    if d is None:
        raise NotFound("数据集不存在")
    return d


def _dataset_seq(ref: str) -> int:
    if not re.fullmatch(r"D[1-9][0-9]*", ref):
        raise NotFound("数据集不存在")
    return int(ref[1:])


def _dataset_detail(d: AnalysisDataset, *, offset: int = 0, limit: int = 100) -> dict[str, Any]:
    meta = d.meta or {}
    source = meta.get("rerun_of")
    return {"ref": f"D{d.seq}", "kind": d.kind, "title": d.title, "columns": d.columns,
            "row_count": len(d.rows), "truncated": d.truncated, "inputs": d.inputs,
            "sql": meta.get("sql"), "metric_version_ids": meta.get("metric_version_ids", []),
            "metric_codes": meta.get("metric_codes", []), "window": meta.get("window", {}),
            "time_column": meta.get("time_column"), "units": meta.get("units", {}),
            "summary_units": meta.get("summary_units", {}), "purpose": meta.get("purpose"),
            "answer_id": d.answer_id, "created_at": d.created_at.isoformat(),
            "explore": bool(meta.get("explore")), "assumption": bool(meta.get("assumption")),
            "partial": bool(meta.get("partial")), "rerunnable": d.kind == "governed" and bool(meta.get("intent")),
            "source": {"run_id": source["run_id"], "ref": source["ref"]} if source else None,
            "meta": meta, "offset": offset, "limit": limit, "rows": d.rows[offset:offset + limit],
            "total": len(d.rows)}


@router.get("/analysis/runs/{run_id}/datasets/{ref}",
            response_model=S.AnalysisDatasetDetailOut | S.AnalysisDatasetLegacyOut)
def get_dataset(run_id: str, ref: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000),
                user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict[str, Any]:
    """D 编号返回分页详情；数字序号保留原有全行结构。"""
    legacy = bool(re.fullmatch(r"[0-9]+", ref))
    seq = int(ref) if legacy else _dataset_seq(ref)
    d = _dataset(session, analysis.visible_run(session, user, run_id), seq)
    require_dataset_access(session, user, d)
    if legacy:
        return {"ref": f"D{d.seq}", "kind": d.kind, "title": d.title, "columns": d.columns, "rows": d.rows,
                "truncated": d.truncated, "meta": d.meta, "inputs": d.inputs, "answer_id": d.answer_id}
    return _dataset_detail(d, offset=offset, limit=limit)


@router.post("/analysis/runs/{run_id}/datasets/{ref}/rerun", response_model=S.AnalysisDatasetDetailOut)
def rerun(run_id: str, ref: str, body: S.AnalysisDatasetRerunIn, user: User = Depends(current_user),
          session: Session = Depends(get_session)) -> dict[str, Any]:
    run = analysis.visible_run(session, user, run_id)
    d = _dataset(session, run, _dataset_seq(ref))
    result = rerun_dataset(session, user, run, d, body.window.model_dump(mode="json", exclude_none=True))
    session.commit()
    return _dataset_detail(result)


@router.get("/analysis/runs/{run_id}/datasets/{seq}/csv")
def dataset_csv(run_id: str, seq: int, user: User = Depends(current_user),
                session: Session = Depends(get_session)) -> Response:
    d = _dataset(session, analysis.visible_run(session, user, run_id), seq)
    require_dataset_access(session, user, d)
    return Response(export.to_csv(d.columns, d.rows), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="analysis-{run_id}-D{d.seq}.csv"'})


def _export_metadata(session: Session, user: User, meta: dict) -> dict:
    """只为本次导出补齐名称，沿用数据集保存的版本，不改写历史元数据。"""
    versions = meta.get("versions") or {}
    version_ids = list(versions.values()) or meta.get("metric_version_ids") or []
    metric_codes = meta.get("metric_codes") or list(versions)
    labels: dict[str, Any] = {"metric_names": [], "dimensions": [],
                              "column_docs": dict(meta.get("column_docs") or {})}
    can_see = None
    for version_id in dict.fromkeys(version_ids):
        version = session.get(MetricVersion, version_id)
        metric = session.get(Metric, version.metric_id) if version else None
        if metric is None or (metric_codes and metric.code not in metric_codes):
            continue
        if metric.name not in labels["metric_names"]:
            labels["metric_names"].append(metric.name)
        for dimension in version.dimensions or []:
            labels["dimensions"].append(dict(dimension))
            name, ref = dimension.get("name"), dimension.get("column") or ""
            if not name or not ref or labels["column_docs"].get(name, {}).get("comment"):
                continue
            alias, column = ref.split(".", 1) if "." in ref else ("b", ref)
            dependency = next((d for d in version.dependency or []
                               if d.get("role") == "dimension" and d.get("alias") == alias
                               and d.get("column") == column), None)
            if dependency is None:
                continue
            asset_id = dependency.get("asset_id")
            snapshot = (version.asset_versions or {}).get(asset_id) or {}
            if not snapshot.get("version_id"):
                continue
            asset = session.get(Asset, asset_id)
            if can_see is None:
                from avelis.governance.column_docs import visible_to

                can_see = visible_to(session, user)
            if asset is None or not can_see(asset):
                continue
            comment = session.scalar(select(ColumnVersion.comment).where(
                ColumnVersion.asset_version_id == snapshot["version_id"], ColumnVersion.name == column))
            if comment and comment.strip() and comment.strip().lower() != column.lower():
                labels["column_docs"][name] = {"comment": comment.strip()}
    return {**meta, "export_labels": labels}


@router.get("/analysis/runs/{run_id}/export")
def export_run(run_id: str, format: str = "pdf", audience: Literal["brief", "full"] | None = None,
               appendix: bool | None = None, datasets: str | None = None,
               user: User = Depends(current_user),
               session: Session = Depends(get_session)) -> Response:
    run = analysis.visible_run(session, user, run_id)
    if run.status != "succeeded":
        raise HTTPException(409, "分析尚未完成，不能导出")
    resolved_audience = export.resolve_audience(audience, appendix)
    view = analysis.run_view(session, run, user=user)
    saved = {f"D{d.seq}": d for d in session.scalars(
        select(AnalysisDataset).where(AnalysisDataset.run_id == run.id))}
    selected = None
    if datasets is not None:
        selected = list(dict.fromkeys(ref.strip() for ref in datasets.split(",")))
        if any(not re.fullmatch(r"D[1-9][0-9]*", ref) or ref not in saved for ref in selected):
            raise HTTPException(422, "datasets 只能包含本次运行已有的 D 编号，以逗号分隔")
    if format in ("md", "pdf", "html"):
        for dataset in view["datasets"]:
            if dataset.get("restricted"):
                continue
            d = saved[dataset["ref"]]
            dataset.update(rows=d.rows, meta=_export_metadata(session, user, d.meta))
    if format == "md":
        return Response(export.to_markdown(view, audience=resolved_audience, datasets=selected).encode("utf-8"),
                        media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="analysis-{run_id}.md"'})
    if format == "pdf":
        return Response(export.to_pdf(view, audience=resolved_audience, datasets=selected), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="analysis-{run_id}.pdf"'})
    if format == "html":
        return Response(export.to_html(view, audience=resolved_audience, datasets=selected).encode("utf-8"),
                        media_type="text/html; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="analysis-{run_id}.html"'})
    if format == "csv":  # 引用来源表：每个数字的数据集、取值路径、格式与原值
        return Response(export.sources_csv(view), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="analysis-{run_id}-sources.csv"'})
    raise HTTPException(422, "format 只能是 pdf、html、md 或 csv")


# --- 任务 ---------------------------------------------------------------------------

def _task(session: Session, user: User, task_id: str) -> AnalysisTask:
    from avelis.governance import access

    t = session.get(AnalysisTask, task_id)
    if t is None or (t.owner_id != user.id and not access.is_admin(session, user)):
        raise NotFound("分析任务不存在或不属于当前用户")
    return t


@router.post("/analysis/tasks", status_code=201, response_model=S.AnalysisTaskOut)
def create_task(body: TaskCreate, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return analysis.task_view(analysis.create_task(session, user, body.title, body.question, _window(body.window),
                                                   body.schedule_cron))


@router.get("/analysis/tasks", response_model=list[S.AnalysisTaskOut])
def list_tasks(user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    return [analysis.task_view(t) for t in session.scalars(
        select(AnalysisTask).where(AnalysisTask.owner_id == user.id).order_by(AnalysisTask.created_at.desc()))]


@router.patch("/analysis/tasks/{task_id}", response_model=S.AnalysisTaskOut)
def patch_task(task_id: str, body: TaskPatch, user: User = Depends(current_user),
               session: Session = Depends(get_session)) -> dict:
    changes = body.model_dump(exclude_unset=True)
    if "window" in changes:
        changes["window"] = _window(body.window)
    return analysis.task_view(analysis.update_task(session, user, _task(session, user, task_id), changes))


@router.post("/analysis/tasks/{task_id}/run", response_model=S.AnalysisRunOut)
def run_task_now(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    """立即运行一次（与定时同一路径：重新检查发起人权限，完成后发站内通知）。在本次请求内执行。"""
    task = _task(session, user, task_id)
    job_id = jobs._enqueue(session, "analysis", {"task_id": task.id, "trigger": "manual"}, user.id)
    session.commit()
    status = jobs.run_and_wait(job_id, _sync_wait_seconds())  # 与定时同一路径，在本进程内领取并执行
    session.expire_all()
    job = session.get(Job, job_id)
    result = job.result or {}
    run_id = result.get("analysis_run_id")
    if run_id is None:
        if status not in jobs.TERMINAL:
            raise HTTPException(409, "运行尚未开始（已由后台 worker 领取），请稍后在任务页查看")
        task_now = session.get(AnalysisTask, task.id)
        reason = result.get("reason") or job.error or (task_now.paused_reason if task_now else None) or "未知原因"
        label = "任务已暂停" if result.get("outcome") == "paused" else "未运行"
        raise HTTPException(409, f"{label}：{reason}")
    return analysis.run_view(session, session.get(AnalysisRun, run_id), user=user)


@router.get("/analysis/tasks/{task_id}/runs", response_model=list[S.AnalysisRunOut])
def task_runs(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    task = _task(session, user, task_id)
    return [analysis.run_view(session, r, full=False, user=user) for r in session.scalars(
        select(AnalysisRun).where(AnalysisRun.task_id == task.id).order_by(AnalysisRun.created_at.desc()).limit(50))]


# --- 通知 ---------------------------------------------------------------------------

@router.get("/notifications", response_model=list[S.NotificationOut])
def list_notifications(unread: bool = False, user: User = Depends(current_user),
                       session: Session = Depends(get_session)) -> list[dict]:
    stmt = select(Notification).where(Notification.user_id == user.id)
    if unread:
        stmt = stmt.where(Notification.read_at.is_(None))
    return [{"id": n.id, "kind": n.kind, "title": n.title, "body": n.body, "link": n.link,
             "read_at": n.read_at.isoformat() if n.read_at else None, "created_at": n.created_at.isoformat()}
            # 未读优先：未读数与列表里能看到、能标为已读的条目一致
            for n in session.scalars(stmt.order_by(Notification.read_at.is_(None).desc(), Notification.created_at.desc())
                                     .limit(100))]


@router.post("/notifications/{notification_id}/read", status_code=204)
def read_notification(notification_id: str, user: User = Depends(current_user),
                      session: Session = Depends(get_session)) -> Response:
    n = session.get(Notification, notification_id)
    if n is None or n.user_id != user.id:
        raise NotFound("通知不存在")
    n.read_at = n.read_at or utcnow()
    return Response(status_code=204)
