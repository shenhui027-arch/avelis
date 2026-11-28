"""Trino 适配器真实联调：真实 Trino + 开发者自己的元数据库，走采集服务、视图血缘、问数执行与执行前核对。

环境：deploy/demo/trino/compose.yaml（独立 Compose 项目 avelis-trino，不碰共享演示环境）。运行：
  AVELIS_DATABASE_URL=postgresql+psycopg://avelis:…@127.0.0.1:29432/avelis_trino_test \\
  AVELIS_TRINO_TEST_PORT=28080 uv run pytest -m db tests/metadb/test_trino_collect.py
未设置 AVELIS_TRINO_TEST_PORT 时整组跳过。测试重建 memory.avelis_it 与 pg.public 里的联调表。
"""

from __future__ import annotations

import os
import time

import pytest
import trino
from sqlalchemy import select

from avelis.collect.service import run_collection
from avelis.connectors.base import QueryError
from avelis.connectors.trino import TrinoConnector
from avelis.db import session_scope
from avelis.engines import get_engine
from avelis.models import (
    Asset,
    AssetVersion,
    ChangeEvent,
    ChangeSet,
    CollectionRun,
    ColumnVersion,
    Datasource,
    Definition,
    LineageEdge,
    ParseResult,
)

pytestmark = pytest.mark.db

SCHEMA = "avelis_it"


def _settings():
    port = os.environ.get("AVELIS_TRINO_TEST_PORT")
    if not port:
        pytest.skip("未设置 AVELIS_TRINO_TEST_PORT：先启动 deploy/demo/trino/compose.yaml")
    return dict(host=os.environ.get("AVELIS_TRINO_TEST_HOST", "127.0.0.1"), port=int(port))


def _execute(t: dict, *statements: str) -> None:
    conn = trino.dbapi.connect(host=t["host"], port=t["port"], user="avelis", catalog="memory", schema="default")
    try:
        cur = conn.cursor()
        for statement in statements:
            cur.execute(statement)
            cur.fetchall()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def tr(metadb):
    settings = _settings()
    _execute(settings, f"DROP SCHEMA IF EXISTS memory.{SCHEMA} CASCADE")
    _execute(
        settings,
        f"CREATE SCHEMA memory.{SCHEMA}",
        "DROP TABLE IF EXISTS pg.public.avelis_it_orders",
        "CREATE TABLE pg.public.avelis_it_orders "
        "(order_id bigint, user_id integer, amount decimal(18,2), status varchar(16))",
        "COMMENT ON TABLE pg.public.avelis_it_orders IS '订单'",
        "COMMENT ON COLUMN pg.public.avelis_it_orders.amount IS '金额'",
        "INSERT INTO pg.public.avelis_it_orders VALUES "
        "(1, 10, 12.50, 'paid'), (2, 11, 3.00, 'void'), (3, 11, 7.25, 'paid')",
        f"CREATE TABLE memory.{SCHEMA}.users (user_id integer, city varchar)",
        f"COMMENT ON TABLE memory.{SCHEMA}.users IS '用户'",
        f"INSERT INTO memory.{SCHEMA}.users VALUES (10, '杭州'), (11, '上海')",
    )
    # 视图里同 Catalog 的表不写 Catalog 与模式（相对视图所在位置），跨 Catalog 的表写全三段
    conn = trino.dbapi.connect(
        host=settings["host"], port=settings["port"], user="avelis", catalog="memory", schema=SCHEMA
    )
    try:
        cur = conn.cursor()
        cur.execute(
            f"CREATE VIEW memory.{SCHEMA}.city_sales COMMENT '城市销售' AS "
            "SELECT u.city, SUM(o.amount) AS sales_amount FROM pg.public.avelis_it_orders o "
            "JOIN users u ON u.user_id = o.user_id WHERE o.status <> 'void' GROUP BY u.city"
        )
        cur.fetchall()
    finally:
        conn.close()
    with session_scope() as s:
        ds = Datasource(
            name="Trino 联调",
            kind="trino",
            host=settings["host"],
            port=settings["port"],
            options={"catalog": "memory", "schemas": [SCHEMA]},
        )
        s.add(ds)
        s.flush()
        settings["ds"] = ds.id
    return settings


def _assets(ds_id: str) -> dict[str, tuple[Asset, AssetVersion]]:
    with session_scope() as s:
        rows = s.execute(
            select(Asset, AssetVersion)
            .join(AssetVersion, AssetVersion.id == Asset.current_version_id)
            .where(Asset.datasource_id == ds_id, Asset.status == "active")
        ).all()
        for asset, version in rows:
            s.expunge(asset)
            s.expunge(version)
        return {f"{a.catalog_name}.{a.db_name}.{a.name}": (a, v) for a, v in rows}


def _columns(version_id: str) -> list[tuple]:
    with session_scope() as s:
        return [
            (c.name, c.data_type, c.nullable, c.comment)
            for c in s.scalars(
                select(ColumnVersion)
                .where(ColumnVersion.asset_version_id == version_id)
                .order_by(ColumnVersion.ordinal)
            )
        ]


def _querier(t: dict) -> TrinoConnector:
    return TrinoConnector(t["host"], t["port"], "avelis", "", {"catalog": "memory", "schemas": [SCHEMA]})


def test_collect_tables_views_and_cross_catalog_lineage(tr):
    run_id = run_collection(tr["ds"], trigger="manual")
    with session_scope() as s:
        run = s.get(CollectionRun, run_id)
        assert run.status == "succeeded" and run.stats["warnings"] == []
        identity = s.get(Datasource, tr["ds"]).server_identity
    assert identity["version"] and identity.get("instance_id") is None
    assets = _assets(tr["ds"])
    assert set(assets) == {f"memory.{SCHEMA}.users", f"memory.{SCHEMA}.city_sales"}
    users, users_v = assets[f"memory.{SCHEMA}.users"]
    assert users.kind == "table" and users.physical_id is None and users_v.comment == "用户"
    assert _columns(users_v.id) == [("user_id", "integer", True, None), ("city", "varchar", True, None)]
    view, view_v = assets[f"memory.{SCHEMA}.city_sales"]
    assert view.kind == "view"
    with session_scope() as s:
        definition = s.scalar(select(Definition).where(Definition.asset_version_id == view_v.id))
        assert definition.dialect == "trino"
        parsed = s.scalar(select(ParseResult).where(ParseResult.definition_id == definition.id))
        # 跨 Catalog 的订单表不在本数据源的采集范围：解析为 partial 并写明原因，已能确定的边照常生成
        assert parsed.status == "partial" and "pg.public.avelis_it_orders" in parsed.reason, parsed.reason
        edges = {
            (e.target_column, e.source_ref, e.source_column, e.relation, e.source_asset_id is not None)
            for e in s.scalars(
                select(LineageEdge).where(LineageEdge.target_asset_id == view.id, LineageEdge.status == "active")
            )
        }
    # 同 Catalog 的 users 绑定到已采集资产；pg Catalog 的订单表不在本数据源，记为未采集来源
    assert ("city", f"memory.{SCHEMA}.users", "city", "value_direct", True) in edges
    assert ("sales_amount", "pg.public.avelis_it_orders", "amount", "value_aggregated", False) in edges
    assert any(e[2] == "status" and e[3] == "filter" for e in edges)


def test_recollect_detects_new_column(tr):
    _execute(tr, f"ALTER TABLE memory.{SCHEMA}.users ADD COLUMN region varchar")
    run_id = run_collection(tr["ds"], trigger="manual")
    with session_scope() as s:
        events = {
            (e.kind, e.column_name)
            for e in s.scalars(
                select(ChangeEvent)
                .join(ChangeSet, ChangeSet.id == ChangeEvent.change_set_id)
                .where(ChangeSet.run_id == run_id)
            )
        }
    assert ("column_added", "region") in events


def test_query_runs_with_limits_timezone_and_cross_catalog_view(tr):
    result = _querier(tr).execute_select(
        f"SELECT city, sales_amount FROM memory.{SCHEMA}.city_sales ORDER BY city",
        timeout_seconds=60,
        mem_limit_bytes=512 * 1024**2,
        max_rows=1,
        timezone="Asia/Shanghai",
    )
    assert result.columns == ["city", "sales_amount"] and result.truncated
    assert [(c, float(v)) for c, v in result.rows] in ([("上海", 7.25)], [("杭州", 12.5)])
    tz = _querier(tr).execute_select(
        "SELECT current_timezone()", timeout_seconds=30, mem_limit_bytes=0, max_rows=1, timezone="Asia/Shanghai"
    )
    assert tz.rows == [["Asia/Shanghai"]]


def test_timeout_memory_and_missing_objects_are_classified(tr):
    started = time.monotonic()
    with pytest.raises(QueryError) as caught:
        _querier(tr).execute_select(
            "SELECT count(*) FROM tpch.sf1000.lineitem a CROSS JOIN tpch.sf1000.lineitem b",
            timeout_seconds=3,
            mem_limit_bytes=0,
            max_rows=10,
        )
    assert caught.value.code == "timeout" and caught.value.db_errno == "EXCEEDED_TIME_LIMIT"
    assert time.monotonic() - started < 20
    with pytest.raises(QueryError) as caught:
        _querier(tr).execute_select(
            "SELECT orderkey, count(*) FROM tpch.sf1.lineitem GROUP BY orderkey ORDER BY 2 DESC",
            timeout_seconds=60,
            mem_limit_bytes=1024**2,
            max_rows=10,
        )
    assert caught.value.code == "other" and "MEMORY" in caught.value.db_errno
    with pytest.raises(QueryError) as caught:
        _querier(tr).execute_select(
            f"SELECT * FROM memory.{SCHEMA}.nope", timeout_seconds=30, mem_limit_bytes=0, max_rows=10
        )
    assert caught.value.code == "invalid_object" and caught.value.db_errno == "TABLE_NOT_FOUND"


def test_live_check_matches_collected_definitions_and_columns(tr):
    from avelis.lineage.sql import canonical_hash_of

    run_collection(tr["ds"], trigger="manual")
    assets = _assets(tr["ds"])
    described = _querier(tr).describe_objects([(SCHEMA, "users"), (SCHEMA, "city_sales"), (SCHEMA, "missing")])
    for name in ("users", "city_sales"):
        _, version = assets[f"memory.{SCHEMA}.{name}"]
        assert described[(SCHEMA, name)]["columns"] == [[c[0], c[1]] for c in _columns(version.id)]
    _, view_v = assets[f"memory.{SCHEMA}.city_sales"]
    with session_scope() as s:
        stored = s.scalar(select(Definition.canonical_hash).where(Definition.asset_version_id == view_v.id))
    dialect = get_engine("trino").dialect_for({"catalog": "memory"})
    assert canonical_hash_of(described[(SCHEMA, "city_sales")]["definition"], dialect)[1] == stored
    assert described[(SCHEMA, "missing")] == {"columns": [], "definition": None, "physical_id": None}


def test_explore_sql_with_week_and_quarter_runs_on_trino(tr):
    from tests.unit.test_analysis_explore import ok

    v = ok(
        "trino",
        "SELECT region, SUM(amount) OVER (PARTITION BY region) AS w, DATE_TRUNC('week', event_date) AS wk, "
        "DATE_TRUNC('quarter', event_date) AS q, event_date + INTERVAL '1' QUARTER AS q1, "
        "event_date - INTERVAL '1' WEEK AS w1 FROM d1.facts",
    )
    sql = v.sql.replace(
        '"memory"."d1"."facts"',
        "(SELECT status AS region, amount, DATE '2026-11-01' AS event_date FROM pg.public.avelis_it_orders)",
    )
    result = _querier(tr).execute_explore(sql, timeout_seconds=60, max_rows=10)
    row = dict(zip(result.columns, result.rows[0], strict=True))
    assert str(row["wk"]) == "2026-10-26" and str(row["q"]) == "2026-10-01"
    assert str(row["q1"]) == "2027-02-01" and str(row["w1"]) == "2026-10-25"
