"""P1 R：用假会话、注入边与缓存替身验证元数据发现，不连接数据库或源库。"""

from __future__ import annotations

import json
from fnmatch import fnmatch
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from avelis import config, engines
from avelis.analysis import retrieval, tools
from avelis.analysis.tools import Scope
from avelis.models import Asset, AssetVersion, ColumnVersion, JoinPath, LineageEdge, Metric, MetricVersion
from tests.unit.test_analysis_toolbox import make_box

BASE_TOOLS = [
    "search_metrics",
    "describe_metric",
    "search_assets",
    "describe_asset",
    "lineage",
    "changes",
    "data_health",
    "query_metric",
    "chart",  # 第 3 阶段的固定工具，不受 retrieval / investigation 开关影响。
    "view_dataset",
    "explain_change",
    "contribution",
    "factor_decompose",
    "calendar_adjust",
    "correlate",
    "coverage",
    "derive",
    "describe",
    "segment_compare",
    "outliers",
    "submit_report",
]
CLUE_NOTE = "只是发现线索，不是批准的关联，跨表比较走探索查询校验或分别查询后合并"


def settings(monkeypatch, *, enabled=False, investigation=False):
    value = config.build_settings(
        {
            "AVELIS_SECRET": "unit-test-secret-only",
            "AVELIS_ANALYSIS_RETRIEVAL": str(enabled).lower(),
            "AVELIS_ANALYSIS_INVESTIGATION": str(investigation).lower(),
        }
    )
    monkeypatch.setattr(tools, "get_settings", lambda: value)
    return value


def box(monkeypatch, *, enabled=False):
    settings(monkeypatch, enabled=enabled)
    value = make_box(monkeypatch=monkeypatch)
    monkeypatch.setattr(value, "explore_available", lambda: False)
    return value


def asset(name, *, status="active", kind="table", domain="d1", datasource_id="ds1"):
    return Asset(
        id=name,
        catalog_name="cat",
        db_name="db",
        name=name,
        current_version_id=f"v-{name}",
        status=status,
        kind=kind,
        domain_id=domain,
        datasource_id=datasource_id,
    )


def column(name="key_a", data_type="VARCHAR(32)", *, is_key=False, ordinal=1, comment=None):
    return ColumnVersion(
        id=f"c-{name}-{ordinal}", name=name, data_type=data_type, is_key=is_key, ordinal=ordinal, comment=comment
    )


def graph(
    monkeypatch, assets=(), *, rows=(), visible=None, columns=None, versions=None, metric_sources=None, upstream=None
):
    """底层读取只返回给定对象；权限仍走产品代码的 can_read_asset。"""
    assets = {a.id: a for a in assets}
    version_map = {
        a.current_version_id: AssetVersion(
            id=a.current_version_id, asset_id=a.id, key_columns=[], unique_keys=[], comment=f"{a.name} 注释"
        )
        for a in assets.values()
    }
    version_map.update(versions or {})

    class Session:
        def get(self, cls, id_, **kwargs):
            return assets.get(id_) if cls is Asset else version_map.get(id_) if cls is AssetVersion else None

        def execute(self, statement):
            params = statement.compile().params
            target = next((value for key, value in params.items() if key.startswith("target_asset_id_")), None)
            return [(source,) for source in (upstream or {}).get(target, [])]

        def scalar(self, statement):
            return None

    result = retrieval.MetadataGraph(
        Session(),
        SimpleNamespace(id="u1"),
        {"d1"},
        metric_sources=metric_sources or (lambda code: (None, None, [], {})),
    )
    monkeypatch.setattr(
        retrieval.access,
        "can_read_asset",
        lambda session, user, value, allowed: value.id in (visible if visible is not None else assets),
    )

    def read(statement):
        selected = tuple(c["entity"] for c in statement.column_descriptions)
        if isinstance(rows, dict) and selected in rows:
            if selected == (Asset, ColumnVersion):
                datasource_ids = [
                    expected for key, expected in statement.compile().params.items() if key.startswith("datasource_id_")
                ]
                return iter(
                    (a, c) for a, c in rows[selected] if all(a.datasource_id == expected for expected in datasource_ids)
                )
            return iter(rows[selected])
        if rows and not isinstance(rows, dict):
            return iter(rows)
        params = statement.compile().params
        if selected == (Asset,):
            return iter(
                (a,)
                for a in assets.values()
                if all(not key.startswith("name_") or a.name == expected for key, expected in params.items())
            )
        if selected == (ColumnVersion,):
            found = []
            for a in assets.values():
                for c in (columns or {}).get(a.id, []):
                    if all(
                        (not key.startswith("asset_version_id_") or a.current_version_id == expected)
                        and (not key.startswith("name_") or c.name == expected)
                        for key, expected in params.items()
                    ):
                        found.append((c,))
            return iter(found)
        return iter(())

    monkeypatch.setattr(result, "_rows", read)
    monkeypatch.setattr(result, "_columns", lambda value: iter((columns or {}).get(value.id, [])))
    return result


def node(name):
    return {
        "id": f"asset:cat.db.{name}",
        "kind": "asset",
        "name": name,
        "qualified": f"cat.db.{name}",
        "asset_id": name,
        "comment": f"{name} 注释",
    }


def edge(start, end, relation="join_key", *, count=1):
    return {
        "type": relation,
        "from": f"asset:cat.db.{start}",
        "to": f"asset:cat.db.{end}",
        "columns": [{"from": f"key_{i}", "to": f"key_{i}"} for i in range(count)],
        "clue": relation in {"shared_key", "shared_value"},
    }


def injected_graph(monkeypatch, names, edges, *, hidden=(), columns=None, upstream=None):
    result = graph(
        monkeypatch, [asset(n) for n in names], visible=set(names) - set(hidden), columns=columns, upstream=upstream
    )
    nodes = {node(n)["id"]: node(n) for n in names if n not in hidden}
    monkeypatch.setattr(result, "_resolve", lambda value: nodes.get(value))
    monkeypatch.setattr(
        result,
        "_candidate_edges",
        lambda frontier, relation: iter(
            e
            for e in edges
            if (not relation or e["type"] == relation) and (e["from"] in frontier or e["to"] in frontier)
        ),
    )
    return result


def test_settings_are_enabled_by_default_and_independently_disabled():
    defaults = config.build_settings({"AVELIS_SECRET": "unit-test-secret-only"})
    assert defaults.analysis_retrieval is True and defaults.analysis_investigation is True
    disabled = config.build_settings({"AVELIS_SECRET": "unit-test-secret-only", "AVELIS_ANALYSIS_RETRIEVAL": "false"})
    assert disabled.analysis_retrieval is False and disabled.analysis_investigation is True
    investigation = config.build_settings(
        {"AVELIS_SECRET": "unit-test-secret-only", "AVELIS_ANALYSIS_INVESTIGATION": "false"}
    )
    assert investigation.analysis_retrieval is True and investigation.analysis_investigation is False


def test_disabled_tool_names_match_original_snapshot(monkeypatch):
    value = box(monkeypatch)
    assert [t.name for t in value.tools()] == BASE_TOOLS
    settings(monkeypatch, investigation=True)
    assert [t.name for t in value.tools()] == BASE_TOOLS  # 本版 H 只定义开关


def test_disabled_context_pack_matches_original_nonempty_snapshot(monkeypatch):
    value = box(monkeypatch)
    value.precheck_metric = {"code": "metric_a", "name": "指标甲", "kind": "base", "expression": "SUM(value_a)"}
    value.precheck_meta = [{"table": "cat.db.a", "comment": "表甲", "columns": []}]
    value.precheck_notes = ["步骤甲未完成"]
    value.precheck_refs = [{"table": "cat.db.b", "comment": "参考表乙"}]
    expected = (
        '口径：{"code": "metric_a", "name": "指标甲", "kind": "base", "expression": "SUM(value_a)"}'
        '\n\n来源表 {"table": "cat.db.a", "comment": "表甲", "columns": []}'
        "\n\n预检中没有完成的步骤：步骤甲未完成"
    )
    assert value.context_pack().encode() == expected.encode()
    settings(monkeypatch, investigation=True)
    assert value.context_pack().encode() == expected.encode()


def test_enabled_tools_append_only_and_describe_clues(monkeypatch):
    value = box(monkeypatch, enabled=True)
    available = value.tools()
    assert {t.name for t in available} == set(BASE_TOOLS) | {"find", "neighbors", "path"}
    assert [t.name for t in available if t.name not in {"find", "neighbors", "path"}] == BASE_TOOLS
    assert CLUE_NOTE in " ".join(t.description for t in available if t.name in {"find", "neighbors", "path"})


@pytest.mark.parametrize(
    ("name", "args", "expected"),
    [
        ("find", {"keyword": "a"}, {"nodes": [], "truncated": False, "omitted": 0}),
        ("neighbors", {"node": "asset:cat.db.a"}, {"nodes": [], "edges": [], "truncated": False, "omitted": 0}),
        ("path", {"start": "asset:cat.db.a", "end": "asset:cat.db.b"}, {"paths": [], "truncated": False, "omitted": 0}),
    ],
)
def test_retrieval_tools_use_guard_and_dispatch_budget(monkeypatch, name, args, expected):
    value = box(monkeypatch, enabled=True)
    calls = []

    def answer(self, *positional, **kwargs):
        calls.append((positional, kwargs))
        return expected

    monkeypatch.setattr(retrieval.MetadataGraph, name, answer)
    original = value._guard
    guarded = []

    def guard(tool, arguments, run):
        guarded.append(tool)
        return original(tool, arguments, run)

    monkeypatch.setattr(value, "_guard", guard)
    assert json.loads(value.dispatch(name, args)) == expected
    assert calls and guarded == [name]
    assert value.budget.used_tools == value.budget.used_calls == 1 and value.budget.used_queries == 0
    assert value.session.added[-1].tool == name


@pytest.mark.parametrize(
    "args",
    [
        {"node": "asset:cat.db.a", "depth": 4},
        {"node": "asset:cat.db.a", "depth": 0},
        {"node": "asset:cat.db.a", "relation": "unknown"},
        {"node": "asset:cat.db.a", "direction": "unknown"},
    ],
)
def test_invalid_neighbors_arguments_are_rejected_before_expansion(monkeypatch, args):
    value = box(monkeypatch, enabled=True)
    monkeypatch.setattr(retrieval.MetadataGraph, "neighbors", lambda *a, **k: pytest.fail("参数错误仍展开图"))
    assert "error" in json.loads(value.dispatch("neighbors", args))
    assert value.budget.used_tools == 1 and value.budget.used_queries == 0


@pytest.mark.parametrize("max_depth", [0, 4])
def test_invalid_path_depth_is_rejected_before_expansion(monkeypatch, max_depth):
    value = box(monkeypatch, enabled=True)
    monkeypatch.setattr(retrieval.MetadataGraph, "path", lambda *a, **k: pytest.fail("参数错误仍寻找路径"))
    result = value.dispatch("path", {"start": "asset:cat.db.a", "end": "asset:cat.db.b", "max_depth": max_depth})
    assert "error" in json.loads(result)


def test_metadata_candidate_rows_are_read_in_bounded_pages():
    calls = []
    values = [(asset(f"a{i}"),) for i in range(107)]

    class Session:
        def execute(self, statement):
            limit, offset = statement._limit_clause.value, statement._offset_clause.value
            calls.append((limit, offset))
            return SimpleNamespace(all=lambda: values[offset : offset + limit])

    value = retrieval.MetadataGraph(Session(), None, set(), metric_sources=lambda code: None)
    assert list(value._rows(select(Asset))) == values
    assert calls == [(100, 0), (100, 100)]


def test_each_expansion_layer_caps_visible_edges_and_marks_omissions(monkeypatch):
    names = ["a", *(f"n{i:03d}" for i in range(185)), "hidden"]
    value = injected_graph(monkeypatch, names, [edge("a", name) for name in names[1:]], hidden={"hidden"})
    expanded, omitted = value._expand({"asset:cat.db.a"}, None, "both")
    assert len(expanded) == 180 and omitted == 5
    assert "hidden" not in json.dumps(expanded)


def test_neighbors_filters_relation_direction_and_depth(monkeypatch):
    value = injected_graph(monkeypatch, "abcd", [edge("a", "b"), edge("b", "c"), edge("d", "a", "lineage")])
    one = value.neighbors("asset:cat.db.a", relation="join_key", direction="out", depth=1)
    two = value.neighbors("asset:cat.db.a", relation="join_key", direction="out", depth=2)
    incoming = value.neighbors("asset:cat.db.a", relation="lineage", direction="in", depth=1)
    assert {n["id"] for n in one["nodes"]} == {"asset:cat.db.a", "asset:cat.db.b"}
    assert {n["id"] for n in two["nodes"]} == {"asset:cat.db.a", "asset:cat.db.b", "asset:cat.db.c"}
    assert {n["id"] for n in incoming["nodes"]} == {"asset:cat.db.a", "asset:cat.db.d"}


def test_lineage_with_nullable_column_names_sorts_with_column_edges(monkeypatch):
    unknown = edge("a", "b", "lineage")
    unknown["columns"] = [{"from": None, "to": None}]
    value = injected_graph(monkeypatch, "ab", [unknown, edge("a", "b", "lineage")])
    result = value.neighbors("asset:cat.db.a", relation="lineage")
    assert len(result["edges"]) == 2 and result["truncated"] is False


def test_neighbors_and_path_do_not_include_hidden_nodes_or_counts(monkeypatch):
    visible_edges = [edge("a", "b")]
    hidden_edges = [edge("a", "hidden"), edge("hidden", "c")]
    clean = injected_graph(monkeypatch, ["a", "b", "c"], visible_edges)
    hidden = injected_graph(monkeypatch, ["a", "b", "c", "hidden"], visible_edges + hidden_edges, hidden={"hidden"})
    assert hidden.neighbors("asset:cat.db.a", depth=3) == clean.neighbors("asset:cat.db.a", depth=3)
    assert hidden.path("asset:cat.db.a", "asset:cat.db.c") == clean.path("asset:cat.db.a", "asset:cat.db.c")
    assert hidden.path("asset:cat.db.a", "asset:cat.db.hidden")["paths"] == []
    assert "hidden" not in json.dumps(hidden.neighbors("asset:cat.db.a", depth=3))


def test_neighbors_has_thirty_node_limit_and_exact_visible_omission_count(monkeypatch):
    names = ["a", *(f"n{i:02d}" for i in range(35))]
    value = injected_graph(monkeypatch, names, [edge("a", name) for name in names[1:]])
    out = value.neighbors("asset:cat.db.a")
    assert len(out["nodes"]) == 30 and out["truncated"] is True and out["omitted"] == 6
    ids = {n["id"] for n in out["nodes"]}
    assert all(e["from"] in ids and e["to"] in ids for e in out["edges"])


def test_neighbors_reports_next_layer_omission_when_first_layer_exactly_fills_limit(monkeypatch):
    names = ["a", *(f"n{i:02d}" for i in range(29)), "z"]
    edges = [edge("a", name) for name in names[1:-1]] + [edge("n00", "z")]
    value = injected_graph(monkeypatch, names, edges)
    one = value.neighbors("asset:cat.db.a", depth=1)
    two = value.neighbors("asset:cat.db.a", depth=2)
    assert len(one["nodes"]) == 30 and one["truncated"] is False and one["omitted"] == 0
    assert len(two["nodes"]) == 30 and two["truncated"] is True and two["omitted"] == 1
    assert "asset:cat.db.z" not in {n["id"] for n in two["nodes"]}


def test_path_returns_at_most_three_equal_shortest_paths(monkeypatch):
    middle = ["b", "c", "d", "e"]
    edges = [edge("a", m) for m in middle] + [edge(m, "z", "shared_key") for m in middle]
    edges += [edge("a", "x"), edge("x", "y"), edge("y", "z")]
    value = injected_graph(monkeypatch, ["a", "z", "x", "y", *middle], edges)
    out = value.path("asset:cat.db.a", "asset:cat.db.z")
    assert len(out["paths"]) == 3 and all(len(p["edges"]) == 2 for p in out["paths"])
    assert out["truncated"] is True and out["omitted"] == 1
    assert all(e["clue"] for p in out["paths"] for e in p["edges"] if e["type"] == "shared_key")
    assert value.path("asset:cat.db.a", "asset:cat.db.z") == out


def test_path_does_not_cross_default_depth_limit(monkeypatch):
    value = injected_graph(monkeypatch, "abcde", [edge(a, b) for a, b in zip("abcd", "bcde")])
    assert value.path("asset:cat.db.a", "asset:cat.db.e")["paths"] == []


def test_path_keeps_direct_target_when_edge_and_node_limits_would_sort_it_out(monkeypatch):
    names = ["a", *(f"n{i:03d}" for i in range(181)), "z_target"]
    value = injected_graph(monkeypatch, names, [edge("a", name) for name in names[1:]])
    result = value.path("asset:cat.db.a", "asset:cat.db.z_target")
    assert len(result["paths"]) == 1 and result["paths"][0]["nodes"] == ["asset:cat.db.a", "asset:cat.db.z_target"]
    assert len(result["paths"][0]["edges"]) == 1
    assert result["truncated"] is True and result["omitted"] > 0


def test_path_does_not_claim_longer_route_is_shortest_after_earlier_truncation(monkeypatch):
    branches = [f"b{i:02d}" for i in range(30)]
    names = ["a", *branches, "z_branch", "middle", "target"]
    edges = [edge("a", name) for name in [*branches, "z_branch"]]
    edges += [edge("b00", "middle"), edge("middle", "target"), edge("z_branch", "target")]
    value = injected_graph(monkeypatch, names, edges)
    result = value.path("asset:cat.db.a", "asset:cat.db.target")
    assert result["paths"] == [] and result["truncated"] is True and result["omitted"] > 0
    assert "尚不能确认最短" in result["note"]


def test_asset_visibility_excludes_inactive_catalog_and_denied_assets(monkeypatch):
    items = [asset("a"), asset("hidden"), asset("gone", status="deleted"), asset("external", kind="catalog_table")]
    value = graph(monkeypatch, items, visible={"a", "gone", "external"})
    assert value._asset("a") is items[0]
    assert all(value._asset(name) is None for name in ["hidden", "gone", "external", "missing"])


def test_find_sorts_name_hits_before_comments_and_filters_metric_visibility(monkeypatch):
    a, b = asset("signal_a"), asset("z")
    metric = Metric(id="m1", code="signal_metric", name="指标甲", domain_id="d1")
    hidden = Metric(id="m2", code="signal_hidden", name="指标乙", domain_id="d2")
    old = Metric(id="m3", code="signal_old", name="指标丙", domain_id="d1")
    current = MetricVersion(id="mv1", status="published", description="口径说明")
    previous = MetricVersion(id="mv2", status="affected", description="signal 受影响")
    col = column("signal_column")

    def visible(code):
        if code == hidden.code:
            raise ValueError("不可见")
        return (metric, current, [current], {}) if code == metric.code else (old, previous, [previous], {})

    value = graph(
        monkeypatch,
        [a, b],
        rows={(Metric,): [(old,), (hidden,), (metric,)], (Asset,): [(b,), (a,)], (Asset, ColumnVersion): [(b, col)]},
        metric_sources=visible,
        versions={"v-z": AssetVersion(id="v-z", asset_id="z", comment="signal 注释", key_columns=[], unique_keys=[])},
    )
    out = value.find("signal")
    assert [n["id"] for n in out["nodes"]] == [
        "asset:cat.db.signal_a",
        "column:cat.db.z.signal_column",
        "metric:signal_metric",
        "asset:cat.db.z",
    ]
    assert out["truncated"] is False and out["omitted"] == 0
    assert "hidden" not in json.dumps(out) and "signal_old" not in json.dumps(out)


def test_find_twenty_result_limit_counts_only_readable_matches(monkeypatch):
    readable = [asset(f"item{i:02d}") for i in range(25)]
    excluded = [
        asset("item_hidden"),
        asset("item_deleted", status="deleted"),
        asset("item_external", kind="catalog_table"),
    ]
    all_items = [*readable, *excluded]
    value = graph(
        monkeypatch,
        all_items,
        rows={(Asset,): [(a,) for a in reversed(all_items)]},
        visible={a.id for a in readable} | {"item_deleted", "item_external"},
    )
    out = value.find("item")
    assert len(out["nodes"]) == 20 and out["truncated"] is True and out["omitted"] == 5
    assert [n["qualified"] for n in out["nodes"]] == [f"cat.db.item{i:02d}" for i in range(20)]
    assert not any(part in json.dumps(out) for part in ["hidden", "deleted", "external"])


def test_lineage_edges_preserve_table_and_column_endpoints(monkeypatch):
    observed = LineageEdge(
        id="l1",
        source_asset_id="a",
        target_asset_id="b",
        source_column="source_a",
        target_column="target_a",
        relation="value_direct",
        status="active",
    )
    value = graph(monkeypatch, [asset("a"), asset("b")], rows={(LineageEdge,): [(observed,)]})
    result = list(value._lineage_edges({"a"}, "lineage"))
    assert len(result) == 2 and all(e["type"] == "lineage" and e["clue"] is False for e in result)
    assert {e["from"] for e in result} == {"asset:cat.db.a", "column:cat.db.a.source_a"}
    assert {e["to"] for e in result} == {"asset:cat.db.b", "column:cat.db.b.target_a"}
    assert all(e["columns"] == [{"from": "source_a", "to": "target_a"}] for e in result)


def test_join_key_edges_use_observed_relation_and_approved_paths(monkeypatch):
    observed = LineageEdge(
        id="l1",
        source_asset_id="a",
        target_asset_id="b",
        source_column="key_a",
        target_column="key_b",
        relation="join_key",
        status="active",
    )
    approved = JoinPath(
        id="j1",
        left_asset_id="a",
        right_asset_id="b",
        left_columns=["key_c", "key_d"],
        right_columns=["key_e", "key_f"],
        status="approved",
    )
    value = graph(
        monkeypatch, [asset("a"), asset("b")], rows={(LineageEdge,): [(observed,)], (JoinPath,): [(approved,)]}
    )
    result = list(value._lineage_edges({"a"}, "join_key"))
    tables = [e for e in result if e["from"].startswith("asset:")]
    assert len(tables) == 2 and all(e["type"] == "join_key" and e["clue"] is False for e in result)
    assert tables[1]["columns"] == [{"from": "key_c", "to": "key_e"}, {"from": "key_d", "to": "key_f"}]


def test_metric_column_edges_use_expression_time_and_base_dimensions(monkeypatch):
    metric = Metric(id="m1", code="metric_a", name="指标甲", domain_id="d1")
    version = MetricVersion(
        id="mv1",
        status="published",
        base_asset_id="a",
        expression="SUM(value_a) + SUM(b.extra_a) + SUM(j.other_a)",
        time_column="b.date_a",
        dimensions=[{"column": "b.dim_a"}, {"column": "j.excluded"}],
    )
    cols = [
        column(name, ordinal=i)
        for i, name in enumerate(["value_a", "extra_a", "date_a", "dim_a", "other_a", "excluded", "unused"])
    ]
    value = graph(
        monkeypatch,
        [asset("a")],
        rows={(Metric,): [(metric,)]},
        columns={"a": cols},
        metric_sources=lambda code: (metric, version, [version], {}),
    )
    result = list(value._metric_edges({"a"}, {metric.code}))
    assert len(result) == 5 and all(e["type"] == "metric_column" and not e["clue"] for e in result)
    table = next(e for e in result if e["to"].startswith("asset:"))
    assert {p["to"] for p in table["columns"]} == {"value_a", "extra_a", "date_a", "dim_a"}
    assert all(e["from"] == "metric:metric_a" for e in result)
    version.status = "affected"
    assert list(value._metric_edges({"a"}, {metric.code})) == []


@pytest.mark.parametrize(
    ("left_type", "right_type", "left_key", "right_key", "expected"),
    [
        ("VARCHAR(32)", "varchar", True, False, True),
        (" VARCHAR ( 64 ) ", "VaRcHaR(128)", False, True, True),
        ("VARCHAR(32)", "integer", True, True, False),
        ("varchar", "varchar", False, False, False),
    ],
)
def test_shared_key_requires_normalized_type_and_key_on_at_least_one_side(
    monkeypatch, left_type, right_type, left_key, right_key, expected
):
    a, b = asset("a"), asset("b")
    left, right = column(data_type=left_type, is_key=left_key), column(data_type=right_type, is_key=right_key)
    value = graph(monkeypatch, [a, b], rows={(Asset, ColumnVersion): [(b, right)]}, columns={"a": [left]})
    result = list(value._shared_key_edges({"a"}))
    assert bool(result) is expected
    assert all(e["type"] == "shared_key" and e["clue"] is True for e in result)


@pytest.mark.parametrize("metadata", [{"key_columns": ["key_a"]}, {"unique_keys": [["key_a", "key_b"]]}])
def test_shared_key_accepts_asset_version_keys(monkeypatch, metadata):
    a, b = asset("a"), asset("b")
    version = AssetVersion(id="v-a", asset_id="a", key_columns=[], unique_keys=[])
    for key, value in metadata.items():
        setattr(version, key, value)
    value = graph(
        monkeypatch,
        [a, b],
        rows={(Asset, ColumnVersion): [(b, column())]},
        columns={"a": [column()]},
        versions={"v-a": version},
    )
    assert list(value._shared_key_edges({"a"}))


@pytest.mark.parametrize(
    ("right_datasource", "expected"), [("ds2", False), ("ds1", True)], ids=["different_datasources", "same_datasource"]
)
def test_shared_key_same_name_and_type_require_same_datasource(monkeypatch, right_datasource, expected):
    a, b = asset("a"), asset("b", datasource_id=right_datasource)
    left, right = column(is_key=True), column()
    value = graph(monkeypatch, [a, b], rows={(Asset, ColumnVersion): [(b, right)]}, columns={"a": [left]})
    result = list(value._shared_key_edges({"a"}))
    if expected:
        assert len(result) == 2 and all(e["type"] == "shared_key" and e["clue"] for e in result)
        assert {e["from"] for e in result} == {"asset:cat.db.a", "column:cat.db.a.key_a"}
        assert {e["to"] for e in result} == {"asset:cat.db.b", "column:cat.db.b.key_a"}
    else:
        assert result == []


@pytest.mark.parametrize(
    ("right_datasource", "expected"), [("ds2", False), ("ds1", True)], ids=["different_datasources", "same_datasource"]
)
def test_shared_value_common_values_require_same_datasource(monkeypatch, right_datasource, expected):
    a, b = asset("a"), asset("b", datasource_id=right_datasource)
    left, right = column("dim_a"), column("dim_b")
    value = graph(monkeypatch, [a, b])
    cached = [(a, left, {"成员甲", "成员乙"}), (b, right, {"成员乙", "成员丙"})]
    monkeypatch.setattr(
        value,
        "_cached_columns",
        lambda assets=None: iter(entry for entry in cached if assets is None or entry[0].id in assets),
    )
    result = list(value._shared_value_edges({"a"}))
    if expected:
        assert len(result) == 2 and all(e["type"] == "shared_value" and e["clue"] for e in result)
        assert {e["from"] for e in result} == {"asset:cat.db.a", "column:cat.db.a.dim_a"}
        assert {e["to"] for e in result} == {"asset:cat.db.b", "column:cat.db.b.dim_b"}
    else:
        assert result == []


@pytest.mark.parametrize("missing", [True, False])
def test_shared_value_uses_only_existing_cache_and_never_source_connectors(monkeypatch, missing):
    items = [asset("a"), asset("b"), asset("hidden")]
    value = graph(monkeypatch, items, visible={"a", "b"}, columns={a.id: [column("dim_a")] for a in items})
    calls = []
    values = {
        "avelis:dimvals:a:v-a:dim_a": "成员甲\x1f成员乙",
        "avelis:dimvals:b:v-b:dim_a": "成员乙\x1f成员丙",
        "avelis:dimvals:hidden:v-hidden:dim_a": "成员乙",
    }

    class Cache:
        def scan_iter(self, **kwargs):
            return iter(key for key in values if fnmatch(key, kwargs["match"]))

        def get(self, key):
            calls.append(key)
            return None if missing else values[key]

    monkeypatch.setattr(retrieval.cache, "client", lambda: Cache())
    monkeypatch.setattr(engines, "query_connector", lambda *a, **k: pytest.fail("发现线索不能查询源库"))
    monkeypatch.setattr(engines, "collect_connector", lambda *a, **k: pytest.fail("发现线索不能采集源库"))
    monkeypatch.setattr(tools.service, "query_connector", lambda *a, **k: pytest.fail("发现线索不能走问数连接器"))
    result = list(value._shared_value_edges({"a"}))
    assert not any("hidden" in key for key in calls)
    if missing:
        assert result == []
    else:
        assert result and all(e["type"] == "shared_value" and e["clue"] is True for e in result)
        tables = [e for e in result if e["from"].startswith("asset:")]
        assert {e["from"] for e in tables} | {e["to"] for e in tables} == {"asset:cat.db.a", "asset:cat.db.b"}


def test_shared_value_cache_failure_is_empty_without_connectors(monkeypatch):
    value = graph(monkeypatch, [asset("a")])

    def failed_cache():
        raise ConnectionError("缓存不可用")

    monkeypatch.setattr(retrieval.cache, "client", failed_cache)
    monkeypatch.setattr(engines, "query_connector", lambda *a, **k: pytest.fail("缓存缺失不能补扫源库"))
    assert list(value._shared_value_edges({"a"})) == []


def test_default_enabled_empty_metadata_does_not_access_graph_or_cache(monkeypatch):
    defaults = config.build_settings({"AVELIS_SECRET": "unit-test-secret-only"})
    assert defaults.analysis_retrieval and defaults.analysis_investigation
    assert not defaults.env.get("AVELIS_MODEL_PROVIDER")
    value = graph(monkeypatch)

    def unexpected(*args, **kwargs):
        pytest.fail("空来源闭包不应扫描元数据、缓存或源库")

    monkeypatch.setattr(value, "_expand", unexpected)
    monkeypatch.setattr(value, "_rows", unexpected)
    monkeypatch.setattr(retrieval.cache, "client", unexpected)
    monkeypatch.setattr(engines, "query_connector", unexpected)
    assert retrieval.reference_tables(value, [], {}) == []


def test_reference_tables_with_assets_but_no_graph_edges_does_not_access_cache(monkeypatch):
    value = graph(monkeypatch, [asset("source"), asset("unrelated")])
    expanded = []
    candidates = value._candidate_edges

    def counted(frontier, relation):
        expanded.append(relation)
        yield from candidates(frontier, relation)

    def unexpected(*args, **kwargs):
        pytest.fail("没有可用图边的自动预检不应等待 Redis 或查询源库")

    monkeypatch.setattr(value, "_candidate_edges", counted)
    monkeypatch.setattr(retrieval.cache, "client", unexpected)
    monkeypatch.setattr(engines, "query_connector", unexpected)
    assert (
        retrieval.reference_tables(value, [SimpleNamespace(base_asset_id="source")], {"source": value._asset("source")})
        == []
    )
    assert expanded == ["join_key", "shared_key", "lineage"]


@pytest.mark.parametrize("relation", ["join_key", "shared_key", "lineage"])
def test_reference_tables_with_graph_edges_retains_shared_value_discovery(monkeypatch, relation):
    value = injected_graph(
        monkeypatch,
        ["source", "linked", "values"],
        [edge("source", "linked", relation), edge("source", "values", "shared_value")],
    )
    candidates = value._candidate_edges

    def first_lineage_only(frontier, kind):
        for item in candidates(frontier, kind):
            yield item
            if kind == "lineage":
                pytest.fail("自动预检只需首条已授权血缘边，不应继续扫描血缘候选")

    monkeypatch.setattr(value, "_candidate_edges", first_lineage_only)
    refs = retrieval.reference_tables(
        value, [SimpleNamespace(base_asset_id="source")], {"source": value._asset("source")}
    )
    found = next(item for item in refs if item["table"] == "cat.db.values")
    assert found["via"] == [{"type": "shared_value", "columns": [{"from": "key_0", "to": "key_0"}], "clue": True}]


def test_explicit_shared_value_neighbors_can_discover_without_structural_graph_edges(monkeypatch):
    value = injected_graph(monkeypatch, ["source", "values"], [edge("source", "values", "shared_value")])
    found = value.neighbors("asset:cat.db.source", relation="shared_value")
    assert [item["type"] for item in found["edges"]] == ["shared_value"]
    assert {item["qualified"] for item in found["nodes"]} == {"cat.db.source", "cat.db.values"}


def test_reference_tables_sort_outside_then_columns_then_name_and_only_direct_neighbors(monkeypatch):
    names = ["a", "inside", "c", "d", "e", "f", "g"]
    edges = [
        edge("a", "c", "shared_key", count=2),
        edge("a", "d", "shared_value"),
        edge("a", "e", "join_key"),
        edge("c", "f", "join_key", count=4),
        edge("a", "g", "lineage"),
    ]
    value = injected_graph(monkeypatch, names, edges)
    closure = {name: value._asset(name) for name in ["a", "inside"]}
    base = SimpleNamespace(base_asset_id="a")
    refs = retrieval.reference_tables(value, [base], closure)
    assert [r["table"] for r in refs] == ["cat.db.c", "cat.db.d", "cat.db.e", "cat.db.inside"]
    assert not {"cat.db.a", "cat.db.f", "cat.db.g"} & {r["table"] for r in refs}
    assert refs == retrieval.reference_tables(value, [base], dict(reversed(list(closure.items()))))
    assert len(retrieval.reference_tables(value, [base], closure, k=2)) == 2
    assert retrieval.reference_tables(value, [base], closure, k=0) == []
    assert any(reason["type"] == "shared_key" and reason["clue"] is True for reason in refs[0]["via"])
    assert refs[-1]["via"] == [{"type": "lineage", "columns": [], "clue": False}]


def test_reference_tables_exclude_cross_datasource_clues_before_ranking_and_limit(monkeypatch):
    source = asset("source")
    cross_key = asset("a_cross_key", datasource_id="ds2")
    cross_value = asset("a_cross_value", datasource_id="ds2")
    same_key, same_value = asset("z_same_key"), asset("z_same_value")
    key, dimension = column(is_key=True), column("dim_a")
    value = graph(
        monkeypatch,
        [source, cross_key, cross_value, same_key, same_value],
        rows={(Asset, ColumnVersion): [(cross_key, key), (same_key, key)]},
        columns={
            source.id: [key],
            cross_key.id: [key],
            same_key.id: [key],
            cross_value.id: [dimension],
            same_value.id: [dimension],
        },
    )
    cached = [(source, key, {"成员甲"}), (cross_value, dimension, {"成员甲"}), (same_value, dimension, {"成员甲"})]
    monkeypatch.setattr(
        value,
        "_cached_columns",
        lambda assets=None: iter(entry for entry in cached if assets is None or entry[0].id in assets),
    )
    versions, closure = [SimpleNamespace(base_asset_id=source.id)], {source.id: source}
    refs = retrieval.reference_tables(value, versions, closure, k=2)
    assert [r["table"] for r in refs] == ["cat.db.z_same_key", "cat.db.z_same_value"]
    assert [[reason["type"] for reason in r["via"]] for r in refs] == [["shared_key"], ["shared_value"]]
    assert retrieval.reference_tables(value, versions, closure) == refs


def test_reference_tables_expand_only_source_closure_once_while_neighbors_allows_three(monkeypatch):
    value = injected_graph(monkeypatch, "abcd", [edge("a", "b"), edge("b", "c"), edge("c", "d")])
    calls = []
    expand = value._expand

    def counted(frontier, relation, direction, **kwargs):
        calls.append(set(frontier))
        return expand(frontier, relation, direction, **kwargs)

    monkeypatch.setattr(value, "_expand", counted)
    refs = retrieval.reference_tables(value, [SimpleNamespace(base_asset_id="a")], {"a": value._asset("a")})
    assert [r["table"] for r in refs] == ["cat.db.b"]
    assert calls and all(frontier == {"asset:cat.db.a"} for frontier in calls)
    assert {n["qualified"] for n in value.neighbors("asset:cat.db.a", depth=3)["nodes"]} == {
        "cat.db.a",
        "cat.db.b",
        "cat.db.c",
        "cat.db.d",
    }


def test_reference_tables_rank_recursive_sibling_after_non_sibling_before_internal(monkeypatch):
    names = ["source", "base", "middle", "other", "a_sibling", "z_external"]
    value = injected_graph(
        monkeypatch,
        names,
        [edge("source", "a_sibling", count=4), edge("source", "z_external")],
        columns={"a_sibling": [column("date_a", "DATE")]},
        upstream={"source": ["base"], "a_sibling": ["middle"], "middle": ["base"], "z_external": ["other"]},
    )
    closure = {name: value._asset(name) for name in ["source", "base"]}
    refs = retrieval.reference_tables(value, [SimpleNamespace(base_asset_id="source")], closure)
    assert [r["table"] for r in refs] == ["cat.db.z_external", "cat.db.a_sibling", "cat.db.base"]
    assert [r["sibling"] for r in refs] == [False, True, True]


def test_reference_sibling_detection_filters_unreadable_upstream_assets(monkeypatch):
    names = ["source", "hidden", "a_external", "z_sibling"]
    value = injected_graph(
        monkeypatch,
        names,
        [edge("source", "a_external"), edge("source", "z_sibling")],
        hidden={"hidden"},
        upstream={"source": ["hidden"], "a_external": ["hidden"], "z_sibling": ["source"]},
    )
    closure = {"source": value._asset("source"), "hidden": value.session.get(Asset, "hidden")}
    refs = retrieval.reference_tables(value, [SimpleNamespace(base_asset_id="source")], closure)
    assert [r["table"] for r in refs] == ["cat.db.a_external", "cat.db.z_sibling"]
    assert [r["sibling"] for r in refs] == [False, True]
    assert "hidden" not in json.dumps(refs)


@pytest.mark.parametrize(("kind", "status"), [("catalog_table", "active"), ("table", "deleted")])
def test_reference_sibling_detection_retains_authorized_terminal_sources_outside_searchable_kinds(
    monkeypatch, kind, status
):
    names = ["source", "inside", "base", "a_external", "z_sibling"]
    value = injected_graph(
        monkeypatch,
        names,
        [edge("source", "a_external"), edge("source", "z_sibling")],
        upstream={"source": ["inside"], "inside": ["base"], "z_sibling": ["base"]},
    )
    terminal = value.session.get(Asset, "base")
    terminal.kind, terminal.status = kind, status
    closure = {name: value.session.get(Asset, name) for name in ["source", "inside", "base"]}
    refs = retrieval.reference_tables(value, [SimpleNamespace(base_asset_id="source")], closure)
    assert [r["table"] for r in refs] == ["cat.db.a_external", "cat.db.z_sibling", "cat.db.inside"]
    assert [r["sibling"] for r in refs] == [False, True, True]
    assert "cat.db.base" not in {r["table"] for r in refs}


@pytest.mark.parametrize(
    "data_type", [" DATE ", "Datetime(6)", "TimeStamp(3)", "TIMESTAMP(6) WITH TIME ZONE", "timestamp without time zone"]
)
def test_reference_tables_rank_normalized_date_types_before_more_connections_and_name(monkeypatch, data_type):
    value = injected_graph(
        monkeypatch,
        ["source", "a_plain", "z_dated"],
        [edge("source", "a_plain", count=4), edge("source", "z_dated")],
        columns={"a_plain": [column("date_named_only", "VARCHAR")], "z_dated": [column("window_a", data_type)]},
    )
    refs = retrieval.reference_tables(
        value, [SimpleNamespace(base_asset_id="source")], {"source": value._asset("source")}
    )
    assert [r["table"] for r in refs] == ["cat.db.z_dated", "cat.db.a_plain"]
    assert [r["date_columns"] for r in refs] == [["window_a"], []]


@pytest.mark.parametrize("data_type", ["date_text", "datetime_text", "timestamp_text"])
def test_reference_date_columns_do_not_match_type_name_prefixes(monkeypatch, data_type):
    value = injected_graph(
        monkeypatch,
        ["source", "a_plain", "z_pseudo"],
        [edge("source", "a_plain"), edge("source", "z_pseudo")],
        columns={"z_pseudo": [column("window_a", data_type)]},
    )
    refs = retrieval.reference_tables(
        value, [SimpleNamespace(base_asset_id="source")], {"source": value._asset("source")}
    )
    assert [r["table"] for r in refs] == ["cat.db.a_plain", "cat.db.z_pseudo"]
    assert all(r["date_columns"] == [] for r in refs)


def test_reference_tables_rank_fifteen_same_layer_candidates_by_signals_and_cap_at_twelve(monkeypatch):
    groups = {prefix: [f"{prefix}{i:02d}" for i in range(5)] for prefix in ["a", "b", "z"]}
    candidates = [name for group in groups.values() for name in group]
    edges = [edge("source", name, count=i + 1) for group in groups.values() for i, name in enumerate(group)]
    dated = {name: [column("window_a", "DATE")] for name in [*groups["a"], *groups["z"]]}
    upstream = {name: ["source"] for name in groups["a"]}
    value = injected_graph(monkeypatch, ["source", *candidates], edges, columns=dated, upstream=upstream)
    closure, versions = {"source": value._asset("source")}, [SimpleNamespace(base_asset_id="source")]
    expected = [f"cat.db.{name}" for prefix in ["z", "b", "a"] for name in reversed(groups[prefix])][:12]
    refs = retrieval.reference_tables(value, versions, closure)
    assert len(refs) == 12 and [r["table"] for r in refs] == expected
    assert retrieval.reference_tables(value, versions, closure, k=12) == refs
    assert retrieval.reference_tables(value, versions, closure, k=99) == refs
    assert retrieval.reference_tables(value, versions, closure, k=3) == refs[:3]
    assert retrieval.reference_tables(value, versions, closure, k=-1) == []


def test_reference_tables_use_compact_entry_format_with_three_date_column_names(monkeypatch):
    value = injected_graph(monkeypatch, ["a", "b"], [edge("a", "b", count=7)])
    cols = [column(f"key_{i}", "INTEGER", ordinal=i + 1, comment=f"连接列 {i}") for i in range(7)]
    cols += [
        column("time_z", "TIMESTAMP", ordinal=8, comment="日期甲"),
        column("date_a", " DATE ", ordinal=9, comment="日期乙"),
        column("datetime_b", "DATETIME(6)", ordinal=10, comment="日期丙"),
        column("timestamp_c", "TimeStamp(3)", ordinal=11, comment="日期丁"),
        column("unrelated", "VARCHAR", ordinal=12, comment="普通列"),
    ]
    monkeypatch.setattr(value, "_columns", lambda item: iter(cols if item.id == "b" else []))
    refs = retrieval.reference_tables(value, [SimpleNamespace(base_asset_id="a")], {"a": value._asset("a")})
    assert refs == [
        {
            "table": "cat.db.b",
            "comment": "b 注释",
            "via": [{"type": "join_key", "columns": edge("a", "b", count=7)["columns"], "clue": False}],
            "date_columns": ["date_a", "datetime_b", "time_z"],
            "sibling": False,
        }
    ]
    assert set(refs[0]) == {"table", "comment", "via", "date_columns", "sibling"}
    assert set(refs[0]["via"][0]) == {"type", "columns", "clue"}
    assert "连接列" not in json.dumps(refs, ensure_ascii=False) and "普通列" not in json.dumps(refs, ensure_ascii=False)


def test_reference_ranking_counts_distinct_candidate_columns_and_keeps_asymmetric_via(monkeypatch):
    asymmetric = edge("a", "c", "join_key")
    asymmetric["columns"] = [
        {"from": "source_x", "to": "target_y"},
        {"from": "source_z", "to": "target_y"},
        {"from": "source_w", "to": "target_y"},
    ]
    paired = edge("a", "d", "join_key")
    paired["columns"] = [{"from": "p", "to": "p"}, {"from": "q", "to": "q"}]
    value = injected_graph(monkeypatch, ["a", "c", "d"], [asymmetric, paired])
    cols = {"c": [column("target_y"), column("source_x")], "d": [column("p"), column("q")]}
    monkeypatch.setattr(value, "_columns", lambda item: iter(cols.get(item.id, [])))
    refs = retrieval.reference_tables(value, [SimpleNamespace(base_asset_id="a")], {"a": value._asset("a")})
    assert [r["table"] for r in refs] == ["cat.db.d", "cat.db.c"]
    candidate = next(r for r in refs if r["table"] == "cat.db.c")
    assert candidate["via"] == [{key: asymmetric[key] for key in ["type", "columns", "clue"]}]
    assert candidate["date_columns"] == []


def test_reference_block_keeps_metadata_and_clue_warning_within_budget():
    refs = [
        {
            "table": f"cat.db.t{i}",
            "comment": "注释" * 400,
            "via": [{"type": "shared_key", "columns": [{"from": "key_a", "to": "key_a"}], "clue": True}],
            "date_columns": ["date_a"],
            "sibling": False,
        }
        for i in range(5)
    ]
    out = retrieval.reference_block(refs)
    assert out.startswith("参考表（元数据图线索）") and len(out) <= 2500
    assert "省略" in out and "张" in out and CLUE_NOTE in out
    assert "cat.db.t0" in out and "shared_key" in out and "key_a" in out
    entries = [line for line in out.splitlines() if line.startswith("{")]
    assert 0 < len(entries) < len(refs)
    assert [json.loads(line) for line in entries] == refs[: len(entries)]
    assert entries == [json.dumps(ref, ensure_ascii=False, separators=(",", ":")) for ref in refs[: len(entries)]]
    assert f"省略 {len(refs) - len(entries)} 张参考表" in out
    assert retrieval.reference_block(refs, limit=10000) == out


def test_compact_reference_block_fits_twelve_small_tables():
    refs = [
        {
            "table": f"cat.db.t{i}",
            "comment": "注释",
            "via": [{"type": "shared_key", "columns": [{"from": "key_a", "to": "key_a"}], "clue": True}],
            "date_columns": ["date_a"],
            "sibling": False,
        }
        for i in range(12)
    ]
    out = retrieval.reference_block(refs)
    assert len(out) <= 2500 and "省略" not in out
    assert [json.loads(line) for line in out.splitlines() if line.startswith("{")] == refs


def test_reference_block_handles_single_oversized_table_and_small_budget():
    refs = [{"table": "cat.db.a", "comment": "注释" * 4000, "via": [], "date_columns": [], "sibling": False}]
    out = retrieval.reference_block(refs)
    assert len(out) <= 2500 and "省略" in out and "1" in out
    assert "cat.db.a" not in out
    for limit in [0, 1, 12, 120]:
        limited = retrieval.reference_block(refs, limit=limit)
        assert len(limited) <= limit and "cat.db.a" not in limited
        assert not limited or "省略 1 张参考表" in limited


def test_context_reference_block_follows_source_metadata_and_obeys_total_budget(monkeypatch):
    value = box(monkeypatch, enabled=True)
    value.precheck_meta = [{"table": "cat.db.a", "columns": []}]
    value.precheck_refs = [
        {"table": "cat.db.b", "comment": "参考表注释", "via": [], "date_columns": [], "sibling": False}
    ]
    out = value.context_pack()
    assert out.index("来源表") < out.index("参考表（元数据图线索）") and "cat.db.b" in out
    assert len(out) <= value.PACK_CHARS
    value.PACK_CHARS = 450
    value.precheck_meta[0]["comment"] = "元数据" * 500
    limited = value.context_pack()
    assert len(limited) <= value.PACK_CHARS and "放不下" in limited


def test_change_precheck_references_are_opt_in_and_follow_source_metadata(monkeypatch):
    value = box(monkeypatch, enabled=True)
    value.run.window = {}
    ordered = []
    for name in ["describe_metric", "changes", "data_health", "explain_change", "calendar_adjust", "query_metric"]:
        monkeypatch.setattr(value, name, lambda **kwargs: "{}")
    monkeypatch.setattr(value, "_source_metadata", lambda code: ordered.append("source") or [])
    monkeypatch.setattr(value, "_metric_sources", lambda code: (None, None, [], {}))
    expected = [{"table": "cat.db.b", "comment": "参考表", "via": [], "date_columns": [], "sibling": False}]

    def references(*args, **kwargs):
        assert kwargs == {"k": 12}
        ordered.append("reference")
        return expected

    monkeypatch.setattr(retrieval, "reference_tables", references)
    scope = Scope(
        question_type="change",
        metric_codes=["metric_a"],
        start_date="2026-09-01",
        end_date="2026-09-30",
        compare_start="2026-08-01",
        compare_end="2026-08-31",
    )
    value.precheck(scope)
    assert ordered[-2:] == ["source", "reference"] and value.precheck_refs == expected
    settings(monkeypatch)
    ordered.clear()
    value.precheck(scope)
    assert ordered == ["source"]
