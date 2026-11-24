"""M4C 单元测试：请求上下文防篡改审计、敏感查看/导出审计、安全事件外发与用户匿名化注销。"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis import audit, config
from avelis.api import analysis as analysis_api
from avelis.api import ask as ask_api
from avelis.api import auth as auth_api
from avelis.api.deps import current_user, get_session
from avelis.db import Base, utcnow
from avelis.governance.anonymize import anonymize_user
from avelis.history import retention
from avelis.models import (
    AnalysisDataset,
    AnalysisRun,
    Answer,
    AuditEvent,
    OAuthClient,
    OAuthGrant,
    OAuthToken,
    PersonalAccessToken,
    User,
    UserRole,
    WebhookSubscription,
)
from avelis.observability import RequestContextMiddleware


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


def test_request_context_middleware_seals_audit_and_audit_api_filters(monkeypatch):
    engine, session = _sqlite_session()
    try:
        s_cfg = config.build_settings(
            {"AVELIS_SECRET": "m4c-secret-0123456789abcdef", "AVELIS_FORWARDED_ALLOW_IPS": "127.0.0.1"}
        )
        monkeypatch.setattr("avelis.observability.context.get_settings", lambda: s_cfg)

        admin = User(id="u_adm", username="admin", display_name="Admin", password_hash="x", is_active=True)
        session.add_all([admin, UserRole(user_id="u_adm", role="admin")])
        session.commit()

        app = FastAPI()
        app.add_middleware(RequestContextMiddleware)
        app.include_router(ask_api.router)
        app.dependency_overrides[current_user] = lambda: admin
        app.dependency_overrides[get_session] = lambda: session

        @app.post("/api/test-audit-ping")
        def _ping(user: User = Depends(current_user), db: Session = Depends(get_session)):
            ev = audit.record(db, user, "test.ping", "user", user.id, note="ok")
            db.commit()
            return {"audit_id": ev.id}

        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            r1 = client.post(
                "/api/test-audit-ping",
                headers={
                    "X-Request-ID": "req-custom-001",
                    "X-Forwarded-For": "203.0.113.9, 10.0.0.1",
                    "User-Agent": "AvelisTestClient/1.0",
                },
            )
            assert r1.status_code == 200
            assert r1.headers["x-request-id"] == "req-custom-001"

            # 非法 X-Request-ID 会被替换为安全 UUID
            r2 = client.post("/api/test-audit-ping", headers={"X-Request-ID": "bad\nreq id!"})
            assert r2.status_code == 200
            gen_rid = r2.headers["x-request-id"]
            assert gen_rid != "bad\nreq id!" and len(gen_rid) == 32

            # GET /api/audit 按 request_id 与 client_ip 精确过滤并返回顶层溯源字段
            res = client.get("/api/audit", params={"request_id": "req-custom-001", "client_ip": "203.0.113.9"})
            assert res.status_code == 200
            rows = res.json()
            assert len(rows) == 1
            assert rows[0]["request_id"] == "req-custom-001"
            assert rows[0]["client_ip"] == "203.0.113.9"
            assert rows[0]["user_agent"] == "AvelisTestClient/1.0"
            assert rows[0]["actor"] == "admin"
    finally:
        session.close()
        engine.dispose()


def test_sensitive_view_and_export_emit_audit_events(monkeypatch):
    engine, session = _sqlite_session()
    try:
        alice = User(id="u_alice", username="alice", display_name="Alice", password_hash="x", is_active=True)
        ans = Answer(
            id="ans1",
            question="销售额？",
            asked_by="u_alice",
            asked_at=utcnow(),
            status="answered",
            model_adapter="rule",
            result={"columns": ["value"], "rows": [[100]], "row_count": 1, "truncated": False},
        )
        run = AnalysisRun(
            id="run1",
            user_id="u_alice",
            question="为什么增长？",
            trigger="manual",
            status="succeeded",
        )
        ds = AnalysisDataset(
            id="ds1",
            run_id="run1",
            seq=1,
            kind="governed",
            title="D1",
            columns=["metric", "value"],
            rows=[["销售额", 100]],
            truncated=False,
        )
        session.add_all([alice, ans, run, ds])
        session.commit()

        app = FastAPI()
        app.add_middleware(RequestContextMiddleware)
        app.include_router(ask_api.router)
        app.include_router(analysis_api.router)
        app.dependency_overrides[current_user] = lambda: alice
        app.dependency_overrides[get_session] = lambda: session
        monkeypatch.setattr(analysis_api.analysis, "visible_run", lambda s, u, rid: run)
        monkeypatch.setattr(analysis_api, "require_dataset_access", lambda s, u, d: None)
        monkeypatch.setattr(
            analysis_api.analysis,
            "run_view",
            lambda s, r, user=None: {
                "id": r.id,
                "question": r.question,
                "status": "succeeded",
                "datasets": [],
                "report": {"findings": [], "title": "t"},
                "meta": {},
            },
        )
        monkeypatch.setattr(analysis_api.export, "sources_csv", lambda view: "col\n")

        with TestClient(app) as client:
            r_ans = client.get("/api/answers/ans1", headers={"X-Request-ID": "req-view-1"})
            assert r_ans.status_code == 200
            r_csv = client.get("/api/analysis/runs/run1/datasets/1/csv", headers={"X-Request-ID": "req-exp-1"})
            assert r_csv.status_code == 200
            r_src = client.get(
                "/api/analysis/runs/run1/export", params={"format": "csv"}, headers={"X-Request-ID": "req-exp-2"}
            )
            assert r_src.status_code == 200

        actions = list(session.scalars(select(AuditEvent).order_by(AuditEvent.at)))
        assert [(e.action, e.detail.get("request_id")) for e in actions] == [
            ("answer.view", "req-view-1"),
            ("analysis.export", "req-exp-1"),
            ("analysis.export", "req-exp-2"),
        ]
    finally:
        session.close()
        engine.dispose()


def test_security_webhook_events_enqueue_outbox():
    engine, session = _sqlite_session()
    try:
        from avelis.models import EventOutbox

        sub = WebhookSubscription(
            id="wh1",
            name="sec-alerts",
            url="https://hooks.invalid/sec",
            event_types=["auth.login_failed", "user.anonymized", "data_policy.changed", "analysis.exported"],
            secret_enc="enc",
            enabled=True,
        )
        session.add(sub)
        session.flush()

        actor = User(id="u1", username="alice", display_name="Alice", password_hash="x", is_active=True)
        audit.record(session, None, "auth.login_failed", "user", "u1", username="alice", failures=3)
        audit.record(session, actor, "asset_rls_policy.create", "asset", "a1", policy_id="p1")
        audit.record(session, actor, "user.anonymized", "user", "u2")
        audit.record(session, actor, "analysis.export", "analysis_run", "r1", format="csv")
        session.flush()

        outbox = list(session.scalars(select(EventOutbox).order_by(EventOutbox.id)))
        types = sorted(o.payload["type"] for o in outbox)
        assert types == ["analysis.exported", "auth.login_failed", "data_policy.changed", "user.anonymized"]
        login_fail = next(o for o in outbox if o.payload["type"] == "auth.login_failed")
        assert login_fail.payload["outcome"] == "failure" and login_fail.payload["error"]["code"] == "AUTH_LOGIN_FAILED"
    finally:
        session.close()
        engine.dispose()


def test_anonymize_user_preserves_audit_chain_and_revokes_tokens():
    engine, session = _sqlite_session()
    try:
        adm1 = User(id="u_adm1", username="admin1", display_name="Admin 1", password_hash="x", is_active=True)
        adm2 = User(
            id="u_adm2",
            username="alice",
            display_name="Alice",
            email="alice@corp.com",
            password_hash="hash_pw",
            totp_enabled=True,
            totp_secret_enc="enc_totp",
            is_active=True,
        )
        pat = PersonalAccessToken(
            id="pat1", user_id="u_adm2", name="cli", token_prefix="avp_", token_hash="h_pat", scopes=["ask:read"]
        )
        client_row = OAuthClient(
            id="oc1",
            client_id="cid1",
            client_name="MCP",
            redirect_uris=["http://localhost/cb"],
            grant_types=["authorization_code"],
            response_types=["code"],
            scope="mcp",
            token_endpoint_auth_method="none",
        )
        grant = OAuthGrant(
            id="og1",
            client_record_id="oc1",
            user_id="u_adm2",
            scope="mcp",
        )
        tok = OAuthToken(
            id="ot1",
            access_token_hash="h_acc",
            refresh_token_hash="h_ref",
            client_record_id="oc1",
            client_id="cid1",
            user_id="u_adm2",
            grant_id="og1",
            family_id="fam1",
            scope="mcp",
            resource="https://avelis.local/mcp",
            expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        )
        session.add_all(
            [
                adm1,
                adm2,
                UserRole(user_id="u_adm1", role="admin"),
                UserRole(user_id="u_adm2", role="admin"),
                pat,
                client_row,
                grant,
                tok,
            ]
        )
        session.commit()

        # 先记一条既有审计
        prior = audit.record(session, adm2, "ask", "answer", "a1")
        session.commit()
        prior_actor_name = prior.actor_name

        # 拒绝匿名化本人
        with pytest.raises(ValueError, match="当前登录账号"):
            anonymize_user(session, "u_adm1", actor=adm1)

        # 通过管理 API 匿名化 alice
        app = FastAPI()
        app.include_router(auth_api.router)
        app.dependency_overrides[current_user] = lambda: adm1
        app.dependency_overrides[get_session] = lambda: session
        with TestClient(app) as client:
            r = client.post("/api/users/u_adm2/anonymize")
            assert r.status_code == 200
            body = r.json()
            assert body["is_active"] is False
            assert body["username"].startswith("anonymized_")
            assert body["display_name"].startswith("已匿名用户 (")

        # 最后一个活跃管理员 u_adm1 也不可被系统匿名化
        with pytest.raises(ValueError, match="最后一个活跃管理员"):
            anonymize_user(session, "admin1", actor=None)

        session.refresh(adm2)
        session.refresh(pat)
        session.refresh(tok)
        assert adm2.password_hash == "!"
        assert adm2.email is None
        assert adm2.totp_enabled is False and adm2.totp_secret_enc is None
        assert adm2.sessions_valid_after is not None
        assert pat.revoked_at is not None
        assert tok.access_revoked_at is not None and tok.refresh_revoked_at is not None
        assert tok.revocation_reason == "user_anonymized"

        # 原有审计历史行完全保持不变（不破坏哈希链），同时新增 user.anonymized 审计
        session.refresh(prior)
        assert prior.actor_name == prior_actor_name
        assert session.scalar(select(AuditEvent).where(AuditEvent.action == "user.anonymized")) is not None
    finally:
        session.close()
        engine.dispose()


def test_retention_plan_includes_answer_and_analysis_dataset(monkeypatch):
    assert "analysis_dataset" in retention.ORDER and "answer" in retention.ORDER
    assert retention.ORDER.index("analysis_dataset") < retention.ORDER.index("answer")

    captured: list[tuple[str, str]] = []

    def fake_ids(_session, sql, **params):
        captured.append((sql.strip(), str(sorted(params.keys()))))
        return []

    monkeypatch.setattr(retention, "_ids", fake_ids)
    retention._plan(SimpleNamespace(), datetime(2025, 1, 1, tzinfo=timezone.utc))
    sql_blob = "\n".join(sql for sql, _ in captured)
    assert "FROM analysis_dataset d" in sql_blob
    assert "FROM answer a" in sql_blob and "verified_query vq" in sql_blob
