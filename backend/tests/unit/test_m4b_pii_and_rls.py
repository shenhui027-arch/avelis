"""M4B 单元测试：列级 PII 脱敏（含外发 LLM 强制脱敏）、SQL AST RLS 注入与策略 API 鉴权。"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis import config, modelcfg
from avelis.analysis import exploration, explore
from avelis.analysis.explore import BoundColumn, BoundTable, ExploreRejected, OutputColumn, Validated, profile_for
from avelis.analysis.tools import AnalysisToolbox, Budget
from avelis.api import data_policy as dp_api
from avelis.api.deps import current_user, get_session
from avelis.ask import agent as ask_agent
from avelis.ask import service as ask_service
from avelis.ask.compiler import compile_metric_query, distinct_values_sql
from avelis.ask.intent import Intent
from avelis.connectors.base import QueryResult
from avelis.db import Base, utcnow
from avelis.governance import data_policy
from avelis.governance.errors import GovernanceError
from avelis.models import (
    Asset,
    AssetColumn,
    AssetColumnPolicy,
    AssetRlsPolicy,
    AssetVersion,
    CollectionRun,
    ColumnVersion,
    Datasource,
    Domain,
    DomainMember,
    LineageEdge,
    Metric,
    MetricVersion,
    User,
    UserRole,
)


def _sqlite_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        execution_options={"schema_translate_map": {"avelis_agent": None}},
    )
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        copied = table.to_metadata(metadata)
        for col in copied.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()
    metadata.create_all(engine)
    return engine, Session(engine, expire_on_commit=False)


def test_default_policy_send_results_is_false():
    assert modelcfg.DEFAULT_POLICY["send_results"] is False
    assert modelcfg.DEFAULT_POLICY["send_code"] is True


def test_pii_mask_value_and_for_llm_always_masks(monkeypatch):
    s = config.build_settings({"AVELIS_SECRET": "m4b-unit-test-secret-0123456789"})
    monkeypatch.setattr(data_policy, "get_settings", lambda: s)

    assert data_policy.mask_value(None, "mask") is None
    assert data_policy.mask_value("alice@example.com", "mask") == "a***@example.com"
    assert data_policy.mask_value("13812345678", "mask") == "138****5678"
    assert data_policy.mask_value("ab", "mask") == "***"
    assert data_policy.mask_value("Alice", "mask") == "A***e"
    assert data_policy.mask_value("abcdefg", "mask") == "ab****fg"
    assert data_policy.mask_value("secret", "redact") == "[REDACTED]"

    h1 = data_policy.mask_value("13812345678", "hash", column_name="phone")
    h2 = data_policy.mask_value("13812345678", "hash", column_name="phone")
    h3 = data_policy.mask_value("13812345679", "hash", column_name="phone")
    assert h1.startswith("sha256:") and h1 == h2 and h1 != h3

    columns = ["email", "amount"]
    rows = [["bob@corp.com", 100]]
    spec = {0: ("pii", "mask", ("admin",))}

    # 豁免角色在界面查看时不脱敏
    exempt_rows, exempt_applied = data_policy.apply_pii_masking(
        columns, rows, spec, user_roles={"admin"}, for_llm=False
    )
    assert exempt_rows == [["bob@corp.com", 100]]
    assert exempt_applied == []

    # 非豁免角色被脱敏
    analyst_rows, analyst_applied = data_policy.apply_pii_masking(
        columns, rows, spec, user_roles={"analyst"}, for_llm=False
    )
    assert analyst_rows == [["b***@corp.com", 100]]
    assert len(analyst_applied) == 1 and analyst_applied[0]["for_llm"] is False

    # 外发 LLM（for_llm=True）忽略豁免角色，始终脱敏
    llm_rows, llm_applied = data_policy.apply_pii_masking(columns, rows, spec, user_roles={"admin"}, for_llm=True)
    assert llm_rows == [["b***@corp.com", 100]]
    assert len(llm_applied) == 1 and llm_applied[0]["for_llm"] is True


def test_validate_rls_predicate_allowlist_and_injection_blocks():
    allowed = {"region", "owner_id", "owner_name", "amount", "status"}
    assert data_policy.validate_rls_predicate("region = '华东'", allowed) is not None
    assert data_policy.validate_rls_predicate("owner_id = :current_user_id OR owner_name = :current_username", allowed)
    assert data_policy.validate_rls_predicate("amount >= 100 AND status IN ('paid', 'done')", allowed)

    for bad in (
        "",
        "region = 'a'; DROP TABLE orders",
        "region = 'a' -- comment",
        "region = 'a' /* block */",
        "region IN (SELECT region FROM secret_tbl)",
        "pg_sleep(1) = 0 AND region = 'a'",
        "unknown_col = 'x'",
        "other.region = 'x'",
        "owner_id = :evil_param",
        "1 = 1",
    ):
        with pytest.raises(ValueError):
            data_policy.validate_rls_predicate(bad, allowed)


def test_explore_rls_ast_injection_and_view_bypass_block():
    pg_profile = profile_for("postgres")
    sr_profile = profile_for("starrocks")
    assert pg_profile is not None and sr_profile is not None

    orders_tbl = BoundTable(
        asset_id="a_orders",
        asset_version_id="v_orders",
        physical_id="101",
        catalog="analytics",
        db="shop",
        name="orders",
        kind="table",
        columns=(
            BoundColumn("order_id", "bigint"),
            BoundColumn("customer_id", "bigint"),
            BoundColumn("region", "varchar"),
            BoundColumn("amount", "decimal"),
        ),
        use_only=True,
        rls_predicates=("region = :current_username",),
        rls_user=("u1", "east_user"),
    )
    dim_tbl = BoundTable(
        asset_id="a_cust",
        asset_version_id="v_cust",
        physical_id="102",
        catalog="analytics",
        db="shop",
        name="customers",
        kind="table",
        columns=(
            BoundColumn("customer_id", "bigint"),
            BoundColumn("phone", "varchar"),
            BoundColumn("vip_level", "varchar"),
        ),
        use_only=True,
        rls_predicates=("vip_level = 'gold'",),
        rls_user=("u1", "east_user"),
    )

    def resolver(_cat, _db, name):
        return orders_tbl if name == "orders" else dim_tbl

    # 基础 FROM 表注入 WHERE RLS，并替换 :current_username
    res = explore.validate(
        "SELECT o.order_id, o.amount FROM shop.orders o ORDER BY o.order_id",
        pg_profile,
        resolver,
        max_rows=50,
    )
    assert "east_user" in res.sql
    assert '"o"."region" = \'east_user\'' in res.sql

    # LEFT JOIN 表注入 ON 条件，不把 LEFT JOIN 变成 INNER JOIN
    res_left = explore.validate(
        "SELECT o.order_id, c.vip_level FROM shop.orders o LEFT JOIN shop.customers c ON o.customer_id = c.customer_id",
        pg_profile,
        resolver,
        max_rows=50,
    )
    left_sql = res_left.sql.upper()
    join_part, where_part = left_sql.split(" WHERE ", 1)
    assert "VIP_LEVEL" in join_part
    assert "EAST_USER" in where_part


def test_ask_compiler_preagg_rls_and_service_isolation_and_view_block(monkeypatch):
    left = SimpleNamespace(catalog_name="default_catalog", db_name="shop", name="order_items")
    right = SimpleNamespace(catalog_name="default_catalog", db_name="shop", name="dim_store")
    path = SimpleNamespace(code="oi_store", left_columns=["store_id"], right_columns=["store_id"])
    version = SimpleNamespace(
        time_column="b.dt",
        time_format=None,
        timezone=None,
        data_grain="day",
        dimensions=[{"name": "store", "column": "s.store_name"}],
        expression="SUM(b.amt)",
        filters=None,
    )
    intent = Intent(
        metric_code="sales",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 2),
        group_by=["store"],
    )
    compiled = compile_metric_query(
        version=version,
        aliases={"b": left, "s": right},
        steps=[{"alias": "s", "from": "b", "path": path, "right": right, "pre_aggregate": True}],
        intent=intent,
        max_rows=100,
        time_type="date",
        dialect="postgres",
        rls_predicates=[
            data_policy.bind_rls_predicate(
                "owner_id = :current_user_id", table_alias="b", user_id="u1", username="alice"
            )
        ],
        pre_agg_rls={
            "s": [data_policy.bind_rls_predicate("region = '华东'", table_alias="x", user_id="u1", username="alice")]
        },
    )
    assert "owner_id" in compiled.sql and "'u1'" in compiled.sql
    assert "x.region = '华东'" in compiled.sql or '"x"."region" = \'华东\'' in compiled.sql

    d_sql = distinct_values_sql(
        "default_catalog.shop.dim_store",
        "store_name",
        limit=10,
        dialect="postgres",
        rls_predicates=[
            data_policy.bind_rls_predicate("region = '华东'", table_alias="t", user_id="u1", username="alice")
        ],
    )
    assert '"t"."region" = \'华东\'' in d_sql

    # 端到端 Ask 服务：PII 脱敏（含豁免角色 + 外发 LLM 强制脱敏）、RLS 缓存签名隔离与视图上游 RLS 阻断
    engine, session = _sqlite_session()
    try:
        s_cfg = config.build_settings({"AVELIS_SECRET": "m4b-ask-test-secret-0123456789"})
        monkeypatch.setattr(data_policy, "get_settings", lambda: s_cfg)

        dom = Domain(id="d1", code="sales", name="销售域", owner_id="u_steward")
        u_steward = User(id="u_steward", username="steward", display_name="Steward", password_hash="x", is_active=True)
        u_analyst1 = User(id="u_a1", username="alice", display_name="Alice", password_hash="x", is_active=True)
        u_analyst2 = User(id="u_a2", username="bob", display_name="Bob", password_hash="x", is_active=True)
        ds = Datasource(
            id="ds1",
            name="pg",
            kind="postgres",
            host="h",
            port=5432,
            options={"database": "app", "schemas": ["shop"]},
            allow_exploration=True,
        )
        run1 = CollectionRun(id="cr1", datasource_id="ds1", trigger="manual", status="succeeded")
        asset = Asset(
            id="a1",
            datasource_id="ds1",
            domain_id="d1",
            catalog_name="app",
            db_name="shop",
            name="orders",
            kind="table",
            physical_id="11",
            status="active",
        )
        av = AssetVersion(
            id="av1",
            asset_id="a1",
            version_no=1,
            observed_run_id="cr1",
            last_seen_run_id="cr1",
            kind="table",
            catalog_name="app",
            db_name="shop",
            name="orders",
            content_hash="h1",
        )
        asset.current_version_id = "av1"
        ac_dt = AssetColumn(id="ac1", asset_id="a1", name="dt")
        ac_phone = AssetColumn(id="ac2", asset_id="a1", name="phone")
        ac_owner = AssetColumn(id="ac3", asset_id="a1", name="owner_name")
        ac_amt = AssetColumn(id="ac4", asset_id="a1", name="amt")
        c_dt = ColumnVersion(id="c1", asset_version_id="av1", column_id="ac1", ordinal=1, name="dt", data_type="date")
        c_phone = ColumnVersion(
            id="c2", asset_version_id="av1", column_id="ac2", ordinal=2, name="phone", data_type="varchar"
        )
        c_owner = ColumnVersion(
            id="c3", asset_version_id="av1", column_id="ac3", ordinal=3, name="owner_name", data_type="varchar"
        )
        c_amt = ColumnVersion(
            id="c4", asset_version_id="av1", column_id="ac4", ordinal=4, name="amt", data_type="numeric"
        )

        # 视图基于 a1，自身未设 RLS
        view_asset = Asset(
            id="a_view",
            datasource_id="ds1",
            domain_id="d1",
            catalog_name="app",
            db_name="shop",
            name="orders_v",
            kind="view",
            physical_id="12",
            status="active",
        )
        av_view = AssetVersion(
            id="av_view",
            asset_id="a_view",
            version_no=1,
            observed_run_id="cr1",
            last_seen_run_id="cr1",
            kind="view",
            catalog_name="app",
            db_name="shop",
            name="orders_v",
            content_hash="h2",
        )
        view_asset.current_version_id = "av_view"
        edge = LineageEdge(
            id="le1",
            source_asset_id="a1",
            source_ref="app.shop.orders",
            target_asset_id="a_view",
            target_asset_version_id="av_view",
            status="active",
            origin="parsed",
            relation="table",
        )

        metric = Metric(
            id="m1", code="orders_amt", name="订单金额", domain_id="d1", owner_id="u_steward", created_by="u_steward"
        )
        mv = MetricVersion(
            id="mv1",
            metric_id="m1",
            version_no=1,
            status="published",
            description="订单金额指标",
            base_asset_id="a1",
            expression="SUM(amt)",
            time_column="dt",
            timezone="UTC",
            created_by="u_steward",
            dimensions=[{"name": "phone", "column": "phone", "label": "手机号"}],
            grain=["dt", "phone"],
            joins=[],
            dependency_fingerprint="fp1",
        )
        col_pol = AssetColumnPolicy(
            id="cp1",
            asset_id="a1",
            column_name="phone",
            classification="pii",
            masking_strategy="mask",
            exempt_roles=["steward"],
            updated_at=utcnow(),
        )
        rls_pol = AssetRlsPolicy(
            id="rp1",
            asset_id="a1",
            name="owner_only",
            scope_type="role",
            scope_value="analyst",
            predicate_sql="owner_name = :current_username",
            enabled=True,
        )
        session.add_all(
            [
                dom,
                u_steward,
                u_analyst1,
                u_analyst2,
                DomainMember(domain_id="d1", user_id="u_steward", role="owner"),
                DomainMember(domain_id="d1", user_id="u_a1", role="viewer"),
                DomainMember(domain_id="d1", user_id="u_a2", role="viewer"),
                UserRole(user_id="u_steward", role="steward"),
                UserRole(user_id="u_a1", role="analyst"),
                UserRole(user_id="u_a2", role="analyst"),
                ds,
                run1,
                asset,
                av,
                ac_dt,
                ac_phone,
                ac_owner,
                ac_amt,
                c_dt,
                c_phone,
                c_owner,
                c_amt,
                view_asset,
                av_view,
                edge,
                metric,
                mv,
                col_pol,
                rls_pol,
            ]
        )
        session.commit()

        # 视图上游有 RLS 时，exploration resolver 拒绝
        src = SimpleNamespace(datasource=ds, profile=profile_for("postgres"), default_catalog="app")
        with pytest.raises(ExploreRejected) as exc:
            exploration.resolver(session, u_analyst1, src)("app", "shop", "orders_v")
        assert exc.value.code == "rls_upstream_view"

        # Ask 内存缓存模拟与假连接器
        mem_cache: dict[str, dict] = {}
        monkeypatch.setattr(ask_service, "_cache_get", lambda k: mem_cache.get(k))
        monkeypatch.setattr(ask_service, "_cache_put", lambda k, v, ttl, tags=None: mem_cache.__setitem__(k, v))
        monkeypatch.setattr(ask_service, "freshness_boundary", lambda *_: {})
        monkeypatch.setattr(ask_service, "dependency_drift", lambda *_: [])
        monkeypatch.setattr(ask_service, "_live_check", lambda *a, **kw: [])

        executed_sqls: list[str] = []

        class FakeConn:
            def freshness(self, _):
                return {}

            def execute_select(self, sql, **_kw):
                executed_sqls.append(sql)
                if "bob" in sql:
                    return QueryResult(columns=["phone", "value"], rows=[["13900009999", 50]], truncated=False)
                return QueryResult(columns=["phone", "value"], rows=[["13812345678", 200]], truncated=False)

        monkeypatch.setattr(ask_service, "query_connector", lambda *_: FakeConn())
        monkeypatch.setattr(ask_service, "publish_check", lambda *_: {"fan_out": []})

        ans_a = ask_service.start_answer(session, u_analyst1, "按手机号查订单金额", "rule", None)
        ask_service.fulfil(
            session,
            u_analyst1,
            Intent(
                metric_code="orders_amt", start_date=date(2026, 9, 1), end_date=date(2026, 9, 1), group_by=["phone"]
            ),
            ans_a,
            ask_service.Catalog([], {"orders_amt": (metric, mv)}),
            settings=s_cfg,
        )
        assert ans_a.status == "answered"
        assert ans_a.result["rows"][0][0] == "138****5678"
        assert "alice" in executed_sqls[-1]

        # 第二位 analyst (bob) 不应命中 alice 的缓存
        ans_b = ask_service.start_answer(session, u_analyst2, "按手机号查订单金额", "rule", None)
        ask_service.fulfil(
            session,
            u_analyst2,
            Intent(
                metric_code="orders_amt", start_date=date(2026, 9, 1), end_date=date(2026, 9, 1), group_by=["phone"]
            ),
            ans_b,
            ask_service.Catalog([], {"orders_amt": (metric, mv)}),
            settings=s_cfg,
        )
        assert ans_b.cache_hit is False
        assert ans_b.result["rows"][0][0] == "139****9999"
        assert "bob" in executed_sqls[-1]

        # steward 豁免角色：页面可见原始手机号，但 _result_for_llm 仍强制脱敏
        ans_s = ask_service.start_answer(session, u_steward, "按手机号查订单金额", "rule", None)
        ask_service.fulfil(
            session,
            u_steward,
            Intent(
                metric_code="orders_amt", start_date=date(2026, 9, 1), end_date=date(2026, 9, 1), group_by=["phone"]
            ),
            ans_s,
            ask_service.Catalog([], {"orders_amt": (metric, mv)}),
            settings=s_cfg,
        )
        assert ans_s.result["rows"][0][0] == "13812345678"
        llm_res = ask_agent._result_for_llm(ans_s)
        assert llm_res["rows"][0][0] == "138****5678"
    finally:
        session.close()
        engine.dispose()


def test_data_policy_api_crud_and_rbac():
    engine, session = _sqlite_session()
    try:
        dom = Domain(id="d1", code="sales", name="销售域", owner_id="u_s")
        steward = User(id="u_s", username="steward", display_name="Steward", password_hash="x", is_active=True)
        analyst = User(id="u_a", username="analyst", display_name="Analyst", password_hash="x", is_active=True)
        ds = Datasource(
            id="ds1", name="pg", kind="postgres", host="h", port=5432, options={"database": "app", "schemas": ["shop"]}
        )
        run1 = CollectionRun(id="cr1", datasource_id="ds1", trigger="manual", status="succeeded")
        asset = Asset(
            id="a1",
            datasource_id="ds1",
            domain_id="d1",
            catalog_name="app",
            db_name="shop",
            name="orders",
            kind="table",
            status="active",
        )
        av = AssetVersion(
            id="av1",
            asset_id="a1",
            version_no=1,
            observed_run_id="cr1",
            last_seen_run_id="cr1",
            kind="table",
            catalog_name="app",
            db_name="shop",
            name="orders",
            content_hash="h1",
        )
        asset.current_version_id = "av1"
        ac1 = AssetColumn(id="ac1", asset_id="a1", name="phone")
        ac2 = AssetColumn(id="ac2", asset_id="a1", name="region")
        c1 = ColumnVersion(
            id="c1", asset_version_id="av1", column_id="ac1", ordinal=1, name="phone", data_type="varchar"
        )
        c2 = ColumnVersion(
            id="c2", asset_version_id="av1", column_id="ac2", ordinal=2, name="region", data_type="varchar"
        )
        session.add_all(
            [
                dom,
                steward,
                analyst,
                DomainMember(domain_id="d1", user_id="u_s", role="owner"),
                DomainMember(domain_id="d1", user_id="u_a", role="viewer"),
                UserRole(user_id="u_s", role="steward"),
                UserRole(user_id="u_a", role="analyst"),
                ds,
                run1,
                asset,
                av,
                ac1,
                ac2,
                c1,
                c2,
            ]
        )
        session.commit()

        app = FastAPI()
        app.include_router(dp_api.router)

        @app.exception_handler(GovernanceError)
        async def _gov_err(_: Request, exc: GovernanceError):
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

        holder = {"user": analyst}
        app.dependency_overrides[current_user] = lambda: holder["user"]
        app.dependency_overrides[get_session] = lambda: session

        with TestClient(app) as client:
            # analyst 可读，不可写
            assert client.get("/api/assets/a1/security-policies").status_code == 200
            assert (
                client.put(
                    "/api/assets/a1/column-policies/phone",
                    json={"classification": "pii", "masking_strategy": "mask", "exempt_roles": []},
                ).status_code
                == 403
            )
            assert (
                client.post(
                    "/api/assets/a1/rls-policies",
                    json={"name": "r1", "scope_type": "all", "predicate_sql": "region = 'east'"},
                ).status_code
                == 403
            )

            # steward 可配置列脱敏与 RLS，非法谓词返回 422
            holder["user"] = steward
            r_col = client.put(
                "/api/assets/a1/column-policies/phone",
                json={"classification": "pii", "masking_strategy": "hash", "exempt_roles": ["steward"]},
            )
            assert r_col.status_code == 200 and r_col.json()["masking_strategy"] == "hash"

            r_bad = client.post(
                "/api/assets/a1/rls-policies",
                json={"name": "bad", "scope_type": "all", "predicate_sql": "pg_sleep(1) = 0"},
            )
            assert r_bad.status_code == 422

            r_ok = client.post(
                "/api/assets/a1/rls-policies",
                json={
                    "name": "east_only",
                    "scope_type": "role",
                    "scope_value": "analyst",
                    "predicate_sql": "region = :current_username",
                },
            )
            assert r_ok.status_code == 201
            pid = r_ok.json()["id"]

            r_patch = client.patch(f"/api/assets/a1/rls-policies/{pid}", json={"enabled": False})
            assert r_patch.status_code == 200 and r_patch.json()["enabled"] is False

            r_list = client.get("/api/assets/a1/security-policies")
            assert len(r_list.json()["column_policies"]) == 1 and len(r_list.json()["rls_policies"]) == 1

            assert client.delete(f"/api/assets/a1/rls-policies/{pid}").status_code == 204
            assert client.delete("/api/assets/a1/column-policies/phone").status_code == 204
    finally:
        session.close()
        engine.dispose()


def test_explore_tools_pii_dataset_and_view_masking(monkeypatch):
    engine, session = _sqlite_session()
    try:
        s_cfg = config.build_settings({"AVELIS_SECRET": "m4b-explore-pii-secret-0123456"})
        monkeypatch.setattr(data_policy, "get_settings", lambda: s_cfg)

        steward = User(id="u_s", username="steward", display_name="Steward", password_hash="x", is_active=True)
        analyst = User(id="u_a", username="analyst", display_name="Analyst", password_hash="x", is_active=True)
        ds = Datasource(
            id="ds1",
            name="pg",
            kind="postgres",
            host="h",
            port=5432,
            options={"database": "app", "schemas": ["shop"]},
            allow_exploration=True,
        )
        session.add_all(
            [steward, analyst, UserRole(user_id="u_s", role="steward"), UserRole(user_id="u_a", role="analyst"), ds]
        )
        session.commit()

        bound_tbl = BoundTable(
            asset_id="a1",
            asset_version_id="av1",
            physical_id="101",
            catalog="app",
            db="shop",
            name="customers",
            kind="table",
            columns=(BoundColumn("phone", "varchar"), BoundColumn("cnt", "bigint")),
            pii_columns=(("phone", "pii", "mask", ("steward",)),),
        )
        validated = Validated(
            sql=(
                'SELECT "customers"."phone", COUNT(*) AS "cnt" '
                'FROM "app"."shop"."customers" GROUP BY "customers"."phone" LIMIT 10'
            ),
            tables=[bound_tbl],
            columns=[
                OutputColumn("phone", "phone", ["app.shop.customers.phone"], "text", False, True, False),
                OutputColumn("cnt", "COUNT(*)", ["app.shop.customers.*"], "count", True, False, False),
            ],
            limit=10,
            dialect="postgres",
            profile="postgres/2",
        )
        source = exploration.ExploreSource(ds, profile_for("postgres"), "app")

        class FakeConn:
            def execute_explore(self, *_a, **_kw):
                return QueryResult(columns=["phone", "cnt"], rows=[["13812345678", 3]], truncated=False)

        monkeypatch.setattr(exploration, "route", lambda *a, **kw: source)
        monkeypatch.setattr(exploration, "validate", lambda *a, **kw: validated)
        monkeypatch.setattr(exploration, "live_check", lambda *a, **kw: None)
        monkeypatch.setattr(exploration, "recheck_access", lambda *a, **kw: None)
        monkeypatch.setattr(exploration, "record", lambda *a, **kw: None)
        monkeypatch.setattr("avelis.engines.query_connector", lambda *a, **kw: FakeConn())

        # 非豁免用户：数据集与大模型视图均脱敏
        tb_analyst = AnalysisToolbox(
            session, analyst, SimpleNamespace(id="run-1"), ask_service.Catalog([], {}), Budget()
        )
        monkeypatch.setattr(tb_analyst, "_authorize", lambda: None)
        monkeypatch.setattr(
            tb_analyst,
            "_dataset",
            lambda kind, title, cols, rows, **kw: SimpleNamespace(
                seq=1,
                kind=kind,
                title=title,
                columns=cols,
                rows=rows,
                truncated=False,
                meta=kw.get("meta") or {},
                inputs=[],
            ),
        )
        view_a, ds_a, st_a = tb_analyst._explore(
            "SELECT phone, COUNT(*) AS cnt FROM shop.customers GROUP BY phone", "p", title="t"
        )
        assert st_a == "ok" and ds_a.rows == [["138****5678", 3]] and view_a["rows"] == [["138****5678", 3]]

        # 豁免角色：数据集明文，但给 LLM 的 _view 仍强制脱敏
        tb_steward = AnalysisToolbox(
            session, steward, SimpleNamespace(id="run-2"), ask_service.Catalog([], {}), Budget()
        )
        monkeypatch.setattr(tb_steward, "_authorize", lambda: None)
        monkeypatch.setattr(
            tb_steward,
            "_dataset",
            lambda kind, title, cols, rows, **kw: SimpleNamespace(
                seq=1,
                kind=kind,
                title=title,
                columns=cols,
                rows=rows,
                truncated=False,
                meta=kw.get("meta") or {},
                inputs=[],
            ),
        )
        view_s, ds_s, st_s = tb_steward._explore(
            "SELECT phone, COUNT(*) AS cnt FROM shop.customers GROUP BY phone", "p", title="t"
        )
        assert st_s == "ok" and ds_s.rows == [["13812345678", 3]] and view_s["rows"] == [["138****5678", 3]]
    finally:
        session.close()
        engine.dispose()


def test_rls_signature_isolates_parameterized_predicates_per_user():
    u_a1 = SimpleNamespace(id="u_a1", username="alice")
    u_a2 = SimpleNamespace(id="u_a2", username="bob")

    sig_uid_1 = data_policy.rls_signature({"a1": ["owner_id = :current_user_id"]}, user=u_a1)
    sig_uid_2 = data_policy.rls_signature({"a1": ["owner_id = :current_user_id"]}, user=u_a2)
    assert sig_uid_1 != sig_uid_2

    sig_uname_1 = data_policy.rls_signature({"a1": ["owner_name = :current_username"]}, user=u_a1)
    sig_uname_2 = data_policy.rls_signature({"a1": ["owner_name = :current_username"]}, user=u_a2)
    assert sig_uname_1 != sig_uname_2

    sig_static_1 = data_policy.rls_signature({"a1": ["region = 'east'"]}, user=u_a1)
    sig_static_2 = data_policy.rls_signature({"a1": ["region = 'east'"]}, user=u_a2)
    assert sig_static_1 == sig_static_2


def test_active_rls_predicates_user_scope_case_insensitive():
    engine, session = _sqlite_session()
    try:
        u_alice = User(id="u_alice", username="alice", display_name="Alice", password_hash="x", is_active=True)
        u_bob = User(id="u_bob", username="bob", display_name="Bob", password_hash="x", is_active=True)
        pol = AssetRlsPolicy(
            id="rp_user",
            asset_id="a1",
            name="alice_only",
            scope_type="user",
            scope_value="ALICE",
            predicate_sql="region = 'east'",
            enabled=True,
        )
        session.add_all([u_alice, u_bob, pol])
        session.commit()

        assert data_policy.active_rls_predicates_for_user(session, u_alice, "a1") == ["region = 'east'"]
        assert data_policy.active_rls_predicates_for_user(session, u_bob, "a1") == []
    finally:
        session.close()
        engine.dispose()
