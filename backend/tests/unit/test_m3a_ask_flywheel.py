"""M3A Ask 飞轮测试：答案赞/踩反馈、长对话压缩、解读数值核验与可选语义缓存。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from redis.exceptions import RedisError

from avelis import cache, config, modelcfg
from avelis.api import ask as ask_api
from avelis.api.deps import current_user, get_session
from avelis.ask import agent as ask_agent
from avelis.ask import service as ask_service
from avelis.ask.grounding import verify_explanation
from avelis.ask.intent import MetricCard
from avelis.db import utcnow
from avelis.governance.errors import GovernanceError
from avelis.models import Answer, AskThread, User


def _make_answer(**overrides):
    base = dict(
        id="ans-1",
        question="销售额多少",
        thread_id="th-1",
        asked_by="u1",
        asked_at=utcnow(),
        status="answered",
        model_adapter="test",
        intent={},
        plan={},
        sql="SELECT 1",
        metric_version_ids=["mv1"],
        evidence={},
        result={
            "columns": ["region", "sales", "rate"],
            "rows": [["华东", 123456, 0.125], ["门店1", 50000, -0.05]],
            "column_roles": {
                "region": {"role": "dimension", "label": "区域"},
                "sales": {"role": "measure", "label": "销售额"},
                "rate": {"role": "rate", "label": "同比增长率"},
            },
            "units": {
                "sales": {"kind": "currency", "unit": "元", "additive": True},
                "rate": {"kind": "rate_change"},
            },
            "insights": [{"kind": "max", "text": "华东销售额最高，占比71.17%。", "value": 123456}],
        },
        message="共 2 行",
        error_code=None,
        freshness={},
        executed_at=utcnow(),
        duration_ms=10,
        cache_hit=False,
        recheck_status=None,
        steps=[],
        explanation="共 2 行",
        explanation_source="template",
        feedback_rating=None,
        feedback_note=None,
        feedback_at=None,
        feedback_by=None,
    )
    base.update(overrides)
    return Answer(**base)


def test_answer_feedback_api_owner_only_and_rejects_clarify():
    u1 = User(id="u1", username="owner", is_active=True)
    u2 = User(id="u2", username="admin", is_active=True)
    ans = _make_answer(id="ans-1", asked_by="u1", status="answered")
    clar = _make_answer(id="ans-2", asked_by="u1", status="clarify")
    store = {Answer: {"ans-1": ans, "ans-2": clar}}
    added = []

    class FakeSession:
        def get(self, cls, id_, **kwargs):
            return store.get(cls, {}).get(id_)

        def scalars(self, stmt):
            return []

        def scalar(self, stmt):
            return None

        def add(self, obj):
            added.append(obj)

        def commit(self):
            pass

    current = {"user": u1}
    app = FastAPI()

    @app.exception_handler(GovernanceError)
    async def governance_error(_request, exc: GovernanceError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, **exc.detail})

    app.include_router(ask_api.router)
    app.dependency_overrides[current_user] = lambda: current["user"]
    app.dependency_overrides[get_session] = lambda: FakeSession()

    with TestClient(app) as client:
        up = client.post("/api/answers/ans-1/feedback", json={"rating": "up", "note": " 很准确 "})
        assert up.status_code == 200
        assert up.json()["feedback_rating"] == "up" and up.json()["feedback_note"] == "很准确"
        assert up.json()["feedback_at"] is not None

        # 清除反馈
        clr = client.post("/api/answers/ans-1/feedback", json={"rating": None})
        assert clr.status_code == 200 and clr.json()["feedback_rating"] is None

        # clarify 状态拒绝反馈
        assert client.post("/api/answers/ans-2/feedback", json={"rating": "down"}).status_code == 409

        # 非提问人（包括管理员）禁止修改他人答案反馈
        current["user"] = u2
        assert client.post("/api/answers/ans-1/feedback", json={"rating": "down"}).status_code == 404


def test_verify_explanation_scale_awareness_exemptions_and_retry_fallback(monkeypatch):
    ans = _make_answer()
    # 12.35万 (123456 ROUND_HALF_UP), 12.5%, 下降 5%, 71.17% (insights), 日期 2026-09, 标识符 V1, 维度 门店1
    valid_text = (
        "2026-09 V1 指标显示华东销售额为 12.35万，同比增长 12.5%。\n"
        "- 门店1销售额为 5万，同比下降 5个百分点。\n"
        "- 华东占比 71.17%，共 2 条记录。"
    )
    ok = verify_explanation(valid_text, ans, {"limit": 10})
    assert ok["ok"] is True and ok["ungrounded"] == []

    # 编造数字 9999 被拦下
    bad = verify_explanation("华东销售额为 9999 元。", ans, {})
    assert bad["ok"] is False and "9999" in bad["ungrounded"]

    # 显式正负号与无符号定性负向表述严格区分
    neg_ans = _make_answer(
        result={
            "columns": ["region", "rate"],
            "rows": [["华东", -0.042]],
            "column_roles": {"region": {"role": "dimension"}, "rate": {"role": "rate"}},
            "units": {"rate": {"kind": "rate_change"}},
            "insights": [],
        }
    )
    wrong_pos = verify_explanation("华东同比增长率为 +4.2%。", neg_ans, {})
    assert wrong_pos["ok"] is False and "+4.2%" in wrong_pos["ungrounded"]
    assert verify_explanation("华东同比增长率为 -4.2%。", neg_ans, {})["ok"] is True
    assert verify_explanation("华东同比下降 4.2%。", neg_ans, {})["ok"] is True

    pos_ans = _make_answer(
        result={
            "columns": ["region", "rate"],
            "rows": [["华东", 0.156]],
            "column_roles": {"region": {"role": "dimension"}, "rate": {"role": "rate"}},
            "units": {"rate": {"kind": "rate_change"}},
            "insights": [],
        }
    )
    assert verify_explanation("华东同比增长 +15.6%。", pos_ans, {})["ok"] is True
    assert verify_explanation("华东同比变化 -15.6%。", pos_ans, {})["ok"] is False

    # 维度免检词元必须按边界替换：不得把 10000 误伤成 000，也不得把 1001 误遮成 1
    dim10_ans = _make_answer(
        result={
            "columns": ["store", "sales"],
            "rows": [["10", 10000]],
            "row_count": 1,
            "column_roles": {"store": {"role": "dimension"}, "sales": {"role": "measure"}},
            "units": {"sales": {"kind": "level"}},
            "insights": [],
        }
    )
    assert verify_explanation("门店 10 的销售额为 10000 元。", dim10_ans, {})["ok"] is True
    assert verify_explanation("门店 10 的销售额为 10,000 元。", dim10_ans, {})["ok"] is True

    dim100_ans = _make_answer(
        result={
            "columns": ["store", "sales"],
            "rows": [["100", 50000]],
            "row_count": 1,
            "column_roles": {"store": {"role": "dimension"}, "sales": {"role": "measure"}},
            "units": {"sales": {"kind": "level"}},
            "insights": [],
        }
    )
    fn_check = verify_explanation("门店销售额为 1001 元。", dim100_ans, {})
    assert fn_check["ok"] is False and "1001" in fn_check["ungrounded"]

    # explain_answer 触发最多 1 次修正重试，若仍未通过则安全回退模板
    model = Mock()
    model.bind_tools.return_value = model
    model.invoke.side_effect = [AIMessage("编造数字 8888"), AIMessage("仍然编造 7777")]
    monkeypatch.setattr(modelcfg, "policy", lambda *_: {"send_results": True})
    monkeypatch.setattr(modelcfg, "resolve", lambda *a, **kw: SimpleNamespace(model=model))
    monkeypatch.setattr(ask_agent, "_explain_supports_tools", lambda *_: False)

    steps = ask_agent.explain_answer(Mock(), object(), ans, {})
    assert model.invoke.call_count == 2
    assert ans.explanation == ans.message and ans.explanation_source == "template"
    assert ans.evidence["explanation_grounding"]["fallback"] == "template"
    assert any(s.get("node") == "explain_grounding" and s.get("ok") is False for s in steps)


def test_ask_thread_compaction_and_redaction_and_api(monkeypatch):
    # _merge_messages 使用 RESET 原子替换，保障摘要在近期轮次之前
    old = [HumanMessage("q1"), AIMessage("a1"), HumanMessage("q2"), AIMessage("a2")]
    reset_applied = ask_agent._merge_messages(
        old,
        [
            SystemMessage("", id=ask_agent.RESET),
            AIMessage("summary", id=ask_agent.ASK_COMPACT_SUMMARY_ID),
            HumanMessage("q3"),
        ],
    )
    assert [m.content for m in reset_applied] == ["summary", "q3"]

    # 长会话压缩：保留工具调用完整对并在 send_results=False 时隐藏原始数值
    long_msgs = []
    for i in range(6):
        long_msgs.append(HumanMessage(f"问题 {i} " + ("x" * 180)))
        long_msgs.append(AIMessage("", tool_calls=[{"name": "search_metrics", "args": {}, "id": f"c{i}"}]))
        long_msgs.append(ToolMessage("{}", tool_call_id=f"c{i}"))
        long_msgs.append(
            AIMessage(
                f"[answered] 销售额 9999{i}",
                additional_kwargs={"avelis_summary": True, "redacted": "[answered] 指标 sales（结果未发送给模型）"},
            )
        )

    compacted, info = ask_agent._compact_messages(None, "th-1", long_msgs, send_results=False, reason="manual")
    assert info["compacted"] is True
    assert compacted[0].id == ask_agent.ASK_COMPACT_SUMMARY_ID
    assert "9999" not in compacted[0].content
    assert not isinstance(compacted[1], ToolMessage)

    # POST /api/ask/threads/{id}/compact waiting 状态返回 409，active 状态成功压缩并暴露到 ThreadDetailOut
    u1 = User(id="u1", username="u1", is_active=True)
    t_wait = AskThread(id="th-wait", user_id="u1", title="wait", status="waiting")
    t_ok = AskThread(id="th-ok", user_id="u1", title="ok", status="active")

    class FakeSession:
        def get(self, cls, id_, **kwargs):
            return {"th-wait": t_wait, "th-ok": t_ok}.get(id_) if cls is AskThread else None

        def scalars(self, stmt):
            return []

        def add(self, obj):
            pass

        def commit(self):
            pass

    fake_state = SimpleNamespace(values={"messages": long_msgs, "compacted_summary": None, "compacted_at": None})
    updated = {}

    class FakeApp:
        def get_state(self, cfg):
            return fake_state

        def update_state(self, cfg, vals):
            updated.update(vals)
            fake_state.values["compacted_summary"] = vals["compacted_summary"]
            fake_state.values["compacted_at"] = vals["compacted_at"]

    monkeypatch.setattr(ask_agent, "graph", lambda: FakeApp())
    monkeypatch.setattr(modelcfg, "policy", lambda *_: {"send_results": False})

    app = FastAPI()

    @app.exception_handler(GovernanceError)
    async def governance_error(_request, exc: GovernanceError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, **exc.detail})

    app.include_router(ask_api.router)
    app.dependency_overrides[current_user] = lambda: u1
    app.dependency_overrides[get_session] = lambda: FakeSession()

    with TestClient(app) as client:
        assert client.post("/api/ask/threads/th-wait/compact").status_code == 409
        res = client.post("/api/ask/threads/th-ok/compact")
        assert res.status_code == 200 and res.json()["compacted"] is True
        assert "9999" not in res.json()["compacted_summary"]
        detail = client.get("/api/ask/threads/th-ok")
        assert detail.status_code == 200 and detail.json()["compacted_summary"] == res.json()["compacted_summary"]

        # 未再次触发压缩时（compacted=False），若 send_results=False，返回已有摘要也必须脱敏
        fake_state.values["messages"] = [
            AIMessage(
                "历史上下文摘要：\n- 销售额 99999",
                id=ask_agent.ASK_COMPACT_SUMMARY_ID,
                additional_kwargs={"redacted": "历史上下文摘要（已脱敏）：\n- 已脱敏"},
            ),
            HumanMessage("q_next"),
        ]
        fake_state.values["compacted_summary"] = "历史上下文摘要：\n- 销售额 99999"
        noop = client.post("/api/ask/threads/th-ok/compact")
        assert noop.status_code == 200 and noop.json()["compacted"] is False
        assert "99999" not in noop.json()["compacted_summary"]
        assert "已脱敏" in noop.json()["compacted_summary"]


def test_semantic_cache_literal_guard_tag_invalidation_and_redis_fail_open(monkeypatch):
    # 默认关闭
    default_cfg = config.build_settings({"AVELIS_SECRET": "x" * 20})
    assert default_cfg.ask_semantic_cache_enabled is False

    store: dict[str, str] = {}
    tags_map: dict[str, set[str]] = {}

    class FakePipe:
        def __init__(self):
            self.ops = []

        def set(self, k, v, ex=None):
            self.ops.append(("set", k, v))

        def sadd(self, t, k):
            self.ops.append(("sadd", t, k))

        def expire(self, t, ttl):
            pass

        def execute(self):
            for op, a, b in self.ops:
                if op == "set":
                    store[a] = b
                elif op == "sadd":
                    tags_map.setdefault(a, set()).add(b)

    class FakeRedis:
        def get(self, k):
            return store.get(k)

        def set(self, k, v, ex=None):
            store[k] = v

        def smembers(self, t):
            return tags_map.get(t, set())

        def delete(self, *keys):
            for k in keys:
                store.pop(k, None)
                tags_map.pop(k, None)

        def pipeline(self):
            return FakePipe()

    monkeypatch.setattr(cache, "client", lambda: FakeRedis())

    cache.semantic_cache_store(
        "scope-a",
        "2026-09 华东销售额前3名",
        query_vec=None,
        payload={"intent": {"metric_codes": ["sales"], "limit": 3}},
        ttl_seconds=300,
        max_entries=50,
        tags=["metric_version:mv1", "asset:a1"],
    )

    # 相似表述同日期/数字可命中
    hit = cache.semantic_cache_lookup("scope-a", "2026-09 华东销售额前3名是多少", query_vec=None, threshold=0.6)
    assert hit is not None and hit["intent"]["metric_codes"] == ["sales"]

    # 字面量守卫：2026-10 绝对不能命中 2026-09
    assert cache.semantic_cache_lookup("scope-a", "2026-10 华东销售额前3名", query_vec=None, threshold=0.5) is None
    # 中文数字守卫：前三名与前五名、九月与十月绝对不能混命中
    cache.semantic_cache_store(
        "scope-a",
        "华东销售额前三名",
        query_vec=None,
        payload={"intent": {"metric_codes": ["sales"], "limit": 3}},
        ttl_seconds=300,
        max_entries=50,
        tags=["metric_version:mv1"],
    )
    assert cache.semantic_cache_lookup("scope-a", "华东销售额前三名是多少", query_vec=None, threshold=0.6) is not None
    assert cache.semantic_cache_lookup("scope-a", "华东销售额前五名", query_vec=None, threshold=0.6) is None
    cache.semantic_cache_store(
        "scope-a",
        "九月华东销售额",
        query_vec=None,
        payload={"intent": {"metric_codes": ["sales"]}},
        ttl_seconds=300,
        max_entries=50,
        tags=["metric_version:mv1"],
    )
    assert cache.semantic_cache_lookup("scope-a", "十月华东销售额", query_vec=None, threshold=0.6) is None
    # 权限作用域隔离：scope-b 无法命中 scope-a
    assert cache.semantic_cache_lookup("scope-b", "2026-09 华东销售额前3名", query_vec=None, threshold=0.5) is None

    # 标签失效：metric_version:mv1 清除缓存
    cache.invalidate_tag("metric_version:mv1")
    assert cache.semantic_cache_lookup("scope-a", "2026-09 华东销售额前3名", query_vec=None, threshold=0.5) is None

    # RedisError 静默降级（fail-open）
    class DownRedis:
        def get(self, k):
            raise RedisError("down")

        def set(self, *a, **kw):
            raise RedisError("down")

        def pipeline(self):
            raise RedisError("down")

    monkeypatch.setattr(cache, "client", lambda: DownRedis())
    assert cache.semantic_cache_lookup("scope-a", "2026-09 华东销售额前3名", query_vec=None, threshold=0.5) is None
    cache.semantic_cache_store(
        "scope-a", "2026-09 华东销售额", query_vec=None, payload={}, ttl_seconds=60, max_entries=10, tags=[]
    )


def test_semantic_cache_hit_skips_llm_understand_but_always_runs_govern(monkeypatch):
    enabled_cfg = config.build_settings({"AVELIS_SECRET": "x" * 20, "AVELIS_ASK_SEMANTIC_CACHE_ENABLED": "true"})
    monkeypatch.setattr(ask_agent, "get_settings", lambda: enabled_cfg)
    monkeypatch.setattr(modelcfg, "policy", lambda *_: {"send_results": True})
    monkeypatch.setattr(
        ask_agent.modelcfg,
        "resolve",
        lambda *a, **kw: SimpleNamespace(
            model=SimpleNamespace(
                bind_tools=lambda *_: (_ for _ in ()).throw(AssertionError("不应调用 understand 模型"))
            ),
            adapter="test",
        ),
    )
    card = MetricCard(code="sales", name="销售额", domain="零售", aliases=[], dimensions=[], status="published")
    catalog = ask_service.Catalog(
        [card], {"sales": (SimpleNamespace(id="m1"), SimpleNamespace(id="mv1", dependency_fingerprint="fp1"))}
    )
    monkeypatch.setattr(ask_service, "visible_catalog", lambda *a, **kw: catalog)
    monkeypatch.setattr(ask_agent.access, "domain_ids", lambda *_: {"d1"})
    monkeypatch.setattr(ask_agent.access, "roles_of", lambda *_: {"analyst"})
    monkeypatch.setattr(
        cache,
        "semantic_cache_lookup",
        lambda *a, **kw: {
            "intent": {"metric_codes": ["sales"], "start_date": "2026-09-01", "end_date": "2026-09-30"},
            "filter_alignment": [],
            "explanation": "共 1 行，销售额为 100。",
            "similarity": 0.95,
        },
    )
    answer = _make_answer(
        evidence={}, result={"columns": ["sales"], "rows": [[100]], "column_roles": {}, "units": {}, "insights": []}
    )
    out = ask_agent.understand(
        {"turns": ["2026-09 销售额"], "messages": []},
        {
            "configurable": {
                "session": SimpleNamespace(),
                "user": User(id="u1", username="u1"),
                "answer": answer,
                "standalone": True,
            }
        },
    )
    assert out["intent"]["metric_codes"] == ["sales"]
    assert answer.evidence["semantic_cache"]["hit"] is True

    # govern 必定执行 service.fulfil
    called = []
    monkeypatch.setattr(ask_service, "fulfil", lambda *a, **kw: called.append(True))
    ask_agent.govern(
        {"intent": out["intent"]},
        {"configurable": {"session": SimpleNamespace(), "user": User(id="u1", username="u1"), "answer": answer}},
    )
    assert called == [True]


def test_semantic_cache_blocks_city_and_relative_time_swaps(monkeypatch):
    import json as _json

    stored: dict[str, str] = {}

    class FakeClient:
        def get(self, k):
            return stored.get(k)

        def set(self, k, v, ex=None):
            stored[k] = v

        def smembers(self, k):
            val = stored.get(k)
            return set(_json.loads(val)) if val else set()

        def mget(self, keys):
            return [stored.get(k) for k in keys]

        def pipeline(self):
            class _Pipe:
                def __init__(self):
                    self.ops = []

                def set(self, k, v, ex=None):
                    self.ops.append(("set", k, v))

                def sadd(self, k, v):
                    self.ops.append(("sadd", k, v))

                def expire(self, k, t):
                    pass

                def execute(self):
                    for op, k, v in self.ops:
                        if op == "set":
                            stored[k] = v
                        elif op == "sadd":
                            cur = set(_json.loads(stored[k])) if k in stored else set()
                            cur.add(v)
                            stored[k] = _json.dumps(list(cur))

            return _Pipe()

    monkeypatch.setattr(cache, "client", lambda: FakeClient())
    scope = "scope-city-guard"
    vec = [1.0, 0.0]
    cache.semantic_cache_store(
        scope,
        "昨天销售额最高的是北京市吗",
        query_vec=vec,
        payload={"intent": {"metric_codes": ["sales"], "city": "北京"}},
        ttl_seconds=600,
        max_entries=200,
        tags=[],
    )
    assert cache.semantic_cache_lookup(scope, "昨天销售额最高的是北京市吗", query_vec=vec, threshold=0.85) is not None
    assert cache.semantic_cache_lookup(scope, "昨天销售额最高的是上海市吗", query_vec=vec, threshold=0.85) is None
    assert cache.semantic_cache_lookup(scope, "今天销售额最高的是北京市吗", query_vec=vec, threshold=0.85) is None
