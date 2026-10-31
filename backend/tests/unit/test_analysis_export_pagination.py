"""超高内容应由 PDF 布局拆页，实际绘制的文字必须完整。"""

from __future__ import annotations

import re

import pytest
from reportlab.pdfgen.textobject import PDFTextObject

from avelis.analysis import export
from tests.unit.test_analysis_export_html import Document


@pytest.fixture
def drawn_text(monkeypatch):
    drawn = []
    original = PDFTextObject._textOut

    def record(self, text, *args, **kwargs):
        drawn.append(text)
        return original(self, text, *args, **kwargs)

    monkeypatch.setattr(PDFTextObject, "_textOut", record)
    return drawn


@pytest.mark.parametrize("audience", ["brief", "full"])
@pytest.mark.parametrize("length", [30, 2500])
def test_summary_paginates_without_losing_text(drawn_text, audience, length):
    sentence = "本期指标变化仍需结合已确认的范围解释。"
    summary = "摘要起点" + (sentence * (length // len(sentence) + 1))[:length - 8] + "摘要终点"
    view = {"report": {"title": "指标变化", "summary": summary}}

    pdf = export.to_pdf(view, audience=audience)

    assert pdf.startswith(b"%PDF")
    assert summary in "".join(drawn_text)
    assert summary in Document(export.to_html(view, audience=audience)).visible
    if length == 2500:
        assert len(re.findall(rb"/Type\s*/Page\b", pdf)) >= 2


@pytest.mark.parametrize("audience", ["brief", "full"])
def test_unresolved_item_paginates_without_losing_text(drawn_text, audience):
    item = "待查起点" + "仍需补充相应范围的证据才能解释变化。" * 160 + "待查终点"
    view = {"report": {"title": "指标变化", "summary": "已发现变化。", "unexplained": item}}

    assert export.to_pdf(view, audience=audience).startswith(b"%PDF")
    assert "• " + item in "".join(drawn_text)
    assert item in Document(export.to_html(view, audience=audience)).visible


def test_metric_card_paginates_without_losing_text(monkeypatch, drawn_text):
    value = "指标值起点" + "甲乙丙丁" * 600 + "指标值终点"
    monkeypatch.setattr(export, "_core_metrics", lambda view: [
        {"label": "本期", "text": value, "raw": 1, "measure": "level", "ref": "D1"},
        {"label": "对比期", "text": "2", "raw": 2, "measure": "level", "ref": "D1"},
    ])
    view = {"report": {"title": "指标变化", "summary": "已发现变化。"}}

    assert export.to_pdf(view).startswith(b"%PDF")
    text = "".join(drawn_text)
    assert "指标值起点" in text and "指标值终点" in text
    # 另一张卡片的文字会插在跨页片段之间；逐字计数确认每页真正绘制了完整值。
    assert all(text.count(char) == value.count(char) for char in "甲乙丙丁")


def test_data_cell_paginates_without_losing_text(drawn_text):
    member = "成员起点" + "甲乙丙丁" * 1000 + "成员终点"
    view = {"report": {"title": "指标变化", "summary": "已发现变化。"}, "datasets": [
        {"ref": "D1", "kind": "governed", "title": "指标明细", "columns": ["成员", "值"],
         "rows": [[member, 1], ["另一成员", 2]], "meta": {"units": {"值": {"kind": "level"}}}},
    ]}

    assert export.to_pdf(view, datasets=["D1"]).startswith(b"%PDF")
    text = "".join(drawn_text)
    assert "成员起点" in text and "成员终点" in text
    # 重复的表头和旁列穿插在各页之间，数据本身应一个字也不丢。
    assert all(text.count(char) == member.count(char) for char in "甲乙丙丁")
