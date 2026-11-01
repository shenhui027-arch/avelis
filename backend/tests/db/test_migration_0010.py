(
    """迁移 0010（变化显著性阈值）：在有口径版本的库上升级 → 降级到 0009 → 再升级（独立测试库）。

material_change_ratio 是可选的分析元数据：降级去掉这一列，已有口径版本保留；"""
    """再升级后为空（用系统默认 1%），可以正常写入。
"""
)

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import inspect, select, text

pytestmark = pytest.mark.db


def test_0010_adds_optional_material_change_ratio_and_round_trips(meta_db, factory):
    from alembic import command

    from avelis.db import get_engine, session_scope
    from avelis.devtools import alembic_config
    from avelis.models import Metric, MetricVersion

    owner = factory.user(roles=("steward",))
    code = factory.metric(owner)
    with session_scope() as s:
        metric = s.scalar(select(Metric).where(Metric.code == code))
        version = MetricVersion(
            metric_id=metric.id,
            version_no=1,
            status="draft",
            description="d",
            kind="base",
            expression="SUM(x)",
            time_column="d",
            timezone="Asia/Shanghai",
            created_by=owner,
            material_change_ratio=Decimal("0.02"),
        )
        s.add(version)
        s.flush()
        version_id = version.id
    engine, cfg = get_engine(), alembic_config()

    command.downgrade(cfg, "0009")
    with engine.connect() as conn:
        assert "material_change_ratio" not in {c["name"] for c in inspect(conn).get_columns("metric_version")}
        assert conn.scalar(text("SELECT count(*) FROM metric_version WHERE id = :id"), {"id": version_id}) == 1

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert (
            conn.scalar(text("SELECT material_change_ratio FROM metric_version WHERE id = :id"), {"id": version_id})
            is None
        )
    with session_scope() as s:
        s.get(MetricVersion, version_id).material_change_ratio = Decimal("0.005")
    with session_scope() as s:
        assert s.get(MetricVersion, version_id).material_change_ratio == Decimal("0.005")
