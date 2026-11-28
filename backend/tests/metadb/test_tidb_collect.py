"""TiDB 适配器真实联调：真实 TiDB + 开发者自己的元数据库，走采集服务、视图血缘、问数执行与执行前核对。

环境：deploy/demo/tidb/compose.yaml（独立 Compose 项目 avelis-tidb，不碰共享演示环境）。运行：
  AVELIS_DATABASE_URL=postgresql+psycopg://avelis:…@127.0.0.1:26432/avelis_tidb_test \\
  AVELIS_TIDB_TEST_PORT=24000 uv run pytest -m db tests/metadb/test_tidb_collect.py
未设置 AVELIS_TIDB_TEST_PORT 时整组跳过。测试用 root（无口令）重建 avelis_it 库，
并创建采集账号与只读问数账号，口令每次随机生成、只在本进程内使用。
"""

from __future__ import annotations

import os
import secrets
import time

import pymysql
import pytest
from sqlalchemy import select

from avelis.collect.service import run_collection
from avelis.connectors.base import QueryError
from avelis.db import session_scope
from avelis.engines import collect_connector, query_connector
from avelis.models import (
    Asset,
    AssetVersion,
    ChangeEvent,
    ChangeSet,
    CollectionRun,
    ColumnVersion,
    Datasource,
    DatasourceCredential,
    Definition,
    LineageEdge,
    ParseResult,
)
from avelis.security import encrypt_secret

pytestmark = pytest.mark.db

DB = "avelis_it"
AUX = "avelis_it_aux"  # 不在采集范围内，只给超时用例放数字表
COLLECT_USER, QUERY_USER = "avelis_it_collect", "avelis_it_query"


def _settings():
    port = os.environ.get("AVELIS_TIDB_TEST_PORT")
    if not port:
        pytest.skip("未设置 AVELIS_TIDB_TEST_PORT：先启动 deploy/demo/tidb/compose.yaml")
    return dict(host=os.environ.get("AVELIS_TIDB_TEST_HOST", "127.0.0.1"), port=int(port))


def _root(tidb: dict, *statements: str) -> None:
    conn = pymysql.connect(host=tidb["host"], port=tidb["port"], user="root", password="", autocommit=True)
    try:
        cur = conn.cursor()
        for statement in statements:
            cur.execute(statement)
    finally:
        conn.close()


@pytest.fixture(scope="module")
def tidb(metadb):
    settings = _settings()
    collect_pw, query_pw = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    _root(
        settings,
        f"DROP DATABASE IF EXISTS {DB}",
        f"CREATE DATABASE {DB}",
        f"CREATE TABLE {DB}.shops (shop_id BIGINT PRIMARY KEY, city VARCHAR(32) COMMENT '城市') COMMENT='门店'",
        f"CREATE TABLE {DB}.sales (sale_id BIGINT NOT NULL PRIMARY KEY COMMENT '销售单 ID', shop_id BIGINT, "
        "status VARCHAR(16), amount DECIMAL(18,2) COMMENT '金额', sale_date DATE, "
        "UNIQUE KEY uk_shop_sale (shop_id, sale_id)) COMMENT='销售'",
        f"INSERT INTO {DB}.shops VALUES (1, '杭州'), (2, '上海')",
        f"INSERT INTO {DB}.sales VALUES (10, 1, 'paid', 12.50, '2026-11-01'), (11, 2, 'void', 3.00, '2026-11-01'), "
        "(12, 2, 'paid', 7.25, '2026-11-02')",
        # 建视图时不写库名：TiDB 保存的定义会补全成 `avelis_it`.`sales`
        f"USE {DB}",
        "CREATE OR REPLACE VIEW city_daily_sales AS SELECT s.sale_date, h.city, SUM(s.amount) AS sales_amount "
        "FROM sales s JOIN shops h ON h.shop_id = s.shop_id WHERE s.status <> 'void' GROUP BY s.sale_date, h.city",
        f"DROP USER IF EXISTS '{COLLECT_USER}'@'%'",
        f"DROP USER IF EXISTS '{QUERY_USER}'@'%'",
        f"CREATE USER '{COLLECT_USER}'@'%' IDENTIFIED BY '{collect_pw}'",
        f"CREATE USER '{QUERY_USER}'@'%' IDENTIFIED BY '{query_pw}'",
        f"GRANT SELECT, SHOW VIEW ON {DB}.* TO '{COLLECT_USER}'@'%'",
        f"GRANT SELECT, SHOW VIEW ON {DB}.* TO '{QUERY_USER}'@'%'",
    )
    with session_scope() as s:
        ds = Datasource(
            name="TiDB 联调", kind="tidb", host=settings["host"], port=settings["port"], options={"databases": [DB]}
        )
        s.add(ds)
        s.flush()
        for purpose, user, pw in (("collect", COLLECT_USER, collect_pw), ("query", QUERY_USER, query_pw)):
            s.add(
                DatasourceCredential(datasource_id=ds.id, purpose=purpose, username=user, secret_enc=encrypt_secret(pw))
            )
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


def _events(run_id: str) -> set[tuple]:
    with session_scope() as s:
        return {
            (e.kind, e.column_name, e.attribution_status)
            for e in s.scalars(
                select(ChangeEvent)
                .join(ChangeSet, ChangeSet.id == ChangeEvent.change_set_id)
                .where(ChangeSet.run_id == run_id)
            )
        }


def _query(tidb: dict):
    with session_scope() as s:
        return query_connector(s, s.get(Datasource, tidb["ds"]))


def test_collect_tables_views_and_lineage(tidb):
    run_id = run_collection(tidb["ds"], trigger="manual")
    with session_scope() as s:
        run = s.get(CollectionRun, run_id)
        assert run.status == "succeeded" and run.stats["warnings"] == []
        identity = s.get(Datasource, tidb["ds"]).server_identity
    assert identity["instance_id"] is None and "TiDB" in identity["version"]
    assets = _assets(tidb["ds"])
    assert set(assets) == {f"def.{DB}.shops", f"def.{DB}.sales", f"def.{DB}.city_daily_sales"}
    sales, sales_v = assets[f"def.{DB}.sales"]
    assert sales.kind == "table" and sales.physical_id is None and sales_v.comment == "销售"
    with session_scope() as s:
        columns = [
            (c.name, c.data_type, c.nullable, c.comment)
            for c in s.scalars(
                select(ColumnVersion)
                .where(ColumnVersion.asset_version_id == sales_v.id)
                .order_by(ColumnVersion.ordinal)
            )
        ]
    assert columns == [
        ("sale_id", "bigint", False, "销售单 ID"),
        ("shop_id", "bigint", True, None),
        ("status", "varchar(16)", True, None),
        ("amount", "decimal(18,2)", True, "金额"),
        ("sale_date", "date", True, None),
    ]
    view, view_v = assets[f"def.{DB}.city_daily_sales"]
    assert view.kind == "view"
    with session_scope() as s:
        definition = s.scalar(select(Definition).where(Definition.asset_version_id == view_v.id))
        assert definition.dialect == "mysql" and f"`{DB}`.`sales`" in definition.raw_sql
        parsed = s.scalar(select(ParseResult).where(ParseResult.definition_id == definition.id))
        assert parsed.status == "ok", parsed.reason
        edges = {
            (e.target_column, e.source_ref, e.source_column, e.relation)
            for e in s.scalars(
                select(LineageEdge).where(LineageEdge.target_asset_id == view.id, LineageEdge.status == "active")
            )
        }
    assert {
        ("sales_amount", f"def.{DB}.sales", "amount", "value_aggregated"),
        ("city", f"def.{DB}.shops", "city", "value_direct"),
        ("sale_date", f"def.{DB}.sales", "sale_date", "value_direct"),
    } <= edges
    assert any(e[2] == "status" and e[3] == "filter" for e in edges)
    assert any(e[2] == "shop_id" and e[3] == "join_key" for e in edges)


def test_truncate_keeps_asset_identity_and_ddl_is_unsupported(tidb):
    before = _assets(tidb["ds"])
    _root(tidb, f"TRUNCATE TABLE {DB}.shops", f"INSERT INTO {DB}.shops VALUES (1, '杭州'), (2, '上海')")
    _root(tidb, f"ALTER TABLE {DB}.sales ADD COLUMN channel VARCHAR(16) COMMENT '渠道'")
    run_id = run_collection(tidb["ds"], trigger="manual")
    after = _assets(tidb["ds"])
    # TRUNCATE 换了 TIDB_TABLE_ID，但资产按名称对应：同一个资产，没有删除与新增
    assert after[f"def.{DB}.shops"][0].id == before[f"def.{DB}.shops"][0].id
    events = _events(run_id)
    assert ("column_added", "channel", "unsupported") in events
    assert not any(kind in ("added", "removed") for kind, _, _ in events)


def test_query_runs_read_only_with_limits(tidb):
    connector = _query(tidb)
    result = connector.execute_select(
        f"SELECT shop_id, SUM(amount) AS total FROM {DB}.sales WHERE status = 'paid' GROUP BY shop_id ORDER BY shop_id",
        timeout_seconds=10,
        mem_limit_bytes=512 * 1024**2,
        max_rows=1,
        timezone="Asia/Shanghai",
    )
    assert result.columns == ["shop_id", "total"]
    assert [[int(a), float(b)] for a, b in result.rows] == [[1, 12.5]] and result.truncated
    view_rows = connector.execute_select(
        f"SELECT city, sales_amount FROM {DB}.city_daily_sales ORDER BY sale_date, city",
        timeout_seconds=10,
        mem_limit_bytes=0,
        max_rows=10,
    ).rows
    assert [(city, float(amount)) for city, amount in view_rows] == [("杭州", 12.5), ("上海", 7.25)]
    # 只读事务由引擎拒绝写入（即使账号有写权限也一样）；这里只读账号本身也没有写权限
    with pytest.raises(QueryError) as caught:
        connector.execute_select(
            f"UPDATE {DB}.sales SET amount = 0", timeout_seconds=10, mem_limit_bytes=0, max_rows=10
        )
    assert caught.value.code in ("permission", "other")


def test_stale_read_transaction_rejects_writes_even_for_root(tidb):
    from avelis.connectors.tidb import TiDBConnector

    root = TiDBConnector(tidb["host"], tidb["port"], "root", "", {"databases": [DB]})
    with pytest.raises(QueryError):
        root.execute_select(f"DELETE FROM {DB}.shops", timeout_seconds=10, mem_limit_bytes=0, max_rows=10)
    rows = root.execute_select(
        f"SELECT COUNT(*) FROM {DB}.shops", timeout_seconds=10, mem_limit_bytes=0, max_rows=10
    ).rows
    assert rows == [[2]]


def test_memory_quota_and_timeout_are_classified(tidb):
    connector = _query(tidb)
    with pytest.raises(QueryError) as caught:
        connector.execute_select(
            f"SELECT a.sale_id, b.sale_id, c.sale_id FROM {DB}.sales a, {DB}.sales b, {DB}.sales c "
            "ORDER BY a.status, b.status, c.status",
            timeout_seconds=10,
            mem_limit_bytes=1,
            max_rows=10,
        )
    assert caught.value.code == "other" and caught.value.db_errno == 8175
    # SLEEP() 被 MAX_EXECUTION_TIME 打断时返回 1 而不报错：用采集范围外的数字表做三方笛卡尔积来验证超时。
    # 历史读读取约 1 秒前的数据，建表后等 2 秒再查。
    _root(
        tidb,
        f"DROP DATABASE IF EXISTS {AUX}",
        f"CREATE DATABASE {AUX}",
        f"CREATE TABLE {AUX}.nums (n INT PRIMARY KEY)",
        "SET SESSION cte_max_recursion_depth = 5000",
        f"INSERT INTO {AUX}.nums WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3000) "
        "SELECT n FROM r",
        f"GRANT SELECT ON {AUX}.* TO '{QUERY_USER}'@'%'",
    )
    time.sleep(2)
    started = time.monotonic()
    with pytest.raises(QueryError) as caught:
        connector.execute_select(
            f"SELECT COUNT(*) FROM {AUX}.nums a, {AUX}.nums b, {AUX}.nums c",
            timeout_seconds=1,
            mem_limit_bytes=0,
            max_rows=10,
        )
    assert caught.value.code == "timeout" and caught.value.db_errno == 3024
    assert time.monotonic() - started < 10


def test_live_check_reads_current_columns_and_definition(tidb):
    with session_scope() as s:
        connector = collect_connector(s, s.get(Datasource, tidb["ds"]))
    described = connector.describe_objects([(DB, "sales"), (DB, "city_daily_sales"), (DB, "missing")])
    assert described[(DB, "sales")]["columns"][0] == ["sale_id", "bigint"]
    assert described[(DB, "city_daily_sales")]["definition"].startswith("SELECT")
    assert described[(DB, "missing")] == {"columns": [], "definition": None, "physical_id": None}
