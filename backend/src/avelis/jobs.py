"""后台任务：事实来源是 job 表，Redis 列表只负责唤醒 worker。

Redis 丢失时 worker 会定期扫描 job 表中排队过久的任务，任务不会丢；同一数据源的采集用 PostgreSQL 锁互斥。
worker 在执行中退出时，任务会停在 running：超过 AVELIS_WORKER_RUNNING_TIMEOUT_SECONDS 仍未结束的任务如实记为失败
（不自动重跑），否则该数据源的定时采集会因“已有任务在跑”而一直被跳过。
定时采集：worker 按每个数据源的 collect_interval_minutes 轮询入队。
定时分析（M8）：到期的分析任务入队 analysis；单次分析为 analysis_run（与运行在同一事务中创建）。
wait=true 与“立即运行”同样创建 Job，在请求进程内领取并执行，中断时恢复器一样能回收。
分析类 Job 的执行与终态由 analysis.service 负责：执行权是 Job 的本次尝试（attempts），运行与 Job 的终态同一事务提交；
Job 的终态跟随分析的业务结果（失败 → failed，暂停或跳过 → skipped）。
Job 被判定放弃时，关联的分析运行一并处理（analysis.service.reconcile_abandoned）。
保留期清理：worker 每 AVELIS_HISTORY_PURGE_INTERVAL_HOURS 小时清理一次超期历史（ADR 0008）。
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from datetime import timedelta, timezone
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


ANALYSIS_KINDS = ("analysis", "analysis_run")
TERMINAL = ("succeeded", "failed", "skipped")


def run_and_wait(job_id: str, timeout_seconds: float) -> str | None:
    """同步入口：在本进程内领取并执行 Job；若已被扫描 worker 抢先领取，等它到终态（或超时）再返回 Job 状态。"""
    run_job(job_id)
    deadline = time.monotonic() + timeout_seconds
    while True:
        with session_scope() as session:
            status = session.scalar(select(Job.status).where(Job.id == job_id))
        if status in TERMINAL or status is None or time.monotonic() > deadline:
            return status
        time.sleep(1)


def run_job(job_id: str) -> None:
    with session_scope() as session:
        claimed = session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "queued")
            .values(status="running", started_at=utcnow(), attempts=Job.attempts + 1)
        ).rowcount
        if not claimed:
            return
        job = session.get(Job, job_id)
        kind, payload, actor, attempt = job.kind, dict(job.payload), job.created_by, job.attempts
    if kind in ANALYSIS_KINDS:
        _run_analysis_job(job_id, attempt, kind, payload)
        return
    result: dict[str, Any] = {}
    error = None
    try:
        if kind == "collect":
            result = {
                "run_id": collect_now(
                    payload["datasource_id"], trigger=payload.get("trigger", "manual"), actor_id=actor
                )
            }
        elif kind == "import":
            from avelis.importer.batch import run_import

            result = run_import(payload["batch_id"], job_id=job_id, attempt=attempt)
        else:
            raise ValueError(f"未知任务类型 {kind}")
    except Exception as exc:  # noqa: BLE001 - 任务失败如实记录
        log.exception("任务 %s 失败", job_id)
        error = f"{type(exc).__name__}: {exc}"
    with session_scope() as session:
        job = session.get(Job, job_id)
        result = {**(job.result or {}), **result} if job is not None else result  # 保留执行中写入的关联
        session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "running", Job.attempts == attempt)
            .values(status="failed" if error else "succeeded", error=error, result=result, finished_at=utcnow())
        )


def _run_analysis_job(job_id: str, attempt: int, kind: str, payload: dict[str, Any]) -> None:
    """分析类 Job：终态由分析服务与运行在同一事务中写入。意外异常时（仍持有本次尝试才）把 Job 记为失败，
    并按恢复器同样的规则处理关联的运行，不留下 running 的运行。"""
    from avelis.analysis import service as analysis_service

    try:
        analysis_service.run_job(job_id, attempt, kind, payload)
    except Exception as exc:  # noqa: BLE001 - 如实记录
        log.exception("分析任务 %s 失败", job_id)
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
        with session_scope() as session:
            job = session.scalar(
                select(Job).where(Job.id == job_id, Job.status == "running", Job.attempts == attempt).with_for_update()
            )
            if job is not None:
                job.status, job.error, job.finished_at = "failed", error, utcnow()
                analysis_service.reconcile_abandoned(session, job, error)


def collect_now(datasource_id: str, *, trigger: str, actor_id: str | None) -> str:
    with advisory_lock(f"collect:{datasource_id}"):
        return run_collection(datasource_id, trigger=trigger, actor_id=actor_id)


def schedule_due() -> list[str]:
    """为到期的数据源入队定时采集。"""
    queued = []
    with session_scope() as session:
        # 固定加锁顺序，避免多个调度器以相反顺序持有事务锁。
        for ds in session.scalars(select(Datasource).where(Datasource.status == "active").order_by(Datasource.id)):
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"schedule:collect:{ds.id}"}
            )
            last = session.scalar(
                select(CollectionRun.started_at)
                .where(CollectionRun.datasource_id == ds.id)
                .order_by(CollectionRun.started_at.desc())
                .limit(1)
            )
            pending = session.scalar(
                select(Job.id)
                .where(
                    Job.kind == "collect",
                    Job.status.in_(("queued", "running")),
                    Job.payload["datasource_id"].as_string() == ds.id,
                )
                .limit(1)
            )
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
    older_than_seconds = (
        get_settings().worker_running_timeout_seconds if older_than_seconds is None else older_than_seconds
    )
    cutoff = utcnow() - timedelta(seconds=older_than_seconds)
    reason = f"运行超过 {older_than_seconds} 秒未结束，执行它的 worker 可能已退出；未自动重跑"
    with session_scope() as session:
        rows = list(
            session.execute(
                update(Job)
                .where(Job.status == "running", Job.started_at < cutoff)
                .values(status="failed", finished_at=utcnow(), error=reason)
                .returning(Job.id, Job.kind, Job.payload, Job.result)
            )
        )
        for job_id, kind, _, _ in rows:  # 关联的分析运行一并处理：未结束的终结，已结束的按运行终态修正 Job
            if kind in ANALYSIS_KINDS:
                from avelis.analysis import service as analysis_service

                analysis_service.reconcile_abandoned(session, session.get(Job, job_id, populate_existing=True), reason)
    ids = [row[0] for row in rows]
    for job_id in ids:
        log.warning("任务 %s 运行超时，已记为失败", job_id)
    return ids


def purge_history_if_due() -> None:
    from avelis.history import retention

    try:
        if retention.purge_due():
            result = retention.purge()
            log.info("保留期清理：%s", {k: result.get(k) for k in ("status", "cutoff", "counts")})
    except retention.PurgeRefused as exc:
        log.error("历史哈希链核验未通过，未清理：%s", exc.report["first_break"])
    except Exception:  # noqa: BLE001 - 清理失败不影响采集
        log.exception("保留期清理失败")


def _guarded(name: str, fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - 记录后继续：一次扫描失败（如死锁被中止）不能让 worker 主循环退出
        log.exception("%s 失败，下一轮重试", name)
        return default


def scan_once() -> None:
    """一轮定时扫描：回收超时任务、入队到期任务、执行排队过久的任务、清理超期历史。每一项单独容错。"""
    from avelis.analysis import service as analysis_service

    _guarded("回收超时任务", abandoned_jobs)
    due = [
        *_guarded("定时采集入队", schedule_due, []),
        *_guarded("定时分析入队", analysis_service.schedule_due, []),
        *_guarded("扫描排队任务", stale_jobs, []),
    ]
    for job_id in due:
        _guarded(f"执行任务 {job_id}", lambda job_id=job_id: run_job(job_id))
    _guarded("清理超期历史", purge_history_if_due)


def main() -> None:
    from avelis.config import ConfigError, report_config_error

    try:
        get_settings()
    except ConfigError as exc:
        report_config_error(exc)
        raise SystemExit(2) from None
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)
    heartbeat_stop = threading.Event()
    heartbeat_ready = threading.Event()

    def _heartbeat():
        while True:
            _guarded(
                "写入 worker 心跳",
                lambda: cache.client().set(
                    cache.WORKER_HEARTBEAT, utcnow().replace(tzinfo=timezone.utc).isoformat(), ex=60
                ),
            )
            heartbeat_ready.set()
            if heartbeat_stop.wait(20):
                break

    heartbeat_thread = threading.Thread(target=_heartbeat, daemon=True)
    heartbeat_thread.start()
    last_schedule = 0.0
    log.info("worker 已启动")
    try:
        heartbeat_ready.wait()  # 首次心跳写入尝试完成后，才开始可能耗时的扫描。
        while not stop:
            if time.monotonic() - last_schedule > get_settings().worker_schedule_seconds:
                last_schedule = time.monotonic()
                scan_once()
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
                except Exception:  # noqa: BLE001 - 单个任务的意外错误不能让 worker 退出
                    log.exception("任务 %s 执行异常", item[1])
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1)
        _guarded("删除 worker 心跳", lambda: cache.client().delete(cache.WORKER_HEARTBEAT))
    log.info("worker 已停止")


if __name__ == "__main__":
    sys.exit(main())
