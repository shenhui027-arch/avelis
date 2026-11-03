"""面向决策者的正文与面向分析师的依据导出必须保持独立。"""

from __future__ import annotations

import re
from copy import deepcopy
from html.parser import HTMLParser

import pytest
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib.colors import HexColor
from reportlab.platypus import PageBreak, Paragraph

from avelis.analysis import export
from avelis.analysis.figures import key_datasets
from tests.unit.test_analysis_export import _flowables, _pdf_story, _story_text, draw_dataset, sample_view

APPROVED_FOOTER = "报告中的数字均来自系统内已审核的指标口径，可在 Avelis 中查看计算依据。"
EXPLORE_FOOTER = "部分数字来自临时查询，尚未经过指标口径审核。"
REFERENCE = re.compile(r"(?<![A-Za-z0-9_])D[1-9]\d*(?![A-Za-z0-9_])")
TECHNICAL = re.compile(r"SELECT|SQL|受治理|计算|探索查询|上下文|口径版本|来源已校验|核验", re.IGNORECASE)


def _drawing_text(item):
    if isinstance(item, String):
        yield item.text
    for child in getattr(item, "contents", ()):
        yield from _drawing_text(child)


def _pdf_text(story):
    # 图形文字按其正文位置插入，避免被误认为附录内容。
    texts = []
    for item in story:
        texts.extend(_story_text([item]))
        for flowable in _flowables([item]):
            if isinstance(flowable, Drawing):
                texts.extend(_drawing_text(flowable))
    return "\n".join(texts)


@pytest.fixture(params=["markdown", "pdf", "html"])
def render(request, monkeypatch):
    if request.param == "markdown":
        return lambda view, **kwargs: export.to_markdown(view, **kwargs)
    if request.param == "html":

        def html_text(view, **kwargs):
            class Text(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.parts = []
                    self.hidden = False

                def handle_starttag(self, tag, attrs):
                    if tag in ("style", "title"):
                        self.hidden = True

                def handle_endtag(self, tag):
                    if tag in ("style", "title"):
                        self.hidden = False

                def handle_data(self, data):
                    if not self.hidden:
                        self.parts.append(data)

            parsed = Text()
            parsed.feed(export.to_html(view, **kwargs))
            return "\n".join(parsed.parts)

        return html_text
    return lambda view, **kwargs: _pdf_text(_pdf_story(monkeypatch, view, **kwargs))


def _assert_brief(text):
    # 唯一保留的“计算”是用户指定的白话脚注“计算依据”。
    without_footer = text.replace(APPROVED_FOOTER, "")
    assert not TECHNICAL.search(without_footer)
    assert not REFERENCE.search(text)
    for phrase in ("附录", "用到的数据", "数字来源", "查询明细", "version-", "推断，未核实"):
        assert phrase not in text


@pytest.mark.parametrize("audience", ["brief", "full"])
def test_restricted_export_discards_stale_rows_metadata_and_report_text(render, audience):
    view = sample_view()
    restricted = view["datasets"][0]
    restricted.update(
        restricted=True,
        title="受限标题不可交付",
        columns=["受限列不可交付"],
        rows=[["受限行不可交付"]],
        sql="SELECT '受限查询不可交付'",
        metric_version_ids=["受限版本不可交付"],
        meta={"summary": "受限汇总不可交付", "sql": "SELECT '受限元数据不可交付'"},
    )
    view["report"] = {
        "title": "中立报告",
        "summary": "可见摘要",
        "claims": [
            {"type": "fact", "refs": ["D1"], "text": "受限结论不可交付"},
            {
                "type": "inference",
                "refs": ["D3"],
                "text": "可见判断",
                "premise": "旧前提不可交付",
                "errors": ["受限前提核验原值不可交付"],
                "details": {"premise": {"text": "受限前提不可交付", "refs": ["D1"]}},
            },
        ],
        "fields": {"unexplained": {"text": "受限未知项不可交付", "refs": ["D1"]}},
        "unexplained": "受限未知项不可交付",
        "causes": [
            {
                "type": "member",
                "label": "受限原因不可交付",
                "evidence": ["D1"],
                "entity": {"member": "受限成员不可交付"},
                "contribution": {"text": "受限贡献不可交付"},
            }
        ],
    }
    view["verification"]["conclusion"] = {"status": "unverified", "problems": ["受限极值与范围不可交付"]}
    original = deepcopy(view)

    text = render(view, audience=audience, datasets=["D1"])

    assert "不可交付" not in text
    assert "可见判断" in text and "可见摘要" in text
    assert "[无访问权限]" not in text
    assert (export.RESTRICTED_NOTE in text) is (audience == "full")
    if audience == "brief":
        _assert_brief(text)
    assert view == original


def test_restricted_datasets_are_ineligible_for_charts_tables_and_core_metrics():
    view = sample_view()
    for dataset in view["datasets"]:
        dataset["restricted"] = True
    font, _ = export.pdf_font()

    assert key_datasets(view) == []
    assert export._core_metrics(view) == []
    assert draw_dataset(view["datasets"][2], font) is None
    assert export._table_data(view["datasets"][2]) == []


def test_restricted_records_keep_visible_verification_indices_aligned(render):
    view = sample_view()
    view["datasets"][0]["restricted"] = True
    view["report"] = {
        "title": "报告",
        "claims": [
            {"type": "fact", "refs": ["D1"], "text": "已失权"},
            {"type": "fact", "refs": ["D3"], "text": "可见结论", "errors": ["结论未通过"]},
        ],
        "causes": [
            {"type": "factor", "label": "已失权", "evidence": ["D1"]},
            {"type": "factor", "label": "可见原因", "evidence": ["D3"], "errors": ["原因未通过"]},
        ],
    }
    view["verification"] = {
        "claims": [
            {"index": 0, "status": "verified"},
            {"index": 1, "status": "unverified", "problems": ["结论未通过"]},
        ],
        "causes": [
            {"index": 0, "status": "verified"},
            {"index": 1, "status": "unverified", "problems": ["原因未通过"]},
        ],
    }

    text = render(view, audience="brief")

    assert "有 2 处数字未通过系统校验" in text
    assert "已失权" not in text


@pytest.mark.parametrize(
    "kwargs", [{}, {"audience": "brief"}, {"appendix": False}, {"audience": "brief", "appendix": True}]
)
def test_default_and_brief_have_only_plain_language_body(render, kwargs):
    text = render(sample_view(), **kwargs)
    _assert_brief(text)
    assert APPROVED_FOOTER in text
    assert "待验证" in text
    assert "依据：其余成员两期合计为净增长+13.70万元。" in text
    assert "建议这样验证：复核已保存的分组对比-12.00万元" in text
    for label in ("成员甲", "成员乙", "日历因素", "指标丁"):
        assert label in text


def test_brief_pdf_footer_is_small_gray_and_has_no_appendix_page_break(monkeypatch):
    story = _pdf_story(monkeypatch, sample_view())
    assert not any(isinstance(item, PageBreak) for item in _flowables(story))
    footer = next(
        item for item in _flowables(story) if isinstance(item, Paragraph) and item.getPlainText() == APPROVED_FOOTER
    )
    assert footer.style.fontSize <= 8
    assert footer.style.textColor == HexColor(export.GRAY)


@pytest.mark.parametrize("audience", ["brief", "full"])
def test_audience_builds_actual_pdf_bytes(audience):
    assert export.to_pdf(sample_view(), audience=audience).startswith(b"%PDF")


@pytest.mark.parametrize("kwargs", [{"audience": "full"}, {"appendix": True}, {"audience": "full", "appendix": False}])
def test_full_and_appendix_compatibility_keep_used_evidence(render, kwargs):
    text = render(sample_view(), **kwargs)
    assert all(phrase in text for phrase in ("附录", "数据检查", "用到的数据", "查询明细"))
    assert "数字来源" not in text and "可追溯 ≠ 可复现" not in text
    assert "SELECT 1253000 AS metric_a, 1400000 AS metric_b" in text
    assert "口径版本" in text and "version-1" in text
    assert REFERENCE.search(text)
    assert "推断，未核实" in text and "前提：" in text and "验证方式：" in text
    assert APPROVED_FOOTER not in text


def test_full_appendix_only_lists_body_references_and_selected_data(render):
    def dataset(seq, kind="governed"):
        return {
            "ref": f"D{seq}",
            "kind": kind,
            "title": f"技术标题{seq}",
            "columns": ["member", "value"],
            "rows": [["甲", 10], ["乙", 30]],
            "row_count": 200 if seq == 4 else 2,
            "truncated": seq == 4,
            "sql": f"SELECT {seq} AS query_marker",
            "metric_version_ids": [f"version-{seq}"],
            "meta": {
                "units": {"value": {"kind": "delta", "unit": "元"}},
                "metric_names": [f"显示指标{seq}"],
                "summary": "检查记录正常",
            },
        }

    def segment(seq):
        return {"kind": "value", "ref": f"D{seq}", "path": f"D{seq}.value[#1]", "text": "10元", "raw": "10"}

    saved = [
        dataset(i, "calc" if i == 2 else "explore" if i == 4 else "context" if i == 8 else "governed")
        for i in range(1, 10)
    ]
    saved[1]["inputs"] = ["D6"]
    view = {
        "datasets": saved,
        "verification": {"ok": True},
        "report": {
            "title": "中立报告",
            "fields": {
                "summary": {"segments": [segment(9)]},
                "unexplained": {"segments": [segment(4)]},
                "method": {"segments": [{"kind": "text", "text": "采用已保存的分析方法"}, segment(5)]},
            },
            "claims": [
                {"type": "fact", "text": "正文结论", "refs": ["D1"]},
                {"type": "recommendation", "text": "建议", "details": {"premise": {"segments": [segment(3)]}}},
                {"type": "fact", "text": "不重复检查原文", "refs": ["D8"]},
            ],
            "causes": [
                {"type": "factor", "label": "原因甲", "evidence": ["D2"], "contribution": {"segments": [segment(2)]}}
            ],
        },
    }
    original = deepcopy(view)
    text = render(view, audience="full", datasets=["D7"])
    appendix = text.split("附录", 1)[1]
    assert appendix.index("分析方法") < appendix.index("数据检查") < appendix.index("用到的数据")
    listing, queries = appendix.split("查询明细", 1)
    assert set(REFERENCE.findall(appendix)) == {"D1", "D2", "D3", "D4", "D7", "D9"}
    for seq in (1, 2, 3, 4, 7, 9):
        assert f"显示指标{seq}" in listing
    for seq in (1, 3, 4, 7, 9):
        assert f"SELECT {seq} AS query_marker" in queries and f"version-{seq}" in queries
    for seq in (2, 5, 6, 8):
        assert f"SELECT {seq} AS query_marker" not in queries
    assert "技术标题" not in appendix
    assert all(source in listing for source in ("指标口径", "临时查询，未经口径审核", "计算"))
    assert "200" in listing and "是" in listing and "否" in listing
    assert "检查记录正常" in listing and "不重复检查原文" not in text
    assert "数字来源" not in text and "可追溯 ≠ 可复现" not in text
    assert view == original
    brief = render(view, audience="brief", datasets=["D7"])
    assert "附录" not in brief and "SELECT" not in brief


@pytest.mark.parametrize("source", ["segment", "direct", "dependency", "summary", "summary_direct"])
def test_brief_warns_when_used_numbers_include_temporary_query(render, source):
    view = sample_view()
    if source == "segment":
        view["report"]["claims"][0]["segments"][0]["explore"] = True
    elif source == "direct":
        view["datasets"][0]["kind"] = "explore"
    elif source == "dependency":
        # 被引用的 D2 经 D5 依赖 D18；循环依赖不能导致递归失控。
        view["datasets"][1]["inputs"] = ["D5"]
        view["datasets"][4]["inputs"] = ["D2", "D18"]
        view["datasets"][17]["kind"] = "explore"
    elif source == "summary":
        view["verification"]["sources"]["with_explore"] = 1
    else:
        view["verification"]["sources"]["direct"]["explore"] = 1
    text = render(view, audience="brief")
    _assert_brief(text)
    assert EXPLORE_FOOTER in text and APPROVED_FOOTER not in text


def test_unused_temporary_query_does_not_change_brief_assurance(render):
    view = sample_view()
    view["datasets"][17]["kind"] = "explore"
    text = render(view)
    assert APPROVED_FOOTER in text and EXPLORE_FOOTER not in text


def test_brief_plain_language_applies_to_saved_body_text_and_dataset_notes(render):
    view = sample_view()
    view["report"].update(
        title="受治理指标 D1 的计算分析",
        summary="来源已校验 D1，计算结果需结合上下文。",
        unexplained="核验 D2 的探索查询与口径版本仍需补充。",
    )
    claim = view["report"]["claims"][2]
    claim["text"] = "根据 D2 受治理计算结果推断，未核实。"
    claim["details"]["premise"]["segments"] = [{"kind": "text", "text": "依据 D2 的口径版本计算。"}]
    claim["details"]["validation"]["segments"] = [{"kind": "text", "text": "核验 D3 的探索查询上下文。"}]
    text = render(view)
    _assert_brief(text)
    assert "待验证" in text and APPROVED_FOOTER in text


def test_brief_reports_failed_numbers_once_without_raw_check_details(render):
    view = sample_view()
    detail = "原始校验错误 SELECT D404 口径版本 version-internal"
    view["verification"].update(
        unverified=2,
        unverified_fields=1,
        unverified_causes=1,
        fields=[{"field": "summary", "status": "unverified", "problems": [detail]}],
        conclusion={"status": "unverified", "problems": [detail]},
    )
    view["verification"]["claims"][0].update(status="unverified", problems=[detail])
    view["verification"]["claims"][1].update(status="unverified", problems=[detail])
    view["verification"]["causes"][0].update(status="unverified", problems=[detail])
    view["report"]["claims"][0]["errors"] = [detail]
    view["report"]["causes"][0]["errors"] = [detail]
    text = render(view)
    _assert_brief(text)
    assert "有 5 处数字未通过系统校验，请谨慎使用。" in text
    assert "原始校验错误" not in text and "version-internal" not in text


def test_saved_record_errors_are_counted_when_summary_counts_are_missing(render):
    view = sample_view()
    view["report"]["claims"][2]["details"]["premise"]["errors"] = ["原始前提错误 D404"]
    view["report"]["causes"][0]["errors"] = ["原始原因错误 D405"]
    text = render(view)
    _assert_brief(text)
    assert "有 2 处数字未通过系统校验，请谨慎使用。" in text
    assert "原始前提错误" not in text and "原始原因错误" not in text


def _dimension_view():
    view = sample_view()
    d = view["datasets"][4]
    columns = ["group_code", "kind_code", "note_code", "unknown_code", "指标甲·对比期"]
    d.update(
        title="指标甲 2026-09-01~2026-09-30 按group_code、kind_code 对比2026-08-01~2026-08-31",
        columns=columns,
        rows=[["甲", "甲类", "注释甲", "未知甲", 100], ["乙", "乙类", "注释乙", "未知乙", 200]],
    )
    d["units"] = d["meta"]["units"] = {"指标甲·对比期": {"kind": "level"}}
    d["intent"]["group_by"] = columns[:-1]
    labels = {
        "metric_names": ["指标甲"],
        "dimensions": [
            {"name": "group_code", "column": "b.group_code", "label": "分组名称", "中文名": "备用名称"},
            {"name": "kind_code", "column": "b.kind_code", "中文名": "类别名称"},
            {"name": "note_code", "column": "b.note_code"},
            {"name": "unknown_code", "column": "b.unknown_code"},
        ],
        "column_docs": {
            "group_code": {"comment": "备用注释", "status": "ok"},
            "note_code": {"comment": "注释显示名", "status": "ok"},
        },
    }
    d["meta"]["export_labels"] = labels
    return view, d, labels


@pytest.mark.parametrize("saved_labels", ["export_labels", "meta"])
def test_brief_dataset_titles_and_headers_use_dimension_metadata(render, saved_labels):
    view, dataset, labels = _dimension_view()
    if saved_labels == "meta":
        dataset["meta"].pop("export_labels")
        dataset["meta"].update(deepcopy(labels))
    original = deepcopy(view)
    text = render(view, audience="brief", datasets=["D5"])
    _assert_brief(text)
    assert "指标甲 · 按分组名称、类别名称、注释显示名、unknown_code" in text
    assert all(label in text for label in ("分组名称", "类别名称", "注释显示名", "unknown_code", "指标甲·对比期"))
    assert "备用名称" not in text and "备用注释" not in text
    assert "group_code" not in text and "kind_code" not in text and "note_code" not in text
    assert dataset["title"] not in text
    assert view == original, "显示名映射不能改写已保存的数据集"


def test_brief_fixed_calculation_columns_have_neutral_chinese_headers(render):
    view = sample_view()
    dataset = view["datasets"][4]
    columns = ["period", "days", "weekend_days", "holidays", "value", "daily"]
    dataset.update(
        kind="calc",
        title="日历对比",
        columns=columns,
        rows=[["本期", 30, 8, 1, 100, 3.33], ["对比期", 31, 10, 0, 110, 3.55]],
    )
    dataset["units"] = dataset["meta"]["units"] = {"value": {"kind": "level"}, "daily": {"kind": "level"}}
    dataset["intent"]["group_by"] = ["period"]
    text = render(view, audience="brief", datasets=["D5"])
    _assert_brief(text)
    assert all(name in text for name in ("天数", "周末天数", "节假日", "合计", "日均"))
    assert "期间" in text or "本期" in text
    assert not re.search(r"\b(?:period|days|weekend_days|holidays|value|daily)\b", text)


def test_full_preserves_original_dataset_titles_and_raw_column_names(render):
    view, dataset, _ = _dimension_view()
    text = render(view, audience="full", datasets=["D5"])
    assert dataset["title"] in text
    assert "group_code" in text and "kind_code" in text and "note_code" in text


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        ("member", ["current", "previous"]),
        ("period", ["本期", "对比期"]),
    ],
)
def test_brief_chart_translates_only_fixed_period_values(column, expected):
    dataset = {
        "kind": "calc",
        "columns": [column, "value"],
        "rows": [["current", 10], ["previous", 30]],
        "meta": {"units": {"value": {"kind": "level"}}},
    }
    font, _ = export.pdf_font()
    chart = draw_dataset(dataset, font, brief=True)
    labels = list(_drawing_text(chart))
    assert all(label in labels for label in expected)
    excluded = ["本期", "对比期"] if column == "member" else ["current", "previous"]
    assert not any(label in labels for label in excluded)


def test_brief_calc_title_uses_segment_dimension_metadata_without_group_by(render):
    view, dataset, _ = _dimension_view()
    dataset["kind"] = "calc"
    dataset["intent"]["group_by"] = []
    dataset["meta"]["args"] = {"segment": "group_code"}
    text = render(view, audience="brief", datasets=["D5"])
    assert "指标甲 · 按分组名称" in text
    assert "group_code" not in text


def test_brief_preserves_spaces_between_text_and_numeric_segments(render):
    view = sample_view()
    view["report"].pop("title")
    numeric = deepcopy(view["report"]["claims"][0]["segments"][0])
    numeric.update(text="10", raw="10")
    segments = [{"kind": "text", "text": "指标 A = "}, numeric, {"kind": "text", "text": " units"}]
    view["report"]["fields"]["title"] = {"segments": deepcopy(segments)}
    view["report"]["claims"][2]["details"]["premise"] = {"segments": deepcopy(segments)}
    text = render(view, audience="brief")
    assert "指标 A = 10 units" in text
    assert "依据：指标 A = 10 units" in text
