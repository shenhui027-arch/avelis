"""关联路径的血缘图 HTTP 契约：内存 SQLite 执行范围与可见性过滤。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis.api import catalog
from avelis.api.deps import current_user, get_session
from avelis.models import (
    Asset,
    Datasource,
    Domain,
    DomainMember,
    IdentityLink,
    JoinPath,
    LineageEdge,
    User,
    UserRole,
)


@pytest.fixture
def api(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (User, UserRole, Domain, DomainMember, Datasource, Asset, IdentityLink, JoinPath, LineageEdge):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    monkeypatch.setattr(catalog, "current_fingerprint_map", lambda session: {})
    monkeypatch.setattr(catalog, "effective_grains", lambda session, asset_id, fingerprints: [])
    app = FastAPI()
    app.include_router(catalog.router)
    with Session(engine, expire_on_commit=False) as session:
        actor = User(id="actor", username="actor", display_name="分析用户", password_hash="unused")
        session.add_all([
            actor, UserRole(user_id=actor.id, role="analyst"),
            Domain(id="visible", code="visible", name="可见域", owner_id=actor.id),
            Domain(id="hidden", code="hidden", name="其他域", owner_id=actor.id),
            DomainMember(user_id=actor.id, domain_id="visible", role="viewer"),
            Datasource(id="ds", name="离线源", kind="fake", host="offline", port=1),
        ])
        for name, domain, status in (("a", "visible", "active"), ("b", "visible", "active"),
                                     ("c", "visible", "active"), ("h", "hidden", "active"),
                                     ("d", "visible", "deleted")):
            session.add(Asset(id=name, datasource_id="ds", catalog_name="cat", db_name="db", name=name,
                              kind="table", domain_id=domain, status=status))
        for name, left, right, status in (("approved", "a", "b", "approved"),
                                          ("pending", "b", "c", "pending"),
                                          ("rejected", "a", "c", "rejected"),
                                          ("affected", "a", "c", "affected"),
                                          ("hidden", "a", "h", "pending"),
                                          ("deleted", "a", "d", "approved")):
            session.add(JoinPath(id=name, code=name, name="按字段关联", left_asset_id=left,
                                 left_columns=["key_a", "key_b"], right_asset_id=right,
                                 right_columns=["ref_a", "ref_b"], cardinality="n1", status=status,
                                 created_by=actor.id))
        session.commit()
        app.dependency_overrides[current_user] = lambda: actor
        app.dependency_overrides[get_session] = lambda: session
        with TestClient(app) as client:
            yield client, session
    engine.dispose()


def test_graph_returns_visible_approved_and_pending_joins_separate_from_edges(api):
    client, _ = api
    response = client.get("/api/lineage/graph")
    assert response.status_code == 200, response.text
    graph = response.json()
    assert {node["id"] for node in graph["nodes"]} == {"a", "b", "c"}
    assert graph["edges"] == []
    assert graph["joins"] == [
        {"id": "approved", "code": "approved", "left_asset_id": "a", "left_columns": ["key_a", "key_b"],
         "right_asset_id": "b", "right_columns": ["ref_a", "ref_b"], "cardinality": "n1", "status": "approved"},
        {"id": "pending", "code": "pending", "left_asset_id": "b", "left_columns": ["key_a", "key_b"],
         "right_asset_id": "c", "right_columns": ["ref_a", "ref_b"], "cardinality": "n1", "status": "pending"},
    ]


def test_graph_joins_require_both_endpoints_in_current_graph(api):
    client, session = api
    scoped = client.get("/api/lineage/graph", params={"asset_id": "a"}).json()
    assert {node["id"] for node in scoped["nodes"]} == {"a"}
    assert scoped["joins"] == []
    session.add(LineageEdge(id="edge", source_asset_id="a", source_ref="cat.db.a", source_column="key_a",
                            target_asset_id="b", target_column="ref_a", relation="value_direct", origin="parsed"))
    session.commit()
    response = client.get("/api/lineage/graph", params={"asset_id": "a"})
    assert response.status_code == 200, response.text
    graph = response.json()
    assert {node["id"] for node in graph["nodes"]} == {"a", "b"}
    assert [path["id"] for path in graph["joins"]] == ["approved"]
    assert len(graph["edges"]) == 1
    assert (graph["edges"][0]["source"], graph["edges"][0]["target"]) == ("a", "b")
    assert graph["edges"][0]["relations"] == ["value_direct"]


def test_graph_hidden_scope_returns_no_join_or_asset(api):
    client, _ = api
    response = client.get("/api/lineage/graph", params={"domain_ids": "hidden"})
    assert response.status_code == 200, response.text
    assert response.json() == {"nodes": [], "edges": [], "identity_links": [], "joins": []}
