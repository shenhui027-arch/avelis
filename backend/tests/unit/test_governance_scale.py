"""治理列表在 SQL 中筛选、分页，批量读取信息并保留完整列表兼容性。"""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.pool import StaticPool

from avelis.api import catalog as catalog_api
from avelis.api import governance as api
from avelis.config import get_settings
from avelis.governance import access
from avelis.models import Domain, DomainMember, JoinPath, JoinPathApproval, Metric, MetricVersion, User, UserRole
from tests.unit import test_ossie_import as ossie_tests
from tests.unit.test_catalog_scale import add_asset, queries
from tests.unit.test_ossie_import import catalog  # noqa: F401


def add_metric(session, identifier, *, code=None, name=None, domain="visible", owner="maintainer", versions=1):
    metric = Metric(
        id=identifier,
        code=code or identifier,
        name=name or identifier,
        aliases=[f"alias_{identifier}"],
        ai_context={"instructions": f"context_{identifier}"},
        domain_id=domain,
        owner_id=owner,
        created_by="maintainer",
    )
    session.add(metric)
    for number in range(1, versions + 1):
        session.add(
            MetricVersion(
                id=f"{identifier}_v{number}",
                metric_id=identifier,
                version_no=number,
                status="draft" if number == versions else "published",
                status_reason=f"reason_{number}",
                description=f"description_{number}",
                base_asset_id="asset",
                expression="SUM(b.value_a)",
                time_column="b.observed_on",
                timezone="UTC",
                unit=f"unit_{number}",
                created_by="maintainer",
                published_at=datetime(2026, 1, number, tzinfo=UTC) if number < versions else None,
            )
        )
    return metric


def add_path(session, identifier, *, code=None, name=None, left="asset", right="asset_b", creator="maintainer"):
    path = JoinPath(
        id=identifier,
        code=code or identifier,
        name=name or identifier,
        left_asset_id=left,
        left_columns=["value_a"],
        right_asset_id=right,
        right_columns=["value_a"],
        cardinality="n1",
        cardinality_basis={"evidence": identifier},
        status="pending",
        source={"method": "test", "identifier": identifier},
        created_by=creator,
    )
    session.add(path)
    session.add(
        JoinPathApproval(
            join_path_id=identifier,
            domain_id="visible",
            user_id=creator,
            decision="approved",
            note=identifier,
        )
    )
    return path


def test_metrics_filter_before_paging_keep_drafts_and_all_version_summaries(catalog):
    session = catalog.session
    for identifier, code, name, domain, count in (
        ("hidden_first", "a_match", "hidden", "hidden", 1),
        ("first", "b_match", "first", "visible", 3),
        ("hidden_middle", "c_match", "hidden", "hidden", 1),
        ("draft", "d_match", "draft", "visible", 1),
        ("empty", "e_match", "without versions", "visible", 0),
        ("by_name", "f_other", "NAME_MATCH", "visible", 2),
        ("literal", "g_100%", "literal_100%", "visible", 1),
        ("wildcard_decoy", "h_1000", "literalX1000", "visible", 1),
    ):
        add_metric(session, identifier, code=code, name=name, domain=domain, versions=count)
    session.commit()
    user = catalog.users["reader"]
    pages = [api.list_metrics(q="MATCH", limit=1, offset=n, user=user, session=session) for n in range(5)]
    assert [[item["id"] for item in page] for page in pages] == [["first"], ["draft"], ["empty"], ["by_name"], []]
    result = api.list_metrics(q="MATCH", limit=None, offset=0, user=user, session=session)
    assert [item["id"] for item in result] == ["first", "draft", "empty", "by_name"]
    assert [v["version_no"] for v in result[0]["versions"]] == [3, 2, 1]
    assert result[0]["versions"][0]["status"] == "draft"
    assert result[2]["versions"] == []
    for item in result:
        assert item == api.metric_view(session, session.get(Metric, item["id"]))
    assert api.list_metrics(domain_id="hidden", limit=None, offset=0, user=user, session=session) == []
    for keyword in ("100%", "literal_100%"):
        assert [
            item["id"] for item in api.list_metrics(q=keyword, limit=None, offset=0, user=user, session=session)
        ] == ["literal"]


def test_join_paths_filter_both_sides_before_paging_and_domain_matches_either_side(catalog):
    session = catalog.session
    add_asset(session, "other_asset", domain="other")
    session.add(DomainMember(domain_id="other", user_id="reader", role="viewer"))
    for identifier, code, name, left, right in (
        ("hidden_left", "a_match", "hidden left", "hidden_asset", "asset"),
        ("first", "b_match", "first", "asset", "asset_b"),
        ("hidden_right", "c_match", "hidden right", "asset", "hidden_asset"),
        ("cross", "d_match", "cross", "asset", "other_asset"),
        ("reverse", "e_match", "reverse", "other_asset", "asset_b"),
        ("by_name", "f_other", "NAME_MATCH", "asset", "asset_b"),
        ("literal", "g_100%", "literal_100%", "asset", "asset_b"),
        ("wildcard_decoy", "h_1000", "literalX1000", "asset", "asset_b"),
    ):
        add_path(session, identifier, code=code, name=name, left=left, right=right)
    session.add(JoinPathApproval(join_path_id="cross", domain_id="other", user_id="outsider", decision="rejected"))
    session.commit()
    user = catalog.users["reader"]
    pages = [api.list_join_paths(q="MATCH", limit=1, offset=n, user=user, session=session) for n in range(5)]
    assert [[item["id"] for item in page] for page in pages] == [["first"], ["cross"], ["reverse"], ["by_name"], []]
    result = api.list_join_paths(q="MATCH", limit=None, offset=0, user=user, session=session)
    for item in result:
        expected = api.join_path_view(session, session.get(JoinPath, item["id"]))
        assert item | {"approvals": sorted(item["approvals"], key=lambda a: a["domain_id"])} == expected | {
            "approvals": sorted(expected["approvals"], key=lambda a: a["domain_id"])
        }
    assert result[1]["required_domains"] == ["visible", "other"]
    assert result[2]["required_domains"] == ["other", "visible"]
    assert [
        item["id"] for item in api.list_join_paths(domain_id="other", limit=None, offset=0, user=user, session=session)
    ] == ["cross", "reverse"]
    assert api.list_join_paths(domain_id="hidden", limit=1, offset=0, user=user, session=session) == []
    for keyword in ("100%", "literal_100%"):
        assert [
            item["id"] for item in api.list_join_paths(q=keyword, limit=None, offset=0, user=user, session=session)
        ] == ["literal"]


@pytest.mark.parametrize("keyword", ["销售", '中文"引号\\', "100%_", "sAlEs"])
def test_metric_alias_search_is_literal_case_insensitive_and_paged_after_visibility(catalog, keyword):
    session = catalog.session
    for identifier, domain in (("a_hidden", "hidden"), ("b_visible", "visible"), ("c_visible", "visible")):
        metric = add_metric(session, identifier, domain=domain)
        metric.aliases = [f"prefix {keyword.upper()} suffix"]
    decoy = add_metric(session, "d_decoy")
    decoy.aliases = ["100XX", "left", "right"]
    session.commit()
    user = catalog.users["reader"]
    with queries(session) as sql:
        result = api.list_metrics(q=keyword, domain_id="visible", limit=1, offset=1, user=user, session=session)
    assert [item["id"] for item in result] == ["c_visible"]
    assert len(sql) == 2 and "json_each" in sql[0][0]
    assert api.list_metrics(q='", "', limit=None, offset=0, user=user, session=session) == []


def test_metric_alias_postgresql_search_uses_decoded_elements_and_bound_literal():
    dialect = postgresql.dialect()
    session = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=dialect))
    keyword = '中"文\\100%_'
    query = select(Metric.id).where(api._metric_list_keyword_clause(session, keyword))
    compiled = query.compile(dialect=dialect)
    assert "jsonb_array_elements_text(metric.aliases)" in str(compiled)
    assert "(value)" in str(compiled)
    assert keyword not in str(compiled)
    assert set(compiled.params.values()) == {keyword.lower().replace("%", "/%").replace("_", "/_")}


@pytest.mark.parametrize("policy", ["allow", "deny"])
def test_governance_lists_preserve_domain_roles_and_unassigned_asset_policy(catalog, monkeypatch, policy):
    monkeypatch.setattr(access, "get_settings", lambda: replace(get_settings(), unassigned_asset_policy=policy))
    session = catalog.session
    add_asset(session, "unassigned", domain=None)
    add_metric(session, "hidden_metric", domain="hidden", owner="reader")
    add_path(session, "hidden_path", left="asset", right="hidden_asset")
    add_path(session, "unassigned_left", left="unassigned", right="asset")
    add_path(session, "unassigned_right", left="asset", right="unassigned")
    session.add(UserRole(user_id="outsider", role="admin"))
    session.commit()
    kwargs = {"limit": None, "offset": 0, "session": session}
    assert api.list_metrics(user=catalog.users["reader"], **kwargs) == []
    assert api.list_metrics(user=catalog.users["outsider"], **kwargs)[0]["id"] == "hidden_metric"
    for role in ("reader", "reviewer"):
        paths = api.list_join_paths(user=catalog.users[role], **kwargs)
        expected = {"unassigned_left", "unassigned_right"} if policy == "allow" or role == "reader" else set()
        assert {item["id"] for item in paths} == expected
        for item in paths:
            assert item == api.join_path_view(session, session.get(JoinPath, item["id"]))
    assert {item["id"] for item in api.list_join_paths(user=catalog.users["outsider"], **kwargs)} == {
        "hidden_path",
        "unassigned_left",
        "unassigned_right",
    }


@pytest.mark.parametrize("limit", [None, 1])
@pytest.mark.parametrize("kind", ["metrics", "join_paths"])
def test_governance_list_query_count_is_constant_with_distinct_related_rows(catalog, limit, kind):
    session = catalog.session
    counts = []
    for count in (2, 50):
        for number in range(0 if count == 2 else 2, count):
            identifier = f"scale_{number:03}"
            owner = f"owner_{number:03}"
            session.add(User(id=owner, username=owner, display_name=f"Owner {number}", password_hash="unused"))
            if kind == "metrics":
                add_metric(session, identifier, owner=owner, versions=3)
            else:
                left = add_asset(session, f"{identifier}_left")
                right = add_asset(session, f"{identifier}_right")
                add_path(session, identifier, left=left.id, right=right.id, creator=owner)
        session.commit()
        session.expunge_all()
        user = session.get(User, "reader")
        with queries(session) as sql:
            result = getattr(api, f"list_{kind}")(
                q="SCALE_", domain_id="visible", limit=limit, offset=0, user=user, session=session
            )
        assert len(result) == (count if limit is None else 1)
        assert "domain_member" in sql[0][0] and "lower(" in sql[0][0]
        assert "domain_id =" in sql[0][0]
        # SQLite 为单独的 OFFSET 自动生成 LIMIT -1；这仍然是不限制行数。
        assert "LIMIT" in sql[0][0] and sql[0][1][-2:] == ((-1 if limit is None else limit), 0)
        counts.append(len(sql))
        if kind == "metrics":
            assert all(len(item["versions"]) == 3 for item in result)
            assert "publish_check" not in sql[1][0] and "expression" not in sql[1][0]
            assert result[-1]["owner"]["display_name"] == f"Owner {len(result) - 1}"
        else:
            assert all(len(item["approvals"]) == 1 for item in result)
            assert result[-1]["created_by"] == f"Owner {len(result) - 1}"
    assert counts == [2, 2]


@pytest.fixture
def http_catalog(monkeypatch):
    # 同步 FastAPI 路由在线程池运行，显式共用允许跨线程访问的内存数据库。
    def make_engine(*args, **kwargs):
        return create_engine(*args, **kwargs, poolclass=StaticPool, connect_args={"check_same_thread": False})

    monkeypatch.setattr(ossie_tests, "create_engine", make_engine)
    yield from ossie_tests.catalog.__wrapped__(monkeypatch)


@pytest.mark.parametrize("path", ["assets", "domains", "metrics", "join-paths"])
def test_list_http_default_returns_more_than_100_and_offset_still_applies(http_catalog, path):
    session = http_catalog.session
    expected = []
    for number in range(105):
        identifier = f"http_{number:03}"
        expected.append(identifier)
        if path == "assets":
            add_asset(session, identifier)
        elif path == "domains":
            session.add(Domain(id=identifier, code=identifier, name=identifier, owner_id="maintainer"))
            session.add(DomainMember(domain_id=identifier, user_id="reader", role="viewer"))
        elif path == "metrics":
            add_metric(session, identifier, versions=0)
        else:
            add_path(session, identifier)
    session.commit()
    app = FastAPI()
    app.include_router(catalog_api.router)
    app.include_router(api.router)
    app.dependency_overrides[api.current_user] = lambda: http_catalog.users["reader"]
    app.dependency_overrides[api.get_session] = lambda: session
    with TestClient(app) as client:
        for params, ids in (
            ({"q": "http_"}, expected),
            ({"q": "http_", "offset": 100}, expected[100:]),
            ({"q": "http_", "limit": 2, "offset": 100}, expected[100:102]),
            ({"q": "http_", "limit": 500}, expected),
        ):
            response = client.get(f"/api/{path}", params=params)
            assert response.status_code == 200, response.text
            assert [item["id"] for item in response.json()] == ids


@pytest.mark.parametrize("path", ["/api/metrics", "/api/join-paths"])
@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"offset": -1}])
def test_governance_rejects_invalid_page_parameters(path, params):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.current_user] = lambda: object()
    app.dependency_overrides[api.get_session] = lambda: object()
    with TestClient(app) as client:
        assert client.get(path, params=params).status_code == 422


def test_governance_openapi_has_optional_bounded_limit_and_preserves_array_response():
    app = FastAPI()
    app.include_router(api.router)
    spec = app.openapi()
    for path in ("/api/metrics", "/api/join-paths"):
        parameters = {p["name"]: p for p in spec["paths"][path]["get"]["parameters"]}
        assert {"q", "domain_id", "limit", "offset"} <= parameters.keys()
        assert parameters["limit"]["required"] is False
        limit_schema = parameters["limit"]["schema"]
        assert "default" not in limit_schema or limit_schema["default"] is None
        assert {branch["type"] for branch in limit_schema["anyOf"]} == {"integer", "null"}
        integer = next(branch for branch in limit_schema["anyOf"] if branch["type"] == "integer")
        assert integer["minimum"] == 1 and integer["maximum"] == 500
        assert parameters["offset"]["schema"]["default"] == 0
        assert parameters["offset"]["schema"]["minimum"] == 0
        assert (
            spec["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["type"] == "array"
        )
