"""MCP OAuth 迁移：PostgreSQL DDL、SQLite 往返与模型约束一致性。"""

import importlib.util
import io
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import JSON, Column, MetaData, String, Table, create_engine, inspect, select

from avelis.models import OAuthAuthorizationCode, OAuthClient, OAuthConsentTicket, OAuthGrant, OAuthToken

MODELS = (OAuthClient, OAuthGrant, OAuthConsentTicket, OAuthAuthorizationCode, OAuthToken)


def load_migration():
    path = Path(__file__).resolve().parents[2] / "src/avelis/migrations/versions/0021_mcp_oauth.py"
    spec = importlib.util.spec_from_file_location("migration_0021", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_oauth_migration_revision_and_postgresql_ddl():
    migration = load_migration()
    assert migration.revision == "0021_mcp_oauth"
    assert migration.down_revision == "0019_metric_ai_context"
    ddl = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": ddl})
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    sql = ddl.getvalue()
    for model in MODELS:
        assert f"CREATE TABLE {model.__tablename__}" in sql
        assert f"DROP TABLE {model.__tablename__}" in sql
    assert "redirect_uris JSONB NOT NULL" in sql
    assert "parameters JSONB NOT NULL" in sql
    assert "REFERENCES app_user (id)" in sql
    assert "REFERENCES oauth_token (id)" in sql
    assert "UNIQUE (ticket_hash)" in sql
    assert "UNIQUE (code_hash)" in sql
    assert "UNIQUE (access_token_hash)" in sql
    assert "UNIQUE (refresh_token_hash)" in sql


def test_oauth_migration_roundtrip_and_model_schema_agree(monkeypatch):
    migration = load_migration()
    # SQLite 验证表结构与约束；PostgreSQL JSONB 由 DDL 编译单独验证。
    monkeypatch.setattr(migration, "JsonType", JSON())
    engine = create_engine("sqlite://")
    metadata = MetaData()
    original = Table("app_user", metadata, Column("id", String(32), primary_key=True))
    metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            connection.execute(original.insert(), {"id": "existing-user"})
            for _ in range(2):
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                inspector = inspect(connection)
                assert set(inspector.get_table_names()) == {"app_user", *(model.__tablename__ for model in MODELS)}
                for model in MODELS:
                    table = model.__table__
                    columns = {column["name"]: column for column in inspector.get_columns(table.name)}
                    assert set(columns) == set(table.c.keys())
                    for column in table.c:
                        assert columns[column.name]["nullable"] == column.nullable
                        expected_type = (
                            "JSON"
                            if column.type.__class__.__name__ == "JSONB"
                            else str(column.type.compile(connection.dialect))
                        )
                        assert str(columns[column.name]["type"]) == expected_type
                    assert set(inspector.get_pk_constraint(table.name)["constrained_columns"]) == {
                        column.name for column in table.primary_key
                    }
                    assert {
                        tuple(unique["column_names"]) for unique in inspector.get_unique_constraints(table.name)
                    } == {
                        tuple(column.name for column in constraint.columns)
                        for constraint in table.constraints
                        if constraint.__class__.__name__ == "UniqueConstraint"
                    }
                    assert {
                        (tuple(index["column_names"]), bool(index["unique"]))
                        for index in inspector.get_indexes(table.name)
                    } == {
                        (tuple(column.name for column in index.columns), bool(index.unique)) for index in table.indexes
                    }
                    assert {
                        (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
                        for fk in inspector.get_foreign_keys(table.name)
                    } == {
                        (
                            tuple(element.parent.name for element in constraint.elements),
                            constraint.referred_table.name,
                            tuple(element.column.name for element in constraint.elements),
                        )
                        for constraint in table.foreign_key_constraints
                    }
                with Operations.context(MigrationContext.configure(connection)):
                    migration.downgrade()
                assert inspect(connection).get_table_names() == ["app_user"]
                assert connection.scalar(select(original.c.id)) == "existing-user"
    finally:
        engine.dispose()
