"""治理事件迁移：离线 PostgreSQL DDL、SQLite 往返与历史摘要兼容。"""

import importlib.util
import io
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import JSON, Column, MetaData, String, Table, create_engine, inspect, select
from sqlalchemy.exc import IntegrityError

from avelis.db import Base
from avelis.models import EventOutbox, WebhookSubscription


def load_migration():
    path = Path(__file__).resolve().parents[2] / "src/avelis/migrations/versions/0020_governance_events.py"
    spec = importlib.util.spec_from_file_location("migration_0020_events", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_governance_events_migration_compiles_postgresql_upgrade_and_downgrade():
    migration = load_migration()
    assert migration.revision == "0020_governance_events"
    assert migration.down_revision == "0019_metric_ai_context"
    ddl = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": ddl, "target_metadata": Base.metadata}
    )
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    sql = ddl.getvalue()
    assert "CREATE TABLE webhook_subscription" in sql
    assert "CREATE TABLE event_outbox" in sql
    assert "event_types JSONB DEFAULT '[]' NOT NULL" in sql
    assert "payload JSONB NOT NULL" in sql
    assert "secret_enc TEXT NOT NULL" in sql
    assert "UNIQUE (subscription_id, event_id)" in sql
    assert "FOREIGN KEY(subscription_id) REFERENCES webhook_subscription (id) ON DELETE CASCADE" in sql
    assert "CHECK (status IN ('pending', 'delivered', 'dead'))" in sql
    assert "CHECK (attempts >= 0)" in sql
    assert "CREATE INDEX ix_event_outbox_due ON event_outbox (status, next_attempt_at)" in sql
    assert "CREATE INDEX ix_event_outbox_subscription_id ON event_outbox (subscription_id)" in sql
    assert "DROP TABLE event_outbox" in sql
    assert "DROP TABLE webhook_subscription" in sql
    assert "audit_event" not in sql
    assert "history_chain_head" not in sql
    assert "FUNCTION" not in sql


@pytest.fixture
def migrated_events(monkeypatch):
    migration = load_migration()
    # SQLite 验证通用 DDL 和约束；上面的 PostgreSQL 编译覆盖 JSONB 的目标类型。
    monkeypatch.setattr(migration, "JsonType", JSON())
    engine = create_engine("sqlite://")
    metadata = MetaData()
    users = Table("app_user", metadata, Column("id", String(32), primary_key=True))
    audit = Table(
        "audit_event",
        metadata,
        Column("id", String(32), primary_key=True),
        Column("detail", JSON(), nullable=False),
        Column("row_hash", String(64), nullable=False),
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        metadata.create_all(connection)
        connection.execute(users.insert(), {"id": "admin"})
        connection.execute(audit.insert(), {"id": "old", "detail": {"safe": "existing"}, "row_hash": "a" * 64})
        context = MigrationContext.configure(connection, opts={"target_metadata": Base.metadata})
        with Operations.context(context):
            migration.upgrade()
    try:
        yield engine, migration
    finally:
        engine.dispose()


def tables(connection):
    metadata = MetaData()
    subscription = Table("webhook_subscription", metadata, autoload_with=connection)
    outbox = Table("event_outbox", metadata, autoload_with=connection)
    return subscription, outbox


def add_subscription(connection, subscription, identifier="subscription"):
    connection.execute(
        subscription.insert(),
        {
            "id": identifier,
            "name": "离线订阅",
            "url": "https://example.invalid/events",
            "event_types": ["metric_version.published"],
            "outcomes": ["success"],
            "secret_enc": "encrypted-secret",
            "created_by": "admin",
        },
    )


def test_governance_events_migration_roundtrip_preserves_audit_and_stored_snapshots(migrated_events):
    engine, migration = migrated_events
    with engine.begin() as connection:
        subscription, outbox = tables(connection)
        add_subscription(connection, subscription)
        payload = {"id": "standalone-event", "type": "metric_version.published", "detail": {"version": 2}}
        # event_id 可以独立于审计表，历史保留期清理不受发件箱外键阻碍。
        connection.execute(
            outbox.insert(),
            {"id": "delivery", "event_id": payload["id"], "subscription_id": "subscription", "payload": payload},
        )
        saved = connection.execute(select(subscription)).mappings().one()
        assert saved["event_types"] == ["metric_version.published"]
        assert saved["outcomes"] == ["success"]
        assert saved["enabled"] is True
        assert saved["created_at"] is not None
        assert saved["updated_at"] is not None
        delivery = connection.execute(select(outbox)).mappings().one()
        assert delivery["payload"] == payload
        assert delivery["status"] == "pending"
        assert delivery["attempts"] == 0
        assert delivery["next_attempt_at"] is not None
        assert delivery["created_at"] is not None
        assert delivery["delivered_at"] is None
        assert delivery["last_error"] is None
        for model, table in ((WebhookSubscription, subscription), (EventOutbox, outbox)):
            # 0020 predates the nullable dead_at column introduced by 0024.
            columns = set(model.__table__.columns.keys())
            if model is EventOutbox:
                columns -= {"dead_at"}
            assert columns == set(table.columns.keys())
        foreign_keys = inspect(connection).get_foreign_keys("event_outbox")
        assert len(foreign_keys) == 1
        assert foreign_keys[0]["referred_table"] == "webhook_subscription"
        assert foreign_keys[0]["options"]["ondelete"] == "CASCADE"
        audit = Table("audit_event", MetaData(), autoload_with=connection)
        expected_audit = {"id": "old", "detail": {"safe": "existing"}, "row_hash": "a" * 64}
        assert connection.execute(select(audit)).mappings().one() == expected_audit
        context = MigrationContext.configure(connection, opts={"target_metadata": Base.metadata})
        with Operations.context(context):
            migration.downgrade()
        assert set(inspect(connection).get_table_names()) == {"app_user", "audit_event"}
        assert connection.execute(select(audit)).mappings().one() == expected_audit
        assert [column["name"] for column in inspect(connection).get_columns("audit_event")] == [
            "id",
            "detail",
            "row_hash",
        ]
        with Operations.context(context):
            migration.upgrade()
        subscription, outbox = tables(connection)
        assert not list(connection.execute(select(subscription)))
        assert not list(connection.execute(select(outbox)))
        assert connection.execute(select(audit)).mappings().one() == expected_audit


def test_governance_events_migration_enforces_deduplication_and_cascading_delete(migrated_events):
    engine, _ = migrated_events
    with engine.begin() as connection:
        subscription, outbox = tables(connection)
        add_subscription(connection, subscription)
        add_subscription(connection, subscription, "other")
        values = {"event_id": "event", "subscription_id": "subscription", "payload": {"id": "event"}}
        connection.execute(outbox.insert(), {"id": "first", **values})
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(outbox.insert(), {"id": "duplicate", **values})
        connection.execute(outbox.insert(), {"id": "other-delivery", **values, "subscription_id": "other"})
        connection.execute(subscription.delete().where(subscription.c.id == "subscription"))
        assert list(connection.scalars(select(outbox.c.id))) == ["other-delivery"]


@pytest.mark.parametrize(
    "invalid",
    [{"status": "processing"}, {"attempts": -1}, {"subscription_id": "missing"}],
)
def test_governance_events_migration_rejects_invalid_delivery_state(migrated_events, invalid):
    engine, _ = migrated_events
    with engine.begin() as connection:
        subscription, outbox = tables(connection)
        add_subscription(connection, subscription)
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                outbox.insert(),
                {"id": "invalid", "event_id": "event", "subscription_id": "subscription", "payload": {}, **invalid},
            )
