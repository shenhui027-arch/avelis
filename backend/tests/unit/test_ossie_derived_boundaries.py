"""Derived Ossie exports preserve Python formulas and dimension grouping boundaries."""

import sqlite3

import pytest
import sqlglot
from sqlalchemy import select

from avelis.governance.derived import evaluate
from avelis.governance.errors import GovernanceError
from avelis.i18n import locale_context
from avelis.interchange.exporter import export_model
from avelis.interchange.expressions import expand_derived_formula
from avelis.interchange.importer import ImportRejected, import_model
from avelis.models import DomainMember, MetricVersion
from tests.unit.test_ossie_export import catalogue as catalogue
from tests.unit.test_ossie_export import metric, validate_official_schema


def _derived(catalogue, formula, components):
    model, version = metric(catalogue, "derived_a", kind="derived")
    version.base_asset_id = None
    version.expression = formula
    version.components = [
        {"metric_code": model.code, "version_id": component.id, "version_no": component.version_no}
        for model, component in components
    ]
    return model, version


def _roundtrip(catalogue, document, formula):
    session = catalogue.session
    session.scalar(select(DomainMember).where(DomainMember.domain_id == "domain")).role = "maintainer"
    report = import_model(session, catalogue.actor, document, "domain", dry_run=False)
    assert report["ok"]
    imported = session.get(MetricVersion, report["results"][0]["version_id"])
    assert imported.kind == "derived" and imported.expression == formula
    return imported


@pytest.mark.parametrize(
    ("codes", "formula"),
    [
        (("null",), "null * 2"),
        (("select",), "select + 1"),
        (("case",), "case - 1"),
        (("a", "b"), "a--b"),
        (("a", "b", "c"), "a - (b - c)"),
        (("a", "b", "c"), "a / (b * c)"),
        (("a", "b", "c"), "a / (b / c)"),
        (("a", "b"), "-(a + b)"),
        (("a", "b"), "a / -b"),
        (("a",), "-(-a) + 1.5 * a"),
    ],
)
def test_ossie_derived_python_formula_executes_and_roundtrips(catalogue, codes, formula):
    components = [metric(catalogue, code) for code in codes]
    expressions = ["SUM(b.value_a)", "SUM(b.value_b)", "COUNT(*)"]
    for (_, version), expression in zip(components, expressions, strict=False):
        version.expression = expression
    model, _ = _derived(catalogue, formula, components)
    document = export_model(catalogue.session, catalogue.actor, "domain", [model.id])
    validate_official_schema(document)
    core = document["metrics"][0]["expression"]["dialects"][0]["expression"]
    with sqlite3.connect(":memory:") as database:
        database.execute("CREATE TABLE records(value_a INTEGER, value_b INTEGER)")
        database.executemany("INSERT INTO records VALUES (?, ?)", [(5, 1), (7, 3)])
        values = {
            code: database.execute(f"SELECT {expression} FROM records AS b").fetchone()[0]
            for code, expression in zip(codes, expressions, strict=False)
        }
        actual = database.execute(
            f"SELECT {sqlglot.transpile(core, read='postgres', write='sqlite')[0]} FROM records"
        ).fetchone()[0]
    assert actual == evaluate(formula, values)
    _roundtrip(catalogue, document, formula)


def test_ossie_derived_formula_rejects_missing_component():
    with pytest.raises(ValueError, match="公式引用了不存在的指标 b"):
        expand_derived_formula("a + b", {"a": "SUM(b.value_a)"}, "postgres")


@pytest.mark.parametrize(
    ("locale", "reason"),
    [
        ("zh-CN", "组成口径的同名维度映射到不同的列，内联后无法保持分组语义"),
        ("zh-TW", "組成口徑的同名維度映射到不同的欄位，內聯後無法保留分組語義"),
        (
            "en",
            "Component dimensions with the same name map to different columns; "
            "inlining cannot preserve grouping semantics",
        ),
    ],
)
def test_ossie_derived_refuses_conflicting_dimension_mappings(catalogue, locale, reason):
    components = [metric(catalogue, code) for code in ("a", "b", "c")]
    # The conflict must be found even when the first component has no such name.
    for (_, version), name, column in zip(
        components, ("band", "region", "region"), ("b.band_id", "b.value_a", "b.value_b"), strict=True
    ):
        version.dimensions = [{"name": name, "column": column}]
    model, _ = _derived(catalogue, "a + b + c", components)
    with locale_context(locale), pytest.raises(GovernanceError, match=reason):
        export_model(catalogue.session, catalogue.actor, "domain", [model.id])


@pytest.mark.parametrize(
    ("name", "column"),
    [("region", "b.band_id"), ("region", '((B."band_id"))'), ("other_region", "b.value_b")],
)
def test_ossie_derived_compatible_dimensions_export_and_roundtrip(catalogue, name, column):
    components = [metric(catalogue, code) for code in ("a", "b")]
    components[0][1].dimensions = [{"name": "region", "column": "b.band_id"}]
    components[1][1].dimensions = [{"name": name, "column": column}]
    model, version = _derived(catalogue, "a + b", components)
    document = export_model(catalogue.session, catalogue.actor, "domain", [model.id])
    validate_official_schema(document)
    imported = _roundtrip(catalogue, document, version.expression)
    assert [dimension["name"] for dimension in imported.dimensions] == (["region"] if name == "region" else [])


def test_ossie_derived_import_refuses_changed_local_dimension_mapping(catalogue):
    components = [metric(catalogue, code) for code in ("a", "b")]
    model, _ = _derived(catalogue, "a + b", components)
    document = export_model(catalogue.session, catalogue.actor, "domain", [model.id])
    validate_official_schema(document)
    session = catalogue.session
    session.scalar(select(DomainMember).where(DomainMember.domain_id == "domain")).role = "maintainer"
    components[1][1].dimensions = [{"name": "band", "column": "b.value_b"}]
    with pytest.raises(ImportRejected) as caught:
        import_model(session, catalogue.actor, document, "domain", dry_run=False)
    assert any(
        "组成口径的同名维度映射到不同的列，内联后无法保持分组语义" in error["reason"]
        for error in caught.value.detail["errors"]
    )
