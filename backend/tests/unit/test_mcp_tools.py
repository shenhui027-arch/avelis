"""离线数据库 + 真正的问数编译器；只有业务数据库连接器替换为受控夹具。"""

import json
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import JSON, MetaData, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from avelis.ask import service
from avelis.ask.intent import DimensionFilter
from avelis.config import Settings
from avelis.connectors.base import QueryResult
from avelis.db import Base, utcnow
from avelis.governance.errors import Invalid, NotFound
from avelis.mcp.tools import ToolService
from avelis.models import (
    AppSetting,
    Asset,
    AssetColumn,
    AssetVersion,
    ChangeEvent,
    ChangeSet,
    CollectionRun,
    ColumnVersion,
    Datasource,
    Domain,
    DomainMember,
    Impact,
    LineageEdge,
    Metric,
    MetricVersion,
    ReviewTask,
    User,
    UserRole,
)

START = date(2026, 1, 1)
END = date(2026, 1, 31)


@pytest.fixture
def toolbox(monkeypatch):
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for original in Base.metadata.tables.values():
        table = original.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    settings = Settings(database_url="sqlite://", redis_url="", secret="test-secret-mcp-x", ask_live_check=False)
    with Session(engine, expire_on_commit=False) as session:
        user = User(id="reader", username="reader", display_name="用户甲", password_hash="unused")
        session.add(user)
        session.add_all(
            [
                UserRole(user_id="reader", role="analyst"),
                Domain(id="visible", code="domain_a", name="域甲", owner_id="reader"),
                Domain(id="hidden", code="domain_b", name="域乙", owner_id="reader"),
                DomainMember(user_id="reader", domain_id="visible", role="viewer"),
                Datasource(id="ds", name="离线数据源", kind="postgres", host="offline", port=1),
                CollectionRun(id="run", datasource_id="ds", trigger="manual", status="succeeded"),
            ]
        )
        for asset_id, domain in (("asset", "visible"), ("upstream", "visible"), ("hidden_asset", "hidden")):
            session.add(
                Asset(
                    id=asset_id,
                    datasource_id="ds",
                    kind="table",
                    catalog_name="catalog",
                    db_name="schema_a",
                    name=asset_id,
                    domain_id=domain,
                    current_version_id=asset_id + "-v1",
                )
            )
            session.add(
                AssetVersion(
                    id=asset_id + "-v1",
                    asset_id=asset_id,
                    version_no=1,
                    observed_run_id="run",
                    last_seen_run_id="run",
                    kind="table",
                    catalog_name="catalog",
                    db_name="schema_a",
                    name=asset_id,
                    content_hash="hash",
                )
            )
            for ordinal, (name, data_type) in enumerate((("value_a", "numeric"), ("day_a", "date"), ("dim_a", "text"))):
                session.add(AssetColumn(id=asset_id + name, asset_id=asset_id, name=name))
                session.add(
                    ColumnVersion(
                        id=asset_id + name + "v",
                        asset_version_id=asset_id + "-v1",
                        column_id=asset_id + name,
                        ordinal=ordinal,
                        name=name,
                        data_type=data_type,
                    )
                )
        for metric_id, domain in (("metric", "visible"), ("hidden_metric", "hidden")):
            session.add(
                Metric(
                    id=metric_id,
                    code="metric_a" if metric_id == "metric" else "metric_b",
                    name=metric_id,
                    aliases=["别名甲"],
                    ai_context={"instructions": "说明甲", "synonyms": ["合成词甲"], "examples": ["问法甲"]},
                    domain_id=domain,
                    owner_id="reader",
                    created_by="reader",
                )
            )
            session.add(
                MetricVersion(
                    id=metric_id + "-v1",
                    metric_id=metric_id,
                    version_no=1,
                    status="published",
                    description="口径说明",
                    base_asset_id="asset" if metric_id == "metric" else "hidden_asset",
                    expression="SUM(b.value_a)",
                    time_column="b.day_a",
                    timezone="UTC",
                    created_by="reader",
                    unit="单位甲",
                    dimensions=[{"name": "dim_a", "column": "b.dim_a", "label": "维度甲"}],
                )
            )
        session.commit()
        calls = []

        class Connector:
            def freshness(self, tables):
                return {"as_of": "2026-01-31T00:00:00Z"}

            def execute_select(self, sql, **kwargs):
                calls.append({"sql": sql, **kwargs})
                count = kwargs["max_rows"]
                return QueryResult(columns=["dim_a", "value"], rows=[[str(i), i] for i in range(count)], truncated=True)

        monkeypatch.setattr(service, "get_settings", lambda: settings)
        monkeypatch.setattr(service, "query_connector", lambda *args: Connector())
        monkeypatch.setattr(service, "freshness_boundary", lambda *args: {})
        monkeypatch.setattr(service, "_cache_get", lambda *args: None)
        monkeypatch.setattr(service, "_cache_put", lambda *args: None)
        yield SimpleNamespace(
            tool=ToolService(session, user, settings),
            session=session,
            user=user,
            calls=calls,
            settings=settings,
        )
    engine.dispose()


@pytest.mark.parametrize("keyword", ["metric_a", "metric", "别名甲", "合成词甲", "问法甲"])
def test_search_reuses_question_search_and_ai_context(toolbox, keyword):
    result = toolbox.tool.search_metrics(keyword)
    assert [row["code"] for row in result] == ["metric_a"]
    assert result[0]["ai_context"]["synonyms"] == ["合成词甲"]
    assert result[0]["domain"]["id"] == "visible"
    assert "expression" not in result[0]
    assert toolbox.tool.search_metrics(domain_id="hidden") == []


def test_search_omits_affected_and_latest_governed_rule(toolbox):
    version = toolbox.session.get(MetricVersion, "metric-v1")
    version.status, version.status_reason = "affected", "结构变化"
    assert toolbox.tool.search_metrics() == []
    result = toolbox.tool.ask_metric("metric_a", START, END)
    assert result["status"] == "blocked" and result["error_code"] == "metric_affected"
    assert "结构变化" in result["message"]
    assert not toolbox.calls


def test_get_metric_complete_definition_and_permission(toolbox):
    result = toolbox.tool.get_metric("metric_a")
    for key in ("expression", "time_column", "timezone", "unit", "dimensions", "sources", "ai_context"):
        assert key in result
    assert result["sources"][0]["id"] == "asset"
    assert result["ai_context"]["instructions"] == "说明甲"
    with pytest.raises(NotFound):
        toolbox.tool.get_metric("metric_b")
    with pytest.raises(NotFound):
        toolbox.tool.ask_metric("metric_b", START, END)


def test_invisible_upstream_blocks_definition_and_query(toolbox):
    toolbox.session.add(
        LineageEdge(
            id="hidden-edge",
            origin="manual",
            target_asset_id="asset",
            target_column="value_a",
            source_asset_id="hidden_asset",
            source_ref="catalog.schema_a.hidden_asset",
            source_column="value_a",
            relation="value_direct",
        )
    )
    toolbox.session.flush()
    for method, args in ((toolbox.tool.get_metric, ("metric_a",)), (toolbox.tool.ask_metric, ("metric_a", START, END))):
        with pytest.raises(NotFound) as error:
            method(*args)
        assert "hidden_asset" not in str(error.value)
    assert not toolbox.calls


def test_ask_uses_same_compiler_executor_limits_and_freshness(toolbox):
    result = toolbox.tool.ask_metric("metric_a", START, END, dimensions=["dim_a"])
    assert result["status"] == "answered", result
    assert result["metric"] == {"code": "metric_a", "version_id": "metric-v1", "version_no": 1}
    assert "LIMIT 201" in result["sql"]
    assert result["max_rows"] == result["result"]["row_count"] == 200
    assert result["freshness"]["tables"]["as_of"] == "2026-01-31T00:00:00Z"
    assert toolbox.calls[0]["max_rows"] == 200
    assert toolbox.calls[0]["timeout_seconds"] == toolbox.settings.query_timeout_seconds
    assert toolbox.calls[0]["mem_limit_bytes"] == toolbox.settings.query_mem_limit_bytes


def test_default_clamps_to_lower_page_limit_but_explicit_excess_is_rejected(toolbox):
    from dataclasses import replace

    tool = ToolService(toolbox.session, toolbox.user, replace(toolbox.settings, query_max_rows=7))
    result = tool.ask_metric("metric_a", START, END, dimensions=["dim_a"])
    assert result["result"]["row_count"] == result["max_rows"] == 7
    assert "LIMIT 8" in result["sql"]
    with pytest.raises(Invalid):
        tool.ask_metric("metric_a", START, END, limit=8)


@pytest.mark.parametrize("limit", [0, -1, 1001])
def test_row_limit_rejects_invalid_values(toolbox, limit):
    with pytest.raises(Invalid):
        toolbox.tool.ask_metric("metric_a", START, END, limit=limit)
    assert not toolbox.calls


def test_sql_is_not_an_argument_and_filters_remain_literals(toolbox):
    with pytest.raises(TypeError):
        toolbox.tool.ask_metric("metric_a", START, END, sql="SELECT 1")
    result = toolbox.tool.ask_metric(
        "metric_a",
        START,
        END,
        filters=[DimensionFilter(dimension="dim_a", values=["x' OR 1=1 --"])],
    )
    assert result["status"] == "answered", result
    assert "'x'' OR 1=1 --'" in result["sql"]


def test_data_policy_compiles_without_connection_cache_freshness_or_rows(toolbox, monkeypatch):
    toolbox.session.add(AppSetting(key="model_policy", value={"send_results": False, "send_code": True}))

    def forbidden(*args, **kwargs):
        pytest.fail("禁外发编译不应访问业务数据库或答案缓存")

    monkeypatch.setattr(service, "query_connector", forbidden)
    monkeypatch.setattr(service, "_cache_get", forbidden)
    monkeypatch.setattr(service, "freshness_boundary", forbidden)
    result = toolbox.tool.ask_metric("metric_a", START, END, dimensions=["dim_a"])
    assert result["status"] == "compiled", result
    assert "LIMIT 201" in result["sql"]
    assert result["definition"]["expression"] == "SUM(b.value_a)"
    assert result["data_returned"] is False and "result" not in result and "rows" not in result
    assert result["freshness"] == {}


@pytest.mark.parametrize("derived", [False, True])
def test_compare_and_derived_compile_all_subqueries_with_policy_disabled(toolbox, monkeypatch, derived):
    toolbox.session.add(AppSetting(key="model_policy", value={"send_results": False}))
    code = "metric_a"
    if derived:
        toolbox.session.add(
            Metric(
                id="derived",
                code="metric_c",
                name="派生甲",
                domain_id="visible",
                owner_id="reader",
                created_by="reader",
            )
        )
        toolbox.session.add(
            MetricVersion(
                id="derived-v1",
                metric_id="derived",
                version_no=1,
                status="published",
                description="说明甲",
                kind="derived",
                expression="metric_a * 2",
                time_column="",
                timezone="UTC",
                created_by="reader",
                components=[
                    {
                        "metric_id": "metric",
                        "version_id": "metric-v1",
                        "metric_code": "metric_a",
                        "metric_name": "metric",
                    }
                ],
                dimensions=[{"name": "dim_a", "column": "b.dim_a"}],
            )
        )
        code = "metric_c"
    monkeypatch.setattr(service, "query_connector", lambda *args: pytest.fail("compile-only must not connect"))
    result = toolbox.tool.ask_metric(code, START, END, dimensions=["dim_a"], compare="previous_period")
    assert result["status"] == "compiled", result
    assert len(result["plan"]["subqueries"]) == 2
    assert result["sql"].count("SELECT") == 2
    assert "result" not in result


def test_lineage_filters_hidden_endpoints_and_column(toolbox):
    for edge_id, source, target, column in (
        ("visible-edge", "upstream", "asset", "value_a"),
        ("hidden-edge", "hidden_asset", "asset", "value_a"),
        ("other-column", "upstream", "asset", "dim_a"),
    ):
        toolbox.session.add(
            LineageEdge(
                id=edge_id,
                origin="manual",
                source_asset_id=source,
                source_ref=source,
                source_column=column,
                target_asset_id=target,
                target_column=column,
                relation="value_direct",
                expression="upstream.value_a + hidden_asset.secret",
            )
        )
    toolbox.session.flush()
    result = toolbox.tool.lineage_neighbors("asset", column="value_a", direction="upstream")
    assert {node["id"] for node in result["nodes"]} == {"upstream", "asset"}
    assert [edge["id"] for edge in result["edges"]] == ["visible-edge"]
    assert "hidden_asset" not in json.dumps(result)
    assert "expression" not in result["edges"][0]
    with pytest.raises(NotFound):
        toolbox.tool.lineage_neighbors("hidden_asset")


def test_changes_filter_hidden_events_and_impacts(toolbox):
    toolbox.session.add(ChangeSet(id="change", datasource_id="ds", run_id="run", status="open", window_end=utcnow()))
    for asset in ("asset", "hidden_asset"):
        toolbox.session.add(
            ChangeEvent(id="event-" + asset, change_set_id="change", asset_id=asset, kind="column_changed")
        )
    for metric in ("metric", "hidden_metric"):
        toolbox.session.add(
            Impact(
                id="impact-" + metric,
                change_set_id="change",
                object_type="metric_version",
                object_id=metric + "-v1",
                level="affected",
                reason="结构变化",
                path=[],
            )
        )
    toolbox.session.flush()
    result = toolbox.tool.list_changes("asset", "asset")
    assert [event["asset_id"] for event in result[0]["events"]] == ["asset"]
    assert [impact["id"] for impact in result[0]["impacts"]] == ["impact-metric"]
    result = toolbox.tool.list_changes("metric", "metric_a")
    assert [impact["id"] for impact in result[0]["impacts"]] == ["impact-metric"]
    assert [event["asset_id"] for event in result[0]["events"]] == ["asset"]
    with pytest.raises(NotFound):
        toolbox.tool.list_changes("asset", "hidden_asset")


@pytest.mark.parametrize("object_type,object_id", [("asset", "asset"), ("metric", "metric_a")])
def test_change_reason_with_hidden_dependency_is_redacted(toolbox, object_type, object_id):
    toolbox.session.add(ChangeSet(id="change", datasource_id="ds", run_id="run", status="open", window_end=utcnow()))
    toolbox.session.add(ChangeEvent(id="event", change_set_id="change", asset_id="asset", kind="column_changed"))
    version = toolbox.session.get(MetricVersion, "metric-v1")
    version.dependency = [{"asset_id": "hidden_asset", "column": "secret"}]
    toolbox.session.add(
        Impact(
            id="impact",
            change_set_id="change",
            object_type="metric_version",
            object_id=version.id,
            level="affected",
            reason="catalog.schema_a.hidden_asset 的 secret 字段变化",
            path=[],
        )
    )
    toolbox.session.flush()
    result = toolbox.tool.list_changes(object_type, object_id)
    assert result[0]["impacts"][0]["reason"] is None
    assert result[0]["impacts"][0]["reason_redacted"] is True
    assert "hidden_asset" not in json.dumps(result)


@pytest.mark.parametrize("replacement,visible", [("hidden_metric", False), ("metric", True)])
def test_retired_metric_replacement_hint_respects_domain_permission(toolbox, replacement, visible):
    version = toolbox.session.get(MetricVersion, "metric-v1")
    version.status, version.status_reason = "retired", "复核停用"
    toolbox.session.add(
        ChangeSet(id="change", datasource_id="ds", run_id="run", status="resolved", window_end=utcnow())
    )
    toolbox.session.add(
        ReviewTask(
            id="task",
            change_set_id="change",
            object_type="metric_version",
            object_id=version.id,
            assignee_id=toolbox.user.id,
            status="resolved",
            linked_metric_version_id=replacement + "-v1",
        )
    )
    toolbox.session.flush()
    result = toolbox.tool.ask_metric("metric_a", START, END)
    assert result["status"] == "blocked" and result["error_code"] == "metric_retired"
    assert ("负责人已发布新口径" in result["message"]) is visible
    assert "hidden_metric" not in result["message"]
    assert not toolbox.calls


def test_changes_return_structural_summary_without_historical_sql_or_attribution(toolbox):
    toolbox.session.add(ChangeSet(id="change", datasource_id="ds", run_id="run", status="open", window_end=utcnow()))
    toolbox.session.add(
        ChangeEvent(
            id="event",
            change_set_id="change",
            asset_id="asset",
            kind="definition_changed",
            column_name="value_a",
            before={"canonical_sql": "SELECT secret FROM schema_b.hidden_asset"},
            after={"canonical_sql": "SELECT value_a FROM schema_a.upstream"},
            attribution={"statement": "ALTER VIEW private_sql_marker"},
        )
    )
    toolbox.session.flush()
    result = toolbox.tool.list_changes("asset", "asset")
    assert result[0]["events"] == [
        {
            "id": "event",
            "kind": "definition_changed",
            "asset_id": "asset",
            "asset": "catalog.schema_a.asset",
            "column": "value_a",
        }
    ]
    snapshot = json.dumps(result)
    assert "hidden_asset" not in snapshot and "SELECT" not in snapshot and "private_sql_marker" not in snapshot
