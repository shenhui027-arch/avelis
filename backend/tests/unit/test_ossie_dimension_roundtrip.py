"""Issue #87: dimension names must not redirect grouping to another physical column."""

import pytest
import sqlglot

from avelis.interchange.exporter import export_model
from avelis.models import ColumnVersion, MetricVersion
from tests.unit.test_ossie_export import avelis, metric
from tests.unit.test_ossie_export import catalogue as catalogue
from tests.unit.test_ossie_export_catalog import reimport


@pytest.fixture
def country_catalogue(catalogue):
    for ordinal, name in enumerate(("country", "country_code"), start=4):
        catalogue.session.add(
            ColumnVersion(
                asset_version_id="records-v1",
                column_id=f"records-{name}",
                ordinal=ordinal,
                name=name,
                data_type="varchar",
            )
        )
    catalogue.session.flush()
    return catalogue


def assert_dimension_roundtrip(catalogue, expected_dimensions, expected_fields):
    # export_model also runs its real structure/model/catalogue/plan self-check.
    document = export_model(catalogue.session, catalogue.actor, "domain")
    assert len(document["datasets"]) == 1
    dataset = document["datasets"][0]
    fields = {field["name"]: field for field in dataset["fields"]}
    assert len({name.casefold() for name in fields}) == len(dataset["fields"])

    # A third-party consumer sees core fields, without consulting vendor extensions.
    dimension_fields = {name: field for name, field in fields.items() if "dimension" in field}
    assert dimension_fields.keys() == expected_fields.keys()
    for name, field in dimension_fields.items():
        expression = field["expression"]
        sql = expression.get("sql") or next(
            entry["expression"] for entry in expression["dialects"] if entry["dialect"] == "postgres"
        )
        physical = sqlglot.parse_one(sql, read="postgres")
        assert isinstance(physical, sqlglot.exp.Column)
        assert physical.name == expected_fields[name]
        assert physical.table in ("", dataset["name"])
        assert field["dimension"] == {"is_time": False}
    for dimensions in expected_dimensions.values():
        for dimension in dimensions:
            physical = sqlglot.parse_one(dimension["column"], read="postgres").name
            assert physical in expected_fields.values()

    # Extensions retain each metric's own dimension name and physical mapping.
    assert {item["name"]: avelis(item)["dimensions"] for item in document["metrics"]} == expected_dimensions
    report = reimport(catalogue, document)  # Validates the unmodified official 48da55a schema too.
    assert report["ok"]
    assert report["errors"] == []
    assert {result["name"] for result in report["results"]} == expected_dimensions.keys()
    for result in report["results"]:
        imported = catalogue.session.get(MetricVersion, result["version_id"])
        assert imported.dimensions == expected_dimensions[result["name"]]
    return fields


@pytest.mark.parametrize("occupied", [False, True], ids=["free-suffix", "casefold-occupied-suffix"])
def test_ossie_dimension_name_collision_preserves_physical_column(country_catalogue, occupied):
    catalogue = country_catalogue
    if occupied:
        catalogue.session.add(
            ColumnVersion(
                asset_version_id="records-v1",
                column_id="records-COUNTRY_2",
                ordinal=6,
                name="COUNTRY_2",
                data_type="varchar",
            )
        )
    _, version = metric(catalogue)
    version.dimensions = [{"name": "country", "column": "b.country_code", "label": "国家代码"}]
    version.time_column = ""
    allocated = "country_3" if occupied else "country_2"

    fields = assert_dimension_roundtrip(catalogue, {"measure_a": version.dimensions}, {allocated: "country_code"})
    assert "dimension" not in fields["country"]
    assert fields["country"]["expression"]["dialects"][0]["expression"] == '"country"'
    assert "dimension" not in fields["country_code"]
    assert fields["country_code"]["expression"]["dialects"][0]["expression"] == '"country_code"'
    assert set(fields) == {
        "observed_at",
        "value_a",
        "value_b",
        "band_id",
        "country",
        "country_code",
        allocated,
        *(["COUNTRY_2"] if occupied else []),
    }


@pytest.mark.parametrize("columns", [("country", "country_code"), ("country_code", "country")])
def test_ossie_shared_dimension_name_keeps_each_metrics_physical_column(country_catalogue, columns):
    catalogue = country_catalogue
    expected = {}
    for code, column in zip(("measure_a", "measure_b"), columns, strict=True):
        _, version = metric(catalogue, code)
        version.dimensions = [{"name": "country", "column": f"b.{column}", "label": "国家"}]
        version.time_column = ""
        expected[code] = version.dimensions

    fields = assert_dimension_roundtrip(catalogue, expected, {"country": "country", "country_2": "country_code"})
    assert "dimension" not in fields["country_code"]
    assert len(fields) == 7


def test_ossie_same_name_and_physical_column_reuses_existing_dimension_field(country_catalogue):
    catalogue = country_catalogue
    expected = {}
    for code in ("measure_a", "measure_b"):
        _, version = metric(catalogue, code)
        version.dimensions = [{"name": "country", "column": "b.country", "label": "国家"}]
        version.time_column = ""
        expected[code] = version.dimensions

    fields = assert_dimension_roundtrip(catalogue, expected, {"country": "country"})
    assert set(fields) == {"observed_at", "value_a", "value_b", "band_id", "country", "country_code"}
    assert fields["country"]["expression"]["dialects"][0]["expression"] == '"country"'
    assert "dimension" not in fields["country_code"]
