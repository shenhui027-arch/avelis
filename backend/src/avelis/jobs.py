"""后台任务：事实来源是 job 表，Redis 列表只负责唤醒 worker。

Redis 丢失时 worker 会定期扫描 job 表中排队过久的任务，任务不会丢；同一数据源的采集用 PostgreSQL 锁互斥。
worker 在执行中退出时，任务会停在 running：超过 AVELIS_WORKER_RUNNING_TIMEOUT_SECONDS 仍未结束的任务如实记为失败
（不自动重跑），否则该数据源的定时采集会因“已有任务在跑”而一直被跳过。
定时采集：worker 按每个数据源的 collect_interval_minutes 轮询入队。
定时分析（M8）：到期的分析任务入队 analysis；API 异步发起的单次分析为 analysis_run（与运行在同一事务中创建）。
Job 的终态跟随分析的业务结果：分析失败 → failed，任务被暂停或跳过 → skipped，不把失败的分析记为成功。
Job 被判定放弃时，同步终结关联的分析运行（payload.run_id 或 result.analysis_run_id）。
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from avelis import cache
from avelis.collect.service import CollectionFailed, run_collection
from avelis.config import get_settings
from avelis.db import advisory_lock, session_scope, utcnow
from avelis.models import CollectionRun, Datasource, Job

log = logging.getLogger("avelis.worker")


def _enqueue(session: Session, kind: str, payload: dict[str, Any], actor_id: str | None) -> str:
    job = Job(kind=kind, payload=payload, status="queued", created_by=actor_id)
    session.add(job)
    session.flush()
    return job.id


def _wake(job_id: str) -> None:
    try:
        cache.client().lpush(cache.JOB_QUEUE, job_id)
    except Exception:  # Redis 不可用：worker 会从 job 表扫描到
        log.warning("Redis 不可用，任务 %s 等待 worker 扫描", job_id)


def enqueue(kind: str, payload: dict[str, Any], actor_id: str | None) -> str:
    with session_scope() as session:
        job_id = _enqueue(session, kind, payload, actor_id)
    _wake(job_id)
    return job_id


def run_job(job_id: str) -> None:
    with session_scope() as session:
        claimed = session.execute(update(Job).where(Job.id == job_id, Job.status == "queued").values(
            status="running", started_at=utcnow(), attempts=Job.attempts + 1)).rowcount
        if not claimed:
            return
        job = session.get(Job, job_id)
        kind, payload, actor, attempt = job.kind, dict(job.payload), job.created_by, job.attempts
    result: dict[str, Any] = {}
    error, outcome = None, "succeeded"
    try:
        if kind == "collect":
            result = {"run_id": collect_now(payload["datasource_id"], trigger=payload.get("trigger", "manual"),
                                            actor_id=actor)}
        elif kind == "analysis":
            from avelis.analysis import service as analysis_service

            out = analysis_service.run_task(payload["task_id"], trigger=payload.get("trigger", "schedule"),
                                            job_id=job_id)
            result = {k: v for k, v in out.items() if k != "status"} | {"outcome": out["status"]}
            outcome = {"succeeded": "succeeded", "failed": "failed"}.get(out["status"], "skipped")
            if outcome == "failed":
                error = out.get("reason") or "分析失败"
        elif kind == "analysis_run":
            from avelis.analysis import service as analysis_service
            from avelis.models import AnalysisRun

            with session_scope() as session:
                run = session.get(AnalysisRun, payload["run_id"])
                if run is None or run.status != "queued":
                    outcome = "skipped"
                    result = {"analysis_run_id": payload["run_id"], "reason": "分析不存在或已不在排队状态"}
                else:
                    analysis_service.execute(session, run)
                    result = {"analysis_run_id": run.id, "status": run.status}
                    if run.status != "succeeded":
                        outcome, error = "failed", run.error or "分析失败"
        else:
            raise ValueError(f"未知任务类型 {kind}")
    except Exception as exc:  # noqa: BLE001 - 任务失败如实记录
        error = f"{type(exc).__name__}: {exc}"
    with session_scope() as session:
        job = session.get(Job, job_id)
        result = {**(job.result or {}), **result} if job is not None else result  # 保留执行中写入的关联
        session.execute(update(Job).where(Job.id == job_id, Job.status == "running", Job.attempts == attempt).values(
            status="failed" if error else outcome, error=error, result=result, finished_at=utcnow()))


def collect_now(datasource_id: str, *, trigger: str, actor_id: str | None) -> str:
    with advisory_lock(f"collect:{datasource_id}"):
        return run_collection(datasource_id, trigger=trigger, actor_id=actor_id)


def schedule_due() -> list[str]:
    """为到期的数据源入队定时采集。"""
    queued = []
    with session_scope() as session:
        # 固定加锁顺序，避免多个调度器以相反顺序持有事务锁。
        for ds in session.scalars(select(Datasource).where(Datasource.status == "active").order_by(Datasource.id)):
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                            {"key": f"schedule:collect:{ds.id}"})
            last = session.scalar(select(CollectionRun.started_at).where(CollectionRun.datasource_id == ds.id)
                                  .order_by(CollectionRun.started_at.desc()).limit(1))
            pending = session.scalar(select(Job.id).where(Job.kind == "collect", Job.status.in_(("queued", "running")),
                                                          Job.payload["datasource_id"].as_string() == ds.id).limit(1))
            if pending:
                continue
            if last is None or utcnow() - last.replace(tzinfo=None) >= timedelta(minutes=ds.collect_interval_minutes):
                queued.append(_enqueue(session, "collect", {"datasource_id": ds.id, "trigger": "schedule"}, None))
    for job_id in queued:
        _wake(job_id)
    return queued


def stale_jobs(older_than_seconds: int | None = None) -> list[str]:
    older_than_seconds = get_settings().worker_stale_seconds if older_than_seconds is None else older_than_seconds
    cutoff = utcnow() - timedelta(seconds=older_than_seconds)
    with session_scope() as session:
        return list(session.scalars(select(Job.id).where(Job.status == "queued", Job.created_at < cutoff)))


def abandoned_jobs(older_than_seconds: int | None = None) -> list[str]:
    """把运行超时的任务记为失败，返回本次更新的任务 ID。"""
    older_than_seconds = (get_settings().worker_running_timeout_seconds if older_than_seconds is None
                          else older_than_seconds)
    cutoff = utcnow() - timedelta(seconds=older_than_seconds)
    reason = f"运行超过 {older_than_seconds} 秒未结束，执行它的 worker 可能已退出；未自动重跑"
    with session_scope() as session:
        rows = list(session.execute(update(Job).where(Job.status == "running", Job.started_at < cutoff).values(
            status="failed", finished_at=utcnow(), error=reason).returning(Job.id, Job.kind, Job.payload, Job.result)))
        from avelis.analysis import service as analysis_service

        for _, kind, payload, result in rows:  # 关联的分析运行一并终结，不留下永远排队或运行中的分析
            if kind == "analysis_run":
                analysis_service.finalize_abandoned(session, (payload or {}).get("run_id"), reason)
            elif kind == "analysis":
                analysis_service.finalize_abandoned(session, (result or {}).get("analysis_run_id"), reason)
    ids = [row[0] for row in rows]
    for job_id in ids:
        log.warning("任务 %s 运行超时，已记为失败", job_id)
    return ids


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)
    last_schedule = 0.0
    log.info("worker 已启动")
    while not stop:
        if time.monotonic() - last_schedule > get_settings().worker_schedule_seconds:
            last_schedule = time.monotonic()
            abandoned_jobs()
            from avelis.analysis import service as analysis_service

            for job_id in schedule_due() + analysis_service.schedule_due() + stale_jobs():
                run_job(job_id)
        try:
            item = cache.client().brpop([cache.JOB_QUEUE], timeout=min(5, get_settings().worker_schedule_seconds))
        except Exception:
            item = None
            time.sleep(5)
        if item:
            try:
                run_job(item[1])
            except CollectionFailed:
                pass
    log.info("worker 已停止")


if __name__ == "__main__":
    sys.exit(main())
