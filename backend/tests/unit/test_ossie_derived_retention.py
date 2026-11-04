"""A derived import retains its own referenced catalogue metadata."""

from copy import deepcopy

from avelis.governance import metrics
from avelis.interchange.exporter import export_model
from avelis.interchange.importer import import_model
from avelis.interchange.validation import validate_structure
from avelis.models import Asset, Grain, JoinPath
from tests.unit.test_ossie_import import (  # noqa: F401
    approve_relationships,
    catalog,
    extension,
    latest_version,
    metric,
    publish,
    relationship_document,
)


def test_derived_retains_own_referenced_metadata_beyond_component_metadata(catalog):
    model = relationship_document()
    model["metrics"][0]["custom_extensions"] = [
        extension(
            base_dataset="records",
            timezone="UTC",
            time_column="b.observed_on",
            dimensions=[{"name": "remote_group", "column": "j1.group_a"}],
            joins=[{"relationship": "path_a", "alias": "j1", "from": "b"}],
        )
    ]
    approve_relationships(catalog, model)
    catalog.session.get(JoinPath, "path_a").cardinality = "n1"
    catalog.session.add(
        Grain(
            id="derived_retention_grain",
            asset_id="asset_b",
            asset_version_id="asset_b_v1",
            columns=["group_a"],
            origin="observed",
            status="verified",
        )
    )
    assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
    check = metrics.publish_check(catalog.session, latest_version(catalog), catalog.actor)
    assert check["ok"], check["blockers"]
    component = publish(catalog)
    component_document = component.publish_check["ossie"]["document"]
    assert all("custom_extensions" not in dataset for dataset in component_document["datasets"])

    derived = deepcopy(model)
    item = derived["metrics"][0]
    item["name"] = "derived_metric"
    item["expression"]["dialects"][0]["expression"] = "SUM(records.value_a) * 2"
    item["custom_extensions"] = [
        extension(
            kind="derived",
            formula="metric_a * 2",
            components=[{"metric_code": "metric_a", "version_no": 1}],
            timezone="UTC",
        )
    ]
    for dataset in derived["datasets"]:
        dataset["custom_extensions"] = [{"vendor_name": "external", "data": f"dataset:{dataset['name']}"}]
        field_name = "value_a" if dataset["name"] == "records" else "group_a"
        next(field for field in dataset["fields"] if field["name"] == field_name)["custom_extensions"] = [
            {"vendor_name": "external", "data": f"field:{dataset['name']}.{field_name}"}
        ]
        dataset["fields"].append(
            {"name": "unused_calculation", "expression": {"sql": "value_a * 10"}, "datatype": "Decimal"}
        )
    derived["relationships"][0]["custom_extensions"] = [{"vendor_name": "external", "data": "relationship:path_a"}]
    unused = deepcopy(derived["datasets"][0])
    unused.update(name="unused", source="catalog.schema_a.records_c")
    derived["datasets"].append(unused)
    unused_alias = deepcopy(derived["datasets"][0])
    unused_alias["name"] = "unused_alias"
    derived["datasets"].append(unused_alias)
    catalog.session.get(Asset, "hidden_asset").domain_id = "visible"

    assert import_model(catalog.session, catalog.actor, derived, "visible", dry_run=False)["ok"]
    retained = latest_version(catalog, "derived_metric").publish_check["ossie"]["document"]
    assert {dataset["name"] for dataset in retained["datasets"]} == {"records", "records_b"}
    for dataset in retained["datasets"]:
        assert "unused_calculation" not in {field["name"] for field in dataset["fields"]}
        assert dataset["custom_extensions"] == [{"vendor_name": "external", "data": f"dataset:{dataset['name']}"}]
        field_name = "value_a" if dataset["name"] == "records" else "group_a"
        field = next(field for field in dataset["fields"] if field["name"] == field_name)
        assert field["custom_extensions"] == [
            {"vendor_name": "external", "data": f"field:{dataset['name']}.{field_name}"}
        ]
    assert retained["relationships"][0]["custom_extensions"] == derived["relationships"][0]["custom_extensions"]

    publish(catalog, "derived_metric")
    exported = export_model(catalog.session, catalog.actor, "visible", [metric(catalog, "derived_metric").id])
    assert validate_structure(exported) == []
    for dataset in exported["datasets"]:
        assert dataset["custom_extensions"] == [{"vendor_name": "external", "data": f"dataset:{dataset['name']}"}]
        field_name = "value_a" if dataset["name"] == "records" else "group_a"
        field = next(field for field in dataset["fields"] if field["name"] == field_name)
        assert field["custom_extensions"] == [
            {"vendor_name": "external", "data": f"field:{dataset['name']}.{field_name}"}
        ]
    assert exported["relationships"][0]["custom_extensions"] == derived["relationships"][0]["custom_extensions"]
