"""Ossie HTTP 契约：下载、上传解析、结果类型、原子错误与审计摘要。"""

import json
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis.api import interchange
from avelis.api.app import create_app
from avelis.api.deps import current_user, get_session
from avelis.db import Base
from avelis.governance.errors import Forbidden, GovernanceError
from avelis.models import AuditEvent, Domain, DomainMember, Metric, MetricVersion, User

DOCUMENT = {
    "version": "0.2.0.dev0",
    "name": "neutral_model",
    "datasets": [],
    "metrics": [
        {"name": "metric_a", "expression": {"dialects": [{"dialect": "postgres", "expression": "SUM(value_a)"}]}}
    ],
}
REPORT = {
    "ok": True,
    "dry_run": True,
    "results": [{"name": "metric_a", "action": "created"}],
    "errors": [],
    "warnings": [{"metric": "metric_a", "path": "$.metrics[0].expression", "reason": "待人工确认"}],
    "counts": {"created": 1, "new_version": 0, "skipped": 0, "failed": 0},
}


@pytest.fixture
def api(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for original in Base.metadata.tables.values():
        table = original.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    app = create_app()
    calls = []

    def export_model(session, actor, domain_id, metric_ids=None):
        calls.append(("export", actor.id, domain_id, metric_ids))
        return DOCUMENT

    def import_model(session, actor, document, domain_id, *, dry_run=True):
        calls.append(("import", actor.id, document, domain_id, dry_run))
        return {**REPORT, "dry_run": dry_run}

    monkeypatch.setattr(interchange.exporter, "export_model", export_model)
    monkeypatch.setattr(interchange.importer, "import_model", import_model)
    with Session(engine, expire_on_commit=False) as session:
        actor = User(id="actor", username="actor", display_name="维护者", password_hash="unused")
        session.add_all([actor, Domain(id="domain_a", code="domain_a", name='域甲/"\r\n', owner_id="actor")])
        session.commit()
        app.dependency_overrides[current_user] = lambda: actor

        def db_session():
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

        app.dependency_overrides[get_session] = db_session
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, session=session, calls=calls)
    engine.dispose()


@pytest.mark.parametrize("format", ["yaml", "json"])
def test_export_download_format_filename_and_summary_audit(api, format):
    response = api.client.get(
        "/api/interchange/ossie/export",
        params=[
            ("domain_id", "domain_a"),
            ("metric_ids", "metric_a,metric_b"),
            ("metric_ids", "metric_a"),
            ("format", format),
        ],
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(f"application/{format}")
    actual = response.json() if format == "json" else yaml.safe_load(response.text)
    assert actual == DOCUMENT
    disposition = response.headers["content-disposition"]
    assert f'filename="domain.ossie.{format}"' in disposition
    assert f"filename*=UTF-8''%E5%9F%9F%E7%94%B2.ossie.{format}" in disposition
    assert "\r" not in unquote(disposition) and "\n" not in unquote(disposition)
    assert api.calls == [("export", "actor", "domain_a", ["metric_a", "metric_b"])]
    event = api.session.scalar(select(AuditEvent).where(AuditEvent.action == "interchange.ossie_export"))
    assert event.actor_id == "actor"
    assert event.detail == {
        "domain_id": "domain_a",
        "metric_ids": ["metric_a", "metric_b"],
        "format": format,
        "metric_count": 1,
    }
    assert "expression" not in json.dumps(event.detail)


def test_export_without_selection_and_denied_export_have_no_extra_audit(api, monkeypatch):
    assert api.client.get("/api/interchange/ossie/export?domain_id=domain_a").status_code == 200
    assert api.calls[-1] == ("export", "actor", "domain_a", None)

    def deny(*args):
        raise Forbidden("无权限")

    monkeypatch.setattr(interchange.exporter, "export_model", deny)
    assert api.client.get("/api/interchange/ossie/export?domain_id=domain_a").status_code == 403
    assert len(list(api.session.scalars(select(AuditEvent)))) == 1


@pytest.mark.parametrize("kind", ["wrapper", "wrapper_text", "raw_json", "yaml", "multipart"])
def test_import_supported_inputs_default_to_dry_run_and_return_typed_report(api, kind):
    url = "/api/interchange/ossie/import"
    if kind == "wrapper":
        response = api.client.post(url, json={"domain_id": "domain_a", "document": DOCUMENT})
    elif kind == "wrapper_text":
        response = api.client.post(url, json={"domain_id": "domain_a", "document": yaml.safe_dump(DOCUMENT)})
    elif kind == "raw_json":
        response = api.client.post(url + "?domain_id=domain_a", json=DOCUMENT)
    elif kind == "yaml":
        response = api.client.post(
            url + "?domain_id=domain_a", content=yaml.safe_dump(DOCUMENT), headers={"Content-Type": "application/yaml"}
        )
    else:
        response = api.client.post(
            url,
            data={"domain_id": "domain_a"},
            files={"file": ("model.yaml", yaml.safe_dump(DOCUMENT), "application/yaml")},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] and body["dry_run"]
    assert body["results"][0]["name"] == "metric_a"
    assert body["counts"] == REPORT["counts"]
    assert body["warnings"] == REPORT["warnings"]
    assert api.calls == [("import", "actor", DOCUMENT, "domain_a", True)]


@pytest.mark.parametrize("kind", ["wrapper", "query", "multipart", "query_precedence"])
def test_import_explicit_write_option(api, kind):
    url = "/api/interchange/ossie/import"
    if kind == "wrapper":
        response = api.client.post(url, json={"domain_id": "domain_a", "dry_run": False, "document": DOCUMENT})
    elif kind == "query":
        response = api.client.post(url + "?domain_id=domain_a&dry_run=false", json=DOCUMENT)
    elif kind == "multipart":
        response = api.client.post(
            url,
            data={"domain_id": "domain_a", "dry_run": "false"},
            files={"file": ("model.json", json.dumps(DOCUMENT), "application/json")},
        )
    else:
        response = api.client.post(
            url + "?domain_id=domain_a&dry_run=false",
            json={"domain_id": "ignored", "dry_run": True, "document": DOCUMENT},
        )
    assert response.status_code == 200, response.text
    assert not response.json()["dry_run"]
    assert api.calls[-1][-2:] == ("domain_a", False)


@pytest.mark.parametrize(
    "raw,media_type",
    [
        ('{"name":"a","name":"b"}', "application/json"),
        ('{"name":"a","\\u006eame":"b"}', "application/json"),
        ('{"domain_id":"domain_a","document":{"name":"a","name":"b"}}', "application/json"),
        ("name: a\nname: b", "application/yaml"),
        ("defaults: &a {name: a}\nmodel: {<<: *a, name: b}", "application/yaml"),
        ("name: &a [*a]", "application/yaml"),
        ("name: 2020-01-01", "application/yaml"),
        ("name: !!python/object:builtins.object {}", "application/yaml"),
        ('{"name":NaN}', "application/json"),
        ('{"name":1e999}', "application/json"),
        ('{"name":"\\ud800"}', "application/json"),
        ("name: {.nan: a}", "application/yaml"),
        ("[]", "application/json"),
        (b"\xff", "application/yaml"),
        ("[invalid", "application/yaml"),
    ],
)
def test_untrusted_documents_are_rejected_without_service_call_or_write(api, raw, media_type):
    response = api.client.post(
        "/api/interchange/ossie/import?domain_id=domain_a&dry_run=false",
        content=raw,
        headers={"Content-Type": media_type},
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["ok"] is False and body["dry_run"] is False
    assert body["errors"][0]["layer"] == "structure"
    assert body["errors"][0]["path"] == "$"
    assert body["results"] == [] and body["counts"]["created"] == 0
    assert api.calls == []
    assert api.session.scalar(select(AuditEvent.id)) is None


def test_yaml_alias_expansion_and_nesting_are_bounded(api, monkeypatch):
    monkeypatch.setattr(interchange, "MAX_DOCUMENT_VALUES", 20)
    for raw in ("a: &a [1, 2, 3, 4]\nb: [*a, *a, *a, *a, *a]", "a: " + "[" * 110 + "0" + "]" * 110):
        response = api.client.post(
            "/api/interchange/ossie/import?domain_id=domain_a",
            content=raw,
            headers={"Content-Type": "application/yaml"},
        )
        assert response.status_code == 400
    assert not api.calls


def test_nonrecursive_yaml_alias_is_data(api):
    response = api.client.post(
        "/api/interchange/ossie/import?domain_id=domain_a",
        content="name: &name neutral_model\ndescription: *name",
        headers={"Content-Type": "application/yaml"},
    )
    assert response.status_code == 200, response.text
    assert api.calls[-1][2] == {"name": "neutral_model", "description": "neutral_model"}


@pytest.mark.parametrize("filename", ["../../neutral.yaml", r"C:\Windows\neutral.yaml", "absolute"])
def test_upload_filename_is_never_used_as_a_filesystem_path(api, tmp_path, filename):
    target = tmp_path / "neutral.yaml"
    response = api.client.post(
        "/api/interchange/ossie/import?domain_id=domain_a",
        files={"file": (str(target) if filename == "absolute" else filename, yaml.safe_dump(DOCUMENT))},
    )
    assert response.status_code == 200, response.text
    assert api.calls[-1] == ("import", "actor", DOCUMENT, "domain_a", True)
    assert not target.exists()


@pytest.mark.parametrize(
    "language,detail,reason",
    [
        ("en", "Invalid Ossie import request", "Import content contains duplicate keys"),
        ("zh-TW", "Ossie 匯入請求無效", "匯入內容包含重複鍵"),
    ],
)
def test_parse_report_translates_fixed_messages(api, language, detail, reason):
    response = api.client.post(
        "/api/interchange/ossie/import?domain_id=domain_a",
        content='{"name":"a","name":"b"}',
        headers={"Content-Type": "application/json", "Accept-Language": language},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == detail
    assert response.json()["errors"][0]["reason"] == reason


@pytest.mark.parametrize(
    "url,kwargs",
    [
        ("/api/interchange/ossie/import", {"json": DOCUMENT}),
        ("/api/interchange/ossie/import?domain_id=domain_a&dry_run=maybe", {"json": DOCUMENT}),
        ("/api/interchange/ossie/import", {"json": {"domain_id": "domain_a", "dry_run": [], "document": DOCUMENT}}),
        (
            "/api/interchange/ossie/import?domain_id=domain_a",
            {"content": "bad", "headers": {"Content-Type": "application/octet-stream"}},
        ),
        ("/api/interchange/ossie/import?domain_id=domain_a", {"files": {"wrong": ("model.yaml", "name: a")}}),
        (
            "/api/interchange/ossie/import?domain_id=domain_a",
            {"content": "bad", "headers": {"Content-Type": "multipart/form-data"}},
        ),
    ],
)
def test_invalid_options_and_multipart_have_consistent_error_report(api, url, kwargs):
    response = api.client.post(url, **kwargs)
    assert response.status_code == 400, response.text
    assert response.json()["errors"] and not response.json()["ok"]
    assert not api.calls


@pytest.mark.parametrize("kind", ["raw", "file"])
def test_request_size_is_bounded_before_import(api, monkeypatch, kind):
    monkeypatch.setattr(interchange, "MAX_IMPORT_BYTES", 128)
    url = "/api/interchange/ossie/import?domain_id=domain_a"
    kwargs = (
        {"content": "x" * 129, "headers": {"Content-Type": "application/yaml"}}
        if kind == "raw"
        else {"files": {"file": ("model.yaml", "x" * 129)}}
    )
    response = api.client.post(url, **kwargs)
    assert response.status_code == 400, response.text
    assert response.json()["errors"] and not api.calls


@pytest.mark.parametrize("status", [400, 403])
def test_service_failure_report_is_preserved(api, monkeypatch, status):
    report = {
        **REPORT,
        "ok": False,
        "errors": [{"layer": "catalog", "path": "$.datasets[0].source", "reason": "源表不存在或无权访问"}],
    }

    def fail(*args, **kwargs):
        error = GovernanceError if status == 400 else Forbidden
        raise error("导入失败", **report)

    monkeypatch.setattr(interchange.importer, "import_model", fail)
    response = api.client.post("/api/interchange/ossie/import", json={"domain_id": "domain_a", "document": DOCUMENT})
    assert response.status_code == status, response.text
    assert response.json() == {"detail": "导入失败", **report}


def test_openapi_exposes_request_media_types_and_typed_results(api):
    schema = api.client.get("/api/openapi.json").json()
    operation = schema["paths"]["/api/interchange/ossie/import"]["post"]
    dry_run = next(parameter for parameter in operation["parameters"] if parameter["name"] == "dry_run")
    assert dry_run["schema"]["type"] == "boolean" and "true（默认）" in dry_run["description"]
    assert set(operation["requestBody"]["content"]) == {"application/json", "application/yaml", "multipart/form-data"}
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/OssieImportReport"
    }
    assert schema["components"]["schemas"]["OssieImportResult"]["properties"]["action"]["enum"] == [
        "created",
        "new_version",
        "skipped",
        "failed",
    ]


def test_unknown_dialect_draft_publish_check_returns_blocker_without_losing_metadata(api):
    hidden_source = "hidden_catalog.hidden_schema.neutral_records"
    metadata = {
        "ossie": {
            "unresolved_dialect": True,
            "warnings": ["待人工确认"],
            "document": {"datasets": [{"name": "hidden_dataset", "source": hidden_source}]},
        }
    }
    api.session.add_all(
        [
            DomainMember(domain_id="domain_a", user_id="actor", role="viewer"),
            Metric(
                id="metric_a",
                code="metric_a",
                name="口径甲",
                domain_id="domain_a",
                owner_id="actor",
                created_by="actor",
            ),
            MetricVersion(
                id="draft_a",
                metric_id="metric_a",
                version_no=1,
                status="draft",
                description="待补充方言的草稿",
                expression="UNRESOLVED(value_a)",
                time_column="",
                timezone="UTC",
                created_by="actor",
                publish_check=metadata,
            ),
        ]
    )
    api.session.commit()
    detail = api.client.get("/api/metric-versions/draft_a")
    assert detail.status_code == 200, detail.text
    assert "ossie" not in detail.json()["publish_check"] and hidden_source not in detail.text
    response = api.client.post("/api/metric-versions/draft_a/check")
    assert response.status_code == 200, response.text
    check = response.json()
    assert check["ok"] is False and check["blockers"]
    assert check["warnings"] == ["待人工确认"]
    assert "ossie" not in check and check["checked_at"] and hidden_source not in response.text
    api.session.expire_all()
    version = api.session.get(MetricVersion, "draft_a")
    assert version.status == "draft" and version.publish_check == metadata
    member = api.session.scalar(select(DomainMember).where(DomainMember.user_id == "actor"))
    member.role = "maintainer"
    api.session.commit()
    blocked = api.client.post("/api/metric-versions/draft_a/submit")
    assert blocked.status_code == 409, blocked.text
    assert "ossie" not in blocked.json()["check"] and hidden_source not in blocked.text
    api.session.expire_all()
    version = api.session.get(MetricVersion, "draft_a")
    assert version.status == "draft" and version.publish_check["ossie"] == metadata["ossie"]
