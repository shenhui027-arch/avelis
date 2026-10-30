"""迁移 0009（口径单位）：在有口径版本的库上升级 → 降级到 0008 → 再升级（独立测试库）。

单位是可选的显示元数据：降级去掉两列，已有口径版本保留；再升级后 unit 为空、unit_scale 为默认 1，可以正常写入。
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, select, text

pytestmark = pytest.mark.db


def test_0009_adds_optional_unit_columns_and_round_trips(meta_db, factory):
    from alembic import command

    from avelis.db import get_engine, session_scope
    from avelis.devtools import alembic_config
    from avelis.models import Metric, MetricVersion

    owner = factory.user(roles=("steward",))
    code = factory.metric(owner)
    with session_scope() as s:
        metric = s.scalar(select(Metric).where(Metric.code == code))
        version = MetricVersion(metric_id=metric.id, version_no=1, status="draft", description="d", kind="derived",
                                expression="a / b", time_column="", timezone="Asia/Shanghai", created_by=owner,
                                unit="元", unit_scale=10000)
        s.add(version)
        s.flush()
        version_id = version.id
    engine, cfg = get_engine(), alembic_config()

    command.downgrade(cfg, "0008")
    with engine.connect() as conn:
        columns = {c["name"] for c in inspect(conn).get_columns("metric_version")}
        assert "unit" not in columns and "unit_scale" not in columns
        assert conn.scalar(text("SELECT count(*) FROM metric_version WHERE id = :id"), {"id": version_id}) == 1

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0009"
        row = conn.execute(text("SELECT unit, unit_scale FROM metric_version WHERE id = :id"), {"id": version_id}).one()
        assert row.unit is None and row.unit_scale == 1
    with session_scope() as s:  # 再升级后可以正常写入单位
        s.get(MetricVersion, version_id).unit = "单"
    with session_scope() as s:
        assert s.get(MetricVersion, version_id).unit == "单"
