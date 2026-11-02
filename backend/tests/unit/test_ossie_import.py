"""Ossie 导入在真实元数据会话中的校验、权限、草稿与事务边界。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from sqlalchemy import JSON, MetaData, create_engine, event, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from avelis import cache
from avelis.db import Base
from avelis.governance import access, metrics
from avelis.governance.errors import GovernanceError
from avelis.interchange.exporter import export_model
from avelis.interchange.importer import import_model
from avelis.interchange.validation import validate_structure
from avelis.models import (
    Asset,
    AssetColumn,
    AssetVersion,
    AuditEvent,
    CollectionRun,
    ColumnFingerprint,
    ColumnVersion,
    Datasource,
    Domain,
    DomainMember,
    Grain,
    JoinPath,
    JoinPathApproval,
    Metric,
    MetricVersion,
    MetricVersionTransition,
    User,
    UserRole,
)

CONTEXT = {"instructions": "用于观察范围甲", "synonyms": ["合成词甲"], "examples": ["观察序列甲"]}
THIRD_PARTY = {"vendor_name": "other_system", "data": '{"display":{"precision":3},"unknown":[1,"x"]}'}


def extension(**data):
    return {"vendor_name": "avelis", "data": json.dumps(data, ensure_ascii=False)}


def document(code="metric_a", *, dialect="postgres", expression="SUM(records.value_a)"):
    return {
        "version": "0.2.0.dev0",
        "name": "scope_a",
        "datasets": [
            {
                "name": "records",
                "source": "catalog.schema_a.records",
                "fields": [
                    {"name": "value_a", "expression": {"dialects": [{"dialect": "postgres", "expression": "value_a"}]}},
                    {
                        "name": "observed_on",
                        "expression": {"dialects": [{"dialect": "postgres", "expression": "observed_on"}]},
                        "dimension": {"is_time": True},
                    },
                    {
                        "name": "group_a",
                        "expression": {"dialects": [{"dialect": "postgres", "expression": "group_a"}]},
                        "dimension": {"is_time": False},
                    },
                ],
            }
        ],
        "metrics": [
            {
                "name": code,
                "description": "合成指标甲",
                "expression": {"dialects": [{"dialect": dialect, "expression": expression}]},
                "ai_context": deepcopy(CONTEXT),
                "custom_extensions": [
                    deepcopy(THIRD_PARTY),
                    extension(
                        name="指标甲",
                        base_dataset="records",
                        timezone="UTC",
                        time_column="b.observed_on",
                        unit="单位甲",
                        data_grain="day",
                        time_aggregation="sum",
                        value_kind="level",
                        polarity="up_good",
                        dimensions=[{"name": "group_a", "label": "分组甲", "column": "b.group_a"}],
                    ),
                ],
            }
        ],
    }


def legacy_document():
    model = document(dialect="ANSI_SQL")
    version = model.pop("version")
    for field in model["datasets"][0]["fields"]:
        field["expression"]["dialects"][0]["dialect"] = "ANSI_SQL"
    return {"version": version, "semantic_model": [model]}


@pytest.fixture
def catalog(monkeypatch):
    # 只替换外部缓存副作用，元数据库、依赖计算与审核状态机均走真实实现。
    monkeypatch.setattr(cache, "invalidate_tag", lambda _: 0)
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for original in Base.metadata.tables.values():
        table = original.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        users = {
            name: User(id=name, username=name, display_name=name, password_hash="unused")
            for name in ("maintainer", "reader", "reviewer", "outsider")
        }
        session.add_all(users.values())
        session.add_all(
            [
                Domain(id="visible", code="scope_a", name="范围甲", owner_id="maintainer"),
                Domain(id="other", code="scope_b", name="范围乙", owner_id="outsider"),
                Domain(id="hidden", code="scope_c", name="保密范围", owner_id="outsider"),
                DomainMember(user_id="maintainer", domain_id="visible", role="maintainer"),
                DomainMember(user_id="maintainer", domain_id="other", role="maintainer"),
                DomainMember(user_id="reader", domain_id="visible", role="viewer"),
                DomainMember(user_id="reviewer", domain_id="visible", role="viewer"),
                UserRole(user_id="maintainer", role="steward"),
                UserRole(user_id="reader", role="steward"),
                UserRole(user_id="reviewer", role="reviewer"),
                Datasource(id="source", name="离线目录", kind="postgres", host="offline", port=1),
                CollectionRun(id="run", datasource_id="source", trigger="manual", status="done"),
            ]
        )
        for asset_id, name, domain_id in (
            ("asset", "records", "visible"),
            ("asset_b", "records_b", "visible"),
            ("hidden_asset", "records_c", "hidden"),
        ):
            version_id = f"{asset_id}_v1"
            session.add_all(
                [
                    Asset(
                        id=asset_id,
                        datasource_id="source",
                        catalog_name="catalog",
                        db_name="schema_a",
                        name=name,
                        kind="table",
                        domain_id=domain_id,
                        current_version_id=version_id,
                    ),
                    AssetVersion(
                        id=version_id,
                        asset_id=asset_id,
                        version_no=1,
                        observed_run_id="run",
                        last_seen_run_id="run",
                        kind="table",
                        catalog_name="catalog",
                        db_name="schema_a",
                        name=name,
                        content_hash="a" * 64,
                    ),
                ]
            )
            for ordinal, (column, datatype) in enumerate(
                (("value_a", "numeric"), ("observed_on", "date"), ("group_a", "text")), start=1
            ):
                column_id = f"{asset_id}_{column}"
                session.add_all(
                    [
                        AssetColumn(id=column_id, asset_id=asset_id, name=column),
                        ColumnVersion(
                            asset_version_id=version_id,
                            column_id=column_id,
                            ordinal=ordinal,
                            name=column,
                            data_type=datatype,
                            comment="采集字段说明",
                        ),
                        ColumnFingerprint(asset_id=asset_id, column_name=column, fingerprint="f" * 64, status="ok"),
                    ]
                )
        session.commit()
        yield SimpleNamespace(session=session, users=users, actor=users["maintainer"])
    engine.dispose()


def versions(catalog):
    return list(catalog.session.scalars(select(MetricVersion).order_by(MetricVersion.version_no)))


def metric(catalog, code="metric_a"):
    return catalog.session.scalar(select(Metric).where(Metric.code == code))


def latest_version(catalog, code="metric_a"):
    return catalog.session.scalar(
        select(MetricVersion)
        .where(MetricVersion.metric_id == metric(catalog, code).id)
        .order_by(MetricVersion.version_no.desc())
        .limit(1)
    )


def publish(catalog, code="metric_a"):
    version = latest_version(catalog, code)
    metrics.submit(catalog.session, catalog.actor, version)
    metrics.approve(catalog.session, catalog.users["reviewer"], version, "确认")
    catalog.session.commit()
    return version


def snapshot(catalog):
    return {
        "metrics": [(m.id, m.code, deepcopy(m.ai_context)) for m in catalog.session.scalars(select(Metric))],
        "versions": [(v.id, v.status, v.expression) for v in versions(catalog)],
        "transitions": [t.id for t in catalog.session.scalars(select(MetricVersionTransition))],
        "audit": [e.id for e in catalog.session.scalars(select(AuditEvent))],
    }


def rejected(catalog, model, *, status=400, actor=None, domain_id="visible"):
    before = snapshot(catalog)
    with pytest.raises(GovernanceError) as caught:
        import_model(catalog.session, actor or catalog.actor, model, domain_id, dry_run=False)
    assert caught.value.status_code == status
    detail = caught.value.detail
    assert detail["ok"] is False and detail["dry_run"] is False
    assert detail["errors"]
    assert all(set(error) >= {"layer", "path", "reason"} for error in detail["errors"])
    assert set(detail) >= {"results", "warnings", "counts"}
    # 显式 flush 会暴露只留下 pending 对象、尚未真正回滚的失败。
    catalog.session.flush()
    assert snapshot(catalog) == before
    return detail


def test_dry_run_preserves_document_and_never_creates_metrics(catalog):
    model = document()
    before = deepcopy(model)
    report = import_model(catalog.session, catalog.actor, model, "visible")
    assert report["ok"] is True and report["dry_run"] is True
    assert report["errors"] == [] and len(report["results"]) == 1
    assert report["results"][0]["action"] in ("created", "skipped")
    assert list(catalog.session.scalars(select(Metric))) == []
    assert versions(catalog) == []
    assert list(catalog.session.scalars(select(MetricVersionTransition))) == []
    assert model == before


@pytest.mark.parametrize("dry_run", [True, False])
def test_legacy_envelope_import_warns_and_preserves_original_dialect(catalog, dry_run):
    model = legacy_document()
    before = deepcopy(model)
    report = import_model(catalog.session, catalog.actor, model, "visible", dry_run=dry_run)
    assert report["ok"] and report["dry_run"] is dry_run
    assert {"metric": "", "path": "$.semantic_model", "reason": "按旧版 Ossie 格式（88e0011）读取"} in report[
        "warnings"
    ]
    assert model == before
    if dry_run:
        assert versions(catalog) == []
    else:
        version = latest_version(catalog)
        assert version.status == "draft"
        retained = version.publish_check["ossie"]["document"]
        assert "semantic_model" not in retained
        assert retained["metrics"][0]["expression"]["dialects"][0]["dialect"] == "ANSI_SQL"
        assert retained["datasets"][0]["fields"][0]["expression"]["dialects"][0]["dialect"] == "ANSI_SQL"


@pytest.mark.parametrize("models", [0, 2])
def test_legacy_envelope_requires_exactly_one_semantic_model(catalog, models):
    model = legacy_document()
    model["semantic_model"] *= models
    report = rejected(catalog, model)
    assert any(error["layer"] == "structure" and "semantic_model" in error["path"] for error in report["errors"])


@pytest.mark.parametrize("incompatible", ["free_dialect", "dataset_metric", "empty_primary_key"])
def test_legacy_envelope_cannot_bypass_either_official_schema(catalog, incompatible):
    model = legacy_document()
    semantic = model["semantic_model"][0]
    if incompatible == "free_dialect":
        semantic["metrics"][0]["expression"]["dialects"][0]["dialect"] = "postgres"
    elif incompatible == "dataset_metric":
        semantic["datasets"][0]["metrics"] = semantic.pop("metrics")
    else:
        semantic["datasets"][0]["primary_key"] = []
    report = rejected(catalog, model)
    assert any(error["layer"] == "structure" for error in report["errors"])


@pytest.mark.parametrize(
    "filename", ["flights.semantic_model.yaml", "tpcds_semantic_model.yaml", "renamed_fields.yaml"]
)
def test_official_examples_reach_catalog_validation_and_reject_missing_assets(catalog, filename):
    fixture = Path(__file__).parents[1] / "fixtures" / "ossie" / filename
    model = yaml.safe_load(fixture.read_text(encoding="utf-8"))
    report = rejected(catalog, model)
    assert {error["layer"] for error in report["errors"]} == {"catalog"}
    assert all(error["path"].endswith(".source") for error in report["errors"])


@pytest.mark.parametrize("shadowed_dialect", [False, True])
def test_sql_expression_imports_without_dialects_and_overrides_matching_ossie_entry(catalog, shadowed_dialect):
    model = document()
    for field in model["datasets"][0]["fields"]:
        field["expression"] = {"sql": field["expression"]["dialects"][0]["expression"]}
    expression = {"sql": "SUM(records.value_a) * 2"}
    if shadowed_dialect:
        expression["dialects"] = [{"dialect": "OSSIE_SQL_2026", "expression": "SUM(absent.absent)"}]
    model["metrics"][0]["expression"] = expression
    report = import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
    assert report["ok"]
    version = latest_version(catalog)
    assert version.expression == "SUM(b.value_a) * 2"
    assert version.publish_check["ossie"]["document"]["metrics"][0]["expression"] == expression
    assert any("人工确认" in warning["reason"] for warning in report["warnings"])


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize(
    "reference,accepted",
    [("VALUE_A", True), ("VaLuE_a", True), ('"value_a"', True), ('"VALUE_A"', False)],
)
def test_portable_metric_identifiers_ignore_unquoted_case_and_match_quoted_case_exactly(
    catalog, scoped, reference, accepted
):
    model = document()
    model["metrics"][0]["expression"] = {"sql": f"SUM({reference if scoped else f'RECORDS.{reference}'})"}
    if scoped:
        model["datasets"][0]["metrics"] = model.pop("metrics")
    if not accepted:
        report = rejected(catalog, model)
        assert {error["layer"] for error in report["errors"]} == {"model"}
        return
    report = import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
    assert report["ok"] and latest_version(catalog).expression == "SUM(b.value_a)"


@pytest.mark.parametrize("dataset,accepted", [('"records"', True), ('"RECORDS"', False), ("ReCoRdS", True)])
def test_portable_metric_dataset_identifier_keeps_quoted_exact_matching(catalog, dataset, accepted):
    model = document()
    model["metrics"][0]["expression"] = {"sql": f"SUM({dataset}.value_a)"}
    if not accepted:
        report = rejected(catalog, model)
        assert {error["layer"] for error in report["errors"]} == {"model"}
        return
    assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
    assert latest_version(catalog).expression == "SUM(b.value_a)"


def test_portable_uppercase_dataset_reference_infers_base_among_multiple_datasets(catalog):
    model = document()
    second = deepcopy(model["datasets"][0])
    second.update(name="records_b", source="catalog.schema_a.records_b")
    model["datasets"].append(second)
    model["metrics"][0]["expression"] = {"sql": "SUM(RECORDS_B.VALUE_A)"}
    model["metrics"][0]["custom_extensions"] = [extension(timezone="UTC")]
    assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
    version = latest_version(catalog)
    assert version.base_asset_id == "asset_b" and version.expression == "SUM(b.value_a)"


@pytest.mark.parametrize("reference,accepted", [("VALUE_A", True), ('"value_a"', True), ('"VALUE_A"', False)])
def test_portable_field_source_identifier_uses_catalog_case_and_preserves_quote_semantics(catalog, reference, accepted):
    model = document()
    model["datasets"][0]["fields"][0]["expression"] = {"sql": reference}
    model["metrics"][0]["expression"] = {"sql": "SUM(records.value_a)"}
    if not accepted:
        report = rejected(catalog, model)
        assert {error["layer"] for error in report["errors"]} == {"catalog"}
        return
    assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
    version = latest_version(catalog)
    assert {dependency["column"] for dependency in version.dependency} == {"value_a", "observed_on", "group_a"}
    assert version.publish_check["ossie"]["document"]["datasets"][0]["fields"][0]["expression"] == {"sql": reference}


@pytest.mark.parametrize(
    "datatype,dimension,time_role",
    [
        ("String", {"is_time": True}, True),
        ("Date", {}, True),
        ("Date", None, True),
        ("Date", {"is_time": False}, False),
    ],
)
def test_time_role_is_independent_of_datatype_with_temporal_default(catalog, datatype, dimension, time_role):
    model = document()
    field = model["datasets"][0]["fields"][1]
    field["datatype"] = datatype
    if dimension is None:
        field.pop("dimension")
    else:
        field["dimension"] = dimension
    model["metrics"][0]["custom_extensions"] = [extension(base_dataset="records", timezone="UTC")]
    report = import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
    assert report["ok"]
    version = latest_version(catalog)
    assert version.time_column == ("b.observed_on" if time_role else "")
    assert ("observed_on" in {dim["name"] for dim in version.dimensions}) is (not time_role)
    assert version.publish_check["ossie"]["document"]["datasets"][0]["fields"][1] == field


def test_import_creates_only_draft_with_dependency_and_context_audit(catalog):
    report = import_model(catalog.session, catalog.actor, document(), "visible", dry_run=False)
    assert report["ok"] and not report["dry_run"]
    assert report["results"][0]["action"] == "created"
    created = metric(catalog)
    version = versions(catalog)[0]
    assert created.ai_context == CONTEXT and created.name == "指标甲"
    assert version.status == "draft" and version.version_no == 1 and version.published_at is None
    assert version.expression == "SUM(b.value_a)"
    assert version.time_column == "b.observed_on" and version.base_asset_id == "asset"
    assert version.unit == "单位甲" and version.value_kind == "level" and version.polarity == "up_good"
    assert version.timezone == "UTC" and version.dimensions[0]["column"] == "b.group_a"
    assert {(d["column"], d["fingerprint"], d["status"]) for d in version.dependency} == {
        (column, "f" * 64, "ok") for column in ("value_a", "observed_on", "group_a")
    }
    assert version.dependency_fingerprint == metrics.dependency_fingerprint(version, version.dependency)
    transitions = list(catalog.session.scalars(select(MetricVersionTransition)))
    assert [transition.to_status for transition in transitions] == ["draft"]
    events = list(catalog.session.scalars(select(AuditEvent)))
    assert {event.action for event in events} >= {"interchange.ossie_import", "metric.ai_context"}
    audit = next(event for event in events if event.action == "interchange.ossie_import")
    assert audit.actor_id == catalog.actor.id
    summary = json.dumps(audit.detail, ensure_ascii=False)
    assert "SUM(" not in summary and CONTEXT["instructions"] not in summary
    # import 没有批准入口；现有批准流程也不能跳过提交。
    with pytest.raises(GovernanceError) as caught:
        metrics.approve(catalog.session, catalog.users["reviewer"], version, "复核")
    assert caught.value.status_code == 409 and version.status == "draft"


def test_existing_metric_gets_new_draft_without_overwriting_published_version(catalog):
    import_model(catalog.session, catalog.actor, document(), "visible", dry_run=False)
    first = versions(catalog)[0]
    metrics.submit(catalog.session, catalog.actor, first)
    metrics.approve(catalog.session, catalog.users["reviewer"], first, "确认")
    catalog.session.commit()
    model = document(expression="SUM(records.value_a) * 2")
    report = import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
    assert report["results"][0]["action"] == "new_version"
    first, second = versions(catalog)
    assert first.status == "published" and first.expression == "SUM(b.value_a)" and first.version_no == 1
    assert second.status == "draft" and second.expression == "SUM(b.value_a) * 2" and second.version_no == 2
    assert len(list(catalog.session.scalars(select(Metric)))) == 1


def test_dry_run_existing_metric_does_not_update_context(catalog):
    import_model(catalog.session, catalog.actor, document(), "visible", dry_run=False)
    catalog.session.commit()
    before = snapshot(catalog)
    model = document()
    model["metrics"][0]["ai_context"] = {"instructions": "变更说明"}
    report = import_model(catalog.session, catalog.actor, model, "visible", dry_run=True)
    assert report["ok"]
    after = snapshot(catalog)
    assert {k: v for k, v in after.items() if k != "audit"} == {k: v for k, v in before.items() if k != "audit"}


def test_existing_code_in_another_domain_cannot_be_overwritten(catalog):
    import_model(catalog.session, catalog.actor, document(), "visible", dry_run=False)
    catalog.session.commit()
    detail = rejected(catalog, document(), domain_id="other")
    assert any("编码" in error["reason"] or "业务域" in error["reason"] for error in detail["errors"])
    assert metric(catalog).domain_id == "visible"


@pytest.mark.parametrize("existing", [False, True])
def test_viewer_cannot_create_or_add_version_even_with_global_steward_role(catalog, existing):
    if existing:
        import_model(catalog.session, catalog.actor, document(), "visible", dry_run=False)
        catalog.session.commit()
    rejected(catalog, document(), status=403, actor=catalog.users["reader"])


@pytest.mark.parametrize(
    "scenario,layer",
    [
        ("structure", "structure"),
        ("dataset_duplicate", "model"),
        ("field_duplicate", "model"),
        ("metric_duplicate", "model"),
        ("relationship_width", "model"),
        ("relationship_endpoint", "model"),
        ("metric_dataset", "model"),
        ("metric_field", "model"),
        ("missing_source", "catalog"),
        ("missing_column", "catalog"),
    ],
)
def test_each_validation_layer_rejects_whole_import_without_writes(catalog, scenario, layer):
    model = document()
    if scenario == "structure":
        del model["metrics"][0]["expression"]
    elif scenario == "dataset_duplicate":
        model["datasets"].append(deepcopy(model["datasets"][0]))
    elif scenario == "field_duplicate":
        model["datasets"][0]["fields"].append(deepcopy(model["datasets"][0]["fields"][0]))
    elif scenario == "metric_duplicate":
        model["metrics"].append(deepcopy(model["metrics"][0]))
    elif scenario.startswith("relationship_"):
        model["relationships"] = [
            {
                "name": "path_a",
                "from": "records",
                "to": "absent" if scenario == "relationship_endpoint" else "records",
                "from_columns": ["group_a", "observed_on"] if scenario == "relationship_width" else ["group_a"],
                "to_columns": ["group_a"],
            }
        ]
    elif scenario == "metric_dataset":
        model["metrics"][0]["expression"]["dialects"][0]["expression"] = "SUM(absent.value_a)"
    elif scenario == "metric_field":
        model["metrics"][0]["expression"]["dialects"][0]["expression"] = "SUM(records.absent)"
    elif scenario == "missing_source":
        model["datasets"][0]["source"] = "catalog.schema_a.absent"
    else:
        model["datasets"][0]["fields"].append(
            {"name": "absent", "expression": {"dialects": [{"dialect": "postgres", "expression": "absent"}]}}
        )
    detail = rejected(catalog, model)
    assert any(error["layer"] == layer for error in detail["errors"])


def test_hidden_source_is_indistinguishable_from_missing_source(catalog):
    model = document()
    model["datasets"][0]["source"] = "catalog.schema_a.records_c"
    hidden = rejected(catalog, model)
    model["datasets"][0]["source"] = "catalog.schema_a.absent"
    missing = rejected(catalog, model)
    assert [error["layer"] for error in hidden["errors"]] == [error["layer"] for error in missing["errors"]]
    serialized = json.dumps(hidden, ensure_ascii=False)
    assert "保密范围" not in serialized and "hidden_asset" not in serialized and "outsider" not in serialized


def test_later_invalid_metric_rolls_back_earlier_metric_context_and_audit(catalog):
    model = document()
    second = deepcopy(model["metrics"][0])
    second["name"] = "metric_b"
    second["custom_extensions"] = [extension(base_dataset="records", data_grain="invalid_grain")]
    model["metrics"].append(second)
    rejected(catalog, model)


def test_later_invalid_context_does_not_leave_earlier_metric_or_audit(catalog):
    model = document()
    second = deepcopy(model["metrics"][0])
    second["name"] = "metric_b"
    second["ai_context"] = {"instructions": "字" * 2001}
    model["metrics"].append(second)
    rejected(catalog, model)


@pytest.mark.parametrize("existing", [False, True])
def test_later_database_write_failure_rolls_back_versions_context_and_audit(catalog, existing):
    if existing:
        import_model(catalog.session, catalog.actor, document(), "visible", dry_run=False)
        catalog.session.commit()
    model = document()
    model["metrics"][0]["ai_context"] = {"instructions": "本次导入的新提示"}
    second = deepcopy(model["metrics"][0])
    second["name"] = "metric_b"
    model["metrics"].append(second)
    attempted = []

    def concurrent_conflict(mapper, connection, target):
        attempted.append(target.code)
        if target.code == "metric_b":
            raise IntegrityError("INSERT metric", {}, RuntimeError("simulated concurrent code conflict"))

    event.listen(Metric, "before_insert", concurrent_conflict)
    try:
        rejected(catalog, model)
    finally:
        event.remove(Metric, "before_insert", concurrent_conflict)
    assert "metric_b" in attempted
    if not existing:
        assert attempted.index("metric_a") < attempted.index("metric_b")


def test_catalog_reports_each_missing_source_without_partial_writes(catalog):
    model = document()
    model["datasets"][0]["source"] = "catalog.schema_a.absent_a"
    second = deepcopy(model["datasets"][0])
    second["name"] = "records_b"
    second["source"] = "catalog.schema_a.absent_b"
    model["datasets"].append(second)
    report = rejected(catalog, model)
    errors = [error for error in report["errors"] if error["layer"] == "catalog"]
    assert len(errors) >= 2
    assert any("datasets[0]" in error["path"] for error in errors)
    assert any("datasets[1]" in error["path"] for error in errors)


def test_matching_dialect_is_used_and_unknown_dialect_survives(catalog):
    model = document()
    unknown = {"dialect": "private_language_v9", "expression": "TOTAL [value_a] WITH UNKNOWN SYNTAX"}
    model["metrics"][0]["expression"]["dialects"].insert(0, unknown)
    report = import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
    assert report["ok"] and versions(catalog)[0].expression == "SUM(b.value_a)"
    saved = versions(catalog)[0].publish_check["ossie"]["document"]
    assert saved["metrics"][0]["expression"]["dialects"][0] == unknown
    assert THIRD_PARTY in saved["metrics"][0]["custom_extensions"]
    assert not report["warnings"]


def test_generic_dialect_transpilation_requires_confirmation_in_publish_check(catalog):
    model = document(dialect="OSSIE_SQL_2026")
    report = import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
    version = versions(catalog)[0]
    assert report["ok"] and version.expression == "SUM(b.value_a)" and version.status == "draft"
    assert report["warnings"]
    assert all(set(warning) >= {"metric", "path", "reason"} for warning in report["warnings"])
    assert any("人工确认" in warning["reason"] for warning in report["warnings"])
    check = metrics.publish_check(catalog.session, version, catalog.actor)
    assert any("人工确认" in warning for warning in check["warnings"])


def test_unknown_only_dialect_preserves_original_and_cannot_submit(catalog):
    text = "TOTAL [value_a] WITH UNKNOWN SYNTAX"
    model = document(dialect="private_language_v9", expression=text)
    report = import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
    version = versions(catalog)[0]
    assert report["ok"] and report["warnings"] and version.status == "draft"
    assert version.expression == text
    assert version.publish_check["ossie"]["unresolved_dialect"] is True
    with pytest.raises(GovernanceError):
        metrics.submit(catalog.session, catalog.actor, version)
    assert version.status == "draft" and version.submitted_at is None

    metrics.update_draft(catalog.session, catalog.actor, version, {"expression": "SUM(b.value_a)"})
    assert not version.publish_check["ossie"]["unresolved_dialect"]
    assert not version.publish_check["ossie"]["warnings"]
    metrics.submit(catalog.session, catalog.actor, version)
    assert version.status == "pending_review"
    assert "UNKNOWN SYNTAX" in json.dumps(version.publish_check["ossie"]["document"])


@pytest.mark.parametrize(
    "field_dialect,field_expression",
    [("private_language_v9", "value_a * 2"), ("postgres", "value_a +")],
)
def test_unmappable_field_expression_does_not_silently_use_same_named_physical_column(
    catalog, field_dialect, field_expression
):
    model = document()
    model["datasets"][0]["fields"][0]["expression"]["dialects"] = [
        {"dialect": field_dialect, "expression": field_expression}
    ]
    report = import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
    version = latest_version(catalog)
    assert report["ok"] and report["warnings"] and version.status == "draft"
    assert version.expression == "SUM(records.value_a)"
    assert version.expression != "SUM(b.value_a)"
    assert version.publish_check["ossie"]["unresolved_dialect"] is True
    stored_field = version.publish_check["ossie"]["document"]["datasets"][0]["fields"][0]
    assert stored_field["expression"]["dialects"] == [{"dialect": field_dialect, "expression": field_expression}]
    check = metrics.publish_check(catalog.session, version, catalog.actor)
    assert not check["ok"] and check["blockers"]
    with pytest.raises(GovernanceError):
        metrics.submit(catalog.session, catalog.actor, version)
    assert version.status == "draft" and version.submitted_at is None


def test_public_version_detail_hides_unused_cross_domain_original_document(catalog):
    catalog.session.add(DomainMember(user_id="maintainer", domain_id="hidden", role="viewer"))
    catalog.session.commit()
    model = document()
    unused = deepcopy(model["datasets"][0])
    unused.update(name="unused_private_dataset", source="catalog.schema_a.records_c")
    unused["description"] = "未使用的保密目录说明"
    model["datasets"].append(unused)
    assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
    version = latest_version(catalog)
    # 导入人的完整文档仍可往返；只有本域阅读权限的用户不能通过口径详情拿到它。
    assert access.can_see_object(catalog.session, catalog.users["reader"], "metric_version", version.id)
    assert not access.can_read_asset(
        catalog.session, catalog.users["reader"], catalog.session.get(Asset, "hidden_asset")
    )
    original = version.publish_check["ossie"]["document"]
    assert original["datasets"][1]["source"] == unused["source"]
    assert "version" not in original
    public_check = metrics.public_publish_check(version.publish_check)
    assert "ossie" not in public_check
    detail = metrics.describe_version(catalog.session, version)
    assert detail["publish_check"] == public_check
    serialized = json.dumps(detail, ensure_ascii=False)
    for private_value in ("unused_private_dataset", "catalog.schema_a.records_c", "未使用的保密目录说明"):
        assert private_value not in serialized


def test_dialect_names_are_case_sensitive(catalog):
    model = document(dialect="POSTGRES", expression="SUM(records.value_a)")
    report = import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
    assert report["ok"] and report["warnings"]
    assert versions(catalog)[0].publish_check["ossie"]["unresolved_dialect"] is True


def test_string_ai_context_becomes_instructions(catalog):
    model = document()
    model["metrics"][0]["ai_context"] = "仅用于观察范围甲"
    import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
    assert metric(catalog).ai_context == {"instructions": "仅用于观察范围甲", "synonyms": [], "examples": []}


def test_unknown_ai_context_members_are_preserved_without_bypassing_governance(catalog):
    model = document()
    model["metrics"][0]["ai_context"]["other_hint"] = {"value": "原样保留"}
    report = import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
    assert report["ok"] and metric(catalog).ai_context == CONTEXT
    assert versions(catalog)[0].publish_check["ossie"]["document"]["metrics"][0]["ai_context"]["other_hint"] == {
        "value": "原样保留"
    }


def test_base_export_dry_run_import_publish_export_preserves_core_and_extensions(catalog):
    model = document()
    model["custom_extensions"] = [{"vendor_name": "other_system", "data": '{"root_flag":true}'}]
    dataset = model["datasets"][0]
    dataset["custom_extensions"] = [{"vendor_name": "other_system", "data": '{"dataset_flag":true}'}]
    dataset["fields"][0]["custom_extensions"] = [{"vendor_name": "other_system", "data": '{"field_flag":true}'}]
    model["metrics"][0]["datatype"] = "Decimal"
    model["metrics"][0]["ai_context"]["other_hint"] = "原样保留"
    avelis_extension = next(e for e in model["metrics"][0]["custom_extensions"] if e["vendor_name"] == "avelis")
    avelis_extension["data"] = json.dumps({**json.loads(avelis_extension["data"]), "future_hint": {"keep": True}})
    model["metrics"][0]["expression"]["dialects"].append(
        {"dialect": "private_language_v9", "expression": "UNKNOWN ORIGINAL EXPRESSION"}
    )
    import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
    publish(catalog)
    first = export_model(catalog.session, catalog.actor, "visible")
    assert validate_structure(first) == []
    before = snapshot(catalog)
    dry = import_model(catalog.session, catalog.actor, first, "visible")
    assert dry["ok"] and dry["dry_run"]
    assert snapshot(catalog)["versions"] == before["versions"]
    applied = import_model(catalog.session, catalog.actor, first, "visible", dry_run=False)
    assert applied["ok"] and applied["results"][0]["action"] == "new_version"
    assert latest_version(catalog).status == "draft"
    publish(catalog)
    second = export_model(catalog.session, catalog.actor, "visible")
    assert validate_structure(second) == []
    assert second == first
    assert second["metrics"][0]["datatype"] == "Decimal"
    assert second["metrics"][0]["ai_context"]["other_hint"] == "原样保留"
    assert THIRD_PARTY in second["metrics"][0]["custom_extensions"]
    exported_avelis = next(e for e in second["metrics"][0]["custom_extensions"] if e["vendor_name"] == "avelis")
    assert json.loads(exported_avelis["data"])["future_hint"] == {"keep": True}


def test_each_metric_uses_its_own_datasource_dialect(catalog):
    catalog.session.add(Datasource(id="mysql_source", name="离线目录乙", kind="mysql", host="offline", port=1))
    catalog.session.get(Asset, "asset_b").datasource_id = "mysql_source"
    catalog.session.commit()
    model = document()
    dataset = deepcopy(model["datasets"][0])
    dataset["name"] = "records_b"
    dataset["source"] = "catalog.schema_a.records_b"
    for field in dataset["fields"]:
        field["expression"]["dialects"].append({"dialect": "mysql", "expression": field["name"]})
    model["datasets"].append(dataset)
    second = deepcopy(model["metrics"][0])
    second["name"] = "metric_b"
    second["expression"]["dialects"] = [
        {"dialect": "postgres", "expression": "SUM(records_b.value_a) * 2"},
        {"dialect": "mysql", "expression": "SUM(records_b.value_a) * 3"},
    ]
    second["custom_extensions"] = [extension(base_dataset="records_b", time_column="b.observed_on")]
    model["metrics"].append(second)
    report = import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
    assert report["ok"] and not report["warnings"]
    assert latest_version(catalog, "metric_a").expression == "SUM(b.value_a)"
    assert latest_version(catalog, "metric_b").expression == "SUM(b.value_a) * 3"


def test_dataset_scoped_metric_is_imported_and_exported_at_root(catalog):
    model = document()
    model["metrics"][0]["expression"]["dialects"][0]["expression"] = "SUM(value_a)"
    model["datasets"][0]["metrics"] = model.pop("metrics")
    assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
    publish(catalog)
    exported = export_model(catalog.session, catalog.actor, "visible")
    assert [m["name"] for m in exported["metrics"]] == ["metric_a"]
    assert all("metrics" not in dataset for dataset in exported["datasets"])


def test_derived_roundtrip_restores_pinned_components_and_formula(catalog):
    for code in ("metric_a", "metric_b"):
        import_model(catalog.session, catalog.actor, document(code), "visible", dry_run=False)
        publish(catalog, code)
    derived = document("metric_c", expression="SUM(records.value_a) - SUM(records.value_a)")
    derived["metrics"][0]["custom_extensions"] = [
        deepcopy(THIRD_PARTY),
        extension(
            kind="derived",
            name="派生指标甲",
            formula="metric_a - metric_b",
            components=[{"metric_code": "metric_a", "version_no": 1}, {"metric_code": "metric_b", "version_no": 1}],
            timezone="UTC",
        ),
    ]
    report = import_model(catalog.session, catalog.actor, derived, "visible", dry_run=False)
    assert report["ok"]
    version = latest_version(catalog, "metric_c")
    assert version.kind == "derived" and version.expression == "metric_a - metric_b"
    assert version.status == "draft"
    publish(catalog, "metric_c")
    selected = [metric(catalog, "metric_c").id]
    first = export_model(catalog.session, catalog.actor, "visible", selected)
    assert validate_structure(first) == []
    assert "metric_a" not in first["metrics"][0]["expression"]["dialects"][0]["expression"]
    assert import_model(catalog.session, catalog.actor, first, "visible")["ok"]
    assert import_model(catalog.session, catalog.actor, first, "visible", dry_run=False)["ok"]
    restored = latest_version(catalog, "metric_c")
    assert restored.kind == "derived" and restored.expression == version.expression and restored.status == "draft"
    publish(catalog, "metric_c")
    second = export_model(catalog.session, catalog.actor, "visible", selected)
    assert second == first
    components = json.loads(
        next(e["data"] for e in second["metrics"][0]["custom_extensions"] if e["vendor_name"] == "avelis")
    )["components"]
    assert components == [
        {"metric_code": code, "version_no": 1, "expression": latest_version(catalog, code).expression}
        for code in ("metric_a", "metric_b")
    ]


def derived_document(component):
    model = document("metric_c", expression="SUM(records.value_a) * 2")
    model["metrics"][0]["custom_extensions"] = [
        extension(kind="derived", formula="metric_a * 2", components=[component], timezone="UTC")
    ]
    return model


@pytest.mark.parametrize("source_number", [1, 99])
def test_derived_import_locks_local_current_version_instead_of_source_number(catalog, source_number):
    import_model(catalog.session, catalog.actor, document(), "visible", dry_run=False)
    original = publish(catalog)
    import_model(
        catalog.session, catalog.actor, document(expression="SUM(records.value_a) * 3"), "visible", dry_run=False
    )
    current = publish(catalog)
    assert current.version_no == 2
    model = derived_document(
        {"metric_code": "metric_a", "version_no": source_number, "expression": original.expression}
    )
    detail = rejected(catalog, model)
    assert all(error["layer"] == "catalog" for error in detail["errors"])
    assert any("核心表达式与扩展中的派生公式不一致" in error["reason"] for error in detail["errors"])
    model["metrics"][0]["expression"]["dialects"][0]["expression"] = "(SUM(records.value_a) * 3) * 2"
    expected = "组成口径 metric_a 在本环境的版本与导出方不同，已按本环境当前版本锁定"
    dry = import_model(catalog.session, catalog.actor, model, "visible")
    applied = import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
    assert dry["ok"] and applied["ok"]
    assert [w["reason"] for w in dry["warnings"]] == [expected]
    assert applied["warnings"] == dry["warnings"]
    version = latest_version(catalog, "metric_c")
    pin = version.publish_check["ossie"]["components"][0]
    assert pin["metric_id"] == metric(catalog).id
    assert pin["version_id"] == current.id
    assert pin["version_no"] == 2
    assert pin["source_version_no"] == source_number
    assert version.components == [pin]
    check = metrics.publish_check(catalog.session, version, catalog.actor)
    assert check["ok"] and check["components"][0]["version_id"] == current.id
    assert expected in check["warnings"]
    assert publish(catalog, "metric_c").components[0]["version_id"] == current.id


@pytest.mark.parametrize("field", ["expression", "dependency_fingerprint"])
@pytest.mark.parametrize("different", [False, True])
def test_derived_import_compares_available_definition_fields(catalog, field, different):
    import_model(catalog.session, catalog.actor, document(), "visible", dry_run=False)
    current = publish(catalog)
    component = {
        "metric_code": "metric_a",
        "version_no": 99,
        field: "different" if different else getattr(current, field),
    }
    report = import_model(catalog.session, catalog.actor, derived_document(component), "visible", dry_run=False)
    assert report["ok"]
    assert bool(report["warnings"]) is different
    assert latest_version(catalog, "metric_c").components[0]["version_id"] == current.id


@pytest.mark.parametrize("replacement", [False, True])
def test_derived_publish_check_falls_back_when_local_pin_is_deleted(catalog, replacement):
    import_model(catalog.session, catalog.actor, document(), "visible", dry_run=False)
    original = publish(catalog)
    import_model(
        catalog.session,
        catalog.actor,
        derived_document({"metric_code": "metric_a", "version_no": 99}),
        "visible",
        dry_run=False,
    )
    version = latest_version(catalog, "metric_c")
    if replacement:
        import_model(catalog.session, catalog.actor, document(), "visible", dry_run=False)
        current = publish(catalog)
    catalog.session.delete(original)
    catalog.session.flush()
    check = metrics.publish_check(catalog.session, version, catalog.actor)
    assert check["ok"] is replacement
    assert any("锁定的本环境版本 V1 已不存在" in warning for warning in check["warnings"])
    if replacement:
        assert check["components"][0]["version_id"] == current.id
        metrics.submit(catalog.session, catalog.actor, version)
        assert version.components[0]["version_id"] == current.id
        assert any("锁定的本环境版本 V1 已不存在" in warning for warning in version.publish_check["warnings"])
    else:
        assert check["components"] == []
        assert any("当前没有已发布版本" in blocker for blocker in check["blockers"])


def relationship_document():
    model = document()
    second = deepcopy(model["datasets"][0])
    second["name"] = "records_b"
    second["source"] = "catalog.schema_a.records_b"
    model["datasets"].append(second)
    model["relationships"] = [
        {"name": "path_a", "from": "records", "to": "records_b", "from_columns": ["group_a"], "to_columns": ["group_a"]}
    ]
    return model


def approve_relationships(catalog, model):
    assets = {asset.name: asset for asset in catalog.session.scalars(select(Asset))}
    datasets = {dataset["name"]: assets[dataset["source"].rsplit(".", 1)[1]] for dataset in model["datasets"]}
    for relationship in model["relationships"]:
        name = relationship["name"]
        catalog.session.add_all(
            [
                JoinPath(
                    id=name,
                    code=name,
                    name=name,
                    left_asset_id=datasets[relationship["from"]].id,
                    left_columns=relationship["from_columns"],
                    right_asset_id=datasets[relationship["to"]].id,
                    right_columns=relationship["to_columns"],
                    cardinality="1n",
                    status="approved",
                    created_by=catalog.actor.id,
                ),
                JoinPathApproval(join_path_id=name, domain_id="visible", user_id="maintainer", decision="approved"),
            ]
        )
    catalog.session.commit()


def path_selection_document(catalog, *, shortcut=True):
    model = relationship_document()
    third = deepcopy(model["datasets"][1])
    third.update(name="records_c", source="catalog.schema_a.records_c")
    model["datasets"].append(third)
    catalog.session.get(Asset, "hidden_asset").domain_id = "visible"
    for dataset in model["datasets"][1:]:
        for field in dataset["fields"]:
            field.pop("dimension", None)
        dataset["fields"][1]["datatype"] = "Date"
    model["metrics"][0]["custom_extensions"] = [deepcopy(THIRD_PARTY)]
    model["relationships"].append(
        {
            "name": "path_b",
            "from": "records_b",
            "to": "records_c",
            "from_columns": ["group_a"],
            "to_columns": ["group_a"],
        }
    )
    if shortcut:
        model["relationships"].append(
            {
                "name": "path_c",
                "from": "records",
                "to": "records_c",
                "from_columns": ["group_a"],
                "to_columns": ["group_a"],
            }
        )
    approve_relationships(catalog, model)
    return model


def test_third_party_base_only_metric_does_not_join_available_relationships_and_can_publish(catalog):
    model = path_selection_document(catalog)
    assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
    version = latest_version(catalog)
    assert version.expression == "SUM(b.value_a)"
    assert version.joins == []
    assert version.dimensions == [{"name": "group_a", "column": "b.group_a", "label": "group_a"}]
    assert version.time_column == "b.observed_on"
    check = metrics.publish_check(catalog.session, version, catalog.actor)
    assert check["ok"] and check["joins"] == [] and check["fan_out"] == []


@pytest.mark.parametrize("shortcut", [False, True])
def test_third_party_expression_joins_only_its_shortest_path(catalog, shortcut):
    model = path_selection_document(catalog, shortcut=shortcut)
    model["metrics"][0]["expression"]["dialects"][0]["expression"] = "SUM(records.value_a + records_c.value_a)"
    assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
    version = latest_version(catalog)
    assert [join["join_path_id"] for join in version.joins] == (["path_c"] if shortcut else ["path_a", "path_b"])
    assert version.expression == "SUM(b.value_a + j2.value_a)"
    assert version.joins[-1]["from"] == ("b" if shortcut else "j1")


@pytest.mark.parametrize("dimension_source", ["field", "extension"])
def test_inferred_joins_include_selected_remote_dimension_path(catalog, dimension_source):
    model = path_selection_document(catalog)
    if dimension_source == "field":
        model["datasets"][2]["fields"][2]["dimension"] = {"is_time": False}
    else:
        model["metrics"][0]["custom_extensions"].append(
            extension(time_column="b.observed_on", dimensions=[{"name": "remote_group", "column": "j2.group_a"}])
        )
    assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
    version = latest_version(catalog)
    assert version.expression == "SUM(b.value_a)"
    assert version.joins == [{"join_path_id": "path_c", "alias": "j2", "from": "b", "pre_aggregate": False}]
    assert any(dimension["column"] == "j2.group_a" for dimension in version.dimensions)


def test_base_inference_is_independent_of_operand_order(catalog):
    model = relationship_document()
    approve_relationships(catalog, model)
    model["metrics"][0]["custom_extensions"] = [extension(timezone="UTC")]
    imported = []
    for expression in ("records.value_a + records_b.value_a", "records_b.value_a + records.value_a"):
        model["metrics"][0]["expression"] = {"sql": f"SUM({expression})"}
        assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
        version = latest_version(catalog)
        imported.append((version.base_asset_id, version.joins, version.dimensions, version.dependency))
    assert imported[0] == imported[1]
    assert imported[0][0] == "asset"
    assert imported[0][1] == [{"join_path_id": "path_a", "alias": "j1", "from": "b", "pre_aggregate": False}]


def test_base_inference_does_not_select_upstream_dataset_for_its_dimensions(catalog):
    model = relationship_document()
    approve_relationships(catalog, model)
    model["metrics"][0]["custom_extensions"] = [extension(timezone="UTC")]
    model["metrics"][0]["expression"] = {"sql": "SUM(records_b.value_a)"}
    assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
    version = latest_version(catalog)
    assert version.base_asset_id == "asset_b" and version.joins == []
    assert version.expression == "SUM(b.value_a)"
    assert version.dimensions == [{"name": "group_a", "column": "b.group_a", "label": "group_a"}]


@pytest.mark.parametrize("base_extension", [False, True])
def test_explicit_base_or_dataset_scope_still_takes_precedence(catalog, base_extension):
    model = relationship_document()
    approve_relationships(catalog, model)
    model["metrics"][0]["expression"] = {"sql": "SUM(records_b.value_a)"}
    model["metrics"][0]["custom_extensions"] = [
        extension(**({"base_dataset": "records"} if base_extension else {}), timezone="UTC", dimensions=[])
    ]
    if not base_extension:
        model["metrics"][0]["expression"] = {"sql": "SUM(value_a)"}
        model["datasets"][1]["metrics"] = model.pop("metrics")
    assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
    version = latest_version(catalog)
    if base_extension:
        assert version.base_asset_id == "asset" and version.joins[0]["join_path_id"] == "path_a"
    else:
        assert version.base_asset_id == "asset_b" and version.joins == []


def test_base_inference_reports_each_unreachable_required_dataset(catalog):
    model = relationship_document()
    model.pop("relationships")
    model["metrics"][0]["custom_extensions"] = [extension(timezone="UTC")]
    model["metrics"][0]["expression"] = {"sql": "SUM(records_b.value_a + records.value_a)"}
    report = rejected(catalog, model)
    assert report["errors"][0]["reason"] == (
        "无法推断指标基础数据集，以下数据集沿关系方向不可达：records -> records_b; records_b -> records"
    )


@pytest.mark.parametrize("shorter_dimension_base", [False, True])
def test_base_inference_uses_shortest_total_distance_then_dataset_name(catalog, shorter_dimension_base):
    model = relationship_document()
    model["metrics"][0]["custom_extensions"] = [extension(timezone="UTC")]
    model["metrics"][0]["expression"] = {"sql": "SUM(records_b.value_a + records.value_a)"}
    if shorter_dimension_base:
        third = deepcopy(model["datasets"][0])
        third.update(name="records_c", source="catalog.schema_a.records_c")
        model["datasets"].append(third)
        catalog.session.get(Asset, "hidden_asset").domain_id = "visible"
        edges = [
            ("records", "records_c"),
            ("records_b", "records_c"),
            ("records_c", "records"),
            ("records_c", "records_b"),
        ]
    else:
        edges = [("records", "records_b"), ("records_b", "records")]
    model["relationships"] = [
        {"name": f"path_{i}", "from": left, "to": right, "from_columns": ["group_a"], "to_columns": ["group_a"]}
        for i, (left, right) in enumerate(edges)
    ]
    # Selection must also be independent of the dataset and relationship array order.
    model["datasets"].reverse()
    model["relationships"].reverse()
    approve_relationships(catalog, model)
    assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
    assert latest_version(catalog).base_asset_id == ("hidden_asset" if shorter_dimension_base else "asset")


@pytest.mark.parametrize("status", [None, "pending", "approved"])
def test_relationships_require_an_existing_approved_path_without_creating_or_approving(catalog, status):
    if status:
        catalog.session.add(
            JoinPath(
                id="path",
                code="path_a",
                name="关联甲",
                left_asset_id="asset",
                left_columns=["group_a"],
                right_asset_id="asset_b",
                right_columns=["group_a"],
                cardinality="n1",
                status=status,
                created_by=catalog.actor.id,
                dependency=[
                    {"asset_id": asset_id, "column": "group_a", "fingerprint": "f" * 64}
                    for asset_id in ("asset", "asset_b")
                ],
            )
        )
        if status == "approved":
            catalog.session.add(
                JoinPathApproval(join_path_id="path", domain_id="visible", user_id="maintainer", decision="approved")
            )
        catalog.session.commit()
    if status == "approved":
        report = import_model(catalog.session, catalog.actor, relationship_document(), "visible", dry_run=False)
        assert report["ok"]
    else:
        report = rejected(catalog, relationship_document())
        assert any("关联路径" in error["reason"] for error in report["errors"])
    paths = list(catalog.session.scalars(select(JoinPath)))
    assert len(paths) == bool(status)
    if paths:
        assert paths[0].status == status


@pytest.mark.parametrize("relationship_name", ["external_path", "a_pending", "z_approved"])
def test_relationship_matching_skips_pending_even_when_its_name_matches(catalog, relationship_name):
    model = relationship_document()
    model["relationships"][0]["name"] = "z_approved"
    approve_relationships(catalog, model)
    approved = catalog.session.get(JoinPath, "z_approved")
    approved.cardinality = "n1"
    catalog.session.add(
        JoinPath(
            id="pending",
            code="a_pending",
            name="pending",
            left_asset_id="asset",
            left_columns=["group_a"],
            right_asset_id="asset_b",
            right_columns=["group_a"],
            cardinality="n1",
            status="pending",
            created_by=catalog.actor.id,
        )
    )
    catalog.session.commit()
    model["relationships"][0]["name"] = relationship_name
    model["metrics"][0]["expression"] = {"sql": "SUM(records.value_a + records_b.value_a)"}
    model["metrics"][0]["custom_extensions"] = [extension(base_dataset="records", timezone="UTC")]
    assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
    assert latest_version(catalog).joins[0]["join_path_id"] == "z_approved"


def test_relationship_matching_uses_effective_approval_not_status_alone(catalog):
    model = relationship_document()
    model["relationships"][0]["name"] = "z_approved"
    approve_relationships(catalog, model)
    approved = catalog.session.get(JoinPath, "z_approved")
    approved.cardinality = "n1"
    catalog.session.add(
        JoinPath(
            id="stale",
            code="a_stale",
            name="stale",
            left_asset_id="asset",
            left_columns=["group_a"],
            right_asset_id="asset_b",
            right_columns=["group_a"],
            cardinality="n1",
            status="approved",
            created_by=catalog.actor.id,
        )
    )
    catalog.session.commit()
    model["relationships"][0]["name"] = "a_stale"
    model["metrics"][0]["expression"] = {"sql": "SUM(records.value_a + records_b.value_a)"}
    model["metrics"][0]["custom_extensions"] = [extension(base_dataset="records", timezone="UTC")]
    assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
    assert latest_version(catalog).joins[0]["join_path_id"] == "z_approved"


def test_multiple_approved_relationship_candidates_are_deterministic_and_warn(catalog):
    model = relationship_document()
    model["relationships"][0]["name"] = "z_approved"
    approve_relationships(catalog, model)
    other = deepcopy(model)
    other["relationships"][0]["name"] = "a_approved"
    approve_relationships(catalog, other)
    model["relationships"][0]["name"] = "external_path"
    model["metrics"][0]["expression"] = {"sql": "SUM(records.value_a + records_b.value_a)"}
    model["metrics"][0]["custom_extensions"] = [extension(base_dataset="records", timezone="UTC")]
    report = import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)
    assert report["ok"] and latest_version(catalog).joins[0]["join_path_id"] == "a_approved"
    assert {
        "metric": "",
        "path": "$.relationships[0]",
        "reason": "关系 external_path 匹配多条已批准的关联路径，已选择 a_approved（候选：a_approved, z_approved）",
    } in report["warnings"]


def test_all_matching_relationship_candidates_still_need_approval(catalog):
    model = relationship_document()
    model["relationships"][0]["name"] = "z_pending"
    approve_relationships(catalog, model)
    other = deepcopy(model)
    other["relationships"][0]["name"] = "a_pending"
    approve_relationships(catalog, other)
    for path in catalog.session.scalars(select(JoinPath)):
        path.status = "pending"
    catalog.session.commit()
    report = rejected(catalog, model)
    assert report["errors"][0]["reason"] == "需要先批准关联路径"


def test_exported_relationship_name_selects_same_approved_path_on_reimport(catalog):
    model = relationship_document()
    model["relationships"][0]["name"] = "z_approved"
    approve_relationships(catalog, model)
    other = deepcopy(model)
    other["relationships"][0]["name"] = "a_approved"
    approve_relationships(catalog, other)
    for path in catalog.session.scalars(select(JoinPath)):
        path.cardinality = "n1"
    # A unique key is needed to publish this ordinary n1 join.
    catalog.session.add(
        Grain(
            id="grain_b",
            asset_id="asset_b",
            asset_version_id="asset_b_v1",
            columns=["group_a"],
            origin="observed",
            status="verified",
        )
    )
    catalog.session.commit()
    model["metrics"][0]["custom_extensions"] = [
        extension(
            base_dataset="records",
            timezone="UTC",
            time_column="b.observed_on",
            joins=[{"relationship": "z_approved", "alias": "r", "from": "b"}],
            dimensions=[{"name": "group_a", "column": "r.group_a"}],
        )
    ]
    assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
    assert latest_version(catalog).joins[0]["join_path_id"] == "z_approved"
    check = metrics.publish_check(catalog.session, latest_version(catalog), catalog.actor)
    assert check["ok"], check["blockers"]
    publish(catalog)
    exported = export_model(catalog.session, catalog.actor, "visible")
    assert exported["relationships"][0]["name"] == "z_approved"
    assert import_model(catalog.session, catalog.actor, exported, "visible", dry_run=False)["ok"]
    assert latest_version(catalog).joins[0]["join_path_id"] == "z_approved"


def test_relationship_extensions_survive_reuse_and_roundtrip(catalog):
    # 已批准路径的编码可以与外部文件不同，复用按资产端点与列对判断。
    catalog.session.add_all(
        [
            JoinPath(
                id="path",
                code="existing_path",
                name="关联甲",
                left_asset_id="asset",
                left_columns=["group_a"],
                right_asset_id="asset_b",
                right_columns=["group_a"],
                cardinality="n1",
                status="approved",
                created_by=catalog.actor.id,
            ),
            JoinPathApproval(join_path_id="path", domain_id="visible", user_id="maintainer", decision="approved"),
        ]
    )
    catalog.session.commit()
    model = relationship_document()
    original = model["relationships"][0]
    original["custom_extensions"] = [deepcopy(THIRD_PARTY)]
    original["ai_context"] = {"instructions": "关联范围甲"}
    # 导入保留路径本身，但口径无需使用关联侧，避免以无粒度证据的 n1 声明代替发布检查。
    options = json.loads(
        next(e["data"] for e in model["metrics"][0]["custom_extensions"] if e["vendor_name"] == "avelis")
    )
    options["joins"] = []
    model["metrics"][0]["custom_extensions"] = [deepcopy(THIRD_PARTY), extension(**options)]
    assert import_model(catalog.session, catalog.actor, model, "visible", dry_run=False)["ok"]
    publish(catalog)
    first = export_model(catalog.session, catalog.actor, "visible")
    assert first["relationships"][0]["name"] == "existing_path"
    assert first["relationships"][0]["custom_extensions"] == [THIRD_PARTY]
    assert first["relationships"][0]["ai_context"] == original["ai_context"]
    assert import_model(catalog.session, catalog.actor, first, "visible", dry_run=False)["ok"]
    publish(catalog)
    assert export_model(catalog.session, catalog.actor, "visible") == first
    assert len(list(catalog.session.scalars(select(JoinPath)))) == 1
