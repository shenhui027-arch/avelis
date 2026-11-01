"""探索查询的方言路由（ADR 0009 第 6 点）：按标准 SQL 里的资产确定数据源，模型不填数据源。在真实元数据库上验证。"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.db


@pytest.fixture
def world(factory):
    """两个可探索的数据源（StarRocks、PostgreSQL）、一个 MySQL 数据源、一个关闭探索的 PostgreSQL 数据源；
    资产都归一个业务域，测试用户是它的成员；另一个域的资产对他不可见。"""
    from avelis.db import session_scope
    from avelis.models import Asset, Datasource, Domain, DomainMember, User

    tag = uuid.uuid4().hex[:6]
    owner, member = factory.user(("admin",)), factory.user()
    with session_scope() as s:
        mine = Domain(code=f"r{tag}", name="路由域", owner_id=owner)
        other = Domain(code=f"o{tag}", name="他人域", owner_id=owner)
        s.add_all([mine, other])
        s.flush()
        s.add(DomainMember(domain_id=mine.id, user_id=member, role="viewer"))
        sources = {
            "sr": Datasource(name=f"数仓{tag}", kind="starrocks", host="h", port=1, options={}),
            "pg": Datasource(name=f"门店{tag}", kind="postgres", host="h", port=1, options={"database": "storedb"}),
            "my": Datasource(name=f"维表{tag}", kind="mysql", host="h", port=1, options={}),
            "off": Datasource(
                name=f"关闭{tag}",
                kind="postgres",
                host="h",
                port=1,
                options={"database": "offdb"},
                allow_exploration=False,
            ),
        }
        s.add_all(sources.values())
        s.flush()

        def asset(src, catalog, db, name, domain=mine):
            s.add(
                Asset(
                    datasource_id=sources[src].id,
                    kind="table",
                    catalog_name=catalog,
                    db_name=f"{db}{tag}",
                    name=name,
                    domain_id=domain.id,
                )
            )

        asset("sr", "default_catalog", "trade", "orders")
        asset("sr", "ext", "dim", "product")  # StarRocks 外部 catalog 里的表
        asset("my", "def", "dim", "product")  # 同名表在 MySQL 的默认 catalog
        asset("pg", "storedb", "store", "sales")
        asset("pg", "storedb", "trade", "orders")  # 与 StarRocks 同名的 库.表
        asset("sr", "default_catalog", "secret", "salary", domain=other)
        asset("off", "offdb", "closed", "t")
        ids = {k: v.id for k, v in sources.items()}
    with session_scope() as s:
        user = s.get(User, member)
        s.expunge(user)
    return tag, ids, user


def route(user, sql):
    from avelis.analysis import exploration
    from avelis.db import session_scope

    with session_scope() as s:
        return exploration.route(s, s.merge(user), sql).datasource.id


def rejected(user, sql):
    from avelis.analysis.explore import ExploreRejected

    with pytest.raises(ExploreRejected) as e:
        route(user, sql)
    assert e.value.view()["sent_to_database"] is False
    return e.value


def test_two_part_names_mean_the_default_catalog_of_each_source(world):
    tag, ids, user = world
    assert route(user, f"SELECT COUNT(*) AS n FROM store{tag}.sales") == ids["pg"]
    assert route(user, f'SELECT COUNT(*) AS n FROM "storedb"."store{tag}"."sales"') == ids["pg"]  # 三段名精确匹配
    assert route(user, f"SELECT COUNT(*) AS n FROM default_catalog.trade{tag}.orders") == ids["sr"]
    # 两段名 dim.product：StarRocks 的那张在外部 catalog（不是默认 catalog），只剩 MySQL 的那张，而 MySQL 没有探索档案
    assert rejected(user, f"SELECT COUNT(*) AS n FROM dim{tag}.product").code == "engine_unsupported"


def test_same_name_in_two_sources_requires_a_full_name(world):
    tag, ids, user = world
    error = rejected(user, f"SELECT COUNT(*) AS n FROM trade{tag}.orders")
    assert error.code == "ambiguous_table" and len(error.hints) == 2
    assert any(f"default_catalog.trade{tag}.orders" in h for h in error.hints)


def test_one_query_one_source(world):
    tag, ids, user = world
    error = rejected(user, f"SELECT s.n FROM store{tag}.sales s JOIN default_catalog.trade{tag}.orders o ON s.n = o.n")
    assert error.code == "cross_datasource" and "分别查询" in error.message


def test_invisible_and_missing_tables_look_the_same(world):
    tag, ids, user = world
    hidden = rejected(user, f"SELECT COUNT(*) AS n FROM secret{tag}.salary")
    missing = rejected(user, f"SELECT COUNT(*) AS n FROM secret{tag}.nothing")
    assert hidden.code == missing.code == "asset_not_visible"
    assert hidden.message.replace("salary", "X") == missing.message.replace("nothing", "X")


def test_switched_off_source_and_unqualified_names(world):
    tag, ids, user = world
    assert rejected(user, f"SELECT COUNT(*) AS n FROM closed{tag}.t").code == "exploration_disabled"
    assert rejected(user, "SELECT COUNT(*) AS n FROM sales").code == "table_unqualified"
    assert rejected(user, "DELETE FROM x.y").code == "not_select"  # 解析阶段就拒绝
