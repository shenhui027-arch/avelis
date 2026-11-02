"""Core relationship references must preserve every executable inner join."""

import pytest
from sqlalchemy import select

from avelis.governance.errors import GovernanceError
from avelis.governance.metrics import resolve_joins
from avelis.interchange.exporter import _expression, export_model
from avelis.interchange.expressions import equivalent_expressions
from avelis.interchange.importer import import_model
from avelis.models import DomainMember, JoinPath, JoinPathApproval, MetricVersion
from tests.unit.test_ossie_export import approved_path, metric, validate_official_schema
from tests.unit.test_ossie_export import catalogue as catalogue


def joined_metric(catalogue, *, chained=False):
    _, version = metric(catalogue)
    approved_path(catalogue)
    version.joins = [{"join_path_id": "path", "alias": "r"}]
    if chained:
        catalogue.assets["private_records"].domain_id = "domain"
        catalogue.session.add_all(
            [
                JoinPath(
                    id="second-path",
                    code="bands_details",
                    name="关联详情",
                    left_asset_id="bands",
                    left_columns=["band_id"],
                    right_asset_id="private_records",
                    right_columns=["band_id"],
                    cardinality="n1",
                    status="approved",
                    created_by="actor",
                ),
                JoinPathApproval(join_path_id="second-path", domain_id="domain", user_id="actor", decision="approved"),
            ]
        )
        version.joins = [*version.joins, {"join_path_id": "second-path", "alias": "detail", "from": "r"}]
    catalogue.session.flush()
    return version


def remove_extensions(value):
    if isinstance(value, dict):
        value.pop("custom_extensions", None)
        for child in value.values():
            remove_extensions(child)
    elif isinstance(value, list):
        for child in value:
            remove_extensions(child)


@pytest.mark.parametrize("chained", [False, True])
def test_export_refuses_unreferenced_inner_join_including_unused_chain_tail(catalogue, chained):
    version = joined_metric(catalogue, chained=chained)
    if chained:
        # Referencing an earlier target does not make a later filtering join necessary.
        version.expression = "SUM(b.value_a + r.value_b)"
    with pytest.raises(GovernanceError, match="关联仅用于过滤行，Ossie 核心模型无法表达必须内连接的语义"):
        export_model(catalogue.session, catalogue.actor, "domain")


@pytest.mark.parametrize("chained", [False, True])
@pytest.mark.parametrize("role", ["expression", "dimension", "time", "filter"])
def test_referenced_inner_joins_roundtrip_through_core_without_extensions(catalogue, role, chained):
    session = catalogue.session
    session.scalar(select(DomainMember).where(DomainMember.domain_id == "domain")).role = "maintainer"
    version = joined_metric(catalogue, chained=chained)
    alias = "detail" if chained else "r"
    if role == "expression":
        version.expression = f"SUM(b.value_a + {alias}.value_b)"
    elif role == "dimension":
        version.dimensions = [{"name": "band", "column": f"{alias}.band_id"}]
    elif role == "time":
        version.time_column = f"{alias}.observed_at"
    else:
        version.time_column = ""
        version.dimensions = []
        version.filters = f"{alias}.band_id = 1"

    document = export_model(session, catalogue.actor, "domain")
    validate_official_schema(document)
    assert len(document["relationships"]) == (2 if chained else 1)
    core_expression = document["metrics"][0]["expression"]["dialects"][0]["expression"]
    remove_extensions(document)
    report = import_model(session, catalogue.actor, document, "domain", dry_run=False)
    assert report["ok"], report
    imported = session.get(MetricVersion, report["results"][0]["version_id"])
    imported_aliases, imported_steps = resolve_joins(session, imported)
    assert imported.base_asset_id == version.base_asset_id
    assert [step["path"].id for step in imported_steps] == [join["join_path_id"] for join in version.joins]
    names = {name: asset.name for name, asset in imported_aliases.items()}
    assert equivalent_expressions(_expression(imported.expression, "postgres", names), core_expression, "postgres")
    target = "private_records" if chained else "bands"
    if role == "dimension":
        assert any(
            equivalent_expressions(_expression(dim["column"], "postgres", names), f"{target}.band_id", "postgres")
            for dim in imported.dimensions
        )
    elif role == "time":
        assert equivalent_expressions(
            _expression(imported.time_column, "postgres", names), f"{target}.observed_at", "postgres"
        )
    elif role == "filter":
        assert imported.filters is None
        assert f"{target}.band_id = 1" in core_expression and "CASE" in core_expression
