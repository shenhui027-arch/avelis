"""筛选对齐在独立 PostgreSQL 测试库执行真实 SQL，保留白名单与执行前核对。"""

from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from avelis import cache
from avelis.analysis import agent
from avelis.analysis.tools import Scope
from avelis.ask import service
from avelis.ask.alignment import align_filters
from avelis.ask.intent import DimensionFilter, Intent
from avelis.config import get_settings
from avelis.connectors.postgres import PostgresConnector
from avelis.db import get_engine, session_scope
from avelis.models import (
    Asset,
    AssetColumn,
    AssetVersion,
    CollectionRun,
    ColumnVersion,
    Datasource,
    JoinPath,
    JoinPathApproval,
    Metric,
    MetricVersion,
    User,
)

pytestmark = pytest.mark.db


@pytest.fixture
def alignment_pg(factory, monkeypatch, request):
    """物理表、采集列、口径和已生效路径均在 tests/db 的临时库中建立。"""
    owner = factory.user(roles=("admin",))
    metric_code = factory.metric(owner)
    schema = f"alignment_{uuid.uuid4().hex[:10]}"
    numeric_keys = getattr(request, "param", None) == "numeric"
    key_type = "integer" if numeric_keys else "text"
    settings = replace(get_settings(), ask_live_check=True)
    database = make_url(settings.database_url)
    statements = []

    class MemoryCache:
        @staticmethod
        def get(key):
            return None

        @staticmethod
        def set(key, value, **kwargs):
            pass

    class RecordingPostgresConnector(PostgresConnector):
        def execute_select(self, sql_text, **kwargs):
            assert kwargs["timeout_seconds"] == 10
            assert kwargs["max_rows"] in (3, service.DIMENSION_VALUES_LIMIT)
            statements.append(sql_text)
            return super().execute_select(sql_text, **kwargs)

    connector = RecordingPostgresConnector(
        host=database.host,
        port=database.port,
        user=database.username,
        password=database.password,
        options={"database": database.database, "schemas": [schema]},
    )
    monkeypatch.setattr(cache, "client", lambda: MemoryCache())
    monkeypatch.setattr(service, "query_connector", lambda *args: connector)

    # schema 只含固定前缀与随机十六进制字符；所有业务值都由数据库参数传入。
    with get_engine().begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        connection.exec_driver_sql(
            f'CREATE TABLE "{schema}".fact (member {key_type} NOT NULL, amount numeric NOT NULL, day date NOT NULL)'
        )
        connection.exec_driver_sql(
            f'CREATE TABLE "{schema}".dimension '
            f"(member {key_type} PRIMARY KEY, alias text NOT NULL, label text NOT NULL)"
        )
        if not numeric_keys:
            connection.execute(
                text(f'INSERT INTO "{schema}".fact (member, amount, day) VALUES (:member, :amount, :day)'),
                [
                    {"member": "member_a", "amount": 10, "day": "2026-01-01"},
                    {"member": "member_b", "amount": 20, "day": "2026-01-01"},
                ],
            )
        connection.execute(
            text(f'INSERT INTO "{schema}".dimension (member, alias, label) VALUES (:member, :alias, :label)'),
            [{"member": key, "alias": "shared_alias", "label": "shared_label"} for key in (1, 2)]
            if numeric_keys
            else [
                {"member": "member_a", "alias": "alias_a", "label": "shared_label"},
                {"member": "member_b", "alias": "x' OR 1=1 --", "label": "shared_label"},
            ],
        )

    with session_scope() as session:
        metric = session.scalar(select(Metric).where(Metric.code == metric_code))
        source = Datasource(
            name=f"{schema}_source",
            kind="postgres",
            host=database.host,
            port=database.port,
            options={"database": database.database, "schemas": [schema]},
        )
        session.add(source)
        session.flush()
        run = CollectionRun(datasource_id=source.id, trigger="manual", status="succeeded")
        session.add(run)
        session.flush()

        def asset(name, columns, *, unique=False):
            current = Asset(
                datasource_id=source.id,
                kind="table",
                catalog_name=database.database,
                db_name=schema,
                name=name,
                domain_id=metric.domain_id,
            )
            session.add(current)
            session.flush()
            observed = AssetVersion(
                asset_id=current.id,
                version_no=1,
                observed_run_id=run.id,
                last_seen_run_id=run.id,
                kind="table",
                catalog_name=database.database,
                db_name=schema,
                name=name,
                key_columns=["member"] if unique else [],
                unique_keys=[["member"]] if unique else [],
                content_hash="a" * 64,
            )
            session.add(observed)
            session.flush()
            for ordinal, (name, data_type) in enumerate(columns, 1):
                column = AssetColumn(asset_id=current.id, name=name)
                session.add(column)
                session.flush()
                session.add(
                    ColumnVersion(
                        asset_version_id=observed.id,
                        column_id=column.id,
                        ordinal=ordinal,
                        name=name,
                        data_type=data_type,
                        is_key=unique and name == "member",
                    )
                )
            current.current_version_id = observed.id
            session.flush()
            return current

        fact = asset("fact", [("member", key_type), ("amount", "numeric"), ("day", "date")])
        dimension = asset("dimension", [("member", key_type), ("alias", "text"), ("label", "text")], unique=True)
        version = MetricVersion(
            metric_id=metric.id,
            version_no=1,
            status="published",
            description="PostgreSQL alignment fixture",
            kind="base",
            base_asset_id=fact.id,
            expression="SUM(b.amount)",
            time_column="b.day",
            timezone="UTC",
            dimensions=[{"name": "dimension_a", "column": "b.member"}],
            dependency=[],
            created_by=owner,
        )
        path = JoinPath(
            code=f"{schema}_path",
            name="Alignment fixture path",
            left_asset_id=fact.id,
            left_columns=["member"],
            right_asset_id=dimension.id,
            right_columns=["member"],
            cardinality="n1",
            status="approved",
            created_by=owner,
        )
        session.add_all([version, path])
        session.flush()
        session.add(
            JoinPathApproval(
                join_path_id=path.id,
                domain_id=metric.domain_id,
                user_id=owner,
                decision="approved",
            )
        )
        ids = SimpleNamespace(owner=owner, version=version.id, path=path.id, dimension=dimension.id)

    with session_scope() as session:
        yield SimpleNamespace(
            session=session,
            user=session.get(User, ids.owner),
            version=session.get(MetricVersion, ids.version),
            path=session.get(JoinPath, ids.path),
            dimension_id=ids.dimension,
            settings=settings,
            statements=statements,
            table=f"{database.database}.{schema}.dimension",
            schema=schema,
        )


def align(world, original):
    return align_filters(
        world.session,
        world.user,
        world.version,
        [DimensionFilter(dimension="dimension_a", values=[original])],
        world.settings,
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_approved_dimension_alias_executes_on_postgresql(alignment_pg, reverse):
    world = alignment_pg
    if reverse:
        world.path.left_asset_id, world.path.right_asset_id = world.path.right_asset_id, world.path.left_asset_id
        world.path.cardinality = "1n"
        world.session.flush()

    result = align(world, "alias_a")

    assert result.filters[0].values == ["member_a"]
    assert result.unresolved == [] and result.unknown == []
    assert result.explanations == [
        {
            "dimension": "dimension_a",
            "original": "alias_a",
            "value": "member_a",
            "method": "dimension_table",
            "asset_id": world.dimension_id,
            "table": world.table,
            "column": "alias",
        }
    ]
    assert any('"dimension"' in statement and "DISTINCT" in statement for statement in world.statements)


def test_dimension_alias_sql_literal_is_escaped_on_postgresql(alignment_pg):
    world = alignment_pg

    result = align(world, "x' OR 1=1 --")

    assert result.filters[0].values == ["member_b"]
    assert result.unresolved == [] and result.unknown == []
    assert result.explanations[0]["column"] == "alias"
    assert any("x'' OR 1=1 --" in statement for statement in world.statements)


def test_dimension_alias_with_multiple_keys_is_a_candidate_on_postgresql(alignment_pg):
    world = alignment_pg

    result = align(world, "shared_label")

    assert result.filters[0].values == ["shared_label"]
    assert result.explanations == [] and result.unknown == []
    assert result.unresolved == [
        {
            "dimension": "dimension_a",
            "original": "shared_label",
            "candidates": ["member_a", "member_b"],
            "has_dimension_table": True,
            "candidate_sources": {"member_a": [world.dimension_id], "member_b": [world.dimension_id]},
        }
    ]


def test_truncated_normalized_sample_still_uses_dimension_table_on_postgresql(alignment_pg, monkeypatch):
    world = alignment_pg
    monkeypatch.setattr(service, "DIMENSION_VALUES_LIMIT", 2)
    world.session.execute(
        text(f'UPDATE "{world.schema}".fact SET member = :member WHERE member = :previous'),
        {"member": "ALIAS_A", "previous": "member_b"},
    )
    world.session.commit()

    result = align(world, "alias_a")

    assert result.filters[0].values == ["member_a"]
    assert result.unresolved == [] and result.unknown == []
    assert result.explanations[0]["method"] == "dimension_table"
    assert result.explanations[0]["column"] == "alias"
    assert any('"dimension"' in statement and "DISTINCT" in statement for statement in world.statements)


def test_array_column_does_not_block_scalar_alias_on_postgresql(alignment_pg):
    world = alignment_pg
    world.session.execute(text(f'ALTER TABLE "{world.schema}".dimension ADD COLUMN tags text[]'))
    world.session.execute(text(f"UPDATE \"{world.schema}\".dimension SET tags = ARRAY['alias_a']"))
    asset = world.session.get(Asset, world.dimension_id)
    column = AssetColumn(asset_id=asset.id, name="tags")
    world.session.add(column)
    world.session.flush()
    world.session.add(
        ColumnVersion(
            asset_version_id=asset.current_version_id, column_id=column.id, ordinal=4, name="tags", data_type="text[]"
        )
    )
    world.session.commit()

    result = align(world, "alias_a")

    assert result.filters[0].values == ["member_a"]
    assert not result.unknown and not result.unresolved
    assert result.explanations[0]["column"] == "alias"
    assert all('"tags"' not in sql for sql in world.statements)


def test_outside_sample_values_share_one_existence_query_on_postgresql(alignment_pg, monkeypatch):
    world = alignment_pg
    monkeypatch.setattr(service, "DIMENSION_VALUES_LIMIT", 2)
    dimension_values = service._dimension_values

    def sampled(*args, **kwargs):
        if kwargs.get("exact_values") is None:
            return {"dimension_a": ["sample_a", "sample_b"]}
        return dimension_values(*args, **kwargs)

    monkeypatch.setattr(service, "_dimension_values", sampled)
    result = align_filters(
        world.session,
        world.user,
        world.version,
        [DimensionFilter(dimension="dimension_a", values=["member_a", "member_b", "member_a"])],
        world.settings,
    )

    assert result.filters[0].values == ["member_a", "member_b", "member_a"]
    assert not result.unknown and not result.unresolved
    assert len(world.statements) == 1
    assert "member_a" in world.statements[0] and "member_b" in world.statements[0]


@pytest.mark.parametrize("alignment_pg", ["numeric"], indirect=True)
def test_confirmed_numeric_dimension_key_executes_empty_query_on_postgresql(alignment_pg):
    world = alignment_pg
    session = world.session
    metric = session.get(Metric, world.version.metric_id)
    catalog = service.Catalog([], {metric.code: (metric, world.version)})
    box = SimpleNamespace(
        session=session,
        user=world.user,
        catalog=catalog,
        budget=SimpleNamespace(remaining=lambda: 60),
        filter_alignment=[],
        filter_alignment_unknown=[],
    )
    scope = Scope(
        question_type="lookup",
        metric_codes=[metric.code],
        filters=[DimensionFilter(dimension="dimension_a", values=["shared_alias"])],
        dimensions=["dimension_a"],
    )
    _, unresolved = agent._align_scope_filters(box, scope)
    _, details = agent._filter_clarification(unresolved)
    assert details["items"][0]["options"] == ["1", "2"]
    assert details["items"][0]["candidate_sources"] == {"1": [world.dimension_id], "2": [world.dimension_id]}
    selections = agent._alignment_selections("1", details["items"])
    value = selections[("dimension_a", "shared_alias")]
    scope.filters[0].values = [value]
    box.filter_alignment = [
        {
            "dimension": "dimension_a",
            "original": "shared_alias",
            "value": value,
            "method": "confirmed",
            "asset_id": world.dimension_id,
        }
    ]
    query_count = len(world.statements)

    aligned, unresolved = agent._align_scope_filters(box, scope)

    assert not unresolved and aligned.filters[0].values == ["1"]
    assert len(world.statements) == query_count  # 确认的数字键不再进入别名匹配 SQL。
    answer = service.start_answer(session, world.user, "shared_alias", "offline", None)
    service.fulfil(
        session,
        world.user,
        Intent(
            metric_code=metric.code,
            filters=aligned.filters,
            group_by=aligned.dimensions,
            start_date="2026-01-01",
            end_date="2026-01-31",
        ),
        answer,
        catalog,
        settings=replace(world.settings, query_max_rows=service.DIMENSION_VALUES_LIMIT, query_timeout_seconds=10),
    )
    assert answer.status == "answered", (answer.error_code, answer.message)
    assert answer.result["rows"] == [] and not answer.cache_hit
    assert len(world.statements) == query_count + 1 and "'1'" in answer.sql
