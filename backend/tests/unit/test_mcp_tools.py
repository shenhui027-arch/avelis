"""离线数据库 + 真正的问数编译器；只有业务数据库连接器替换为受控夹具。"""

import json
from datetime import date
from types import SimpleNamespace

import pytest
import sqlglot
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
                AppSetting(key="model_policy", value={"send_results": True, "send_code": True}),
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
    assert f"LIMIT {toolbox.settings.query_max_rows + 1}" in result["sql"]
    assert result["max_rows"] == result["result"]["row_count"] == 200
    assert result["freshness"]["tables"]["as_of"] == "2026-01-31T00:00:00Z"
    assert toolbox.calls[0]["max_rows"] == toolbox.settings.query_max_rows
    assert toolbox.calls[0]["timeout_seconds"] == toolbox.settings.query_timeout_seconds
    assert toolbox.calls[0]["mem_limit_bytes"] == toolbox.settings.query_mem_limit_bytes


@pytest.mark.parametrize("method,expected", [("last", 30), ("avg", 20), ("sum", 60)])
@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("time_grain", [None, "month"])
def test_client_limit_is_applied_after_time_aggregation(toolbox, monkeypatch, method, expected, grouped, time_grain):
    version = toolbox.session.get(MetricVersion, "metric-v1")
    version.time_aggregation = method
    version.data_grain = "day"

    class Connector:
        def freshness(self, tables):
            return {}

        def execute_select(self, sql, **kwargs):
            toolbox.calls.append({"sql": sql, **kwargs})
            query = sqlglot.parse_one(sql, read="postgres")
            columns = query.named_selects
            rows = []
            for dimension, factor in (("a", 1), ("b", 10)) if grouped else ((None, 1),):
                prefix = (["2026-01"] if time_grain else []) + ([dimension] if grouped else [])
                values = [10 * factor, 20 * factor, 30 * factor]
                if "time_bucket" in columns:
                    rows.extend([*prefix, f"2026-01-0{day}", value] for day, value in enumerate(values, 1))
                else:
                    rows.append([*prefix, sum(values)])
            sql_limit = int(query.args["limit"].expression.this)
            rows = rows[:sql_limit]
            return QueryResult(
                columns=columns, rows=rows[: kwargs["max_rows"]], truncated=len(rows) > kwargs["max_rows"]
            )

    monkeypatch.setattr(service, "query_connector", lambda *args: Connector())
    response = toolbox.tool.ask_metric(
        "metric_a", START, END, dimensions=["dim_a"] if grouped else [], time_grain=time_grain, limit=1
    )
    assert response["status"] == "answered", response
    result = response["result"]
    assert result["row_count"] == response["max_rows"] == len(result["rows"]) == 1
    assert float(result["rows"][0][-1]) == expected
    assert result["truncated"] is grouped
    assert "time_bucket" not in result["columns"]
    assert toolbox.calls[0]["max_rows"] == toolbox.settings.query_max_rows
    assert f"LIMIT {toolbox.settings.query_max_rows + 1}" in response["sql"]


@pytest.mark.parametrize("method", ["last", "avg", "sum"])
def test_execution_truncation_is_preserved_when_client_result_fits(toolbox, monkeypatch, method):
    version = toolbox.session.get(MetricVersion, "metric-v1")
    version.time_aggregation = method

    class Connector:
        def freshness(self, tables):
            return {}

        def execute_select(self, sql, **kwargs):
            columns = sqlglot.parse_one(sql, read="postgres").named_selects
            row = ["2026-01-01", 10] if "time_bucket" in columns else [10]
            return QueryResult(columns=columns, rows=[row], truncated=True)

    monkeypatch.setattr(service, "query_connector", lambda *args: Connector())
    response = toolbox.tool.ask_metric("metric_a", START, END, limit=1)
    assert response["status"] == "answered", response
    assert response["result"]["row_count"] == 1
    assert response["result"]["truncated"] is True


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
    toolbox.session.merge(AppSetting(key="model_policy", value={"send_results": False, "send_code": True}))
    toolbox.session.flush()

    def forbidden(*args, **kwargs):
        pytest.fail("禁外发编译不应访问业务数据库或答案缓存")

    monkeypatch.setattr(service, "query_connector", forbidden)
    monkeypatch.setattr(service, "_cache_get", forbidden)
    monkeypatch.setattr(service, "freshness_boundary", forbidden)
    result = toolbox.tool.ask_metric("metric_a", START, END, dimensions=["dim_a"])
    assert result["status"] == "compiled", result
    assert f"LIMIT {toolbox.settings.query_max_rows + 1}" in result["sql"]
    assert result["definition"]["expression"] == "SUM(b.value_a)"
    assert result["data_returned"] is False and "result" not in result and "rows" not in result
    assert result["freshness"] == {}


@pytest.mark.parametrize("derived", [False, True])
def test_compare_and_derived_compile_all_subqueries_with_policy_disabled(toolbox, monkeypatch, derived):
    toolbox.session.merge(AppSetting(key="model_policy", value={"send_results": False}))
    toolbox.session.flush()
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


def _add_second_metric(session, code="metric_d", expr="COUNT(*)", dimensions=None):
    session.add(
        Metric(
            id=f"id-{code}",
            code=code,
            name=f"指标_{code}",
            domain_id="visible",
            owner_id="reader",
            created_by="reader",
        )
    )
    session.add(
        MetricVersion(
            id=f"id-{code}-v1",
            metric_id=f"id-{code}",
            version_no=1,
            status="published",
            description="第二指标",
            base_asset_id="asset",
            expression=expr,
            time_column="b.day_a",
            timezone="UTC",
            created_by="reader",
            unit="单",
            dimensions=dimensions
            if dimensions is not None
            else [{"name": "dim_a", "column": "b.dim_a", "label": "维度甲"}],
        )
    )
    session.flush()


def test_ask_metric_supports_multi_metric_list_and_csv(toolbox, monkeypatch):
    _add_second_metric(toolbox.session, "metric_d", "COUNT(*)")

    class Connector:
        def freshness(self, tables):
            return {"as_of": "2026-01-31T00:00:00Z"}

        def execute_select(self, sql, **kwargs):
            toolbox.calls.append({"sql": sql, **kwargs})
            cols = sqlglot.parse_one(sql, read="postgres").named_selects
            if "COUNT(*)" in sql:
                return QueryResult(columns=cols, rows=[["east", 5], ["west", 2]], truncated=False)
            return QueryResult(columns=cols, rows=[["east", 100], ["west", 200]], truncated=False)

    monkeypatch.setattr(service, "query_connector", lambda *args: Connector())
    res_list = toolbox.tool.ask_metric(
        metrics=["metric_a", "metric_d", "metric_a"],
        start_date=START,
        end_date=END,
        dimensions=["dim_a"],
    )
    assert res_list["status"] == "answered", res_list
    assert [m["code"] for m in res_list["metrics"]] == ["metric_a", "metric_d"]
    assert res_list["metric"]["code"] == "metric_a"
    assert len(res_list["result"]["rows"]) == 2
    assert len(res_list["result"]["columns"]) == 3

    res_csv = toolbox.tool.ask_metric(
        metrics="metric_a, metric_d",
        start_date=START,
        end_date=END,
        dimensions=["dim_a"],
    )
    assert res_csv["status"] == "answered", res_csv
    assert [m["code"] for m in res_csv["metrics"]] == ["metric_a", "metric_d"]


def test_ask_metric_multi_metric_validation_and_policy_definitions(toolbox, monkeypatch):
    _add_second_metric(toolbox.session, "metric_d")
    with pytest.raises(Invalid):
        toolbox.tool.ask_metric(start_date=START, end_date=END)
    with pytest.raises(Invalid):
        toolbox.tool.ask_metric(metrics=["metric_a", ""], start_date=START, end_date=END)
    with pytest.raises(Invalid):
        toolbox.tool.ask_metric(metrics=["m1", "m2", "m3", "m4", "m5"], start_date=START, end_date=END)
    with pytest.raises(NotFound):
        toolbox.tool.ask_metric(metrics=["metric_a", "metric_b"], start_date=START, end_date=END)

    toolbox.session.merge(AppSetting(key="model_policy", value={"send_results": False}))
    toolbox.session.flush()
    monkeypatch.setattr(service, "query_connector", lambda *args: pytest.fail("compile-only must not connect"))
    compiled = toolbox.tool.ask_metric(
        metrics=["metric_a", "metric_d"],
        start_date=START,
        end_date=END,
        dimensions=["dim_a"],
    )
    assert compiled["status"] == "compiled"
    assert [d["expression"] for d in compiled["definitions"]] == ["SUM(b.value_a)", "COUNT(*)"]
    assert compiled["definition"]["expression"] == "SUM(b.value_a)"


def test_ask_metric_order_and_limit_alignment(toolbox, monkeypatch):
    class Connector:
        def freshness(self, tables):
            return {}

        def execute_select(self, sql, **kwargs):
            toolbox.calls.append({"sql": sql, **kwargs})
            rows = [["west", 50], ["east", 200]] if "ASC" in sql.upper() else [["east", 200], ["west", 50]]
            return QueryResult(columns=["dim_a", "value"], rows=rows[: kwargs["max_rows"]], truncated=False)

    monkeypatch.setattr(service, "query_connector", lambda *args: Connector())
    asc = toolbox.tool.ask_metric(
        "metric_a",
        START,
        END,
        dimensions=["dim_a"],
        order="asc",
        limit=10,
    )
    assert asc["status"] == "answered"
    assert "ORDER BY value ASC" in asc["sql"]
    assert "LIMIT 10" in asc["sql"]
    assert asc["result"]["rows"][0][0] == "west"

    # limit > MAX_LIMIT (100) must not fail Intent validation; falls back to query_max_rows + 1 & client slice
    over_intent = toolbox.tool.ask_metric(
        "metric_a",
        START,
        END,
        dimensions=["dim_a"],
        order="desc",
        limit=150,
    )
    assert over_intent["status"] == "answered"
    assert f"LIMIT {toolbox.settings.query_max_rows + 1}" in over_intent["sql"]
    assert "ORDER BY value DESC" in over_intent["sql"]

    with pytest.raises(Invalid):
        toolbox.tool.ask_metric("metric_a", START, END, order="desc")
    with pytest.raises(Invalid):
        toolbox.tool.ask_metric("metric_a", START, END, dimensions=["dim_a"], order="sideways")
    with pytest.raises(Invalid):
        toolbox.tool.ask_metric(
            "metric_a", START, END, dimensions=["dim_a"], compare="previous_period", time_grain="day"
        )


def test_ask_metric_aligns_filters_and_surfaces_unresolved_warnings(toolbox, monkeypatch):
    class Connector:
        def freshness(self, tables):
            return {}

        def execute_select(self, sql, **kwargs):
            toolbox.calls.append({"sql": sql, **kwargs})
            if "DISTINCT" in sql.upper():
                return QueryResult(columns=["dim_a"], rows=[["east"], ["west"]], truncated=False)
            return QueryResult(columns=["dim_a", "value"], rows=[["east", 120]], truncated=False)

    monkeypatch.setattr(service, "query_connector", lambda *args: Connector())
    aligned = toolbox.tool.ask_metric(
        "metric_a",
        START,
        END,
        dimensions=["dim_a"],
        filters=[{"dimension": "dim_a", "values": ["EAST"]}],
    )
    assert aligned["status"] == "answered", aligned
    assert "'east'" in aligned["sql"]
    assert aligned["filter_alignment"]["explanations"][0]["method"] == "normalized"
    assert "warnings" not in aligned

    unresolved = toolbox.tool.ask_metric(
        "metric_a",
        START,
        END,
        dimensions=["dim_a"],
        filters=[DimensionFilter(dimension="dim_a", values=["north"])],
    )
    assert unresolved["status"] == "answered", unresolved
    assert unresolved["filter_alignment"]["unresolved"][0]["candidates"] == ["east", "west"]
    assert unresolved["warnings"] and "north" in unresolved["warnings"][0]


def test_ask_metric_multi_metric_alignment_conflict_becomes_unresolved(toolbox, monkeypatch):
    _add_second_metric(
        toolbox.session,
        "metric_d",
        dimensions=[{"name": "dim_a", "column": "b.dim_a", "label": "维度甲"}],
    )
    # Give metric_d a different base asset so _dimension_values returns different casing
    toolbox.session.add(
        Asset(
            id="asset_2",
            datasource_id="ds",
            kind="table",
            catalog_name="catalog",
            db_name="schema_a",
            name="asset_2",
            domain_id="visible",
            current_version_id="asset_2-v1",
        )
    )
    toolbox.session.add(
        AssetVersion(
            id="asset_2-v1",
            asset_id="asset_2",
            version_no=1,
            observed_run_id="run",
            last_seen_run_id="run",
            kind="table",
            catalog_name="catalog",
            db_name="schema_a",
            name="asset_2",
            content_hash="hash2",
        )
    )
    for ordinal, (name, data_type) in enumerate((("value_a", "numeric"), ("day_a", "date"), ("dim_a", "text"))):
        toolbox.session.add(AssetColumn(id="asset_2" + name, asset_id="asset_2", name=name))
        toolbox.session.add(
            ColumnVersion(
                id="asset_2" + name + "v",
                asset_version_id="asset_2-v1",
                column_id="asset_2" + name,
                ordinal=ordinal,
                name=name,
                data_type=data_type,
            )
        )
    v2 = toolbox.session.get(MetricVersion, "id-metric_d-v1")
    v2.base_asset_id = "asset_2"
    toolbox.session.flush()

    class Connector:
        def freshness(self, tables):
            return {}

        def execute_select(self, sql, **kwargs):
            if "DISTINCT" in sql.upper():
                if "asset_2" in sql:
                    return QueryResult(columns=["dim_a"], rows=[["East"]], truncated=False)
                return QueryResult(columns=["dim_a"], rows=[["east"]], truncated=False)
            cols = sqlglot.parse_one(sql, read="postgres").named_selects
            return QueryResult(columns=cols, rows=[], truncated=False)

    monkeypatch.setattr(service, "query_connector", lambda *args: Connector())
    result = toolbox.tool.ask_metric(
        metrics=["metric_a", "metric_d"],
        start_date=START,
        end_date=END,
        dimensions=["dim_a"],
        filters=[DimensionFilter(dimension="dim_a", values=["EAST"])],
    )
    assert result["status"] == "answered", result
    unresolved = result["filter_alignment"]["unresolved"]
    assert len(unresolved) == 1
    assert set(unresolved[0]["candidates"]) == {"east", "East"}
    assert result["warnings"]


def test_ask_metric_alignment_skipped_when_send_results_disabled(toolbox, monkeypatch):
    toolbox.session.merge(AppSetting(key="model_policy", value={"send_results": False}))
    toolbox.session.flush()
    monkeypatch.setattr(service, "query_connector", lambda *args: pytest.fail("compile-only must not connect"))
    res = toolbox.tool.ask_metric(
        "metric_a",
        START,
        END,
        dimensions=["dim_a"],
        filters=[DimensionFilter(dimension="dim_a", values=["EAST"])],
    )
    assert res["status"] == "compiled"
    assert "filter_alignment" not in res
    assert "'EAST'" in res["sql"]


@pytest.mark.parametrize("grouped", [True, False])
def test_client_slice_truncation_clears_insights_and_updates_message(toolbox, monkeypatch, grouped):
    class Connector:
        def freshness(self, tables):
            return {}

        def execute_select(self, sql, **kwargs):
            cols = sqlglot.parse_one(sql, read="postgres").named_selects
            rows = (
                [["east", 100], ["west", 200], ["north", 300]]
                if grouped
                else [["2026-01", 100], ["2026-02", 200], ["2026-03", 300]]
            )
            return QueryResult(columns=cols, rows=rows, truncated=False)

    monkeypatch.setattr(service, "query_connector", lambda *args: Connector())
    res = toolbox.tool.ask_metric(
        "metric_a",
        START,
        END,
        dimensions=["dim_a"] if grouped else None,
        time_grain=None if grouped else "month",
        limit=1,
    )
    assert res["status"] == "answered"
    assert res["result"]["row_count"] == 1
    assert res["result"]["truncated"] is True
    assert res["result"]["insights"] == []
    assert "共 1 行" in res["message"]
    assert "已截断" in res["message"]
    assert "共 3 行" not in res["message"]


def test_client_slice_above_max_limit_clears_insights_and_updates_message(toolbox, monkeypatch):
    _add_second_metric(toolbox.session, "metric_d", "COUNT(*)")

    class Connector:
        def freshness(self, tables):
            return {}

        def execute_select(self, sql, **kwargs):
            cols = sqlglot.parse_one(sql, read="postgres").named_selects
            rows = [[f"d{i}", 180 - i] for i in range(180)]
            return QueryResult(columns=cols, rows=rows, truncated=False)

    monkeypatch.setattr(service, "query_connector", lambda *args: Connector())

    single = toolbox.tool.ask_metric(
        "metric_a",
        START,
        END,
        dimensions=["dim_a"],
        order="desc",
        limit=150,
    )
    assert single["status"] == "answered"
    assert single["result"]["row_count"] == 150
    assert single["result"]["truncated"] is True
    assert single["result"]["insights"] == []
    assert "共 150 行" in single["message"]
    assert "已截断" in single["message"]
    assert "共 180 行" not in single["message"]

    multi = toolbox.tool.ask_metric(
        metrics=["metric_a", "metric_d"],
        start_date=START,
        end_date=END,
        dimensions=["dim_a"],
        order="desc",
        limit=150,
    )
    assert multi["status"] == "answered"
    assert multi["result"]["row_count"] == 150
    assert multi["result"]["truncated"] is True
    assert multi["result"]["insights"] == []
    assert "共 150 行" in multi["message"]
    assert "已截断" in multi["message"]
    assert "从高到低" in multi["message"]
    assert "共 180 行" not in multi["message"]
