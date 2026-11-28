"""Spark SQL 适配器真实联调：真实 Spark Thrift Server + 开发者自己的元数据库，走采集服务、视图血缘、问数与执行前核对。

环境：deploy/demo/spark/compose.yaml（独立 Compose 项目 avelis-spark，不碰共享演示环境）。运行：
  AVELIS_DATABASE_URL=postgresql+psycopg://avelis:…@127.0.0.1:22432/avelis_spark_test \\
  AVELIS_SPARK_TEST_PORT=21000 uv run pytest -m db tests/metadb/test_spark_collect.py
未设置 AVELIS_SPARK_TEST_PORT 时整组跳过。测试会重建 Spark 中的 avelis_it 库。
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from avelis.collect.service import run_collection
from avelis.connectors.base import QueryError
from avelis.connectors.spark import SparkConnector
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
    Grain,
    LineageEdge,
    ParseResult,
)

pytestmark = pytest.mark.db

DB = "avelis_it"


def _settings():
    port = os.environ.get("AVELIS_SPARK_TEST_PORT")
    if not port:
        pytest.skip("未设置 AVELIS_SPARK_TEST_PORT：先启动 deploy/demo/spark/compose.yaml")
    return dict(host=os.environ.get("AVELIS_SPARK_TEST_HOST", "127.0.0.1"), port=int(port))


def _connector(sp: dict) -> SparkConnector:
    return SparkConnector(sp["host"], sp["port"], "", "", {"auth": "NONE", "databases": [DB]})


def _execute(sp: dict, *statements: str) -> None:
    conn = _connector(sp)._connect()
    try:
        cur = conn.cursor()
        for statement in statements:
            try:
                cur.execute(statement)
            except IndexError:
                pass  # impyla 对 Spark 不返回结果集的语句偶尔在取结果元数据时报 IndexError；语句已执行
    finally:
        conn.close()


@pytest.fixture(scope="module")
def sp(metadb):
    settings = _settings()
    _execute(
        settings,
        f"DROP DATABASE IF EXISTS {DB} CASCADE",
        f"CREATE DATABASE {DB} COMMENT '联调库'",
        f"CREATE TABLE {DB}.shops (shop_id BIGINT COMMENT '门店 ID', city STRING COMMENT '城市') "
        "USING parquet COMMENT '门店'",
        f"CREATE TABLE {DB}.sales (sale_id BIGINT COMMENT '销售单 ID', shop_id BIGINT, status STRING, "
        "amount DECIMAL(18,2) COMMENT '金额', sale_date STRING) "
        "USING parquet PARTITIONED BY (sale_date) COMMENT '销售'",
        f"INSERT INTO {DB}.shops VALUES (1, '杭州'), (2, '上海')",
        f"INSERT INTO {DB}.sales VALUES (10, 1, 'paid', 12.50, '2026-11-01'), (11, 2, 'void', 3.00, '2026-11-01')",
        f"INSERT INTO {DB}.sales VALUES (12, 2, 'paid', 7.25, '2026-11-02')",
        # 建视图时不写库名：Spark 保存原文，不写库名的表按建视图时的当前库（这里是 avelis_it）解析
        f"USE {DB}",
        "CREATE VIEW city_daily_sales COMMENT '城市日销售' AS "
        "SELECT s.sale_date, h.city, SUM(s.amount) AS sales_amount "
        "FROM sales s JOIN shops h ON h.shop_id = s.shop_id WHERE s.status <> 'void' GROUP BY s.sale_date, h.city",
    )
    with session_scope() as s:
        ds = Datasource(
            name="Spark 联调", kind="spark", host=settings["host"], port=settings["port"], options={"databases": [DB]}
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


def test_collect_tables_views_and_lineage(sp):
    run_id = run_collection(sp["ds"], trigger="manual")
    with session_scope() as s:
        run = s.get(CollectionRun, run_id)
        assert run.status == "succeeded" and run.stats["warnings"] == []
        identity = s.get(Datasource, sp["ds"]).server_identity
    assert identity["version"].startswith("4.")
    assets = _assets(sp["ds"])
    assert set(assets) == {
        f"spark_catalog.{DB}.shops",
        f"spark_catalog.{DB}.sales",
        f"spark_catalog.{DB}.city_daily_sales",
    }
    sales, sales_v = assets[f"spark_catalog.{DB}.sales"]
    assert sales.kind == "table" and sales_v.comment == "销售"
    assert sales_v.properties["partition_columns"] == ["sale_date"] and sales_v.properties["provider"] == "parquet"
    assert [c[0] for c in _columns(sales_v.id)] == ["sale_id", "shop_id", "status", "amount", "sale_date"]
    assert _columns(sales_v.id)[3] == ("amount", "decimal(18,2)", True, "金额")
    view, view_v = assets[f"spark_catalog.{DB}.city_daily_sales"]
    assert view.kind == "view" and view_v.comment == "城市日销售"
    assert view_v.properties["view_context"]["db"] == DB
    with session_scope() as s:
        definition = s.scalar(select(Definition).where(Definition.asset_version_id == view_v.id))
        # Spark 保存原文、不补库名；适配器按建视图时的当前库补全
        assert definition.dialect == "spark" and f"FROM {DB}.sales AS s" in definition.raw_sql
        parsed = s.scalar(select(ParseResult).where(ParseResult.definition_id == definition.id))
        assert parsed.status == "ok", parsed.reason
        edges = {
            (e.target_column, e.source_ref, e.source_column, e.relation)
            for e in s.scalars(
                select(LineageEdge).where(LineageEdge.target_asset_id == view.id, LineageEdge.status == "active")
            )
        }
    assert {
        ("sales_amount", f"spark_catalog.{DB}.sales", "amount", "value_aggregated"),
        ("city", f"spark_catalog.{DB}.shops", "city", "value_direct"),
    } <= edges
    assert any(e[2] == "status" and e[3] == "filter" for e in edges)


def test_recollect_detects_new_column(sp):
    _execute(sp, f"ALTER TABLE {DB}.shops ADD COLUMNS (region STRING COMMENT '区域')")
    run_id = run_collection(sp["ds"], trigger="manual")
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


def test_query_with_row_cap_and_timezone(sp):
    result = _connector(sp).execute_select(
        f"SELECT city, sales_amount FROM {DB}.city_daily_sales ORDER BY sale_date, city",
        timeout_seconds=120,
        mem_limit_bytes=0,
        max_rows=1,
        timezone="Asia/Shanghai",
    )
    assert [c.split(".")[-1] for c in result.columns] == ["city", "sales_amount"] and result.truncated
    assert [(c, float(v)) for c, v in result.rows] == [("杭州", 12.5)]
    tz = _connector(sp).execute_select(
        "SELECT current_timezone()", timeout_seconds=60, mem_limit_bytes=0, max_rows=1, timezone="Asia/Shanghai"
    )
    assert tz.rows == [["Asia/Shanghai"]]


def test_timeout_cancels_and_errors_are_classified(sp):
    started = time.monotonic()
    with pytest.raises(QueryError) as caught:
        _connector(sp).execute_select(
            "SELECT count(*) FROM range(100000000) a CROSS JOIN range(100000000) b",
            timeout_seconds=3,
            mem_limit_bytes=0,
            max_rows=10,
        )
    assert caught.value.code == "timeout" and time.monotonic() - started < 20
    with pytest.raises(QueryError) as caught:
        _connector(sp).execute_select(f"SELECT * FROM {DB}.nope", timeout_seconds=60, mem_limit_bytes=0, max_rows=10)
    assert caught.value.code == "invalid_object" and caught.value.db_errno == "TABLE_OR_VIEW_NOT_FOUND"


def test_live_check_matches_collection_and_freshness(sp):
    from avelis.lineage.sql import canonical_hash_of

    run_collection(sp["ds"], trigger="manual")
    assets = _assets(sp["ds"])
    described = _connector(sp).describe_objects([(DB, "sales"), (DB, "city_daily_sales"), (DB, "missing")])
    for name in ("sales", "city_daily_sales"):
        _, version = assets[f"spark_catalog.{DB}.{name}"]
        assert described[(DB, name)]["columns"] == [[c[0], c[1]] for c in _columns(version.id)]
    _, view_v = assets[f"spark_catalog.{DB}.city_daily_sales"]
    with session_scope() as s:
        stored = s.scalar(select(Definition.canonical_hash).where(Definition.asset_version_id == view_v.id))
    assert canonical_hash_of(described[(DB, "city_daily_sales")]["definition"], get_engine("spark").sql)[1] == stored
    assert described[(DB, "missing")] == {"columns": [], "definition": None, "physical_id": None}
    fresh = _connector(sp).freshness([(DB, "sales"), (DB, "shops")])
    at = datetime.fromisoformat(fresh[f"{DB}.sales"])
    assert abs(datetime.now(timezone.utc) - at) < timedelta(hours=1), fresh
    # Spark 的非分区数据源表（USING parquet）没有 transient_lastDdlTime：不报告新鲜度
    assert fresh[f"{DB}.shops"] is None


def test_explore_sql_with_week_and_quarter_runs_on_spark(sp):
    from tests.unit.test_analysis_explore import ok

    v = ok(
        "spark",
        "SELECT region, SUM(amount) OVER (PARTITION BY region) AS w, DATE_TRUNC('week', event_date) AS wk, "
        "DATE_TRUNC('quarter', event_date) AS q, event_date + INTERVAL '1' QUARTER AS q1, "
        "event_date - INTERVAL '1' WEEK AS w1, CAST(event_date AS TIMESTAMP) AS ts FROM d1.facts",
    )
    sql = v.sql.replace(
        "`d1`.`facts`",
        f"(SELECT status AS region, amount, DATE '2026-11-01' AS event_date FROM {DB}.sales)",
    )
    result = _connector(sp).execute_explore(sql, timeout_seconds=120, max_rows=10, timezone="Asia/Shanghai")
    row = dict(zip([c.split(".")[-1] for c in result.columns], result.rows[0], strict=True))
    assert str(row["wk"]).startswith("2026-10-26") and str(row["q"]).startswith("2026-10-01")
    assert str(row["q1"]) == "2027-02-01" and str(row["w1"]) == "2026-10-25"


def test_renamed_columns_uppercase_names_and_recreated_context(sp):
    # CREATE VIEW v (列, …) 声明的列名、视图里大写的表名、在另一个当前库下按同样的文本重建视图
    _execute(
        sp,
        f"CREATE DATABASE IF NOT EXISTS {DB}_alt",
        f"CREATE TABLE IF NOT EXISTS {DB}_alt.shops (shop_id BIGINT, city STRING) USING parquet",
        f"USE {DB}",
        "CREATE OR REPLACE VIEW renamed (id_renamed, city_renamed) AS SELECT SHOP_ID, CITY FROM SHOPS",
        # 声明的列名 + 重名的输出列 + 分组：采集按位置对应列名，粒度照常推导
        "CREATE OR REPLACE VIEW paired (left_id, right_id, total) AS "
        "SELECT s.shop_id, h.shop_id, SUM(s.amount) AS amount_sum "
        "FROM sales s JOIN shops h ON h.shop_id = s.shop_id GROUP BY s.shop_id, h.shop_id",
    )
    run_collection(sp["ds"], trigger="manual")
    view, view_v = _assets(sp["ds"])[f"spark_catalog.{DB}.renamed"]
    with session_scope() as s:
        definition = s.scalar(select(Definition).where(Definition.asset_version_id == view_v.id))
        parsed = s.scalar(select(ParseResult).where(ParseResult.definition_id == definition.id))
        assert parsed.status == "ok", parsed.reason
        edges = {
            (e.target_column, e.source_ref, e.source_column, e.source_asset_id is not None)
            for e in s.scalars(
                select(LineageEdge).where(LineageEdge.target_asset_id == view.id, LineageEdge.status == "active")
            )
        }
    assert ("id_renamed", f"spark_catalog.{DB}.shops", "shop_id", True) in edges
    assert ("city_renamed", f"spark_catalog.{DB}.shops", "city", True) in edges
    paired, paired_v = _assets(sp["ds"])[f"spark_catalog.{DB}.paired"]
    with session_scope() as s:
        paired_edges = {
            (e.target_column, e.source_ref, e.source_column)
            for e in s.scalars(
                select(LineageEdge).where(
                    LineageEdge.target_asset_id == paired.id,
                    LineageEdge.status == "active",
                    LineageEdge.target_column.is_not(None),
                )
            )
        }
        grain = s.scalar(select(Grain).where(Grain.asset_version_id == paired_v.id))
    assert ("left_id", f"spark_catalog.{DB}.sales", "shop_id") in paired_edges
    assert ("right_id", f"spark_catalog.{DB}.shops", "shop_id") in paired_edges
    assert ("total", f"spark_catalog.{DB}.sales", "amount") in paired_edges
    assert grain is not None and grain.columns == ["left_id", "right_id"]
    # 上游表变化 → 视图重新解析、视图自己的版本不变：旧的解析粒度要被替换，不能并存
    _execute(sp, f"ALTER TABLE {DB}.sales ADD COLUMNS (note STRING)")
    run_collection(sp["ds"], trigger="manual")
    with session_scope() as s:
        active = s.scalars(
            select(Grain).where(Grain.asset_id == paired.id, Grain.origin == "parsed", Grain.superseded_at.is_(None))
        ).all()
        assert [g.columns for g in active] == [["left_id", "right_id"]]
    before = definition.canonical_hash
    _execute(
        sp,
        f"USE {DB}_alt",
        f"CREATE OR REPLACE VIEW {DB}.renamed (id_renamed, city_renamed) AS SELECT SHOP_ID, CITY FROM SHOPS",
    )
    run_collection(sp["ds"], trigger="manual")
    _, view_v2 = _assets(sp["ds"])[f"spark_catalog.{DB}.renamed"]
    with session_scope() as s:
        after = s.scalar(select(Definition.canonical_hash).where(Definition.asset_version_id == view_v2.id))
    assert view_v2.id != view_v.id and after != before  # 文本相同、解析上下文不同：产生新版本
    _execute(
        sp,
        f"DROP VIEW IF EXISTS {DB}.renamed",
        f"DROP VIEW IF EXISTS {DB}.paired",
        f"DROP DATABASE IF EXISTS {DB}_alt CASCADE",
    )
