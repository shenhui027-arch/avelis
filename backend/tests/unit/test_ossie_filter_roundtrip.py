"""Fixed filters remain executable in the core and lossless in Avelis round trips."""

import json
import sqlite3

import pytest
import sqlglot
from sqlalchemy import select

from avelis.governance.errors import GovernanceError
from avelis.interchange.exporter import export_model
from avelis.interchange.expressions import equivalent_expressions, fold_filters
from avelis.interchange.importer import import_model
from avelis.models import DomainMember, MetricVersion
from tests.unit.test_ossie_export import approved_path, avelis, metric, validate_official_schema
from tests.unit.test_ossie_export import catalogue as catalogue


@pytest.mark.parametrize(
    "expression",
    [
        "SUM(b.value_a)",
        "AVG(b.value_a)",
        "MIN(b.value_a)",
        "MAX(b.value_a)",
        "COUNT(b.value_a)",
        "COUNT(*)",
        "SUM(DISTINCT b.value_a)",
        "AVG(DISTINCT b.value_a)",
        "MIN(DISTINCT b.value_a)",
        "MAX(DISTINCT b.value_a)",
        "COUNT(DISTINCT b.value_a)",
        "SUM(b.value_a) / NULLIF(COUNT(*), 0)",
    ],
)
@pytest.mark.parametrize("filters", ["b.band_id = 1", "b.band_id = 999"])
def test_folded_aggregate_has_the_same_sql_result_as_where(expression, filters):
    folded = fold_filters(expression, filters, "postgres")
    assert "CASE WHEN" in folded
    with sqlite3.connect(":memory:") as database:
        database.execute("CREATE TABLE records(value_a NUMERIC, band_id INTEGER)")
        database.executemany(
            "INSERT INTO records VALUES (?, ?)", [(10, 1), (20, 1), (10, 1), (100, 2), (None, 1), (None, 2)]
        )
        expected = database.execute(f"SELECT {expression} FROM records AS b WHERE {filters}").fetchone()
        actual = database.execute(f"SELECT {folded} FROM records AS b").fetchone()
    assert actual == expected


@pytest.mark.parametrize(
    "expression",
    [
        "SUM(b.value_a) OVER ()",
        "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY b.value_a)",
        "STDDEV(b.value_a)",
        "SUM(b.value_a) + b.value_b",
        "SUM(MAX(b.value_a))",
        "SUM(b.value_a ORDER BY b.value_b)",
        "SUM(b.value_a) FILTER (WHERE b.value_b > 0)",
        "COUNT(DISTINCT b.value_a, b.value_b)",
        "COUNT(b.*)",
        "SUM((SELECT MAX(value_a) FROM records))",
        "SUM(b.value_a) + CUSTOM_AGG(1)",
    ],
)
def test_export_refuses_aggregates_that_cannot_safely_fold_filters(catalogue, expression):
    _, version = metric(catalogue, filters="b.band_id = 1")
    version.expression = expression
    with pytest.raises(GovernanceError, match="固定过滤条件无法安全折入核心表达式"):
        export_model(catalogue.session, catalogue.actor, "domain")


def test_export_filters_every_aggregate_and_round_trip_preserves_original_strings(catalogue):
    _, version = metric(catalogue, filters="b.band_id = 1")
    version.expression = "sum( b.value_a ) + COUNT(DISTINCT b.value_b) + COUNT(*)"
    original_expression, original_filters = version.expression, version.filters
    exported = export_model(catalogue.session, catalogue.actor, "domain")
    validate_official_schema(exported)
    item = exported["metrics"][0]
    core = item["expression"]["dialects"][0]["expression"]
    assert core.count("CASE WHEN") == 3
    assert "COUNT(DISTINCT CASE WHEN" in core
    assert "COUNT(CASE WHEN records.band_id = 1 THEN 1 END)" in core
    assert avelis(item)["expression"] == original_expression
    assert avelis(item)["filters"] == original_filters
    with sqlite3.connect(":memory:") as database:
        sql = sqlglot.parse_one(core, read="postgres").sql(dialect="sqlite")
        value = database.execute(
            f"WITH records(value_a, value_b, band_id) AS (VALUES (10, 7, 1), (20, 7, 1), (100, 9, 2)) "
            f"SELECT {sql} FROM records"
        ).fetchone()[0]
    assert value == 33
    report, roundtripped = _import(catalogue, exported)
    assert report["ok"] and not report["warnings"]
    assert roundtripped.expression == original_expression
    assert roundtripped.filters == original_filters


def _import(catalogue, document):
    catalogue.session.scalar(select(DomainMember).where(DomainMember.domain_id == "domain")).role = "maintainer"
    report = import_model(catalogue.session, catalogue.actor, document, "domain", dry_run=False)
    version = catalogue.session.get(MetricVersion, report["results"][0]["version_id"])
    return report, version


def test_import_changed_core_discards_extension_expression_and_filter(catalogue):
    metric(catalogue, filters="b.band_id = 1")
    exported = export_model(catalogue.session, catalogue.actor, "domain")
    exported["metrics"][0]["expression"]["dialects"][0]["expression"] = "SUM(records.value_b)"
    report, version = _import(catalogue, exported)
    assert equivalent_expressions(version.expression, "SUM(b.value_b)", "postgres")
    assert version.filters is None
    assert any("已采用核心表达式" in warning["reason"] for warning in report["warnings"])


@pytest.mark.parametrize("original", ["SUM(b.missing)", "SUM(b.value_a); SELECT 1", "SUM("])
def test_import_untrusted_extension_cannot_override_core(catalogue, original):
    metric(catalogue, filters="b.band_id = 1")
    exported = export_model(catalogue.session, catalogue.actor, "domain")
    item = exported["metrics"][0]
    data = avelis(item)
    data["expression"] = original
    next(extension for extension in item["custom_extensions"] if extension["vendor_name"] == "avelis")["data"] = (
        json.dumps(data)
    )
    report, version = _import(catalogue, exported)
    assert "CASE WHEN" in version.expression and version.filters is None
    assert any("已采用核心表达式" in warning["reason"] for warning in report["warnings"])


def test_normalized_core_keeps_filter_and_original_expression(catalogue):
    _, original = metric(catalogue, filters="b.band_id = 1")
    exported = export_model(catalogue.session, catalogue.actor, "domain")
    exported["metrics"][0]["expression"]["dialects"][0]["expression"] = (
        'sum((case when (records."band_id" = 1) then (records."value_a") end))'
    )
    report, version = _import(catalogue, exported)
    assert not report["warnings"]
    assert version.expression == original.expression and version.filters == original.filters


def test_filter_roundtrip_maps_logical_fields_and_join_aliases(catalogue):
    _, original = metric(catalogue, filters="b.band_id = 1 AND r.value_b > 0")
    approved_path(catalogue)
    original.expression = "SUM(r.value_a)"
    original.joins = [{"join_path_id": "path", "alias": "r"}]
    original.publish_check = {
        "ossie": {
            "document": {
                "datasets": [
                    {
                        "name": name,
                        "source": f"catalog.public.{name}",
                        "fields": [
                            {"name": column, "expression": {"sql": f"{column} * 2"}}
                            for column in ("band_id", "value_a", "value_b")
                        ],
                    }
                    for name in ("records", "bands")
                ]
            }
        }
    }
    exported = export_model(catalogue.session, catalogue.actor, "domain")
    core = exported["metrics"][0]["expression"]["dialects"][0]["expression"]
    assert "records.band_id__column" in core and "bands.value_a__column" in core
    report, version = _import(catalogue, exported)
    assert not report["warnings"]
    assert version.expression == original.expression and version.filters == original.filters
    assert version.joins[0]["alias"] == "r"


def test_equivalence_preserves_quoted_identifier_semantics():
    assert equivalent_expressions('SUM(((b."value_a")))', "sum(value_a)", "postgres")
    assert not equivalent_expressions('SUM(b."VALUE_A")', "SUM(b.value_a)", "postgres")
    assert not equivalent_expressions("SUM(b.value_a); SELECT 1", "SUM(b.value_a)", "postgres")
