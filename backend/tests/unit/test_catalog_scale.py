"""目录筛选与分页在 SQL 中完成；详情查询只随实际依赖层数变化。"""

from contextlib import contextmanager
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from avelis.api import catalog as api
from avelis.config import get_settings
from avelis.governance import access
from avelis.lineage.sql import PARSER_VERSION
from avelis.models import (
    Asset,
    AssetColumn,
    AssetVersion,
    ColumnFingerprint,
    ColumnVersion,
    Confirmation,
    Definition,
    Domain,
    DomainMember,
    Grain,
    IdentityLink,
    LineageEdge,
    ParseResult,
    User,
    UserRole,
)
from tests.unit.test_ossie_import import catalog  # noqa: F401


@contextmanager
def queries(session):
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append((statement, parameters))

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def assets(catalog, **kwargs):
    return api.list_assets(user=catalog.users["reader"], session=catalog.session, limit=100, offset=0, **kwargs)


def add_asset(session, identifier, *, name=None, domain="visible", columns=0, kind="table"):
    asset = Asset(
        id=identifier,
        datasource_id="source",
        catalog_name="catalog",
        db_name="schema_a",
        name=name or identifier,
        kind=kind,
        domain_id=domain,
        current_version_id=f"{identifier}_v1",
    )
    session.add(asset)
    session.add(
        AssetVersion(
            id=asset.current_version_id,
            asset_id=asset.id,
            version_no=1,
            observed_run_id="run",
            last_seen_run_id="run",
            catalog_name=asset.catalog_name,
            db_name=asset.db_name,
            name=asset.name,
            kind=kind,
            content_hash="a" * 64,
        )
    )
    add_columns(session, asset, range(columns))
    return asset


def add_columns(session, asset, numbers, *, comment="采集字段说明"):
    for n in numbers:
        name = f"field_{n:03}"
        column_id = f"{asset.id}_{name}"
        session.add_all(
            [
                AssetColumn(id=column_id, asset_id=asset.id, name=name),
                ColumnVersion(
                    asset_version_id=asset.current_version_id,
                    column_id=column_id,
                    ordinal=n + 1,
                    name=name,
                    data_type="text",
                    comment=comment,
                ),
                ColumnFingerprint(asset_id=asset.id, column_name=name, fingerprint="f" * 64, status="ok"),
            ]
        )


def test_assets_filter_before_paging_and_use_stable_tiebreaker(catalog):
    session = catalog.session
    for identifier, name, domain in (
        ("hidden_first", "a_match", "hidden"),
        ("first", "b_match", "visible"),
        ("hidden_middle", "c_match", "hidden"),
        ("tie_b", "d_match", "visible"),
        ("tie_a", "d_match", "visible"),
        ("last", "e_match", "visible"),
        ("literal", "x_100%", "visible"),
    ):
        add_asset(session, identifier, name=name, domain=domain)
    session.commit()
    pages = [
        api.list_assets(q="MATCH", limit=1, offset=n, user=catalog.users["reader"], session=session) for n in range(5)
    ]
    assert [[a["id"] for a in page] for page in pages] == [["first"], ["tie_a"], ["tie_b"], ["last"], []]
    assert assets(catalog, domain_id="hidden") == []
    assert [a["id"] for a in assets(catalog, q="100%")] == ["literal"]
    assert [a["id"] for a in assets(catalog, q="x_100%")] == ["literal"]
    assert assets(catalog, datasource_id="missing") == []
    session.get(Asset, "first").status = "deleted"
    session.commit()
    assert assets(catalog, q="b_match") == []
    assert [a["id"] for a in assets(catalog, q="b_match", include_deleted=True)] == ["first"]


@pytest.mark.parametrize("policy", ["allow", "deny"])
def test_assets_keep_unassigned_policy_and_role_rules(catalog, monkeypatch, policy):
    monkeypatch.setattr(access, "get_settings", lambda: replace(get_settings(), unassigned_asset_policy=policy))
    session = catalog.session
    add_asset(session, "unassigned", domain=None)
    session.add(UserRole(user_id="outsider", role="admin"))
    session.commit()
    kwargs = {"q": "unassigned", "limit": 1, "offset": 0, "session": session}
    assert api.list_assets(user=catalog.users["maintainer"], **kwargs)[0]["id"] == "unassigned"
    assert bool(api.list_assets(user=catalog.users["reviewer"], **kwargs)) is (policy == "allow")
    assert api.list_assets(user=catalog.users["outsider"], **kwargs)[0]["id"] == "unassigned"
    assert (
        len(api.list_assets(user=catalog.users["outsider"], domain_id="hidden", limit=100, offset=0, session=session))
        == 1
    )


@pytest.mark.parametrize("limit", [None, 1])
def test_asset_page_is_one_query_when_catalog_grows(catalog, limit):
    session = catalog.session
    totals = []
    for count in (2, 50):
        for n in range(0 if count == 2 else 2, count):
            add_asset(session, f"paged_{n}")
        session.commit()
        session.expunge_all()
        user = session.get(User, "reader")
        with queries(session) as sql:
            result = api.list_assets(q="paged_", limit=limit, offset=0, user=user, session=session)
        assert len(result) == (1 if limit else count)
        assert "domain_member" in sql[0][0]
        if limit is not None:
            assert "LIMIT" in sql[0][0]
        totals.append(len(sql))
    assert totals == [1, 1]


@pytest.mark.parametrize("limit", [None, 1])
def test_domains_filter_permission_and_batch_page_members(catalog, limit):
    session = catalog.session
    counts = []
    for count in (2, 50):
        for n in range(0 if count == 2 else 2, count):
            domain = Domain(id=f"extra_{n:03}", code=f"extra_{n:03}", name="匹配业务域", owner_id="maintainer")
            session.add_all([domain, DomainMember(domain_id=domain.id, user_id="reader", role="viewer")])
        session.commit()
        with queries(session) as sql:
            result = api.list_domains(q="匹配", limit=limit, offset=1, user=catalog.users["reader"], session=session)
        assert len(result) == (1 if limit else count - 1) and result[0]["id"] == "extra_001"
        assert result[0]["members"][0]["user_id"] == "reader"
        counts.append(len(sql))
    assert counts == [2, 2]
    assert (
        api.list_domains(domain_id="hidden", limit=100, offset=0, user=catalog.users["reader"], session=session) == []
    )
    assert [d["id"] for d in api.list_domains(limit=500, offset=0, user=catalog.users["reader"], session=session)][
        -1
    ] == "visible"


def test_detail_queries_do_not_grow_with_columns_or_unrelated_catalog(catalog):
    session = catalog.session
    root = add_asset(session, "detail_root", columns=2)
    counts = []
    for count in (2, 50):
        if count == 50:
            add_columns(session, root, range(2, 50), comment=None)
            for n in range(50):
                add_asset(session, f"unrelated_{n}", columns=2)
        session.add(
            Confirmation(
                asset_id=root.id,
                column_name=f"field_{count - 1:03}",
                fingerprint="f" * 64,
                meaning="已确认",
                confirmed_by="maintainer",
            )
        )
        session.commit()
        session.expunge_all()
        user = session.get(User, "reader")
        with queries(session) as sql:
            detail = api.asset_detail("detail_root", user=user, session=session)
        assert len(detail["columns"]) == count
        assert detail["columns"][-1]["confirmations"][0]["valid"]
        fingerprints = [(query, params) for query, params in sql if "FROM column_fingerprint" in query]
        assert len(fingerprints) == 1
        assert fingerprints[0][1] == ("detail_root",)
        assert "column_fingerprint.asset_id IN" in fingerprints[0][0]
        assert sum("FROM confirmation" in query for query, _ in sql) == 1
        counts.append(len(sql))
        root = session.get(Asset, "detail_root")
    assert counts[0] == counts[1]


@pytest.mark.parametrize("count", [2, 50])
def test_detail_batches_inherited_docs_and_keeps_hidden_sources_private(catalog, count):
    session = catalog.session
    source = add_asset(session, "doc_source", columns=count)
    hidden = add_asset(session, "doc_hidden", domain="hidden", columns=1)
    view = add_asset(session, "doc_view", columns=count, kind="view")
    for column in session.scalars(
        select(ColumnVersion).where(ColumnVersion.asset_version_id == view.current_version_id)
    ):
        column.comment = None
        session.add(
            LineageEdge(
                target_asset_id=view.id,
                target_asset_version_id=view.current_version_id,
                target_column=column.name,
                source_asset_id=hidden.id if column.name == "field_000" else source.id,
                source_ref="catalog.schema_a.source",
                source_column=column.name,
                origin="parsed",
                relation="value_direct",
            )
        )
    session.commit()
    with queries(session) as sql:
        detail = api.asset_detail(view.id, user=catalog.users["reader"], session=session)
    docs = [c["doc"] for c in detail["columns"]]
    assert docs[0]["status"] == "missing" and docs[0]["source"] is None
    assert all(d["status"] == "inherited" and d["comment"] == "采集字段说明" for d in docs[1:])
    assert sum("FROM column_version" in query for query, _ in sql) == 2
    assert sum("FROM lineage_edge" in query for query, _ in sql) == 1
    assert sum("FROM column_fingerprint" in query for query, _ in sql) == 1


def test_detail_keeps_linked_grains_fingerprints_and_batches_reverse_links(catalog):
    session = catalog.session
    target = add_asset(session, "linked_target", columns=1)
    external = add_asset(session, "linked_external", columns=1, kind="external_table")
    session.scalar(
        select(ColumnVersion).where(ColumnVersion.asset_version_id == external.current_version_id)
    ).comment = None
    session.add(
        Grain(
            asset_id=target.id,
            asset_version_id=target.current_version_id,
            columns=["field_000"],
            origin="declared",
            status="verified",
            column_fingerprints={"field_000": "f" * 64},
        )
    )
    for n in range(50):
        source = external if n == 0 else add_asset(session, f"reverse_{n}", kind="external_table")
        session.add(
            IdentityLink(source_asset_id=source.id, target_asset_id=target.id, status="confirmed", method="test")
        )
    session.commit()
    detail = api.asset_detail(external.id, user=catalog.users["reader"], session=session)
    assert detail["columns"][0]["doc"]["status"] == "linked"
    assert detail["grains"][0]["origin"] == "linked" and detail["grains"][0]["status"] == "verified"
    with queries(session) as sql:
        detail = api.asset_detail(target.id, user=catalog.users["reader"], session=session)
    assert len(detail["linked_from"]) == 50
    assert sum("FROM asset \n" in query for query, _ in sql) <= 2


def test_detail_hides_identity_links_to_invisible_assets(catalog):
    session = catalog.session
    session.add_all(
        [
            IdentityLink(source_asset_id="asset", target_asset_id="hidden_asset", status="confirmed", method="test"),
            IdentityLink(source_asset_id="hidden_asset", target_asset_id="asset", status="confirmed", method="test"),
        ]
    )
    session.commit()
    detail = api.asset_detail("asset", user=catalog.users["reader"], session=session)
    assert detail["identity_link"] is None and detail["linked_from"] == []


def test_detail_includes_grain_parse_dependencies_without_column_edges(catalog):
    session = catalog.session
    source = add_asset(session, "grain_source", columns=1)
    view = add_asset(session, "grain_view", columns=1, kind="view")
    definition = Definition(
        id="grain_view_definition",
        asset_version_id=view.current_version_id,
        dialect="postgres",
        raw_sql="SELECT field_000 FROM catalog.schema_a.grain_source",
        canonical_hash="a" * 64,
    )
    session.add_all(
        [
            definition,
            ParseResult(
                definition_id=definition.id,
                parser_version=PARSER_VERSION,
                status="ok",
                details={
                    "passthrough": {"base": "catalog.schema_a.grain_source", "mapping": {"field_000": "field_000"}}
                },
            ),
            Grain(
                asset_id=source.id,
                asset_version_id=source.current_version_id,
                columns=["field_000"],
                origin="declared",
                status="verified",
                column_fingerprints={"field_000": "f" * 64},
            ),
        ]
    )
    session.commit()
    with queries(session) as sql:
        detail = api.asset_detail(view.id, user=catalog.users["reader"], session=session)
    assert detail["grains"][0]["origin"] == "passthrough" and detail["grains"][0]["status"] == "verified"
    fingerprint_queries = [(query, parameters) for query, parameters in sql if "FROM column_fingerprint" in query]
    assert len(fingerprint_queries) == 1
    assert set(fingerprint_queries[0][1]) == {"grain_view", "grain_source"}


@pytest.mark.parametrize("path", ["/api/assets", "/api/domains"])
@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"offset": -1}])
def test_catalog_rejects_invalid_page_parameters(path, params):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.current_user] = lambda: object()
    app.dependency_overrides[api.get_session] = lambda: object()
    with TestClient(app) as client:
        assert client.get(path, params=params).status_code == 422


def test_catalog_openapi_exposes_bounded_list_parameters():
    app = FastAPI()
    app.include_router(api.router)
    spec = app.openapi()
    for path in ("/api/assets", "/api/domains"):
        parameters = {p["name"]: p["schema"] for p in spec["paths"][path]["get"]["parameters"]}
        assert {"q", "domain_id", "limit", "offset"} <= parameters.keys()
        assert "default" not in parameters["limit"]
        limit = next(s for s in parameters["limit"]["anyOf"] if s["type"] == "integer")
        assert limit["minimum"] == 1 and limit["maximum"] == 500
        assert {"type": "null"} in parameters["limit"]["anyOf"]
        assert parameters["offset"]["default"] == 0 and parameters["offset"]["minimum"] == 0
        assert (
            spec["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["type"] == "array"
        )
