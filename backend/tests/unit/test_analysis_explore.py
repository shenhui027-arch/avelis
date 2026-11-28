"""探索查询的允许清单校验（ADR 0009 第 6 点）：模型写 Avelis 标准 SQL，StarRocks 与 PostgreSQL 各一套接受、拒绝用例，
每个标准函数在两个引擎上各有一条转换测试。

校验是纯函数：拒绝时还没有任何东西发到数据库（ExploreRejected.view() 的 sent_to_database 为 False）；
工具层“没有发到数据库”的断言见 test_analysis_explore_tool.py。表源用假的 resolver 绑定。
"""

from __future__ import annotations

import pytest

from avelis.analysis.explore import PROFILES, BoundColumn, BoundTable, ExploreRejected, profile_for, validate

COLS = (
    BoundColumn("region", "varchar(64)", "地区"),
    BoundColumn("amount", "decimal(18,2)", "金额"),
    BoundColumn("qty", "int", "数量"),
    BoundColumn("event_date", "date", "日期"),
    BoundColumn("item_id", "bigint", "条目"),
)
DIM = (BoundColumn("item_id", "bigint"), BoundColumn("category", "varchar(32)", "类目"))


def resolver(catalog_default: str, db_default: str | None):
    tables = {
        ("d1", "facts"): BoundTable("a1", "v1", "101", catalog_default, "d1", "facts", "table", COLS),
        ("d1", "items"): BoundTable("a2", "v2", "102", catalog_default, "d1", "items", "table", DIM),
    }

    def resolve(catalog, db, name):
        db = db or db_default
        if catalog not in (None, catalog_default) or (db, name) not in tables:
            raise ExploreRejected("binding", "asset_not_visible", f"表 {name} 不存在或当前用户不可见")
        return tables[(db, name)]

    return resolve


ENGINES = {
    "starrocks": resolver("default_catalog", None),
    "doris": resolver("internal", None),
    "mysql": resolver("def", None),
    "postgres": resolver("appdb", None),
    "clickhouse": resolver("clickhouse", None),
    "sqlserver": resolver("master", None),
    "oracle": resolver("FREEPDB1", None),
    "hive": resolver("hive", None),
    "tidb": resolver("def", None),
}


def ok(engine, sql, max_rows=100):
    return validate(sql, PROFILES[engine], ENGINES[engine], max_rows=max_rows)


def rejected(engine, sql):
    with pytest.raises(ExploreRejected) as e:
        ok(engine, sql)
    assert e.value.view()["sent_to_database"] is False
    return e.value


def test_all_connectors_have_profiles():
    expected_catalog_in_sql = {
        "starrocks": True,
        "doris": True,
        "mysql": False,
        "postgres": True,
        "clickhouse": False,
        "sqlserver": True,
        "oracle": False,
        "hive": False,
        "tidb": False,
    }
    assert set(PROFILES) == set(expected_catalog_in_sql)
    for kind, catalog_in_sql in expected_catalog_in_sql.items():
        p = profile_for(kind)
        assert p is not None and p.catalog_in_sql is catalog_in_sql
    assert profile_for("unknown") is None


@pytest.mark.parametrize("engine", ["starrocks", "postgres"])
def test_accepts_bound_tables_aggregates_ctes_and_equi_joins(engine):
    v = ok(
        engine,
        "SELECT f.region, SUM(f.amount) AS total, COUNT(*) AS n FROM d1.facts f "
        "WHERE f.event_date >= '2026-09-01' GROUP BY f.region ORDER BY total DESC",
    )
    assert [c.name for c in v.columns] == ["region", "total", "n"] and v.limit == 101
    assert "LIMIT 101" in v.sql and [t.asset_id for t in v.tables] == ["a1"]
    assert {c.name: (c.kind, c.eligible, c.label_ok) for c in v.columns} == {
        "region": ("text", False, True),
        "total": ("level", True, False),
        "n": ("count", True, False),
    }
    joined = ok(
        engine,
        "WITH base AS (SELECT item_id, amount FROM d1.facts) "
        "SELECT i.category, SUM(b.amount) AS total FROM base b JOIN d1.items i ON b.item_id = i.item_id "
        "GROUP BY i.category",
    )
    assert {t.asset_id for t in joined.tables} == {"a1", "a2"}
    assert joined.columns[1].deps == [f"{v.tables[0].qualified}.amount"]  # 派生列追溯到真实输入
    quoted = ok(engine, 'SELECT "region" FROM d1."facts"')  # 标准 SQL 用双引号；生成时换成目标方言的引号
    assert quoted.columns[0].label_ok and ("`region`" if engine == "starrocks" else '"region"') in quoted.sql


@pytest.mark.parametrize("engine", ["starrocks", "postgres"])
def test_evidence_and_kinds_are_inferred_from_expressions(engine):
    v = ok(
        engine,
        "SELECT region, SUM(CASE WHEN qty > 5 THEN 1 ELSE 0 END) AS big, SUM(amount) / SUM(qty) AS unit, "
        "SUM(amount) * 100 AS scaled, SUM(amount) * 0.3 AS guess, 42 AS literal, COUNT(DISTINCT item_id) AS items, "
        "COALESCE(SUM(amount), 7) AS padded, CAST(region AS VARCHAR) AS up FROM d1.facts GROUP BY region",
    )
    got = {c.name: (c.kind, c.eligible, c.assumption, c.label_ok) for c in v.columns}
    assert got["big"] == ("count", True, False, False)  # 条件计数：条件里的字面量只用作过滤
    assert got["unit"] == ("unknown", True, False, False)
    assert got["scaled"] == ("unknown", True, False, False)  # 允许的常量
    assert got["guess"][1:3] == (False, True)  # 其他常量：假设计算
    assert got["literal"][1:3] == (False, True)  # 纯字面量
    assert got["items"] == ("count", True, False, False)
    assert got["padded"][1:3] == (False, True)  # 常量 7 进入数值：假设计算
    assert got["up"] == ("text", False, False, False)  # 函数处理后的文本不能当 LabelRef


@pytest.mark.parametrize("engine", ["starrocks", "postgres"])
def test_limit_is_generated_by_the_system(engine):
    assert ok(engine, "SELECT region FROM d1.facts LIMIT 10").limit == 10
    big = ok(engine, "SELECT region FROM d1.facts LIMIT 100000")
    assert big.limit == 101 and "LIMIT 101" in big.sql


@pytest.mark.parametrize("engine", ["starrocks", "postgres"])
@pytest.mark.parametrize(
    "sql, stage, code",
    [
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
        ("SELECT region FROM d1.facts /* hi */", "syntax", "comment"),
        ("SELECT sleep(5) AS s FROM d1.facts", "syntax", "banned_function"),
        ("SELECT my_udf(amount) AS u FROM d1.facts", "syntax", "function_not_allowed"),
        ("SELECT ROUND(amount, 2) AS r FROM d1.facts", "syntax", "function_not_allowed"),  # 不在标准函数清单
        (
            "SELECT DATE_ADD(event_date, INTERVAL 7 DAY) AS d FROM d1.facts",
            "syntax",
            "function_not_allowed",
        ),  # 规范形式唯一
        ("SELECT DATE_TRUNC('hour', event_date) AS d FROM d1.facts", "syntax", "date_unit"),
        ("SELECT event_date + INTERVAL '3' HOUR AS d FROM d1.facts", "syntax", "date_unit"),
        ("SELECT `region` FROM d1.facts", "parse", "parse_error"),  # 方言写法（反引号）不是标准 SQL
        ("SELECT region FROM d1.secret_table", "binding", "asset_not_visible"),
        ("SELECT region FROM other_catalog.d1.facts", "binding", "asset_not_visible"),
        ("SELECT nope FROM d1.facts", "column", "column_unresolved"),
        ("SELECT item_id FROM d1.facts f JOIN d1.items i ON f.item_id = i.item_id", "column", "column_unresolved"),
        ("SELECT region AS x, qty AS x FROM d1.facts", "column", "duplicate_output"),
        ("SELECT CAST(amount AS JSON) AS j FROM d1.facts", "syntax", "cast_type"),
    ],
)
def test_rejections_never_reach_the_database(engine, sql, stage, code):
    error = rejected(engine, sql)
    assert (error.stage, error.code) == (stage, code), error.message


def test_engine_specific_rejections():
    assert rejected("starrocks", "SELECT /*+ SET_VAR(query_timeout=1) */ region FROM d1.facts").code in (
        "comment",
        "node_not_allowed",
        "select_clause_not_allowed",
    )  # hint / SET_VAR
    assert rejected("starrocks", "SELECT path FROM FILES('path'='s3://x')").code in (
        "table_function",
        "banned_function",
        "function_not_allowed",
    )
    assert rejected("starrocks", "SELECT * FROM TABLE(generate_series(1, 3))").stage in ("syntax",)
    assert rejected("postgres", "SELECT g FROM generate_series(1, 3) g").stage == "syntax"
    assert rejected("postgres", "SELECT pg_read_file('/etc/passwd') AS x FROM d1.facts").code in (
        "banned_function",
        "function_not_allowed",
    )
    assert rejected("postgres", "SELECT region FROM d1.facts FOR UPDATE").stage == "syntax"
    assert rejected("postgres", "SELECT region FROM d1.facts FETCH FIRST 5 ROWS WITH TIES").stage == "syntax"
    assert rejected("postgres", "SELECT DISTINCT ON (region) region FROM d1.facts").code == "distinct_on"


def test_cte_shadowing_binds_qualified_names_to_physical_tables():
    """带库名的引用一律按物理表绑定，不会被同名 CTE 遮蔽；不带库名的才是 CTE。"""
    assert (
        rejected("postgres", "WITH facts AS (SELECT category FROM d1.items) SELECT region FROM facts").code
        == "column_unresolved"
    )  # 不带库名：是 CTE（它没有 region 列）
    v = ok("postgres", "WITH facts AS (SELECT category FROM d1.items) SELECT f.region FROM d1.facts f")
    assert {t.asset_id for t in v.tables} == {
        "a1",
        "a2",
    } and '"appdb"."d1"."facts" AS "f"' in v.sql  # 带库名：物理表；CTE 里的表同样绑定、核对可见性


def test_static_size_limits():
    long_in = ", ".join(f"'{i}'" for i in range(250))
    assert rejected("postgres", f"SELECT region FROM d1.facts WHERE region IN ({long_in})").code == "in_list_too_long"
    assert rejected("postgres", "SELECT region FROM d1.facts WHERE " + " AND ".join(["qty > 1"] * 200)).stage == "size"
    order = ", ".join(["region"] * 11)
    assert rejected("postgres", f"SELECT region FROM d1.facts ORDER BY {order}").code == "too_many_order_keys"
    joins = " ".join(f"JOIN d1.items i{n} ON f.item_id = i{n}.item_id" for n in range(5))
    assert rejected("postgres", f"SELECT f.region FROM d1.facts f {joins}").code == "too_many_joins"
    assert (
        rejected("postgres", "SELECT region FROM d1.facts WHERE region = '" + "x" * 6000 + "'").code == "sql_too_long"
    )


def test_postgres_uses_only_and_regenerated_sql_is_executed():
    resolve = resolver("appdb", None)

    def only(catalog, db, name):
        t = resolve(catalog, db, name)
        return BoundTable(
            t.asset_id, t.asset_version_id, t.physical_id, t.catalog, t.db, t.name, t.kind, t.columns, use_only=True
        )

    v = validate("select region from d1.facts where qty > 1", PROFILES["postgres"], only, max_rows=10)
    assert 'FROM ONLY "appdb"."d1"."facts"' in v.sql and v.sql.startswith("SELECT")


# --- Codex 审查（ecfd094）的触发场景 ----------------------------------------------------------


def test_cte_body_reference_with_same_name_is_a_physical_table():
    """#1 CTE 名不能遮蔽同名物理表：CTE 自己定义里的同名引用指物理表，必须经过 resolver。"""
    seen = []
    resolve = resolver("appdb", None)

    def tracking(catalog, db, name):
        seen.append((db, name))
        return resolve(catalog, db, name)

    error = None
    try:
        validate(
            "WITH secret AS (SELECT COUNT(*) AS n FROM secret) SELECT n FROM secret",
            PROFILES["postgres"],
            tracking,
            max_rows=10,
        )
    except ExploreRejected as e:
        error = e
    assert error is not None and error.stage == "binding" and seen == [(None, "secret")]
    v = validate(
        "WITH facts AS (SELECT region FROM d1.facts) SELECT region FROM facts",
        PROFILES["postgres"],
        tracking,
        max_rows=10,
    )
    assert [t.asset_id for t in v.tables] == ["a1"]  # 主查询里的 facts 是 CTE
    later = validate(
        "WITH a AS (SELECT region FROM d1.facts), b AS (SELECT region FROM a) SELECT region FROM b",
        PROFILES["postgres"],
        tracking,
        max_rows=10,
    )
    assert [t.asset_id for t in later.tables] == ["a1"]  # 后面的 CTE 可以引用前面的 CTE
    assert (
        rejected(
            "postgres", "WITH a AS (SELECT region FROM b), b AS (SELECT region FROM d1.facts) SELECT region FROM a"
        ).stage
        == "binding"
    )  # 引用排在后面的 CTE：按物理表绑定


def test_query_without_real_source_is_rejected():
    """#7 没有 FROM 的查询直接拒绝；COUNT(*) 的依赖取当前作用域的真实输入。"""
    assert rejected("postgres", "SELECT COUNT(*) * 100000000 AS n").code == "no_source"
    assert rejected("postgres", "SELECT n FROM (SELECT 1 AS n) t").code == "no_source"
    v = ok("postgres", "SELECT COUNT(*) AS n FROM d1.facts")
    assert v.columns[0].deps == [f"{v.tables[0].qualified}.*"] and v.columns[0].eligible


def test_join_must_link_the_new_table_to_previous_ones():
    """#10 ON 条件必须把本次 JOIN 引入的表和前面已有的表连起来。"""
    sql = (
        "SELECT f.region FROM d1.facts f JOIN d1.items i ON f.item_id = i.item_id "
        "JOIN d1.items j ON f.item_id = i.item_id"
    )
    assert rejected("postgres", sql).code == "join_not_linked"
    assert rejected("starrocks", sql).code == "join_not_linked"
    ok(
        "postgres",
        "SELECT f.region FROM d1.facts f JOIN d1.items i ON f.item_id = i.item_id "
        "JOIN d1.items j ON j.item_id = f.item_id",
    )


# --- 标准函数到目标方言的转换（每个标准函数在两个引擎上各一条；没有测试的函数不进 STANDARD_FUNCTIONS）---------

TRANSLATIONS = [  # (标准 SQL 的输出表达式, StarRocks, PostgreSQL)
    ("SUM(amount)", "SUM(`facts`.`amount`)", 'SUM("facts"."amount")'),
    ("COUNT(*)", "COUNT(*)", "COUNT(*)"),
    ("COUNT(DISTINCT item_id)", "COUNT(DISTINCT `facts`.`item_id`)", 'COUNT(DISTINCT "facts"."item_id")'),
    ("AVG(amount)", "AVG(`facts`.`amount`)", 'AVG("facts"."amount")'),
    ("MIN(amount)", "MIN(`facts`.`amount`)", 'MIN("facts"."amount")'),
    ("MAX(amount)", "MAX(`facts`.`amount`)", 'MAX("facts"."amount")'),
    ("COALESCE(amount, 0)", "COALESCE(`facts`.`amount`, 0)", 'COALESCE("facts"."amount", 0)'),
    ("NULLIF(qty, 0)", "NULLIF(`facts`.`qty`, 0)", 'NULLIF("facts"."qty", 0)'),
    (
        "CASE WHEN qty > 5 THEN 1 ELSE 0 END",
        "CASE WHEN `facts`.`qty` > 5 THEN 1 ELSE 0 END",
        'CASE WHEN "facts"."qty" > 5 THEN 1 ELSE 0 END',
    ),
    ("amount / qty", "`facts`.`amount` / `facts`.`qty`", 'CAST("facts"."amount" AS DOUBLE PRECISION) / "facts"."qty"'),
    ("CAST(qty AS BIGINT)", "CAST(`facts`.`qty` AS BIGINT)", 'CAST("facts"."qty" AS BIGINT)'),
    (
        "CAST(amount AS DECIMAL(18, 2))",
        "CAST(`facts`.`amount` AS DECIMAL(18, 2))",
        'CAST("facts"."amount" AS DECIMAL(18, 2))',
    ),
    ("CAST(amount AS DOUBLE)", "CAST(`facts`.`amount` AS DOUBLE)", 'CAST("facts"."amount" AS DOUBLE PRECISION)'),
    ("CAST(item_id AS VARCHAR)", "CAST(`facts`.`item_id` AS VARCHAR)", 'CAST("facts"."item_id" AS VARCHAR)'),
    ("CAST(region AS DATE)", "CAST(`facts`.`region` AS DATE)", 'CAST("facts"."region" AS DATE)'),
    (
        "CAST(event_date AS TIMESTAMP)",
        "CAST(`facts`.`event_date` AS DATETIME)",
        'CAST("facts"."event_date" AS TIMESTAMP)',
    ),
    (
        "DATE_TRUNC('month', event_date)",
        "DATE_TRUNC('MONTH', `facts`.`event_date`)",
        'DATE_TRUNC(\'MONTH\', "facts"."event_date")',
    ),
    (
        "event_date + INTERVAL '7' DAY",
        "`facts`.`event_date` + INTERVAL '7' DAY",
        '"facts"."event_date" + INTERVAL \'7 DAY\'',
    ),
    (
        "event_date - INTERVAL '1' MONTH",
        "`facts`.`event_date` - INTERVAL '1' MONTH",
        '"facts"."event_date" - INTERVAL \'1 MONTH\'',
    ),
    (
        "ROW_NUMBER() OVER (PARTITION BY region ORDER BY amount DESC)",
        "ROW_NUMBER() OVER (PARTITION BY `facts`.`region` "
        "ORDER BY CASE WHEN `facts`.`amount` IS NULL THEN 1 ELSE 0 END DESC, `facts`.`amount` DESC)",
        'ROW_NUMBER() OVER (PARTITION BY "facts"."region" ORDER BY "facts"."amount" DESC)',
    ),
    (
        "RANK() OVER (ORDER BY amount DESC)",
        "RANK() OVER (ORDER BY CASE WHEN `facts`.`amount` IS NULL THEN 1 ELSE 0 END DESC, `facts`.`amount` DESC)",
        'RANK() OVER (ORDER BY "facts"."amount" DESC)',
    ),
    (
        "DENSE_RANK() OVER (ORDER BY amount DESC)",
        "DENSE_RANK() OVER (ORDER BY CASE WHEN `facts`.`amount` IS NULL THEN 1 ELSE 0 END DESC, `facts`.`amount` DESC)",
        'DENSE_RANK() OVER (ORDER BY "facts"."amount" DESC)',
    ),
    (
        "LAG(amount, 1) OVER (PARTITION BY region ORDER BY event_date)",
        "LAG(`facts`.`amount`, 1) OVER (PARTITION BY `facts`.`region` "
        "ORDER BY CASE WHEN `facts`.`event_date` IS NULL THEN 1 ELSE 0 END, `facts`.`event_date`)",
        'LAG("facts"."amount", 1) OVER (PARTITION BY "facts"."region" ORDER BY "facts"."event_date")',
    ),
    (
        "LEAD(amount, 1, 0) OVER (PARTITION BY region ORDER BY event_date)",
        "LEAD(`facts`.`amount`, 1, 0) OVER (PARTITION BY `facts`.`region` "
        "ORDER BY CASE WHEN `facts`.`event_date` IS NULL THEN 1 ELSE 0 END, `facts`.`event_date`)",
        'LEAD("facts"."amount", 1, 0) OVER (PARTITION BY "facts"."region" ORDER BY "facts"."event_date")',
    ),
]


@pytest.mark.parametrize("standard, starrocks, postgres", TRANSLATIONS)
def test_standard_functions_translate(standard, starrocks, postgres):
    for engine, expected in (("starrocks", starrocks), ("postgres", postgres)):
        v = ok(engine, f"SELECT {standard} AS x FROM d1.facts")
        alias = " AS `x` FROM " if engine == "starrocks" else ' AS "x" FROM '
        assert v.sql.removeprefix("SELECT ").split(alias)[0] == expected, (engine, v.sql)
        table = "`default_catalog`.`d1`.`facts`" if engine == "starrocks" else '"appdb"."d1"."facts"'
        assert f"FROM {table}" in v.sql, v.sql


def test_every_standard_function_has_a_translation_test():
    from sqlglot import exp

    from avelis.analysis.explore import STANDARD_FUNCTIONS

    tested = {
        exp.Sum,
        exp.Count,
        exp.Avg,
        exp.Min,
        exp.Max,
        exp.Coalesce,
        exp.Nullif,
        exp.TimestampTrunc,
        exp.RowNumber,
        exp.Rank,
        exp.DenseRank,
        exp.Lag,
        exp.Lead,
    }
    assert set(STANDARD_FUNCTIONS) == tested  # 新增标准函数时必须同时补上 TRANSLATIONS


@pytest.mark.parametrize(
    "engine",
    ["starrocks", "doris", "mysql", "postgres", "clickhouse", "sqlserver", "oracle", "hive", "tidb"],
)
def test_all_eight_dialects_translate_standard_and_window_functions(engine):
    sql = (
        "SELECT region, SUM(amount) OVER (PARTITION BY region) AS w_sum, "
        "ROW_NUMBER() OVER (PARTITION BY region ORDER BY amount DESC) AS rn, "
        "RANK() OVER (ORDER BY amount DESC) AS rk, "
        "DENSE_RANK() OVER (ORDER BY amount DESC) AS drk, "
        "LAG(amount, 1) OVER (PARTITION BY region ORDER BY event_date) AS prev_amt, "
        "LEAD(amount, 1, 0) OVER (PARTITION BY region ORDER BY event_date) AS next_amt, "
        "SUM(amount) OVER (PARTITION BY region ORDER BY event_date ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS roll, "
        "DATE_TRUNC('month', event_date) AS m, "
        "DATE_TRUNC('week', event_date) AS wk, "
        "DATE_TRUNC('quarter', event_date) AS qtr, "
        "event_date + INTERVAL '7' DAY AS d7, "
        "event_date - INTERVAL '1' WEEK AS w1, "
        "event_date + INTERVAL '1' QUARTER AS q1, "
        "CAST(region AS VARCHAR) AS r_txt, "
        "CAST(amount AS DOUBLE) AS a_dbl, "
        "CAST(event_date AS TIMESTAMP) AS ts "
        "FROM d1.facts"
    )
    v = ok(engine, sql)
    assert v.dialect == PROFILES[engine].dialect
    if PROFILES[engine].catalog_in_sql:
        assert ENGINES[engine](None, "d1", "facts").catalog in v.sql
    else:
        assert ENGINES[engine](None, "d1", "facts").catalog not in v.sql
    if engine == "oracle":
        assert "FETCH FIRST 101 ROWS ONLY" in v.sql and "TRUNC(" in v.sql and "TIMESTAMP_TRUNC" not in v.sql
    elif engine == "sqlserver":
        assert "TOP 101" in v.sql and "DATEADD(" in v.sql and "INTERVAL" not in v.sql
    elif engine == "mysql":
        assert "1900-01-01 00:00:00" in v.sql and "0000-01-01" not in v.sql
    got = {c.name: (c.kind, c.eligible, c.assumption, c.label_ok) for c in v.columns}
    assert got["w_sum"] == ("level", True, False, False)
    assert got["rn"] == ("count", True, False, False)
    assert got["prev_amt"] == ("unknown", True, False, False)
    assert got["next_amt"] == ("unknown", True, False, False)
    assert got["roll"] == ("level", True, False, False)


@pytest.mark.parametrize("engine", ["starrocks", "postgres", "mysql", "oracle", "sqlserver"])
@pytest.mark.parametrize(
    "sql, code",
    [
        (
            "SELECT region, SUM(amount) OVER (PARTITION BY region) AS w FROM d1.facts QUALIFY w > 10",
            "select_clause_not_allowed",
        ),
        ("SELECT region, SUM(ROW_NUMBER() OVER (ORDER BY amount)) OVER () AS x FROM d1.facts", "nested_window"),
        ("SELECT region FROM d1.facts WHERE ROW_NUMBER() OVER (ORDER BY amount) = 1", "window_clause_not_allowed"),
        (
            "SELECT region, SUM(amount) AS s FROM d1.facts GROUP BY ROW_NUMBER() OVER (ORDER BY region)",
            "window_clause_not_allowed",
        ),
        (
            "SELECT region, SUM(amount) AS s FROM d1.facts "
            "GROUP BY region HAVING ROW_NUMBER() OVER (ORDER BY region) = 1",
            "window_clause_not_allowed",
        ),
        ("SELECT region, ROW_NUMBER() AS rn FROM d1.facts", "window_requires_over"),
        ("SELECT region, LAG(amount, 1) AS prev FROM d1.facts", "window_requires_over"),
        (
            "SELECT region, LAG(amount, 1) IGNORE NULLS OVER (ORDER BY event_date) AS p FROM d1.facts",
            "window_function_not_allowed",
        ),
        (
            "SELECT region, SUM(amount) OVER (ORDER BY event_date "
            "ROWS BETWEEN qty PRECEDING AND CURRENT ROW) AS s FROM d1.facts",
            "window_frame_not_allowed",
        ),
    ],
)
def test_unsafe_window_patterns_are_rejected(engine, sql, code):
    assert rejected(engine, sql).code == code


def test_window_lag_text_column_cannot_be_label_ref():
    v = ok("postgres", "SELECT region, LAG(region, 1) OVER (ORDER BY event_date) AS prev_region FROM d1.facts")
    got = {c.name: (c.kind, c.eligible, c.label_ok) for c in v.columns}
    assert got["region"] == ("text", False, True)
    assert got["prev_region"] == ("text", False, False)


# --- 源方言与标识符大小写（标准 SQL 按 sqlglot 的 postgres 方言解析，tobymao/sqlglot#8845） -----------------------

CASED = (
    BoundColumn("OrderID", "bigint", "订单号"),
    BoundColumn("AMT", "decimal(18,2)", "金额"),
    BoundColumn("订单数", "int", "订单数"),
    BoundColumn("region", "varchar(16)", "地区"),
)


def cased_resolver(catalog_default):
    table = BoundTable("a9", "v9", "109", catalog_default, "d1", "orders", "table", CASED)

    def resolve(catalog, db, name):
        if (db, name) != ("d1", "orders"):
            raise ExploreRejected("binding", "asset_not_visible", f"表 {name} 不存在或当前用户不可见")
        return table

    return resolve


@pytest.mark.parametrize("engine,catalog,quote", [("starrocks", "default_catalog", "`"), ("postgres", "appdb", '"')])
def test_identifier_case_follows_postgres_rules(engine, catalog, quote):
    """有大写字母的列名（StarRocks 资产的大写、大小写混合列名，PostgreSQL 加了引号的列名）写法与 describe_asset 一致并加
    双引号；中文列名加不加引号都可以；生成的 SQL 用目标方言的引号保留原样。只差大小写时提示加双引号的写法。"""

    def run(sql):
        return validate(sql, PROFILES[engine], cased_resolver(catalog), max_rows=100)

    v = run(
        'SELECT "OrderID", SUM("AMT") AS total, SUM(订单数) AS n, SUM("订单数") AS m FROM d1.orders GROUP BY "OrderID"'
    )
    q = quote
    assert f"{q}orders{q}.{q}OrderID{q}" in v.sql and f"{q}orders{q}.{q}AMT{q}" in v.sql and f"{q}订单数{q}" in v.sql
    assert {c.name: c.deps for c in v.columns}["total"] == [f"{catalog}.d1.orders.AMT"]
    assert {c.name: c.deps for c in v.columns}["n"] == [f"{catalog}.d1.orders.订单数"]
    with pytest.raises(ExploreRejected) as e:
        run("SELECT OrderID, SUM(amt) AS total FROM d1.orders GROUP BY OrderID")  # 不加引号：折成小写，找不到
    assert e.value.code == "column_unresolved" and e.value.view()["sent_to_database"] is False
    assert any('写成 "AMT"' in h for h in e.value.hints) and any('写成 "OrderID"' in h for h in e.value.hints)


@pytest.mark.parametrize("engine", ["starrocks", "postgres"])
def test_division_is_numeric_on_every_engine(engine):
    """标准 SQL 的除法是数值除法：不按 PostgreSQL 的整数相除取整，两个引擎结果一致。"""
    v = ok(engine, "SELECT SUM(amount) / SUM(qty) AS unit FROM d1.facts")
    assert (
        ("CAST(SUM(" in v.sql and "AS DOUBLE PRECISION) / SUM(" in v.sql)
        if engine == "postgres"
        else "SUM(`facts`.`amount`) / SUM(`facts`.`qty`)" in v.sql
    )


def test_unquoted_identifiers_fold_to_lowercase_before_routing_and_binding():
    """路由与绑定之前按 PostgreSQL 规则规范化：未加引号的表名、库名折成小写，加了引号的原样保留。"""
    from avelis.analysis.explore import table_refs

    assert table_refs("SELECT 1 AS x FROM PUBLIC.ORDERS") == [(None, "public", "orders")]
    assert table_refs('SELECT 1 AS x FROM "Sales"."Orders"') == [(None, "Sales", "Orders")]
    v = ok("postgres", "SELECT REGION, SUM(AMOUNT) AS total FROM D1.FACTS GROUP BY REGION")
    assert [t.asset_id for t in v.tables] == ["a1"] and '"facts"."region"' in v.sql
    assert rejected("postgres", 'SELECT region FROM "D1".facts').code == "asset_not_visible"  # 加引号：大小写原样


@pytest.mark.parametrize("engine", ["starrocks", "postgres"])
def test_conditional_aggregates_for_two_periods_are_evidence(engine):
    (
        """两期对比的常见写法：SUM(CASE WHEN 日期 BETWEEN … THEN 列 ELSE 0 END)。"""
        """条件里的日期字面量只用来过滤，ELSE 0 是中性值，
    整体是列的登记聚合，有资格作为证据；两个这样的 SUM 相减是差额。分支值是其他常量时仍判为假设。"""
    )
    cur = (
        "SUM(CASE WHEN f.event_date BETWEEN CAST('2026-09-01' AS DATE) AND CAST('2026-09-30' AS DATE) "
        "THEN f.qty ELSE 0 END)"
    )
    prev = (
        "SUM(CASE WHEN f.event_date BETWEEN CAST('2026-08-01' AS DATE) AND CAST('2026-08-31' AS DATE) "
        "THEN f.qty ELSE 0 END)"
    )
    v = ok(
        engine,
        f"SELECT f.region, {cur} AS cur, {prev} AS prev, {cur} - {prev} AS chg FROM d1.facts f GROUP BY f.region",
    )
    got = {c.name: (c.kind, c.eligible, c.assumption) for c in v.columns}
    assert got["cur"] == ("level", True, False) and got["prev"] == ("level", True, False)
    assert got["chg"] == ("delta", True, False)
    deps = {c.name: c.deps for c in v.columns}["cur"]
    assert any(d.endswith(".qty") for d in deps) and any(d.endswith(".event_date") for d in deps)
    bad = ok(engine, "SELECT SUM(CASE WHEN f.qty > 5 THEN 99999 ELSE 0 END) AS padded FROM d1.facts f")
    assert bad.columns[0].assumption is True and bad.columns[0].eligible is False  # 分支值是业务常量：假设
    counted = ok(engine, "SELECT SUM(CASE WHEN f.region IN ('甲', '乙') THEN 1 ELSE 0 END) AS n FROM d1.facts f")
    assert counted.columns[0].kind == "count" and counted.columns[0].eligible  # 条件计数照旧


def test_clickhouse_dialect_rewrites_frame_and_intervals():
    sql = (
        "SELECT region, "
        "LAG(amount, 1) OVER (PARTITION BY region ORDER BY event_date) AS p, "
        "LEAD(amount, 1, 0) OVER (PARTITION BY region ORDER BY event_date) AS n, "
        "event_date + INTERVAL '7' DAY AS d7, "
        "event_date - INTERVAL '1' WEEK AS w1 "
        "FROM d1.facts"
    )
    v = ok("clickhouse", sql)
    assert "lagInFrame(" in v.sql and "leadInFrame(" in v.sql
    assert "toIntervalDay(7)" in v.sql and "toIntervalWeek(1)" in v.sql
