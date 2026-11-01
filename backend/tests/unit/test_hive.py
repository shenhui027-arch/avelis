"""Hive 适配器：DESCRIBE FORMATTED 解析、类型与分区列、视图定义提取与列级血缘、认证方式与凭据。

样例行取自 Hive 4.1.0 实机（deploy/demo/hive/compose.yaml）经 impyla 返回的原样结果（含右侧补齐的空白）。
"""

from __future__ import annotations

import pytest

from avelis.collect.service import _sqlglot_type
from avelis.connectors.hive import (
    CATALOG,
    HiveConnector,
    observed,
    parse_described,
    view_query,
)
from avelis.engines import collect_connector, get_engine
from avelis.engines.base import QUERY, VIEW_LINEAGE
from avelis.lineage.sql import parse_query
from avelis.models import Datasource

SALES = [
    ("sale_id", "bigint", "销售单 ID"),
    ("shop_id", "bigint", ""),
    ("status", "varchar(16)", ""),
    ("amount", "decimal(18,2)", "金额"),
    ("", None, None),
    ("# Partition Information", None, None),
    ("# col_name", "data_type", "comment"),
    ("sale_date", "date", "销售日期"),
    ("", None, None),
    ("# Detailed Table Information", None, None),
    ("Database:           ", "store               ", None),
    ("Owner:              ", "hive                ", None),
    ("CreateTime:         ", "Sun Nov 01 03:03:25 UTC 2026", None),
    ("Location:           ", "file:/opt/hive/data/warehouse/store.db/sales", None),
    ("Table Type:         ", "EXTERNAL_TABLE      ", None),
    ("Table Parameters:", None, None),
    ("", "COLUMN_STATS_ACCURATE", '{\\"BASIC_STATS\\":\\"true\\"}'),
    ("", "EXTERNAL            ", "TRUE                "),
    ("", "comment             ", "销售                  "),
    ("", "numRows             ", "0                   "),
    ("", None, None),
    ("# Storage Information", None, None),
    ("SerDe Library:      ", "org.apache.hadoop.hive.ql.io.orc.OrcSerde", None),
    ("InputFormat:        ", "org.apache.hadoop.hive.ql.io.orc.OrcInputFormat", None),
    ("Storage Desc Params:", None, None),
    ("", "serialization.format", "1                   "),
]

SHOPS = [
    ("shop_id", "bigint", "门店 ID"),
    ("city", "string", "城市"),
    ("tags", "array<string>", ""),
    ("attrs", "map<string,int>", ""),
    ("addr", "struct<street:string,zip:int>", "地址"),
    ("", None, None),
    ("# Detailed Table Information", None, None),
    ("Table Type:         ", "MANAGED_TABLE       ", None),
    ("Table Parameters:", None, None),
    ("", "comment             ", "门店                  "),
]

# 带约束、多行注释与 uniontype、嵌套 decimal 的表
CONSTRAINED = [
    ("id", "int", "id 列"),
    ("u", "uniontype<int,string>", ""),
    ("n", "struct<a:decimal(10,2),b:int>", ""),
    ("", None, None),
    ("# Detailed Table Information", None, None),
    ("Table Type:         ", "EXTERNAL_TABLE      ", None),
    ("Table Parameters:", None, None),
    ("", "comment             ", "multi\\nline comment "),
    ("", None, None),
    ("# Constraints", None, None),
    ("", None, None),
    ("# Primary Key", None, None),
    ("Table:              ", "probe.cons          ", None),
    ("Constraint Name:    ", "pk_147833237_1793502256466_0", None),
    ("Column Name:        ", "id                  ", None),
    ("", None, None),
    ("# Not Null Constraints", None, None),
    ("Table:              ", "probe.cons          ", None),
    ("Constraint Name:    ", "nn_147831408_1793502256478_0", None),
    ("Column Name:        ", "id                  ", None),
    ("", None, None),
]

VIEW = [
    ("sale_date", "date", ""),
    ("city", "string", ""),
    ("sales_amount", "decimal(28,2)", ""),
    ("", None, None),
    ("# Detailed Table Information", None, None),
    ("Table Type:         ", "VIRTUAL_VIEW        ", None),
    ("Table Parameters:", None, None),
    ("", "comment             ", "城市日销售               "),
    ("", None, None),
    ("# View Information", None, None),
    ("Original Query:     ", "SELECT s.sale_date, h.city,", None),
    ("", "                    ", "       SUM(s.amount) AS sales_amount"),
    ("", "                    ", "FROM store.sales s  "),
    ("Expanded Query:     ", "SELECT `s`.`sale_date`, `h`.`city`,", None),
    ("", "                    ", "       SUM(`s`.`amount`) AS `sales_amount`"),
    ("", "                    ", "FROM `store`.`sales` `s`"),
]

MATERIALIZED = [
    ("a", "int", ""),
    ("c", "bigint", ""),
    ("", None, None),
    ("# Detailed Table Information", None, None),
    ("Table Type:         ", "MATERIALIZED_VIEW   ", None),
    ("Table Parameters:", None, None),
    ("", "comment             ", "物化                  "),
    ("", None, None),
    ("# Materialized View Information", None, None),
    ("Original Query:     ", "SELECT a, count(*) AS c FROM probe.acid GROUP BY a", None),
    ("Expanded Query:     ", "SELECT `acid`.`a`, count(*) AS `c` FROM `probe`.`acid` GROUP BY `acid`.`a`", None),
    ("Rewrite Enabled:    ", "Yes                 ", None),
    ("", None, None),
    ("# Materialized View Source table information", None, None),
    ("Table name", "Snapshot", None),
    ("probe.acid", ":0:9223372036854775807::", None),
]

# SHOW CREATE TABLE store.city_daily_sales 的输出（每行一条记录）
SHOW_CREATE_VIEW = "\n".join([
    "CREATE VIEW `store`.`city_daily_sales` AS SELECT `s`.`sale_date`, `h`.`city`,",
    "       SUM(`s`.`amount`) AS `sales_amount`",
    "FROM `store`.`sales` `s`",
    "JOIN `store`.`shops` `h` ON `h`.`shop_id` = `s`.`shop_id`",
    "WHERE `s`.`status` <> 'void'",
    "GROUP BY `s`.`sale_date`, `h`.`city`",
])


def test_partitioned_table_columns_comments_and_storage():
    d = parse_described(SALES)
    assert d.table_type == "EXTERNAL_TABLE" and d.comment == "销售"
    assert d.parameters["COLUMN_STATS_ACCURATE"] == '{"BASIC_STATS":"true"}'  # 还原 Java 转义
    # 第二个 “# col_name” 表头不是小节；Storage Desc Params 的参数不混入表参数
    assert "serialization.format" not in d.parameters
    a = observed("store", "sales", d, None)
    assert (a.catalog, a.kind, a.table_model, a.physical_id) == (CATALOG, "table", "EXTERNAL_TABLE", None)
    # 分区列排在普通列之后，序号连续；类型保留 Hive 原样
    assert [(c.name, c.data_type, c.ordinal, c.comment) for c in a.columns] == [
        ("sale_id", "bigint", 1, "销售单 ID"), ("shop_id", "bigint", 2, None), ("status", "varchar(16)", 3, None),
        ("amount", "decimal(18,2)", 4, "金额"), ("sale_date", "date", 5, "销售日期")]
    assert all(c.nullable for c in a.columns) and not a.key_columns and not a.unique_keys
    assert a.properties["partition_columns"] == ["sale_date"]
    assert a.properties["location"] == "file:/opt/hive/data/warehouse/store.db/sales"
    assert a.properties["input_format"].endswith("OrcInputFormat") and a.definition is None


def test_complex_types_constraints_and_escaped_comment():
    shops = observed("store", "shops", parse_described(SHOPS), None)
    assert shops.table_model == "MANAGED_TABLE" and shops.comment == "门店"
    assert [c.data_type for c in shops.columns][2:] == ["array<string>", "map<string,int>",
                                                        "struct<street:string,zip:int>"]
    cons = parse_described(CONSTRAINED)
    assert cons.comment == "multi\nline comment" and cons.primary_key == ["id"] and cons.not_null == {"id"}
    a = observed("probe", "cons", cons, None)
    # NOT NULL 约束强制执行 → 不可空；主键 DISABLE NOVALIDATE 不校验 → 只记录，不作粒度依据
    assert [(c.name, c.nullable) for c in a.columns] == [("id", False), ("u", True), ("n", True)]
    assert a.key_columns == [] and a.unique_keys == [] and a.properties["declared_primary_key"] == ["id"]


def test_hive_types_map_into_lineage_schema():
    # 采集到的 Hive 原样类型进入解析用的 schema（collect.service.build_schema）：逐层去掉参数，嵌套结构完整保留，
    # 并能被 SQLGlot（hive）解析成同样的类型；引用这些列的视图照常解析
    from sqlglot import exp

    assert _sqlglot_type("struct<a:decimal(10,2),b:int>") == "STRUCT<A:DECIMAL,B:INT>"
    assert _sqlglot_type("map<string,varchar(10)>") == "MAP<STRING,VARCHAR>"
    assert _sqlglot_type("array<decimal(10,2)>") == "ARRAY<DECIMAL>"
    assert _sqlglot_type("timestamp(6) with time zone") == "TIMESTAMP WITH TIME ZONE"
    assert _sqlglot_type("Array(Decimal(18, 2))") == "ARRAY" and _sqlglot_type("") == "VARCHAR"
    union = exp.DataType.build(_sqlglot_type("uniontype<int,string>"), dialect="hive")
    assert union.is_type(exp.DType.UNION) and union.sql("hive") == "UNIONTYPE<INT, STRING>"
    nested = exp.DataType.build(_sqlglot_type("struct<a:decimal(10,2),b:int>"), dialect="hive")
    assert nested.sql("hive") == "STRUCT<A: DECIMAL, B: INT>"
    hive = get_engine("hive").sql
    types = ["bigint", "decimal(18,2)", "varchar(16)", "date", "timestamp", "string", "binary", "boolean",
             "array<string>", "map<string,int>", "struct<street:string,zip:int>", "struct<a:decimal(10,2),b:int>",
             "array<decimal(10,2)>", "uniontype<int,string>"]
    schema = {"hive": {"probe": {"t": {f"c{i}": _sqlglot_type(t) for i, t in enumerate(types)}}}}
    columns = ", ".join(f"c{i}" for i in range(len(types)))
    for sql in (f"SELECT {columns} FROM probe.t", "SELECT * FROM probe.t"):
        parsed = parse_query(sql, dialect=hive, schema=schema)
        assert parsed.status == "ok", parsed.reason
        assert [c.name for c in parsed.columns] == [f"c{i}" for i in range(len(types))]
        assert all(c.sources[0].ref.table == "hive.probe.t" for c in parsed.columns)


def test_view_and_materialized_view_definitions():
    view = parse_described(VIEW)
    assert view.table_type == "VIRTUAL_VIEW" and view.comment == "城市日销售"
    # 展开文本多行：续行在第三列，右侧补齐的空白去掉
    assert view.expanded_query == ("SELECT `s`.`sale_date`, `h`.`city`,\n       SUM(`s`.`amount`) AS `sales_amount`\n"
                                   "FROM `store`.`sales` `s`")
    # 视图定义取 SHOW CREATE TABLE 中 AS 之后的原文（不经 SQLGlot 重新生成）
    query = view_query(SHOW_CREATE_VIEW)
    assert query.startswith("SELECT `s`.`sale_date`, `h`.`city`,\n       SUM(") and query.endswith("`h`.`city`")
    a = observed("store", "city_daily_sales", view, query)
    assert a.kind == "view" and a.definition == query
    mv = observed("probe", "mv", parse_described(MATERIALIZED), parse_described(MATERIALIZED).expanded_query)
    assert mv.kind == "materialized_view" and mv.comment == "物化"
    assert mv.definition == "SELECT `acid`.`a`, count(*) AS `c` FROM `probe`.`acid` GROUP BY `acid`.`a`"
    # 其他写法用 SQLGlot（hive）取出查询；空输出没有定义
    assert view_query("CREATE VIEW IF NOT EXISTS v COMMENT 'x' AS SELECT 1 AS a") == "SELECT 1 AS a"
    assert view_query("  ") is None


def test_view_lineage_with_hive_dialect():
    hive = get_engine("hive").dialect_for({"databases": ["store"]})
    assert hive.name == "hive" and hive.default_catalog == "hive" and not hive.catalog_in_sql
    tables = {"sales": SALES, "shops": SHOPS}
    schema = {"hive": {"store": {name: {c.name: _sqlglot_type(c.data_type) for c in
                                        observed("store", name, parse_described(rows), None).columns}
                                 for name, rows in tables.items()}}}
    parsed = parse_query(view_query(SHOW_CREATE_VIEW), dialect=hive, schema=schema)
    assert parsed.status == "ok", parsed.reason
    assert sorted(parsed.tables) == ["hive.store.sales", "hive.store.shops"]
    sources = {c.name: {(s.ref.table, s.ref.column, s.relation) for s in c.sources} for c in parsed.columns}
    assert sources == {
        "sale_date": {("hive.store.sales", "sale_date", "value_direct")},
        "city": {("hive.store.shops", "city", "value_direct")},
        "sales_amount": {("hive.store.sales", "amount", "value_aggregated")},
    }
    influences = {(i.ref.table, i.ref.column, i.relation) for i in parsed.influences}
    assert {("hive.store.sales", "status", "filter"), ("hive.store.sales", "shop_id", "join_key"),
            ("hive.store.shops", "shop_id", "join_key")} <= influences
    assert parsed.group_outputs == ["sale_date", "city"]


def test_engine_declares_only_integrated_capabilities():
    engine = get_engine("hive")
    assert engine.supports(VIEW_LINEAGE) and not engine.supports(QUERY) and engine.querier is None
    assert engine.default_port == 10000 and engine.name_levels == ("database", "table")
    d = engine.describe()
    auth = {o["key"]: o for o in d["options"]}["auth"]
    assert auth["type"] == "choice" and auth["choices"] == ["NONE", "NOSASL", "LDAP"] and auth["default"] == "NONE"
    assert d["credential_free"] == {"option": "auth", "values": ["NONE", "NOSASL"]}
    # 未填认证方式时取默认值 NONE：采集账号可不填；LDAP 必须有账号
    assert not engine.credentials_required({}) and not engine.credentials_required({"auth": "NOSASL"})
    assert engine.credentials_required({"auth": "LDAP"})
    assert get_engine("mysql").credentials_required({}) and not get_engine("fake").credentials_required({})


class _NoCredential:
    def scalar(self, _):
        return None


def test_collect_connector_without_credential_for_none_auth():
    ds = Datasource(name="数仓", kind="hive", host="hs2", port=10000, options={"databases": ["store"]})
    conn = collect_connector(_NoCredential(), ds)  # 不连库：构造连接器只整理参数
    assert isinstance(conn, HiveConnector) and conn.auth == "NONE"
    assert conn._conn_args["auth_mechanism"] == "PLAIN" and conn._conn_args["user"] == "avelis"
    from avelis.engines import CredentialMissing

    ldap = Datasource(name="数仓", kind="hive", host="hs2", port=10000,
                      options={"databases": ["store"], "auth": "LDAP"})
    with pytest.raises(CredentialMissing):
        collect_connector(_NoCredential(), ldap)


def test_connector_auth_modes():
    assert HiveConnector("h", 1, "", "", {"auth": "NOSASL"})._conn_args["auth_mechanism"] == "NOSASL"
    ldap = HiveConnector("h", 1, "u", "p", {"auth": "ldap"})
    assert ldap._conn_args["auth_mechanism"] == "LDAP" and ldap._conn_args["user"] == "u"
    with pytest.raises(ValueError, match="LDAP 需要采集账号与口令"):
        HiveConnector("h", 1, "u", "", {"auth": "LDAP"})
    with pytest.raises(ValueError, match="不支持：KERBEROS"):
        HiveConnector("h", 1, "", "", {"auth": "KERBEROS"})
    with pytest.raises(ValueError, match="名称不合法"):
        HiveConnector("h", 1, "", "", {"databases": ["store; DROP"]}).snapshot()


class _FakeCursor:
    """按 SQL 返回脚本化结果的 HiveServer2 游标（不连库）。"""

    def __init__(self, databases: list[str]):
        self.databases = databases
        self.executed: list[str] = []
        self._rows: list[tuple] = []

    def execute(self, sql: str) -> None:
        self.executed.append(sql)
        if sql == "SELECT version()":
            self._rows = [("4.1.0 r75e40b7537c91a70ccaa31c397d21823c7528eeb",)]
        elif sql == "SHOW DATABASES":
            self._rows = [(d,) for d in self.databases]
        elif sql.startswith("SHOW TABLES IN"):
            self._rows = [("sales",)]
        elif sql.startswith("DESCRIBE FORMATTED"):
            self._rows = SALES
        else:
            raise AssertionError(f"未预期的语句：{sql}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def _fake_connector(monkeypatch, visible: list[str]) -> tuple[HiveConnector, _FakeCursor, _FakeConnection]:
    cursor = _FakeCursor(visible)
    conn = _FakeConnection(cursor)
    connector = HiveConnector("hs2", 10000, "", "", {"databases": ["store"]})
    monkeypatch.setattr(connector, "_connect", lambda timeout=60: conn)
    return connector, cursor, conn


def test_snapshot_fails_when_configured_database_is_not_visible(monkeypatch):
    # 授权插件会按权限过滤 SHOW DATABASES：看不到不代表已删除，整次采集失败，而不是返回一个缺库的“成功”快照
    connector, cursor, conn = _fake_connector(monkeypatch, ["default"])
    with pytest.raises(RuntimeError, match="配置的库 store 在 SHOW DATABASES 中不可见"):
        connector.snapshot()
    assert not any(s.startswith(("SHOW TABLES", "DESCRIBE")) for s in cursor.executed) and conn.closed


def test_snapshot_collects_visible_database(monkeypatch):
    connector, cursor, _ = _fake_connector(monkeypatch, ["default", "store"])
    snap = connector.snapshot()
    assert [a.qualified for a in snap.assets] == ["hive.store.sales"] and snap.warnings == []
    assert "DESCRIBE FORMATTED `store`.`sales`" in cursor.executed


def test_invisible_database_leaves_catalog_untouched(monkeypatch):
    # 采集服务：快照失败时不进入 _apply_snapshot（不会把已有资产记成删除），批次记为失败并保留原因
    from contextlib import contextmanager
    from types import SimpleNamespace

    from avelis.collect import service

    run = SimpleNamespace(id="run-1", status="running", finished_at=None, error=None)
    ds = Datasource(id="ds-1", name="数仓", kind="hive", host="hs2", port=10000, options={"databases": ["store"]})

    class _Session:
        def get(self, model, key):
            return ds if model is Datasource else run

        def scalar(self, _):
            return None

        def add(self, obj):
            obj.id = run.id

        def flush(self):
            pass

    @contextmanager
    def _scope():
        yield _Session()

    connector, _, _ = _fake_connector(monkeypatch, ["default"])
    applied = []
    monkeypatch.setattr(service, "session_scope", _scope)
    monkeypatch.setattr(service, "collect_connector", lambda session, d: connector)
    monkeypatch.setattr(service, "_apply_snapshot", lambda *a, **k: applied.append(a))
    with pytest.raises(service.CollectionFailed, match="配置的库 store"):
        service.run_collection("ds-1", trigger="manual")
    assert applied == [] and run.status == "failed" and "配置的库 store" in run.error
