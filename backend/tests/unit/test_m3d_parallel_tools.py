"""M3D 并发只读工具执行单元测试：并发耗时、确定性 ToolMessage 顺序、预算竞争、串行工具隔离、ContextVar 与会话隔离。"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis.analysis import agent
from avelis.analysis import tools as ana_tools
from avelis.analysis.tools import CONCURRENT_READ_ONLY_TOOLS, Budget, _worker_session_factory
from avelis.ask import service
from avelis.i18n import current_locale, locale_context
from avelis.models import AnalysisDataset, AnalysisStep, Metric, MetricVersion, User
from tests.unit.test_analysis_b0_fixes import Replies
from tests.unit.test_analysis_toolbox import make_box


def _qm_call(call_id: str, group_by: list[str] | None = None, metric: str = "ec_gmv") -> dict:
    return {
        "id": call_id,
        "name": "query_metric",
        "args": {
            "metric_codes": [metric],
            "start_date": "2026-09-01",
            "end_date": "2026-09-30",
            "group_by": group_by or [],
            "purpose": f"call {call_id}",
        },
    }


def _parallel_box(monkeypatch, budget: Budget | None = None):
    metric = SimpleNamespace(id="m1", code="ec_gmv", name="成交额", domain_id="dA")
    version = SimpleNamespace(
        id="mv1",
        metric_id="m1",
        kind="base",
        expression="SUM(amount)",
        value_kind="level",
        unit="元",
        unit_scale=1,
        time_column="event_date",
        time_format="%Y-%m-%d",
        data_grain="day",
        dimensions=[
            {"name": "region", "label": "区域", "column": "region"},
            {"name": "channel", "label": "渠道", "column": "channel"},
            {"name": "category", "label": "类目", "column": "category"},
            {"name": "city", "label": "城市", "column": "city"},
        ],
        components=[],
        polarity=None,
    )
    catalog = service.Catalog([], {"ec_gmv": (metric, version)})
    box = make_box(budget=budget, catalog=catalog, monkeypatch=monkeypatch)
    box.session.store[(Metric, "m1")] = metric
    box.session.store[(MetricVersion, "mv1")] = version
    monkeypatch.setattr(service, "version_sources", lambda s, v: {})
    return box


def _fake_fulfil(monkeypatch, delays: dict[tuple[str, ...], float]):
    observed_locales: list[str] = []
    locale_lock = threading.Lock()

    def fake_fulfil(session, user, intent, answer, scoped):
        key = tuple(intent.group_by or ())
        time.sleep(delays.get(key, 0.05))
        with locale_lock:
            observed_locales.append(current_locale())
        answer.status = "answered"
        answer.sql = "SELECT 1"
        answer.metric_version_ids = ["mv1"]
        if key:
            dim = key[0]
            answer.result = {
                "columns": [dim, "成交额"],
                "rows": [["华东", 100], ["华南", 80]],
                "truncated": False,
            }
        else:
            answer.result = {"columns": ["成交额"], "rows": [[180]], "truncated": False}

    monkeypatch.setattr(service, "fulfil", fake_fulfil)
    monkeypatch.setattr(service, "finish_answer", lambda s, u, a, started: None)
    return observed_locales


def test_parallel_read_only_tools_run_concurrently_and_preserve_order(monkeypatch):
    box = _parallel_box(monkeypatch)
    _fake_fulfil(
        monkeypatch,
        {
            ("region",): 0.20,
            ("channel",): 0.05,
            ("category",): 0.12,
        },
    )
    reply = AIMessage(
        content="",
        tool_calls=[
            _qm_call("c1", ["region"]),
            _qm_call("c2", ["channel"]),
            _qm_call("c3", ["category"]),
        ],
    )
    t0 = time.monotonic()
    out = agent._round({"messages": []}, box, Replies(reply))
    elapsed = time.monotonic() - t0

    assert elapsed < 0.35, f"expected parallel execution < 0.35s, got {elapsed:.3f}s"
    tool_msgs = [m for m in out if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_msgs] == ["c1", "c2", "c3"]

    payloads = [json.loads(m.content) for m in tool_msgs]
    assert len({p["dataset"] for p in payloads}) == 3
    steps = [s for s in box.session.added if isinstance(s, AnalysisStep) and s.kind == "tool"]
    assert sorted(s.seq for s in steps) == [s.seq for s in steps]
    assert len({s.seq for s in steps}) == 3
    datasets = [d for d in box.session.added if isinstance(d, AnalysisDataset)]
    assert sorted(d.seq for d in datasets) == [1, 2, 3]


def test_parallel_budget_and_query_limit_are_enforced_atomically(monkeypatch):
    box = _parallel_box(monkeypatch, budget=Budget(queries=2, tool_calls=10))
    _fake_fulfil(monkeypatch, {("region",): 0.05, ("channel",): 0.05, ("category",): 0.05, ("city",): 0.05})
    reply = AIMessage(
        content="",
        tool_calls=[
            _qm_call("q1", ["region"]),
            _qm_call("q2", ["channel"]),
            _qm_call("q3", ["category"]),
            _qm_call("q4", ["city"]),
        ],
    )
    out = agent._round({"messages": []}, box, Replies(reply))
    tool_msgs = [m for m in out if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_msgs] == ["q1", "q2", "q3", "q4"]
    parsed = [json.loads(m.content) for m in tool_msgs]
    ok_count = sum(1 for p in parsed if "dataset" in p)
    err_count = sum(1 for p in parsed if "查询额度已用完" in p.get("error", ""))
    assert ok_count == 2 and err_count == 2
    assert box.budget.used_queries == 2 and box.budget.used_tools == 4


def test_mixed_batch_runs_parallel_queries_before_serial_chart_and_submit(monkeypatch):
    box = _parallel_box(monkeypatch, budget=Budget(queries=10, tool_calls=15))
    _fake_fulfil(monkeypatch, {("region",): 0.04, ("channel",): 0.04})

    reply = AIMessage(
        content="",
        tool_calls=[
            _qm_call("q1", ["region"]),
            _qm_call("q2", ["channel"]),
            {
                "id": "ch1",
                "name": "chart",
                "args": {"dataset": "D1", "kind": "bar", "title": "区域对比"},
            },
            {
                "id": "sub1",
                "name": "submit_report",
                "args": {
                    "title": "区域对比报告",
                    "summary": "已完成对比",
                    "conclusion": "insufficient_evidence",
                    "claims": [
                        {
                            "type": "fact",
                            "parts": [
                                "区域 ",
                                {"dataset": "D1", "label": "region", "row": 1},
                                " 数值 ",
                                {"dataset": "D1", "column": "成交额", "row": 1},
                            ],
                        }
                    ],
                },
            },
        ],
    )
    out = agent._round({"messages": []}, box, Replies(reply))
    tool_msgs = [m for m in out if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_msgs] == ["q1", "q2", "ch1", "sub1"]
    assert json.loads(tool_msgs[2].content) == {"ok": True}
    assert json.loads(tool_msgs[3].content) == {"ok": True}
    assert box.report is not None and len(box.report["figures"]) == 1
    assert box.report["figures"][0]["dataset"] == "D1"


def test_parallel_workers_inherit_request_locale_contextvar(monkeypatch):
    box = _parallel_box(monkeypatch)
    observed = _fake_fulfil(monkeypatch, {("region",): 0.03, ("channel",): 0.03})
    reply = AIMessage(
        content="",
        tool_calls=[
            _qm_call("c1", ["region"]),
            _qm_call("c2", ["channel"]),
        ],
    )
    with locale_context("en"):
        agent._round({"messages": []}, box, Replies(reply))
    assert observed == ["en", "en"]


def test_worker_session_factory_and_sandbox_tool_classification():
    assert "sandbox_calc" in CONCURRENT_READ_ONLY_TOOLS
    assert not (CONCURRENT_READ_ONLY_TOOLS & {"submit_report", "chart", "load_skill"})

    sqlite_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with Session(sqlite_engine) as s:
        assert _worker_session_factory(s) is None

    # Non-SQLite real SQLAlchemy Session gets an isolated sessionmaker and worker Session
    pg_like = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    fake_sa_session = SimpleNamespace(get_bind=lambda: pg_like)
    assert _worker_session_factory(fake_sa_session) is None  # not isinstance(..., Session)

    # Real SQLAlchemy Session with mocked non-sqlite dialect on bind
    with Session(sqlite_engine) as real_session:
        orig_name = real_session.get_bind().dialect.name
        try:
            real_session.get_bind().dialect.name = "postgresql"
            factory = _worker_session_factory(real_session)
            assert factory is not None
        finally:
            real_session.get_bind().dialect.name = orig_name


def test_run_in_worker_opens_and_closes_isolated_session_when_pooled(monkeypatch):
    sqlite_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with Session(sqlite_engine) as base_session:
        box = make_box(monkeypatch=monkeypatch)
        box.session = base_session
        box.executor = "job1:1"
        opened: list[Session] = []

        class IsolatedSession:
            def __init__(self):
                self.expunged = False
                self.closed = False
                self.rolled_back = False

            def get(self, cls, ident, **kw):
                if cls is User:
                    return SimpleNamespace(id=ident, is_active=True)
                return None

            def expunge_all(self):
                self.expunged = True

            def rollback(self):
                self.rolled_back = True

            def close(self):
                self.closed = True

        def fake_factory():
            s = IsolatedSession()
            opened.append(s)
            return s

        monkeypatch.setattr(ana_tools, "_worker_session_factory", lambda s: fake_factory)
        assert box.can_parallelize_tools() is True
        res = box.run_in_worker(lambda: (box.session, box.user.id))
        assert len(opened) == 1
        assert res[0] is opened[0] and res[0] is not base_session
        assert opened[0].expunged is True and opened[0].closed is True
        assert box.session is base_session


def test_can_parallelize_tools_detects_class_level_dispatch_monkeypatch(monkeypatch):
    box = _parallel_box(monkeypatch)
    box.executor = "exec:1"
    assert box.can_parallelize_tools() is True

    orig = ana_tools.AnalysisToolbox.dispatch
    monkeypatch.setattr(
        ana_tools.AnalysisToolbox, "dispatch", lambda self, name, args, **kw: orig(self, name, args, **kw)
    )
    assert box.can_parallelize_tools() is False


def test_object_new_toolbox_prepare_and_dispatch_without_tools_attr():
    box = object.__new__(ana_tools.AnalysisToolbox)
    box.__dict__["_state_lock"] = threading.RLock()
    box.__dict__["permission_lost"] = None
    box.__dict__["aborted"] = None
    box.__dict__["user_stopped"] = False
    box.__dict__["check_executor"] = lambda: None
    box.__dict__["tools"] = lambda: []
    box.__dict__["budget"] = Budget()
    box.__dict__["_step"] = lambda *a, **kw: None

    box.prepare_parallel_dispatch()
    assert box.__dict__["_tools"] == {}

    del box.__dict__["_tools"]
    out = json.loads(box.dispatch("unknown_tool", {}))
    assert "未知工具" in out["error"]
    assert box.__dict__["_tools"] == {}
