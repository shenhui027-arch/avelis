"""Ask 模式评测套件（M3D Part B）：在隔离的 SQLite 元数据库与模拟连接器上端到端运行 ask_questions.yaml。"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis import engines, modelcfg
from avelis.ask import agent as ask_agent
from avelis.ask import service as ask_service
from avelis.connectors.base import QueryResult
from avelis.models import (
    Answer,
    Asset,
    AssetColumn,
    AssetVersion,
    Base,
    CollectionRun,
    ColumnVersion,
    Datasource,
    Domain,
    DomainMember,
    JoinPath,
    JoinPathApproval,
    Metric,
    MetricVersion,
    User,
    UserRole,
)
from tests.eval.ask_scoring import load_ask_questions, score_ask_case, summarize_ask


def _uid() -> str:
    return uuid.uuid4().hex[:16]


def _seed_ask_db(session: Session) -> dict[str, Any]:
    wang = User(id="u_wang", username="wang", display_name="王强", password_hash="x", is_active=True)
    sun = User(id="u_sun", username="sun", display_name="孙悦", password_hash="x", is_active=True)
    other_domain = Domain(id="dom_other", code="other", name="其他域", owner_id=sun.id)
    ec_domain = Domain(id="dom_ec", code="ec", name="电商经营", owner_id=wang.id)
    session.add_all(
        [
            wang,
            sun,
            other_domain,
            ec_domain,
            UserRole(id=_uid(), user_id=wang.id, role="analyst"),
            UserRole(id=_uid(), user_id=sun.id, role="analyst"),
            DomainMember(id=_uid(), domain_id=ec_domain.id, user_id=wang.id, role="viewer"),
            DomainMember(id=_uid(), domain_id=other_domain.id, user_id=sun.id, role="viewer"),
        ]
    )
    ds = Datasource(
        id="ds_ec",
        name="电商数仓",
        kind="starrocks",
        host="127.0.0.1",
        port=9030,
        options={},
        status="active",
        allow_exploration=True,
    )
    crun = CollectionRun(id="crun_1", datasource_id=ds.id, trigger="manual", status="succeeded")
    session.add_all([ds, crun])

    def make_table(aid: str, name: str, cols: list[tuple[str, str, str]], keys: list[str] | None = None):
        asset = Asset(
            id=aid,
            datasource_id=ds.id,
            kind="table",
            catalog_name="default_catalog",
            db_name="ec",
            name=name,
            physical_id=aid,
            status="active",
            domain_id=ec_domain.id,
            current_version_id=f"av_{aid}",
        )
        av = AssetVersion(
            id=f"av_{aid}",
            asset_id=aid,
            version_no=1,
            observed_run_id=crun.id,
            last_seen_run_id=crun.id,
            kind="table",
            catalog_name="default_catalog",
            db_name="ec",
            name=name,
            table_model="DUPLICATE_KEYS",
            key_columns=keys or [],
            unique_keys=[],
            content_hash=aid,
        )
        session.add_all([asset, av])
        for idx, (cname, dtype, comment) in enumerate(cols, 1):
            col = AssetColumn(id=f"ac_{aid}_{cname}", asset_id=aid, name=cname, status="active")
            cv = ColumnVersion(
                id=f"cv_{aid}_{cname}",
                asset_version_id=av.id,
                column_id=col.id,
                ordinal=idx,
                name=cname,
                data_type=dtype,
                nullable=False,
                comment=comment,
            )
            session.add_all([col, cv])
        return asset, av

    a_orders, av_orders = make_table(
        "a_orders",
        "v_orders_daily",
        [
            ("dt", "date", "日期"),
            ("region", "varchar(32)", "区域"),
            ("channel", "varchar(32)", "渠道"),
            ("paid_amount", "decimal(18,2)", "成交额"),
            ("orders", "bigint", "订单数"),
        ],
    )
    a_cat, av_cat = make_table(
        "a_cat",
        "v_category_daily",
        [
            ("dt", "date", "日期"),
            ("category", "varchar(32)", "类目"),
            ("units", "bigint", "销量"),
            ("sales", "decimal(18,2)", "销售额"),
        ],
    )
    a_store, av_store = make_table(
        "a_store",
        "v_store_daily",
        [
            ("dt", "date", "日期"),
            ("region", "varchar(32)", "区域"),
            ("store_id", "varchar(32)", "门店编码"),
            ("store", "varchar(64)", "门店"),
            ("paid_amount", "decimal(18,2)", "门店成交额"),
            ("visitors", "bigint", "进店客流"),
        ],
    )
    a_dim_store, av_dim_store = make_table(
        "a_dim_store",
        "dim_store",
        [
            ("store_id", "varchar(32)", "门店编码"),
            ("store_name", "varchar(64)", "门店名称"),
            ("alias", "varchar(64)", "门店别名"),
        ],
        keys=["store_id"],
    )
    a_dim_region, av_dim_region = make_table(
        "a_dim_region",
        "dim_region",
        [
            ("region_name", "varchar(32)", "区域名"),
            ("alias", "varchar(32)", "区域别名"),
        ],
        keys=["region_name"],
    )
    jp_store = JoinPath(
        id="jp_store",
        code="jp_store",
        name="门店维表关联",
        left_asset_id=a_store.id,
        left_columns=["store"],
        right_asset_id=a_dim_store.id,
        right_columns=["store_name"],
        cardinality="n1",
        status="approved",
        created_by=wang.id,
    )
    jp_region = JoinPath(
        id="jp_region",
        code="jp_region",
        name="区域维表关联",
        left_asset_id=a_orders.id,
        left_columns=["region"],
        right_asset_id=a_dim_region.id,
        right_columns=["region_name"],
        cardinality="n1",
        status="approved",
        created_by=wang.id,
    )
    session.add_all(
        [
            jp_store,
            jp_region,
            JoinPathApproval(
                id="jpa_store",
                join_path_id=jp_store.id,
                domain_id=ec_domain.id,
                user_id=wang.id,
                decision="approved",
            ),
            JoinPathApproval(
                id="jpa_region",
                join_path_id=jp_region.id,
                domain_id=ec_domain.id,
                user_id=wang.id,
                decision="approved",
            ),
        ]
    )

    def add_metric(
        mid: str,
        code: str,
        name: str,
        asset: Asset,
        av: AssetVersion,
        expr: str,
        dims: list[dict],
        *,
        aliases: list[str] | None = None,
        synonyms: list[str] | None = None,
        status: str = "published",
        kind: str = "base",
        components: list[dict] | None = None,
        joins: list[dict] | None = None,
        unit: str | None = None,
        value_kind: str = "level",
    ):
        m = Metric(
            id=mid,
            code=code,
            name=name,
            aliases=aliases or [],
            ai_context={"synonyms": synonyms or []},
            domain_id=ec_domain.id,
            owner_id=wang.id,
            created_by=wang.id,
        )
        mv = MetricVersion(
            id=f"mv_{code}",
            metric_id=mid,
            version_no=1,
            status=status,
            description=name,
            kind=kind,
            base_asset_id=asset.id if kind == "base" else None,
            expression=expr,
            components=components or [],
            time_column="dt",
            timezone="Asia/Shanghai",
            data_grain="day",
            time_aggregation="sum",
            value_kind=value_kind,
            dimensions=dims,
            joins=joins or [],
            grain=["dt"],
            unit=unit,
            created_by=wang.id,
        )
        session.add_all([m, mv])
        return m, mv

    m_gmv, mv_gmv = add_metric(
        "m_gmv",
        "ec_gmv",
        "成交额",
        a_orders,
        av_orders,
        "SUM(paid_amount)",
        [
            {"name": "region", "label": "区域", "column": "region"},
            {"name": "channel", "label": "渠道", "column": "channel"},
        ],
        aliases=["GMV", "实付金额"],
        synonyms=["销售额"],
        unit="元",
    )
    m_orders, mv_orders = add_metric(
        "m_orders",
        "ec_orders",
        "订单数",
        a_orders,
        av_orders,
        "SUM(orders)",
        [
            {"name": "region", "label": "区域", "column": "region"},
            {"name": "channel", "label": "渠道", "column": "channel"},
        ],
        synonyms=["单量"],
        unit="单",
        value_kind="count",
    )
    add_metric(
        "m_units",
        "ec_units",
        "销量",
        a_cat,
        av_cat,
        "SUM(units)",
        [{"name": "category", "label": "类目", "column": "category"}],
        synonyms=["销售件数"],
        unit="件",
        value_kind="count",
    )
    add_metric(
        "m_aov",
        "ec_aov",
        "客单价",
        a_orders,
        av_orders,
        "ec_gmv / ec_orders",
        [
            {"name": "region", "label": "区域", "column": "region"},
            {"name": "channel", "label": "渠道", "column": "channel"},
        ],
        synonyms=["平均订单金额"],
        kind="derived",
        components=[
            {"metric_id": m_gmv.id, "metric_code": "ec_gmv", "metric_name": "成交额", "version_id": mv_gmv.id},
            {"metric_id": m_orders.id, "metric_code": "ec_orders", "metric_name": "订单数", "version_id": mv_orders.id},
        ],
        unit="元",
        value_kind="ratio",
    )
    add_metric(
        "m_store_sales",
        "ec_store_sales",
        "门店成交额",
        a_store,
        av_store,
        "SUM(paid_amount)",
        [
            {"name": "region", "label": "区域", "column": "region"},
            {"name": "store", "label": "门店", "column": "store"},
        ],
        unit="元",
    )
    add_metric(
        "m_visitors",
        "ec_visitors",
        "进店客流",
        a_store,
        av_store,
        "SUM(visitors)",
        [
            {"name": "region", "label": "区域", "column": "region"},
            {"name": "store", "label": "门店", "column": "store"},
        ],
        unit="人",
        value_kind="count",
    )
    add_metric(
        "m_retired",
        "ec_retired",
        "已停用指标",
        a_orders,
        av_orders,
        "SUM(orders)",
        [{"name": "region", "label": "区域", "column": "region"}],
        status="affected",
    )
    session.commit()
    return {"wang": wang, "sun": sun, "ds": ds}


class _AskFakeConnector:
    def __init__(
        self,
        *,
        values: dict[tuple[str, str], list[str]] | None = None,
        alias_rows: dict[str, list[list[Any]]] | None = None,
    ):
        self.values = values or {
            ("v_orders_daily", "region"): ["华东", "华南", "华北", "西南"],
            ("v_orders_daily", "channel"): ["线上", "门店"],
            ("v_store_daily", "store"): ["深圳福田中心店", "北京一号店", "北京二号店", "成都太古里店"],
        }
        self.alias_rows = alias_rows or {}
        self.executed_sql: list[str] = []

    def describe_objects(self, objects, *, timeout_seconds=None):
        out = {}
        for db, name in objects:
            cols = {
                "v_orders_daily": [
                    ["dt", "date"],
                    ["region", "varchar(32)"],
                    ["channel", "varchar(32)"],
                    ["paid_amount", "decimal(18,2)"],
                    ["orders", "bigint"],
                ],
                "v_category_daily": [
                    ["dt", "date"],
                    ["category", "varchar(32)"],
                    ["units", "bigint"],
                    ["sales", "decimal(18,2)"],
                ],
                "v_store_daily": [
                    ["dt", "date"],
                    ["region", "varchar(32)"],
                    ["store_id", "varchar(32)"],
                    ["store", "varchar(64)"],
                    ["paid_amount", "decimal(18,2)"],
                    ["visitors", "bigint"],
                ],
                "dim_store": [["store_id", "varchar(32)"], ["store_name", "varchar(64)"], ["alias", "varchar(64)"]],
                "dim_region": [["region_name", "varchar(32)"], ["alias", "varchar(32)"]],
            }.get(name, [])
            pid = {
                "v_orders_daily": "a_orders",
                "v_category_daily": "a_cat",
                "v_store_daily": "a_store",
                "dim_store": "a_dim_store",
                "dim_region": "a_dim_region",
            }.get(name)
            out[(db, name)] = {"columns": cols, "definition": None, "physical_id": pid}
        return out

    def freshness(self, tables):
        return {f"{d}.{n}": "2026-09-30 23:59:59" for d, n in tables}

    def execute_select(
        self, sql_text: str, *, timeout_seconds: int, mem_limit_bytes: int, max_rows: int, timezone: str | None = None
    ):
        self.executed_sql.append(sql_text)
        upper = sql_text.upper()
        lim_match = re.search(r"\bLIMIT\s+(\d+)\b", upper)
        row_cap = min(max_rows, int(lim_match.group(1))) if lim_match else max_rows

        if "dim_store" in sql_text or "dim_region" in sql_text:
            case_n = upper.count("CASE WHEN")
            if case_n:
                return QueryResult(columns=[f"c{i}" for i in range(case_n)], rows=[[1] * case_n], truncated=False)
            for key, rows in self.alias_rows.items():
                if f"'{key}'" in sql_text or key in sql_text:
                    canons = list(dict.fromkeys(r[0] for r in rows))
                    return QueryResult(columns=["canonical"], rows=[[c] for c in canons[:row_cap]], truncated=False)
            return QueryResult(columns=["canonical"], rows=[], truncated=False)
        # Dimension-value sampling or exact-match probes from ask/service & alignment
        if "DISTINCT" in upper:
            for (tbl, col), vals in self.values.items():
                if tbl in sql_text and col in sql_text:
                    if "= " in sql_text:
                        hit = [v for v in vals if f"'{v}'" in sql_text]
                        return QueryResult(columns=[col], rows=[[v] for v in hit[:row_cap]], truncated=False)
                    return QueryResult(columns=[col], rows=[[v] for v in vals[:row_cap]], truncated=len(vals) > row_cap)
        # Metric query results: _fulfil executes one SQL per (metric, period) returning [...group_keys, value]
        if "v_store_daily" in sql_text and "paid_amount" in sql_text:
            if "GROUP BY" in upper:
                rows = [["深圳福田中心店", 500], ["成都太古里店", 420], ["北京一号店", 310], ["北京二号店", 290]]
                return QueryResult(columns=["store", "value"], rows=rows[:row_cap], truncated=False)
            return QueryResult(columns=["value"], rows=[[1520]], truncated=False)
        if "v_store_daily" in sql_text and "visitors" in sql_text:
            if "GROUP BY" in upper:
                rows = [["北京二号店", 120], ["北京一号店", 150], ["成都太古里店", 320], ["深圳福田中心店", 540]]
                return QueryResult(columns=["store", "value"], rows=rows[:row_cap], truncated=False)
            return QueryResult(columns=["value"], rows=[[1130]], truncated=False)
        if "v_orders_daily" in sql_text and "paid_amount" in sql_text:
            if "GROUP BY" in upper:
                return QueryResult(
                    columns=["region", "value"], rows=[["华东", 120000], ["华南", 90000]][:row_cap], truncated=False
                )
            return QueryResult(columns=["value"], rows=[[210000]], truncated=False)
        if "v_orders_daily" in sql_text and "orders" in sql_text:
            is_prior = "2026-08" in sql_text or "2025-" in sql_text
            if "GROUP BY" in upper:
                rows = [["华东", 1400], ["华南", 1200]] if is_prior else [["华东", 1500], ["华南", 1100]]
                return QueryResult(columns=["region", "value"], rows=rows[:row_cap], truncated=False)
            return QueryResult(columns=["value"], rows=[[2600]], truncated=False)
        return QueryResult(columns=["value"], rows=[[100]], truncated=False)


def test_ask_eval_suite(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    spec = load_ask_questions()
    results = []

    with Session(engine) as session:
        seeded = _seed_ask_db(session)
        for q in spec["questions"]:
            user = seeded["sun"] if q["user"] == "sun" else seeded["wang"]
            cat = q["category"]

            if cat == "retrieval":
                catalog = ask_service.visible_catalog(session, user, with_values=False)
                box = ask_agent.Toolbox(session, user, catalog)
                ranked = json.loads(box.search_metrics(q["question"]))
                outcome = {"ranked_codes": [item["code"] for item in ranked]}

            elif cat == "alignment":
                alias_rows: dict[str, list[list[Any]]] = {}
                for canon, aliases in (q.get("dim_table_aliases") or {}).items():
                    for al in aliases:
                        alias_rows.setdefault(al, []).append([canon, al])
                raw_values = dict(
                    q.get("raw_values_override")
                    or {
                        "region": ["华东", "华南", "华北", "西南"],
                        "store": ["深圳福田中心店", "北京一号店", "北京二号店", "成都太古里店"],
                    }
                )
                if lt := q.get("long_tail_count"):
                    raw_values["store"] = [f"门店_{i:03d}" for i in range(1, int(lt) + 1)]
                conn = _AskFakeConnector(
                    values={
                        ("v_orders_daily", "region"): raw_values.get("region", []),
                        ("v_store_daily", "store"): raw_values.get("store", []),
                    },
                    alias_rows=alias_rows,
                )
                monkeypatch.setattr(engines, "query_connector", lambda s, ds, c=conn: c)
                monkeypatch.setattr(ask_service, "query_connector", lambda s, ds, c=conn: c)

                catalog = ask_service.visible_catalog(session, user, with_values=False)
                box = ask_agent.Toolbox(session, user, catalog)

                def fake_dim_values(s, u, v, settings, rv=raw_values, **kw):
                    return {d["name"]: rv.get(d["name"], [])[:200] for d in v.dimensions or []}

                monkeypatch.setattr(ask_service, "_dimension_values", fake_dim_values)
                monkeypatch.setattr(
                    ask_service,
                    "_existing_dimension_values",
                    lambda s, u, v, dim, vals, settings, rv=raw_values, **kw: {x for x in vals if x in rv.get(dim, [])},
                )

                if q.get("resume_selection"):
                    # Run through real ask graph clarify -> Command(resume=...)
                    class _AlignModel:
                        def __init__(self):
                            self.step = 0

                        def bind_tools(self, tools, **kw):
                            return self

                        def invoke(self, messages):
                            self.step += 1
                            if self.step == 1:
                                return AIMessage(
                                    content="",
                                    tool_calls=[
                                        {
                                            "id": "si1",
                                            "name": "submit_intent",
                                            "args": {
                                                "metric_codes": q["metric_codes"],
                                                "start_date": q["window"]["start"],
                                                "end_date": q["window"]["end"],
                                                "filters": q["intent_filters"],
                                            },
                                        }
                                    ],
                                )
                            if self.step == 2:
                                return AIMessage(
                                    content="",
                                    tool_calls=[
                                        {
                                            "id": "au1",
                                            "name": "ask_user",
                                            "args": {
                                                "question": "请选择门店",
                                                "options": sorted({k for k in q["dim_table_aliases"]}),
                                            },
                                        }
                                    ],
                                )
                            return AIMessage(
                                content="",
                                tool_calls=[
                                    {
                                        "id": "si2",
                                        "name": "submit_intent",
                                        "args": {
                                            "metric_codes": q["metric_codes"],
                                            "start_date": q["window"]["start"],
                                            "end_date": q["window"]["end"],
                                            "filters": [{"dimension": "store", "values": [q["resume_selection"]]}],
                                        },
                                    }
                                ],
                            )

                    align_model = _AlignModel()
                    monkeypatch.setattr(
                        modelcfg,
                        "resolve",
                        lambda s, purpose, m=align_model, **kw: SimpleNamespace(
                            model=m, adapter="fake", context_window=None
                        ),
                    )
                    monkeypatch.setattr(
                        modelcfg,
                        "resolve_routed",
                        lambda s, purpose, m=align_model, **kw: SimpleNamespace(
                            model=m, adapter="fake", context_window=None
                        ),
                    )
                    monkeypatch.setattr(modelcfg, "policy", lambda s: {"send_results": False})
                    graph = ask_agent.build_graph(MemorySaver())
                    ans = ask_service.start_answer(session, user, q["question"], "fake", None)
                    cfg = {
                        "configurable": {"thread_id": f"ask-{q['id']}", "session": session, "user": user, "answer": ans}
                    }
                    first = graph.invoke(
                        {
                            "turns": [q["question"]],
                            "messages": [HumanMessage(q["question"])],
                            "intent": None,
                            "clarification": None,
                            "alignment_clarifications": [],
                            "semantic_cache_hit": None,
                            "context": {"today": "2026-09-30", "tz": "Asia/Shanghai"},
                        },
                        cfg,
                    )
                    interrupt_payload = first["__interrupt__"][0].value
                    resumed = graph.invoke(Command(resume=q["resume_selection"]), cfg)
                    intent_out = resumed.get("intent") or {}
                    outcome = {
                        "status": "clarify_then_resolved" if ans.status == "answered" else ans.status,
                        "clarify_options": interrupt_payload.get("options") or [],
                        "aligned_filters": {f["dimension"]: f["values"] for f in intent_out.get("filters") or []},
                        "alignment_evidence": True,
                    }
                else:
                    res = json.loads(
                        box.submit_intent(
                            metric_codes=q["metric_codes"],
                            start_date=date.fromisoformat(q["window"]["start"]),
                            end_date=date.fromisoformat(q["window"]["end"]),
                            filters=q["intent_filters"],
                        )
                    )
                    outcome = {
                        "status": "accepted" if res.get("accepted") else "rejected",
                        "aligned_filters": {f.dimension: f.values for f in (box.intent.filters if box.intent else [])},
                        "alignment_evidence": box.filter_alignment,
                    }

            elif cat == "multi_order":
                conn = _AskFakeConnector()
                monkeypatch.setattr(engines, "query_connector", lambda s, ds, c=conn: c)
                monkeypatch.setattr(ask_service, "query_connector", lambda s, ds, c=conn: c)
                catalog = ask_service.visible_catalog(session, user, with_values=False)
                box = ask_agent.Toolbox(session, user, catalog)
                if q.get("invalid_combos"):
                    rejected_all = True
                    for combo in q["invalid_combos"]:
                        try:
                            res = json.loads(
                                box.submit_intent(
                                    metric_codes=combo["metric_codes"],
                                    start_date=date.fromisoformat(q["window"]["start"]),
                                    end_date=date.fromisoformat(q["window"]["end"]),
                                    group_by=combo.get("group_by"),
                                    order=combo.get("order"),
                                    compare=combo.get("compare"),
                                    time_grain=combo.get("time_grain"),
                                )
                            )
                            if res.get("accepted"):
                                rejected_all = False
                        except ValueError:
                            pass
                    outcome = {"status": "rejected_all" if rejected_all else "accepted_unexpected"}
                else:
                    res = json.loads(
                        box.submit_intent(
                            metric_codes=q["metric_codes"],
                            start_date=date.fromisoformat(q["window"]["start"]),
                            end_date=date.fromisoformat(q["window"]["end"]),
                            group_by=q.get("group_by"),
                            order=q.get("order"),
                            limit=q.get("limit"),
                            compare=q.get("compare"),
                        )
                    )
                    assert res.get("accepted") is True and box.intent is not None
                    ans = ask_service.start_answer(session, user, q["question"], "fake", None)
                    ask_service.fulfil(session, user, box.intent, ans, catalog)
                    outcome = {
                        "status": ans.status,
                        "metric_codes": list(box.intent.metric_codes),
                        "group_by": list(box.intent.group_by),
                        "order": box.intent.order,
                        "limit": box.intent.limit,
                        "compare": box.intent.compare,
                        "rows": (ans.result or {}).get("rows") or [],
                    }

            elif cat == "negative":
                conn = _AskFakeConnector()
                monkeypatch.setattr(engines, "query_connector", lambda s, ds, c=conn: c)
                monkeypatch.setattr(ask_service, "query_connector", lambda s, ds, c=conn: c)
                if q.get("retired_metric"):
                    catalog = ask_service.visible_catalog(session, user, with_values=False)
                    box = ask_agent.Toolbox(session, user, catalog)
                    box.submit_intent(
                        metric_codes=["ec_retired"],
                        start_date=date.fromisoformat(q["window"]["start"]),
                        end_date=date.fromisoformat(q["window"]["end"]),
                    )
                    ans = ask_service.start_answer(session, user, q["question"], "fake", None)
                    ask_service.fulfil(session, user, box.intent, ans, catalog)
                    outcome = {"status": ans.status, "sql": ans.sql, "message": ans.message}
                elif q.get("direct_intent"):
                    catalog = ask_service.visible_catalog(session, user, with_values=False)
                    intent = ask_service.Intent(
                        metric_codes=q["direct_intent"]["metric_codes"],
                        start_date=date.fromisoformat(q["window"]["start"]),
                        end_date=date.fromisoformat(q["window"]["end"]),
                    )
                    ans = ask_service.start_answer(session, user, q["question"], "fake", None)
                    ask_service.fulfil(session, user, intent, ans, catalog)
                    outcome = {"status": ans.status, "sql": ans.sql, "message": ans.message}
                else:

                    class _RefuseModel:
                        def bind_tools(self, tools, **kw):
                            return self

                        def invoke(self, messages):
                            return AIMessage(
                                content="",
                                tool_calls=[
                                    {
                                        "id": "au_neg",
                                        "name": "ask_user",
                                        "args": {"question": "没有对应已发布口径，请确认您要查询的业务指标。"},
                                    }
                                ],
                            )

                    monkeypatch.setattr(
                        modelcfg,
                        "resolve",
                        lambda s, purpose, **kw: SimpleNamespace(
                            model=_RefuseModel(), adapter="fake", context_window=None
                        ),
                    )
                    monkeypatch.setattr(
                        modelcfg,
                        "resolve_routed",
                        lambda s, purpose, **kw: SimpleNamespace(
                            model=_RefuseModel(), adapter="fake", context_window=None
                        ),
                    )
                    monkeypatch.setattr(modelcfg, "policy", lambda s: {"send_results": False})
                    graph = ask_agent.build_graph(MemorySaver())
                    ans = ask_service.start_answer(session, user, q["question"], "fake", None)
                    cfg = {
                        "configurable": {"thread_id": f"ask-{q['id']}", "session": session, "user": user, "answer": ans}
                    }
                    res = graph.invoke(
                        {
                            "turns": [q["question"]],
                            "messages": [HumanMessage(q["question"])],
                            "intent": None,
                            "clarification": None,
                            "alignment_clarifications": [],
                            "semantic_cache_hit": None,
                            "context": {"today": "2026-09-30", "tz": "Asia/Shanghai"},
                        },
                        cfg,
                    )
                    outcome = {
                        "status": "clarify" if "__interrupt__" in res else res.get("status"),
                        "sql": ans.sql,
                        "message": res["__interrupt__"][0].value["question"] if "__interrupt__" in res else ans.message,
                    }

            elif cat == "grounding":
                send_results = q.get("send_results", True)
                monkeypatch.setattr(modelcfg, "policy", lambda s, sr=send_results: {"send_results": sr})
                monkeypatch.setattr(modelcfg, "routing_config", lambda s: {"enabled": False})
                replies = iter([AIMessage(content=t) for t in q.get("model_replies") or []])
                calls = {"n": 0}

                def fake_invoke(resolved, messages, tools=None):
                    calls["n"] += 1
                    return next(replies)

                monkeypatch.setattr(
                    modelcfg,
                    "resolve",
                    lambda s, purpose, **kw: SimpleNamespace(model=object(), adapter="fake", context_window=None),
                )
                ans = Answer(
                    id=_uid(),
                    asked_by=user.id,
                    question=q["question"],
                    status="answered",
                    message="模板说明：查询已完成。",
                    result=q["result"],
                    evidence={},
                )
                ask_agent.explain_answer(session, user, ans, {}, invoke=fake_invoke)
                g = (ans.evidence or {}).get("explanation_grounding") or {}
                outcome = {
                    "explanation_source": ans.explanation_source,
                    "explanation": ans.explanation,
                    "grounding_ok": g.get("ok"),
                    "ungrounded_count": len(g.get("ungrounded") or []),
                    "fallback": g.get("fallback"),
                    "model_calls": calls["n"],
                }

            results.append(score_ask_case(q, outcome))

    summary = summarize_ask(results)
    assert summary["overall"]["hits"] == summary["overall"]["runs"], [r for r in results if not r["passed"]]
    assert summary["overall"]["rate"] == 1.0
    assert set(summary["by_category"]) == {"retrieval", "alignment", "multi_order", "negative", "grounding"}
