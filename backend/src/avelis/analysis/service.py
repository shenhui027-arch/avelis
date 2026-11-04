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
import json
import logging
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from langgraph.types import Command
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from avelis import audit, modelcfg
from avelis.analysis import agent as analysis_agent
from avelis.analysis.tools import AnalysisToolbox, Budget, PermissionLost
from avelis.ask import service as ask_service
from avelis.ask.intent import business_today
from avelis.config import get_settings
from avelis.db import session_scope, utcnow
from avelis.governance import access, experiences, joins
from avelis.governance.errors import Conflict, Forbidden, GovernanceError, Invalid, NotFound
from avelis.i18n import Msg, current_locale, gettext, locale_context
from avelis.models import (
    AnalysisDataset,
    AnalysisMessage,
    AnalysisRun,
    AnalysisStep,
    AnalysisTask,
    Metric,
    Notification,
    User,
)

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
            raise Invalid(Msg("时间窗口不完整或日期格式错误：{error}", error=exc)) from None
        if s > e or (cs and ce and cs > ce):
            raise Invalid("时间窗口的开始日期晚于结束日期")
    else:
        raise Invalid(Msg("不支持的时间窗口类型：{kind}", kind=kind))
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
        minute, hour, dom, month, dow = (
            _field(p, lo, hi) for p, (lo, hi) in zip(parts, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7)))
        )
    except ValueError:
        raise Invalid(Msg("cron 表达式无效：{expr}", expr=expr)) from None
    if 7 in dow:
        dow = (dow - {7}) | {0}
    return minute, hour, dom, month, dow


CRON_SEARCH_DAYS = 366 * 8 + 2  # 覆盖完整的闰年周期（含 2100 年这类不闰的世纪年）：2 月 29 日的任务也能找到下一次


def next_run(expr: str, after: datetime) -> datetime:
    """after（UTC）之后的下一次触发时间（UTC）。日与周同时限定时按“任一满足”（与 cron 一致）。
    逐日查找；永远不会触发的表达式（如 2 月 30 日）判为无效。"""
    minute, hour, dom, month, dow = parse_cron(expr)
    tz = ZoneInfo(get_settings().business_timezone)
    start = after.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).replace(
        second=0, microsecond=0, tzinfo=None
    ) + timedelta(minutes=1)
    times = sorted((h, m) for h in hour for m in minute)
    dom_any, dow_any = len(dom) == 31, len(dow) == 7
    day = start.date()
    for _ in range(CRON_SEARCH_DAYS):
        cron_dow = (day.weekday() + 1) % 7
        day_ok = (
            (day.day in dom or cron_dow in dow) if not (dom_any or dow_any) else (day.day in dom and cron_dow in dow)
        )
        if day.month in month and day_ok:
            for h, m in times:
                candidate = datetime.combine(day, dtime(h, m))
                if candidate >= start:
                    return candidate.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        day += timedelta(days=1)
    raise Invalid(Msg("cron 表达式不会触发（日期不存在，如 2 月 30 日）：{expr}", expr=expr))


# --- 运行 ---------------------------------------------------------------------------


def require_analysis_model(session: Session, model_choice: dict | None = None) -> modelcfg.Resolved:
    if not modelcfg.policy(session)["send_results"]:
        raise Conflict("分析模式需要允许向模型发送查询结果（模型配置 → 数据外发）。需保密请接入本地模型")
    resolved = (
        modelcfg.resolve_choice(session, **model_choice, temperature=0.2)
        if model_choice is not None
        else modelcfg.resolve(session, "analysis", temperature=0.2)
    )
    if resolved is None:
        raise Conflict("分析模式需要配置并检测通过一个支持工具调用的模型（模型配置 → 分析，未指定时沿用问数模型）")
    return resolved


def create_run(
    session: Session,
    user: User,
    question: str,
    window: dict | None,
    *,
    task: AnalysisTask | None = None,
    trigger: str = "manual",
    confirmation_mode: str = "auto",
    model: dict | None = None,
    reasoning_effort: str | None = None,
    _model_choice: dict | None = None,
) -> AnalysisRun:
    access.require_role(session, user, "analyst", "steward", "reviewer")
    if not question.strip():
        raise Invalid("请输入要分析的问题")
    choice = _model_choice
    if choice is not None:
        choice = {
            **modelcfg.analysis_model_choice(session, {key: choice[key] for key in ("provider_id", "model_id")}),
            "reasoning_effort": choice["reasoning_effort"],
        }
    elif model is not None or reasoning_effort is not None:
        choice = modelcfg.analysis_model_choice(session, model, reasoning_effort)
    if choice is not None:
        require_analysis_model(session, choice)
    else:
        require_analysis_model(session)
    tz = get_settings().business_timezone
    run = AnalysisRun(
        task_id=task.id if task else None,
        user_id=user.id,
        question=question.strip(),
        trigger=trigger,
        status="queued",
        window=resolve_window(window, business_today(tz)),
        budget=Budget().view(),
        confirmation_mode=confirmation_mode,
        checkpoint={"request_locale": current_locale()},
        model_choice=choice,
    )
    session.add(run)
    session.flush()
    audit.record(
        session, user, "analysis.create", "analysis_run", run.id, trigger=trigger, task_id=task.id if task else None
    )
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
    """本次尝试仍持有的 Job（加锁）：状态为 running 且 attempts 一致；已被恢复器终结或重新领取时为 None。
    加锁顺序全局一致：Job → 运行 → 任务。"""
    from avelis.models import Job

    return session.scalar(
        select(Job).where(Job.id == job_id, Job.status == "running", Job.attempts == attempt).with_for_update()
    )


def _close_job(job, outcome: dict[str, Any]) -> dict[str, Any]:
    """在调用方的事务中写入 Job 终态（与运行、任务、通知同一事务提交）。"""
    job.status = JOB_STATUS[outcome["status"]]
    job.error = outcome.get("reason") if job.status == "failed" else None
    job.result = {
        **(job.result or {}),
        **{k: v for k, v in outcome.items() if k != "status"},
        "outcome": outcome["status"],
    }
    job.finished_at = utcnow()
    return outcome


def _claim_run(session: Session, run_id: str, job, attempt: int) -> str | None:
    """领取执行权：Job 由本次尝试持有、运行仍在排队时，条件更新为 running 并写入执行权标记；否则返回 None。
    已终结的运行不会被迟到的执行者重新改回 running。"""
    token = f"{job.id}:{attempt}"
    pending = session.get(AnalysisRun, run_id)
    saved = (pending.checkpoint or {}).get("snapshot") if pending else None
    # 恢复继承暂停前的预算；执行权仍只属于新 Job 的本次尝试。
    budget = (saved or {}).get("budget") or Budget().view()
    claimed = session.execute(
        update(AnalysisRun)
        .where(AnalysisRun.id == run_id, AnalysisRun.status == "queued")
        .values(
            status="running",
            started_at=(pending.started_at if pending else None) or utcnow(),
            finished_at=None,
            error=None,
            budget={**budget, "executor": token, "job_id": job.id},
        )
    ).rowcount
    return token if claimed else None


def execute(
    session: Session,
    run_id: str,
    job_id: str,
    attempt: int,
    finish: Callable[[Session, AnalysisRun], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """恢复运行保存的请求语言，再执行 Job 管线。"""
    run = session.get(AnalysisRun, run_id)
    locale = (run.checkpoint or {}).get("request_locale", "zh-CN") if run is not None else "zh-CN"
    with locale_context(locale):
        return _execute(session, run_id, job_id, attempt, finish)


def _execute(
    session: Session,
    run_id: str,
    job_id: str,
    attempt: int,
    finish: Callable[[Session, AnalysisRun], dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
        outcome = _close_job(
            job, {"status": "skipped", "analysis_run_id": run_id, "reason": "分析不存在或已不在排队状态"}
        )
        session.commit()
        return outcome
    session.commit()
    run = session.get(AnalysisRun, run_id, populate_existing=True)
    user = session.get(User, run.user_id, populate_existing=True)
    checkpoint = dict(run.checkpoint or {})
    saved = checkpoint.get("snapshot") or {}
    budget = Budget.restore(saved["budget"]) if saved.get("budget") else Budget()
    status, error, plan, report, verification, adapter, toolbox = "failed", None, {}, {}, {}, None, None
    effective = None  # 实际生效的思考强度（ADR 0012）
    try:
        if not checkpoint.get("resume_pending"):
            problem = identity_problem(session, user)
            if problem:
                raise PermissionLost(problem)
        resolved = (
            require_analysis_model(session, run.model_choice)
            if run.model_choice is not None
            else require_analysis_model(session)
        )
        adapter, effective = resolved.adapter, getattr(resolved, "effective_effort", None)
        toolbox = AnalysisToolbox(
            session, user, run, ask_service.Catalog([], {}), budget, executor=token, adapter=adapter
        )
        if checkpoint.get("resume_pending"):
            toolbox.restore(saved)
            budget = toolbox.budget
        settings = get_settings()
        example = analysis_agent.prompt_example(ask_service.visible_catalog(session, user, with_values=False))
        config = {
            "configurable": {"thread_id": f"analysis:{run.id}", "toolbox": toolbox, "model": resolved},
            "recursion_limit": 50,
        }
        graph = _graph()
        if checkpoint.get("resume_pending"):
            if not graph.get_state(config).next:
                raise Conflict("分析检查点不存在或已经恢复")
            result = graph.invoke(Command(resume=checkpoint["feedback"]), config)
        else:
            result = graph.invoke(
                analysis_agent.initial_state(
                    run.question,
                    business_today(settings.business_timezone),
                    settings.business_timezone,
                    run.window,
                    example,
                    inherited=checkpoint.get("inherited"),
                    confirmation_mode=run.confirmation_mode,
                    unattended=run.trigger == "schedule",
                    request_locale=checkpoint.get("request_locale", "zh-CN"),
                ),
                config,
            )
        state = graph.get_state(config).values
        plan, verification = toolbox.plan or {}, dict(toolbox.verification or {})
        report = {**(toolbox.report or {}), "notes": state.get("notes", [])}
        if toolbox.user_stopped:
            status, error = "stopped", "用户停止"
        elif toolbox.aborted:
            error = toolbox.aborted
        elif toolbox.permission_lost:
            error = f"分析中发起人权限失效：{toolbox.permission_lost}；之后的取数全部被拒绝"
        elif result.get("__interrupt__"):
            status = "waiting"
            checkpoint.update(
                pending=result["__interrupt__"][0].value,
                proposal=state.get("proposal") or {},
                snapshot=toolbox.snapshot(),
                resume_pending=False,
            )
            report = {"title": gettext("等待确认"), "incomplete": True, "notes": state.get("notes", [])}
        elif toolbox.clarification:  # 待澄清：用户可见的短问题与可恢复的上下文（问题、范围、已用预算）
            status = "clarify"
            report = {
                "title": gettext("需要澄清"),
                "summary": toolbox.clarification["question"],
                "clarification": toolbox.clarification,
                "incomplete": True,
                "notes": state.get("notes", []),
            }
        else:
            status = "succeeded"
        checkpoint.update(
            confirmed_scope=state.get("confirmed_scope") or checkpoint.get("confirmed_scope") or {},
            confirmed_items=state.get("confirmed_items") or checkpoint.get("confirmed_items") or [],
            proposal=state.get("proposal") or checkpoint.get("proposal") or {},
        )
        if "inherited" in state:
            checkpoint["inherited"] = state["inherited"]
        if status != "waiting":
            checkpoint.pop("pending", None)
            checkpoint["resume_pending"] = False
            checkpoint["snapshot"] = toolbox.snapshot()
    except PermissionLost as exc:
        error = f"发起人权限不足：{exc.message}"
        report = {"permission": {"lost": exc.message, "denials": []}}
    except Exception as exc:  # noqa: BLE001 - 模型、网络或治理错误如实记录
        log.exception("分析 %s 失败", run_id)
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
    # 异常路径同样带上已记录的权限拒绝：定时任务据此暂停
    if toolbox is not None and (toolbox.permission_lost or toolbox.denials):
        report = {**report, "permission": {"lost": toolbox.permission_lost, "denials": toolbox.denials}}
    if toolbox is not None:
        report["evidence"] = {
            **(report.get("evidence") or {}),
            "filter_alignment": list(toolbox.filter_alignment),
            **(
                {"filter_alignment_unknown": list(toolbox.filter_alignment_unknown)}
                if toolbox.filter_alignment_unknown
                else {}
            ),
        }
    session.rollback()  # 丢弃未提交的半步写入；已完成的步骤与数据集已逐步提交
    # 加锁顺序全局一致：Job → 运行 → 任务（恢复器、领取、任务路径相同），并发收尾不会互等
    job = _own_job(session, job_id, attempt)
    run = session.get(AnalysisRun, run_id, with_for_update=True, populate_existing=True)
    if run.status != "running" or (run.budget or {}).get("executor") != token or job is None:
        log.warning("分析 %s 的执行权已失去（运行 %s），不写入本次结果", run_id, run.status)
        session.rollback()
        return {"status": "lost", "analysis_run_id": run_id, "reason": "执行权已失去"}
    # 插话在执行过程中独立提交；不能被执行入口时复制的旧 checkpoint 覆盖。
    candidates = list((run.checkpoint or {}).get("candidates") or [])
    for candidate in checkpoint.get("candidates") or []:
        if candidate not in candidates:
            candidates.append(candidate)
    # 停止请求与最终完成竞争时，以本次行锁读取的标记为准，不再请求模型或执行工具。
    if run.stop_requested:
        if toolbox is None:
            toolbox = AnalysisToolbox(session, user, run, ask_service.Catalog([], {}), budget)
            toolbox.restore(saved, check_permissions=False)
            budget = toolbox.budget
        status, error = "stopped", "用户停止"
        report, verification = _stopped_report(toolbox, notes=report.get("notes"))
        plan = toolbox.plan or plan
        checkpoint.pop("pending", None)
        checkpoint["resume_pending"] = False
        checkpoint["snapshot"] = toolbox.snapshot()
    run.status, run.error, run.plan, run.report, run.verification = status, error, plan, report, verification
    if candidates:
        # 等理解阶段结束后补引用，避免连续追问时丢掉前一轮回答；仍无引用的理解和插话候选不提供保存。
        if status != "waiting" or checkpoint["pending"]["kind"] != "clarify":
            for candidate in candidates:
                if candidate["source"]["checkpoint"] in ("understand", "interject") and not candidate["refs"]:
                    candidate["refs"] = _scope_metric_refs(session, checkpoint)
            candidates = [
                c for c in candidates if c["refs"] or c["source"]["checkpoint"] not in ("understand", "interject")
            ]
        checkpoint["candidates"] = candidates
    run.checkpoint = checkpoint
    run.model_adapter = adapter
    run.reasoning_effort = effective
    run.budget = budget.view()
    run.finished_at = None if status == "waiting" else utcnow()
    session.flush()
    outcome = (
        finish(session, run) if finish else _finish_task(session, run.task_id, run) if run.task_id else _outcome(run)
    )
    _close_job(job, {**outcome, "analysis_run_id": run.id})
    audit.record(
        session,
        user,
        "analysis.wait" if status == "waiting" else "analysis.finish",
        "analysis_run",
        run.id,
        status=run.status,
        verified=(run.verification or {}).get("ok"),
        queries=budget.used_queries,
    )
    session.commit()
    return {**outcome, "analysis_run_id": run.id}


def _outcome(run: AnalysisRun) -> dict[str, Any]:
    """运行终态 → Job 结果：待澄清是本次执行正常结束（等用户回答），不是失败。"""
    if run.status in ("clarify", "waiting"):
        return {"status": "succeeded", "reason": None, "run_status": run.status}
    return {"status": "succeeded" if run.status == "succeeded" else "failed", "reason": run.error}


def run_job(job_id: str, attempt: int, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """analysis_run / analysis 两类 Job（jobs.run_job 领取之后调用）。"""
    if kind == "analysis_run":
        with session_scope() as session:
            return execute(session, payload["run_id"], job_id, attempt)
    with locale_context(payload.get("request_locale", "zh-CN")):
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
    outcome = _outcome(run)
    if job.kind == "analysis" and run.task_id:
        if not (already and _task_notified(session, run)):
            outcome = _finish_task(session, run.task_id, run)
        elif _paused_by(run):  # 已暂停且已通知：保留 paused 的结果（Job 记为 skipped），不再重复通知
            outcome = {"status": "paused", "run_status": run.status, "reason": _paused_by(run)}
    _close_job(job, {**outcome, "analysis_run_id": run.id})


def visible_run(session: Session, user: User, run_id: str) -> AnalysisRun:
    run = session.get(AnalysisRun, run_id)
    if run is None or (run.user_id != user.id and not access.is_admin(session, user)):
        raise NotFound("分析不存在或不属于当前用户")
    return run


def _live_run(session: Session, user: User, run_id: str) -> AnalysisRun:
    """运行交互只属于发起人；管理员读取权限不包含代为交互。"""
    run = session.get(AnalysisRun, run_id, with_for_update=True, populate_existing=True)
    if run is None or run.user_id != user.id:
        raise NotFound("分析不存在或不属于当前用户")
    if run.status not in ("queued", "running", "waiting"):
        raise Conflict("分析已经结束，不能插话、停止或压缩上下文")
    return run


def add_message(session: Session, user: User, run_id: str, text: str) -> AnalysisMessage:
    run = _live_run(session, user, run_id)
    if not text.strip():
        raise Invalid("请输入插话内容")
    message = AnalysisMessage(run_id=run.id, user_id=user.id, text=text)
    session.add(message)
    session.flush()
    audit.record(session, user, "analysis.message", "analysis_run", run.id, message_id=message.id, text=message.text)
    return message


def _stopped_report(toolbox: AnalysisToolbox, *, notes: list[str] | None = None) -> tuple[dict, dict]:
    """沿用预算截止的后端未完成报告与核验路径；停止不调用模型、不增加步骤。"""
    toolbox.user_stopped = True
    toolbox.report = None
    try:
        toolbox.check_copied_permissions()
    except PermissionLost:
        pass  # 停止只记录失权；核验与 run_view 仍按当前权限投影历史证据。
    analysis_agent.verify({}, {"configurable": {"toolbox": toolbox, "model": None}})
    stop_notes = list(notes or [])
    if "分析停止：用户停止" not in stop_notes:
        stop_notes.append("分析停止：用户停止")
    report = {**toolbox.report, "notes": stop_notes}
    if toolbox.permission_lost or toolbox.denials:
        report["permission"] = {"lost": toolbox.permission_lost, "denials": toolbox.denials}
    return report, dict(toolbox.verification or {})


def stop_run(session: Session, user: User, run_id: str) -> AnalysisRun:
    run = _live_run(session, user, run_id)
    run.stop_requested = True
    if run.status == "waiting":
        checkpoint = dict(run.checkpoint or {})
        saved = checkpoint.get("snapshot") or {}
        toolbox = AnalysisToolbox(session, user, run, ask_service.Catalog([], {}), Budget())
        toolbox.restore(saved, check_permissions=False)
        run.report, run.verification = _stopped_report(toolbox, notes=(run.report or {}).get("notes"))
        run.plan = toolbox.plan or run.plan
        run.status, run.error, run.finished_at = "stopped", "用户停止", utcnow()
        checkpoint.pop("pending", None)
        checkpoint["resume_pending"] = False
        checkpoint["snapshot"] = toolbox.snapshot()
        run.checkpoint = checkpoint
    audit.record(session, user, "analysis.stop", "analysis_run", run.id, status=run.status)
    session.flush()
    return run


def compact_run(session: Session, user: User, run_id: str) -> AnalysisRun:
    run = _live_run(session, user, run_id)
    run.compact_requested = True
    audit.record(session, user, "analysis.compact", "analysis_run", run.id, status=run.status)
    session.flush()
    return run


def message_view(message: AnalysisMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "text": message.text,
        "created_at": message.created_at.isoformat(),
        "consumed_at": message.consumed_at.isoformat() if message.consumed_at else None,
    }


def _feedback(proposal: dict, feedback: dict) -> dict:
    """每项必须明确确认或否决；补充文字不隐式确认任何提议。"""
    expected = {item["id"] for item in proposal.get("items") or []}
    decisions = feedback.get("decisions") or []
    supplied = [d.get("item_id") for d in decisions]
    if len(supplied) != len(set(supplied)) or set(supplied) != expected:
        raise Invalid("请逐项确认或否决，且不要重复提交同一项")
    return {
        "decisions": [{"item_id": d["item_id"], "decision": d["decision"]} for d in decisions],
        "supplement": (feedback.get("supplement") or "").strip(),
    }


def _scope_metric_refs(session: Session, checkpoint: dict) -> list[dict]:
    from avelis.governance.metrics import latest_governed_version

    scope = (checkpoint.get("snapshot") or {}).get("scope") or checkpoint.get("confirmed_scope") or {}
    refs = []
    for code in scope.get("metric_codes") or []:
        metric = session.scalar(select(Metric).where(Metric.code == code))
        version = latest_governed_version(session, metric) if metric else None
        if version is not None:
            refs.append({"object_type": "metric_version", "object_id": version.id, "version": version.version_no})
    return refs


def _propose_confirmed_joins(session: Session, user: User, run: AnalysisRun, checkpoint: dict, feedback: dict) -> None:
    confirmed = {d["item_id"] for d in feedback["decisions"] if d["decision"] == "confirmed"}
    proposal = dict(checkpoint.get("proposal") or {})
    proposal["items"] = [dict(item) for item in proposal.get("items") or []]
    candidates = list(checkpoint.get("join_candidates") or [])
    warnings = list(checkpoint.get("warnings") or [])
    for item in proposal["items"]:
        if item["id"] not in confirmed or item.get("kind") != "join" or item.get("source") != "inferred":
            continue
        assets = analysis_agent.validate_proposal_join(session, user, item)
        if assets is None:
            warning = "这条关联缺少结构，无法提议为关联路径"
            if warning not in warnings:
                warnings.append(warning)
            continue
        spec = item["join"]
        try:
            path = joins.propose(
                session,
                user,
                left_asset_id=assets[0].id,
                left_columns=spec["left_columns"],
                right_asset_id=assets[1].id,
                right_columns=spec["right_columns"],
                source={"run_id": run.id, "checkpoint": "assumptions", "user_text": item["content"]},
            )
        except GovernanceError as exc:
            warning = f"这条关联没能提议为关联路径：{exc}"
            if warning not in warnings:
                warnings.append(warning)
            continue
        candidate = {"item_id": item["id"], "id": path.id, "status": path.status}
        if candidate not in candidates:
            candidates.append(candidate)
    checkpoint.update(proposal=proposal, join_candidates=candidates, warnings=warnings)


def _clarification_answer(pending: dict, answer: Any) -> str | list[dict[str, str]]:
    if isinstance(answer, str):
        answer = answer.strip()
        if answer:
            return answer
        raise Invalid("请回答追问后继续分析")
    if not isinstance(answer, list) or not answer or len(answer) > 100:
        raise Invalid("请按待确认项提交筛选候选")
    items = {
        (item.get("dimension"), item.get("original")): item.get("options") or []
        for item in pending.get("items") or []
        if isinstance(item, dict)
    }
    selections, seen = [], set()
    for selection in answer:
        if not isinstance(selection, dict) or any(
            not isinstance(selection.get(field), str) or not selection[field]
            for field in ("dimension", "original", "value")
        ):
            raise Invalid("请按待确认项提交筛选候选")
        key = (selection["dimension"], selection["original"])
        if key not in items or key in seen or selection["value"] not in items[key]:
            raise Invalid("筛选回答包含未知、重复的待确认项或无效候选")
        seen.add(key)
        selections.append({field: selection[field] for field in ("dimension", "original", "value")})
    return selections


def resume_run(session: Session, user: User, run_id: str, feedback: dict) -> AnalysisRun:
    """只领取一次暂停；新 Job、反馈、状态转换与审计由 API 在同一事务提交。"""
    from avelis import jobs

    run = session.get(AnalysisRun, run_id, with_for_update=True, populate_existing=True)
    if run is None or run.user_id != user.id:
        raise NotFound("分析不存在或不属于当前用户")
    if run.status != "waiting":
        raise Conflict("这次确认已提交，或分析当前不在等待确认")
    checkpoint = dict(run.checkpoint or {})
    pending = checkpoint["pending"]
    if pending["kind"] == "clarify":
        answer = _clarification_answer(pending, feedback.get("answer", ""))
        feedback = {"answer": answer}
        answer_text = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        candidates = experiences.candidates_from_feedback(
            run_id=run.id,
            proposal={},
            feedback={"supplement": f"问题：{pending['question']} 回答：{answer_text}"},
            refs=_scope_metric_refs(session, checkpoint),
            checkpoint="understand",
        )
    else:
        feedback = _feedback(checkpoint.get("proposal") or {}, feedback)
        _propose_confirmed_joins(session, user, run, checkpoint, feedback)
        candidates = experiences.candidates_from_feedback(
            run_id=run.id,
            proposal=checkpoint.get("proposal") or {},
            feedback=feedback,
            refs=_scope_metric_refs(session, checkpoint),
        )
    checkpoint.pop("pending")
    checkpoint.update(feedback=feedback, resume_pending=True)
    checkpoint["candidates"] = [*(checkpoint.get("candidates") or []), *candidates]
    claimed = session.execute(
        update(AnalysisRun)
        .where(AnalysisRun.id == run.id, AnalysisRun.status == "waiting")
        .values(status="queued", checkpoint=checkpoint, finished_at=None)
    ).rowcount
    if not claimed:
        raise Conflict("这次确认已提交")
    job_id = jobs._enqueue(session, "analysis_run", {"run_id": run.id}, user.id)
    run.status, run.checkpoint = "queued", checkpoint
    run.budget = {**(run.budget or {}), "job_id": job_id}
    run.finished_at = None
    audit.record(session, user, "analysis.resume", "analysis_run", run.id, feedback=feedback, job_id=job_id)
    session.flush()
    return run


def followup_run(
    session: Session,
    user: User,
    run_id: str,
    text: str,
    confirmation_mode: str = "auto",
    *,
    model: dict | None = None,
    reasoning_effort: str | None = None,
) -> AnalysisRun:
    parent = visible_run(session, user, run_id)
    if parent.user_id != user.id:
        raise Forbidden("只有发起人可以继续分析")
    # 与归档、删除共用根运行锁，不能在对话检查结束后再插入新轮次。
    _owned_conversation(session, user, parent.conversation_id or parent.id)
    if parent.status != "succeeded":
        raise Conflict("只有已完成的分析可以发起续问")
    text = text.strip()
    saved = parent.checkpoint or {}
    lookup = (parent.report or {}).get("kind") == "lookup"
    scope = (
        (parent.plan or {}).get("scope") if lookup else saved.get("confirmed_scope") or (parent.plan or {}).get("scope")
    ) or {}
    if (parent.report or {}).get("kind") == "chart" or scope.get("question_type") == "chart":
        # 改图轮次的范围只描述呈现，不是确认过的分析范围：下一轮重新理解，需要新数据时才能取数。
        scope = {}
    window = {**parent.window, "kind": "fixed"} if not lookup and (parent.window or {}).get("start") else None
    inherited_choice = None
    if model is None and reasoning_effort is None and parent.model_choice is not None:
        # 未核实思考档位时也能继承模型；隐含默认值不作为用户新提交的档位校验。
        inherited_choice = dict(parent.model_choice)
    elif model is None and parent.model_choice is not None:
        model = {key: parent.model_choice[key] for key in ("provider_id", "model_id")}
    run = create_run(
        session,
        user,
        text if lookup or not scope else parent.question,
        window,
        confirmation_mode=confirmation_mode,
        model=model,
        reasoning_effort=reasoning_effort,
        _model_choice=inherited_choice,
    )
    run.parent_run_id = parent.id
    run.conversation_id = parent.conversation_id or parent.id
    inherited = {
        "user_text": text,
        "previous_question": parent.question,
        "previous_scope": scope,
        "previous_window": parent.window or {},
    }
    if not lookup:
        inherited.update(scope=scope, items=saved.get("confirmed_items") or [])
    from avelis.analysis.conversation_context import build_followup_context

    inherited["conversation"] = build_followup_context(session, user, parent, run)
    run.checkpoint = {
        **(run.checkpoint or {}),
        "inherited": inherited,
        "candidates": experiences.candidates_from_feedback(
            run_id=run.id,
            proposal={},
            feedback={"challenge": text},
            refs=_scope_metric_refs(session, saved),
            checkpoint="conclusion",
        ),
    }
    audit.record(session, user, "analysis.followup", "analysis_run", run.id, parent_run_id=parent.id, user_text=text)
    session.flush()
    return run


def _conversation_runs(session: Session, root: AnalysisRun) -> list[AnalysisRun]:
    return list(
        session.scalars(
            select(AnalysisRun)
            .where(AnalysisRun.conversation_id == root.id)
            .order_by(AnalysisRun.parent_run_id.is_not(None), AnalysisRun.created_at, AnalysisRun.id)
        )
    )


def visible_conversation(session: Session, user: User, conversation_id: str) -> AnalysisRun:
    root = visible_run(session, user, conversation_id)
    if root.conversation_id != root.id:
        raise NotFound("对话不存在")
    return root


def _owned_conversation(session: Session, user: User, conversation_id: str) -> AnalysisRun:
    root = visible_conversation(session, user, conversation_id)
    root = session.get(AnalysisRun, root.id, with_for_update=True, populate_existing=True)
    if root is None:
        raise NotFound("对话不存在")
    if root.user_id != user.id:
        raise Forbidden("只有发起人可以修改对话")
    return root


def conversation_view(
    session: Session, root: AnalysisRun, latest: AnalysisRun, turn_count: int, *, user: User
) -> dict[str, Any]:
    title = run_view(session, root, full=False, user=user)["title"]
    activity = latest.finished_at or latest.started_at or latest.created_at
    return {
        "id": root.id,
        "title": root.display_title or title or root.question,
        "display_title": root.display_title,
        "question": root.question,
        "latest_run_id": latest.id,
        "status": latest.status,
        "turn_count": turn_count,
        "archived_at": root.archived_at.isoformat() if root.archived_at else None,
        "created_at": root.created_at.isoformat(),
        "updated_at": activity.isoformat(),
        "started_at": latest.started_at.isoformat() if latest.started_at else None,
        "finished_at": latest.finished_at.isoformat() if latest.finished_at else None,
    }


def list_conversations(session: Session, user: User, *, archived: bool = False) -> list[dict[str, Any]]:
    activity = func.coalesce(AnalysisRun.finished_at, AnalysisRun.started_at, AnalysisRun.created_at)
    roots = select(AnalysisRun.id).where(
        AnalysisRun.user_id == user.id,
        AnalysisRun.conversation_id == AnalysisRun.id,
        AnalysisRun.archived_at.is_not(None) if archived else AnalysisRun.archived_at.is_(None),
    )
    groups = session.execute(
        select(AnalysisRun.conversation_id, func.max(activity).label("activity"), func.count().label("turn_count"))
        .where(AnalysisRun.conversation_id.in_(roots))
        .group_by(AnalysisRun.conversation_id)
        .order_by(func.max(activity).desc(), AnalysisRun.conversation_id)
        .limit(100)
    )
    conversations = []
    for conversation_id, _, turn_count in groups:
        root = session.get(AnalysisRun, conversation_id)
        latest = session.scalar(
            select(AnalysisRun)
            .where(AnalysisRun.conversation_id == conversation_id)
            .order_by(
                AnalysisRun.parent_run_id.is_not(None).desc(), AnalysisRun.created_at.desc(), AnalysisRun.id.desc()
            )
            .limit(1)
        )
        conversations.append(conversation_view(session, root, latest, turn_count, user=user))
    return conversations


def get_conversation(session: Session, user: User, conversation_id: str) -> dict[str, Any]:
    root = visible_conversation(session, user, conversation_id)
    runs = _conversation_runs(session, root)
    return {
        **conversation_view(session, root, runs[-1], len(runs), user=user),
        "runs": [run_view(session, run, full=False, user=user) for run in runs],
    }


def rename_conversation(session: Session, user: User, conversation_id: str, display_title: str) -> dict[str, Any]:
    root = _owned_conversation(session, user, conversation_id)
    display_title = display_title.strip()
    if len(display_title) > 80:
        raise Invalid("对话显示名最多 80 字")
    previous_title = root.display_title
    root.display_title = display_title or None
    audit.record(
        session,
        user,
        "analysis.conversation.rename",
        "analysis_run",
        root.id,
        previous_title=previous_title,
        display_title=root.display_title,
    )
    session.flush()
    return get_conversation(session, user, root.id)


def _inactive_conversation_runs(session: Session, root: AnalysisRun) -> list[AnalysisRun]:
    runs = list(
        session.scalars(
            select(AnalysisRun)
            .where(AnalysisRun.conversation_id == root.id)
            .order_by(AnalysisRun.created_at, AnalysisRun.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if any(run.status in {"queued", "running", "waiting"} for run in runs):
        raise Conflict("对话中有正在运行或等待确认的分析，请先停止再操作")
    return runs


def archive_conversation(session: Session, user: User, conversation_id: str, *, archived: bool) -> dict[str, Any]:
    root = _owned_conversation(session, user, conversation_id)
    if archived:
        _inactive_conversation_runs(session, root)
    root.archived_at = (root.archived_at or utcnow()) if archived else None
    action = "archive" if archived else "restore"
    audit.record(session, user, f"analysis.conversation.{action}", "analysis_run", root.id)
    session.flush()
    return get_conversation(session, user, root.id)


def delete_conversation(session: Session, user: User, conversation_id: str) -> None:
    """删除运行及其附属内容；治理对象、执行记录与历史审计独立保留。由请求事务统一提交。"""
    from avelis.ask import agent as ask_agent

    root = _owned_conversation(session, user, conversation_id)
    runs = _inactive_conversation_runs(session, root)
    run_ids = [run.id for run in runs]
    # 检查点独立自动提交，先清理可在失败时保留完整的应用数据供重试。
    saver = ask_agent.checkpointer()
    for run_id in run_ids:
        saver.delete_thread(f"analysis:{run_id}")
    title = root.display_title or run_view(session, root, full=False, user=user)["title"] or root.question
    audit.record(
        session,
        user,
        "analysis.conversation.delete",
        "analysis_run",
        root.id,
        conversation_id=root.id,
        turn_count=len(runs),
        checkpoint_threads=len(run_ids),
        title=title,
    )
    session.execute(update(AnalysisTask).where(AnalysisTask.last_run_id.in_(run_ids)).values(last_run_id=None))
    links = [f"/analysis/runs/{run_id}" for run_id in run_ids]
    links.append(f"/analysis/conversations/{root.id}")
    session.execute(update(Notification).where(Notification.link.in_(links)).values(link=None))
    for model in (AnalysisMessage, AnalysisStep, AnalysisDataset):
        session.execute(delete(model).where(model.run_id.in_(run_ids)))
    # 同一条语句删除整条链，满足父子关系与根运行自身的外键约束。
    session.execute(delete(AnalysisRun).where(AnalysisRun.id.in_(run_ids)))
    session.flush()


# --- 任务与定时 ---------------------------------------------------------------------


def create_task(
    session: Session, user: User, title: str, question: str, window: dict | None, schedule_cron: str | None
) -> AnalysisTask:
    access.require_role(session, user, "analyst", "steward", "reviewer")
    resolve_window(window, date.today())  # 先校验
    task = AnalysisTask(
        title=title.strip() or question[:60],
        question=question.strip(),
        owner_id=user.id,
        window=window or {},
        schedule_cron=schedule_cron or None,
        status="active",
    )
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
    task.next_run_at = (
        next_run(task.schedule_cron, utcnow()) if task.schedule_cron and task.status == "active" else None
    )
    task.updated_at = utcnow()
    audit.record(
        session,
        user,
        "analysis_task.update",
        "analysis_task",
        task.id,
        **{k: v for k, v in changes.items() if k in ("status", "schedule_cron")},
    )
    return task


def _notify(session: Session, user_id: str, kind: str, title: str, body: str | None, link: str | None) -> None:
    session.add(Notification(user_id=user_id, kind=kind, title=title[:255], body=body, link=link))


TASKS_LINK = "/analysis/tasks"


def _pause(session: Session, task: AnalysisTask, reason: str) -> None:
    task.status, task.paused_reason, task.next_run_at = "paused", reason, None
    task.updated_at = utcnow()
    _notify(session, task.owner_id, "analysis_paused", f"分析任务已暂停：{task.title}", reason, TASKS_LINK)
    audit.record(session, None, "analysis_task.pause", "analysis_task", task.id, reason=reason)


def _paused_by(run: AnalysisRun) -> str | None:
    """运行中明确的权限拒绝或失去权限（任务因此暂停）的原因。"""
    permission = (run.report or {}).get("permission") or {}
    return permission.get("lost") or "；".join(d["message"] for d in (permission.get("denials") or [])[:3]) or None


def _finish_task(session: Session, task_id: str, run: AnalysisRun) -> dict[str, Any]:
    """任务运行的收尾（在运行终态的同一事务中）：明确的权限拒绝 → 暂停并通知；否则发完成 / 失败通知。"""
    task = session.get(AnalysisTask, task_id, with_for_update=True, populate_existing=True)
    if task is None:
        return _outcome(run)
    verification = run.verification or {}
    reason = _paused_by(run)
    if reason:
        # 明确的权限拒绝：不能用“还能看到别的口径”代替“有执行这个任务的权限”
        _pause(session, task, f"执行中被拒绝访问：{reason}")
        return {"status": "paused", "run_status": run.status, "reason": reason}
    if run.status == "waiting":
        return _outcome(run)
    if run.status == "clarify":  # 定时任务无人回答追问：暂停任务并通知，等任务所有者改写问题后再启用
        question = (run.report or {}).get("summary") or "问题需要澄清"
        _pause(session, task, f"需要澄清：{question}")
        return {"status": "paused", "run_status": run.status, "reason": question}
    if run.status != "succeeded":
        title, body = f"分析运行失败：{task.title}", run.error
    elif not verification.get("ok", False):
        title = f"分析完成，有 {verification.get('unverified', 0)} 条结论未核实：{task.title}"
        body = (run.report or {}).get("summary")
    else:
        title, body = f"分析完成：{task.title}", (run.report or {}).get("summary")
    _notify(
        session,
        task.owner_id,
        "analysis_finished" if run.status == "succeeded" else "analysis_failed",
        title,
        body,
        f"/analysis/runs/{run.id}",
    )
    return {"status": "succeeded" if run.status == "succeeded" else "failed", "reason": run.error}


def _task_notified(session: Session, run: AnalysisRun) -> bool:
    if session.scalar(select(Notification.id).where(Notification.link == f"/analysis/runs/{run.id}").limit(1)):
        return True
    task = session.get(AnalysisTask, run.task_id)
    return bool(
        task
        and task.status == "paused"
        and task.last_run_id == run.id
        and task.updated_at >= (run.finished_at or run.created_at)
    )


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
        for task in session.scalars(
            select(AnalysisTask)
            .where(
                AnalysisTask.status == "active", AnalysisTask.next_run_at.is_not(None), AnalysisTask.next_run_at <= now
            )
            .order_by(AnalysisTask.id)
            .with_for_update(skip_locked=True)
        ):
            pending = session.scalar(
                select(Job.id)
                .where(
                    Job.kind == "analysis",
                    Job.status.in_(("queued", "running")),
                    Job.payload["task_id"].as_string() == task.id,
                )
                .limit(1)
            )
            task.next_run_at = next_run(task.schedule_cron, now)
            if pending:
                continue
            queued.append(
                jobs._enqueue(session, "analysis", {"task_id": task.id, "trigger": "schedule"}, task.owner_id)
            )
    for job_id in queued:
        jobs._wake(job_id)
    return queued


def run_view(session: Session, run: AnalysisRun, *, full: bool = True, user: User | None = None) -> dict[str, Any]:
    from avelis.analysis.dataset_access import can_read_dataset
    from avelis.analysis.dataset_projection import (
        build_column_roles,
        dataset_meta,
        record_refs,
        report_dimension_labels,
        restrict_report,
        restrict_verification,
    )
    from avelis.analysis.figures import report_chart_preference
    from avelis.charts import recommend
    from avelis.models import AnalysisDataset, AnalysisStep, Answer

    checkpoint = run.checkpoint or {}
    conversation_id = run.conversation_id or run.id
    turn = (
        1
        if conversation_id == run.id
        else 1
        + session.scalar(
            select(func.count())
            .select_from(AnalysisRun)
            .where(
                AnalysisRun.conversation_id == conversation_id,
                AnalysisRun.parent_run_id.is_not(None),
                or_(
                    AnalysisRun.created_at < run.created_at,
                    (AnalysisRun.created_at == run.created_at) & (AnalysisRun.id <= run.id),
                ),
            )
        )
    )
    data = {
        "id": run.id,
        "task_id": run.task_id,
        "parent_run_id": run.parent_run_id,
        "conversation_id": conversation_id,
        "turn": turn,
        "confirmation_mode": run.confirmation_mode,
        "question": run.question,
        "trigger": run.trigger,
        "status": run.status,
        "window": run.window,
        "pending": checkpoint.get("pending"),
        "budget": {k: v for k, v in (run.budget or {}).items() if k != "executor"},
        "model_adapter": run.model_adapter,
        "reasoning_effort": run.reasoning_effort,
        "model_choice": run.model_choice,
        "error": run.error,
        "created_at": run.created_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "title": (run.report or {}).get("title"),
        "verified": (run.verification or {}).get("ok"),
        "messages": [],
        "filter_alignment": (run.report or {}).get("evidence", {}).get("filter_alignment")
        or (checkpoint.get("snapshot") or {}).get("state", {}).get("filter_alignment")
        or [],
    }
    checkpoint, candidates_restricted = ask_service.project_alignment_clarifications(session, user, checkpoint)
    projected_report, report_candidates_restricted = ask_service.project_alignment_clarifications(
        session, user, run.report or {}
    )
    candidates_restricted |= report_candidates_restricted
    data["pending"] = checkpoint.get("pending")
    alignment_restricted = (
        candidates_restricted
        or user is not None
        and ask_service.filter_alignment_restricted(session, user, data["filter_alignment"])
    )
    if not full:
        data["filter_alignment"] = []
        title_refs = record_refs(((run.report or {}).get("fields") or {}).get("title") or {})
        if (run.report or {}).get("kind") == "chart" and (run.report or {}).get("dataset"):
            title_refs.add(run.report["dataset"])
        if user is not None and title_refs:
            datasets = list(session.scalars(select(AnalysisDataset).where(AnalysisDataset.run_id == run.id)))
            known = {f"D{d.seq}": d for d in datasets}
            restricted_refs = {
                f"D{d.seq}"
                for d in datasets
                if f"D{d.seq}" in title_refs and not can_read_dataset(session, user, d, known)
            }
            data["title"] = restrict_report(run.report or {}, restricted_refs).get("title")
            if restricted_refs:
                data["filter_alignment"] = []
        return data
    lookup_restricted = False
    if user is not None and (run.report or {}).get("kind") == "lookup":
        from avelis.api.ask import answer_view

        source = session.get(Answer, run.report.get("answer_id"))
        if source is not None:
            alignment_restricted = alignment_restricted or ask_service.filter_alignment_restricted(
                session, user, (source.evidence or {}).get("filter_alignment") or []
            )
        lookup_restricted = source is None or not can_read_dataset(
            session,
            user,
            AnalysisDataset(
                kind="governed", answer_id=source.id, meta={"metric_version_ids": source.metric_version_ids}
            ),
        )
        if not lookup_restricted:
            answer = session.scalar(
                select(Answer).where(Answer.id == run.report.get("answer_id"), Answer.asked_by == user.id)
            )
            if answer is not None:
                data["answer"] = answer_view(answer, session=session, user=user)
    steps = list(session.scalars(select(AnalysisStep).where(AnalysisStep.run_id == run.id).order_by(AnalysisStep.seq)))
    for step in steps:
        _, hidden = ask_service.project_alignment_clarifications(session, user, step.output or {})
        candidates_restricted |= hidden
    alignment_restricted |= candidates_restricted
    if candidates_restricted:
        # 早期澄清步骤也可能被后续报告复述；没有逐段来源信息的正文保守隐藏。
        safe_clarification, hidden = ask_service.project_alignment_clarifications(
            session, user, (run.report or {}).get("clarification") or {}
        )
        projected_report = restrict_report(projected_report, set(), lookup_restricted=True)
        projected_report.pop("title", None)
        if hidden:
            projected_report["clarification"] = safe_clarification
    if user is not None:
        alignment_restricted = alignment_restricted or ask_service.filter_alignment_restricted(
            session,
            user,
            [entry for step in steps for entry in (step.output or {}).get("filter_alignment") or []],
        )
    datasets = list(
        session.scalars(select(AnalysisDataset).where(AnalysisDataset.run_id == run.id).order_by(AnalysisDataset.seq))
    )
    known = {f"D{d.seq}": d for d in datasets}
    restricted = {d.id for d in datasets if user is not None and not can_read_dataset(session, user, d, known)}
    readable = {d.id for d in datasets} - restricted
    report = restrict_report(
        projected_report,
        {f"D{d.seq}" for d in datasets if d.id in restricted},
        lookup_restricted=lookup_restricted or (alignment_restricted and (run.report or {}).get("kind") == "lookup"),
    )
    report = report_dimension_labels(session, report, [d for d in datasets if d.id in readable])
    metadata = {d.id: dataset_meta(session, d) for d in datasets if d.id in readable}
    has_restricted = bool(restricted or lookup_restricted or alignment_restricted)
    if has_restricted:
        data["filter_alignment"] = []
        if report.get("evidence"):
            report = {
                **report,
                "evidence": {
                    key: value
                    for key, value in report["evidence"].items()
                    if key not in {"filter_alignment", "filter_alignment_unknown"}
                },
            }

    def is_metric_code(value) -> bool:
        return isinstance(value, str) and re.fullmatch(r"[a-z0-9_]{1,64}", value) is not None

    def is_window_date(value) -> bool:
        if not isinstance(value, str):
            return False
        try:
            return date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False

    def step_args(step) -> dict:
        args = dict(step.args)
        if not has_restricted or step.kind == "user" and not candidates_restricted:
            return args
        # 后续步骤可能复述任意来源的数据，只保留明确的结构字段。
        projected = {
            key: value for key, value in args.items() if key in {"code", "metric_code"} and is_metric_code(value)
        }
        codes = args.get("metric_codes")
        if isinstance(codes, list) and all(is_metric_code(code) for code in codes):
            projected["metric_codes"] = codes
        projected.update(
            {
                key: value
                for key, value in args.items()
                if key in {"start_date", "end_date", "compare_start", "compare_end"} and is_window_date(value)
            }
        )
        projected.update(
            {key: args[key] for key in ("dataset", "ref") if isinstance(args.get(key), str) and args[key] in known}
        )
        if isinstance(args.get("window"), dict):
            projected["window"] = {
                key: value
                for key, value in args["window"].items()
                if key in {"start", "end", "compare_start", "compare_end"} and is_window_date(value)
            }
        return projected

    def step_output(step) -> dict:
        if not has_restricted or step.kind == "user" and not candidates_restricted:
            return step.output
        if step.kind == "clarify" and candidates_restricted and not restricted and not lookup_restricted:
            return ask_service.project_alignment_clarifications(session, user, step.output)[0]
        if step.kind == "tool" and step.dataset_id in readable:
            return (
                {
                    key: value
                    for key, value in step.output.items()
                    if key not in {"filter_alignment", "filter_alignment_unknown"}
                }
                if alignment_restricted
                else step.output
            )
        # 模型与其他步骤可能复述任意来源；工具只有明确关联可读数据集时才保留输出。
        return {"rows": [], "restricted": True} if step.kind == "tool" else {"restricted": True}

    messages = session.scalars(
        select(AnalysisMessage)
        .where(AnalysisMessage.run_id == run.id)
        .order_by(AnalysisMessage.created_at, AnalysisMessage.id)
    )
    data.update(
        plan=run.plan,
        report=report,
        title=report.get("title"),
        verification=restrict_verification(run.verification or {}, report),
        messages=[] if candidates_restricted else [message_view(message) for message in messages],
        proposal=checkpoint.get("proposal"),
        experience_candidates=checkpoint.get("candidates") or [],
        join_candidates=checkpoint.get("join_candidates") or [],
        checkpoint_warnings=checkpoint.get("warnings") or [],
        confirmed_scope=checkpoint.get("confirmed_scope") or {},
        confirmed_items=checkpoint.get("confirmed_items") or [],
        steps=[
            {
                "seq": s.seq,
                "kind": s.kind,
                "tool": s.tool,
                "args": step_args(s),
                "reason": None if has_restricted and (s.kind != "user" or candidates_restricted) else s.reason,
                "status": s.status,
                "output": step_output(s),
                "dataset": _ref(session, s.dataset_id),
                "duration_ms": s.duration_ms,
                "at": s.created_at.isoformat(),
            }
            for s in steps
        ],
        datasets=[
            {
                "ref": f"D{d.seq}",
                "seq": d.seq,
                "kind": d.kind,
                "title": d.title,
                "answer_id": d.answer_id,
                "inputs": d.inputs,
                "columns": d.columns,
                "rows": [] if d.id in restricted else d.rows[:50],
                "row_count": len(d.rows),
                **({"restricted": True} if d.id in restricted else {}),
                "truncated": d.truncated,
                "summary": None if d.id in restricted else d.meta.get("summary"),
                "sql": None if d.id in restricted else d.meta.get("sql"),
                "metric_version_ids": d.meta.get("metric_version_ids", []),
                "time_column": d.meta.get("time_column"),
                "units": metadata[d.id].get("units", {}) if d.id in readable else {},
                "polarity": metadata[d.id].get("polarity", {}) if d.id in readable else {},
                "column_roles": build_column_roles(d.columns, metadata[d.id]) if d.id in readable else {},
                "charts": recommend(
                    d.columns,
                    d.rows,
                    build_column_roles(d.columns, metadata[d.id]),
                    metadata[d.id].get("units") or {},
                    metadata[d.id].get("polarity") or {},
                    metadata[d.id].get("intent"),
                    preference=report_chart_preference(report, f"D{d.seq}"),
                )
                if d.id in readable
                else [],
            }
            for d in datasets
        ],
    )
    from avelis.analysis.figures import report_figures

    data["figures"] = report_figures(
        {
            "report": report,
            "datasets": [
                {
                    "ref": f"D{d.seq}",
                    "kind": d.kind,
                    "title": d.title,
                    "columns": d.columns,
                    "rows": d.rows,
                    "meta": metadata[d.id],
                }
                for d in datasets
                if d.id in readable
            ],
        }
    )
    if "figures" in report:
        refs = {figure["ref"] for figure in data["figures"]}
        # 保留显式选择的存在性，避免全部失效时被导出当作旧报告重新选图。
        data["report"] = {
            **report,
            "figures": [
                f if f.get("dataset") in refs else {"dataset": f.get("dataset"), "kind": f.get("kind")}
                for f in report["figures"]
            ],
        }
    return data


def _ref(session: Session, dataset_id: str | None) -> str | None:
    from avelis.models import AnalysisDataset

    if not dataset_id:
        return None
    d = session.get(AnalysisDataset, dataset_id)
    return f"D{d.seq}" if d else None


def task_view(task: AnalysisTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "question": task.question,
        "owner_id": task.owner_id,
        "window": task.window,
        "schedule_cron": task.schedule_cron,
        "status": task.status,
        "paused_reason": task.paused_reason,
        "last_run_id": task.last_run_id,
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
        "created_at": task.created_at.isoformat(),
    }
