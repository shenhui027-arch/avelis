"""更多引擎的通用验收场景（ADR 0004）：每种引擎在真实实例上走同一组场景，只在 EngineProfile 中给出差异。

演示数据与 PostgreSQL 门店库相同：store.shops / sales / returns 与视图 city_daily_sales；
2026-09-15 上海 250.00（作废单不计）、北京 80.00；退货单 sale_id = 9002 有两条。
场景：建数据源与采集、视图列级血缘与粒度、发布口径与按该引擎方言问数、粒度核验、物理身份（改名 / 删后重建）、
定义变化的执行前核对与受影响、超时与错误归类、账号只读。全部经 HTTP API 与真实数据库。
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from tests.conftest import Api

DAY = "2026-09-15"
TABLES = ("shops", "sales", "returns", "city_daily_sales")


class EngineProfile:
    kind: str
    label: str  # 引擎显示名，用于数据源与口径命名
    port_key: str
    collect_keys: tuple[str, str]  # (用户名, .env 中口令的键)
    query_keys: tuple[str, str]
    options: dict
    missing_option: tuple[dict, str]  # 缺少必填项的配置，以及 422 提示中应出现的配置名
    catalog: str  # 资产名的 Catalog 段
    sql_from: str  # 问数 SQL 中的 FROM 片段
    grain_sql_name: str  # 粒度核验 SQL 中的表名写法
    sales_unique_key: bool  # sales 是否有数据库保证的唯一键（观察到的粒度）
    rename: tuple[str, ...]
    recreate: tuple[str, ...]
    replace_view: tuple[str, ...]  # 去掉作废单过滤：上海变为 310
    sleep_sql: str
    write_sql: str
    data_sql: str = "SELECT COUNT(*) FROM store.sales"
    # 采集账号读业务数据时的错误归类（Oracle 对无权限的对象报“不存在”，即 ORA-00942）
    collector_denied: str = "permission"

    def running(self, env: dict) -> bool:
        raise NotImplementedError

    def seed(self, env: dict) -> None:
        raise NotImplementedError

    def ddl(self, env: dict, *statements: str) -> None:
        raise NotImplementedError

    def connector(self, env: dict, purpose: str):
        raise NotImplementedError

    def ident(self, name: str) -> str:
        """数据库目录中保存的标识符写法（Oracle 把未加引号的名字存为大写）。"""
        return name

    def qualified(self, name: str) -> str:
        return f"{self.catalog}.{self.ident('store')}.{self.ident(name)}"


class EngineSuite:
    """子类给出 profile；pytest 按 test_ 前缀收集。共用同一个模块级演示环境（stack）。"""

    profile: EngineProfile
    ctx: dict

    @pytest.fixture(scope="class", autouse=True)
    @classmethod
    def _engine(cls, stack):
        _, env, _ = stack
        if not cls.profile.running(env):
            pytest.fail(f"{cls.profile.label} 演示库未启动：先运行 uv run scripts/demo.py up --engines")
        cls.profile.seed(env)
        cls.ctx = {}

    # --- helpers ---------------------------------------------------------------

    def collect(self, api: Api) -> dict:
        run = api.post(f"/api/datasources/{self.ctx['ds']}/collect?wait=true")
        assert run["status"] == "succeeded", run
        return run

    def assets(self, api: Api) -> dict[str, dict]:
        return {a["qualified"]: a for a in api.get("/api/assets", params={"datasource_id": self.ctx["ds"]})}

    @staticmethod
    def latest_events(api: Api) -> set[tuple]:
        cs = api.get(f"/api/change-sets/{api.get('/api/change-sets')[0]['id']}")
        return {(e["kind"], e["asset_id"], e["column"]) for e in cs["events"]}

    @staticmethod
    def value_of(answer: dict) -> Decimal:
        assert answer["status"] == "answered", answer
        return Decimal(str(answer["result"]["rows"][0][-1]))

    def question(self, city: str) -> str:
        return f"{DAY} {city}{self.profile.label}门店销售额"

    # --- scenarios -------------------------------------------------------------

    def test_00_create_and_collect(self, stack, admin: Api):
        from avelis import demo_setup

        client, env, _ = stack
        p = self.profile
        demo_setup.setup(reset=False)
        pw = env["AVELIS_DEMO_USER_PASSWORD"]
        self.ctx["apis"] = {u: Api(client, u, pw) for u in ("zhang", "li", "wang")}
        engine = {e["kind"]: e for e in admin.get("/api/engines")}[p.kind]
        assert {"metadata", "view_lineage", "query", "live_check"} <= set(engine["capabilities"])
        base = {"name": f"门店库（{p.label} 验收）", "kind": p.kind, "host": "127.0.0.1", "port": int(env[p.port_key]),
                "collect": {"username": p.collect_keys[0], "password": env[p.collect_keys[1]]},
                "query": {"username": p.query_keys[0], "password": env[p.query_keys[1]]}}
        bad, label = p.missing_option
        assert label in admin.post("/api/datasources", {**base, "options": bad}, expect=422)["detail"]
        ds = admin.post("/api/datasources", {**base, "options": p.options}, expect=201)
        assert ds["engine"]["kind"] == p.kind
        self.ctx["ds"] = ds["id"]
        run = self.collect(admin)
        assert run["stats"]["assets_observed"] == 4
        found = self.assets(admin)
        assert set(found) == {p.qualified(t) for t in TABLES}
        self.ctx["ids"] = {t: found[p.qualified(t)]["id"] for t in TABLES}
        assert found[p.qualified("city_daily_sales")]["kind"] == "view"
        trade = next(d["id"] for d in admin.get("/api/domains") if d["code"] == "trade")
        for asset_id in self.ctx["ids"].values():
            self.ctx["apis"]["zhang"].patch(f"/api/assets/{asset_id}", {"domain_id": trade})
        self.ctx["trade"] = trade

    def test_01_view_lineage_grains_and_comments(self, admin: Api):
        n = self.profile.ident
        ids = self.ctx["ids"]
        view = admin.get(f"/api/assets/{ids['city_daily_sales']}")
        assert view["parse"]["status"] == "ok", view["parse"]
        assert all(c["fingerprint_status"] == "ok" for c in view["columns"])
        assert any(g["columns"] == [n("sale_date"), n("city")] and g["origin"] == "parsed" for g in view["grains"])
        sales = admin.get(f"/api/assets/{ids['sales']}")
        observed = [g["columns"] for g in sales["grains"] if g["origin"] == "observed"]
        # 只有数据库保证唯一的键才算观察到的粒度（ClickHouse 的排序键不算）
        assert observed == ([[n("sale_id")]] if self.profile.sales_unique_key else [])
        docs = {c["name"]: c["doc"] for c in sales["columns"]}
        assert docs[n("amount")]["status"] == "ok" and docs[n("amount")]["comment"] == "成交金额"
        assert docs[n("status")]["status"] == "missing"
        graph = admin.get("/api/lineage/graph", params={"asset_id": ids["city_daily_sales"]})
        edge = next(e for e in graph["edges"]
                    if e["source"] == ids["sales"] and e["target"] == ids["city_daily_sales"])
        rel = {(c["target_column"], c["source_column"]): c["relation"] for c in edge["columns"]}
        assert rel[(n("sales_amount"), n("amount"))] == "value_aggregated"
        assert rel[(None, n("status"))] == "filter" and rel[(None, n("shop_id"))] == "join_key"
        city = {c["name"]: c["doc"] for c in view["columns"]}[n("city")]
        assert city["status"] == "missing"  # 视图与上游 shops.city 都没有注释

    def test_02_publish_and_ask_in_engine_dialect(self, admin: Api):
        p, n = self.profile, self.profile.ident
        zhang, li, wang = (self.ctx["apis"][u] for u in ("zhang", "li", "wang"))
        m = zhang.post("/api/metrics", {"code": f"store_sales_{p.kind}", "name": f"{p.label}门店销售额",
                                        "domain_id": self.ctx["trade"]}, expect=201)
        v = zhang.post(f"/api/metrics/{m['id']}/versions", {
            "description": "门店成交金额按日期、城市汇总（不含作废单）", "base_asset_id": self.ctx["ids"]["city_daily_sales"],
            "expression": f"SUM({n('sales_amount')})", "time_column": n("sale_date"),
            "dimensions": [{"name": "city", "column": n("city"), "label": "城市"}],
            "grain": [n("sale_date"), n("city")]}, expect=201)
        zhang.post(f"/api/metric-versions/{v['id']}/submit")
        assert li.post(f"/api/metric-versions/{v['id']}/approve", {"note": f"{p.label} 验收"})["status"] == "published"
        self.ctx["v1"] = v["id"]
        answer = wang.post("/api/ask", {"question": self.question("上海")})
        assert self.value_of(answer) == Decimal("250.00")
        assert p.sql_from in answer["sql"], answer["sql"]
        assert self.value_of(wang.post("/api/ask", {"question": self.question("北京")})) == Decimal("80.00")
        ranked = wang.post("/api/ask", {"question": f"{DAY} 各城市{p.label}门店销售额前 1"})
        assert ranked["status"] == "answered" and [r[0] for r in ranked["result"]["rows"]] == ["上海"]

    def test_03_grain_verify(self, admin: Api):
        zhang = self.ctx["apis"]["zhang"]
        bad = zhang.post(f"/api/assets/{self.ctx['ids']['returns']}/grains",
                         {"columns": [self.profile.ident("sale_id")], "reason": "错误声明"}, expect=201)
        checked = zhang.post(f"/api/grains/{bad['id']}/verify")
        assert checked["status"] == "violated" and checked["verify_detail"]["duplicate_example"][0] == "9002"
        assert self.profile.grain_sql_name in checked["verify_detail"]["sql"]

    def test_04_identity_rename_and_recreate(self, stack, admin: Api):
        _, env, _ = stack
        p = self.profile
        returns_id = self.ctx["ids"]["returns"]
        p.ddl(env, *p.rename)
        self.collect(admin)
        found = self.assets(admin)
        assert found[p.qualified("refunds")]["id"] == returns_id  # 物理身份不变：同一资产
        assert ("renamed", returns_id, None) in self.latest_events(admin)
        p.ddl(env, *p.recreate)
        self.collect(admin)
        recreated = self.assets(admin)[p.qualified("refunds")]["id"]
        assert recreated != returns_id and admin.get(f"/api/assets/{returns_id}")["status"] == "deleted"

    def test_05_definition_change_live_check_then_affected(self, stack, admin: Api):
        _, env, _ = stack
        wang = self.ctx["apis"]["wang"]
        self.profile.ddl(env, *self.profile.replace_view)
        stale = wang.post("/api/ask", {"question": self.question("上海")})
        assert stale["status"] == "blocked" and stale["error_code"] == "metadata_stale", stale
        assert stale["result"] == {} and stale["executed_at"] is None
        self.collect(admin)
        view_id = self.ctx["ids"]["city_daily_sales"]
        events = self.latest_events(admin)
        assert ("definition_changed", view_id, None) in events
        assert ("derivation_changed", view_id, self.profile.ident("sales_amount")) in events
        assert admin.get(f"/api/metric-versions/{self.ctx['v1']}")["status"] == "affected"
        blocked = wang.post("/api/ask", {"question": self.question("上海")})
        assert blocked["status"] == "blocked" and blocked["error_code"] == "metric_affected" and blocked["sql"] is None

    def test_06_timeout_errors_and_read_only(self, stack):
        from avelis.connectors.base import QueryError

        _, env, _ = stack
        p = self.profile
        reader = p.connector(env, "query")
        started = time.monotonic()
        with pytest.raises(QueryError) as timeout:
            reader.execute_select(p.sleep_sql, timeout_seconds=1, mem_limit_bytes=0, max_rows=1)
        assert timeout.value.code == "timeout", timeout.value.message
        assert time.monotonic() - started < 8
        with pytest.raises(QueryError) as missing:
            reader.execute_select("SELECT nope FROM store.sales", timeout_seconds=5, mem_limit_bytes=0, max_rows=1)
        assert missing.value.code == "invalid_object", missing.value.message
        with pytest.raises(QueryError) as write:
            reader.execute_select(p.write_sql, timeout_seconds=5, mem_limit_bytes=0, max_rows=1)
        assert write.value.code == "permission", write.value.message
        count = reader.execute_select(p.data_sql, timeout_seconds=5, mem_limit_bytes=0, max_rows=1)
        assert int(count.rows[0][0]) == 5  # 写入被拒，数据未变
        with pytest.raises(QueryError) as denied:  # 采集账号只读元数据，读不到业务数据
            p.connector(env, "collect").execute_select(p.data_sql, timeout_seconds=5, mem_limit_bytes=0, max_rows=1)
        assert denied.value.code == p.collector_denied, denied.value.message
