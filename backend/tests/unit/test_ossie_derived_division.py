"""Derived formula division rules must not change executable component SQL."""

import sqlite3

import pytest
import sqlglot
from sqlglot import exp

from avelis.governance.derived import evaluate
from avelis.interchange.exporter import export_model
from tests.unit.test_ossie_export import catalogue as catalogue
from tests.unit.test_ossie_export import metric, validate_official_schema


@pytest.mark.parametrize("values", [(1, 2), (0, 0), (-2, -1)])
@pytest.mark.parametrize(
    "formula",
    [
        "measure_a * 2",
        "measure_a / 2",
        "measure_a / (measure_a + 1)",
        "(measure_a / 2) / (measure_a / 4)",
        "measure_a / (measure_a - measure_a)",
    ],
)
def test_derived_export_preserves_component_integer_division(catalogue, values, formula):
    model, component = metric(catalogue)
    component.expression = "SUM(b.value_a) / COUNT(*)"
    _, derived = metric(catalogue, "derived_a", kind="derived")
    derived.expression = formula
    derived.components = [{"metric_code": model.code, "version_id": component.id, "version_no": component.version_no}]

    document = export_model(catalogue.session, catalogue.actor, "domain", ["derived_a"])
    validate_official_schema(document)
    core = document["metrics"][0]["expression"]["dialects"][0]["expression"]
    node = sqlglot.parse_one(core, read="postgres")
    divisions = list(node.find_all(exp.Div))
    component_divisions = [division for division in divisions if isinstance(division.this, exp.Sum)]
    assert component_divisions
    assert all(
        division.sql(dialect="postgres") == "SUM(records.value_a) / COUNT(*)" for division in component_divisions
    )
    formula_divisions = [division for division in divisions if division not in component_divisions]
    assert len(formula_divisions) == len(list(sqlglot.parse_one(formula).find_all(exp.Div)))
    assert len(list(node.find_all(exp.Nullif))) == len(formula_divisions)
    assert all(isinstance(division.expression, exp.Nullif) for division in formula_divisions)

    with sqlite3.connect(":memory:") as database:
        database.execute("CREATE TABLE records(value_a INTEGER)")
        database.executemany("INSERT INTO records VALUES (?)", [(value,) for value in values])
        base_value = database.execute("SELECT SUM(b.value_a) / COUNT(*) FROM records AS b").fetchone()[0]
        actual = database.execute(
            f"SELECT {sqlglot.transpile(core, read='postgres', write='sqlite')[0]} FROM records"
        ).fetchone()[0]
    assert actual == evaluate(formula, {"measure_a": base_value})
