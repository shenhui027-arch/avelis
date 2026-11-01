"""Hive 适配器真实联调：真实 HiveServer2 + 开发者自己的元数据库，走采集服务、视图血缘解析与变更识别。

环境：deploy/demo/hive/compose.yaml（独立 Compose 项目 avelis-hive，不碰共享演示环境）。运行：
  AVELIS_DATABASE_URL=postgresql+psycopg://avelis:…@127.0.0.1:25432/avelis_hive_test \\
  AVELIS_HIVE_TEST_PORT=20000 [AVELIS_HIVE_TEST_AUTH=LDAP AVELIS_HIVE_TEST_USER=admin AVELIS_HIVE_TEST_PASSWORD=…] \\
  uv run pytest -m db tests/metadb/test_hive_collect.py
未设置 AVELIS_HIVE_TEST_PORT 时连真实 Hive 的用例跳过；“库不可见”用例用脚本化的游标，只需要元数据库。
测试会重建 Hive 中的 avelis_it 库。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select

from avelis.collect.service import CollectionFailed, run_collection
from avelis.connectors.hive import HiveConnector
from avelis.db import session_scope
from avelis.models import (
    Asset,
    AssetColumn,
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
SEED = [
    f"DROP DATABASE IF EXISTS {DB} CASCADE",
    f"CREATE DATABASE {DB} COMMENT '联调库'",
    f"CREATE TABLE {DB}.shops (shop_id BIGINT COMMENT '门店 ID', city STRING COMMENT '城市', "
    "tags ARRAY<STRING>, addr STRUCT<street:STRING,zip:INT> COMMENT '地址', u UNIONTYPE<INT,STRING>) "
    "COMMENT '门店'",
    f"CREATE TABLE {DB}.sales (sale_id BIGINT NOT NULL COMMENT '销售单 ID', shop_id BIGINT, status VARCHAR(16), "
    "amount DECIMAL(18,2) COMMENT '金额') COMMENT '销售' PARTITIONED BY (sale_date DATE COMMENT '销售日期') "
    "STORED AS ORC",
    # 建视图时不写库名：Hive 保存的展开文本会补全成 avelis_it.sales
    f"USE {DB}",
    """CREATE VIEW city_daily_sales COMMENT '城市日销售' AS
SELECT s.sale_date, h.city,
       SUM(s.amount) AS sales_amount
FROM sales s
JOIN shops h ON h.shop_id = s.shop_id
WHERE s.status <> 'void'
GROUP BY s.sale_date, h.city""",
]


def _settings():
    port = os.environ.get("AVELIS_HIVE_TEST_PORT")
    if not port:
        pytest.skip("未设置 AVELIS_HIVE_TEST_PORT：先启动 deploy/demo/hive/compose.yaml")
    return dict(
        host=os.environ.get("AVELIS_HIVE_TEST_HOST", "127.0.0.1"),
        port=int(port),
        auth=os.environ.get("AVELIS_HIVE_TEST_AUTH", "NONE"),
        user=os.environ.get("AVELIS_HIVE_TEST_USER", ""),
        password=os.environ.get("AVELIS_HIVE_TEST_PASSWORD", ""),
    )


def _execute(hive: dict, *statements: str) -> None:
    conn = HiveConnector(hive["host"], hive["port"], hive["user"], hive["password"], {"auth": hive["auth"]})._connect()
    try:
        cur = conn.cursor()
        for statement in statements:
            cur.execute(statement)
    finally:
        conn.close()


@pytest.fixture(scope="module")
def hive(metadb):
    settings = _settings()
    _execute(settings, *SEED)
    with session_scope() as s:
        ds = Datasource(
            name=f"Hive 联调（{settings['auth']}）",
            kind="hive",
            host=settings["host"],
            port=settings["port"],
            options={"databases": [DB], "auth": settings["auth"]},
        )
        s.add(ds)
        s.flush()
        if settings["user"]:
            s.add(
                DatasourceCredential(
                    datasource_id=ds.id,
                    purpose="collect",
                    username=settings["user"],
                    secret_enc=encrypt_secret(settings["password"]),
                )
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


def _events(run_id: str) -> set[tuple]:
    with session_scope() as s:
        return {
            (e.kind, e.column_name)
            for e in s.scalars(
                select(ChangeEvent)
                .join(ChangeSet, ChangeSet.id == ChangeEvent.change_set_id)
                .where(ChangeSet.run_id == run_id)
            )
        }


def test_collect_tables_views_and_lineage(hive):
    run_id = run_collection(hive["ds"], trigger="manual")
    with session_scope() as s:
        run = s.get(CollectionRun, run_id)
        assert run.status == "succeeded" and run.stats["warnings"] == []
        identity = s.get(Datasource, hive["ds"]).server_identity
    assert identity["version"].startswith("4.") and identity["auth"] == hive["auth"]
    assets = _assets(hive["ds"])
    assert set(assets) == {f"hive.{DB}.shops", f"hive.{DB}.sales", f"hive.{DB}.city_daily_sales"}
    sales, sales_v = assets[f"hive.{DB}.sales"]
    assert sales.kind == "table" and sales.physical_id is None and sales_v.comment == "销售"
    assert sales_v.properties["partition_columns"] == ["sale_date"]
    # 分区列在普通列之后；NOT NULL 约束的列不可空；类型保留 Hive 原样
    assert _columns(sales_v.id) == [
        ("sale_id", "bigint", False, "销售单 ID"),
        ("shop_id", "bigint", True, None),
        ("status", "varchar(16)", True, None),
        ("amount", "decimal(18,2)", True, "金额"),
        ("sale_date", "date", True, "销售日期"),
    ]
    _, shops_v = assets[f"hive.{DB}.shops"]
    assert [c[1] for c in _columns(shops_v.id)][2:] == [
        "array<string>",
        "struct<street:string,zip:int>",
        "uniontype<int,string>",
    ]
    view, view_v = assets[f"hive.{DB}.city_daily_sales"]
    assert view.kind == "view" and view_v.comment == "城市日销售"
    with session_scope() as s:
        definition = s.scalar(select(Definition).where(Definition.asset_version_id == view_v.id))
        assert definition.dialect == "hive" and f"`{DB}`.`sales`" in definition.raw_sql  # 展开文本已补全库名
        parsed = s.scalar(select(ParseResult).where(ParseResult.definition_id == definition.id))
        assert parsed.status == "ok", parsed.reason
        edges = {
            (e.target_column, e.source_ref, e.source_column, e.relation)
            for e in s.scalars(
                select(LineageEdge).where(LineageEdge.target_asset_id == view.id, LineageEdge.status == "active")
            )
        }
    assert {
        ("sales_amount", f"hive.{DB}.sales", "amount", "value_aggregated"),
        ("city", f"hive.{DB}.shops", "city", "value_direct"),
        ("sale_date", f"hive.{DB}.sales", "sale_date", "value_direct"),
    } <= edges
    assert any(e[2] == "status" and e[3] == "filter" for e in edges)
    assert any(e[2] == "shop_id" and e[3] == "join_key" for e in edges)


def test_recollect_detects_column_comment_and_definition_changes(hive):
    _execute(
        hive,
        f"ALTER TABLE {DB}.sales ADD COLUMNS (channel STRING COMMENT '渠道')",
        f"ALTER TABLE {DB}.shops CHANGE COLUMN city city STRING COMMENT '所在城市'",
        f"ALTER VIEW {DB}.city_daily_sales AS SELECT s.sale_date, h.city, SUM(s.amount) AS sales_amount "
        f"FROM {DB}.sales s JOIN {DB}.shops h ON h.shop_id = s.shop_id GROUP BY s.sale_date, h.city",
    )
    run_id = run_collection(hive["ds"], trigger="manual")
    events = _events(run_id)
    assert {("column_added", "channel"), ("column_comment_changed", "city"), ("definition_changed", None)} <= events
    _, sales_v = _assets(hive["ds"])[f"hive.{DB}.sales"]
    # 新增的普通列排在分区列之前（与 Hive 的列序一致）
    assert [c[0] for c in _columns(sales_v.id)] == ["sale_id", "shop_id", "status", "amount", "channel", "sale_date"]
    # 改定义后重新解析：过滤条件去掉了，status 不再是 filter 影响
    view, _ = _assets(hive["ds"])[f"hive.{DB}.city_daily_sales"]
    with session_scope() as s:
        relations = {
            (e.source_column, e.relation)
            for e in s.scalars(
                select(LineageEdge).where(LineageEdge.target_asset_id == view.id, LineageEdge.status == "active")
            )
        }
    assert ("status", "filter") not in relations and ("amount", "value_aggregated") in relations


def test_rename_without_physical_id_is_drop_and_add(hive):
    # HiveServer2 不暴露表 ID：改名按删除 + 新增记录，不冒充有物理身份的改名
    _execute(hive, f"ALTER TABLE {DB}.shops RENAME TO {DB}.stores")
    events = _events(run_collection(hive["ds"], trigger="manual"))
    assert ("deleted", None) in events and ("added", None) in events and ("renamed", None) not in events
    assert f"hive.{DB}.stores" in _assets(hive["ds"])


def test_wrong_ldap_password_fails_collection(hive):
    if hive["auth"] != "LDAP":
        pytest.skip("只在 LDAP 认证下检查")
    bad = HiveConnector(hive["host"], hive["port"], hive["user"], "wrong-password", {"auth": "LDAP", "databases": [DB]})
    with pytest.raises(Exception):  # noqa: B017 - 驱动在握手阶段报认证失败
        bad.server_identity()
    with session_scope() as s:
        cred = s.scalar(select(DatasourceCredential).where(DatasourceCredential.datasource_id == hive["ds"]))
        cred.secret_enc = encrypt_secret("wrong-password")
    with pytest.raises(CollectionFailed):
        run_collection(hive["ds"], trigger="manual")


class _ScriptedCursor:
    """不连 Hive：SHOW DATABASES 的结果可切换，其余返回一张固定的表（模拟授权收紧后库在列表中被过滤）。"""

    def __init__(self):
        self.visible = True
        self._rows: list[tuple] = []

    def execute(self, sql: str) -> None:
        if sql == "SELECT version()":
            self._rows = [("4.1.0",)]
        elif sql == "SHOW DATABASES":
            self._rows = [("default",), *([(DB,)] if self.visible else [])]
        elif sql.startswith("SHOW TABLES IN"):
            self._rows = [("orders",)]
        elif sql.startswith("DESCRIBE FORMATTED"):
            self._rows = [
                ("order_id", "bigint", "订单 ID"),
                ("amount", "decimal(18,2)", ""),
                ("", None, None),
                ("# Detailed Table Information", None, None),
                ("Table Type:         ", "EXTERNAL_TABLE      ", None),
            ]
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def test_invisible_database_fails_and_keeps_catalog(metadb, monkeypatch):
    from types import SimpleNamespace

    from avelis.collect import service

    cursor = _ScriptedCursor()
    connection = SimpleNamespace(cursor=lambda: cursor, close=lambda: None)

    def connector(session, ds):
        conn = HiveConnector(ds.host, ds.port, "", "", ds.options)
        conn._connect = lambda timeout=60: connection
        return conn

    monkeypatch.setattr(service, "collect_connector", connector)
    with session_scope() as s:
        ds = Datasource(name="Hive 授权收紧", kind="hive", host="hs2", port=10000, options={"databases": [DB]})
        s.add(ds)
        s.flush()
        ds_id = ds.id
    first = run_collection(ds_id, trigger="manual")
    before = _assets(ds_id)
    assert set(before) == {f"hive.{DB}.orders"}

    cursor.visible = False
    with pytest.raises(CollectionFailed, match=f"配置的库 {DB}"):
        run_collection(ds_id, trigger="manual")
    with session_scope() as s:
        failed = s.scalars(
            select(CollectionRun).where(CollectionRun.datasource_id == ds_id).order_by(CollectionRun.started_at.desc())
        ).first()
        assert failed.status == "failed" and DB in failed.error
        asset = s.scalar(select(Asset).where(Asset.datasource_id == ds_id))
        assert asset.status == "active" and asset.deleted_at is None
        assert asset.current_version_id == before[f"hive.{DB}.orders"][1].id
        assert s.get(AssetVersion, asset.current_version_id).superseded_at is None
        columns = s.scalars(select(AssetColumn).where(AssetColumn.asset_id == asset.id)).all()
        assert {c.name for c in columns} == {"order_id", "amount"} and all(c.status == "active" for c in columns)
        kinds = {
            e.kind
            for e in s.scalars(
                select(ChangeEvent)
                .join(ChangeSet, ChangeSet.id == ChangeEvent.change_set_id)
                .where(ChangeSet.datasource_id == ds_id)
            )
        }
    assert "deleted" not in kinds and "column_removed" not in kinds
    # 库重新可见后照常采集，没有凭空的变更
    cursor.visible = True
    assert _events(run_collection(ds_id, trigger="manual")) == set() and first
