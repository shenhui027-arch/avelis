"""对话归档与删除的 HTTP、事务和外键范围验证；仅使用离线 SQLite。"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis.analysis import service
from avelis.analysis.conversation_context import refresh_followup_context
from avelis.api.deps import current_user
from avelis.ask import agent as ask_agent
from avelis.db import utcnow
from avelis.models import (
    AnalysisDataset,
    AnalysisMessage,
    AnalysisRun,
    AnalysisStep,
    AnalysisTask,
    AuditEvent,
    Experience,
    ExperienceVersion,
    HistoryAnchor,
    HistoryChainHead,
    HistoryMark,
    JoinPath,
    JoinPathApproval,
    Notification,
    User,
    UserRole,
)
from tests.unit.test_analysis_conversations import followup, model  # noqa: F401  pytest 夹具按参数名注入
from tests.unit.test_analysis_dataset_api import api  # noqa: F401  pytest 夹具按参数名注入

PATH = "/api/analysis/conversations"


@pytest.fixture(autouse=True)
def checkpointer(monkeypatch):
    threads = set()
    saver = SimpleNamespace(threads=threads, delete_thread=Mock(side_effect=threads.discard))
    monkeypatch.setattr(ask_agent, "checkpointer", lambda: saver)
    return saver


def _finished_followup(api, parent=None):
    run = (service.followup_run(api.session, api.user, parent.id, "继续查原因")
           if parent is not None else followup(api))
    run.status = "succeeded"
    api.session.flush()
    return run


def _content(run):
    return deepcopy((run.question, run.report, run.checkpoint, run.plan, run.window, run.conversation_id))


def test_archive_filter_detail_and_restore_preserve_analysis_and_followup_context(api, model):
    child = _finished_followup(api)
    api.session.commit()
    context = refresh_followup_context(api.session, api.user, child)
    before = {run.id: _content(run) for run in (api.run, child)}

    response = api.client.post(f"{PATH}/{api.run.id}/archive")
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["archived_at"] and detail["turn_count"] == 2
    assert [run["id"] for run in detail["runs"]] == [api.run.id, child.id]
    assert api.client.get(PATH).json() == []
    archived = api.client.get(PATH, params={"archived": "true"}).json()
    assert len(archived) == 1 and archived[0]["id"] == api.run.id
    assert archived[0]["archived_at"] == detail["archived_at"]
    assert api.client.get(PATH, params={"archived": "false"}).json() == []
    assert api.client.get(f"{PATH}/{api.run.id}").json() == detail
    assert api.client.get(f"/api/analysis/runs/{child.id}").status_code == 200
    assert {run.id: _content(run) for run in (api.run, child)} == before

    renamed = api.client.patch(f"{PATH}/{api.run.id}", json={"display_title": "归档中的显示名"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "归档中的显示名"
    assert refresh_followup_context(api.session, api.user, child) == context
    next_run = service.followup_run(api.session, api.user, child.id, "继续复核")
    api.session.commit()
    inherited = next_run.checkpoint["inherited"]
    assert next_run.conversation_id == api.run.id and inherited["user_text"] == "继续复核"
    assert "归档中的显示名" not in str(inherited)
    assert api.client.get(PATH).json() == []

    # 恢复允许已归档对话存在新的排队轮次；只移除归档标记。
    response = api.client.post(f"{PATH}/{api.run.id}/restore")
    assert response.status_code == 200, response.text
    assert response.json()["archived_at"] is None
    assert api.client.get(PATH, params={"archived": "true"}).json() == []
    visible = api.client.get(PATH).json()
    assert len(visible) == 1 and visible[0]["title"] == "归档中的显示名"
    assert visible[0]["turn_count"] == 3 and visible[0]["status"] == "queued"
    assert {run.id: _content(run) for run in (api.run, child)} == before
    actions = set(api.session.scalars(select(AuditEvent.action)))
    assert {"analysis.conversation.archive", "analysis.conversation.restore"} <= actions


@pytest.mark.parametrize("status", ["queued", "running", "waiting"])
@pytest.mark.parametrize("busy_turn", ["root", "child"])
@pytest.mark.parametrize("operation", ["archive", "delete"])
def test_any_busy_turn_blocks_archive_and_delete_without_mutation(api, model, status, busy_turn, operation):
    child = _finished_followup(api)
    (api.run if busy_turn == "root" else child).status = status
    api.session.commit()
    before = {run.id: _content(run) for run in (api.run, child)}
    response = (api.client.delete(f"{PATH}/{api.run.id}") if operation == "delete"
                else api.client.post(f"{PATH}/{api.run.id}/archive"))
    assert response.status_code == 409, response.text
    assert api.run.archived_at is None
    assert {run.id: _content(run) for run in (api.run, child)} == before
    assert set(api.session.scalars(select(AnalysisRun.id))) == {api.run.id, child.id}
    assert api.session.scalar(select(AuditEvent).where(
        AuditEvent.action == f"analysis.conversation.{operation}")) is None


@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("operation", ["archive", "restore", "delete"])
def test_only_initiator_can_archive_restore_or_delete(api, admin, operation):
    actor = User(id="other", username="other", display_name="其他用户", password_hash="unused")
    api.session.add(actor)
    if admin:
        api.session.add(UserRole(user_id=actor.id, role="admin"))
    api.run.archived_at = utcnow() if operation == "restore" else None
    api.session.commit()
    api.session.refresh(api.run)
    archived_at = api.run.archived_at
    api.app.dependency_overrides[current_user] = lambda: actor
    response = (api.client.delete(f"{PATH}/{api.run.id}") if operation == "delete"
                else api.client.post(f"{PATH}/{api.run.id}/{operation}"))
    assert response.status_code == (403 if admin else 404), response.text
    assert api.client.get(PATH).json() == []
    assert api.client.get(PATH, params={"archived": "true"}).json() == []
    api.session.refresh(api.run)
    assert api.run.archived_at == archived_at
    assert api.session.get(AnalysisDataset, api.dataset.id) is not None
    assert api.session.scalar(select(AuditEvent).where(
        AuditEvent.action == f"analysis.conversation.{operation}")) is None


@pytest.mark.parametrize("operation", ["archive", "restore", "delete"])
def test_lifecycle_requires_existing_root_conversation(api, model, operation):
    child = _finished_followup(api)
    api.session.commit()
    for identifier in (child.id, "missing"):
        response = (api.client.delete(f"{PATH}/{identifier}") if operation == "delete"
                    else api.client.post(f"{PATH}/{identifier}/{operation}"))
        assert response.status_code == 404, response.text
    assert api.session.get(AnalysisRun, api.run.id) is not None


def _seed_delete_scope(api):
    # 打开真实的外键检查，验证多轮自引用及步骤/数据集/消息的删除顺序。
    api.session.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
    assert api.session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    child = _finished_followup(api)
    grandchild = _finished_followup(api, child)
    api.run.display_title = "待删除的对话"
    api.run.checkpoint = {"candidates": [{"id": "root-candidate", "source": {"run_id": api.run.id}}]}
    other = AnalysisRun(id="unrelated-run", user_id=api.user.id, question="另一对话", trigger="manual",
                        status="stopped", checkpoint={"candidates": [{"id": "unrelated-candidate"}]})
    api.session.add(other)
    api.session.flush()
    for run in (api.run, child, grandchild, other):
        dataset = api.session.scalar(select(AnalysisDataset).where(AnalysisDataset.run_id == run.id))
        if dataset is None:
            dataset = AnalysisDataset(run_id=run.id, seq=1, kind="context", title="保留数据", columns=["value"],
                                      rows=[[1]])
            api.session.add(dataset)
            api.session.flush()
        api.session.add_all([
            AnalysisStep(run_id=run.id, seq=1, kind="tool", tool="view_dataset", status="ok", dataset_id=dataset.id),
            AnalysisMessage(run_id=run.id, user_id=api.user.id, text="原文补充"),
        ])
    task = AnalysisTask(id="saved-task", owner_id=api.user.id, title="继续运行的任务", question=api.run.question,
                        status="active", schedule_cron="0 9 * * *", last_run_id=grandchild.id)
    other_task = AnalysisTask(id="unrelated-task", owner_id=api.user.id, title="其他任务", question="其他问题",
                              status="active", last_run_id=other.id)
    experience = Experience(id="saved-experience", owner_id=api.user.id)
    api.session.add_all([task, other_task, experience])
    api.session.flush()
    version = ExperienceVersion(id="saved-experience-version", experience_id=experience.id, version=1,
                                kind="rule", content="独立保存的经验", confirmed_by=api.user.id,
                                refs=[{"object_type": "analysis_run", "object_id": api.run.id}],
                                source={"run_id": child.id, "checkpoint": "conclusion", "user_text": "保留"})
    path = JoinPath(id="saved-path", code="saved-path", name="已提交的关联路径", left_asset_id=api.asset.id,
                    right_asset_id=api.asset.id, left_columns=["key"], right_columns=["key"], cardinality="11",
                    status="pending", created_by=api.user.id, source={"run_id": grandchild.id})
    api.session.add_all([version, path])
    api.session.flush()
    approval = JoinPathApproval(id="saved-approval", join_path_id=path.id, domain_id="asset-domain",
                                user_id=api.user.id, decision="approved")
    old_audit = AuditEvent(id="old-audit", actor_id=api.user.id, actor_name=api.user.username,
                           action="analysis.followup", object_type="analysis_run", object_id=child.id,
                           detail={"parent_run_id": api.run.id}, chain_seq=1, prev_hash="0" * 64, row_hash="a" * 64)
    mark = HistoryMark(id="history-mark", table_name="audit_event", row_id=old_audit.id, column_name="detail",
                       old_value={}, new_value={"run_id": child.id}, chain_seq=2, prev_hash="a" * 64, row_hash="b" * 64)
    anchor = HistoryAnchor(id="history-anchor", kind="purge", cutoff=utcnow(), retention_days=90,
                           counts={}, gaps=[], actor_name=api.user.username,
                           chain_seq=3, prev_hash="b" * 64, row_hash="c" * 64)
    head = HistoryChainHead(id=1, last_seq=3, last_hash="c" * 64)
    notifications = [Notification(id=f"notice-{i}", user_id=api.user.id, kind="analysis_done", title="分析通知",
                                  link=link) for i, link in enumerate([
        f"/analysis/runs/{api.run.id}", f"/analysis/runs/{child.id}", f"/analysis/runs/{grandchild.id}",
        f"/analysis/conversations/{api.run.id}", f"/analysis/runs/{other.id}",
        f"/analysis/runs/{api.run.id}-different", "/analysis/tasks", None,
    ])]
    api.session.add_all([approval, old_audit, mark, anchor, head, *notifications])
    api.session.commit()
    return {"deleted_ids": {api.run.id, child.id, grandchild.id}, "other_id": other.id,
            "experience_source": deepcopy(version.source), "path_source": deepcopy(path.source)}


def _assert_deleted_scope(session, scope):
    assert set(session.scalars(select(AnalysisRun.id))) == {scope["other_id"]}
    other = session.get(AnalysisRun, scope["other_id"])
    assert other.checkpoint == {"candidates": [{"id": "unrelated-candidate"}]}
    for table in (AnalysisStep, AnalysisDataset, AnalysisMessage):
        rows = list(session.scalars(select(table)))
        assert len(rows) == 1 and rows[0].run_id == scope["other_id"]
    assert session.get(AnalysisTask, "saved-task").last_run_id is None
    assert session.get(AnalysisTask, "saved-task").status == "active"
    assert session.get(AnalysisTask, "unrelated-task").last_run_id == scope["other_id"]
    assert session.get(Experience, "saved-experience").status == "active"
    version = session.get(ExperienceVersion, "saved-experience-version")
    assert version.content == "独立保存的经验" and version.source == scope["experience_source"]
    assert session.get(JoinPath, "saved-path").source == scope["path_source"]
    assert session.get(JoinPathApproval, "saved-approval").decision == "approved"
    assert all(session.get(Notification, f"notice-{i}").link is None for i in range(4))
    assert session.get(Notification, "notice-4").link == f"/analysis/runs/{scope['other_id']}"
    assert session.get(Notification, "notice-5").link == "/analysis/runs/run-different"
    assert session.get(Notification, "notice-6").link == "/analysis/tasks"
    assert session.get(Notification, "notice-7").link is None
    assert session.get(AuditEvent, "old-audit").row_hash == "a" * 64
    assert session.get(HistoryMark, "history-mark").row_hash == "b" * 64
    assert session.get(HistoryAnchor, "history-anchor").row_hash == "c" * 64
    assert session.get(HistoryChainHead, 1).last_hash == "c" * 64
    event = session.scalar(select(AuditEvent).where(AuditEvent.action == "analysis.conversation.delete"))
    assert event is not None and event.object_id == "run" and event.actor_id == "actor"
    assert event.detail == {"conversation_id": "run", "turn_count": 3, "title": "待删除的对话",
                            "checkpoint_threads": 3}


@pytest.mark.parametrize("archived", [False, True])
def test_delete_http_commits_full_scope_and_preserves_independent_objects(api, model, archived, checkpointer):
    scope = _seed_delete_scope(api)
    threads = {f"analysis:{run_id}" for run_id in scope["deleted_ids"]}
    checkpointer.threads.update(threads | {f"analysis:{scope['other_id']}"})
    if archived:
        response = api.client.post(f"{PATH}/run/archive")
        assert response.status_code == 200, response.text
    response = api.client.delete(f"{PATH}/run")
    assert response.status_code == 204, response.text
    assert response.content == b""
    assert {args.args[0] for args in checkpointer.delete_thread.call_args_list} == threads
    assert checkpointer.delete_thread.call_count == len(threads)
    assert checkpointer.threads == {f"analysis:{scope['other_id']}"}
    # 新会话读取，证明 HTTP 已提交，而非只在请求的 identity map 中消失。
    with Session(api.session.get_bind()) as independent:
        _assert_deleted_scope(independent, scope)
    for run_id in scope["deleted_ids"]:
        assert api.client.get(f"/api/analysis/runs/{run_id}").status_code == 404
    assert api.client.get(f"{PATH}/run").status_code == 404
    assert [item["id"] for item in api.client.get(PATH).json()] == [scope["other_id"]]
    assert api.client.get(PATH, params={"archived": "true"}).json() == []


def test_delete_never_executed_run_without_checkpoints(api, checkpointer):
    api.run.status = "stopped"
    api.run.started_at = None
    api.run.checkpoint = {}
    api.session.commit()
    response = api.client.delete(f"{PATH}/run")
    assert response.status_code == 204, response.text
    checkpointer.delete_thread.assert_called_once_with("analysis:run")
    assert api.session.get(AnalysisRun, "run") is None


def test_checkpoint_cleanup_failure_preserves_all_application_data(api, model, checkpointer):
    scope = _seed_delete_scope(api)
    checkpointer.threads.update(f"analysis:{run_id}" for run_id in scope["deleted_ids"])
    models = (AnalysisRun, AnalysisMessage, AnalysisStep, AnalysisDataset, AnalysisTask, Notification, AuditEvent)

    def application_rows(session):
        return {model: deepcopy([dict(row) for row in session.execute(
            select(model.__table__).order_by(model.id)).mappings()]) for model in models}

    before = application_rows(api.session)

    def delete_thread(thread_id):
        if checkpointer.delete_thread.call_count == 2:
            raise RuntimeError("检查点清理失败")
        checkpointer.threads.discard(thread_id)

    checkpointer.delete_thread.side_effect = delete_thread
    with pytest.raises(RuntimeError, match="检查点清理失败"):
        service.delete_conversation(api.session, api.user, "run")
    assert checkpointer.delete_thread.call_count == 2
    assert len(checkpointer.threads) == len(scope["deleted_ids"]) - 1
    assert application_rows(api.session) == before
    api.session.rollback()
    with Session(api.session.get_bind()) as independent:
        assert application_rows(independent) == before


def test_delete_service_remains_one_rollbackable_transaction(api, model):
    scope = _seed_delete_scope(api)
    before_audits = set(api.session.scalars(select(AuditEvent.id)))
    with pytest.raises(RuntimeError, match="模拟提交前失败"):
        service.delete_conversation(api.session, api.user, "run")
        _assert_deleted_scope(api.session, scope)
        raise RuntimeError("模拟提交前失败")
    api.session.rollback()
    assert set(api.session.scalars(select(AnalysisRun.id))) == scope["deleted_ids"] | {scope["other_id"]}
    for table in (AnalysisStep, AnalysisDataset, AnalysisMessage):
        assert len(list(api.session.scalars(select(table)))) == 4
    assert api.session.get(AnalysisRun, "run").checkpoint["candidates"][0]["id"] == "root-candidate"
    assert api.session.get(AnalysisTask, "saved-task").last_run_id in scope["deleted_ids"]
    assert api.session.get(Notification, "notice-0").link == "/analysis/runs/run"
    assert api.session.get(Notification, "notice-3").link == "/analysis/conversations/run"
    assert set(api.session.scalars(select(AuditEvent.id))) == before_audits


def test_archiving_task_conversation_does_not_pause_or_join_future_scheduled_runs(api, model):
    task = AnalysisTask(id="scheduled-task", owner_id=api.user.id, title="每天分析", question="定时分析问题",
                        status="active", schedule_cron="0 9 * * *", next_run_at=utcnow())
    api.session.add(task)
    api.session.flush()
    run = service.create_run(api.session, api.user, task.question, None, task=task, trigger="schedule")
    run.status = "succeeded"
    task.last_run_id = run.id
    api.session.commit()
    before_task = (task.question, deepcopy(task.window), task.status, task.schedule_cron, task.next_run_at,
                   task.last_run_id)
    response = api.client.post(f"{PATH}/{run.id}/archive")
    assert response.status_code == 200, response.text
    assert before_task == (task.question, task.window, task.status, task.schedule_cron, task.next_run_at,
                           task.last_run_id)
    next_run = service.create_run(api.session, api.user, task.question, None, task=task, trigger="schedule")
    api.session.commit()
    assert next_run.task_id == run.task_id == task.id
    assert next_run.conversation_id == next_run.id != run.conversation_id == run.id
    assert next_run.parent_run_id is None and next_run.archived_at is None
    assert run.id not in {item["id"] for item in api.client.get(PATH).json()}
    assert next_run.id in {item["id"] for item in api.client.get(PATH).json()}
    assert [item["id"] for item in api.client.get(PATH, params={"archived": "true"}).json()] == [run.id]
