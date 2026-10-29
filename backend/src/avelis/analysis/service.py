"""分析运行、分析任务与定时（ADR 0007 第 2、7、11 点）。

- 分析需要真实模型（支持工具调用）且允许向模型发送查询结果；两者缺一时如实拒绝，不退化为“看不到数的分析”。
- 每次运行以发起人身份执行：开始时核对账号与查询角色，之后每次工具调用都按发起人当前的权限重新核对。
- 每次运行都经过 Job（异步由 worker 领取；wait=true 与“立即运行”在同一进程内领取并执行），恢复器只需扫描 Job。
- 执行权 = Job 的本次尝试（job_id:attempts，写在 run.budget.executor）：领取、每一步提交、终态写入都带执行权条件，
  失去执行权的执行者不能复活或改写运行。运行与 Job 的终态（及任务的暂停、通知）在同一事务提交；
  恢复器遇到运行已是终态时按运行的终态修正 Job。
- 定时任务按 cron（业务时区）由 worker 入队；消费时在任务锁下重新检查任务状态，已暂停的任务跳过。
  发起人停用、失去查询角色，或执行中被明确拒绝访问所需的口径或来源时，任务自动暂停并发站内通知。
"""

from __future__ import annotations

import calendar
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from avelis import audit, modelcfg
from avelis.analysis import agent as analysis_agent
from avelis.analysis.tools import AnalysisToolbox, Budget, PermissionLost
from avelis.ask import service as ask_service
from avelis.ask.intent import business_today
from avelis.config import get_settings
from avelis.db import session_scope, utcnow
from avelis.governance import access
from avelis.governance.errors import Conflict, Forbidden, Invalid, NotFound
from avelis.models import AnalysisRun, AnalysisTask, Notification, User

log = logging.getLogger("avelis.analysis")
_GRAPH: dict[str, Any] = {}


def reset_graph_cache() -> None:
    _GRAPH.clear()


def _graph():
    from avelis.ask import agent as ask_agent

    saver = ask_agent.checkpointer()
    key = str(id(saver))
    if key not in _GRAPH:
        _GRAPH.clear()
        _GRAPH[key] = analysis_agent.build_graph(saver)
    return _GRAPH[key]


# --- 时间窗口 -----------------------------------------------------------------------

def _month(d: date, offset: int) -> tuple[date, date]:
    y, m = d.year, d.month + offset
    while m <= 0:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def resolve_window(window: dict | None, today: date) -> dict[str, str]:
    """{"kind": "last_month"}：上个自然月对比再上个月；{"kind": "fixed", ...}：原样；空：不限定，由模型从问题中理解。"""
    if not window:
        return {}
    kind = window.get("kind")
    if kind == "last_month":
        (s, e), (cs, ce) = _month(today, -1), _month(today, -2)
    elif kind == "fixed":
        try:
            s, e = date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
            cs = date.fromisoformat(window["compare_start"]) if window.get("compare_start") else None
            ce = date.fromisoformat(window["compare_end"]) if window.get("compare_end") else None
        except (KeyError, ValueError) as exc:
            raise Invalid(f"时间窗口不完整或日期格式错误：{exc}") from None
        if s > e or (cs and ce and cs > ce):
            raise Invalid("时间窗口的开始日期晚于结束日期")
    else:
        raise Invalid(f"不支持的时间窗口类型：{kind}")
    out = {"kind": kind, "start": s.isoformat(), "end": e.isoformat()}
    if cs and ce:
        out.update(compare_start=cs.isoformat(), compare_end=ce.isoformat())
    return out


# --- cron（5 段：分 时 日 月 周，业务时区）-------------------------------------------------

def _field(spec: str, low: int, high: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part == "*":
            a, b = low, high
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
        if a < low or b > high or a > b or step < 1:
            raise ValueError
        out.update(range(a, b + 1, step))
    return out


def parse_cron(expr: str) -> tuple[set[int], ...]:
    parts = expr.split()
    if len(parts) != 5:
        raise Invalid("cron 需要 5 段：分 时 日 月 周（如 0 9 1 * * 表示每月 1 日 9:00）")
    try:
        minute, hour, dom, month, dow = (_field(p, lo, hi) for p, (lo, hi) in
                                         zip(parts, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))))
    except ValueError:
        raise Invalid(f"cron 表达式无效：{expr}") from None
    if 7 in dow:
        dow = (dow - {7}) | {0}
    return minute, hour, dom, month, dow


CRON_SEARCH_DAYS = 366 * 8 + 2  # 覆盖完整的闰年周期（含 2100 年这类不闰的世纪年）：2 月 29 日的任务也能找到下一次


def next_run(expr: str, after: datetime) -> datetime:
    """after（UTC）之后的下一次触发时间（UTC）。日与周同时限定时按“任一满足”（与 cron 一致）。
    逐日查找；永远不会触发的表达式（如 2 月 30 日）判为无效。"""
    minute, hour, dom, month, dow = parse_cron(expr)
    tz = ZoneInfo(get_settings().business_timezone)
    start = (after.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).replace(second=0, microsecond=0, tzinfo=None)
             + timedelta(minutes=1))
    times = sorted((h, m) for h in hour for m in minute)
    dom_any, dow_any = len(dom) == 31, len(dow) == 7
    day = start.date()
    for _ in range(CRON_SEARCH_DAYS):
        cron_dow = (day.weekday() + 1) % 7
        day_ok = (day.day in dom or cron_dow in dow) if not (dom_any or dow_any) else (
            day.day in dom and cron_dow in dow)
        if day.month in month and day_ok:
            for h, m in times:
                candidate = datetime.combine(day, dtime(h, m))
                if candidate >= start:
                    return candidate.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        day += timedelta(days=1)
    raise Invalid(f"cron 表达式不会触发（日期不存在，如 2 月 30 日）：{expr}")


# --- 运行 ---------------------------------------------------------------------------

def require_analysis_model(session: Session) -> modelcfg.Resolved:
    if not modelcfg.policy(session)["send_results"]:
        raise Conflict("分析模式需要允许向模型发送查询结果（模型配置 → 数据外发）。需保密请接入本地模型")
    resolved = modelcfg.resolve(session, "analysis", temperature=0.2)
    if resolved is None:
        raise Conflict("分析模式需要配置并检测通过一个支持工具调用的模型（模型配置 → 分析，未指定时沿用问数模型）")
    return resolved


def create_run(session: Session, user: User, question: str, window: dict | None, *, task: AnalysisTask | None = None,
               trigger: str = "manual") -> AnalysisRun:
    access.require_role(session, user, "analyst", "steward", "reviewer")
    if not question.strip():
        raise Invalid("请输入要分析的问题")
    require_analysis_model(session)
    tz = get_settings().business_timezone
    run = AnalysisRun(task_id=task.id if task else None, user_id=user.id, question=question.strip(), trigger=trigger,
                      status="queued", window=resolve_window(window, business_today(tz)),
                      budget=Budget().view())
    session.add(run)
    session.flush()
    audit.record(session, user, "analysis.create", "analysis_run", run.id, trigger=trigger,
                 task_id=task.id if task else None)
    return run


def warm_up() -> None:
    """在写入任何数据之前建好会话状态表（见 devtools.reset_metadata_db 的说明），避免在本请求的事务中自锁。"""
    _graph()


def identity_problem(session: Session, user: User | None) -> str | None:
    """发起人账号与查询角色（执行入口与任务调度共用）。"""
    if user is None or not user.is_active:
        return "发起人账号不存在或已停用"
    if not access.roles_of(session, user) & {"analyst", "steward", "reviewer", "admin"}:
        return "发起人已没有查询角色"
    return None


JOB_STATUS = {"succeeded": "succeeded", "failed": "failed", "paused": "skipped", "skipped": "skipped"}


def _own_job(session: Session, job_id: str, attempt: int):
    """本次尝试仍持有的 Job（加锁）：状态为 running 且 attempts 一致；已被恢复器终结或重新领取时为 None。"""
    from avelis.models import Job

    return session.scalar(select(Job).where(Job.id == job_id, Job.status == "running", Job.attempts == attempt)
                          .with_for_update())


def _close_job(job, outcome: dict[str, Any]) -> dict[str, Any]:
    """在调用方的事务中写入 Job 终态（与运行、任务、通知同一事务提交）。"""
    job.status = JOB_STATUS[outcome["status"]]
    job.error = outcome.get("reason") if job.status == "failed" else None
    job.result = {**(job.result or {}), **{k: v for k, v in outcome.items() if k != "status"},
                  "outcome": outcome["status"]}
    job.finished_at = utcnow()
    return outcome


def _claim_run(session: Session, run_id: str, job, attempt: int) -> str | None:
    """领取执行权：Job 由本次尝试持有、运行仍在排队时，条件更新为 running 并写入执行权标记；否则返回 None。
    已终结的运行不会被迟到的执行者重新改回 running。"""
    token = f"{job.id}:{attempt}"
    claimed = session.execute(update(AnalysisRun).where(AnalysisRun.id == run_id, AnalysisRun.status == "queued")
                              .values(status="running", started_at=utcnow(), error=None,
                                      budget={**Budget().view(), "executor": token, "job_id": job.id})).rowcount
    return token if claimed else None


def execute(session: Session, run_id: str, job_id: str, attempt: int,
            finish: Callable[[Session, AnalysisRun], dict[str, Any]] | None = None) -> dict[str, Any]:
    """执行一次分析（worker 与同步入口都经过 Job，见 jobs.run_job）。
    - 领取：Job 与运行都满足条件才写 running；
    - 中间写入：每一步提交前核对执行权（toolbox._step），失去执行权即停止、不再写入；
    - 终态：再次核对执行权，运行、Job（及 finish 写入的任务与通知）在同一事务提交。
    返回 {"status": succeeded | failed | paused | skipped | lost, ...}；lost 表示执行权已失去，什么都没写。"""
    warm_up()
    job = _own_job(session, job_id, attempt)
    if job is None:
        session.rollback()
        return {"status": "lost", "reason": "Job 已不由本次尝试持有"}
    token = _claim_run(session, run_id, job, attempt)
    if token is None:
        outcome = _close_job(job, {"status": "skipped", "analysis_run_id": run_id, "reason": "分析不存在或已不在排队状态"})
        session.commit()
        return outcome
    session.commit()
    run = session.get(AnalysisRun, run_id, populate_existing=True)
    user = session.get(User, run.user_id, populate_existing=True)
    budget = Budget()
    status, error, plan, report, verification, adapter, toolbox = "failed", None, {}, {}, {}, None, None
    try:
        problem = identity_problem(session, user)
        if problem:
            raise PermissionLost(problem)
        resolved = require_analysis_model(session)
        adapter = resolved.adapter
        toolbox = AnalysisToolbox(session, user, run, ask_service.Catalog([], {}), budget, executor=token,
                                  adapter=adapter)
        settings = get_settings()
        system = analysis_agent.system_message(business_today(settings.business_timezone),
                                               settings.business_timezone, run.window, budget)
        config = {"configurable": {"thread_id": f"analysis:{run.id}", "toolbox": toolbox, "model": resolved},
                  "recursion_limit": 50}
        from langchain_core.messages import HumanMessage

        _graph().invoke({"messages": [system, HumanMessage(run.question)], "phase": "plan", "rounds": 0,
                         "notes": []}, config)
        state = _graph().get_state(config).values
        plan, verification = toolbox.plan or {}, dict(toolbox.verification or {})
        report = {**(toolbox.report or {}), "notes": state.get("notes", [])}
        if toolbox.aborted:
            error = toolbox.aborted
        elif toolbox.permission_lost:
            error = f"分析中发起人权限失效：{toolbox.permission_lost}；之后的取数全部被拒绝"
        else:
            status = "succeeded"
    except PermissionLost as exc:
        error = f"发起人权限不足：{exc.message}"
        report = {"permission": {"lost": exc.message, "denials": []}}
    except Exception as exc:  # noqa: BLE001 - 模型、网络或治理错误如实记录
        log.exception("分析 %s 失败", run_id)
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
    # 异常路径同样带上已记录的权限拒绝：定时任务据此暂停
    if toolbox is not None and (toolbox.permission_lost or toolbox.denials):
        report = {**report, "permission": {"lost": toolbox.permission_lost, "denials": toolbox.denials}}
    session.rollback()  # 丢弃未提交的半步写入；已完成的步骤与数据集已逐步提交
    run = session.get(AnalysisRun, run_id, with_for_update=True, populate_existing=True)
    job = _own_job(session, job_id, attempt)
    if run.status != "running" or (run.budget or {}).get("executor") != token or job is None:
        log.warning("分析 %s 的执行权已失去（运行 %s），不写入本次结果", run_id, run.status)
        session.rollback()
        return {"status": "lost", "analysis_run_id": run_id, "reason": "执行权已失去"}
    run.status, run.error, run.plan, run.report, run.verification = status, error, plan, report, verification
    run.model_adapter = adapter
    run.budget = budget.view()
    run.finished_at = utcnow()
    session.flush()
    outcome = finish(session, run) if finish else {
        "status": "succeeded" if run.status == "succeeded" else "failed", "reason": run.error}
    _close_job(job, {**outcome, "analysis_run_id": run.id})
    audit.record(session, user, "analysis.finish", "analysis_run", run.id, status=run.status,
                 verified=(run.verification or {}).get("ok"), queries=budget.used_queries)
    session.commit()
    return {**outcome, "analysis_run_id": run.id}


def run_job(job_id: str, attempt: int, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """analysis_run / analysis 两类 Job（jobs.run_job 领取之后调用）。"""
    if kind == "analysis_run":
        with session_scope() as session:
            return execute(session, payload["run_id"], job_id, attempt)
    return run_task(payload["task_id"], payload.get("trigger", "schedule"), job_id, attempt)


def reconcile_abandoned(session: Session, job, reason: str) -> None:
    """恢复器判定 Job 被放弃时调用（与把 Job 记为失败同一事务）：
    - 关联的运行仍在排队或运行中：记为失败并收回执行权，定时任务补发失败通知；
    - 关联的运行已是终态（终态已提交、Job 尚未收尾）：按运行的终态修正 Job，并补发应有的任务通知。"""
    run_id = (job.payload or {}).get("run_id") or (job.result or {}).get("analysis_run_id")
    run = session.get(AnalysisRun, run_id, with_for_update=True, populate_existing=True) if run_id else None
    if run is None:
        return
    already = run.status not in ("queued", "running")
    if not already:
        run.status, run.error, run.finished_at = "failed", reason, utcnow()
        run.budget = {k: v for k, v in (run.budget or {}).items() if k != "executor"}  # 收回执行权
    outcome = {"status": "succeeded" if run.status == "succeeded" else "failed", "reason": run.error}
    if job.kind == "analysis" and run.task_id and not (already and _task_notified(session, run)):
        outcome = _finish_task(session, run.task_id, run)
    _close_job(job, {**outcome, "analysis_run_id": run.id})


def visible_run(session: Session, user: User, run_id: str) -> AnalysisRun:
    run = session.get(AnalysisRun, run_id)
    if run is None or (run.user_id != user.id and not access.is_admin(session, user)):
        raise NotFound("分析不存在或不属于当前用户")
    return run


# --- 任务与定时 ---------------------------------------------------------------------

def create_task(session: Session, user: User, title: str, question: str, window: dict | None,
                schedule_cron: str | None) -> AnalysisTask:
    access.require_role(session, user, "analyst", "steward", "reviewer")
    resolve_window(window, date.today())  # 先校验
    task = AnalysisTask(title=title.strip() or question[:60], question=question.strip(), owner_id=user.id,
                        window=window or {}, schedule_cron=schedule_cron or None, status="active")
    if task.schedule_cron:
        task.next_run_at = next_run(task.schedule_cron, utcnow())
    session.add(task)
    session.flush()
    audit.record(session, user, "analysis_task.create", "analysis_task", task.id, cron=task.schedule_cron)
    return task


def update_task(session: Session, user: User, task: AnalysisTask, changes: dict) -> AnalysisTask:
    if task.owner_id != user.id and not access.is_admin(session, user):
        raise Forbidden("只有任务发起人或管理员可以修改")
    if "window" in changes:
        resolve_window(changes["window"], date.today())
        task.window = changes["window"] or {}
    if "schedule_cron" in changes:
        task.schedule_cron = changes["schedule_cron"] or None
    if "status" in changes:
        if changes["status"] not in ("active", "paused"):
            raise Invalid("状态只能是 active 或 paused")
        task.status = changes["status"]
        task.paused_reason = None if task.status == "active" else (changes.get("reason") or "手动暂停")
    if "title" in changes and changes["title"]:
        task.title = changes["title"]
    task.next_run_at = next_run(task.schedule_cron, utcnow()) if task.schedule_cron and task.status == "active" \
        else None
    task.updated_at = utcnow()
    audit.record(session, user, "analysis_task.update", "analysis_task", task.id, **{
        k: v for k, v in changes.items() if k in ("status", "schedule_cron")})
    return task


def _notify(session: Session, user_id: str, kind: str, title: str, body: str | None, link: str | None) -> None:
    session.add(Notification(user_id=user_id, kind=kind, title=title[:255], body=body, link=link))


TASKS_LINK = "/analysis/tasks"


def _pause(session: Session, task: AnalysisTask, reason: str) -> None:
    task.status, task.paused_reason, task.next_run_at = "paused", reason, None
    task.updated_at = utcnow()
    _notify(session, task.owner_id, "analysis_paused", f"分析任务已暂停：{task.title}", reason, TASKS_LINK)
    audit.record(session, None, "analysis_task.pause", "analysis_task", task.id, reason=reason)


def _finish_task(session: Session, task_id: str, run: AnalysisRun) -> dict[str, Any]:
    """任务运行的收尾（在运行终态的同一事务中）：明确的权限拒绝 → 暂停并通知；否则发完成 / 失败通知。"""
    task = session.get(AnalysisTask, task_id, with_for_update=True, populate_existing=True)
    if task is None:
        return {"status": "succeeded" if run.status == "succeeded" else "failed", "reason": run.error}
    verification = run.verification or {}
    permission = (run.report or {}).get("permission") or {}
    if permission.get("lost") or permission.get("denials"):
        # 明确的权限拒绝：不能用“还能看到别的口径”代替“有执行这个任务的权限”
        reason = permission.get("lost") or "；".join(d["message"] for d in permission["denials"][:3])
        _pause(session, task, f"执行中被拒绝访问：{reason}")
        return {"status": "paused", "run_status": run.status, "reason": reason}
    if run.status != "succeeded":
        title, body = f"分析运行失败：{task.title}", run.error
    elif not verification.get("ok", False):
        title = f"分析完成，有 {verification.get('unverified', 0)} 条结论未核实：{task.title}"
        body = (run.report or {}).get("summary")
    else:
        title, body = f"分析完成：{task.title}", (run.report or {}).get("summary")
    _notify(session, task.owner_id, "analysis_finished" if run.status == "succeeded" else "analysis_failed",
            title, body, f"/analysis/runs/{run.id}")
    return {"status": "succeeded" if run.status == "succeeded" else "failed", "reason": run.error}


def _task_notified(session: Session, run: AnalysisRun) -> bool:
    if session.scalar(select(Notification.id).where(Notification.link == f"/analysis/runs/{run.id}").limit(1)):
        return True
    task = session.get(AnalysisTask, run.task_id)
    return bool(task and task.status == "paused" and task.last_run_id == run.id
                and task.updated_at >= (run.finished_at or run.created_at))


def run_task(task_id: str, trigger: str, job_id: str, attempt: int) -> dict[str, Any]:
    """执行一次分析任务（定时或手动，都经过 Job），返回明确的结果：
    {"status": succeeded | failed | paused | skipped | lost, "analysis_run_id", "reason"}。
    - 关联 Job 时检查它仍由本次尝试持有（状态与 attempt），否则什么都不做；
    - 定时触发在任务锁下重新检查任务状态：已暂停（或已删除）的任务跳过，不创建运行，也不改下次运行时间；
    - 发起人失去账号或角色、执行中被明确拒绝访问所需的口径或来源时，暂停任务并通知；
    - 运行与 Job 的关联在创建运行的同一事务中写入；任务的暂停与通知与运行、Job 的终态同一事务提交。"""
    with session_scope() as session:
        job = _own_job(session, job_id, attempt)
        if job is None:
            return {"status": "lost", "reason": "Job 已不由本次尝试持有"}
        task = session.get(AnalysisTask, task_id, with_for_update=True, populate_existing=True)
        if task is None:
            return _close_job(job, {"status": "skipped", "reason": "任务不存在"})
        if trigger == "schedule" and task.status != "active":
            return _close_job(job, {"status": "skipped", "reason": f"任务已暂停：{task.paused_reason or '—'}"})
        owner = session.get(User, task.owner_id, populate_existing=True)
        problem = identity_problem(session, owner)
        if problem:
            _pause(session, task, problem)
            return _close_job(job, {"status": "paused", "reason": problem})
        warm_up()
        try:
            run = create_run(session, owner, task.question, task.window, task=task, trigger=trigger)
        except (Conflict, Invalid, Forbidden) as exc:
            _pause(session, task, exc.message)
            return _close_job(job, {"status": "paused", "reason": exc.message})
        task.last_run_id = run.id
        if task.schedule_cron and trigger == "schedule":
            task.next_run_at = next_run(task.schedule_cron, utcnow())
        job.result = {**(job.result or {}), "analysis_run_id": run.id}
        run_id = run.id
    with session_scope() as session:
        return execute(session, run_id, job_id, attempt, finish=lambda s, r: _finish_task(s, task_id, r))


def schedule_due() -> list[str]:
    """到期的分析任务入队（worker 调用）。同一任务有排队或运行中的分析时跳过。"""
    from avelis import jobs
    from avelis.models import Job

    queued = []
    with session_scope() as session:
        now = utcnow()
        for task in session.scalars(select(AnalysisTask).where(
                AnalysisTask.status == "active", AnalysisTask.next_run_at.is_not(None),
                AnalysisTask.next_run_at <= now).order_by(AnalysisTask.id).with_for_update(skip_locked=True)):
            pending = session.scalar(select(Job.id).where(Job.kind == "analysis", Job.status.in_(("queued", "running")),
                                                          Job.payload["task_id"].as_string() == task.id).limit(1))
            task.next_run_at = next_run(task.schedule_cron, now)
            if pending:
                continue
            queued.append(jobs._enqueue(session, "analysis", {"task_id": task.id, "trigger": "schedule"},
                                        task.owner_id))
    for job_id in queued:
        jobs._wake(job_id)
    return queued


def run_view(session: Session, run: AnalysisRun, *, full: bool = True) -> dict[str, Any]:
    from avelis.models import AnalysisDataset, AnalysisStep

    data = {"id": run.id, "task_id": run.task_id, "question": run.question, "trigger": run.trigger,
            "status": run.status, "window": run.window,
            "budget": {k: v for k, v in (run.budget or {}).items() if k != "executor"}, "model_adapter": run.model_adapter,
            "error": run.error, "created_at": run.created_at.isoformat(),
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "title": (run.report or {}).get("title"), "verified": (run.verification or {}).get("ok")}
    if not full:
        return data
    steps = session.scalars(select(AnalysisStep).where(AnalysisStep.run_id == run.id).order_by(AnalysisStep.seq))
    datasets = session.scalars(select(AnalysisDataset).where(AnalysisDataset.run_id == run.id)
                               .order_by(AnalysisDataset.seq))
    data.update(plan=run.plan, report=run.report, verification=run.verification,
                steps=[{"seq": s.seq, "kind": s.kind, "tool": s.tool, "args": s.args, "reason": s.reason,
                        "status": s.status, "output": s.output, "dataset": _ref(session, s.dataset_id),
                        "duration_ms": s.duration_ms, "at": s.created_at.isoformat()} for s in steps],
                datasets=[{"ref": f"D{d.seq}", "seq": d.seq, "kind": d.kind, "title": d.title, "answer_id": d.answer_id,
                           "inputs": d.inputs, "columns": d.columns, "rows": d.rows[:50], "row_count": len(d.rows),
                           "truncated": d.truncated, "summary": d.meta.get("summary"), "sql": d.meta.get("sql"),
                           "metric_version_ids": d.meta.get("metric_version_ids", []),
                           "time_column": d.meta.get("time_column")} for d in datasets])
    return data


def _ref(session: Session, dataset_id: str | None) -> str | None:
    from avelis.models import AnalysisDataset

    if not dataset_id:
        return None
    d = session.get(AnalysisDataset, dataset_id)
    return f"D{d.seq}" if d else None


def task_view(task: AnalysisTask) -> dict[str, Any]:
    return {"id": task.id, "title": task.title, "question": task.question, "owner_id": task.owner_id,
            "window": task.window, "schedule_cron": task.schedule_cron, "status": task.status,
            "paused_reason": task.paused_reason, "last_run_id": task.last_run_id,
            "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
            "created_at": task.created_at.isoformat()}


