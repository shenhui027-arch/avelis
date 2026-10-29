"""分析运行、分析任务与定时（ADR 0007 第 2、7、11 点）。

- 分析需要真实模型（支持工具调用）且允许向模型发送查询结果；两者缺一时如实拒绝，不退化为“看不到数的分析”。
- 每次运行以发起人身份执行：口径可见范围、来源权限、执行前核对都按发起人当时的权限重新检查。
- 定时任务按 cron（业务时区）由 worker 入队；发起人停用、失去查询角色或没有任何可见口径时，任务自动暂停并通知。
"""

from __future__ import annotations

import calendar
import logging

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis import audit, modelcfg
from avelis.analysis import agent as analysis_agent
from avelis.analysis.tools import AnalysisToolbox, Budget
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


def next_run(expr: str, after: datetime) -> datetime:
    """after（UTC）之后的下一次触发时间（UTC）。日与周同时限定时按“任一满足”（与 cron 一致）。"""
    minute, hour, dom, month, dow = parse_cron(expr)
    tz = ZoneInfo(get_settings().business_timezone)
    local = after.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
    dom_any, dow_any = len(dom) == 31, len(dow) == 7
    for _ in range(60 * 24 * 400):
        cron_dow = (local.weekday() + 1) % 7
        day_ok = (local.day in dom or cron_dow in dow) if not (dom_any or dow_any) else (
            local.day in dom and cron_dow in dow)
        if local.month in month and day_ok and local.hour in hour and local.minute in minute:
            return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        local += timedelta(minutes=1)
    raise Invalid(f"cron 表达式在一年内不会触发：{expr}")


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


def execute(session: Session, run: AnalysisRun) -> AnalysisRun:
    """在当前会话中执行一次分析（API 的 wait=true 与 worker 都调用这里）。"""
    warm_up()
    user = session.get(User, run.user_id)
    run.status, run.started_at = "running", utcnow()
    session.flush()
    budget = Budget()
    try:
        resolved = require_analysis_model(session)
        run.model_adapter = resolved.adapter
        catalog = ask_service.visible_catalog(session, user, with_values=False)
        toolbox = AnalysisToolbox(session, user, run, catalog, budget)
        settings = get_settings()
        system = analysis_agent.system_message(business_today(settings.business_timezone),
                                               settings.business_timezone, run.window, budget)
        config = {"configurable": {"thread_id": f"analysis:{run.id}", "toolbox": toolbox, "model": resolved.model},
                  "recursion_limit": 50}
        from langchain_core.messages import HumanMessage

        _graph().invoke({"messages": [system, HumanMessage(run.question)], "phase": "plan", "rounds": 0,
                         "notes": []}, config)
        state = _graph().get_state(config).values
        run.plan = toolbox.plan or {}
        run.report = {**(toolbox.report or {}), "notes": state.get("notes", [])}
        run.status = "succeeded"
    except Exception as exc:  # noqa: BLE001 - 模型、网络或治理错误如实记录
        log.exception("分析 %s 失败", run.id)
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {str(exc)[:500]}"
    run.budget = budget.view()
    run.finished_at = utcnow()
    session.flush()
    audit.record(session, user, "analysis.finish", "analysis_run", run.id, status=run.status,
                 verified=(run.verification or {}).get("ok"), queries=budget.used_queries)
    return run


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


def _owner_problem(session: Session, owner: User | None) -> str | None:
    if owner is None or not owner.is_active:
        return "发起人账号已停用"
    roles = access.roles_of(session, owner)
    if not roles & {"analyst", "steward", "reviewer", "admin"}:
        return "发起人已没有查询角色"
    if not ask_service.visible_catalog(session, owner, with_values=False).versions:
        return "发起人已没有任何可见口径"
    return None


def run_task(task_id: str, trigger: str) -> str | None:
    """执行一次分析任务（定时或手动）。发起人失去权限时暂停任务并通知，返回 None。"""
    with session_scope() as session:
        task = session.get(AnalysisTask, task_id)
        if task is None:
            return None
        owner = session.get(User, task.owner_id)
        problem = _owner_problem(session, owner)
        if problem:
            task.status, task.paused_reason, task.next_run_at = "paused", problem, None
            _notify(session, task.owner_id, "analysis_paused", f"分析任务已暂停：{task.title}", problem,
                    f"/analysis/tasks/{task.id}")
            audit.record(session, None, "analysis_task.pause", "analysis_task", task.id, reason=problem)
            return None
        warm_up()
        try:
            run = create_run(session, owner, task.question, task.window, task=task, trigger=trigger)
        except (Conflict, Invalid) as exc:
            task.status, task.paused_reason, task.next_run_at = "paused", exc.message, None
            _notify(session, task.owner_id, "analysis_paused", f"分析任务已暂停：{task.title}", exc.message,
                    f"/analysis/tasks/{task.id}")
            return None
        task.last_run_id = run.id
        if task.schedule_cron and trigger == "schedule":
            task.next_run_at = next_run(task.schedule_cron, utcnow())
        run_id = run.id
    with session_scope() as session:
        run = session.get(AnalysisRun, run_id)
        execute(session, run)
        task = session.get(AnalysisTask, task_id)
        verification = run.verification or {}
        if run.status != "succeeded":
            title, body = f"分析运行失败：{task.title}", run.error
        elif not verification.get("ok", False):
            title = f"分析完成，有 {verification.get('unverified', 0)} 条结论未核实：{task.title}"
            body = run.report.get("summary")
        else:
            title, body = f"分析完成：{task.title}", run.report.get("summary")
        _notify(session, task.owner_id, "analysis_finished" if run.status == "succeeded" else "analysis_failed",
                title, body, f"/analysis/runs/{run.id}")
    return run_id


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
            "status": run.status, "window": run.window, "budget": run.budget, "model_adapter": run.model_adapter,
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
                           "metric_version_ids": d.meta.get("metric_version_ids", [])} for d in datasets])
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


