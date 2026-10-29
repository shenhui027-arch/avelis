"""分析运行、Job、任务与通知的一致性，以及执行中的重新授权（审计 #1、#11–#14 与复核的状态机问题）。

在独立的测试库上运行真实的 jobs.run_job / analysis.service / abandoned_jobs；只把模型图换成按脚本调用工具的替身，
断言数据库中的实际状态（运行、Job、任务、通知、步骤），不只看返回值。
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from avelis import jobs
from avelis.analysis import service
from avelis.db import session_scope, utcnow
from avelis.models import AnalysisRun, AnalysisStep, AnalysisTask, Job, Notification, User, UserRole

pytestmark = pytest.mark.db


class Crash(BaseException):
    """模拟进程中途退出：不是 Exception，任何 except Exception 都拦不住。"""


class ScriptedGraph:
    """替代 LangGraph：invoke 时按脚本操作工具箱（如同模型发出的工具调用）。"""

    def __init__(self, script):
        self.script, self.calls = script, 0

    def invoke(self, state, config):
        self.calls += 1
        self.script(config["configurable"]["toolbox"])

    def get_state(self, config):
        return SimpleNamespace(values={"notes": []})


@pytest.fixture
def no_model(monkeypatch):
    """不连接模型与会话状态表：分析模型视为已配置，图由各测试提供。"""
    monkeypatch.setattr(service, "warm_up", lambda: None)
    monkeypatch.setattr(service, "require_analysis_model",
                        lambda session: SimpleNamespace(adapter="test", model=None, factory=None))
    monkeypatch.setattr(jobs, "_wake", lambda job_id: None)


def graph(monkeypatch, script):
    g = ScriptedGraph(script)
    monkeypatch.setattr(service, "_graph", lambda: g)
    return g


def _run_with_job(user_id: str) -> tuple[str, str]:
    with session_scope() as s:
        run = AnalysisRun(user_id=user_id, question="测试分析", trigger="manual", status="queued")
        s.add(run)
        s.flush()
        return run.id, jobs._enqueue(s, "analysis_run", {"run_id": run.id}, user_id)


def _task_with_job(user_id: str, trigger: str = "schedule") -> tuple[str, str]:
    with session_scope() as s:
        task = AnalysisTask(title="归因", question="上个月成交额为什么变化？", owner_id=user_id, window={},
                            schedule_cron="0 9 1 * *", status="active", next_run_at=utcnow())
        s.add(task)
        s.flush()
        return task.id, jobs._enqueue(s, "analysis", {"task_id": task.id, "trigger": trigger}, user_id)


def _claim(job_id: str) -> int:
    """像 jobs.run_job 一样领取 Job，但不执行（模拟领取后挂起的 worker）。"""
    with session_scope() as s:
        s.execute(update(Job).where(Job.id == job_id, Job.status == "queued").values(
            status="running", started_at=utcnow(), attempts=Job.attempts + 1))
        return s.get(Job, job_id).attempts


def _backdate(job_id: str) -> None:
    with session_scope() as s:
        s.get(Job, job_id).started_at = utcnow() - timedelta(hours=2)


def _get(cls, id_):
    with session_scope() as s:
        obj = s.get(cls, id_)
        s.expunge(obj)
        return obj


def _steps(run_id: str) -> list[tuple[str, str]]:
    with session_scope() as s:
        return [(st.tool, st.status) for st in s.scalars(select(AnalysisStep).where(AnalysisStep.run_id == run_id)
                                                         .order_by(AnalysisStep.seq))]


# --- 授权 ---------------------------------------------------------------------------

def test_execute_rejects_inactive_user_before_any_model_call(factory, no_model, monkeypatch):
    g = graph(monkeypatch, lambda box: pytest.fail("停用账号不能进入分析"))
    run_id, job_id = _run_with_job(factory.user(active=False))
    jobs.run_job(job_id)
    run, job = _get(AnalysisRun, run_id), _get(Job, job_id)
    assert run.status == "failed" and "停用" in run.error and run.report["permission"]["lost"]
    assert job.status == "failed" and "停用" in job.error and g.calls == 0


def test_revocation_during_run_denies_later_fetches_and_is_reported(factory, no_model, monkeypatch):
    """分析中停用账号：之后的工具调用一律拒绝，不再请求模型；运行失败并在报告中如实记录。"""
    uid = factory.user()
    seen = {}

    def script(box):
        seen["first"] = json.loads(box.dispatch("search_metrics", {"keyword": ""}))
        with session_scope() as s:  # 管理员在另一个事务中停用账号
            s.get(User, uid).is_active = False
        seen["second"] = json.loads(box.dispatch("search_metrics", {"keyword": ""}))
        seen["third"] = json.loads(box.dispatch("search_assets", {"keyword": ""}))

    graph(monkeypatch, script)
    run_id, job_id = _run_with_job(uid)
    jobs.run_job(job_id)
    assert "metrics" in seen["first"]
    assert "停用" in seen["second"]["error"] and "不再执行" in seen["third"]["error"]
    run = _get(AnalysisRun, run_id)
    assert run.status == "failed" and "权限失效" in run.error and "停用" in run.report["permission"]["lost"]
    assert _steps(run_id) == [("search_metrics", "ok"), ("search_metrics", "denied")]
    assert _get(Job, job_id).status == "failed"


def test_role_removed_during_run_loses_permission(factory, no_model, monkeypatch):
    uid = factory.user()

    def script(box):
        with session_scope() as s:
            for r in s.scalars(select(UserRole).where(UserRole.user_id == uid)):
                s.delete(r)
        assert "查询角色" in json.loads(box.dispatch("search_metrics", {"keyword": ""}))["error"]

    graph(monkeypatch, script)
    run_id, job_id = _run_with_job(uid)
    jobs.run_job(job_id)
    assert "查询角色" in _get(AnalysisRun, run_id).report["permission"]["lost"]


# --- 状态机 ---------------------------------------------------------------------------

def test_sync_run_interrupted_is_recovered_through_its_job(factory, no_model, monkeypatch):
    """wait=true 也经过 Job：请求进程在执行中退出后，恢复器按 Job 回收运行（复核：同步执行永久 running）。"""
    from avelis.api import analysis as api
    from avelis.db import session_factory

    def script(box):
        box.dispatch("search_metrics", {"keyword": ""})
        raise Crash()

    graph(monkeypatch, script)
    uid = factory.user()
    session = session_factory()()
    try:
        with pytest.raises(Crash):
            api.create_run(api.RunCreate(question="为什么 9 月成交额下降？"), wait=True, user=session.get(User, uid),
                           session=session)
    finally:
        session.close()
    with session_scope() as s:
        run = s.scalar(select(AnalysisRun).where(AnalysisRun.user_id == uid))
        job = s.scalar(select(Job).where(Job.payload["run_id"].as_string() == run.id))
        run_id, job_id = run.id, job.id
        assert (run.status, job.status) == ("running", "running")  # 中断：两者都停在 running
    _backdate(job_id)
    assert job_id in jobs.abandoned_jobs(older_than_seconds=60)
    run, job = _get(AnalysisRun, run_id), _get(Job, job_id)
    assert run.status == "failed" and "worker 可能已退出" in run.error and job.status == "failed"
    assert _steps(run_id) == [("search_metrics", "ok")]  # 中断前已完成的步骤保留


def test_late_worker_cannot_write_after_recovery(factory, no_model, monkeypatch):
    """执行中被判定放弃：旧 worker 的后续步骤与终态都不写入，Job 不被改成成功。"""
    seen = {}

    def script(box):
        box.dispatch("search_metrics", {"keyword": ""})
        with session_scope() as s:  # 其他连接能看到 running 与已完成的步骤
            seen["status"] = s.get(AnalysisRun, box.run.id).status
            seen["steps"] = len(list(s.scalars(select(AnalysisStep).where(AnalysisStep.run_id == box.run.id))))
        job_id = box.executor.split(":")[0]
        _backdate(job_id)
        jobs.abandoned_jobs(older_than_seconds=60)  # 另一个 worker 的恢复器判定放弃
        box._step("tool", "late", {}, "ok", {})  # 旧 worker 迟到的一步
        seen["after"] = json.loads(box.dispatch("search_metrics", {"keyword": ""}))
        box.report = {"title": "迟到的结果", "summary": "", "claims": []}

    graph(monkeypatch, script)
    run_id, job_id = _run_with_job(factory.user())
    jobs.run_job(job_id)
    assert seen["status"] == "running" and seen["steps"] == 1 and "不再执行" in seen["after"]["error"]
    run, job = _get(AnalysisRun, run_id), _get(Job, job_id)
    assert run.status == "failed" and "worker 可能已退出" in run.error and run.report.get("title") is None
    assert job.status == "failed" and _steps(run_id) == [("search_metrics", "ok")]


def test_late_worker_cannot_revive_finished_run(factory, no_model, monkeypatch):
    """领取 Job 后挂起的旧 worker：运行已被终结、或 Job 已被重新领取后，它都不能把运行改回 running。"""
    g = graph(monkeypatch, lambda box: pytest.fail("旧 worker 不应执行"))
    run_id, job_id = _run_with_job(factory.user())
    attempt = _claim(job_id)
    _backdate(job_id)
    jobs.abandoned_jobs(older_than_seconds=60)
    assert service.run_job(job_id, attempt, "analysis_run", {"run_id": run_id})["status"] == "lost"
    with session_scope() as s:  # 运维把 Job 重新排队，由新的 worker 领取（attempt 变了）
        s.get(Job, job_id).status = "queued"
    newer = _claim(job_id)
    assert newer == attempt + 1
    assert service.run_job(job_id, attempt, "analysis_run", {"run_id": run_id})["status"] == "lost"
    run = _get(AnalysisRun, run_id)
    assert run.status == "failed" and run.started_at is None and g.calls == 0 and _steps(run_id) == []


def test_task_path_checks_job_attempt_before_creating_run(factory, no_model, monkeypatch):
    graph(monkeypatch, lambda box: pytest.fail("不应执行"))
    task_id, job_id = _task_with_job(factory.user())
    attempt = _claim(job_id)
    _backdate(job_id)
    jobs.abandoned_jobs(older_than_seconds=60)
    assert service.run_task(task_id, "schedule", job_id, attempt)["status"] == "lost"
    with session_scope() as s:
        assert s.scalar(select(AnalysisRun.id).where(AnalysisRun.task_id == task_id)) is None


def test_recovery_follows_terminal_run_and_sends_missing_notification(factory, no_model):
    """运行终态已提交、Job 未收尾（如旧版本的分开提交）：恢复器按运行终态修正 Job，并补发任务通知（只发一次）。"""
    uid = factory.user()
    task_id, job_id = _task_with_job(uid)
    _claim(job_id)
    with session_scope() as s:
        run = AnalysisRun(user_id=uid, task_id=task_id, question="q", trigger="schedule", status="succeeded",
                          report={"summary": "完成"}, verification={"ok": True}, finished_at=utcnow())
        s.add(run)
        s.flush()
        run_id = run.id
        s.get(Job, job_id).result = {"analysis_run_id": run_id}
    _backdate(job_id)
    assert job_id in jobs.abandoned_jobs(older_than_seconds=60)
    job, run = _get(Job, job_id), _get(AnalysisRun, run_id)
    assert job.status == "succeeded" and job.error is None and run.status == "succeeded"
    with session_scope() as s:
        notes = list(s.scalars(select(Notification).where(Notification.link == f"/analysis/runs/{run_id}")))
        assert [n.kind for n in notes] == ["analysis_finished"]


def test_failed_analysis_marks_job_failed_in_same_transaction(factory, no_model, monkeypatch):
    def script(box):
        raise RuntimeError("模型超时（测试）")

    graph(monkeypatch, script)
    run_id, job_id = _run_with_job(factory.user())
    jobs.run_job(job_id)
    job, run = _get(Job, job_id), _get(AnalysisRun, run_id)
    assert run.status == "failed" and "模型超时" in run.error
    assert job.status == "failed" and "模型超时" in job.error and job.result["analysis_run_id"] == run_id
    assert job.finished_at is not None and run.finished_at is not None


def test_api_creates_run_and_job_in_one_transaction(factory, no_model, monkeypatch):
    """异步发起：运行与 Job 在同一事务中提交，提交之后才唤醒 worker（审计 #11 场景 A）。"""
    from avelis.api import analysis as api
    from avelis.db import session_factory

    woke = []

    def wake(job_id):
        with session_scope() as s:  # 唤醒时 Job 与运行都已提交
            job = s.get(Job, job_id)
            woke.append((job.kind, s.get(AnalysisRun, job.payload["run_id"]).status))

    monkeypatch.setattr(jobs, "_wake", wake)
    uid = factory.user()
    session = session_factory()()
    try:
        view = api.create_run(api.RunCreate(question="为什么 9 月成交额下降？"), wait=False,
                              user=session.get(User, uid), session=session)
    finally:
        session.close()
    assert woke == [("analysis_run", "queued")] and view["status"] == "queued"


def test_manual_task_run_goes_through_a_job(factory, no_model, monkeypatch):
    from avelis.api import analysis as api
    from avelis.db import session_factory

    def script(box):
        box.report = {"title": "完成", "summary": "无数字。", "claims": []}

    graph(monkeypatch, script)
    uid = factory.user()
    with session_scope() as s:
        task = AnalysisTask(title="手动", question="上个月成交额为什么变化？", owner_id=uid, window={}, status="active")
        s.add(task)
        s.flush()
        task_id = task.id
    session = session_factory()()
    try:
        view = api.run_task_now(task_id, user=session.get(User, uid), session=session)
    finally:
        session.close()
    assert view["status"] == "succeeded"
    with session_scope() as s:
        job = s.scalar(select(Job).where(Job.kind == "analysis", Job.payload["task_id"].as_string() == task_id))
        assert job.status == "succeeded" and job.result["analysis_run_id"] == view["id"]
        assert s.scalar(select(Notification.kind).where(Notification.link == f"/analysis/runs/{view['id']}")) == \
            "analysis_finished"


def test_scheduled_job_for_paused_task_is_skipped(factory, no_model, monkeypatch):
    """入队后任务被暂停：worker 在任务锁下重新检查，跳过且不创建运行，Job 记为 skipped（审计 #12、#13）。"""
    graph(monkeypatch, lambda box: pytest.fail("不应执行分析"))
    task_id, job_id = _task_with_job(factory.user())
    with session_scope() as s:  # 用户在 worker 消费前暂停
        t = s.get(AnalysisTask, task_id)
        t.status, t.paused_reason, t.next_run_at = "paused", "手动暂停", None
    jobs.run_job(job_id)
    job, task = _get(Job, job_id), _get(AnalysisTask, task_id)
    assert job.status == "skipped" and job.result["outcome"] == "skipped" and "暂停" in job.result["reason"]
    assert task.status == "paused" and task.next_run_at is None and task.last_run_id is None
    with session_scope() as s:
        assert s.scalar(select(AnalysisRun.id).where(AnalysisRun.task_id == task_id)) is None


def _denied_query(box, code):
    out = json.loads(box.dispatch("query_metric", {
        "metric_codes": [code], "start_date": "2026-09-01", "end_date": "2026-09-30", "purpose": "取数"}))
    assert out["status"] == "denied" and box.budget.used_queries == 0


def test_permission_denial_during_task_pauses_and_notifies(factory, no_model, monkeypatch):
    """发起人仍能看到别的口径，但任务需要的指标已无权访问：执行中的明确拒绝让任务暂停并通知（审计 #14）。"""
    admin, uid = factory.user(roles=("admin",)), factory.user()
    factory.metric(admin, member_id=uid)  # 仍可见的另一个域
    hidden = factory.metric(admin)  # 任务要用、但发起人不在其业务域的指标

    def script(box):
        _denied_query(box, hidden)
        box.report = {"title": "部分完成", "summary": "无法取数。", "claims": []}

    graph(monkeypatch, script)
    task_id, job_id = _task_with_job(uid)
    jobs.run_job(job_id)
    task, job = _get(AnalysisTask, task_id), _get(Job, job_id)
    assert task.status == "paused" and hidden in task.paused_reason and task.next_run_at is None
    assert job.status == "skipped" and job.result["outcome"] == "paused" and job.result["analysis_run_id"]
    run = _get(AnalysisRun, job.result["analysis_run_id"])
    assert run.report["permission"]["denials"][0]["metric_code"] == hidden
    with session_scope() as s:
        note = s.scalar(select(Notification).where(Notification.user_id == uid, Notification.kind == "analysis_paused"))
        assert note.link == "/analysis/tasks" and hidden in note.body


def test_denial_followed_by_failure_still_pauses_task(factory, no_model, monkeypatch):
    """工具已拒绝访问、随后模型异常：拒绝记录仍写入报告，任务据此暂停（复核：异常路径丢失拒绝）。"""
    admin, uid = factory.user(roles=("admin",)), factory.user()
    hidden = factory.metric(admin)

    def script(box):
        _denied_query(box, hidden)
        raise RuntimeError("模型超时（测试）")

    graph(monkeypatch, script)
    task_id, job_id = _task_with_job(uid)
    jobs.run_job(job_id)
    task, job = _get(AnalysisTask, task_id), _get(Job, job_id)
    run = _get(AnalysisRun, job.result["analysis_run_id"])
    assert run.status == "failed" and run.report["permission"]["denials"][0]["metric_code"] == hidden
    assert task.status == "paused" and hidden in task.paused_reason and job.result["outcome"] == "paused"
