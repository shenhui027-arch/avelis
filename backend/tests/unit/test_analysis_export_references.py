"""简报隐藏引用编号时，维度成员与引用标记必须保持语义区别。"""

from copy import deepcopy

import pytest

from avelis.analysis import export
from avelis.analysis.grounding import render as ground
from tests.unit.test_analysis_export import _pdf_story
from tests.unit.test_analysis_export_audience import _pdf_text


@pytest.fixture(params=["markdown", "html", "pdf"])
def render(request, monkeypatch):
    if request.param == "pdf":
        return lambda view, **kwargs: _pdf_text(_pdf_story(monkeypatch, view, **kwargs))
    return getattr(export, "to_" + request.param)


def _member_view():
    dataset = {"D1": {"kind": "governed", "columns": ["member"], "rows": [{"member": "D1"}]}}

    def record(prefix, suffix, citation):
        result = ground([prefix, {"dataset": "D1", "label": "member"}, suffix], dataset, None)
        assert not result["errors"]
        # 兼容已有保存结果中的引用标记；label 段的 text 则是实际成员名。
        result["segments"].append({"kind": "ref", "ref": citation, "text": f"[{citation}]"})
        result["text"] += f"[{citation}]"
        return result

    summary = record("成员 ", " 的收入下降", "D901")
    claim = record("成员 ", " 的收入下降，仍需复核", "D902")
    claim.update(type="inference", refs=["D903"], details={"premise": record("已确认成员 ", "", "D904")})
    return {"datasets": [], "verification": {}, "report": {
        "title": "门店D1的收入下降（D905）", "summary": summary["text"], "fields": {"summary": summary},
        "claims": [claim, {"type": "fact", "text": "门店D1的收入下降；D1 的观测结果仍需复核", "refs": ["D906"]}],
        "causes": [{"type": "member", "label": "D1", "entity": {"member": "D1"},
                    "direction": "down", "evidence": ["D907"], "contribution": {"text": "收入下降"}}],
    }}


def test_brief_preserves_grounded_members_and_only_hides_references(render):
    view = _member_view()
    original = deepcopy(view)

    text = render(view, audience="brief")

    assert "门店D1的收入下降" in text
    assert "成员 D1 的收入下降" in text
    assert "D1 的观测结果仍需复核" in text
    assert "已确认成员 D1" in text
    assert "D1 · 下降" in text
    assert "未命名成员" not in text
    for reference in ("D901", "D902", "D903", "D904", "D905", "D906", "D907"):
        assert reference not in text
    assert view == original


def test_full_keeps_the_same_reference_markers_and_member_identifiers(render):
    text = render(_member_view(), audience="full")

    assert "成员 D1 的收入下降[D901]" in text
    assert "D1 · 下降" in text
    for reference in ("D902", "D903", "D904", "D905", "D906", "D907"):
        assert reference in text


@pytest.mark.parametrize("structured", [False, True])
def test_brief_preserves_unidentified_member_without_label_or_entity_metadata(render, structured):
    text = "D1 的收入下降"
    record = {"text": text}
    if structured:
        record["segments"] = [{"kind": "text", "text": text}]
    view = {"datasets": [], "verification": {}, "report": {
        "title": text, "summary": text, "fields": {"summary": deepcopy(record)},
        "claims": [{"type": "fact", **record}],
    }}

    assert render(view, audience="brief").count(text) >= 3


def test_brief_keeps_bare_members_despite_declared_refs_and_hides_contextual_references(render):
    view = {"datasets": [], "verification": {}, "report": {
        "title": "已保存引用 D901", "summary": "来源已校验 D902",
        "claims": [{"type": "fact", "text": "D903 的结果已保存；D1 的收入下降", "refs": ["D903"],
                    "segments": [{"kind": "text", "text": "D903 的结果已保存；D1 的收入下降"}]}],
    }}

    text = render(view, audience="brief")

    assert "D1 的收入下降" in text
    assert "D903 的结果已保存" in text
    for reference in ("D901", "D902"):
        assert reference not in text


@pytest.mark.parametrize("member", ["D1", "(D1)", "引用 D1"])
def test_brief_preserves_text_member_matching_an_adjacent_reference_segment(render, member):
    body = member + " 的收入下降"
    claim = {"type": "fact", "refs": ["D1"], "text": body + "[D1]",
             "segments": [{"kind": "text", "text": body},
                          {"kind": "ref", "ref": "D1", "text": "[D1]"}]}
    view = {"datasets": [], "verification": {}, "report": {"claims": [claim]}}

    text = render(view, audience="brief")

    assert body in text
    assert "[D1]" not in text


def test_brief_does_not_treat_a_known_member_as_a_global_reference_whitelist(render):
    view = {"datasets": [], "verification": {}, "report": {
        "summary": "引用 D1 的保存结果",
        "causes": [{"type": "member", "entity": {"member": "D1"}, "direction": "down"}],
    }}

    text = render(view, audience="brief")

    assert "引用 D1" not in text
    assert "D1 · 下降" in text
