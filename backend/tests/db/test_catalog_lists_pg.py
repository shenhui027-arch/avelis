"""目录列表的关键词与分页在真实 PostgreSQL 上执行（别名按 JSONB 数组元素搜索）。"""

import pytest
from sqlalchemy import select

from avelis.api import governance
from avelis.db import session_scope
from avelis.models import Metric, User

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
        assert domains and all(
            d["code"].startswith("d") or "d" in (d["name"] + (d["description"] or "")) for d in domains
        )
        if limit is not None:
            assert len(domains) <= limit and len(assets) <= limit and len(joins) <= limit
