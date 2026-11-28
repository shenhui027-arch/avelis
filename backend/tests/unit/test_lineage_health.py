"""血缘体检的确定性规则（纯函数 findings）。

覆盖字段已不存在、来源未采集、定义未解析、待确认与低置信候选，以及范围、去重、排序与截断。
"""

from avelis.governance import lineage_health as h
from avelis.governance.lineage_health import AssetRow, CandidateRow, EdgeRow


def asset(aid, qualified, cols=(), kind="table", status=None, reason=None):
    return AssetRow(aid, qualified, kind, frozenset(cols), status, reason)


def edge(eid, src, src_ref, src_col, dst, dst_ref, dst_col, relation="value_direct"):
    return EdgeRow(eid, src, src_ref, src_col, dst, dst_ref, dst_col, relation)


ASSETS = {
    "A": asset("A", "c.ods.orders", ["id", "amount", "region"]),
    "B": asset("B", "c.dw.daily", ["day", "total"]),
    "V": asset("V", "c.dw.v_daily", ["day", "total"], kind="view", status="partial", reason="窗口函数未展开"),
    "T": asset("T", "c.dw.ok_view", ["x"], kind="view", status="ok"),
}
EDGES = [
    # A.amount → B.total 正常
    edge("e1", "A", "c.ods.orders", "amount", "B", "c.dw.daily", "total", "value_aggregated"),
    # A.price 已不存在（两条边都引用它，只报一次）
    edge("e2", "A", "c.ods.orders", "price", "B", "c.dw.daily", "total", "value_computed"),
    edge("e3", "A", "c.ods.orders", "price", "V", "c.dw.v_daily", "total"),
    # B.week 作为目标字段已不存在
    edge("e4", "A", "c.ods.orders", "id", "B", "c.dw.daily", "week"),
    # 设计时来源 → 已采集的 B（同一来源两条边只报一次）
    edge("e5", None, "c.tmp.staging", "x", "B", "c.dw.daily", "day"),
    edge("e6", None, "c.tmp.staging", "y", "B", "c.dw.daily", "total"),
    # 表级边（字段为空）不检查字段
    edge("e7", "B", "c.dw.daily", None, "V", "c.dw.v_daily", None, "table"),
    # 目标未采集的边不报 uncollected_source（来源已采集也不报）
    edge("e8", "A", "c.ods.orders", "id", None, "c.tmp.out", "id"),
]
CANDS = [
    CandidateRow("k1", "c.ods.orders", "c.dw.daily", 0.9),
    CandidateRow("k2", "c.ods.orders", "c.dw.daily", 0.4),
    CandidateRow("k3", "c.ods.orders", "c.dw.daily", 0.55),
    CandidateRow("k4", "c.ods.orders", "c.tmp.unknown", None),
]


def codes(result):
    return [(f["code"], f["subject"]) for f in result["findings"]]


def test_all_scope_rules_ordering_and_summary():
    r = h.findings(EDGES, ASSETS, CANDS)
    assert codes(r) == [
        ("missing_column", "c.dw.daily.week"),
        ("missing_column", "c.ods.orders.price"),
        ("definition_unparsed", "c.dw.v_daily"),
        ("uncollected_source", "c.tmp.staging"),
        ("low_confidence", "c.dw.daily"),
        ("pending_candidates", "c.dw.daily"),
        ("pending_candidates", "c.tmp.unknown"),
    ]
    assert r["summary"] == {
        "missing_column": 2,
        "definition_unparsed": 1,
        "uncollected_source": 1,
        "low_confidence": 1,
        "pending_candidates": 2,
    }
    assert r["truncated"] is False
    by = {(f["code"], f["subject"]): f for f in r["findings"]}
    price = by[("missing_column", "c.ods.orders.price")]
    assert price["severity"] == "danger" and price["asset_id"] == "A"
    assert price["detail"] == {"edge_id": "e2", "side": "source", "column": "price", "edges": 2}
    week = by[("missing_column", "c.dw.daily.week")]
    assert week["asset_id"] == "B"
    assert week["detail"] == {"edge_id": "e4", "side": "target", "column": "week", "edges": 1}
    assert by[("uncollected_source", "c.tmp.staging")] | {} == {
        "code": "uncollected_source",
        "severity": "warn",
        "asset_id": "B",
        "subject": "c.tmp.staging",
        "detail": {"target": "c.dw.daily"},
    }
    assert by[("definition_unparsed", "c.dw.v_daily")]["detail"] == {"status": "partial", "reason": "窗口函数未展开"}
    assert by[("pending_candidates", "c.dw.daily")]["detail"] == {"count": 3}
    assert by[("pending_candidates", "c.dw.daily")]["asset_id"] == "B"
    assert by[("pending_candidates", "c.tmp.unknown")]["asset_id"] is None
    assert by[("low_confidence", "c.dw.daily")]["detail"] == {"count": 2, "min": 0.4}


def test_focus_limits_to_direct_edges_own_definition_and_incoming_candidates():
    r = h.findings(EDGES, ASSETS, CANDS, focus="V")
    # V 相关的边：e3（A.price 已不存在）、e7（表级）；V 自身定义未解析；没有目标是 V 的候选
    assert codes(r) == [("missing_column", "c.ods.orders.price"), ("definition_unparsed", "c.dw.v_daily")]
    assert r["findings"][0]["detail"] == {"edge_id": "e3", "side": "source", "column": "price", "edges": 1}

    r = h.findings(EDGES, ASSETS, CANDS, focus="B")
    assert codes(r) == [
        ("missing_column", "c.dw.daily.week"),
        ("missing_column", "c.ods.orders.price"),
        ("uncollected_source", "c.tmp.staging"),
        ("low_confidence", "c.dw.daily"),
        ("pending_candidates", "c.dw.daily"),
    ]


def test_clean_graph_has_no_findings():
    r = h.findings([EDGES[0]], ASSETS, [], focus="A")
    assert r == {"findings": [], "summary": {}, "truncated": False}
    assert h.findings([], {"T": ASSETS["T"]}, []) == {"findings": [], "summary": {}, "truncated": False}


def test_truncates_but_summary_counts_everything(monkeypatch):
    monkeypatch.setattr(h, "MAX_FINDINGS", 2)
    r = h.findings(EDGES, ASSETS, CANDS)
    assert len(r["findings"]) == 2 and r["truncated"] is True
    assert sum(r["summary"].values()) == 7
