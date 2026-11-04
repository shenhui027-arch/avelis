"""目录列表的关键词与分页在真实 PostgreSQL 上执行（别名按 JSONB 数组元素搜索）。"""

import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from avelis.api import catalog, governance
from avelis.config import get_settings
from avelis.db import session_scope
from avelis.governance import access
from avelis.models import Asset, Datasource, DomainMember, JoinPath, Metric, User

pytestmark = pytest.mark.db


def test_metric_list_searches_jsonb_aliases_literally_and_pages(factory):
    admin = factory.user(roles=("admin",))
    codes = [factory.metric(admin) for _ in range(3)]
    with session_scope() as session:
        rows = {m.code: m for m in session.scalars(select(Metric).where(Metric.code.in_(codes)))}
        rows[codes[0]].aliases = ["毛利率", "100%_达成"]
        rows[codes[1]].aliases = ['含"引号"\\与反斜杠']
        rows[codes[2]].aliases = []
    with session_scope() as session:
        user = session.get(User, admin)

        def found(q, **kw):
            out = governance.list_metrics(
                q=q, domain_id=None, limit=kw.get("limit"), offset=kw.get("offset", 0), user=user, session=session
            )
            return [m["code"] for m in out if m["code"] in codes]

        assert found("毛利") == [codes[0]]
        assert found("100%_") == [codes[0]]
        assert found("%") == [codes[0]]  # % 与 _ 按字面匹配，不是通配符
        assert found('"引号"\\') == [codes[1]]
        assert found("不存在的别名") == []
        everything = found(None)
        assert sorted(everything) == sorted(codes)
        first = governance.list_metrics(q=None, domain_id=None, limit=1, offset=0, user=user, session=session)
        assert len(first) == 1


@pytest.mark.parametrize("limit", [None, 1])
def test_asset_domain_and_join_lists_run_on_postgresql(factory, limit):
    from avelis.api import catalog

    admin = factory.user(roles=("admin",))
    factory.metric(admin)
    with session_scope() as session:
        user = session.get(User, admin)
        common = {"limit": limit, "offset": 0, "user": user, "session": session}
        domains = catalog.list_domains(q="d", domain_id=None, **common)
        assets = catalog.list_assets(datasource_id=None, domain_id=None, q="x", include_deleted=False, **common)
        joins = governance.list_join_paths(q="x", domain_id=None, **common)
        assert domains and all("d" in (d["code"] + d["name"] + (d["description"] or "")).lower() for d in domains)
        if limit is not None:
            assert len(domains) <= limit and len(assets) <= limit and len(joins) <= limit


@pytest.fixture
def catalog_world(factory):
    admin, reader, steward = factory.user(roles=("admin",)), factory.user(), factory.user(roles=("steward",))
    visible_code, hidden_code = factory.metric(admin, reader), factory.metric(admin)
    prefix = f"catalog_{uuid.uuid4().hex[:10]}"
    with session_scope() as session:
        domains = dict(
            session.execute(
                select(Metric.code, Metric.domain_id).where(Metric.code.in_((visible_code, hidden_code)))
            ).all()
        )
        visible, hidden = domains[visible_code], domains[hidden_code]
        session.add(DomainMember(user_id=steward, domain_id=visible, role="viewer"))
        query_ds = Datasource(name=f"{prefix}_query", kind="postgres", host="offline", port=1)
        metadata_ds = Datasource(name=f"{prefix}_metadata", kind="hive", host="offline", port=1)
        session.add_all((query_ds, metadata_ds))
        session.flush()
        assets = {}
        for name, domain, datasource, status in (
            ("visible", visible, query_ds.id, "active"),
            ("second", visible, query_ds.id, "active"),
            ("metadata", visible, metadata_ds.id, "active"),
            ("hidden", hidden, query_ds.id, "active"),
            ("unassigned", None, query_ds.id, "active"),
            ("deleted", visible, query_ds.id, "deleted"),
        ):
            asset = Asset(
                name=f"{prefix}_{name}",
                catalog_name="catalog",
                db_name="schema_a",
                kind="table",
                datasource_id=datasource,
                domain_id=domain,
                status=status,
            )
            session.add(asset)
            session.flush()
            assets[name] = asset.id
        for name, left, right in (
            ("visible", "visible", "second"),
            ("hidden_left", "hidden", "visible"),
            ("hidden_right", "visible", "hidden"),
            ("unassigned", "visible", "unassigned"),
            ("deleted", "visible", "deleted"),
        ):
            session.add(
                JoinPath(
                    code=f"{prefix}_{name}",
                    name=name,
                    left_asset_id=assets[left],
                    right_asset_id=assets[right],
                    left_columns=["value"],
                    right_columns=["value"],
                    cardinality="n1",
                    status="pending",
                    created_by=admin,
                )
            )
        return SimpleNamespace(
            admin=admin,
            reader=reader,
            steward=steward,
            assets=assets,
            datasource=query_ds.id,
            domain=visible,
            prefix=prefix,
        )


def test_asset_ids_and_queryable_intersect_permissions_in_postgresql(catalog_world, monkeypatch):
    world = catalog_world
    monkeypatch.setattr(access, "get_settings", lambda: replace(get_settings(), unassigned_asset_policy="deny"))
    with session_scope() as session:
        user = session.get(User, world.reader)
        common = {"limit": None, "offset": 0, "user": user, "session": session, "ids": list(world.assets.values())}

        def found(**kwargs):
            return {a["id"] for a in catalog.list_assets(**common, **kwargs)}

        assert found() == {world.assets[n] for n in ("visible", "second", "metadata")}
        assert found(queryable=True) == {world.assets[n] for n in ("visible", "second")}
        assert found(queryable=False) == {world.assets["metadata"]}
        assert found(queryable=False, datasource_id=world.datasource) == set()
        assert found(queryable=True, q="_visible", domain_id=world.domain) == {world.assets["visible"]}
        assert found(include_deleted=True) == {world.assets[n] for n in ("visible", "second", "metadata", "deleted")}
        missing = catalog.list_assets(ids=["missing"], limit=None, offset=0, user=user, session=session)
        assert missing == []


def test_asset_keywords_intersect_source_name_and_literal_table_name_in_postgresql(catalog_world):
    world = catalog_world
    with session_scope() as session:
        source_name = session.get(Datasource, world.datasource).name
        replica = Datasource(name=f"{world.prefix}_replica", kind="postgres", host="offline", port=1)
        session.add(replica)
        session.flush()
        hidden_domain = session.get(Asset, world.assets["hidden"]).domain_id
        identifiers = {}
        for label, source, domain, status, name in (
            ("target", world.datasource, world.domain, "active", "100%_orders"),
            ("replica", replica.id, world.domain, "active", "100%_orders"),
            ("hidden", world.datasource, hidden_domain, "active", "100%_orders"),
            ("deleted", world.datasource, world.domain, "deleted", "100%_orders"),
            ("decoy", world.datasource, world.domain, "active", "100XXorders"),
        ):
            asset = Asset(
                name=name,
                catalog_name="catalog",
                db_name="schema_a",
                kind="table",
                datasource_id=source,
                domain_id=domain,
                status=status,
            )
            session.add(asset)
            session.flush()
            identifiers[label] = asset.id
        user = session.get(User, world.reader)

        def found(q, **kwargs):
            return [
                a["id"]
                for a in catalog.list_assets(
                    q=q, ids=list(identifiers.values()), limit=None, offset=0, user=user, session=session, **kwargs
                )
            ]

        assert found("100%_orders") == sorted(identifiers[label] for label in ("target", "replica"))
        for word in ("100%_orders", "100%", "_orders"):
            assert found(f"\t{source_name.upper()}  {word} \n") == [identifiers["target"]]
        assert found(f"{source_name} 100%_orders TABLE") == [identifiers["target"]]
        assert found(f"{source_name} 100%_orders tab") == []
        assert set(found(f"{source_name} 100%_orders", include_deleted=True)) == {
            identifiers["target"],
            identifiers["deleted"],
        }
        assert found(f"{source_name} 100%_orders", domain_id=hidden_domain, include_deleted=True) == []


@pytest.mark.parametrize("policy", ["allow", "deny"])
def test_catalog_counts_match_visible_lists_with_one_query_in_postgresql(catalog_world, monkeypatch, policy):
    world = catalog_world
    monkeypatch.setattr(access, "get_settings", lambda: replace(get_settings(), unassigned_asset_policy=policy))
    with session_scope() as session:
        for identifier in (world.reader, world.steward, world.admin):
            user = session.get(User, identifier)
            common = {"limit": None, "offset": 0, "user": user, "session": session}
            expected = {
                "metrics": len(governance.list_metrics(**common)),
                "assets": len(catalog.list_assets(**common)),
                "domains": len(catalog.list_domains(**common)),
                "join_paths": len(governance.list_join_paths(**common)),
            }
            statements = []

            def record(conn, cursor, statement, parameters, context, executemany):
                statements.append(statement)

            engine = session.get_bind()
            event.listen(engine, "before_cursor_execute", record)
            try:
                assert catalog.catalog_counts(user=user, session=session) == expected
            finally:
                event.remove(engine, "before_cursor_execute", record)
            assert len(statements) == 1 and statements[0].lstrip().upper().startswith("SELECT")
            if identifier == world.reader:
                assert expected["metrics"] == 1 and expected["domains"] == 1
                assert expected["assets"] >= 3 and expected["join_paths"] >= 2
                if policy == "deny":
                    assert expected["assets"] == 3 and expected["join_paths"] == 2


def test_catalog_count_query_count_does_not_grow_in_postgresql(catalog_world, monkeypatch):
    world = catalog_world
    monkeypatch.setattr(access, "get_settings", lambda: replace(get_settings(), unassigned_asset_policy="deny"))
    with session_scope() as session:
        user = session.get(User, world.reader)
        counts = []
        for size in (2, 50):
            for number in range(0 if size == 2 else 2, size):
                session.add(
                    Asset(
                        name=f"{world.prefix}_extra_{number}",
                        catalog_name="catalog",
                        db_name="schema_a",
                        kind="table",
                        datasource_id=world.datasource,
                        domain_id=world.domain,
                    )
                )
            session.flush()
            statements = []

            def record(conn, cursor, statement, parameters, context, executemany):
                statements.append(statement)

            engine = session.get_bind()
            event.listen(engine, "before_cursor_execute", record)
            try:
                result = catalog.catalog_counts(user=user, session=session)
            finally:
                event.remove(engine, "before_cursor_execute", record)
            assert result["assets"] == size + 3
            counts.append(len(statements))
        assert counts == [1, 1]
