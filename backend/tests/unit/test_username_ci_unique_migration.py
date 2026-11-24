"""Username case-insensitive uniqueness migration (0032): PostgreSQL DDL, conflict detection, and SQLite roundtrip."""

from __future__ import annotations

import importlib.util
import io
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Boolean, Column, DateTime, MetaData, String, Table, create_engine, select, text
from sqlalchemy.exc import IntegrityError


def _load_migration():
    path = Path(__file__).resolve().parents[2] / "src/avelis/migrations/versions/0032_username_ci_unique.py"
    spec = importlib.util.spec_from_file_location("migration_0032", path)
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)
    return migration


def _app_user_table(metadata: MetaData) -> Table:
    return Table(
        "app_user",
        metadata,
        Column("id", String(32), primary_key=True),
        Column("username", String(64), unique=True, nullable=False),
        Column("display_name", String(128), nullable=False),
        Column("auth_provider", String(32), server_default="local", nullable=False),
        Column("password_hash", String(255), nullable=False),
        Column("is_active", Boolean, nullable=False),
        Column("must_change_password", Boolean, nullable=False),
        Column("created_at", DateTime, nullable=False),
    )


def test_migration_0032_postgresql_ddl():
    migration = _load_migration()
    assert migration.revision == "0032_username_ci_unique"
    assert migration.down_revision == "0031_m4b_pii_and_rls"

    ddl = io.StringIO()
    ctx = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": ddl})
    with Operations.context(ctx):
        migration.upgrade()
        migration.downgrade()
    sql = ddl.getvalue()
    assert "CREATE UNIQUE INDEX uq_app_user_username_lower ON app_user (lower(username))" in sql
    assert "DROP INDEX uq_app_user_username_lower" in sql


def test_migration_0032_aborts_with_conflict_report_and_roundtrips_cleanly():
    migration = _load_migration()
    engine = create_engine("sqlite://")
    metadata = MetaData()
    tbl = _app_user_table(metadata)
    metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    try:
        with engine.begin() as conn:
            conn.execute(
                tbl.insert(),
                [
                    {
                        "id": "u-upper",
                        "username": "Alice",
                        "display_name": "Alice Upper",
                        "auth_provider": "local",
                        "password_hash": "h1",
                        "is_active": True,
                        "must_change_password": False,
                        "created_at": now,
                    },
                    {
                        "id": "u-lower",
                        "username": "alice",
                        "display_name": "alice lower",
                        "auth_provider": "local",
                        "password_hash": "h2",
                        "is_active": True,
                        "must_change_password": False,
                        "created_at": now,
                    },
                ],
            )
            with pytest.raises(RuntimeError) as exc_info:
                with Operations.context(MigrationContext.configure(conn)):
                    migration.upgrade()
            msg = str(exc_info.value)
            assert "u-upper" in msg and "'Alice'" in msg
            assert "u-lower" in msg and "'alice'" in msg

            # 原有两条记录均未被覆盖或删除
            remaining = conn.execute(select(tbl.c.id, tbl.c.username).order_by(tbl.c.id)).fetchall()
            assert remaining == [("u-lower", "alice"), ("u-upper", "Alice")]

            # 人工处理冲突后，迁移成功并阻止再插入仅大小写不同的重名账号
            conn.execute(tbl.update().where(tbl.c.id == "u-upper").values(username="alice_legacy"))
            for _ in range(2):
                with Operations.context(MigrationContext.configure(conn)):
                    migration.upgrade()
                idx_names = set(
                    conn.execute(text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='app_user'"))
                    .scalars()
                    .all()
                )
                assert "uq_app_user_username_lower" in idx_names

                with pytest.raises(IntegrityError):
                    with conn.begin_nested():
                        conn.execute(
                            tbl.insert(),
                            {
                                "id": "u-dup",
                                "username": "ALICE",
                                "display_name": "Dup",
                                "auth_provider": "local",
                                "password_hash": "h3",
                                "is_active": True,
                                "must_change_password": False,
                                "created_at": now,
                            },
                        )

                with Operations.context(MigrationContext.configure(conn)):
                    migration.downgrade()
                assert "uq_app_user_username_lower" not in set(
                    conn.execute(text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='app_user'"))
                    .scalars()
                    .all()
                )
    finally:
        engine.dispose()
