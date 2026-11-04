"""真实 SQLAlchemy 会话中，指纹查询与指标数无关且仅覆盖操作涉及的资产。"""

from copy import deepcopy

import pytest
from sqlalchemy import event

from avelis.collect.service import current_fingerprints
from avelis.interchange.exporter import export_model
from avelis.interchange.importer import import_model
from tests.unit.test_ossie_export import catalogue, metric, validate_official_schema  # noqa: F401
from tests.unit.test_ossie_import import catalog, derived_document, document, latest_version, publish  # noqa: F401


def fingerprint_reads(session):
    queries = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "FROM column_fingerprint" in statement:
            queries.append((statement, parameters))

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    return queries, lambda: event.remove(engine, "before_cursor_execute", record)


@pytest.mark.parametrize("count", [2, 20])
@pytest.mark.parametrize("dry_run", [True, False])
def test_import_shares_one_filtered_snapshot(catalog, count, dry_run):
    model = document()
    item = model["metrics"][0]
    model["metrics"] = [{**deepcopy(item), "name": f"metric_{i}"} for i in range(count)]
    before = deepcopy(model)
    queries, remove = fingerprint_reads(catalog.session)
    try:
        result = import_model(catalog.session, catalog.actor, model, "visible", dry_run=dry_run)
    finally:
        remove()
    assert result["ok"] and result["counts"]["skipped" if dry_run else "created"] == count and model == before
    assert len(queries) == 1
    assert "column_fingerprint.asset_id IN" in queries[0][0] and queries[0][1] == ("asset",)
    if not dry_run:
        dependencies = latest_version(catalog, "metric_0").dependency
        assert {d["column"] for d in dependencies} == {"value_a", "group_a", "observed_on"}
        assert all(d["fingerprint"] == "f" * 64 and d["status"] == "ok" for d in dependencies)


@pytest.mark.parametrize("count", [2, 20])
def test_export_reimport_check_shares_one_filtered_snapshot(catalogue, count):
    for i in range(count):
        metric(catalogue, f"metric_{i}")
    queries, remove = fingerprint_reads(catalogue.session)
    try:
        model = export_model(catalogue.session, catalogue.actor, "domain")
    finally:
        remove()
    assert len(model["metrics"]) == count
    validate_official_schema(model)
    assert len(queries) == 1
    assert "column_fingerprint.asset_id IN" in queries[0][0] and queries[0][1] == ("records",)


def test_empty_asset_filter_does_not_scan_catalog(catalog):
    queries, remove = fingerprint_reads(catalog.session)
    try:
        assert current_fingerprints(catalog.session, asset_ids=set()) == {}
    finally:
        remove()
    assert queries == []


@pytest.mark.parametrize("count", [2, 20])
@pytest.mark.parametrize("operation", ["import", "export"])
def test_derived_component_drift_checks_also_share_snapshot(catalog, count, operation):
    import_model(catalog.session, catalog.actor, document(), "visible", dry_run=False)
    base = publish(catalog)
    model = derived_document({"metric_code": "metric_a", "version_no": base.version_no})
    item = model["metrics"][0]
    model["metrics"] = [{**deepcopy(item), "name": f"derived_{i}"} for i in range(count)]
    if operation == "export":
        result = import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
        for entry in result["results"]:
            publish(catalog, entry["name"])
    queries, remove = fingerprint_reads(catalog.session)
    try:
        if operation == "import":
            assert import_model(catalog.session, catalog.actor, model, "visible")["ok"]
        else:
            exported = export_model(catalog.session, catalog.actor, "visible")
            validate_official_schema(exported)
            assert len(exported["metrics"]) == count + 1
    finally:
        remove()
    assert len(queries) == 1 and queries[0][1] == ("asset",)
