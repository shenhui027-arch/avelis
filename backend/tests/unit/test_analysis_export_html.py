"""HTML 与 PDF 使用同一份报告内容，下载文件不依赖外部资源。"""

from __future__ import annotations

import re
from copy import deepcopy
from html import escape
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from avelis.analysis import export, grounding
from tests.unit.test_analysis_export import _pdf_story, _story_text, sample_view


class Document(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.tags = []
        self.text = []
        self.hidden = False
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "style":
            self.hidden = True

    def handle_endtag(self, tag):
        if tag == "style":
            self.hidden = False

    def handle_data(self, text):
        if not self.hidden:
            self.text.append(text)

    @property
    def visible(self):
        return "\n".join(self.text)


def test_html_default_is_plain_language_and_full_has_slim_appendix():
    view = sample_view()
    brief = export.to_html(view)
    text = Document(brief).visible
    assert brief.startswith("<!doctype html>") and '<html lang="zh-CN">' in brief
    assert "SELECT" not in brief and "受治理" not in brief and "口径版本" not in brief
    assert not re.search(r"(?<![A-Za-z0-9_])D[1-9]\d*(?![A-Za-z0-9_])", text)
    assert "附录" not in text and "查询明细" not in text and "待验证" in text

    full = export.to_html(view, audience="full")
    body, appendix = full.split('<section class="appendix">', 1)
    assert "附录" not in Document(body).visible
    assert all(value in Document(appendix).visible for value in ("附录", "数据检查", "用到的数据", "查询明细"))
    assert "SELECT 1253000 AS metric_a, 1400000 AS metric_b" in appendix
    assert "version-1" in appendix and "version-12" in appendix and "version-13" not in appendix
    assert "数字来源" not in full and "来源构成" not in full and "sample_step" not in full


@pytest.mark.parametrize("audience", ["brief", "full"])
def test_html_escapes_titles_claims_tables_and_svg_labels(audience):
    view = sample_view()
    attack = '<script>alert("unsafe")</script>'
    short_attack = "<b>&"
    view["report"]["title"] = attack
    view["report"]["summary"] = attack
    view["report"]["claims"][0]["text"] = attack
    selected = view["datasets"][4]
    selected["title"] = attack
    selected["columns"][0] = short_attack
    selected["rows"][0][0] = attack
    selected["rows"][1][0] = short_attack
    selected["sql"] = "SELECT '" + attack + "'"

    markup = export.to_html(view, audience=audience, datasets=["D5"])
    document = Document(markup)
    tags = [tag for tag, _ in document.tags]
    assert "script" not in tags and "b" not in tags and "img" not in tags
    assert attack not in markup and escape(attack, quote=True) in markup
    assert attack in document.visible and short_attack in document.visible
    svg = re.findall(r"<svg\b.*?</svg>", markup, flags=re.S)
    assert len(svg) == 2 and any("&lt;b&gt;&amp;" in chart for chart in svg)
    assert all("<script>" not in chart and "<b>" not in chart for chart in svg)


def test_html_has_only_inline_resources_and_unique_svg_ids():
    view = sample_view()
    # URL 文本可以作为数据出现；资源检测不能把文本、SVG namespace 当成外部加载。
    view["report"]["summary"] += " 参考文本 https://example.test/说明"
    markup = export.to_html(view, audience="full", datasets=["D3", "D5"])
    document = Document(markup)
    assert '<!DOCTYPE svg' not in markup and '<?xml' not in markup
    assert "https://example.test/说明" in document.visible
    assert 'http://www.w3.org/2000/svg' in markup
    assert not {"script", "link", "img", "image", "iframe", "object", "embed"} & {
        tag for tag, _ in document.tags}
    for _, attrs in document.tags:
        for name, value in attrs.items():
            if name in ("src", "href", "xlink:href", "poster", "data"):
                assert not re.match(r"(?:https?:)?//", value or "", flags=re.I)
    assert not re.search(r"@import|url\(\s*['\"]?(?:https?:)?//", markup, flags=re.I)
    identifiers = [attrs["id"] for _, attrs in document.tags if "id" in attrs]
    assert identifiers and len(identifiers) == len(set(identifiers))
    assert 'font-family: system-ui' in markup and 'Microsoft YaHei' in markup
    assert 'font-family: AvelisCJK' not in markup and 'font-family: STSong-Light' not in markup


def test_html_matches_pdf_saved_values_and_content_rules(monkeypatch):
    view = sample_view()
    markup = export.to_html(view, audience="full")
    text = Document(markup).visible
    pdf_text = "\n".join(_story_text(_pdf_story(monkeypatch, view, audience="full")))
    expected = ("指标变化分析", "核心指标", "125.30万元", "140.00万元", "-14.70万元", "-10.5%",
                "结论", "关键发现", "判断与建议", "还没查清", "关键数据", "附录", "口径版本：version-1",
                "其余成员两期合计为净增长+13.70万元。", "共 25 行，显示前 10 行 · 结果被截断")
    assert all(value in text and value in pdf_text for value in expected)
    for value in ("-14.47万元", "+13.70万元", "-13.93万元", "成员甲", "成员乙", "日历因素"):
        assert value in text
    assert "口径与数据方面，已保存的检查未发现异常。" not in text
    assert len([tag for tag, _ in Document(markup).tags if tag == "svg"]) == 3


def test_html_dataset_selection_and_table_limits_are_shared_with_pdf():
    view = sample_view()
    selected = view["datasets"][4]
    selected["columns"] += ["额外一", "额外二", "额外三", "额外四", "第七列"]
    selected["rows"] = [[f"成员{i}", i * 1000, "甲", "乙", "丙", "丁", "不可输出"] for i in range(1, 13)]
    selected["row_count"] = 12

    markup = export.to_html(view, audience="full", datasets=["D2", "D5", "D5"])
    body = markup.split('<section class="appendix">', 1)[0]
    text = Document(body).visible
    assert "D5 另一个可选指标" in text and "D2 主要成员的变化" not in text
    assert "指标甲分组对比" not in text and "1000" in text and "1,000" not in text
    assert "成员10" in text and "成员11" not in text
    assert "第七列" not in text and "不可输出" not in text
    assert "共 12 行，显示前 10 行" in text
    assert len([tag for tag, _ in Document(body).tags if tag == "svg"]) == 2


@pytest.mark.parametrize("style,value,expected", [
    ("cn", "1899.1256", "1899.13"), ("intl", "1899.1256", "1899.13"),
    ("cn", "12500", "1.25万"), ("intl", "12500", "12.50K"),
])
def test_exports_keep_grounded_number_format_with_and_without_units(style, value, expected, monkeypatch):
    monkeypatch.setattr(grounding, "get_settings", lambda: SimpleNamespace(number_style=style))
    view = sample_view()
    selected = view["datasets"][4]
    selected["columns"] = ["成员", "带单位", "无单位"]
    selected["rows"] = [["成员甲", value, value], ["成员乙", "0", "0"]]
    selected["row_count"] = 2
    units = {"带单位": {"kind": "level", "unit": "单位甲"}, "无单位": {"kind": "level"}}
    selected["units"] = selected["meta"]["units"] = units
    source = {**selected, "rows": [dict(zip(selected["columns"], selected["rows"][0]))]}
    grounded = grounding.render([
        {"dataset": "D5", "column": "带单位"}, "；", {"dataset": "D5", "column": "无单位"},
    ], {"D5": source}, None)
    assert grounded["errors"] == []
    assert grounded["text"] == expected + "单位甲；" + expected
    assert export._table_data(selected)[1] == ["成员甲", expected + "单位甲", expected]

    documents = [export.to_markdown(view, appendix=False, datasets=["D5"]),
                 Document(export.to_html(view, datasets=["D5"])).visible,
                 "\n".join(_story_text(_pdf_story(monkeypatch, view, datasets=["D5"])))]
    for document in documents:
        assert expected + "单位甲" in document
        assert "1,899" not in document


def test_exports_keep_method_warning_separate_from_report_verdict(monkeypatch):
    view = sample_view()
    problem = "方法描述中有未引用的数值"
    view["report"]["method"] = "核对 9 月的查询结果。"
    view["report"]["fields"] = {"method": {"status": "unverified", "errors": [problem]}}
    view["verification"].update(unverified_fields=1, fields=[
        {"field": "method", "status": "unverified", "problems": [problem]},
    ])

    documents = [export.to_markdown(view, audience="full"),
                 Document(export.to_html(view, audience="full")).visible,
                 "\n".join(_story_text(_pdf_story(monkeypatch, view, audience="full")))]
    for document in documents:
        assert "来源全部通过，含未核实的推断，1 处报告字段未通过" in document
        assert "分析方法：" + problem in document
        assert "有未通过的事实或字段" not in document
    assert "有 1 处数字未通过系统校验，请谨慎使用。" in export.to_markdown(view)


def test_html_reuses_nearby_level_chart_suppression():
    view = sample_view()
    view["report"]["causes"] = []
    view["datasets"][4]["rows"] = [["本期", 100], ["对比期", 109]]

    markup = export.to_html(view, datasets=["D5"])

    tags = [tag for tag, _ in Document(markup).tags]
    assert "svg" not in tags and tags.count("table") == 1


def test_html_keeps_core_levels_dark_and_changes_use_pdf_sign_colors():
    view = sample_view()
    view["datasets"][0]["rows"] = [["1253000", "1400000", "147000", "-0.105"]]
    markup = export.to_html(view)
    assert '<strong class="metric-value ">125.30万元</strong>' in markup
    assert '<strong class="metric-value ">140.00万元</strong>' in markup
    assert '<strong class="metric-value positive">+14.70万元</strong>' in markup
    assert '<strong class="metric-value negative">-10.5%</strong>' in markup
    assert export.POSITIVE in markup and export.NEGATIVE in markup and export.BLUE in markup
    assert "max-width: 880px" in markup and "@media (max-width: 600px)" in markup
    assert "grid-template-columns: 1fr" in markup and "@media print" in markup
    assert "break-inside: avoid" in markup and "background: transparent" in markup


def test_html_does_not_mutate_saved_view_and_resolves_legacy_audience():
    view = sample_view()
    original = deepcopy(view)
    assert "附录" in Document(export.to_html(view, appendix=True)).visible
    assert "附录" not in Document(export.to_html(view, audience="brief", appendix=True)).visible
    assert "附录" in Document(export.to_html(view, audience="full", appendix=False)).visible
    assert view == original
    with pytest.raises(ValueError, match="audience"):
        export.to_html(view, audience="unknown")


@pytest.mark.parametrize("audience", ["brief", "full"])
def test_direct_html_renderer_enforces_restricted_dataset_boundary(audience):
    from avelis.analysis.export_html import to_html

    view = sample_view()
    selected = view["datasets"][4]
    selected.update(restricted=True, title="不可交付标题", sql="SELECT '不可交付查询'",
                    rows=[["不可交付行", 12345]], meta={"sql": "SELECT '不可交付元数据'"})
    original = deepcopy(view)

    text = Document(to_html(view, audience=audience, datasets=["D5"])).visible

    assert "不可交付" not in text
    assert (export.RESTRICTED_NOTE in text) is (audience == "full")
    assert view == original
