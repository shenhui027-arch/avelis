"""Core consumers must receive the same executable metric semantics."""

import sqlite3

import pytest
import sqlglot

from avelis.governance.errors import GovernanceError
from avelis.interchange.exporter import export_model
from tests.unit.test_ossie_export import approved_path, avelis, metric, validate_official_schema
from tests.unit.test_ossie_export import catalogue as catalogue


@pytest.mark.parametrize("method", ["last", "avg"])
@pytest.mark.parametrize("derived", [False, True])
def test_export_refuses_time_bucket_composition_in_selected_metrics_and_dependencies(catalogue, method, derived):
    model, version = metric(catalogue, time_aggregation=method)
    selected = model.id
    if derived:
        child, formula = metric(catalogue, "double_measure", kind="derived")
        formula.expression = "measure_a * 2"
        formula.components = [{"metric_code": model.code, "version_id": version.id, "version_no": version.version_no}]
        selected = child.id
    with pytest.raises(GovernanceError, match=f"跨时间聚合方式 {method} 无法在 Ossie 核心模型中等价表达"):
        export_model(catalogue.session, catalogue.actor, "domain", [selected])


def test_export_keeps_governance_grain_and_dedupe_evidence_without_changing_distinct_sql(catalogue):
    _, version = metric(catalogue, grain=["b.band_id"], dedupe_basis="按 value_a 的不同取值计数")
    version.expression = "COUNT(DISTINCT b.value_a)"
    document = export_model(catalogue.session, catalogue.actor, "domain")
    validate_official_schema(document)
    item = document["metrics"][0]
    assert item["expression"]["dialects"][0]["expression"] == "COUNT(DISTINCT records.value_a)"
    assert avelis(item)["grain"] == version.grain and avelis(item)["dedupe_basis"] == version.dedupe_basis


@pytest.mark.parametrize("filters", ["b.band_id = 1", "b.band_id = 999"])
def test_export_inlines_identical_component_filters_for_core_consumers(catalogue, filters):
    left, first = metric(catalogue, "measure_a", filters=filters)
    right, second = metric(catalogue, "measure_b", filters=filters)
    for version in (first, second):
        version.dimensions = []
        version.time_column = ""
    second.expression = "COUNT(*)"
    _, derived = metric(catalogue, "ratio_a", kind="derived")
    derived.expression = "measure_a / measure_b"
    derived.components = [
        {"metric_code": model.code, "version_id": version.id, "version_no": version.version_no}
        for model, version in [(left, first), (right, second)]
    ]
    document = export_model(catalogue.session, catalogue.actor, "domain", ["ratio_a"])
    validate_official_schema(document)
    item = document["metrics"][0]
    core = item["expression"]["dialects"][0]["expression"]
    assert core.count("CASE WHEN") == 2
    assert "measure_a" not in core and "measure_b" not in core
    with sqlite3.connect(":memory:") as database:
        database.execute("CREATE TABLE records(value_a NUMERIC, band_id INTEGER)")
        database.executemany("INSERT INTO records VALUES (?, ?)", [(10, 1), (20, 1), (100, 2)])
        expected = database.execute(
            f"SELECT 1.0 * SUM(b.value_a) / NULLIF(COUNT(*), 0) FROM records AS b WHERE {filters}"
        ).fetchone()[0]
        actual = database.execute(
            f"SELECT {sqlglot.transpile(core, read='postgres', write='sqlite')[0]} FROM records"
        ).fetchone()[0]
    assert actual == expected
    assert avelis(item)["formula"] == derived.expression


@pytest.mark.parametrize("difference", ["base_asset_id", "joins", "filters", "timezone", "data_grain"])
def test_export_refuses_derived_components_with_different_row_sets(catalogue, difference):
    left, first = metric(catalogue, "measure_a")
    right, second = metric(catalogue, "measure_b")
    if difference == "joins":
        approved_path(catalogue)
        second.joins = [{"join_path_id": "path", "alias": "r"}]
    else:
        setattr(
            second,
            difference,
            {"base_asset_id": "bands", "filters": "b.band_id = 1", "timezone": "Asia/Taipei", "data_grain": "month"}[
                difference
            ],
        )
    _, derived = metric(catalogue, "ratio_a", kind="derived")
    derived.expression = "measure_a / measure_b"
    derived.components = [
        {"metric_code": model.code, "version_id": version.id, "version_no": version.version_no}
        for model, version in [(left, first), (right, second)]
    ]
    with pytest.raises(GovernanceError, match="组成口径不共享相同的行集"):
        export_model(catalogue.session, catalogue.actor, "domain", ["ratio_a"])
