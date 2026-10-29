"""迁移 0008（分析）在有数据的库上升级 → 降级到 0007（变更留底）→ 再升级（独立测试库）。

0007 的历史表只追加：降级、再升级 0008 不能改动它们已有的行（审计、变更记录照常保留）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

pytestmark = pytest.mark.db

TABLES = {"analysis_task", "analysis_run", "analysis_step", "analysis_dataset", "notification"}


def test_0008_upgrade_downgrade_upgrade_with_data(meta_db, factory):
    from alembic import command

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
    engine = get_engine()
    cfg = alembic_config()

    with engine.connect() as conn:
        audit_before = conn.scalar(text("SELECT count(*) FROM audit_event"))
    command.downgrade(cfg, "0007")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0007"
        assert conn.scalar(text("SELECT count(*) FROM audit_event")) == audit_before  # 只追加的历史不受影响
        assert not TABLES & set(inspect(conn).get_table_names())
        assert conn.scalar(text("SELECT count(*) FROM app_user WHERE id = :id"), {"id": uid}) == 1  # 已有数据保留

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0008"
        assert TABLES <= set(inspect(conn).get_table_names())
        assert conn.scalar(text("SELECT count(*) FROM analysis_run")) == 0
    with session_scope() as s:  # 再升级后可以正常写入
        s.add(AnalysisRun(user_id=uid, question="q2", trigger="manual", status="queued"))
        assert s.get(User, uid) is not None
