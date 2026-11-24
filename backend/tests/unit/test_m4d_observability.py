"""M4D 单元测试：W3C TraceID 全链路传播、脱敏结构化日志、Uvicorn 配置、增强版 Prometheus /metrics 与 OTel 钩子。"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from io import StringIO
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis import audit, config, configfile, jobs, observability
from avelis.api import app as app_mod
from avelis.api import ask as ask_api
from avelis.api.deps import current_user, get_session
from avelis.db import Base
from avelis.models import Job, User, UserRole
from avelis.observability import (
    RequestContextMiddleware,
    bind_request_context,
    get_log_context,
    parse_traceparent,
    reset_request_context,
    set_request_context,
)
from avelis.security import create_access_token


def _sqlite_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        execution_options={"schema_translate_map": {"avelis_agent": None}},
    )
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        copied = table.to_metadata(metadata)
        for col in copied.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()
    metadata.create_all(engine)
    return engine, Session(engine, expire_on_commit=False)


def _settings(**extra: str):
    return config.build_settings(
        {
            "AVELIS_SECRET": "m4d-unit-test-secret-0123456789abcdef",
            "AVELIS_MCP_ENABLED": "false",
            **extra,
        }
    )


def test_w3c_traceparent_and_trace_id_headers_and_audit_filter(monkeypatch):
    assert parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01") == (
        "4bf92f3577b34da6a3ce929d0e0e4736",
        "00f067aa0ba902b7",
    )
    assert parse_traceparent("00-00000000000000000000000000000000-00f067aa0ba902b7-01") is None
    assert parse_traceparent("invalid") is None

    engine, session = _sqlite_session()
    try:
        monkeypatch.setattr("avelis.observability.context.get_settings", lambda: _settings())
        admin = User(id="u_adm", username="admin", display_name="Admin", password_hash="x", is_active=True)
        session.add_all([admin, UserRole(user_id="u_adm", role="admin")])
        session.commit()

        app = FastAPI()
        app.add_middleware(RequestContextMiddleware)
        app.include_router(ask_api.router)
        app.dependency_overrides[current_user] = lambda: admin
        app.dependency_overrides[get_session] = lambda: session

        @app.post("/api/test-trace-ping")
        def _ping(user: User = Depends(current_user), db: Session = Depends(get_session)):
            bind_request_context(user_id=user.id)
            ev = audit.record(db, user, "test.trace", "user", user.id)
            db.commit()
            return {"audit_id": ev.id, "ctx": get_log_context()}

        with TestClient(app) as client:
            r_tp = client.post(
                "/api/test-trace-ping",
                headers={
                    "traceparent": "00-4BF92F3577B34DA6A3CE929D0E0E4736-00F067AA0BA902B7-01",
                    "X-Request-ID": "req-tp-1",
                },
            )
            assert r_tp.status_code == 200
            assert r_tp.headers["x-trace-id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
            assert r_tp.headers["x-request-id"] == "req-tp-1"
            assert r_tp.headers["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
            ctx = r_tp.json()["ctx"]
            assert ctx["parent_span_id"] == "00f067aa0ba902b7"
            assert len(ctx["span_id"]) == 16 and ctx["span_id"] != "00f067aa0ba902b7"
            assert ctx["user_id"] == "u_adm"

            # X-Trace-ID 回退与 GET /api/audit?trace_id=... 查询
            r_x = client.post("/api/test-trace-ping", headers={"X-Trace-ID": "custom-trace-99"})
            assert r_x.headers["x-trace-id"] == "custom-trace-99"
            assert "traceparent" not in r_x.headers  # 非 32hex 自定义 trace_id 不强拼 W3C traceparent

            audit_rows = client.get("/api/audit", params={"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"}).json()
            assert len(audit_rows) == 1
            assert audit_rows[0]["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
            assert audit_rows[0]["request_id"] == "req-tp-1"
    finally:
        session.close()
        engine.dispose()


def test_job_enqueue_and_run_job_propagate_trace_context(monkeypatch):
    engine, session = _sqlite_session()
    observed: list[dict[str, str]] = []

    @contextmanager
    def _scope():
        yield session
        session.commit()

    monkeypatch.setattr(jobs, "session_scope", _scope)

    def _fake_collect(ds_id, **_kwargs):
        observed.append(get_log_context())
        return "run-123"

    monkeypatch.setattr(jobs, "collect_now", _fake_collect)
    try:
        tokens = set_request_context(trace_id="trace-api-to-worker", request_id="req-api-1", user_id="u_op")
        try:
            job_id = jobs._enqueue(session, "collect", {"datasource_id": "ds1"}, "u_op")
            session.commit()
        finally:
            reset_request_context(tokens)

        stored = session.get(Job, job_id)
        assert stored.payload["_trace"] == {"trace_id": "trace-api-to-worker", "request_id": "req-api-1"}

        jobs.run_job(job_id)
        assert len(observed) == 1
        assert observed[0]["trace_id"] == "trace-api-to-worker"
        assert observed[0]["request_id"] == "req-api-1"
        assert observed[0]["job_id"] == job_id
        assert observed[0]["user_id"] == "u_op"
        assert len(observed[0]["span_id"]) == 16
    finally:
        session.close()
        engine.dispose()


def test_secret_redacting_and_json_text_formatters():
    raw = (
        "auth=Bearer secret_jwt_token_12345 url=postgresql://avelis:SuperSecretPwd@db:5432/avelis "
        "api_key=sk-1234567890abcdef1234 pat_abcdef1234567890 password='my pass'"
    )
    redacted = observability.redact_secrets(raw)
    assert "secret_jwt_token_12345" not in redacted
    assert "SuperSecretPwd" not in redacted
    assert "sk-1234567890abcdef1234" not in redacted
    assert "pat_abcdef1234567890" not in redacted
    assert "my pass" not in redacted
    assert "postgresql://avelis:***@db:5432/avelis" in redacted

    formatter = observability.JsonLogFormatter(service="avelis-api", env="staging")
    filt = observability.SecretRedactingFilter()
    tokens = set_request_context(
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
        span_id="1122334455667788",
        request_id="req-log-42",
        user_id="u_alice",
        client_ip="203.0.113.55",
    )
    try:
        try:
            raise RuntimeError("boom Bearer supersecret1234")
        except RuntimeError:
            record = logging.LogRecord(
                name="avelis.test",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="failure %s api_key=sk-secretkey12345678",
                args=("demo",),
                exc_info=sys.exc_info(),
            )
            record.__dict__["event"] = "test.fail"
            record.__dict__["duration_ms"] = 12.5
            assert filt.filter(record)
            parsed = json.loads(formatter.format(record))

        text_fmt = observability.TextLogFormatter()
        text_line = text_fmt.format(record)
    finally:
        reset_request_context(tokens)

    assert parsed["level"] == "ERROR"
    assert parsed["service"] == "avelis-api" and parsed["env"] == "staging"
    assert parsed["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert parsed["span_id"] == "1122334455667788"
    assert parsed["request_id"] == "req-log-42"
    assert parsed["user_id"] == "u_alice"
    assert parsed["client_ip"] == "203.0.113.55"
    assert parsed["event"] == "test.fail" and parsed["duration_ms"] == 12.5
    assert parsed["error_type"] == "RuntimeError"
    assert "supersecret1234" not in parsed["error_message"]
    assert "sk-secretkey12345678" not in parsed["message"]
    assert parsed["timestamp"].endswith("Z")
    assert "[trace=4bf92f3577b3 req=req-log-42 uid=u_alice]" in text_line


def test_configure_logging_and_uvicorn_log_config():
    cfg = _settings(AVELIS_LOG_FORMAT="json", AVELIS_LOG_LEVEL="trace", AVELIS_ENVIRONMENT="test")
    ucfg = observability.build_uvicorn_log_config(cfg)
    assert ucfg["loggers"]["uvicorn.access"]["handlers"] == []
    assert ucfg["loggers"]["uvicorn"]["level"] == "DEBUG"
    assert ucfg["formatters"]["avelis"]["env"] == "test"

    observability.configure_logging("info", "json", service="avelis-worker", env="test")
    try:
        root = logging.getLogger()
        stream_handlers = [h for h in root.handlers if not type(h).__module__.startswith("_pytest")]
        assert len(stream_handlers) == 1
        assert isinstance(stream_handlers[0].formatter, observability.JsonLogFormatter)
        buf = StringIO()
        stream_handlers[0].stream = buf
        logging.getLogger("avelis.m4d").info("hello Authorization=secretval")
        data = json.loads(buf.getvalue().strip())
        assert data["service"] == "avelis-worker"
        assert "secretval" not in data["message"]

        observability.configure_logging("debug", "text")
        assert root.level == logging.DEBUG
    finally:
        observability.configure_logging("info", "text")


def test_observability_config_settings_and_sensitive_keys():
    assert "AVELIS_METRICS_TOKEN" in configfile.SENSITIVE_KEYS
    assert "server.log_format" in configfile.CONFIG_KEYS
    assert "observability.metrics_enabled" in configfile.CONFIG_KEYS

    default_cfg = _settings()
    assert default_cfg.log_format == "text"
    assert default_cfg.metrics_enabled is False
    assert default_cfg.metrics_token == ""
    assert default_cfg.otel_enabled is False

    custom_cfg = _settings(
        AVELIS_LOG_FORMAT="json",
        AVELIS_METRICS_ENABLED="true",
        AVELIS_METRICS_TOKEN="  scrape-token-abc  ",
        AVELIS_OTEL_ENABLED="true",
        AVELIS_OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:4318/v1/traces",
    )
    assert custom_cfg.log_format == "json"
    assert custom_cfg.metrics_enabled is True
    assert custom_cfg.metrics_token == "scrape-token-abc"
    assert custom_cfg.otel_enabled is True
    assert custom_cfg.otel_endpoint == "http://127.0.0.1:4318/v1/traces"

    with pytest.raises(config.ConfigError, match="AVELIS_LOG_FORMAT"):
        _settings(AVELIS_LOG_FORMAT="xml")


def test_metrics_disabled_returns_404_even_with_frontend_spa(tmp_path):
    observability.METRICS.reset()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>SPA</body></html>", encoding="utf-8")

    app = app_mod.create_app(settings=_settings(AVELIS_METRICS_ENABLED="false", AVELIS_FRONTEND_DIR=str(dist)))
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 404
        spa = client.get("/some-ui-route")
        assert spa.status_code == 200 and "SPA" in spa.text


def test_metrics_token_auth_and_full_prometheus_exposition(monkeypatch, caplog):
    observability.METRICS.reset()
    monkeypatch.setattr("avelis.observability.context.get_settings", lambda: _settings())
    engine, session = _sqlite_session()
    try:
        app = app_mod.create_app(
            settings=_settings(AVELIS_METRICS_ENABLED="true", AVELIS_METRICS_TOKEN="scrape-secret-123")
        )
        app.dependency_overrides[get_session] = lambda: session
        observability.METRICS.observe_ask("answered", 0.42)
        observability.METRICS.observe_llm("ask", "ok", duration_seconds=0.85, input_tokens=120, output_tokens=45)
        observability.METRICS.observe_datasource_query("starrocks", "ok", 0.03)
        observability.METRICS.observe_job("collect", "succeeded", 1.5)

        with caplog.at_level(logging.DEBUG, logger="avelis.access"):
            with TestClient(app) as client:
                assert client.get("/api/health").status_code == 200
                assert client.get("/metrics").status_code == 401
                assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
                res = client.get("/metrics", headers={"Authorization": "Bearer scrape-secret-123"})
                assert res.status_code == 200
                assert res.headers["content-type"].startswith("text/plain")
                text = res.text

        assert 'avelis_http_requests_total{method="GET",path="/api/health",status="200"}' in text
        assert 'avelis_http_request_duration_seconds_bucket{le="+Inf",method="GET",path="/api/health"}' in text
        assert "avelis_http_requests_in_flight 1" in text
        assert "avelis_http_requests_in_flight 0" in observability.METRICS.render_prometheus()
        assert 'avelis_ask_requests_total{status="answered"} 1' in text
        assert 'avelis_ask_duration_seconds_count{status="answered"} 1' in text
        assert 'avelis_llm_calls_total{purpose="ask",status="ok"} 1' in text
        assert 'avelis_llm_call_duration_seconds_count{purpose="ask"} 1' in text
        assert 'avelis_llm_tokens_total{purpose="ask",type="input"} 120' in text
        assert 'avelis_llm_tokens_total{purpose="ask",type="output"} 45' in text
        assert 'avelis_datasource_queries_total{engine="starrocks",status="ok"} 1' in text
        assert 'avelis_datasource_query_duration_seconds_count{engine="starrocks"} 1' in text
        assert 'avelis_job_runs_total{kind="collect",status="succeeded"} 1' in text
        assert 'avelis_job_duration_seconds_count{kind="collect"} 1' in text
        assert "avelis_process_uptime_seconds " in text
        assert "avelis_build_info{" in text

        # 探针成功请求记录为 DEBUG，401 记录为 WARNING
        probe_records = [r for r in caplog.records if getattr(r, "route", None) == "/api/health"]
        unauth_records = [r for r in caplog.records if getattr(r, "status_code", None) == 401]
        assert probe_records and probe_records[0].levelno == logging.DEBUG
        assert unauth_records and unauth_records[0].levelno == logging.WARNING
    finally:
        session.close()
        engine.dispose()


def test_metrics_admin_rbac_and_route_template_bucketing(monkeypatch):
    observability.METRICS.reset()
    cfg = _settings(AVELIS_METRICS_ENABLED="true")
    monkeypatch.setattr("avelis.observability.context.get_settings", lambda: cfg)
    monkeypatch.setattr("avelis.security.get_settings", lambda: cfg)
    monkeypatch.setattr("avelis.cache.is_revoked", lambda _jti: False)

    engine, session = _sqlite_session()
    try:
        admin = User(id="u_adm", username="adm", display_name="Adm", password_hash="x", is_active=True)
        analyst = User(id="u_ana", username="ana", display_name="Ana", password_hash="x", is_active=True)
        session.add_all(
            [
                admin,
                analyst,
                UserRole(user_id="u_adm", role="admin"),
                UserRole(user_id="u_ana", role="analyst"),
            ]
        )
        session.commit()

        adm_tok, _, _ = create_access_token("u_adm", "adm")
        ana_tok, _, _ = create_access_token("u_ana", "ana")

        app = app_mod.create_app(settings=cfg)
        app.dependency_overrides[get_session] = lambda: session
        with TestClient(app) as client:
            assert client.get("/metrics").status_code == 401
            assert client.get("/metrics", headers={"Authorization": f"Bearer {ana_tok}"}).status_code == 403
            ok = client.get("/metrics", headers={"Authorization": f"Bearer {adm_tok}"})
            assert ok.status_code == 200
            assert client.get("/no-such-path-abc123").status_code == 404
            assert client.get("/no-such-path-xyz999").status_code == 404
            metrics_text = client.get("/metrics", headers={"Authorization": f"Bearer {adm_tok}"}).text
            assert 'path="unmatched"' in metrics_text
            assert "no-such-path-abc123" not in metrics_text
            assert "no-such-path-xyz999" not in metrics_text
    finally:
        session.close()
        engine.dispose()


def test_init_tracing_disabled_and_missing_optional_deps_warns(caplog):
    disabled = SimpleNamespace(otel_enabled=False, otel_endpoint="")
    assert observability.init_tracing(object(), disabled) is False

    enabled = SimpleNamespace(otel_enabled=True, otel_endpoint="")
    with caplog.at_level(logging.WARNING, logger="avelis.observability.tracing"):
        res = observability.init_tracing(object(), enabled)
    assert res is False
    assert "opentelemetry" in caplog.text
