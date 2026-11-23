"""Auth extensions migration (0027): PostgreSQL DDL, SQLite roundtrip, and model parity."""

import importlib.util
import io
from datetime import datetime, timezone
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import JSON, Boolean, Column, DateTime, MetaData, String, Table, create_engine, inspect, select

from avelis.models import PersonalAccessToken, User


def load_migration():
    path = Path(__file__).resolve().parents[2] / "src/avelis/migrations/versions/0027_auth_extensions.py"
    spec = importlib.util.spec_from_file_location("migration_0027", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _baseline_app_user(metadata: MetaData) -> Table:
    return Table(
        "app_user",
        metadata,
        Column("id", String(32), primary_key=True),
        Column("username", String(64), unique=True, nullable=False),
        Column("display_name", String(128), nullable=False),
        Column("password_hash", String(255), nullable=False),
        Column("is_active", Boolean, nullable=False),
        Column("must_change_password", Boolean, nullable=False),
        Column("password_changed_at", DateTime),
        Column("last_login_at", DateTime),
        Column("sessions_valid_after", DateTime),
        Column("created_at", DateTime, nullable=False),
    )


def test_auth_extensions_migration_revision_and_postgresql_ddl():
    migration = load_migration()
    assert migration.revision == "0027_auth_extensions"
    assert migration.down_revision == "0026_skills"
    ddl = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": ddl})
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    sql = ddl.getvalue()
    assert "ALTER TABLE app_user ADD COLUMN email VARCHAR(255)" in sql
    assert "ALTER TABLE app_user ADD COLUMN auth_provider VARCHAR(32) DEFAULT 'local' NOT NULL" in sql
    assert "ALTER TABLE app_user ADD COLUMN totp_enabled BOOLEAN DEFAULT false NOT NULL" in sql
    assert "CREATE TABLE personal_access_token" in sql
    assert "scopes JSONB DEFAULT '[]' NOT NULL" in sql
    assert "REFERENCES app_user (id)" in sql
    assert "UNIQUE (token_hash)" in sql
    assert "uq_app_user_external_identity" in sql
    assert "DROP TABLE personal_access_token" in sql


def test_auth_extensions_migration_roundtrip_and_model_schema_agree(monkeypatch):
    migration = load_migration()
    monkeypatch.setattr(migration, "JsonType", JSON())
    engine = create_engine("sqlite://")
    metadata = MetaData()
    original = _baseline_app_user(metadata)
    metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            connection.execute(
                original.insert(),
                {
                    "id": "existing-user",
                    "username": "alice",
                    "display_name": "Alice",
                    "password_hash": "hash",
                    "is_active": True,
                    "must_change_password": False,
                    "created_at": datetime.now(timezone.utc),
                },
            )
            for _ in range(2):
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                inspector = inspect(connection)
                assert set(inspector.get_table_names()) == {"app_user", "personal_access_token"}

                for model in (User, PersonalAccessToken):
                    table = model.__table__
                    columns = {c["name"]: c for c in inspector.get_columns(table.name)}
                    assert set(columns) == set(table.c.keys())
                    for column in table.c:
                        assert columns[column.name]["nullable"] == column.nullable

                pat_table = PersonalAccessToken.__table__
                assert set(inspector.get_pk_constraint(pat_table.name)["constrained_columns"]) == {"id"}
                assert {
                    (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
                    for fk in inspector.get_foreign_keys(pat_table.name)
                } == {(("user_id",), "app_user", ("id",))}

                user_uniques = {tuple(u["column_names"]) for u in inspector.get_unique_constraints("app_user")}
                assert ("external_issuer", "external_sub") in user_uniques
                user_indexes = {
                    (tuple(i["column_names"]), bool(i["unique"])) for i in inspector.get_indexes("app_user")
                }
                assert (("email",), False) in user_indexes

                with Operations.context(MigrationContext.configure(connection)):
                    migration.downgrade()
                assert set(inspect(connection).get_table_names()) == {"app_user"}
                assert set(c["name"] for c in inspect(connection).get_columns("app_user")) == set(original.c.keys())
                assert connection.scalar(select(original.c.username)) == "alice"
    finally:
        engine.dispose()
