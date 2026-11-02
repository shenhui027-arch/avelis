"""Derived extensions must describe the same executable core as local pinned components."""

import json

import pytest

from avelis.interchange.exporter import export_model
from avelis.interchange.importer import import_model
from tests.unit.test_ossie_export import validate_official_schema
from tests.unit.test_ossie_import import catalog as catalog
from tests.unit.test_ossie_import import (
    derived_document,
    document,
    extension,
    latest_version,
    metric,
    publish,
    rejected,
)


def _export_derived(catalog):
    import_model(catalog.session, catalog.actor, document(), "visible", dry_run=False)
    source = publish(catalog)
    model = derived_document({"metric_code": "metric_a", "version_no": 1, "expression": source.expression})
    import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
    publish(catalog, "metric_c")
    exported = export_model(catalog.session, catalog.actor, "visible", [metric(catalog, "metric_c").id])
    validate_official_schema(exported)
    return exported


def _assert_catalog_failure(catalog, model, reason):
    detail = rejected(catalog, model)
    assert all(error["layer"] == "catalog" for error in detail["errors"])
    assert any(reason in error["reason"] for error in detail["errors"])
    return detail


def test_import_derived_rejects_changed_executable_core_without_writes(catalog):
    exported = _export_derived(catalog)
    core = exported["metrics"][0]["expression"]["dialects"][0]
    core["expression"] = f"({core['expression']}) * 100"
    _assert_catalog_failure(catalog, exported, "核心表达式与扩展中的派生公式不一致，需人工处理")


def test_import_derived_normalizes_core_and_maps_logical_fields_to_local_columns(catalog):
    exported = _export_derived(catalog)
    fields = exported["datasets"][0]["fields"]
    next(field for field in fields if field["name"] == "value_a")["name"] = "amount"
    exported["metrics"][0]["expression"]["dialects"][0]["expression"] = '((sum( "records"."amount" )) * (2))'
    report = import_model(catalog.session, catalog.actor, exported, "visible", dry_run=False)
    assert report["ok"] and latest_version(catalog, "metric_c").expression == "metric_a * 2"


def test_import_derived_accepts_equivalent_local_component_definition_with_warning(catalog):
    exported = _export_derived(catalog)
    source = latest_version(catalog)
    source.expression = "sum( (b.value_a) )"
    catalog.session.flush()
    report = import_model(catalog.session, catalog.actor, exported, "visible", dry_run=False)
    assert report["ok"]
    assert any("本环境的版本与导出方不同" in warning["reason"] for warning in report["warnings"])
    assert latest_version(catalog, "metric_c").components[0]["version_id"] == source.id


@pytest.mark.parametrize("difference", ["time_aggregation", "filters", "expression", "base_asset_id"])
def test_import_derived_reports_local_components_that_cannot_expand_safely(catalog, difference):
    exported = _export_derived(catalog)
    source = latest_version(catalog)
    setattr(
        source,
        difference,
        {
            "time_aggregation": "last",
            "filters": "b.group_a = 'A'",
            "expression": "SUM(b.value_a); SELECT 1",
            "base_asset_id": "asset_b",
        }[difference],
    )
    catalog.session.flush()
    detail = _assert_catalog_failure(catalog, exported, "无法按本环境锁定的组成口径展开派生公式")
    expected = {
        "time_aggregation": "跨时间聚合方式 last",
        "filters": "固定过滤条件无法在 Ossie 核心模型中保持分组结果",
        "expression": "Expected one SQL expression",
        "base_asset_id": "未在文档中声明",
    }[difference]
    assert any(expected in error["reason"] for error in detail["errors"])


def test_import_derived_checks_local_component_row_sets_before_inlining(catalog):
    for code in ("metric_a", "metric_b"):
        import_model(catalog.session, catalog.actor, document(code), "visible", dry_run=False)
        publish(catalog, code)
    latest_version(catalog, "metric_b").timezone = "Asia/Taipei"
    catalog.session.flush()
    model = document("metric_c", expression="SUM(records.value_a) - SUM(records.value_a)")
    model["metrics"][0]["custom_extensions"] = [
        extension(
            kind="derived",
            formula="metric_a - metric_b",
            components=[{"metric_code": code, "version_no": 1} for code in ("metric_a", "metric_b")],
            timezone="UTC",
        )
    ]
    _assert_catalog_failure(catalog, model, "组成口径不共享相同的行集")


def test_import_derived_reports_unavailable_core_dialect(catalog):
    exported = _export_derived(catalog)
    exported["metrics"][0]["expression"]["dialects"] = [{"dialect": "OPAQUE_SQL", "expression": "OPAQUE()"}]
    _assert_catalog_failure(catalog, exported, "派生口径核心表达式没有可匹配的可解析方言")


def test_import_derived_uses_formula_rather_than_stale_extension_expression(catalog):
    exported = _export_derived(catalog)
    entry = next(e for e in exported["metrics"][0]["custom_extensions"] if e["vendor_name"] == "avelis")
    data = json.loads(entry["data"])
    data["formula"] = "metric_a * 3"
    entry["data"] = json.dumps(data)
    _assert_catalog_failure(catalog, exported, "核心表达式与扩展中的派生公式不一致")
