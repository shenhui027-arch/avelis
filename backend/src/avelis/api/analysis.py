"""分析智能体接口（M8，ADR 0007）：发起分析、查看步骤与数据集、导出报告、分析任务与定时、站内通知。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis import jobs
from avelis.analysis import export
from avelis.analysis import service as analysis
from avelis.api import schemas as S
from avelis.api.deps import current_user, get_session
from avelis.db import utcnow
from avelis.governance.errors import NotFound
from avelis.models import AnalysisDataset, AnalysisRun, AnalysisTask, Notification, User

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


@router.post("/analysis/runs", status_code=201, response_model=S.AnalysisRunOut)
def create_run(body: RunCreate, wait: bool = False, user: User = Depends(current_user),
               session: Session = Depends(get_session)) -> dict:
    """发起一次分析。wait=true 时在本次请求内执行完再返回（演示与验收用）；否则交给 worker，前端轮询。"""
    analysis.warm_up()
    run = analysis.create_run(session, user, body.question, _window(body.window))
    if wait:
        analysis.execute(session, run)
    else:  # 运行与 Job 同一事务提交，提交后再唤醒 worker：不会留下没有 Job 的排队分析
        job_id = jobs._enqueue(session, "analysis_run", {"run_id": run.id}, user.id)
        session.commit()
        jobs._wake(job_id)
    return analysis.run_view(session, run)


@router.get("/analysis/runs", response_model=list[S.AnalysisRunOut])
def list_runs(user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    runs = session.scalars(select(AnalysisRun).where(AnalysisRun.user_id == user.id)
                           .order_by(AnalysisRun.created_at.desc()).limit(100))
    return [analysis.run_view(session, r, full=False) for r in runs]


@router.get("/analysis/runs/{run_id}", response_model=S.AnalysisRunOut)
def get_run(run_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return analysis.run_view(session, analysis.visible_run(session, user, run_id))


def _dataset(session: Session, run: AnalysisRun, seq: int) -> AnalysisDataset:
    d = session.scalar(select(AnalysisDataset).where(AnalysisDataset.run_id == run.id, AnalysisDataset.seq == seq))
    if d is None:
        raise NotFound("数据集不存在")
    return d


@router.get("/analysis/runs/{run_id}/datasets/{seq}")
def get_dataset(run_id: str, seq: int, user: User = Depends(current_user),
                session: Session = Depends(get_session)) -> dict[str, Any]:
    d = _dataset(session, analysis.visible_run(session, user, run_id), seq)
    return {"ref": f"D{d.seq}", "kind": d.kind, "title": d.title, "columns": d.columns, "rows": d.rows,
            "truncated": d.truncated, "meta": d.meta, "inputs": d.inputs, "answer_id": d.answer_id}


@router.get("/analysis/runs/{run_id}/datasets/{seq}/csv")
def dataset_csv(run_id: str, seq: int, user: User = Depends(current_user),
                session: Session = Depends(get_session)) -> Response:
    d = _dataset(session, analysis.visible_run(session, user, run_id), seq)
    return Response(export.to_csv(d.columns, d.rows), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="analysis-{run_id}-D{d.seq}.csv"'})


@router.get("/analysis/runs/{run_id}/export")
def export_run(run_id: str, format: str = "pdf", user: User = Depends(current_user),
               session: Session = Depends(get_session)) -> Response:
    run = analysis.visible_run(session, user, run_id)
    if run.status != "succeeded":
        raise HTTPException(409, "分析尚未完成，不能导出")
    view = analysis.run_view(session, run)
    if format == "md":
        return Response(export.to_markdown(view).encode("utf-8"), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="analysis-{run_id}.md"'})
    if format == "pdf":
        return Response(export.to_pdf(view), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="analysis-{run_id}.pdf"'})
    raise HTTPException(422, "format 只能是 pdf 或 md")


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
    session.commit()
    out = analysis.run_task(task.id, trigger="manual")
    session.expire_all()
    if out.get("analysis_run_id") is None:
        raise HTTPException(409, f"任务已暂停：{session.get(AnalysisTask, task.id).paused_reason or out.get('reason')}")
    return analysis.run_view(session, session.get(AnalysisRun, out["analysis_run_id"]))


@router.get("/analysis/tasks/{task_id}/runs", response_model=list[S.AnalysisRunOut])
def task_runs(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    task = _task(session, user, task_id)
    return [analysis.run_view(session, r, full=False) for r in session.scalars(
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
            for n in session.scalars(stmt.order_by(Notification.created_at.desc()).limit(100))]


@router.post("/notifications/{notification_id}/read", status_code=204)
def read_notification(notification_id: str, user: User = Depends(current_user),
                      session: Session = Depends(get_session)) -> Response:
    n = session.get(Notification, notification_id)
    if n is None or n.user_id != user.id:
        raise NotFound("通知不存在")
    n.read_at = n.read_at or utcnow()
    return Response(status_code=204)
