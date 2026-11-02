"""真实 PostgreSQL 的事件迁移往返与失败留底哈希链。

仅显式设置 AVELIS_EVENTS_TEST_DATABASE_URL 时运行；在随机独立 schema 中建表、迁移并清理，
不读取 .env，不使用现有 meta_db 夹具，也不重置任何已有数据库或 schema。
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from avelis import audit, events
from avelis.history import integrity
from avelis.models import AuditEvent, EventOutbox, User, WebhookSubscription

pytestmark = pytest.mark.db


@pytest.fixture
def isolated_event_schema():
    url = os.environ.get("AVELIS_EVENTS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("未显式设置 AVELIS_EVENTS_TEST_DATABASE_URL；不连接现有元数据库")
    assert make_url(url).get_backend_name() == "postgresql"
    schema = f"events_{uuid.uuid4().hex}"
    admin = create_engine(url, connect_args={"connect_timeout": 3})
    engine = None
    created = False
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        created = True
        engine = create_engine(
            url, connect_args={"options": f"-csearch_path={schema} -ctimezone=UTC", "connect_timeout": 3}
        )
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            # schema 只由本夹具的固定前缀和随机十六进制后缀构成，且本次确实创建成功。
            assert schema.startswith("events_") and len(schema) == 39
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def migrate(engine, revision, *, downgrade=False):
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        if downgrade:
            command.downgrade(config, revision)
        else:
            command.upgrade(config, revision)


def assert_history(engine, expected_hash):
    with Session(engine) as session:
        old = session.get(AuditEvent, "old-audit")
        assert old.row_hash == expected_hash
        report = integrity.verify(session)
        assert report["ok"], report["issues"]


def audit_columns(connection):
    return [
        (column["name"], str(column["type"]), column["nullable"])
        for column in inspect(connection).get_columns("audit_event")
    ]


def test_events_migration_preserves_old_hash_and_independent_failure_is_sealed(isolated_event_schema, monkeypatch):
    engine = isolated_event_schema
    migrate(engine, "0019_metric_ai_context")
    with Session(engine) as session:
        session.add(
            AuditEvent(
                id="old-audit",
                actor_name="system",
                action="historical.baseline",
                object_type="system",
                detail={"safe": "old unchanged detail"},
            )
        )
        session.commit()
        old_hash = session.get(AuditEvent, "old-audit").row_hash
        old_seq = session.get(AuditEvent, "old-audit").chain_seq
        assert old_hash is not None and old_seq == 1
    assert_history(engine, old_hash)
    with engine.connect() as connection:
        old_columns = audit_columns(connection)

    migrate(engine, "0020_governance_events")
    assert_history(engine, old_hash)
    with engine.connect() as connection:
        assert audit_columns(connection) == old_columns
        assert {"event_outbox", "webhook_subscription"} <= set(inspect(connection).get_table_names())
    migrate(engine, "0019_metric_ai_context", downgrade=True)
    assert_history(engine, old_hash)
    with engine.connect() as connection:
        assert {"event_outbox", "webhook_subscription"}.isdisjoint(inspect(connection).get_table_names())
    migrate(engine, "0020_governance_events")
    assert_history(engine, old_hash)

    @contextmanager
    def independent_scope():
        with Session(engine) as session:
            with session.begin():
                yield session

    monkeypatch.setattr(events, "session_scope", independent_scope)
    with Session(engine) as session:
        session.add(
            WebhookSubscription(
                id="failure-subscription",
                name="失败订阅",
                url="https://example.invalid/events",
                event_types=[events.EventType.METRIC_VERSION_PUBLISHED.value],
                outcomes=["failure"],
                secret_enc="offline-encrypted-secret",
            )
        )
        session.commit()
    original = RuntimeError("business failure")
    with Session(engine) as business:
        with pytest.raises(RuntimeError) as caught:
            with events.failure_guard(
                events.EventType.METRIC_VERSION_PUBLISHED, None, "metric_version", "version", session=business
            ):
                business.add(User(id="rolled-back", username="rolled-back", display_name="回滚用户", password_hash="x"))
                audit.record(business, None, "metric_version.approve", "metric_version", "version")
                business.flush()
                raise original
        assert caught.value is original
    with Session(engine) as session:
        assert session.get(User, "rolled-back") is None
        rows = list(session.scalars(select(AuditEvent).where(AuditEvent.id != "old-audit")))
        assert len(rows) == 1
        failure = rows[0]
        assert failure.detail["_governance"]["outcome"] == "failure"
        assert failure.chain_seq == old_seq + 1
        assert failure.prev_hash == old_hash
        assert failure.row_hash is not None
        delivery = session.scalars(select(EventOutbox)).one()
        assert delivery.event_id == failure.id
        assert delivery.payload["outcome"] == "failure"
    assert_history(engine, old_hash)
