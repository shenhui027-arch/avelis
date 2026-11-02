"""Published interchange mapping, catalogue visibility, and lossless vendor metadata."""

import json
from copy import deepcopy
from importlib.resources import files
from types import SimpleNamespace

import pytest
import sqlglot
from jsonschema import Draft202012Validator
from sqlalchemy import JSON, MetaData, create_engine, select
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from avelis.db import Base
from avelis.governance.errors import Forbidden, GovernanceError
from avelis.interchange.exporter import export_model
from avelis.interchange.importer import import_model
from avelis.models import (
    Asset,
    ColumnVersion,
    Datasource,
    Domain,
    DomainMember,
    JoinPath,
    JoinPathApproval,
    Metric,
    MetricVersion,
    User,
)


@pytest.fixture
def catalogue():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for original in Base.metadata.tables.values():
        table = original.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        actor = User(id="actor", username="actor", display_name="维护者", password_hash="unused")
        session.add_all(
            [
                actor,
                Domain(id="domain", code="observations", name="观测域", owner_id="actor"),
                Domain(id="hidden", code="hidden", name="隐藏域", owner_id="actor"),
                DomainMember(domain_id="domain", user_id="actor", role="viewer"),
                Datasource(id="source", name="离线源", kind="postgres", host="offline", port=1),
            ]
        )
        session.flush()
        assets = {}
        for name, domain in (("records", "domain"), ("bands", "domain"), ("private_records", "hidden")):
            asset = Asset(
                id=name,
                datasource_id="source",
                kind="table",
                catalog_name="catalog",
                db_name="public",
                name=name,
                domain_id=domain,
                current_version_id=f"{name}-v1",
            )
            assets[name] = asset
            session.add(asset)
            for ordinal, column in enumerate(("observed_at", "value_a", "value_b", "band_id")):
                session.add(
                    ColumnVersion(
                        asset_version_id=f"{name}-v1",
                        column_id=f"{name}-{column}",
                        ordinal=ordinal,
                        name=column,
                        data_type="timestamp" if column == "observed_at" else "decimal",
                        comment=f"字段 {column}",
                    )
                )
        session.flush()
        yield SimpleNamespace(session=session, actor=actor, assets=assets)
    engine.dispose()


def metric(catalogue, code="measure_a", *, asset="records", status="published", domain="domain", **changes):
    session = catalogue.session
    model = Metric(
        id=code,
        code=code,
        name=f"名称 {code}",
        domain_id=domain,
        owner_id="actor",
        created_by="actor",
        ai_context={"instructions": "先核对观察范围", "synonyms": ["观察甲"], "examples": ["范围甲如何变化"]},
    )
    version = MetricVersion(
        id=f"{code}-v1",
        metric_id=code,
        version_no=1,
        status=status,
        description="范围说明",
        base_asset_id=asset,
        expression="SUM(b.value_a)",
        time_column="b.observed_at",
        timezone="UTC",
        dimensions=[{"name": "band", "label": "分组", "column": "b.band_id"}],
        joins=[],
        created_by="actor",
        **changes,
    )
    session.add_all([model, version])
    session.flush()
    return model, version


def avelis(item):
    return json.loads(next(e["data"] for e in item["custom_extensions"] if e["vendor_name"] == "avelis"))


def foreign(label):
    return {"vendor_name": "external_lab", "data": json.dumps({"marker": label})}


def validate_official_schema(document):
    """Validate exports directly against the unmodified pinned upstream schema."""
    schema = json.loads(files("avelis.interchange").joinpath("ossie_schema_48da55a.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(document)


def no_export(catalogue, metric_ids=None):
    with pytest.raises(GovernanceError, match="没有可导出的已发布口径") as caught:
        export_model(catalogue.session, catalogue.actor, "domain", metric_ids)
    assert caught.value.status_code == 400


def test_export_current_published_fields_context_and_source(catalogue):
    session = catalogue.session
    model, version = metric(
        catalogue, unit="件", unit_scale=1000, data_grain="month", value_kind="count", polarity="up_good"
    )
    session.add(
        MetricVersion(
            id="next",
            metric_id=model.id,
            version_no=2,
            status="draft",
            description="未发布",
            base_asset_id="records",
            expression="SUM(b.value_b)",
            time_column="b.observed_at",
            timezone="UTC",
            created_by="actor",
        )
    )
    metric(catalogue, "unpublished", status="draft")
    metric(catalogue, "affected", status="affected")
    metric(catalogue, "hidden_measure", domain="hidden", asset="private_records")
    session.flush()

    document = export_model(session, catalogue.actor, "domain")
    validate_official_schema(document)
    assert document["version"] == "0.2.0.dev0"
    assert document["name"] == "observations" and "semantic_model" not in document
    assert [m["name"] for m in document["metrics"]] == [model.code]
    item = document["metrics"][0]
    assert item["expression"]["dialects"] == [{"dialect": "postgres", "expression": "SUM(records.value_a)"}]
    assert item["ai_context"] == model.ai_context
    assert "datatype" not in item
    data = avelis(item)
    assert data["expression"] == version.expression and data["base_dataset"] == "records"
    assert data["unit"] == "件" and data["unit_scale"] == 1000 and data["data_grain"] == "month"
    assert data["value_kind"] == "count" and data["polarity"] == "up_good"
    dataset = document["datasets"][0]
    assert dataset["source"] == "catalog.public.records"
    fields = {f["name"]: f for f in dataset["fields"]}
    assert fields["observed_at"]["dimension"] == {"is_time": True}
    assert fields["band"]["dimension"] == {"is_time": False}
    assert fields["band"]["label"] == "分组"
    assert fields["band"]["expression"]["dialects"][0]["expression"] == '"band_id"'
    assert all("datatype" not in f for f in fields.values())
    assert export_model(session, catalogue.actor, "domain", [model.id])["metrics"] == document["metrics"]
    no_export(catalogue, ["hidden_measure"])


def test_export_denies_invisible_domain_and_does_not_expose_assets(catalogue):
    metric(catalogue, asset="private_records")
    no_export(catalogue)
    with pytest.raises(Forbidden):
        export_model(catalogue.session, catalogue.actor, "hidden")


def test_export_preserves_foreign_extensions_unknown_dialects_and_explicit_datatype(catalogue):
    _, version = metric(catalogue)
    original = {
        "version": "0.2.0.dev0",
        "name": "imported",
        "custom_extensions": [foreign("model"), foreign("model")],
        "ai_context": {"instructions": "模型提示", "extra": {"scope": "alpha"}},
        "datasets": [
            {
                "name": "observations_a",
                "source": "catalog.public.records",
                "custom_extensions": [foreign("dataset")],
                "fields": [
                    {
                        "name": "value_a",
                        "datatype": "Decimal",
                        "expression": {"dialects": [{"dialect": "OTHER_DIALECT", "expression": "RAW(value_a)"}]},
                        "custom_extensions": [foreign("field")],
                    }
                ],
            }
        ],
        "metrics": [
            {
                "name": "measure_a",
                "datatype": "Decimal",
                "ai_context": {"vendor_hint": {"scope": "alpha"}, "instructions": "旧说明"},
                "expression": {
                    "dialects": [
                        {"dialect": "postgres", "expression": "SUM(observations_a.value_a)"},
                        {"dialect": "POSTGRES", "expression": "KeEpCaSe()"},
                        {"dialect": "OTHER_DIALECT", "expression": "OpaqueExpression"},
                    ]
                },
                "custom_extensions": [
                    foreign("metric"),
                    {"vendor_name": "avelis", "data": '{"future_option": [1, 2]}'},
                ],
            }
        ],
    }
    version.publish_check = {
        "ossie": {
            "document": original,
            "metric_name": "measure_a",
            "dataset_name": None,
            "warnings": [],
            "unknown_dialects": [{"dialect": "OTHER_DIALECT", "expression": "OpaqueExpression"}],
        }
    }
    document = export_model(catalogue.session, catalogue.actor, "domain")
    validate_official_schema(document)
    assert document["custom_extensions"] == [foreign("model"), foreign("model")]
    assert document["ai_context"] == original["ai_context"]
    dataset = document["datasets"][0]
    assert dataset["name"] == "observations_a" and dataset["custom_extensions"] == [foreign("dataset")]
    field = next(f for f in dataset["fields"] if f["name"] == "value_a")
    assert field == original["datasets"][0]["fields"][0]
    item = document["metrics"][0]
    assert item["datatype"] == "Decimal" and item["custom_extensions"][0] == foreign("metric")
    assert item["ai_context"]["vendor_hint"] == {"scope": "alpha"}
    assert item["ai_context"]["instructions"] == "先核对观察范围"
    assert item["expression"]["dialects"] == [
        {"dialect": "postgres", "expression": "SUM(observations_a.value_a__column)"},
        *original["metrics"][0]["expression"]["dialects"][1:],
    ]
    assert avelis(item)["original_expression"] == original["metrics"][0]["expression"]
    assert avelis(item)["future_option"] == [1, 2]
    assert avelis(item)["unknown_dialects"] == [{"dialect": "OTHER_DIALECT", "expression": "OpaqueExpression"}]
    assert avelis(item)["base_dataset"] == "observations_a"
    assert version.publish_check["ossie"]["document"] == original


@pytest.mark.parametrize(
    ("column_name", "field_sql", "occupied", "identity_name"),
    [
        ("value_a", "value_a * 2", False, "value_a__column"),
        ("value_a", "value_a", False, None),
        ("value_a", "records.value_a", False, None),
        ("value_a", "VALUE_A", False, None),
        ("value_a", "value_a * 2", True, "value_a__column_2"),
        ("value a", '"value a" * 2', False, "value_a__column"),
        ("a²", '"a²" * 2', False, "a__column"),
        (
            "value_a",
            [
                {"dialect": "ANSI_SQL", "expression": "value_a * 2"},
                {"dialect": "OSSIE_SQL_2026", "expression": "value_a"},
            ],
            False,
            "value_a__column",
        ),
        (
            "value_a",
            [
                {"dialect": "OSSIE_SQL_2026", "expression": "value_a"},
                {"dialect": "ANSI_SQL", "expression": "value_a * 2"},
            ],
            False,
            None,
        ),
        (
            "value_a",
            [
                {"dialect": "ANSI_SQL", "expression": "value_a * 2"},
                {"dialect": "postgres", "expression": "value_a"},
            ],
            False,
            None,
        ),
        (
            "value_a",
            [
                {"dialect": "OSSIE_SQL_2026", "expression": "value_a"},
                {"dialect": "postgres", "expression": "value_a * 2"},
            ],
            False,
            "value_a__column",
        ),
    ],
)
def test_export_import_preserves_physical_column_semantics(catalogue, column_name, field_sql, occupied, identity_name):
    session = catalogue.session
    session.scalar(select(DomainMember).where(DomainMember.domain_id == "domain")).role = "maintainer"
    _, version = metric(catalogue)
    column = session.scalar(select(ColumnVersion).where(ColumnVersion.column_id == "records-value_a"))
    column.name = column_name
    dialects = field_sql if isinstance(field_sql, list) else [{"dialect": "postgres", "expression": field_sql}]
    field = {"name": column_name, "expression": {"dialects": dialects}}
    fields = [
        field,
        {"name": "observed_at", "expression": {"sql": "observed_at"}, "dimension": {"is_time": True}},
    ]
    if occupied:
        fields.append({"name": "VALUE_A__COLUMN", "expression": {"sql": "value_b"}})
    reference = sqlglot.exp.column(column_name, table="records", quoted=not column_name.isidentifier()).sql(
        dialect="postgres"
    )
    source = {
        "version": "0.2.0.dev0",
        "name": "logical_fields",
        "datasets": [{"name": "records", "source": "catalog.public.records", "fields": fields}],
        "metrics": [
            {
                "name": "measure_a",
                "expression": {"dialects": [{"dialect": "postgres", "expression": f"SUM({reference})"}]},
            }
        ],
    }
    report = import_model(session, catalogue.actor, source, "domain", dry_run=False)
    imported = session.get(MetricVersion, report["results"][0]["version_id"])
    imported.status = "published"
    version.status = "archived"
    original_expression = imported.expression

    for _ in range(2):
        exported = export_model(session, catalogue.actor, "domain")
        validate_official_schema(exported)
        exported_fields = {item["name"]: item for item in exported["datasets"][0]["fields"]}
        assert exported_fields[column_name] == field
        if identity_name:
            assert identity_name.isidentifier()
            assert exported_fields[identity_name]["expression"]["dialects"] == [
                {
                    "dialect": "postgres",
                    "expression": sqlglot.exp.column(column_name, quoted=True).sql(dialect="postgres"),
                }
            ]
            assert len([name for name in exported_fields if "__column" in name.casefold()]) == 1 + occupied
            expression = exported["metrics"][0]["expression"]["dialects"][0]["expression"]
            assert (
                next(sqlglot.parse_one(expression, read="postgres").find_all(sqlglot.exp.Column)).name == identity_name
            )
        else:
            assert all("__column" not in name for name in exported_fields)
        report = import_model(session, catalogue.actor, exported, "domain", dry_run=False)
        roundtripped = session.get(MetricVersion, report["results"][0]["version_id"])
        before = sqlglot.parse_one(original_expression, read="postgres")
        after = sqlglot.parse_one(roundtripped.expression, read="postgres")
        for tree in (before, after):
            for identifier in tree.find_all(sqlglot.exp.Identifier):
                identifier.set("quoted", False)
        assert after == before
        imported.status = "archived"
        roundtripped.status = "published"
        imported = roundtripped


def test_export_requires_actual_join_approvals_and_preserves_relationship_metadata(catalogue):
    session = catalogue.session
    _, version = metric(catalogue)
    path = JoinPath(
        id="path",
        code="records_bands",
        name="关联甲",
        left_asset_id="records",
        left_columns=["band_id"],
        right_asset_id="bands",
        right_columns=["band_id"],
        cardinality="n1",
        status="approved",
        created_by="actor",
    )
    session.add(path)
    version.joins = [{"join_path_id": "path", "alias": "band_ref"}]
    session.flush()
    no_export(catalogue)
    session.add(JoinPathApproval(join_path_id="path", domain_id="domain", user_id="actor", decision="approved"))
    version.publish_check = {
        "ossie": {
            "document": {
                "datasets": [
                    {"name": "records", "source": "catalog.public.records"},
                    {"name": "bands", "source": "catalog.public.bands"},
                ],
                "relationships": [
                    {
                        "name": "records_bands",
                        "from": "records",
                        "to": "bands",
                        "from_columns": ["band_id"],
                        "to_columns": ["band_id"],
                        "custom_extensions": [foreign("relationship")],
                    }
                ],
            },
            "metric_name": "measure_a",
        }
    }
    session.flush()
    document = export_model(session, catalogue.actor, "domain")
    validate_official_schema(document)
    assert document["relationships"] == [
        {
            "name": "records_bands",
            "from": "records",
            "to": "bands",
            "from_columns": ["band_id"],
            "to_columns": ["band_id"],
            "custom_extensions": [foreign("relationship")],
        }
    ]
    assert avelis(document["metrics"][0])["joins"] == [
        {"relationship": "records_bands", "from": "b", "alias": "band_ref", "pre_aggregate": False}
    ]
    path.status = "affected"
    session.flush()
    no_export(catalogue)


def test_export_merges_foreign_field_metadata_from_separate_imports(catalogue):
    _, first = metric(catalogue, "measure_a")
    _, second = metric(catalogue, "measure_b")
    document = {
        "datasets": [
            {
                "name": "records",
                "source": "catalog.public.records",
                "fields": [
                    {
                        "name": "value_a",
                        "expression": {"dialects": [{"dialect": "postgres", "expression": "value_a"}]},
                        "custom_extensions": [foreign("first_field")],
                    }
                ],
            }
        ]
    }
    first.publish_check = {"ossie": {"document": document}}
    other = deepcopy(document)
    other["datasets"][0]["fields"][0]["custom_extensions"] = [foreign("second_field")]
    second.publish_check = {"ossie": {"document": other}}
    exported = export_model(catalogue.session, catalogue.actor, "domain")
    field = next(f for f in exported["datasets"][0]["fields"] if f["name"] == "value_a")
    assert field["custom_extensions"] == [foreign("first_field"), foreign("second_field")]


def test_export_renames_conflicting_datasets_without_discarding_metadata(catalogue):
    _, first = metric(catalogue, "measure_a")
    _, second = metric(catalogue, "measure_b", asset="bands")
    first.publish_check = {
        "ossie": {
            "document": {
                "datasets": [
                    {"name": "shared", "source": "catalog.public.records", "custom_extensions": [foreign("first")]}
                ]
            }
        }
    }
    second.publish_check = {
        "ossie": {
            "document": {
                "datasets": [
                    {"name": "shared", "source": "catalog.public.bands", "custom_extensions": [foreign("second")]}
                ]
            }
        }
    }
    document = export_model(catalogue.session, catalogue.actor, "domain")
    validate_official_schema(document)
    assert [item["name"] for item in document["datasets"]] == ["shared", "shared_2"]
    assert [item["custom_extensions"] for item in document["datasets"]] == [[foreign("first")], [foreign("second")]]
    document = export_model(catalogue.session, catalogue.actor, "domain", ["measure_b"])
    assert document["datasets"][0]["custom_extensions"] == [foreign("second")]


@pytest.mark.parametrize("transformed", [False, True])
def test_export_derived_pins_components_and_expands_only_shared_row_sets(catalogue, transformed):
    session = catalogue.session
    left, first = metric(catalogue, "measure_a")
    right, second = metric(catalogue, "measure_b")
    if transformed:
        first.expression = "SUM(b.value_a * 2)"
        first.publish_check = {
            "ossie": {
                "document": {
                    "datasets": [
                        {
                            "name": "records",
                            "source": "catalog.public.records",
                            "fields": [{"name": "value_a", "expression": {"sql": "value_a * 2"}}],
                        }
                    ]
                }
            }
        }
    second.expression = "COUNT(b.value_b)"
    _, derived = metric(catalogue, "ratio_a", kind="derived")
    derived.base_asset_id = None
    derived.expression = "measure_a / measure_b"
    derived.components = [
        {"metric_code": left.code, "version_id": first.id, "version_no": first.version_no},
        {"metric_code": right.code, "version_id": second.id, "version_no": second.version_no},
    ]
    document = export_model(session, catalogue.actor, "domain", ["ratio_a"])
    validate_official_schema(document)
    assert [m["name"] for m in document["metrics"]] == ["ratio_a"]
    item = document["metrics"][0]
    text = item["expression"]["dialects"][0]["expression"]
    physical = "value_a__column" if transformed else "value_a"
    assert f"SUM(records.{physical}{' * 2' if transformed else ''})" in text
    assert "COUNT(records.value_b)" in text and "NULLIF" in text
    sqlite_expression = sqlglot.transpile(text, read="postgres", write="sqlite")[0]
    assert session.execute(
        sql_text(
            f"WITH records({physical}, value_b) AS (VALUES (1, 1), (0, 1)) SELECT {sqlite_expression} FROM records"
        )
    ).scalar_one() == (1.0 if transformed else 0.5)
    data = avelis(item)
    assert data["formula"] == "measure_a / measure_b" and data["expression"] == derived.expression
    assert data["components"] == [
        {"metric_code": "measure_a", "version_no": 1, "expression": first.expression},
        {"metric_code": "measure_b", "version_no": 1, "expression": second.expression},
    ]
    first.filters = "b.band_id = 1"
    with pytest.raises(GovernanceError, match="组成口径不共享相同的行集"):
        export_model(session, catalogue.actor, "domain", ["ratio_a"])
    right.domain_id = "hidden"
    session.flush()
    no_export(catalogue, ["ratio_a"])


def approved_path(catalogue, right="bands"):
    path = JoinPath(
        id="path",
        code="records_relation",
        name="关联甲",
        left_asset_id="records",
        left_columns=["band_id"],
        right_asset_id=right,
        right_columns=["band_id"],
        cardinality="n1",
        status="approved",
        created_by="actor",
    )
    catalogue.session.add_all(
        [path, JoinPathApproval(join_path_id="path", domain_id="domain", user_id="actor", decision="approved")]
    )
    catalogue.session.flush()
    return path


def test_export_import_preserves_physical_join_keys_with_transformed_logical_fields(catalogue):
    session = catalogue.session
    session.scalar(select(DomainMember).where(DomainMember.domain_id == "domain")).role = "maintainer"
    _, version = metric(catalogue)
    approved_path(catalogue)
    version.joins = [{"join_path_id": "path", "alias": "r"}]
    version.expression = "SUM(r.value_a * 2)"
    version.publish_check = {
        "ossie": {
            "document": {
                "datasets": [
                    {
                        "name": name,
                        "source": f"catalog.public.{name}",
                        "fields": [
                            {"name": column, "expression": {"sql": f"{column} * 2"}}
                            for column in ("band_id", "value_a")
                        ],
                    }
                    for name in ("records", "bands")
                ]
            }
        }
    }
    exported = export_model(session, catalogue.actor, "domain")
    validate_official_schema(exported)
    relationship = exported["relationships"][0]
    assert relationship["from_columns"] == relationship["to_columns"] == ["band_id__column"]
    assert exported["metrics"][0]["expression"]["dialects"][0]["expression"] == "SUM(bands.value_a__column * 2)"
    report = import_model(session, catalogue.actor, exported, "domain", dry_run=False)
    roundtripped = session.get(MetricVersion, report["results"][0]["version_id"])
    assert roundtripped.expression == 'SUM(r."value_a" * 2)'
    assert [(join["join_path_id"], join["alias"]) for join in roundtripped.joins] == [("path", "r")]


@pytest.mark.parametrize("portable", ["postgres", "ANSI_SQL", "OSSIE_SQL_2026", "sql"])
@pytest.mark.parametrize("reverse", [False, True])
def test_export_preserves_relationship_extensions_for_logical_join_fields(catalogue, portable, reverse):
    _, version = metric(catalogue)
    approved_path(catalogue)
    version.joins = [{"join_path_id": "path", "alias": "r"}]
    relationship = {
        "name": "imported_relation",
        "from": "records",
        "to": "bands",
        "from_columns": ["slot_key"],
        "to_columns": ["band_key"],
        "custom_extensions": [foreign("logical_relation")],
    }
    if reverse:
        relationship.update(
            {"from": "bands", "to": "records", "from_columns": ["band_key"], "to_columns": ["slot_key"]}
        )
    version.publish_check = {
        "ossie": {
            "document": {
                "datasets": [
                    {
                        "name": "records",
                        "source": "catalog.public.records",
                        "fields": [
                            {
                                "name": "slot_key",
                                "expression": (
                                    {"sql": "band_id"}
                                    if portable == "sql"
                                    else {"dialects": [{"dialect": portable, "expression": "records.band_id"}]}
                                ),
                            }
                        ],
                    },
                    {
                        "name": "bands",
                        "source": "catalog.public.bands",
                        "fields": [
                            {
                                "name": "band_key",
                                "expression": (
                                    {"sql": "band_id"}
                                    if portable == "sql"
                                    else {"dialects": [{"dialect": portable, "expression": "band_id"}]}
                                ),
                            }
                        ],
                    },
                ],
                "relationships": [relationship],
            }
        }
    }
    document = export_model(catalogue.session, catalogue.actor, "domain")
    validate_official_schema(document)
    assert document["relationships"] == [
        {
            "name": "records_relation",
            "from": "records",
            "to": "bands",
            "from_columns": ["band_id"],
            "to_columns": ["band_id"],
            "custom_extensions": [foreign("logical_relation")],
        }
    ]


def test_export_refuses_preaggregate_dependencies_of_selected_derived_metric(catalogue):
    left, first = metric(catalogue, "measure_a")
    right, second = metric(catalogue, "measure_b")
    approved_path(catalogue)
    first.joins = [{"join_path_id": "path", "alias": "r", "pre_aggregate": {"row_count": "COUNT(*)"}}]
    second.joins = deepcopy(first.joins)
    first.expression = "SUM(r.row_count)"
    second.expression = "COUNT(b.value_b)"
    _, derived = metric(catalogue, "ratio_a", kind="derived")
    derived.base_asset_id = None
    derived.expression = "measure_a / measure_b"
    derived.components = [
        {"metric_code": left.code, "version_id": first.id, "version_no": first.version_no},
        {"metric_code": right.code, "version_id": second.id, "version_no": second.version_no},
    ]
    with pytest.raises(GovernanceError, match="预聚合关联无法在 Ossie 核心模型中等价表达"):
        export_model(catalogue.session, catalogue.actor, "domain", ["ratio_a"])


@pytest.mark.parametrize("expression", ["SUM(r.row_count)", "COUNT(r.band_id)", "SUM(b.value_a)"])
def test_export_refuses_every_preaggregate_join_even_with_only_physical_or_unused_columns(catalogue, expression):
    _, version = metric(catalogue)
    approved_path(catalogue)
    version.joins = [{"join_path_id": "path", "alias": "r", "pre_aggregate": True}]
    version.expression = expression
    with pytest.raises(GovernanceError, match="预聚合关联无法在 Ossie 核心模型中等价表达") as caught:
        export_model(catalogue.session, catalogue.actor, "domain", ["measure_a"])
    assert caught.value.status_code == 400
    version.joins = [{"join_path_id": "path", "alias": "r", "pre_aggregate": False}]
    version.expression = "COUNT(r.band_id)"
    document = export_model(catalogue.session, catalogue.actor, "domain", ["measure_a"])
    validate_official_schema(document)
    assert document["metrics"][0]["expression"]["dialects"] == [
        {"dialect": "postgres", "expression": "COUNT(bands.band_id)"}
    ]


def test_export_dataset_metric_promotes_portable_sql_and_keeps_opaque_dialects(catalogue):
    _, version = metric(catalogue)
    original = {
        "name": "measure_a",
        "expression": {
            "sql": "SUM(value_a)",
            "dialects": [
                {"dialect": "OSSIE_SQL_2026", "expression": "SUM(value_b)"},
                {"dialect": "ANSI_SQL", "expression": "SUM(value_a)"},
                {"dialect": "CustomLanguage", "expression": "TOTAL [value_a]"},
            ],
        },
    }
    version.publish_check = {
        "ossie": {
            "dataset_name": "records",
            "metric_name": "measure_a",
            "document": {"datasets": [{"name": "records", "source": "catalog.public.records", "metrics": [original]}]},
        }
    }
    document = export_model(catalogue.session, catalogue.actor, "domain")
    validate_official_schema(document)
    assert "metrics" not in document["datasets"][0]
    expression = document["metrics"][0]["expression"]
    assert expression["sql"] == "SUM(records.value_a)"
    assert expression["dialects"] == [
        {"dialect": "postgres", "expression": "SUM(records.value_a)"},
        {"dialect": "ANSI_SQL", "expression": "SUM(records.value_a)"},
        {"dialect": "CustomLanguage", "expression": "TOTAL [value_a]"},
    ]
    assert avelis(document["metrics"][0])["original_expression"] == original["expression"]
    assert original["expression"]["sql"] == "SUM(value_a)"


@pytest.mark.parametrize("dataset_scope", [False, True])
@pytest.mark.parametrize("edited", [False, True])
@pytest.mark.parametrize("unparseable", [False, True])
def test_export_keeps_portable_expression_only_when_current_native_expression_matches(
    catalogue, dataset_scope, edited, unparseable
):
    _, version = metric(catalogue)
    sql = "SUM(value_a)" if dataset_scope else "SUM(records.value_a)"
    if unparseable:
        sql = "SUM("
    original = {
        "name": "measure_a",
        "expression": {
            "sql": sql,
            "dialects": [
                {"dialect": "ANSI_SQL", "expression": sql},
                {
                    "dialect": "OSSIE_SQL_2026",
                    "expression": "SUM(value_b)" if dataset_scope else "SUM(records.value_b)",
                },
                {"dialect": "CustomLanguage", "expression": "TOTAL [value_a]"},
            ],
        },
    }
    source = {"name": "records", "source": "catalog.public.records"}
    document = {"datasets": [source]}
    (source if dataset_scope else document)["metrics"] = [original]
    version.publish_check = {
        "ossie": {
            "dataset_name": "records" if dataset_scope else None,
            "metric_name": "measure_a",
            "document": document,
        }
    }
    if edited:
        version.expression = "SUM(b.value_b)"
    exported = export_model(catalogue.session, catalogue.actor, "domain")
    validate_official_schema(exported)
    item = exported["metrics"][0]
    assert item["expression"]["dialects"][0] == {
        "dialect": "postgres",
        "expression": "SUM(records.value_b)" if edited else "SUM(records.value_a)",
    }
    assert item["expression"]["dialects"][-1] == {"dialect": "CustomLanguage", "expression": "TOTAL [value_a]"}
    assert all(entry["dialect"] != "OSSIE_SQL_2026" for entry in item["expression"]["dialects"])
    if edited or unparseable:
        assert "sql" not in item["expression"]
        assert all(entry["dialect"] != "ANSI_SQL" for entry in item["expression"]["dialects"])
        assert avelis(item)["original_expression"] == original["expression"]
    else:
        assert item["expression"]["sql"] == "SUM(records.value_a)"
        assert item["expression"]["dialects"][1] == {"dialect": "ANSI_SQL", "expression": "SUM(records.value_a)"}
    assert document == version.publish_check["ossie"]["document"]


def test_export_rejects_saved_metadata_that_breaks_the_current_official_schema(catalogue):
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
                                "name": "",
                                "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "value_a"}]},
                            }
                        ],
                    }
                ]
            }
        }
    }
    with pytest.raises(GovernanceError) as caught:
        export_model(catalogue.session, catalogue.actor, "domain")
    assert "$.datasets[0].fields[0].name" in caught.value.detail["paths"]


@pytest.mark.parametrize(
    ("datatype", "dimension", "expected"),
    [("Date", {}, True), ("DateTimeTz", {"is_time": False}, False), ("Integer", {"is_time": True}, True)],
)
def test_export_temporal_role_is_independent_of_datatype(catalogue, datatype, dimension, expected):
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
                                "name": "band",
                                "expression": {"sql": "band_id"},
                                "datatype": datatype,
                                "dimension": dimension,
                            }
                        ],
                    }
                ]
            }
        }
    }
    document = export_model(catalogue.session, catalogue.actor, "domain")
    validate_official_schema(document)
    field = next(f for f in document["datasets"][0]["fields"] if f["name"] == "band")
    assert field["datatype"] == datatype
    assert field["dimension"] == {"is_time": expected}


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ({"primary_key": [], "unique_keys": [[]]}, {}),
        (
            {"primary_key": ["band_id"], "unique_keys": [[], ["value_a", "value_b"]]},
            {"primary_key": ["band_id"], "unique_keys": [["value_a", "value_b"]]},
        ),
    ],
)
def test_export_key_definitions_have_at_least_one_column(catalogue, keys, expected):
    _, version = metric(catalogue)
    version.publish_check = {
        "ossie": {"document": {"datasets": [{"name": "records", "source": "catalog.public.records", **keys}]}}
    }
    document = export_model(catalogue.session, catalogue.actor, "domain")
    validate_official_schema(document)
    dataset = document["datasets"][0]
    assert {key: dataset[key] for key in ("primary_key", "unique_keys") if key in dataset} == expected


@pytest.mark.parametrize("right", ["records", "bands"])
def test_export_refuses_ambiguous_self_join_or_repeated_join_roles(catalogue, right):
    _, version = metric(catalogue)
    approved_path(catalogue, right=right)
    version.joins = [{"join_path_id": "path", "alias": "r"}]
    if right == "bands":
        version.joins.append({"join_path_id": "path", "alias": "r_again"})
    with pytest.raises(GovernanceError, match="同一资产的多个关联角色") as caught:
        export_model(catalogue.session, catalogue.actor, "domain")
    assert caught.value.status_code == 400
