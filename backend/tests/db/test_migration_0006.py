"""迁移 0006 在有数据的库上升级 → 降级 → 再升级（独立测试库）。"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

pytestmark = pytest.mark.db

TABLES = {"analysis_task", "analysis_run", "analysis_step", "analysis_dataset", "notification"}


def test_0006_upgrade_downgrade_upgrade_with_data(meta_db, factory):
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

    command.downgrade(cfg, "0005")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0005"
        assert not TABLES & set(inspect(conn).get_table_names())
        assert conn.scalar(text("SELECT count(*) FROM app_user WHERE id = :id"), {"id": uid}) == 1  # 已有数据保留

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0006"
        assert TABLES <= set(inspect(conn).get_table_names())
        assert conn.scalar(text("SELECT count(*) FROM analysis_run")) == 0
    with session_scope() as s:  # 再升级后可以正常写入
        s.add(AnalysisRun(user_id=uid, question="q2", trigger="manual", status="queued"))
        assert s.get(User, uid) is not None
