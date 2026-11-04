"""导出的系统数字后缀随语言变化，报告存档与核验仍使用中文原值。"""

from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest
from reportlab.graphics.shapes import Drawing, String
from reportlab.platypus import Paragraph

from avelis.analysis import export, grounding
from avelis.analysis.figures import dataset_charts
from avelis.i18n import locale_context
from tests.unit.test_analysis_export import _flowables, _pdf_story, sample_view


def comparison_view():
    units, columns = {}, ["区域"]
    for name, unit in (("金额", {"kind": "level", "unit": "元"}), ("比率", {"kind": "ratio"})):
        columns += [name, f"{name}·对比期", f"{name}·变化", f"{name}·变化率"]
        units.update(
            {
                name: unit,
                f"{name}·对比期": unit,
                f"{name}·变化": {**unit, "kind": "delta", "of_ratio": unit["kind"] == "ratio"},
                f"{name}·变化率": {"kind": "rate_change"},
            }
        )
    dataset = {
        "ref": "D1",
        "seq": 1,
        "title": "区域指标对比",
        "kind": "governed",
        "columns": columns,
        "rows": [
            ["北", 20, 15, 5, 1 / 3, 0.2, 0.15, 0.05, 1 / 3],
            ["南", 10, 15, -5, -1 / 3, 0.1, 0.15, -0.05, -1 / 3],
        ],
        "meta": {"units": units, "intent": {"group_by": ["区域"], "compare": "previous_period"}},
    }
    rendered = grounding.render(
        ["变化 ", {"dataset": "D1", "column": "比率·变化", "row": 1}],
        {"D1": {**dataset, "units": units, "rows": [dict(zip(columns, row, strict=True)) for row in dataset["rows"]]}},
        None,
    )
    assert not rendered["errors"]
    view = sample_view()
    view["datasets"] = [dataset]
    view["report"] = {
        "title": dataset["title"],
        "summary": rendered["text"],
        "fields": {"summary": rendered},
        "claims": [{**rendered, "type": "fact"}],
    }
    return view


@pytest.mark.parametrize("locale,suffix", [("zh-CN", "个百分点"), ("zh-TW", "個百分點"), ("en", " pp")])
@pytest.mark.parametrize("audience", ["brief", "full"])
def test_export_localizes_ratio_delta_without_changing_saved_report(monkeypatch, locale, suffix, audience):
    view = comparison_view()
    before = deepcopy(view)
    with locale_context(locale):
        outputs = [
            export.to_markdown(view, audience=audience, datasets=["D1"]),
            export.to_html(view, audience=audience, datasets=["D1"]),
            export.sources_csv(view).decode("utf-8-sig"),
        ]
        story = _pdf_story(monkeypatch, view, audience=audience, datasets=["D1"])
        pdf_text = []
        for item in _flowables(story):
            if isinstance(item, Paragraph):
                pdf_text.append(item.getPlainText())
            if isinstance(item, Drawing):
                pdf_text.extend(shape.text for shape in item.contents if isinstance(shape, String))
        outputs.append("\n".join(pdf_text))
        assert (
            grounding.display(Decimal("0.05"), grounding._unit({"kind": "delta", "of_ratio": True})) == "+5.0个百分点"
        )
    for output in outputs:
        assert f"+5.0{suffix}" in output
        if locale != "zh-CN":
            assert "个百分点" not in output
    assert view == before


@pytest.mark.parametrize(
    "locale,wan,yi", [("zh-CN", "万", "亿"), ("zh-TW", "萬", "億"), ("en", "×10,000", "×100,000,000")]
)
def test_export_localizes_compact_system_suffixes(monkeypatch, locale, wan, yi):
    monkeypatch.setattr(grounding, "get_settings", lambda: SimpleNamespace(number_style="cn"))
    with locale_context(locale):
        assert export._formatted(12500, {"kind": "level", "unit": "件"}) == f"1.25{wan}件"
        assert export._formatted(125000000, {"kind": "level"}) == f"1.25{yi}"
        assert export._axis(12500) == f"1.25{wan}"
        assert export._axis_label(125000000, {"kind": "level"}) == f"1.25{yi}"
        assert export._axis_label(125, {"kind": "ratio"}) == f"1.3{wan}%"
        assert grounding.display(Decimal("12500"), grounding._unit({"kind": "level"})) == "1.25万"


@pytest.mark.parametrize("locale", ["en", "zh-TW"])
@pytest.mark.parametrize("unit", ["万", "亿", "个百分点"])
def test_business_units_labels_and_free_text_are_not_translated(locale, unit):
    report = {
        "title": f"1{unit}",
        "claims": [
            {
                "text": f"1{unit} 1{unit}",
                "segments": [
                    {"kind": "label", "text": f"1{unit}"},
                    {"kind": "text", "text": " "},
                    {"kind": "value", "text": f"1{unit}", "unit": unit},
                ],
            }
        ],
    }
    with locale_context(locale):
        assert export._formatted(1, {"kind": "level", "unit": unit}) == f"1{unit}"
        assert export._localized_report(report) == report
        assert export._localized_number("1个百分点", "元") == "1个百分点"


def test_comparison_multiples_export_all_panels(monkeypatch):
    view = comparison_view()
    spec = dataset_charts(view["datasets"][0])[0]
    assert spec["kind"] == "multiples"
    assert len(spec["series"]) == 2
    story = _pdf_story(monkeypatch, view, audience="full", datasets=["D1"])
    drawings = [item for item in _flowables(story) if isinstance(item, Drawing)]
    assert len(drawings) == 2
    markup = export.to_html(view, audience="full", datasets=["D1"])
    markdown = export.to_markdown(view, audience="full", datasets=["D1"])
    assert markdown.count("```mermaid") == 2
    for series in spec["series"]:
        assert series["name"] in markup
        assert series["name"] in markdown
    texts = {shape.text for drawing in drawings for shape in drawing.contents if isinstance(shape, String)}
    assert {series["name"] for series in spec["series"]} <= texts


@pytest.mark.parametrize("locale", ["zh-CN", "zh-TW", "en"])
def test_comparison_eight_panels_generate_real_paginated_pdf(locale):
    view = comparison_view()
    dataset = view["datasets"][0]
    for index in range(7):
        name = f"指标{index}"
        columns = [name, f"{name}·对比期", f"{name}·变化", f"{name}·变化率"]
        dataset["columns"].extend(columns)
        for column, kind in zip(columns, ["level", "level", "delta", "rate_change"], strict=True):
            dataset["meta"]["units"][column] = {"kind": kind, "unit": f"单位{index}"}
        for row in dataset["rows"]:
            row.extend([20, 15, 5, 1 / 3])
    with locale_context(locale):
        spec = dataset_charts(dataset)[0]
        assert len(spec["series"]) == 8 and spec["dropped_series"] == 1
        pdf = export.to_pdf(view, audience="full", datasets=["D1"])
    assert pdf.startswith(b"%PDF")
    assert pdf.count(b"/Type /Page\n") >= 2
