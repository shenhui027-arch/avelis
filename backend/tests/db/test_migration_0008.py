"""迁移 0008（分析）在有数据的库上升级 → 降级到 0007（变更留底）→ 再升级（独立测试库）。

0007 的历史表只追加：降级、再升级 0008 不能改动它们已有的行（审计、变更记录照常保留）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

pytestmark = pytest.mark.db

TABLES = {"analysis_task", "analysis_run", "analysis_step", "analysis_dataset", "notification"}


def _history(engine) -> tuple[list[dict], dict]:
    """本测试写入的审计行（全部列，含哈希链字段）与整条历史链的核验结果（REPEATABLE READ 下同一快照）。"""
    from sqlalchemy.orm import Session

    from avelis.history import integrity

    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="REPEATABLE READ")
        with Session(bind=conn) as session:
            rows = [dict(r) for r in session.execute(text(
                "SELECT * FROM audit_event WHERE action LIKE 'test.migration.%' ORDER BY action")).mappings()]
            return rows, integrity.verify(session)


def test_0008_upgrade_downgrade_upgrade_with_data(meta_db, factory):
    from alembic import command

    from avelis import audit
    from avelis.db import get_engine, session_scope
    from avelis.devtools import alembic_config
    from avelis.models import AnalysisDataset, AnalysisRun, AnalysisStep, AnalysisTask, Notification, User

    uid = factory.user()
    with session_scope() as s:
        task = AnalysisTask(title="t", question="q", owner_id=uid, window={}, status="active")
        s.add(task)
        s.flush()
        run = AnalysisRun(user_id=uid, task_id=task.id, question="q", trigger="manual", status="succeeded",
                          report={"title": "r"})
        s.add(run)
        s.flush()
        ds = AnalysisDataset(run_id=run.id, seq=1, kind="calc", title="D1", inputs=[], columns=["a"], rows=[[1]],
                             truncated=False, meta={})
        s.add(ds)
        s.flush()
        s.add(AnalysisStep(run_id=run.id, seq=1, kind="tool", tool="derive", args={}, status="ok", output={},
                           dataset_id=ds.id))
        s.add(Notification(user_id=uid, kind="analysis_finished", title="完成", link="/analysis/tasks"))
        for i in range(3):  # 历史审计行：降级、再升级前后内容与哈希链都不能变
            audit.record(s, s.get(User, uid), f"test.migration.{i}", "analysis_run", run.id, step=i)
    engine = get_engine()
    cfg = alembic_config()

    before, chain = _history(engine)
    assert len(before) == 3 and chain["ok"] and chain["checked_rows"] >= 3, chain
    command.downgrade(cfg, "0007")
    after_down, chain_down = _history(engine)
    assert after_down == before and chain_down["ok"] and chain_down["head"] == chain["head"]
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0007"
        assert not TABLES & set(inspect(conn).get_table_names())
        assert conn.scalar(text("SELECT count(*) FROM app_user WHERE id = :id"), {"id": uid}) == 1  # 已有数据保留

    command.upgrade(cfg, "head")
    after_up, chain_up = _history(engine)
    assert after_up == before and chain_up["ok"] and chain_up["head"] == chain["head"]
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0008"
        assert TABLES <= set(inspect(conn).get_table_names())
        assert conn.scalar(text("SELECT count(*) FROM analysis_run")) == 0
    with session_scope() as s:  # 再升级后可以正常写入
        s.add(AnalysisRun(user_id=uid, question="q2", trigger="manual", status="queued"))
        assert s.get(User, uid) is not None
