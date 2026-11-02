"""Exports must keep names and retained metadata valid against the current catalogue."""

import logging

import pytest
from sqlalchemy import select

from avelis.governance.errors import Invalid
from avelis.interchange.exporter import export_model
from avelis.interchange.importer import import_model
from avelis.models import ColumnVersion, DomainMember, MetricVersion
from tests.unit.test_ossie_export import approved_path, metric, validate_official_schema
from tests.unit.test_ossie_export import catalogue as catalogue


def reimport(catalogue, document):
    validate_official_schema(document)
    catalogue.session.scalar(select(DomainMember).where(DomainMember.domain_id == "domain")).role = "maintainer"
    return import_model(catalogue.session, catalogue.actor, document, "domain", dry_run=False)


@pytest.mark.parametrize("retained", [False, True])
def test_export_allocates_dataset_names_case_insensitively(catalogue, retained):
    catalogue.assets["bands"].name = "RECORDS"
    catalogue.assets["bands"].db_name = "another_schema"
    _, first = metric(catalogue)
    _, second = metric(catalogue, "measure_b", asset="bands")
    if retained:
        for version, name, source in (
            (first, "records", "catalog.public.records"),
            (second, "RECORDS", "catalog.another_schema.RECORDS"),
        ):
            version.publish_check = {
                "ossie": {
                    "document": {
                        "datasets": [
                            {
                                "name": name,
                                "source": source,
                                "fields": [{"name": "declared", "expression": {"sql": f"{name.swapcase()}.value_b"}}],
                            }
                        ]
                    }
                }
            }
    document = export_model(catalogue.session, catalogue.actor, "domain")
    names = [item["name"] for item in document["datasets"]]
    assert names == ["records", "RECORDS_2"]
    assert len({name.casefold() for name in names}) == 2
    assert reimport(catalogue, document)["ok"]


def test_export_allocates_retained_physical_and_dimension_field_names_case_insensitively(catalogue):
    session = catalogue.session
    _, version = metric(catalogue)
    session.add(
        ColumnVersion(
            asset_version_id="records-v1", column_id="uppercase", ordinal=9, name="VALUE_A", data_type="decimal"
        )
    )
    version.expression = 'SUM(b.value_a) + SUM(b."VALUE_A")'
    version.dimensions = [{"name": "FIELD", "column": "b.band_id"}]
    version.publish_check = {
        "ossie": {
            "document": {
                "datasets": [
                    {
                        "name": "records",
                        "source": "catalog.public.records",
                        "fields": [
                            {"name": "field", "expression": {"sql": "value_a * 2"}},
                            {"name": "FIELD", "expression": {"sql": "value_b"}},
                            {"name": "value_a__column", "expression": {"sql": "value_b"}},
                        ],
                    }
                ]
            }
        }
    }
    document = export_model(session, catalogue.actor, "domain")
    fields = document["datasets"][0]["fields"]
    names = [field["name"] for field in fields]
    assert len({name.casefold() for name in names}) == len(names)
    assert {"field", "FIELD_2", "FIELD_3"} <= set(names)
    assert reimport(catalogue, document)["ok"]
    imported = session.scalars(select(MetricVersion).order_by(MetricVersion.version_no.desc())).first()
    assert 'b."VALUE_A"' in imported.expression and 'b."value_a"' in imported.expression


@pytest.mark.parametrize(("column", "physical"), [("VALUE_A", "value_a"), ('"VALUE_A"', "VALUE_A")])
@pytest.mark.parametrize("retained_identity", [False, True])
def test_export_preserves_dialect_case_folding_of_physical_column_references(
    catalogue, column, physical, retained_identity
):
    session = catalogue.session
    _, version = metric(catalogue)
    session.add(
        ColumnVersion(
            asset_version_id="records-v1", column_id="uppercase", ordinal=9, name="VALUE_A", data_type="decimal"
        )
    )
    version.expression = f"SUM(b.{column})"
    if retained_identity:
        version.publish_check = {
            "ossie": {
                "document": {
                    "datasets": [
                        {
                            "name": "records",
                            "source": "catalog.public.records",
                            "fields": [
                                {
                                    "name": "f",
                                    "expression": {"dialects": [{"dialect": "postgres", "expression": "VALUE_A"}]},
                                }
                            ],
                        }
                    ]
                }
            }
        }
    document = export_model(session, catalogue.actor, "domain")
    report = reimport(catalogue, document)
    imported = session.get(MetricVersion, report["results"][0]["version_id"])
    assert imported.expression == f'SUM(b."{physical}")'


@pytest.mark.parametrize("usage", ["expression", "filters", "join"])
def test_retained_logical_field_cannot_hide_a_deleted_physical_column(catalogue, usage):
    _, version = metric(catalogue)
    version.publish_check = {
        "ossie": {
            "document": {
                "datasets": [
                    {
                        "name": "records",
                        "source": "catalog.public.records",
                        "fields": [{"name": "value_b", "expression": {"sql": "value_a"}}],
                    }
                ]
            }
        }
    }
    if usage == "expression":
        version.expression = "SUM(B.value_b)"
    elif usage == "filters":
        version.filters = "b.value_b > 0"
        version.dimensions = []
        version.time_column = ""
    else:
        path = approved_path(catalogue)
        path.left_columns = ["value_b"]
        version.joins = [{"join_path_id": "path", "alias": "r"}]
        version.dimensions = [{"name": "band", "column": "r.band_id"}]
    catalogue.session.delete(
        catalogue.session.scalar(
            select(ColumnVersion).where(ColumnVersion.asset_version_id == "records-v1", ColumnVersion.name == "value_b")
        )
    )
    with pytest.raises(Invalid) as caught:
        export_model(catalogue.session, catalogue.actor, "domain")
    assert caught.value.status_code == 422
    assert any("records.value_b" in error["reason"] for error in caught.value.detail["errors"])


@pytest.mark.parametrize("expression", ["value_b", "value_b + value_a"])
@pytest.mark.parametrize("datatype", [None, "Date"])
def test_export_drops_deleted_unused_retained_field_and_logs_warning(catalogue, caplog, expression, datatype):
    session = catalogue.session
    _, version = metric(catalogue)
    version.publish_check = {
        "ossie": {
            "document": {
                "datasets": [
                    {
                        "name": "records",
                        "source": "catalog.public.records",
                        "fields": [
                            {
                                "name": "old_field",
                                "expression": {"sql": expression},
                                **({"datatype": datatype} if datatype else {}),
                            }
                        ],
                    }
                ]
            }
        }
    }
    stale = session.scalar(
        select(ColumnVersion).where(ColumnVersion.asset_version_id == "records-v1", ColumnVersion.name == "value_b")
    )
    session.delete(stale)
    with caplog.at_level(logging.WARNING, logger="avelis.interchange.exporter"):
        document = export_model(session, catalogue.actor, "domain")
    assert all(field["name"] != "old_field" for field in document["datasets"][0]["fields"])
    assert "records.old_field" in caplog.text
    assert "old_field" in str(version.publish_check)
    assert reimport(catalogue, document)["ok"]


@pytest.mark.parametrize("affected_path", [False, True])
def test_export_drops_field_used_only_by_relationship_that_will_not_be_exported(catalogue, affected_path):
    _, version = metric(catalogue)
    if affected_path:
        approved_path(catalogue).status = "affected"
    version.publish_check = {
        "ossie": {
            "document": {
                "datasets": [
                    {
                        "name": "records",
                        "source": "catalog.public.records",
                        "fields": [
                            {"name": "old_field", "expression": {"sql": "value_b"}},
                        ],
                    },
                    {"name": "bands", "source": "catalog.public.bands"},
                ],
                "relationships": [
                    {
                        "name": "old_relation",
                        "from": "records",
                        "to": "bands",
                        "from_columns": ["old_field"],
                        "to_columns": ["band_id"],
                    }
                ],
            }
        }
    }
    catalogue.session.delete(
        catalogue.session.scalar(
            select(ColumnVersion).where(ColumnVersion.asset_version_id == "records-v1", ColumnVersion.name == "value_b")
        )
    )
    document = export_model(catalogue.session, catalogue.actor, "domain")
    assert document["relationships"] == []
    assert all(field["name"] != "old_field" for field in document["datasets"][0]["fields"])
    assert reimport(catalogue, document)["ok"]


def test_metadata_renaming_preserves_unqualified_fields_in_other_datasets(catalogue):
    _, first = metric(catalogue)
    _, second = metric(catalogue, "measure_b", asset="bands")
    for version, names in ((first, ("b", "other")), (second, ("records", "b"))):
        version.publish_check = {
            "ossie": {
                "document": {
                    "datasets": [
                        {
                            "name": name,
                            "source": f"catalog.public.{asset}",
                            "fields": [
                                {"name": "declared", "expression": {"sql": "value_b"}},
                            ],
                        }
                        for name, asset in zip(names, ("records", "bands"), strict=True)
                    ]
                }
            }
        }
    document = export_model(catalogue.session, catalogue.actor, "domain")
    for dataset in document["datasets"]:
        assert next(field for field in dataset["fields"] if field["name"] == "declared")["expression"] == {
            "sql": "value_b"
        }
    assert reimport(catalogue, document)["ok"]


@pytest.mark.parametrize("usage", ["expression", "filters", "dimension", "time", "primary_key", "unique_key", "join"])
def test_export_rejects_deleted_column_when_still_used(catalogue, usage):
    session = catalogue.session
    _, version = metric(catalogue)
    dataset = {
        "name": "records",
        "source": "catalog.public.records",
        "fields": [{"name": "old_field", "expression": {"sql": "value_b"}}],
    }
    version.publish_check = {"ossie": {"document": {"datasets": [dataset]}}}
    if usage == "expression":
        version.expression = "SUM(b.value_b)"
    elif usage == "filters":
        version.filters = "b.value_b > 0"
        version.dimensions = []
        version.time_column = ""
    elif usage == "dimension":
        version.dimensions = [{"name": "old_field", "column": "b.value_b"}]
    elif usage == "time":
        version.time_column = "b.value_b"
    elif usage == "primary_key":
        dataset["primary_key"] = ["old_field"]
    elif usage == "unique_key":
        dataset["unique_keys"] = [["old_field"]]
    else:
        path = approved_path(catalogue)
        path.left_columns = ["value_b"]
        version.joins = [{"join_path_id": "path", "alias": "r"}]
        version.dimensions = [{"name": "band", "column": "r.band_id"}]
    session.delete(
        session.scalar(
            select(ColumnVersion).where(ColumnVersion.asset_version_id == "records-v1", ColumnVersion.name == "value_b")
        )
    )
    with pytest.raises(Invalid) as caught:
        export_model(session, catalogue.actor, "domain")
    assert caught.value.status_code == 422
    assert caught.value.detail["errors"]
    assert all(set(error) == {"layer", "path", "reason"} for error in caught.value.detail["errors"])
