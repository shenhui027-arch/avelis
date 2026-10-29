"""分析运行、Job、任务与通知的一致性，以及执行中的重新授权（审计 #1、#11–#14）。

在独立的测试库上运行真实的 execute / run_task / run_job / abandoned_jobs；只把模型图换成按脚本调用工具的替身，
断言数据库中的实际状态（运行、Job、任务、通知、步骤），不只看返回值。
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from avelis import jobs
from avelis.analysis import service
from avelis.db import session_scope, utcnow
from avelis.models import AnalysisRun, AnalysisStep, AnalysisTask, Job, Notification, User, UserRole

pytestmark = pytest.mark.db


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


def _run(user_id: str, status: str = "queued", task_id: str | None = None) -> str:
    with session_scope() as s:
        run = AnalysisRun(user_id=user_id, question="测试分析", trigger="manual", status=status, task_id=task_id)
        s.add(run)
        s.flush()
        return run.id


def _get(cls, id_):
    with session_scope() as s:
        obj = s.get(cls, id_)
        s.expunge(obj)
        return obj


def test_execute_rejects_inactive_user_before_any_model_call(factory, no_model, monkeypatch):
    graph = ScriptedGraph(lambda box: pytest.fail("停用账号不能进入分析"))
    monkeypatch.setattr(service, "_graph", lambda: graph)
    uid = factory.user(active=False)
    run_id = _run(uid)
    with session_scope() as s:
        service.execute(s, s.get(AnalysisRun, run_id))
    run = _get(AnalysisRun, run_id)
    assert run.status == "failed" and "停用" in run.error and run.report["permission"]["lost"]
    assert graph.calls == 0


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

    monkeypatch.setattr(service, "_graph", lambda: ScriptedGraph(script))
    run_id = _run(uid)
    with session_scope() as s:
        service.execute(s, s.get(AnalysisRun, run_id))
    assert "metrics" in seen["first"]
    assert "停用" in seen["second"]["error"] and "不再执行" in seen["third"]["error"]
    run = _get(AnalysisRun, run_id)
    assert run.status == "failed" and "权限失效" in run.error and "停用" in run.report["permission"]["lost"]
    with session_scope() as s:
        steps = [(st.tool, st.status) for st in s.scalars(select(AnalysisStep).where(AnalysisStep.run_id == run_id)
                                                          .order_by(AnalysisStep.seq))]
    assert steps == [("search_metrics", "ok"), ("search_metrics", "denied")]


def test_role_removed_during_run_loses_permission(factory, no_model, monkeypatch):
    uid = factory.user()

    def script(box):
        with session_scope() as s:
            for r in s.scalars(select(UserRole).where(UserRole.user_id == uid)):
                s.delete(r)
        out = json.loads(box.dispatch("search_metrics", {"keyword": ""}))
        assert "查询角色" in out["error"]

    monkeypatch.setattr(service, "_graph", lambda: ScriptedGraph(script))
    run_id = _run(uid)
    with session_scope() as s:
        service.execute(s, s.get(AnalysisRun, run_id))
    assert "查询角色" in _get(AnalysisRun, run_id).report["permission"]["lost"]


def test_progress_is_committed_and_late_worker_does_not_overwrite(factory, no_model, monkeypatch):
    """运行状态与步骤即时提交；运行被判定放弃后，迟到的执行者停止且不覆盖终态（审计 #11）。"""
    uid = factory.user()
    seen = {}

    def script(box):
        box.dispatch("search_metrics", {"keyword": ""})
        with session_scope() as s:  # 其他连接能看到 running 与已完成的步骤
            seen["status"] = s.get(AnalysisRun, box.run.id).status
            seen["steps"] = len(list(s.scalars(select(AnalysisStep).where(AnalysisStep.run_id == box.run.id))))
            service.finalize_abandoned(s, box.run.id, "worker 已退出（测试）")
        seen["after"] = json.loads(box.dispatch("search_metrics", {"keyword": ""}))
        box.report = {"title": "迟到的结果", "summary": "", "claims": []}

    monkeypatch.setattr(service, "_graph", lambda: ScriptedGraph(script))
    run_id = _run(uid)
    with session_scope() as s:
        service.execute(s, s.get(AnalysisRun, run_id))
    assert seen["status"] == "running" and seen["steps"] == 1
    assert "不再执行" in seen["after"]["error"]
    run = _get(AnalysisRun, run_id)
    assert run.status == "failed" and run.error == "worker 已退出（测试）" and run.report.get("title") is None


def test_abandoned_jobs_finalize_linked_runs(factory):
    uid = factory.user()
    direct, from_task = _run(uid, "running"), _run(uid, "queued")
    old = utcnow() - timedelta(hours=2)
    with session_scope() as s:
        a = Job(kind="analysis_run", payload={"run_id": direct}, status="running", started_at=old)
        b = Job(kind="analysis", payload={"task_id": "t"}, result={"analysis_run_id": from_task}, status="running",
                started_at=old)
        s.add_all([a, b])
        s.flush()
        ids = {a.id, b.id}
    assert ids <= set(jobs.abandoned_jobs(older_than_seconds=60))
    for run_id in (direct, from_task):
        run = _get(AnalysisRun, run_id)
        assert run.status == "failed" and "worker 可能已退出" in run.error and run.finished_at


def test_failed_analysis_marks_job_failed(factory, no_model, monkeypatch):
    def script(box):
        raise RuntimeError("模型超时（测试）")

    monkeypatch.setattr(service, "_graph", lambda: ScriptedGraph(script))
    uid = factory.user()
    run_id = _run(uid)
    with session_scope() as s:
        job_id = jobs._enqueue(s, "analysis_run", {"run_id": run_id}, uid)
    jobs.run_job(job_id)
    job, run = _get(Job, job_id), _get(AnalysisRun, run_id)
    assert run.status == "failed" and "模型超时" in run.error
    assert job.status == "failed" and "模型超时" in job.error and job.result["analysis_run_id"] == run_id


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
    assert woke == [("analysis_run", "queued")]
    with session_scope() as s:
        assert s.scalar(select(Job.id).where(Job.payload["run_id"].as_string() == view["id"])) is not None


def test_scheduled_job_for_paused_task_is_skipped(factory, no_model, monkeypatch):
    """入队后任务被暂停：worker 在任务锁下重新检查，跳过且不创建运行，Job 记为 skipped（审计 #12、#13）。"""
    monkeypatch.setattr(service, "_graph", lambda: pytest.fail("不应执行分析"))
    uid = factory.user()
    with session_scope() as s:
        task = AnalysisTask(title="定时", question="上个月成交额为什么变化？", owner_id=uid, window={},
                            schedule_cron="0 9 1 * *", status="active", next_run_at=utcnow())
        s.add(task)
        s.flush()
        task_id = task.id
        job_id = jobs._enqueue(s, "analysis", {"task_id": task_id, "trigger": "schedule"}, uid)
    with session_scope() as s:  # 用户在 worker 消费前暂停
        t = s.get(AnalysisTask, task_id)
        t.status, t.paused_reason, t.next_run_at = "paused", "手动暂停", None
    jobs.run_job(job_id)
    job, task = _get(Job, job_id), _get(AnalysisTask, task_id)
    assert job.status == "skipped" and job.result["outcome"] == "skipped" and "暂停" in job.result["reason"]
    assert task.status == "paused" and task.next_run_at is None and task.last_run_id is None
    with session_scope() as s:
        assert s.scalar(select(AnalysisRun.id).where(AnalysisRun.task_id == task_id)) is None


def test_permission_denial_during_task_pauses_and_notifies(factory, no_model, monkeypatch):
    """发起人仍能看到别的口径，但任务需要的指标已无权访问：执行中的明确拒绝让任务暂停并通知（审计 #14）。"""
    admin, uid = factory.user(roles=("admin",)), factory.user()
    factory.metric(admin, member_id=uid)  # 仍可见的另一个域
    hidden = factory.metric(admin)  # 任务要用、但发起人不在其业务域的指标

    def script(box):
        out = json.loads(box.dispatch("query_metric", {
            "metric_codes": [hidden], "start_date": "2026-09-01", "end_date": "2026-09-30", "purpose": "取数"}))
        assert out["status"] == "denied" and box.budget.used_queries == 0
        box.report = {"title": "部分完成", "summary": "无法取数。", "claims": []}

    monkeypatch.setattr(service, "_graph", lambda: ScriptedGraph(script))
    with session_scope() as s:
        task = AnalysisTask(title="归因", question="上个月成交额为什么变化？", owner_id=uid, window={},
                            schedule_cron="0 9 1 * *", status="active", next_run_at=utcnow())
        s.add(task)
        s.flush()
        task_id = task.id
        job_id = jobs._enqueue(s, "analysis", {"task_id": task_id, "trigger": "schedule"}, uid)
    jobs.run_job(job_id)
    task, job = _get(AnalysisTask, task_id), _get(Job, job_id)
    assert task.status == "paused" and hidden in task.paused_reason and task.next_run_at is None
    assert job.status == "skipped" and job.result["outcome"] == "paused" and job.result["analysis_run_id"]
    run = _get(AnalysisRun, job.result["analysis_run_id"])
    assert run.report["permission"]["denials"][0]["metric_code"] == hidden
    with session_scope() as s:
        note = s.scalar(select(Notification).where(Notification.user_id == uid, Notification.kind == "analysis_paused"))
        assert note.link == "/analysis/tasks" and hidden in note.body
