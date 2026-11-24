"""M3A 混合检索测试：词法 TF-IDF、向量缓存与降级、配置键以及 Ask/Analysis 集成。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from redis.exceptions import RedisError

from avelis import config, configfile, retrieval_hybrid
from avelis.analysis import retrieval as analysis_retrieval
from avelis.analysis.tools import AnalysisToolbox, Budget
from avelis.ask import agent as ask_agent
from avelis.ask import service as ask_service
from avelis.ask.intent import MetricCard
from avelis.models import Asset, AssetVersion, Domain, Metric, MetricVersion
from avelis.retrieval_hybrid import SearchDocument, embed_texts, hybrid_rank, tfidf_scores, tokenize

SECRET = "offline-unit-test-secret-0123456789"


@pytest.fixture(autouse=True)
def _clean_emb_cache():
    retrieval_hybrid.clear_embedding_cache()
    yield
    retrieval_hybrid.clear_embedding_cache()


def test_tokenize_and_tfidf_scores_rank_discriminative_terms_offline():
    tokens = tokenize("order_gmv 客单价")
    assert "order_gmv" in tokens and "order" in tokens and "gmv" in tokens
    assert "客单" in tokens and "单价" in tokens and "客" in tokens

    docs = [
        SearchDocument(key="a", fields=[(3.0, "全站总销售额"), (1.0, "total_sales")]),
        SearchDocument(key="b", fields=[(3.0, "客单价"), (1.0, "avg_order_value 订单均价")]),
        SearchDocument(key="c", fields=[(3.0, "活跃用户数"), (1.0, "dau")]),
    ]
    scores = tfidf_scores("客单价变化", docs)
    assert scores["b"] > scores["a"] >= 0.0 and scores["c"] == 0.0
    ranked = hybrid_rank("客单价", docs)
    assert ranked[0][0] == "b"


def test_embed_texts_caches_in_redis_and_lru_and_falls_back_on_errors(monkeypatch):
    settings = config.build_settings(
        {
            "AVELIS_SECRET": SECRET,
            "AVELIS_EMBEDDING_BASE_URL": "https://emb.example/v1",
            "AVELIS_EMBEDDING_API_KEY": "secret-emb-key",
            "AVELIS_EMBEDDING_MODEL": "text-embedding-3-small",
            "AVELIS_EMBEDDING_TIMEOUT_SECONDS": "3",
        }
    )
    assert "AVELIS_EMBEDDING_API_KEY" in configfile.SENSITIVE_KEYS

    store: dict[str, str] = {}
    calls: list[dict] = []

    class FakePipe:
        def set(self, k, v, ex=None):
            store[k] = v

        def execute(self):
            return [True]

    class FakeRedis:
        def mget(self, keys):
            return [store.get(k) for k in keys]

        def pipeline(self):
            return FakePipe()

    monkeypatch.setattr(retrieval_hybrid.cache, "client", lambda: FakeRedis())

    def fake_post(url, *, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        vecs = [[1.0, 0.0] if "成交" in text else [0.0, 1.0] for text in json["input"]]
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": [{"index": i, "embedding": v} for i, v in enumerate(vecs)]},
        )

    monkeypatch.setattr(retrieval_hybrid.httpx, "post", fake_post)

    docs = [
        SearchDocument(key="gmv", fields=[(3.0, "实付成交额")]),
        SearchDocument(key="dau", fields=[(3.0, "日活用户")]),
    ]
    ranked = hybrid_rank("交易成交", docs, settings=settings)
    assert ranked[0][0] == "gmv" and ranked[0][1] > ranked[1][1]
    assert len(calls) == 1 and calls[0]["url"] == "https://emb.example/v1/embeddings"
    assert calls[0]["headers"]["Authorization"] == "Bearer secret-emb-key"
    assert len(store) == 3

    # 第二次命中进程内 LRU，不再发起 HTTP 请求
    ranked_again = hybrid_rank("交易成交", docs, settings=settings)
    assert ranked_again == ranked and len(calls) == 1

    # 清空 LRU 后命中 Redis
    retrieval_hybrid.clear_embedding_cache()
    assert embed_texts(["交易成交"], settings=settings) == [[1.0, 0.0]]
    assert len(calls) == 1

    # RedisError + httpx 网络异常均优雅降级为纯词法排序
    retrieval_hybrid.clear_embedding_cache()

    class BrokenRedis:
        def mget(self, keys):
            raise RedisError("redis down")

        def pipeline(self):
            raise RedisError("redis down")

    monkeypatch.setattr(retrieval_hybrid.cache, "client", lambda: BrokenRedis())
    monkeypatch.setattr(
        retrieval_hybrid.httpx,
        "post",
        lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("offline")),
    )
    fallback = hybrid_rank("日活用户", docs, settings=settings)
    assert fallback[0][0] == "dau"


def test_ask_and_analysis_metric_and_asset_search_use_hybrid_ranking(monkeypatch):
    cards = [
        MetricCard(
            code="m_gmv",
            name="实付金额",
            domain="零售",
            aliases=["成交总额"],
            ai_context={"synonyms": ["GMV"], "examples": ["上月成交总额"]},
            dimensions=[{"name": "region", "label": "区域"}],
            description="实际支付成交金额",
            status="published",
        ),
        MetricCard(
            code="m_users",
            name="新增用户",
            domain="零售",
            aliases=[],
            ai_context={"synonyms": [], "examples": []},
            dimensions=[{"name": "region", "label": "区域"}],
            description="每日新增注册用户数",
            status="published",
        ),
    ]
    versions = {
        c.code: (
            SimpleNamespace(id=c.code, code=c.code, name=c.name, domain_id="d1"),
            SimpleNamespace(kind="base", expression="SUM(x)", unit="元", dimensions=c.dimensions),
        )
        for c in cards
    }
    catalog = ask_service.Catalog(cards, versions)
    ask_box = ask_agent.Toolbox(SimpleNamespace(), SimpleNamespace(id="u1"), catalog)
    found = json.loads(ask_box.search_metrics("gmv 成交"))
    assert [item["code"] for item in found] == ["m_gmv"]

    monkeypatch.setattr(ask_service, "visible_catalog", lambda *a, **kw: catalog)
    a_box = AnalysisToolbox(SimpleNamespace(), SimpleNamespace(id="u1"), SimpleNamespace(id="r1"), catalog, Budget())
    monkeypatch.setattr(a_box, "_authorize", lambda: None)
    monkeypatch.setattr(a_box, "_step", lambda *a, **kw: None)
    analysis_metrics = json.loads(a_box.search_metrics("gmv 成交"))["metrics"]
    assert [item["code"] for item in analysis_metrics] == ["m_gmv"]

    # ask/service.search_metric_catalog 保留域可见性与平局序
    domain = Domain(id="d1", code="retail", name="零售", owner_id="u1")
    m1 = Metric(id="m1", code="m_alpha", name="成交金额", domain_id="d1", owner_id="u1", aliases=[], ai_context={})
    m2 = Metric(id="m2", code="m_beta", name="成交金额", domain_id="d1", owner_id="u1", aliases=[], ai_context={})
    v1 = MetricVersion(id="v1", metric_id="m1", version_no=1, status="published", description="订单金额")
    v2 = MetricVersion(id="v2", metric_id="m2", version_no=1, status="published", description="订单金额")

    class FakeSession:
        def execute(self, stmt):
            return [(m1, v1, domain), (m2, v2, domain)]

    monkeypatch.setattr(ask_service.access, "require_role", lambda *a: None)
    monkeypatch.setattr(ask_service.access, "domain_visibility_clause", lambda *a: True)
    rows = ask_service.search_metric_catalog(FakeSession(), SimpleNamespace(id="u1"), keyword="成交")
    assert [m.code for m, _, _ in rows] == ["m_alpha", "m_beta"]


def test_metadata_graph_find_hybrid_fallback_discovers_multi_token_matches(monkeypatch):
    a1 = Asset(
        id="a1",
        catalog_name="cat",
        db_name="dw",
        name="orders_fact",
        current_version_id="av1",
        status="active",
        kind="table",
        domain_id="d1",
        datasource_id="ds1",
    )
    a2 = Asset(
        id="a2",
        catalog_name="cat",
        db_name="dw",
        name="users_dim",
        current_version_id="av2",
        status="active",
        kind="table",
        domain_id="d1",
        datasource_id="ds1",
    )
    av1 = AssetVersion(id="av1", asset_id="a1", comment="订单支付明细表", key_columns=[], unique_keys=[])
    av2 = AssetVersion(id="av2", asset_id="a2", comment="注册用户维表", key_columns=[], unique_keys=[])

    class FakeSession:
        def get(self, cls, id_, **kwargs):
            return {"a1": a1, "a2": a2}.get(id_) if cls is Asset else {"av1": av1, "av2": av2}.get(id_)

    graph = analysis_retrieval.MetadataGraph(
        FakeSession(), SimpleNamespace(id="u1"), {"d1"}, metric_sources=lambda c: None
    )
    monkeypatch.setattr(analysis_retrieval.access, "can_read_asset", lambda *a: True)

    def fake_rows(stmt):
        # 模拟 SQL icontains 命中为空（例如多词语义查询），但全表扫描返回可见资产
        if "WHERE" in str(stmt) and "LIKE" in str(stmt).upper():
            return iter(())
        entities = tuple(c["entity"] for c in stmt.column_descriptions)
        if entities == (Asset,):
            return iter([(a1,), (a2,)])
        return iter(())

    monkeypatch.setattr(graph, "_rows", fake_rows)
    out = graph.find("orders 支付")
    assert [n["id"] for n in out["nodes"]] == ["asset:cat.dw.orders_fact"]


def test_embed_texts_malformed_json_falls_back_and_punctuation_skips_embeddings(monkeypatch):
    settings = config.build_settings(
        {
            "AVELIS_SECRET": SECRET,
            "AVELIS_EMBEDDING_BASE_URL": "https://emb.example/v1",
            "AVELIS_EMBEDDING_MODEL": "text-embedding-3-small",
        }
    )

    class EmptyRedis:
        def mget(self, keys):
            return [None] * len(keys)

        def pipeline(self):
            raise AssertionError("不应写入 Redis")

    monkeypatch.setattr(retrieval_hybrid.cache, "client", lambda: EmptyRedis())
    docs = [
        SearchDocument(key="gmv", fields=[(3.0, "实付成交额")]),
        SearchDocument(key="dau", fields=[(3.0, "日活用户")]),
    ]

    for bad_payload in (["bad"], {"data": "oops"}, {"data": ["bad", "worse", "x"]}):
        monkeypatch.setattr(
            retrieval_hybrid.httpx,
            "post",
            lambda *a, payload=bad_payload, **kw: SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: payload,
            ),
        )
        assert embed_texts(["成交额"], settings=settings) is None
        ranked = hybrid_rank("实付成交额", docs, settings=settings)
        assert ranked[0][0] == "gmv" and ranked[1][1] == 0.0

    # 纯标点查询不触发 /embeddings 请求且所有得分均为 0.0
    monkeypatch.setattr(
        retrieval_hybrid.httpx,
        "post",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("纯标点不应调用 /embeddings")),
    )
    assert hybrid_rank("???", docs, settings=settings) == [("gmv", 0.0), ("dau", 0.0)]
    assert hybrid_rank("！？。---...", docs, settings=settings) == [("gmv", 0.0), ("dau", 0.0)]


def test_embed_texts_batches_dedup_and_cosine_floor(monkeypatch):
    retrieval_hybrid.clear_embedding_cache()
    settings = config.build_settings(
        {
            "AVELIS_SECRET": SECRET,
            "AVELIS_EMBEDDING_BASE_URL": "https://emb.example/v1",
            "AVELIS_EMBEDDING_MODEL": "text-embedding-3-small",
        }
    )

    class EmptyRedis:
        def mget(self, keys):
            return [None] * len(keys)

        def pipeline(self):
            class _P:
                def set(self, *a, **kw):
                    pass

                def execute(self):
                    pass

            return _P()

    monkeypatch.setattr(retrieval_hybrid.cache, "client", lambda: EmptyRedis())
    batch_sizes: list[int] = []

    def fake_post(url, json=None, headers=None, timeout=None):
        items = json["input"]
        batch_sizes.append(len(items))
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(len(items))]},
        )

    monkeypatch.setattr(retrieval_hybrid.httpx, "post", fake_post)
    texts = [f"metric_{i}" for i in range(70)] + ["metric_0", "metric_1"]
    vecs = embed_texts(texts, settings=settings)
    assert vecs is not None and len(vecs) == 72
    assert batch_sizes == [64, 6]

    # 低余弦相似度 (< _MIN_SEMANTIC_COSINE=0.35) 且无词法/精确命中时应归零
    retrieval_hybrid.clear_embedding_cache()
    monkeypatch.setattr(
        retrieval_hybrid,
        "embed_texts",
        lambda items, settings=None: [[1.0, 0.0], [0.2, 0.979795897], [0.95, 0.312249899]],
    )
    docs = [
        SearchDocument(key="low", fields=[(1.0, "无关指标")]),
        SearchDocument(key="high", fields=[(1.0, "相关性高")]),
    ]
    ranked = dict(hybrid_rank("完全不重合的查询", docs, settings=settings))
    assert ranked["low"] == 0.0
    assert ranked["high"] > 0.0
