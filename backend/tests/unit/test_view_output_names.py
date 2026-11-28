"""视图声明的列名按位置对应到查询的输出列（parse_query 的 output_names）：列级来源、表达式、分组键与透传映射都跟着换名。

CREATE VIEW v (a, b) AS … 声明的列名不在保存的查询里（如 Spark 的 View Text）；视图的列就是查询的输出列、按位置对应。
"""

from __future__ import annotations

from avelis.engines import get_engine
from avelis.lineage.sql import parse_query

DIALECT = get_engine("spark").sql
SCHEMA = {"spark_catalog": {"db": {"t": {"id": "int", "category": "string", "amt": "decimal(10,2)"}}}}


def parse(sql: str, names: list[str] | None):
    return parse_query(sql, dialect=DIALECT, schema=SCHEMA, default_db="db", output_names=names)


def test_columns_and_expressions_follow_declared_names_by_position():
    parsed = parse("SELECT 1 AS a, 2 AS b FROM db.t", ["y", "x"])
    assert [(c.name, c.expression) for c in parsed.columns] == [("y", "1"), ("x", "2")]
    assert [(c["name"], c["expression"]) for c in parsed.details()["columns"]] == [("y", "1"), ("x", "2")]


def test_passthrough_mapping_uses_declared_names():
    parsed = parse("SELECT id, category FROM db.t", ["category", "id"])
    assert parsed.passthrough["mapping"] == {"id": "category", "category": "id"}


def test_group_key_is_found_by_position_not_by_duplicated_name():
    parsed = parse("SELECT MAX(id) AS k, category AS k FROM db.t GROUP BY category", ["maximum", "category_key"])
    assert parsed.group_outputs == ["category_key"]


def test_duplicate_output_names_map_to_their_own_view_columns():
    parsed = parse(
        "SELECT a.id, b.id FROM db.t a JOIN db.t b ON a.id = b.id GROUP BY a.id, b.id", ["left_id", "right_id"]
    )
    assert [c.name for c in parsed.columns] == ["left_id", "right_id"]
    assert parsed.group_outputs == ["left_id", "right_id"]


def test_mismatched_column_count_keeps_query_names():
    parsed = parse("SELECT id, category FROM db.t", ["only_one"])
    assert [c.name for c in parsed.columns] == ["id", "category"]
    unnamed = parse("SELECT id, category FROM db.t", None)
    assert [c.name for c in unnamed.columns] == ["id", "category"]
