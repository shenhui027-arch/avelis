"""探索查询的允许清单校验（ADR 0009 第 6 点）：StarRocks 与 PostgreSQL 各一套接受、拒绝用例。

校验是纯函数：拒绝时还没有任何东西发到数据库（ExploreRejected.view() 的 sent_to_database 为 False）；
工具层“没有发到数据库”的断言见 test_analysis_explore_tool.py。表源用假的 resolver 绑定。
"""

from __future__ import annotations

import pytest

from avelis.analysis.explore import PROFILES, BoundColumn, BoundTable, ExploreRejected, profile_for, validate

COLS = (BoundColumn("region", "varchar(64)", "地区"), BoundColumn("amount", "decimal(18,2)", "金额"),
        BoundColumn("qty", "int", "数量"), BoundColumn("event_date", "date", "日期"),
        BoundColumn("item_id", "bigint", "条目"))
DIM = (BoundColumn("item_id", "bigint"), BoundColumn("category", "varchar(32)", "类目"))


def resolver(catalog_default: str, db_default: str | None):
    tables = {("d1", "facts"): BoundTable("a1", "v1", "101", catalog_default, "d1", "facts", "table", COLS),
              ("d1", "items"): BoundTable("a2", "v2", "102", catalog_default, "d1", "items", "table", DIM)}

    def resolve(catalog, db, name):
        db = db or db_default
        if catalog not in (None, catalog_default) or (db, name) not in tables:
            raise ExploreRejected("binding", "asset_not_visible", f"表 {name} 不存在或当前用户不可见")
        return tables[(db, name)]
    return resolve


ENGINES = {"starrocks": resolver("default_catalog", None), "postgres": resolver("appdb", None)}


def ok(engine, sql, max_rows=100):
    return validate(sql, PROFILES[engine], ENGINES[engine], max_rows=max_rows)


def rejected(engine, sql):
    with pytest.raises(ExploreRejected) as e:
        ok(engine, sql)
    assert e.value.view()["sent_to_database"] is False
    return e.value


def test_only_starrocks_and_postgres_have_profiles():
    assert profile_for("starrocks") and profile_for("postgres")
    for kind in ("mysql", "clickhouse", "sqlserver", "oracle", "doris"):
        assert profile_for(kind) is None


@pytest.mark.parametrize("engine", ["starrocks", "postgres"])
def test_accepts_bound_tables_aggregates_ctes_and_equi_joins(engine):
    v = ok(engine, "SELECT f.region, SUM(f.amount) AS total, COUNT(*) AS n FROM d1.facts f "
                   "WHERE f.event_date >= '2026-09-01' GROUP BY f.region ORDER BY total DESC")
    assert [c.name for c in v.columns] == ["region", "total", "n"] and v.limit == 101
    assert "LIMIT 101" in v.sql and [t.asset_id for t in v.tables] == ["a1"]
    assert {c.name: (c.kind, c.eligible, c.label_ok) for c in v.columns} == {
        "region": ("text", False, True), "total": ("level", True, False), "n": ("count", True, False)}
    joined = ok(engine, 'WITH base AS (SELECT item_id, amount FROM d1.facts) '
                        'SELECT i.category, SUM(b.amount) AS total FROM base b JOIN d1.items i ON b.item_id = i.item_id '
                        'GROUP BY i.category')
    assert {t.asset_id for t in joined.tables} == {"a1", "a2"}
    assert joined.columns[1].deps == [f"{v.tables[0].qualified}.amount"]  # 派生列追溯到真实输入
    quoted = ok(engine, 'SELECT "region" FROM d1.facts' if engine == "postgres" else "SELECT `region` FROM d1.facts")
    assert quoted.columns[0].label_ok


@pytest.mark.parametrize("engine", ["starrocks", "postgres"])
def test_evidence_and_kinds_are_inferred_from_expressions(engine):
    v = ok(engine, "SELECT region, SUM(CASE WHEN qty > 5 THEN 1 ELSE 0 END) AS big, SUM(amount) / SUM(qty) AS unit, "
                   "SUM(amount) * 100 AS scaled, SUM(amount) * 0.3 AS guess, 42 AS literal, COUNT(DISTINCT item_id) AS items, "
                   "ROUND(SUM(amount), 2) AS rounded, UPPER(region) AS up FROM d1.facts GROUP BY region")
    got = {c.name: (c.kind, c.eligible, c.assumption, c.label_ok) for c in v.columns}
    assert got["big"] == ("count", True, False, False)  # 条件计数：条件里的字面量只用作过滤
    assert got["unit"] == ("ratio", True, False, False)
    assert got["scaled"] == ("unknown", True, False, False)  # 允许的常量
    assert got["guess"][1:3] == (False, True)  # 其他常量：假设计算
    assert got["literal"][1:3] == (False, True)  # 纯字面量
    assert got["items"] == ("count", True, False, False)
    assert got["rounded"][1:3] == (False, True)
    assert got["up"] == ("text", False, False, False)  # 函数处理后的文本不能当 LabelRef


@pytest.mark.parametrize("engine", ["starrocks", "postgres"])
def test_limit_is_generated_by_the_system(engine):
    assert ok(engine, "SELECT region FROM d1.facts LIMIT 10").limit == 10
    big = ok(engine, "SELECT region FROM d1.facts LIMIT 100000")
    assert big.limit == 101 and "LIMIT 101" in big.sql


@pytest.mark.parametrize("engine", ["starrocks", "postgres"])
@pytest.mark.parametrize("sql, stage, code", [
    ("SELECT region FROM d1.facts; SELECT 1", "parse", "multiple_statements"),
    ("DELETE FROM d1.facts", "syntax", "not_select"),
    ("INSERT INTO d1.facts SELECT * FROM d1.facts", "syntax", "not_select"),
    ("SELECT * FROM d1.facts", "syntax", "star"),
    ("SELECT f.* FROM d1.facts f", "syntax", "node_not_allowed"),
    ("SELECT region FROM d1.facts UNION SELECT category FROM d1.items", "syntax", "not_select"),
    ("WITH RECURSIVE r AS (SELECT region FROM d1.facts) SELECT region FROM r", "syntax", "recursive"),
    ("SELECT region FROM d1.facts OFFSET 5", "syntax", "select_clause_not_allowed"),
    ("SELECT region FROM d1.facts, d1.items", "join", "cartesian"),
    ("SELECT f.region FROM d1.facts f JOIN d1.items i ON f.item_id > i.item_id", "join", "non_equi_join"),
    ("SELECT f.region FROM d1.facts f JOIN d1.items i USING (item_id)", "syntax", "join_form"),
    ("SELECT region, (SELECT MAX(qty) FROM d1.facts) AS m FROM d1.facts", "syntax", "scalar_subquery"),
    ("SELECT region FROM d1.facts WHERE item_id IN (SELECT item_id FROM d1.items)", "syntax", "in_subquery"),
    ("SELECT region, SUM(amount) OVER (PARTITION BY region) AS w FROM d1.facts", "syntax", "node_not_allowed"),
    ("SELECT region FROM d1.facts /* hi */", "syntax", "comment"),
    ("SELECT sleep(5) AS s FROM d1.facts", "syntax", "banned_function"),
    ("SELECT my_udf(amount) AS u FROM d1.facts", "syntax", "function_not_allowed"),
    ("SELECT region FROM d1.secret_table", "binding", "asset_not_visible"),
    ("SELECT region FROM other_catalog.d1.facts", "binding", "asset_not_visible"),
    ("SELECT nope FROM d1.facts", "column", "column_unresolved"),
    ("SELECT item_id FROM d1.facts f JOIN d1.items i ON f.item_id = i.item_id", "column", "column_unresolved"),
    ("SELECT region AS x, qty AS x FROM d1.facts", "column", "duplicate_output"),
    ("SELECT CAST(amount AS JSON) AS j FROM d1.facts", "syntax", "cast_type"),
])
def test_rejections_never_reach_the_database(engine, sql, stage, code):
    error = rejected(engine, sql)
    assert (error.stage, error.code) == (stage, code), error.message


def test_engine_specific_rejections():
    assert rejected("starrocks", "SELECT /*+ SET_VAR(query_timeout=1) */ region FROM d1.facts").code in (
        "comment", "node_not_allowed", "select_clause_not_allowed")  # hint / SET_VAR
    assert rejected("starrocks", "SELECT path FROM FILES('path'='s3://x')").code in ("table_function", "banned_function",
                                                                                   "function_not_allowed")
    assert rejected("starrocks", "SELECT * FROM TABLE(generate_series(1, 3))").stage in ("syntax",)
    assert rejected("postgres", "SELECT g FROM generate_series(1, 3) g").stage == "syntax"
    assert rejected("postgres", "SELECT pg_read_file('/etc/passwd') AS x FROM d1.facts").code in ("banned_function",
                                                                                                "function_not_allowed")
    assert rejected("postgres", "SELECT region FROM d1.facts FOR UPDATE").stage == "syntax"
    assert rejected("postgres", "SELECT region FROM d1.facts FETCH FIRST 5 ROWS WITH TIES").stage == "syntax"
    assert rejected("postgres", "SELECT DISTINCT ON (region) region FROM d1.facts").code == "distinct_on"


def test_cte_shadowing_binds_qualified_names_to_physical_tables():
    """带库名的引用一律按物理表绑定，不会被同名 CTE 遮蔽；不带库名的才是 CTE。"""
    assert rejected("postgres", "WITH facts AS (SELECT category FROM d1.items) SELECT region FROM facts").code ==         "column_unresolved"  # 不带库名：是 CTE（它没有 region 列）
    v = ok("postgres", "WITH facts AS (SELECT category FROM d1.items) SELECT f.region FROM d1.facts f")
    assert {t.asset_id for t in v.tables} == {"a1", "a2"} and '"appdb"."d1"."facts" AS "f"' in v.sql  # 带库名：物理表；CTE 里的表同样绑定、核对可见性


def test_static_size_limits():
    long_in = ", ".join(f"'{i}'" for i in range(250))
    assert rejected("postgres", f"SELECT region FROM d1.facts WHERE region IN ({long_in})").code == "in_list_too_long"
    assert rejected("postgres", "SELECT region FROM d1.facts WHERE " + " AND ".join(["qty > 1"] * 200)).stage == "size"
    order = ", ".join(["region"] * 11)
    assert rejected("postgres", f"SELECT region FROM d1.facts ORDER BY {order}").code == "too_many_order_keys"
    joins = " ".join(f"JOIN d1.items i{n} ON f.item_id = i{n}.item_id" for n in range(5))
    assert rejected("postgres", f"SELECT f.region FROM d1.facts f {joins}").code == "too_many_joins"
    assert rejected("postgres", "SELECT region FROM d1.facts WHERE region = '" + "x" * 6000 + "'").code == "sql_too_long"


def test_postgres_uses_only_and_regenerated_sql_is_executed():
    resolve = resolver("appdb", None)

    def only(catalog, db, name):
        t = resolve(catalog, db, name)
        return BoundTable(t.asset_id, t.asset_version_id, t.physical_id, t.catalog, t.db, t.name, t.kind, t.columns,
                          use_only=True)
    v = validate("select region from d1.facts where qty > 1", PROFILES["postgres"], only, max_rows=10)
    assert 'FROM ONLY "appdb"."d1"."facts"' in v.sql and v.sql.startswith("SELECT")
