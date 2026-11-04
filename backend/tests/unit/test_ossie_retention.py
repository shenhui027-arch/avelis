"""Bound retained interchange metadata independently of unrelated shared content."""

import json
from copy import deepcopy
from hashlib import sha256

import pytest
from sqlalchemy import event, select

from avelis.governance import metrics
from avelis.interchange import importer
from avelis.interchange.exporter import _root_extension_originals, export_model
from avelis.interchange.importer import ImportRejected, import_model
from avelis.models import MetricVersion
from tests.unit.test_ossie_export import validate_official_schema
from tests.unit.test_ossie_import import (  # noqa: F401
    approve_relationships,
    catalog,
    document,
    extension,
    latest_version,
    publish,
    relationship_document,
    snapshot,
)


def test_retains_only_referenced_datasets_and_fields(catalog):
    model = document()
    unused = deepcopy(model["datasets"][0])
    unused.update(name="unused", source="catalog.schema_a.records_b")
    model["datasets"].append(unused)
    model["datasets"][0]["fields"].append(
        {"name": "unused_calculation", "expression": {"sql": "value_a * 10"}, "datatype": "Decimal"}
    )

    assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
    retained = latest_version(catalog).publish_check["ossie"]["document"]
    assert [dataset["name"] for dataset in retained["datasets"]] == ["records"]
    assert [field["name"] for field in retained["datasets"][0]["fields"]] == [
        "value_a",
        "observed_on",
        "group_a",
    ]
    assert retained["metrics"] == model["metrics"]
    assert "relationships" not in retained


def test_unused_relationships_are_not_retained(catalog):
    model = relationship_document()
    approve_relationships(catalog, model)
    model["metrics"][0]["custom_extensions"] = [
        extension(base_dataset="records", timezone="UTC", time_column="b.observed_on", joins=[], dimensions=[])
    ]
    assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
    retained = latest_version(catalog).publish_check["ossie"]["document"]
    assert [dataset["name"] for dataset in retained["datasets"]] == ["records"]
    assert "relationships" not in retained


def test_thousand_metrics_do_not_multiply_large_root_extensions(catalog):
    model = document()
    item = model["metrics"][0]
    model["metrics"] = [{**deepcopy(item), "name": f"metric_{index:04d}"} for index in range(1000)]
    model["custom_extensions"] = [{"vendor_name": "external", "data": json.dumps({"large": "x" * (4 * 1024**2)})}]
    raw = json.dumps(
        {"custom_extensions": model["custom_extensions"]}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    report = import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
    assert report["counts"]["created"] == 1000
    versions = list(catalog.session.scalars(select(MetricVersion)))
    sizes = []
    holders = []
    for version in versions:
        check = version.publish_check
        metadata = check["ossie"]
        assert metadata["root_extensions_summary"] == {
            "sha256": sha256(raw).hexdigest(),
            "bytes": len(raw),
            "top_level_keys": ["custom_extensions"],
        }
        if "custom_extensions" in metadata["document"]:
            holders.append(version)
            assert metadata["document"]["custom_extensions"] == model["custom_extensions"]
        assert len(metadata["document"]["metrics"]) == 1
        sizes.append(len(json.dumps(check).encode("utf-8")))
    assert len(sizes) == 1000
    assert [version.id for version in holders] == [report["results"][0]["version_id"]]
    assert sum(size == min(sizes) for size in sizes) == 999
    assert len(raw) < sum(sizes) < 2 * len(raw)

    # State-machine transitions are covered elsewhere; this regression exercises
    # the full large export without adding 1,000 unrelated approval/audit writes.
    for version in versions:
        version.status = "published"
    catalog.session.flush()
    exported = export_model(catalog.session, catalog.actor, "visible")
    validate_official_schema(exported)
    assert exported["custom_extensions"] == model["custom_extensions"]
    assert len(exported["metrics"]) == 1000
    subset = export_model(catalog.session, catalog.actor, "visible", [versions[-1].metric_id])
    validate_official_schema(subset)
    assert subset["custom_extensions"] == model["custom_extensions"]
    assert len(subset["metrics"]) == 1

    small = importer._root_extensions_summary({"custom_extensions": [{"vendor_name": "external", "data": "small"}]})
    large = importer._root_extensions_summary(model)
    assert len(json.dumps(large)) - len(json.dumps(small)) < 10


def test_root_extensions_keep_opaque_strings_and_repeated_entries(catalog, caplog):
    model = document()
    original = {"vendor_name": "external", "data": '{ "root_note": "原文  \\u4e2d", "n": 1.00 }\n'}
    model["custom_extensions"] = [original, deepcopy(original)]
    assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
    publish(catalog)

    exported = export_model(catalog.session, catalog.actor, "visible")
    validate_official_schema(exported)
    assert exported["custom_extensions"] == model["custom_extensions"]
    assert "根级自定义扩展仅保留摘要" not in caplog.text
    assert import_model(catalog.session, catalog.actor, exported, "visible", dry_run=False)["ok"]
    publish(catalog)
    assert export_model(catalog.session, catalog.actor, "visible") == exported


def test_missing_historical_root_original_still_warns(catalog, caplog):
    model = document()
    model["custom_extensions"] = [{"vendor_name": "external", "data": '{"root_note":"original"}'}]
    assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
    version = latest_version(catalog)
    check = deepcopy(version.publish_check)
    check["ossie"]["document"].pop("custom_extensions")
    version.publish_check = check
    publish(catalog)

    exported = export_model(catalog.session, catalog.actor, "visible")
    validate_official_schema(exported)
    assert "custom_extensions" not in exported
    assert "根级自定义扩展仅保留摘要，导出不包含扩展原文" in caplog.text


@pytest.mark.parametrize("subset", [False, True])
def test_explicit_empty_root_extensions_survive_export(catalog, caplog, subset):
    model = document()
    model["metrics"].append({**deepcopy(model["metrics"][0]), "name": "metric_b"})
    model["custom_extensions"] = []
    report = import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
    for result in report["results"]:
        catalog.session.get(MetricVersion, result["version_id"]).status = "published"
    catalog.session.flush()
    selection = [report["results"][1]["metric_id"]] if subset else None
    exported = export_model(catalog.session, catalog.actor, "visible", selection)
    validate_official_schema(exported)
    assert exported["custom_extensions"] == []
    assert "根级自定义扩展仅保留摘要" not in caplog.text


def test_root_lookup_batches_and_deduplicates_shared_digests(catalog):
    model = document()
    model["metrics"].append({**deepcopy(model["metrics"][0]), "name": "metric_b"})
    model["custom_extensions"] = [{"vendor_name": "external", "data": "shared original"}]
    first = import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
    second = import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
    subset = [catalog.session.get(MetricVersion, report["results"][1]["version_id"]) for report in (first, second)]
    catalog.session.flush()
    queries = []

    def count(connection, cursor, statement, parameters, context, many):
        queries.append(statement)

    event.listen(catalog.session.bind, "before_cursor_execute", count)
    try:
        originals = _root_extension_originals(catalog.session, subset)
    finally:
        event.remove(catalog.session.bind, "before_cursor_execute", count)
    assert len(queries) == 1
    assert list(originals.values()) == [model["custom_extensions"]]


@pytest.mark.parametrize("dry_run", [True, False])
def test_retained_budget_counts_the_single_shared_original(catalog, monkeypatch, dry_run):
    model = document()
    model["custom_extensions"] = [{"vendor_name": "external", "data": "x" * 8192}]
    before = snapshot(catalog)
    monkeypatch.setattr(importer, "MAX_RETAINED_METADATA_BYTES", 8192)
    with pytest.raises(ImportRejected) as caught:
        import_model(catalog.session, catalog.actor, model, "visible", dry_run=dry_run)
    assert "元数据总量超过" in caught.value.detail["errors"][0]["reason"]
    catalog.session.flush()
    assert snapshot(catalog) == before


@pytest.mark.parametrize("dry_run", [True, False])
def test_retained_budget_rejects_all_versions_before_writes(catalog, monkeypatch, dry_run):
    model = document()
    model["metrics"].append({**deepcopy(model["metrics"][0]), "name": "metric_b"})
    before = snapshot(catalog)
    sizes = []
    original_check = importer._publish_check

    def check_with_budget(plan):
        check = original_check(plan)
        sizes.append(len(json.dumps(check).encode("utf-8")))
        monkeypatch.setattr(importer, "MAX_RETAINED_METADATA_BYTES", sizes[0] * 3 // 2)
        return check

    def no_write(*args, **kwargs):
        pytest.fail("metadata budget must be checked before creating any metric/version")

    monkeypatch.setattr(metrics, "create_metric", no_write)
    monkeypatch.setattr(metrics, "create_version", no_write)
    monkeypatch.setattr(importer, "_publish_check", check_with_budget)
    with pytest.raises(ImportRejected) as caught:
        import_model(catalog.session, catalog.actor, model, "visible", dry_run=dry_run)
    detail = caught.value.detail
    assert max(sizes) < importer.MAX_RETAINED_METADATA_BYTES < sum(sizes)
    assert "元数据总量超过" in detail["errors"][0]["reason"]
    assert detail["counts"] == {"created": 0, "new_version": 0, "skipped": 0, "failed": 2}
    catalog.session.flush()
    assert snapshot(catalog) == before
