"""Exports pass the importer's model, catalogue, and plan checks before download."""

import pytest
from sqlalchemy import select

from avelis.api import interchange
from avelis.governance import access
from avelis.governance.errors import Forbidden
from avelis.interchange.exporter import export_model as real_export_model
from avelis.models import Asset, AuditEvent, ColumnVersion, Datasource, DomainMember, Metric, MetricVersion, User

from .test_ossie_api import api as api


@pytest.fixture
def export_api(api, monkeypatch):
    monkeypatch.setattr(interchange.exporter, "export_model", real_export_model)
    member = api.session.scalar(select(DomainMember).where(DomainMember.domain_id == "domain_a"))
    member.role = "viewer"
    api.session.add_all(
        [
            Datasource(id="source", name="离线源", kind="postgres", host="offline", port=1),
            Asset(
                id="records",
                datasource_id="source",
                kind="table",
                catalog_name="catalog",
                db_name="public",
                name="records",
                domain_id="domain_a",
                current_version_id="records-v1",
            ),
            Metric(
                id="measure_a",
                code="measure_a",
                name="度量甲",
                domain_id="domain_a",
                owner_id="actor",
                created_by="actor",
            ),
            MetricVersion(
                id="measure_a-v1",
                metric_id="measure_a",
                version_no=1,
                status="published",
                description="范围甲",
                base_asset_id="records",
                expression="SUM(b.value_a)",
                time_column="b.observed_at",
                timezone="UTC",
                created_by="actor",
            ),
        ]
    )
    for ordinal, column in enumerate(("value_a", "observed_at")):
        api.session.add(
            ColumnVersion(
                asset_version_id="records-v1",
                column_id=f"records-{column}",
                ordinal=ordinal,
                name=column,
                data_type="timestamp" if column == "observed_at" else "decimal",
            )
        )
    api.session.commit()
    return api


def snapshot(session):
    return {
        "metrics": list(session.scalars(select(Metric.id).order_by(Metric.id))),
        "versions": list(session.scalars(select(MetricVersion.id).order_by(MetricVersion.id))),
        "audit": list(session.scalars(select(AuditEvent.id).order_by(AuditEvent.id))),
    }


@pytest.mark.parametrize(
    ("attribute", "value", "layer", "path", "reason"),
    [
        (
            "metric_code",
            "Measure_A",
            "model",
            "$.metrics[0].name",
            "小写字母",
        ),
        ("time_column", "b.removed_column", "catalog", "$.datasets[0].fields", "不存在"),
        ("time_column", "DATE_TRUNC('day', b.removed_column)", "catalog", "$.datasets[0].fields", "不存在"),
        # A malformed dimension passes structural/column checks; the import plan must reject it.
        ("dimensions", [{"column": "b.value_a"}], "catalog", "$.metrics[0]", "维度需要"),
    ],
)
def test_export_reimport_errors_return_422_before_download(export_api, attribute, value, layer, path, reason):
    session = export_api.session
    if attribute == "metric_code":
        session.get(Metric, "measure_a").code = value
    else:
        setattr(session.get(MetricVersion, "measure_a-v1"), attribute, value)
    session.commit()
    before = snapshot(session)

    response = export_api.client.get("/api/interchange/ossie/export", params={"domain_id": "domain_a"})

    assert response.status_code == 422, response.text
    assert "Content-Disposition" not in response.headers
    body = response.json()
    assert body["detail"] == "Ossie 导出结果无法通过再导入校验"
    assert body["errors"]
    assert all(set(error) == {"layer", "path", "reason"} for error in body["errors"])
    assert any(
        error["layer"] == layer and error["path"] == path and reason in error["reason"] for error in body["errors"]
    )
    assert snapshot(session) == before


def test_export_selfcheck_is_read_only_and_does_not_require_maintainer(export_api):
    session = export_api.session
    actor = session.get(User, "actor")
    with pytest.raises(Forbidden):
        access.require_domain_maintainer(session, actor, "domain_a")
    before = snapshot(session)

    document = real_export_model(session, actor, "domain_a")

    assert [item["name"] for item in document["metrics"]] == ["measure_a"]
    assert snapshot(session) == before
    assert not session.new
    response = export_api.client.get(
        "/api/interchange/ossie/export", params={"domain_id": "domain_a", "format": "json"}
    )
    assert response.status_code == 200, response.text
    assert response.json() == document
    after = snapshot(session)
    assert after["metrics"] == before["metrics"]
    assert after["versions"] == before["versions"]
    # The successful HTTP download records its normal audit entry; the self-check itself records none.
    assert len(after["audit"]) == len(before["audit"]) + 1
    assert session.scalar(select(AuditEvent.action)) == "interchange.ossie_export"
