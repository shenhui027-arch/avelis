"""元数据图的 PostgreSQL 集成测试：只使用 tests/db 的独立测试库，不访问源库或 Redis。"""

from __future__ import annotations

import uuid
from fnmatch import fnmatchcase
from types import SimpleNamespace

import pytest

from avelis import cache, engines
from avelis.analysis.retrieval import MetadataGraph
from avelis.analysis.tools import AnalysisToolbox, Budget
from avelis.ask import service
from avelis.db import session_scope
from avelis.governance import access
from avelis.models import (
    Asset,
    AssetColumn,
    AssetVersion,
    CollectionRun,
    ColumnVersion,
    Datasource,
    Domain,
    DomainMember,
    JoinPath,
    LineageEdge,
    Metric,
    MetricVersion,
    User,
)

pytestmark = pytest.mark.db


@pytest.fixture
def world(factory, monkeypatch):
    """身份、当前观察版本和发布版本均写入真实元数据库；缓存只提供测试指定的已有值。"""
    tag = uuid.uuid4().hex[:10]
    owner, member = factory.user(("admin",)), factory.user()
    values: dict[str, str] = {}
    reads: list[str] = []

    class CachedValues:
        @staticmethod
        def scan_iter(match, count):
            assert match.startswith("avelis:dimvals:") and count > 0
            return iter(key for key in sorted(values) if fnmatchcase(key, match))

        @staticmethod
        def get(key):
            reads.append(key)
            return values.get(key)

    def no_source(*args, **kwargs):
        pytest.fail("元数据图不得请求源库连接器")

    monkeypatch.setattr(cache, "client", lambda: CachedValues())
    monkeypatch.setattr(service, "query_connector", no_source)
    monkeypatch.setattr(engines, "query_connector", no_source)
    with session_scope() as session:
        mine = Domain(code=f"r{tag}", name="图测试域", owner_id=owner)
        other = Domain(code=f"h{tag}", name="不可见域", owner_id=owner)
        source = Datasource(name=f"graph{tag}", kind="postgres", host="unused", port=1, options={})
        session.add_all([mine, other, source])
        session.flush()
        session.add(DomainMember(domain_id=mine.id, user_id=member, role="viewer"))
        run = CollectionRun(datasource_id=source.id, trigger="manual", status="succeeded")
        session.add(run)
        session.flush()

        def version(asset, columns, *, number=1, comment="当前注释", key_columns=None, unique_keys=None):
            observed = AssetVersion(
                asset_id=asset.id,
                version_no=number,
                observed_run_id=run.id,
                last_seen_run_id=run.id,
                kind=asset.kind,
                catalog_name=asset.catalog_name,
                db_name=asset.db_name,
                name=asset.name,
                comment=comment,
                key_columns=key_columns or [],
                unique_keys=unique_keys or [],
                content_hash="a" * 64,
            )
            session.add(observed)
            session.flush()
            for ordinal, (name, data_type, is_key) in enumerate(columns, 1):
                column = AssetColumn(asset_id=asset.id, name=name)
                session.add(column)
                session.flush()
                session.add(
                    ColumnVersion(
                        asset_version_id=observed.id,
                        column_id=column.id,
                        ordinal=ordinal,
                        name=name,
                        data_type=data_type,
                        is_key=is_key,
                        comment=f"{name} 注释",
                    )
                )
            asset.current_version_id = observed.id
            session.flush()
            return observed

        def asset(name, columns=(("n", "BIGINT", False),), *, hidden=False, status="active", kind="table", **kwargs):
            obj = Asset(
                datasource_id=source.id,
                catalog_name="catalog",
                db_name=f"g{tag}",
                name=name,
                kind=kind,
                status=status,
                domain_id=other.id if hidden else mine.id,
            )
            session.add(obj)
            session.flush()
            version(obj, columns, **kwargs)
            return obj

        def edge(left, right, *, relation="value_direct", status="active", left_column="n", right_column="n"):
            obj = LineageEdge(
                origin="manual",
                source_asset_id=left.id,
                source_ref=qualified(left),
                source_column=left_column,
                target_asset_id=right.id,
                target_asset_version_id=right.current_version_id,
                target_column=right_column,
                relation=relation,
                status=status,
            )
            session.add(obj)
            session.flush()
            return obj

        def metric(base, *, name="发布口径", status="published", description="口径说明"):
            obj = Metric(
                code=f"m{uuid.uuid4().hex[:12]}", name=name, domain_id=mine.id, owner_id=owner, created_by=owner
            )
            session.add(obj)
            session.flush()
            published = MetricVersion(
                metric_id=obj.id,
                version_no=1,
                status=status,
                description=description,
                base_asset_id=base.id,
                expression="SUM(b.n)",
                time_column="d",
                timezone="UTC",
                dimensions=[{"name": "分类", "column": "b.category"}],
                created_by=owner,
            )
            session.add(published)
            session.flush()
            return obj

        user = session.get(User, member)
        allowed = access.domain_ids(session, user)
        box = AnalysisToolbox(session, user, SimpleNamespace(model_adapter=None), service.Catalog([], {}), Budget())
        box._allowed = allowed

        def graph():
            session.flush()
            return MetadataGraph(session, user, allowed, metric_sources=box._metric_sources)

        yield SimpleNamespace(
            session=session,
            tag=tag,
            asset=asset,
            version=version,
            edge=edge,
            metric=metric,
            graph=graph,
            owner=owner,
            values=values,
            reads=reads,
        )


def qualified(asset):
    return f"{asset.catalog_name}.{asset.db_name}.{asset.name}"


def node(asset):
    return f"asset:{qualified(asset)}"


def test_find_counts_only_visible_active_objects_and_uses_current_versions(world):
    """命中超过一页时，真实 SQL 的排序与省略数不包含无权、停用和目录映射对象。"""
    prefix = f"find_{world.tag}"
    visible = [world.asset(f"{prefix}_{i:03}") for i in range(123)]
    world.asset(f"{prefix}_hidden", hidden=True)
    world.asset(f"{prefix}_inactive", status="deleted")
    world.asset(f"{prefix}_catalog", kind="catalog_table")
    older = world.asset("versioned", columns=((f"old_{world.tag}", "VARCHAR(20)", True),))
    world.version(older, (("current_col", "BIGINT", False),), number=2)
    result = world.graph().find(prefix)
    assert [entry["id"] for entry in result["nodes"]] == [node(a) for a in visible[:20]]
    assert result["truncated"] is True and result["omitted"] == 103
    assert world.graph().find(f"old_{world.tag}")["nodes"] == []


def test_hidden_asset_cannot_be_a_neighbor_or_a_path_bridge(world):
    left, hidden, right = world.asset("left"), world.asset("hidden", hidden=True), world.asset("right")
    world.edge(left, hidden)
    world.edge(hidden, right)
    graph = world.graph()
    neighbors = graph.neighbors(node(left), relation="lineage", depth=3)
    assert [entry["id"] for entry in neighbors["nodes"]] == [node(left)]
    assert neighbors["edges"] == [] and neighbors["omitted"] == 0
    assert graph.path(node(left), node(right))["paths"] == []
    invisible = graph.neighbors(node(hidden), depth=1)
    missing = graph.neighbors(f"asset:catalog.g{world.tag}.missing", depth=1)
    assert invisible == missing


def test_all_edge_types_are_backed_by_metadata_and_cached_values(world):
    base = world.asset(
        "base",
        columns=(
            ("n", "BIGINT", False),
            ("d", "DATE", False),
            ("code", "VARCHAR(32)", True),
            ("category", "VARCHAR(30)", False),
        ),
    )
    upstream = world.asset("upstream")
    approved = world.asset("approved", columns=(("ref", "BIGINT", False),))
    observed = world.asset("observed")
    shared_key = world.asset("shared_key", columns=(("code", "varchar", False),))
    shared_value = world.asset("shared_value", columns=(("label", "TEXT", False),))
    world.edge(upstream, base)
    world.edge(base, observed, relation="join_key")
    world.session.add(
        JoinPath(
            code=f"j{world.tag}",
            name="批准路径",
            left_asset_id=base.id,
            left_columns=["n"],
            right_asset_id=approved.id,
            right_columns=["ref"],
            cardinality="n1",
            status="approved",
            created_by=world.owner,
        )
    )
    metric = world.metric(base)
    world.values[f"avelis:dimvals:{base.id}:{base.current_version_id}:category"] = "甲\x1f乙"
    world.values[f"avelis:dimvals:{shared_value.id}:{shared_value.current_version_id}:label"] = "乙\x1f丙"
    result = world.graph().neighbors(node(base), depth=1)
    by_type = {
        kind: [e for e in result["edges"] if e["type"] == kind]
        for kind in ("lineage", "metric_column", "join_key", "shared_key", "shared_value")
    }
    assert all(by_type.values())
    assert any(node(approved) in (e["from"], e["to"]) for e in by_type["join_key"])
    assert any(node(observed) in (e["from"], e["to"]) for e in by_type["join_key"])
    assert any(node(shared_key) in (e["from"], e["to"]) for e in by_type["shared_key"])
    assert all(e["clue"] is True for kind in ("shared_key", "shared_value") for e in by_type[kind])
    assert "不是批准的关联" in result["note"]
    metric_edges = world.graph().neighbors(f"metric:{metric.code}", relation="metric_column")["edges"]
    targets = {e["to"] for e in metric_edges}
    assert {f"column:{qualified(base)}.{c}" for c in ("n", "d", "category")} <= targets


@pytest.mark.parametrize("key", ["is_key", "key_columns", "unique_keys"])
def test_shared_key_accepts_each_existing_key_source_and_normalizes_type(world, key):
    kwargs = {key: ["code"]} if key == "key_columns" else {"unique_keys": [["code"]]} if key == "unique_keys" else {}
    base = world.asset("base", columns=(("code", "VARCHAR(32)", key == "is_key"), ("plain", "BIGINT", False)), **kwargs)
    same = world.asset("same", columns=(("code", "varchar", False), ("plain", "BIGINT", False)))
    world.asset("wrong_type", columns=(("code", "BIGINT", False),))
    result = world.graph().neighbors(node(base), relation="shared_key")
    assert {entry["id"] for entry in result["nodes"]} == {node(base), node(same)}
    assert all(e["clue"] and e["columns"] == [{"from": "code", "to": "code"}] for e in result["edges"])


def test_shared_value_requires_current_visible_cached_values(world):
    base = world.asset("base", columns=(("category", "VARCHAR", False),))
    other = world.asset("other", columns=(("category", "VARCHAR", False),))
    hidden = world.asset("hidden", columns=(("category", "VARCHAR", False),), hidden=True)
    old = other.current_version_id
    world.version(other, (("category", "VARCHAR", False),), number=2)
    world.values[f"avelis:dimvals:{base.id}:{base.current_version_id}:category"] = "甲"
    world.values[f"avelis:dimvals:{other.id}:{old}:category"] = "甲"
    world.values[f"avelis:dimvals:{hidden.id}:{hidden.current_version_id}:category"] = "甲"
    assert world.graph().neighbors(node(base), relation="shared_value")["edges"] == []
    assert all(hidden.id not in key and old not in key for key in world.reads)
    world.values[f"avelis:dimvals:{other.id}:{other.current_version_id}:category"] = "甲"
    edges = world.graph().neighbors(node(base), relation="shared_value")["edges"]
    assert edges and all(e["clue"] is True for e in edges)
    world.values.clear()
    assert world.graph().neighbors(node(base), relation="shared_value")["edges"] == []


def test_path_returns_at_most_three_shortest_paths_from_real_edges(world):
    start, end = world.asset("start"), world.asset("end")
    for i in range(4):
        middle = world.asset(f"middle_{i}")
        world.edge(start, middle)
        world.edge(middle, end)
    first, second = world.asset("long_first"), world.asset("long_second")
    world.edge(start, first)
    world.edge(first, second)
    world.edge(second, end)
    result = world.graph().path(node(start), node(end))
    assert len(result["paths"]) == 3
    assert all(
        len(path["edges"]) == 2 and path["nodes"][0] == node(start) and path["nodes"][-1] == node(end)
        for path in result["paths"]
    )
    assert result["truncated"] is True and result["omitted"] >= 1
