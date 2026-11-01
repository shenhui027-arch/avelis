"""探索查询工具 query_sql / sample_values（ADR 0009 第 6 点）：经统一分发调用，连接器用替身记录每次调用。

每个拒绝用例都断言：探索 SQL 没有发到数据库（execute_explore 没被调用）、审计记为 rejected 且 sent_to_database 为 False。
需要真实引擎的执行与取消测试放在验收（第 5 步）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from avelis.analysis import exploration
from avelis.analysis.exploration import ExploreSource
from avelis.analysis.explore import PROFILES, ExploreRejected
from avelis.analysis.tools import Budget
from avelis.connectors.base import QueryError, QueryResult
from avelis.models import AuditEvent, Datasource
from tests.unit.test_analysis_explore import resolver as fake_resolver
from tests.unit.test_analysis_toolbox import make_box


class FakeConnector:
    def __init__(self, result=None, physical=None, error=None):
        self.calls, self.result, self.physical, self.error = [], result, physical or {}, error
        self.on_describe = None  # 物理核对时的副作用（例如消耗时间）

    def describe_objects(self, objects, *, timeout_seconds=None):
        self.calls.append(("describe", tuple(objects), timeout_seconds))
        if self.on_describe:
            self.on_describe()
        out = {}
        for db, name in objects:
            cols = {"facts": ["region", "amount", "qty", "event_date", "item_id"], "items": ["item_id", "category"]}[name]
            out[(db, name)] = {"physical_id": self.physical.get(name, {"facts": "101", "items": "102"}[name]),
                               "columns": [[c, "x"] for c in cols]}
        return out

    def execute_explore(self, sql, **kwargs):
        self.calls.append(("execute", sql, kwargs))
        if self.error:
            raise self.error
        return self.result or QueryResult(columns=["region", "total"], rows=[["r1", 10], ["r2", 20]], truncated=False)


def explore_box(monkeypatch, engine="postgres", connector=None, budget=None, recheck=None):
    box = make_box(budget=budget, monkeypatch=monkeypatch)
    monkeypatch.setattr(box, "explore_available", lambda: True)  # 有开启了探索、引擎有档案的数据源
    box.user.username = "tester"  # 审计记录执行人
    catalog = "appdb" if engine == "postgres" else "default_catalog"
    ds = SimpleNamespace(id="ds1", name="源甲", kind=engine, server_identity={"version": "x"})
    # 方言路由按 SQL 里的资产确定数据源（真实逻辑见 tests/db/test_explore_route.py）：这里固定路由到“源甲”
    monkeypatch.setattr(exploration, "route", lambda session, user, sql: ExploreSource(ds, PROFILES[engine], catalog))
    monkeypatch.setattr(exploration, "resolver", lambda session, user, source: fake_resolver(catalog, None))
    monkeypatch.setattr(exploration, "recheck_access", recheck or (lambda session, user, validated: None))
    monkeypatch.setattr(exploration, "execution_identity", lambda session, ds: "reader")
    connector = connector or FakeConnector()
    import avelis.engines
    monkeypatch.setattr(avelis.engines, "query_connector", lambda session, ds: connector)
    return box, connector


def audits(box):
    return [e.detail for e in box.session.added if isinstance(e, AuditEvent) and e.action == "analysis.explore"]


GOOD = "SELECT region, SUM(amount) AS total FROM d1.facts GROUP BY region"


@pytest.mark.parametrize("engine", ["starrocks", "postgres"])
def test_accepted_query_runs_regenerated_sql_and_marks_explore(monkeypatch, engine):
    box, conn = explore_box(monkeypatch, engine)
    out = json.loads(box.dispatch("query_sql", {"sql": GOOD, "purpose": "口径没有覆盖"}))
    ds = box.datasets()[out["dataset"]]
    executed = [c for c in conn.calls if c[0] == "execute"]
    assert len(executed) == 1 and executed[0][1] == out["sql"] and "LIMIT 1001" in out["sql"]  # 执行的是重新生成的 SQL
    assert ds.kind == "explore" and ds.meta["explore"] is True and ds.meta["sql"] == out["sql"]
    assert ds.meta["units"] == {"total": {"kind": "level"}} and ds.meta["column_evidence"]["region"]["label_ok"]
    assert ds.meta["tables"][0]["asset_version_id"] == "v1" and "探索查询（未经口径审核）" in out["note"]
    log = audits(box)[-1]
    assert log["outcome"] == "ok" and log["sent_to_database"] is True and log["sql"] == out["sql"]
    assert log["execution_identity"] == "reader" and log["digest"] and log["limits"]["max_rows"] == 1000
    assert box.budget.used_explore == 1


@pytest.mark.parametrize("engine", ["starrocks", "postgres"])
@pytest.mark.parametrize("sql, code", [
    ("DELETE FROM d1.facts", "not_select"),
    ("SELECT region FROM d1.facts; SELECT 1", "multiple_statements"),
    ("SELECT region FROM d1.secret", "asset_not_visible"),
    ("SELECT nope FROM d1.facts", "column_unresolved"),
    ("SELECT region FROM d1.facts, d1.items", "cartesian"),
    ("SELECT * FROM d1.facts", "star"),
    ("SELECT region FROM d1.facts OFFSET 3", "select_clause_not_allowed"),
])
def test_rejections_are_audited_and_never_sent(monkeypatch, engine, sql, code):
    box, conn = explore_box(monkeypatch, engine)
    out = json.loads(box.dispatch("query_sql", {"sql": sql, "purpose": "x"}))
    assert out["code"] == code and out["sent_to_database"] is False
    assert conn.calls == []  # 连执行前核对都没有发生
    log = audits(box)[-1]
    assert log["outcome"] == "rejected" and log["sent_to_database"] is False and log["code"] == code
    assert box.budget.used_explore == 1  # 失败的调用同样计数


def test_budget_counts_failures_and_stops_at_limit(monkeypatch):
    box, conn = explore_box(monkeypatch, budget=Budget(explore=2))
    box.dispatch("query_sql", {"sql": "SELECT nope FROM d1.facts", "purpose": "x"})
    box.dispatch("query_sql", {"sql": GOOD, "purpose": "x"})
    out = json.loads(box.dispatch("query_sql", {"sql": GOOD, "purpose": "x"}))
    assert "探索查询已达上限 2 次" in out["error"] and box.budget.used_explore == 2
    assert len([c for c in conn.calls if c[0] == "execute"]) == 1


def test_changed_asset_is_rejected_before_execution(monkeypatch):
    box, conn = explore_box(monkeypatch, connector=FakeConnector(physical={"facts": "999"}))
    out = json.loads(box.dispatch("query_sql", {"sql": GOOD, "purpose": "x"}))
    assert out["code"] == "asset_changed" and not [c for c in conn.calls if c[0] == "execute"]
    assert audits(box)[-1]["sent_to_database"] is False


def test_revoked_after_planning_or_before_delivery(monkeypatch):
    def revoked(session, user, validated):
        raise ExploreRejected("binding", "access_revoked", "表 d1.facts 当前已无权访问")
    box, conn = explore_box(monkeypatch, recheck=revoked)
    out = json.loads(box.dispatch("query_sql", {"sql": GOOD, "purpose": "x"}))
    assert out["code"] == "access_revoked" and not [c for c in conn.calls if c[0] == "execute"]
    calls = {"n": 0}

    def later(session, user, validated):  # 执行前通过、交付前撤权：结果不交付
        calls["n"] += 1
        if calls["n"] > 1:
            raise ExploreRejected("binding", "access_revoked", "表 d1.facts 当前已无权访问")
    box, conn = explore_box(monkeypatch, recheck=later)
    out = json.loads(box.dispatch("query_sql", {"sql": GOOD, "purpose": "x"}))
    assert out["sent_to_database"] is True and out["note"] == "结果未交付" and not box.datasets()
    assert audits(box)[-1]["outcome"] == "withheld"


def test_execution_errors_and_timeouts_are_reported(monkeypatch):
    box, conn = explore_box(monkeypatch, connector=FakeConnector(error=QueryError("timeout", 57014, "canceling statement")))
    out = json.loads(box.dispatch("query_sql", {"sql": GOOD, "purpose": "x"}))
    assert out["code"] == "timeout" and not box.datasets() and audits(box)[-1]["outcome"] == "failed"
    box, conn = explore_box(monkeypatch)
    monkeypatch.setattr(box.budget, "remaining", lambda: 10)
    out = json.loads(box.dispatch("query_sql", {"sql": GOOD, "purpose": "x"}))
    assert out["code"] == "deadline" and conn.calls == []


def test_query_sql_takes_no_datasource_or_column_declarations(monkeypatch):
    """数据源由系统按 SQL 里的资产确定，模型不填；列的量纲声明已删除（显示由系统按量纲决定）。多余参数一律拒绝。"""
    box, conn = explore_box(monkeypatch)
    for extra in ({"datasource": "源甲"}, {"columns": [{"name": "total", "kind": "count"}]}):
        out = box.dispatch("query_sql", {"sql": GOOD, "purpose": "x", **extra})
        assert "ValidationError" in out or "error" in json.loads(out)
    assert not [c for c in conn.calls if c[0] == "execute"]
    ok = json.loads(box.dispatch("query_sql", {"sql": GOOD, "purpose": "x"}))
    assert ok["datasource"] == "源甲" and ok["engine"] == "postgres"  # 返回实际所在的数据源与引擎
    assert box.datasets()[ok["dataset"]].meta["units"]["total"] == {"kind": "level"}


def test_fabricated_numbers_and_text_cannot_be_cited_as_facts(monkeypatch):
    """用常量伪造事实、把数字转成文本冒充维度：渲染与核验拦下。"""
    from avelis.analysis.verify import ground_report

    result = QueryResult(columns=["region", "fake", "up"], rows=[["r1", 42, "R1"]], truncated=False)
    box, _ = explore_box(monkeypatch, connector=FakeConnector(result=result))
    out = json.loads(box.dispatch("query_sql", {"purpose": "x",
                                                "sql": "SELECT region, 42 AS fake, CAST(region AS VARCHAR) AS up FROM d1.facts"}))
    ds = box.datasets()[out["dataset"]]
    data = {out["dataset"]: {"kind": ds.kind, "columns": ds.columns, "rows": [dict(zip(ds.columns, r)) for r in ds.rows],
                             **{k: ds.meta.get(k) for k in ("units", "explore", "column_evidence")}}}
    claims = [{"type": "fact", "parts": ["数值 ", {"dataset": out["dataset"], "column": "fake", "row": 1}]},
              {"type": "fact", "parts": [{"dataset": out["dataset"], "label": "up", "row": 1}, " 最高"]},
              {"type": "fact", "parts": [{"dataset": out["dataset"], "label": "region", "row": 1}, " 最高"]}]
    rendered, verification = ground_report({"title": "t", "summary": "s", "claims": claims}, data, None)
    first, second, third = verification["claims"]
    assert first["status"] == "unverified" and "假设计算" in first["problems"][0]
    assert second["status"] == "unverified" and "不是维度列的直接投影" in second["problems"][0]
    assert third["status"] == "verified" and rendered["claims"][2]["segments"][0]["kind"] == "label"
    assert rendered["claims"][0]["segments"][1]["explore"] is True


def test_sample_values_goes_through_the_same_checks(monkeypatch):
    box, conn = explore_box(monkeypatch, connector=FakeConnector(
        result=QueryResult(columns=["value", "n"], rows=[["r1", 3]], truncated=False)))
    asset = SimpleNamespace(datasource_id="ds1", catalog_name="appdb", db_name="d1", name="facts")
    monkeypatch.setattr(box.session, "scalar", lambda stmt: asset)
    box.session.store[(Datasource, "ds1")] = SimpleNamespace(kind="postgres")
    out = json.loads(box.dispatch("sample_values", {"asset": "appdb.d1.facts", "column": "region", "keyword": "r'; --"}))
    executed = [c for c in conn.calls if c[0] == "execute"][0]
    assert "LIMIT 21" in executed[1] and "'%r''; --%'" in executed[1]  # 关键字按字面量转义
    # 关键字匹配先转成文本：数值、日期列也能用，不会把 LIKE 用在非文本列上（第 5 步评测里 StarRocks 报错的缺陷）
    assert 'CAST("facts"."region" AS VARCHAR) LIKE' in executed[1]
    qty = json.loads(box.dispatch("sample_values", {"asset": "appdb.d1.facts", "column": "qty", "keyword": "5"}))
    assert "error" not in qty and 'CAST("facts"."qty" AS VARCHAR) LIKE' in [c for c in conn.calls if c[0] == "execute"][-1][1]
    ds = box.datasets()[out["dataset"]]
    assert ds.meta["preview"] is True and box.budget.used_explore == 2 and "取值预览" in out["note"]
    monkeypatch.setattr(box.session, "scalar", lambda stmt: None)
    missing = json.loads(box.dispatch("sample_values", {"asset": "appdb.d1.nope", "column": "x"}))
    assert "不存在或当前用户不可见" in missing["error"] and box.budget.used_explore == 3


def test_sources_without_profile_or_switched_off(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    for kind, allow, code in (("mysql", True, "engine_unsupported"), ("postgres", False, "exploration_disabled")):
        box.session.store[(Datasource, "ds9")] = Datasource(id="ds9", name="源乙", kind=kind, host="h", port=1,
                                                             options={}, allow_exploration=allow)
        with pytest.raises(ExploreRejected) as e:
            exploration.explore_source(box.session, "ds9")
        assert e.value.code == code
    assert "query_sql" not in {t.name for t in box.tools()}  # 没有可探索的数据源：不提供工具


def test_real_resolver_binding_rules(monkeypatch):
    """绑定规则：不可见与不存在同样报错；StarRocks 只允许原生内表；PostgreSQL 普通表生成 FROM ONLY、分区表拒绝；
    视图要求上游血缘已确认且全部可见；必须写出库名。"""
    from avelis.ask import service as ask_service
    from avelis.models import AssetVersion

    assets = {
        ("default_catalog", "d1", "native"): SimpleNamespace(id="a1", kind="table", physical_id="11", current_version_id="v1"),
        ("default_catalog", "d1", "ext"): SimpleNamespace(id="a2", kind="external_table", physical_id=None, current_version_id="v2"),
        ("ext_catalog", "d1", "t"): SimpleNamespace(id="a3", kind="table", physical_id=None, current_version_id="v3"),
        ("default_catalog", "d1", "dup_model_missing"): SimpleNamespace(id="a4", kind="table", physical_id="14", current_version_id="v4"),
        ("default_catalog", "d1", "v_ok"): SimpleNamespace(id="a5", kind="view", physical_id=None, current_version_id="v5"),
        ("default_catalog", "d1", "v_hidden"): SimpleNamespace(id="a6", kind="view", physical_id=None, current_version_id="v6"),
        ("default_catalog", "d1", "v_unknown"): SimpleNamespace(id="a7", kind="view", physical_id=None, current_version_id="v7"),
        ("default_catalog", "d1", "v_unbound"): SimpleNamespace(id="a9", kind="view", physical_id=None, current_version_id="v9"),
        ("default_catalog", "d1", "secret"): SimpleNamespace(id="a8", kind="table", physical_id="18", current_version_id="v8"),
        ("appdb", "public", "plain"): SimpleNamespace(id="b1", kind="table", physical_id="21", current_version_id="w1"),
        ("appdb", "public", "parted"): SimpleNamespace(id="b2", kind="table", physical_id="22", current_version_id="w2"),
    }
    versions = {"v1": {"table_model": "DUPLICATE_KEYS"}, "v2": {}, "v3": {"table_model": "x"}, "v4": {"table_model": None},
                "v5": {}, "v6": {}, "v7": {}, "v8": {"table_model": "x"}, "v9": {},
                "w1": {"properties": {"relkind": "r"}}, "w2": {"properties": {"relkind": "p"}}}

    class Session:
        def __init__(self):
            self.key = None

        def scalar(self, stmt):
            params = stmt.compile().params
            if "asset_version_id_1" in params:  # 视图定义
                return "SELECT c1 FROM d1.native"
            vals = [v for k, v in params.items() if k.startswith(("catalog_name", "db_name", "name"))]
            return assets.get(tuple(vals[:3]))

        def execute(self, stmt):  # 血缘边：(source_asset_id, source_ref)
            target = stmt.compile().params["target_asset_id_1"]
            return {"a5": [("u1", "d1.native")], "a6": [("up_hidden", "d1.hidden")],
                    "a9": [("u1", "d1.native"), (None, "d1.unknown_src")]}.get(target, [])

        def get(self, cls, ident, **kw):
            if cls is AssetVersion:
                v = versions[ident]
                return SimpleNamespace(id=ident, table_model=v.get("table_model"), properties=v.get("properties", {}))
            return None

        def scalars(self, stmt):
            return [SimpleNamespace(name="c1", data_type="int", comment=None)]

    def node(ident, kind="table"):
        return SimpleNamespace(id=ident, kind=kind, db_name="d1", name=ident, current_version_id=f"ver_{ident}")
    monkeypatch.setattr(exploration.access, "domain_ids", lambda s, u: {"dA"})
    monkeypatch.setattr(exploration.access, "can_read_asset", lambda s, u, a, allowed=None: a.id not in ("a8", "up_hidden"))
    monkeypatch.setattr(ask_service, "upstream_closure", lambda s, ids: {
        "a5": {"a5": node("a5", "view"), "u1": node("u1")}, "a6": {"a6": node("a6", "view"), "up": node("up_hidden")},
        "a7": {"a7": node("a7", "view")}, "a9": {"a9": node("a9", "view"), "u1": node("u1")}}[ids[0]])

    def bind(engine, catalog, db, name):
        source = ExploreSource(SimpleNamespace(id="ds1"), PROFILES[engine], "default_catalog" if engine == "starrocks" else "appdb")
        return exploration.resolver(Session(), SimpleNamespace(id="u"), source)(catalog, db, name)

    def code(engine, catalog, db, name):
        with pytest.raises(ExploreRejected) as e:
            bind(engine, catalog, db, name)
        return e.value.code

    assert bind("starrocks", None, "d1", "native").physical_id == "11"
    assert code("starrocks", None, "d1", "ext") == "kind_not_allowed"
    assert code("starrocks", "ext_catalog", "d1", "t") == "kind_not_allowed"  # 外部 catalog
    assert code("starrocks", None, "d1", "dup_model_missing") == "kind_not_allowed"  # 看不出是原生内表：拒绝
    assert code("starrocks", None, "d1", "secret") == code("starrocks", None, "d1", "no_such") == "asset_not_visible"
    view = bind("starrocks", None, "d1", "v_ok")
    assert view.kind == "view" and view.definition == "SELECT c1 FROM d1.native" and view.upstream == (("u1", "ver_u1"),)
    assert code("starrocks", None, "d1", "v_unbound") == "view_lineage_unresolved"  # #2 上游有未绑定的来源边
    with pytest.raises(ExploreRejected) as unresolved:
        bind("starrocks", None, "d1", "v_unbound")
    assert "unknown_src" not in unresolved.value.message and "d1.v_unbound" in unresolved.value.message  # 不泄露未授权的名称
    assert "unknown_src" not in json.dumps(unresolved.value.view(), ensure_ascii=False)
    assert code("starrocks", None, "d1", "v_hidden") == "view_upstream_not_visible"
    assert code("starrocks", None, "d1", "v_unknown") == "view_lineage_unknown"
    assert code("starrocks", None, None, "native") == "table_unqualified"
    assert bind("postgres", None, "public", "plain").use_only is True
    assert code("postgres", None, "public", "parted") == "partitioned_table"


def _view_validated():
    from avelis.analysis.explore import BoundColumn, BoundTable, Validated

    view = BoundTable("av", "vv1", None, "default_catalog", "d1", "v_sales", "view", (BoundColumn("c1", "int"),),
                      definition="SELECT c1  FROM d1.base", upstream=(("ab", "vb1"),))
    return Validated(sql="SELECT 1", tables=[view], columns=[], limit=10, dialect="starrocks", profile="starrocks/1")


def test_view_definition_change_is_caught_before_execution():
    """#3 视图被 CREATE OR REPLACE 改了定义：执行前与绑定版本的定义比较，变了就拒绝。"""
    validated = _view_validated()

    class Conn:
        def __init__(self, definition):
            self.definition = definition

        def describe_objects(self, objects, *, timeout_seconds=None):
            return {("d1", "v_sales"): {"physical_id": None, "columns": [["c1", "int"]], "definition": self.definition}}

    exploration.live_check(Conn("SELECT c1 FROM   d1.base"), validated, timeout_seconds=10)  # 只是空白不同
    with pytest.raises(ExploreRejected) as e:
        exploration.live_check(Conn("SELECT c1 FROM d1.other"), validated, timeout_seconds=10)
    assert e.value.code == "asset_changed" and "定义已变化" in e.value.message


def test_recheck_covers_the_view_upstream_closure(monkeypatch):
    """#4 执行前、交付前的权限复查一起看视图的上游闭包与绑定版本。"""
    from avelis.models import Asset

    validated = _view_validated()
    assets = {"av": SimpleNamespace(id="av", status="active", current_version_id="vv1"),
              "ab": SimpleNamespace(id="ab", status="active", current_version_id="vb1")}

    class Session:
        def get(self, cls, ident, **kw):
            return assets.get(ident) if cls is Asset else None

    visible = {"av": True, "ab": True}
    monkeypatch.setattr(exploration.access, "domain_ids", lambda s, u: set())
    monkeypatch.setattr(exploration.access, "can_read_asset", lambda s, u, a, allowed=None: visible[a.id])
    exploration.recheck_access(Session(), None, validated)
    visible["ab"] = False  # 撤掉上游的权限
    with pytest.raises(ExploreRejected) as e:
        exploration.recheck_access(Session(), None, validated)
    assert e.value.code == "access_revoked" and "上游" in e.value.message
    visible["ab"] = True
    assets["ab"].current_version_id = "vb2"  # 上游有了新版本
    with pytest.raises(ExploreRejected) as e:
        exploration.recheck_access(Session(), None, validated)
    assert e.value.code == "asset_changed"


def test_calc_tools_inherit_column_evidence(monkeypatch):
    """#5 假设列经过 derive 仍是假设；#6 CAST 出来的列经过 segment_compare 复制成分组键后，不能重新当作 LabelRef。"""
    from avelis.analysis.verify import ground_report

    result = QueryResult(columns=["region", "label", "fake", "total"], rows=[["r1", "1", 99999, 10], ["r2", "2", 99999, 20]],
                         truncated=False)
    box, _ = explore_box(monkeypatch, connector=FakeConnector(result=result))
    out = json.loads(box.dispatch("query_sql", {"purpose": "x", "sql":
        "SELECT region, CAST(qty AS VARCHAR) AS label, 99999 AS fake, SUM(amount) AS total FROM d1.facts GROUP BY region, qty"}))
    ref = out["dataset"]
    assert box.datasets()[ref].meta["column_evidence"]["fake"]["assumption"] is True
    derived = json.loads(box.dispatch("derive", {"label": "看似干净", "formula": "a", "values": [
        {"name": "a", "dataset": ref, "column": "fake", "match": {"region": "r1"}}]}))
    assert box.datasets()[derived["dataset"]].meta["assumption"] is True  # 不会因为再算一次就不是假设
    grouped = json.loads(box.dispatch("segment_compare", {"dataset": ref, "segment": "label", "measures": ["total"]}))
    seg = box.datasets()[grouped["dataset"]]
    assert seg.meta["column_evidence"]["label"]["label_ok"] is False
    data = {r: {"kind": d.kind, "columns": d.columns, "rows": [dict(zip(d.columns, x)) for x in d.rows],
                "summary": d.meta.get("summary"),
                **{k: d.meta.get(k) for k in ("units", "summary_units", "explore", "assumption", "column_evidence")}}
            for r, d in box.datasets().items()}
    claims = [{"type": "calc", "parts": ["结果 ", {"dataset": derived["dataset"], "summary": "value"}]},
              {"type": "fact", "parts": [{"dataset": grouped["dataset"], "label": "label", "row": 1}, " 最高"]}]
    _, v = ground_report({"title": "t", "summary": "s", "conclusion": "insufficient_evidence", "claims": claims}, data, None)
    assert "假设计算" in v["claims"][0]["problems"][0]
    assert "不是维度列的直接投影" in v["claims"][1]["problems"][0]


def test_timeout_is_rechecked_before_dispatch(monkeypatch):
    """#8 物理核对本身耗时，核对之后剩余时间不足：派发前重新计算超时值，≤ 0 时拒绝，不会用 statement_timeout = 0
    （不限时）执行；物理核对查询也带统一截止时间。"""
    box, conn = explore_box(monkeypatch)

    def slow_check():  # 物理核对用掉了几乎全部剩余时间：之后只剩约 4 秒（4 − 5 < 0）
        box.budget.started -= box.budget.remaining() - 4
    conn.on_describe = slow_check
    out = json.loads(box.dispatch("query_sql", {"sql": GOOD, "purpose": "x"}))
    describe = [c for c in conn.calls if c[0] == "describe"]
    assert len(describe) == 1 and 0 < describe[0][2] <= box.EXPLORE_MAX_SECONDS  # 核对确实执行了，且带超时
    assert out["code"] == "deadline" and out["sent_to_database"] is False
    assert not [c for c in conn.calls if c[0] == "execute"] and audits(box)[-1]["sent_to_database"] is False


def test_live_check_uses_remaining_time(monkeypatch):
    """#8 物理核对的超时取剩余时间（不超过探索上限）；剩余时间已不足时不再发核对查询。"""
    box, conn = explore_box(monkeypatch)
    box.budget.started -= box.budget.seconds - 20  # 只剩约 20 秒
    json.loads(box.dispatch("query_sql", {"sql": GOOD, "purpose": "x"}))
    describe = [c for c in conn.calls if c[0] == "describe"]
    assert describe and 10 <= describe[0][2] <= 15
    execute = [c for c in conn.calls if c[0] == "execute"]
    assert execute and 0 < execute[0][2]["timeout_seconds"] <= 15


@pytest.mark.parametrize("kill_error, outcome, words, failure", [
    (None, "kill_sent", "已向服务端发送取消", None),
    ((1094, "Unknown thread id"), "already_finished", "服务端查询已结束", None),
    ((1227, "Access denied"), "cancel_failed", "发送取消失败", "InternalError: (1227, 'Access denied')"),
    ("lost", "cancel_failed", "发送取消失败", "OperationalError: (2013, 'Lost connection to MySQL server during query')"),
    ("connect", "cancel_failed", "发送取消失败", "OperationalError: (2003, \"Can't connect to MySQL server\")"),
])
def test_starrocks_kill_query_outcome_is_reported_truthfully(monkeypatch, kill_error, outcome, words, failure):
    """#9 KILL QUERY 尽力而为：语句被接受记为“已发送取消”（不写成已取消）；目标已结束、发送失败（权限、通信超时、
    断连）如实区分，失败时的异常类别与消息写进审计，不进给模型的消息。"""
    import pymysql

    from avelis.connectors.starrocks import StarRocksConnector

    killed = []

    class Cursor:
        def __init__(self, role):
            self.role = role

        def execute(self, sql):
            if self.role == "killer":
                killed.append(sql)
                if kill_error == "lost":  # 发 KILL 时通信超时、断连
                    raise pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query")
                if kill_error:
                    raise pymysql.err.InternalError(*kill_error)
            elif sql.startswith("SELECT region"):
                raise pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query")

        def fetchone(self):
            return (42,)

    class Conn:
        def __init__(self, role):
            self.role = role

        def cursor(self, *a):
            return Cursor(self.role)

        def close(self):
            pass

    roles = iter(["query", "killer"])
    connector = StarRocksConnector("h", 1, "u", "p")

    def connect(read_timeout=60):
        role = next(roles)
        if role == "killer" and kill_error == "connect":  # 取消用的连接建不起来
            raise pymysql.err.OperationalError(2003, "Can't connect to MySQL server")
        return Conn(role)
    monkeypatch.setattr(connector, "_connect", connect)
    with pytest.raises(QueryError) as e:
        connector.execute_explore("SELECT region FROM t", timeout_seconds=5, max_rows=10, mem_limit_bytes=1)
    assert killed == ([] if kill_error == "connect" else ["KILL QUERY 42"])
    assert e.value.cancel == outcome and words in e.value.message and e.value.cancel_error == failure
    assert "已取消" not in e.value.message and (failure is None or failure not in e.value.message)
    detail = {}
    monkeypatch.setattr(exploration.audit, "record", lambda s, u, a, t, i, **d: detail.update(d))
    exploration.record(None, None, "r1", step=1, source=None, purpose="x", outcome="failed", error=e.value)
    assert detail["cancel"] == outcome and detail.get("cancel_error") == failure
