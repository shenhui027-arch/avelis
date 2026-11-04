"""改图规格的导出：标注与高亮由同一规格驱动，不重新排序或计算图中数值。"""

from copy import deepcopy

import pytest
from reportlab.graphics.shapes import Circle, Drawing, Line, Polygon, Rect, String
from reportlab.lib.colors import HexColor

from avelis.analysis import export
from avelis.analysis.figures import dataset_charts, report_chart_preference, report_figures
from tests.unit.test_analysis_export import _pdf_story
from tests.unit.test_analysis_export_audience import _pdf_text
from tests.unit.test_analysis_export_html import Document


def chart_view(kind="bar", *, time=False):
    preference = {
        "kind": kind,
        "title": "结果图",
        "marks": ["max", "min", "average"],
        "highlight": ["Beta"],
    }
    return {
        "report": {
            "kind": "chart",
            "dataset": "D1",
            "preference": preference,
            "title": "改图",
            "summary": "已更新图表。",
            "figures": [{"dataset": "D1", **preference}],
        },
        "datasets": [
            {
                "ref": "D1",
                "kind": "calc",
                "title": "结果",
                "columns": ["category", "amount"],
                "rows": [["Alpha", 10], ["Beta", 30], ["Gamma", 20]],
                "row_count": 3,
                "inputs": [],
                "meta": {
                    "units": {"amount": {"kind": "level", "unit": "元", "additive": True}},
                    "column_roles": {
                        "category": {"role": "time" if time else "dimension", "label": "类目"},
                        "amount": {"role": "measure", "label": "金额"},
                    },
                },
            }
        ],
    }


def shapes(drawing):
    for item in drawing.contents:
        yield item
        if isinstance(item, Drawing):
            yield from shapes(item)


@pytest.mark.parametrize("audience", ["brief", "full"])
@pytest.mark.parametrize("format", ["markdown", "pdf", "html"])
def test_all_exports_include_marks_highlight_and_mean_definition(monkeypatch, audience, format):
    source = chart_view()
    before = deepcopy(source)
    spec = report_figures(source)[0]["chart"]
    if format == "pdf":
        assert export.to_pdf(source, audience=audience).startswith(b"%PDF")
        text = _pdf_text(_pdf_story(monkeypatch, source, audience=audience))
    elif format == "html":
        text = Document(export.to_html(source, audience=audience)).visible
    else:
        text = export.to_markdown(source, audience=audience)
    assert "最高点" in text and "最低点" in text and "所示值平均" in text
    assert "平均标注表示各系列所示非空值的算术平均，不是加权平均。" in text
    assert "突出显示：Beta；其余类目已弱化。" in text
    if format == "markdown":
        assert export._chart_annotation_notes(spec)[0] in text
        assert export._chart_annotation_notes(spec)[1] in text
        assert export._chart_annotation_notes(spec)[2] in text
        assert text.index("```mermaid") < text.index("最高点：")
    assert source == before


@pytest.mark.parametrize("kind,time", [("bar", False), ("line", True)])
def test_cartesian_draws_extrema_points_average_line_and_dims_other_categories(kind, time):
    source = chart_view(kind, time=time)
    spec = report_figures(source)[0]["chart"]
    before = deepcopy(spec)
    font, _ = export.pdf_font()
    drawn = list(shapes(export._spec_chart(spec, font)))
    assert len([item for item in drawn if isinstance(item, Circle) and item.r == 4]) == 2
    average = [item for item in drawn if isinstance(item, Line) and item.strokeDashArray == [4, 3]]
    assert len(average) == 1
    values = (
        [item for item in drawn if isinstance(item, Rect)]
        if kind == "bar"
        else [item for item in drawn if isinstance(item, Circle) and item.r == 2]
    )
    assert [item.fillColor for item in values].count(HexColor(export.VIZ_DIM)) == 2
    assert [item.fillColor for item in values].count(HexColor(export.VIZ_PALETTE[0])) == 1
    assert spec == before


def test_average_geometry_uses_the_materialized_value_without_recalculation():
    spec = report_figures(chart_view())[0]["chart"]
    font, _ = export.pdf_font()
    original = next(
        item
        for item in shapes(export._spec_chart(spec, font))
        if isinstance(item, Line) and item.strokeDashArray == [4, 3]
    )
    altered = deepcopy(spec)
    next(mark for mark in altered["marks"] if mark["type"] == "average")["value"] = 25
    shifted = next(
        item
        for item in shapes(export._spec_chart(altered, font))
        if isinstance(item, Line) and item.strokeDashArray == [4, 3]
    )
    assert shifted.x1 > original.x1


def test_donut_highlights_sectors_and_rings_extrema_with_textual_average():
    spec = report_figures(chart_view("donut"))[0]["chart"]
    font, _ = export.pdf_font()
    drawn = list(shapes(export._spec_chart(spec, font)))
    sectors = [item for item in drawn if isinstance(item, Polygon)]
    assert len(sectors) == 3
    assert [item.fillColor for item in sectors].count(HexColor(export.VIZ_DIM)) == 2
    assert len([item for item in drawn if isinstance(item, Circle) and item.r == 4]) == 2
    assert any("所示值平均" in item.text for item in drawn if isinstance(item, String))


def test_multiples_keep_same_column_annotations_on_their_series(monkeypatch):
    source = chart_view()
    spec = report_figures(source)[0]["chart"]
    first, second = deepcopy(spec["series"][0]), deepcopy(spec["series"][0])
    first["name"], second["name"] = "系列甲", "系列乙"
    second["values"] = [4, 5, 6]
    spec.update(
        kind="multiples",
        series=[first, second],
        marks=[
            {"type": "max", "series": "amount", "series_index": 0, "x": spec["categories"][0], "value": 30},
            {"type": "min", "series": "amount", "series_index": 1, "x": spec["categories"][0], "value": 4},
            {"type": "average", "series": "amount", "series_index": 1, "x": None, "value": 5},
        ],
    )
    panel = export._chart_panel(spec, 1)
    assert [mark["type"] for mark in panel["marks"]] == ["min", "average"]
    assert all(mark["series_index"] == 0 for mark in panel["marks"])
    monkeypatch.setattr(export, "report_figures", lambda *args: [{"ref": "D1", "title": "分图", "chart": spec}])
    assert export.to_pdf(source, audience="full").startswith(b"%PDF")
    text = _pdf_text(_pdf_story(monkeypatch, source, audience="full"))
    assert text.count("最高点") == 1 and text.count("最低点") == 1
    assert "所示值平均：系列乙" in text and "所示值平均：系列甲" not in text


def test_brief_period_labels_are_remapped_for_marks_and_highlight_together():
    source = chart_view()
    dataset = source["datasets"][0]
    dataset["columns"][0] = "period"
    dataset["rows"] = [["current", 10], ["previous", 30]]
    dataset["meta"]["column_roles"]["period"] = dataset["meta"]["column_roles"].pop("category")
    spec = dataset_charts(dataset)[0]
    spec.update(
        marks=[{"type": "max", "series": "amount", "series_index": 0, "x": "previous", "value": 30}],
        highlight=["previous"],
    )
    figure = export._display_figure({"ref": "D1", "title": "结果", "chart": spec}, dataset, brief=True)
    assert figure["chart"]["marks"][0]["x"] == "对比期"
    assert figure["chart"]["highlight"] == ["对比期"]
    assert "previous" not in figure["chart"]["categories"]


def test_only_chart_reports_apply_the_saved_preference_to_dataset_projection():
    report = chart_view()["report"]
    assert report_chart_preference(report, "D1") == report["preference"]
    assert report_chart_preference(report, "D2") is None
    assert report_chart_preference({**report, "kind": "analysis"}, "D1") is None


def test_legacy_text_contribution_keeps_its_readable_chart_target():
    source = chart_view()
    source["report"] = {"causes": [{"type": "member", "contribution": "原有文字说明", "evidence": ["D1"]}]}
    assert report_figures(source)[0]["ref"] == "D1"


def test_stacked_export_anchors_extrema_to_segment_ends_and_lists_average_as_text():
    """堆叠柱：极值点画在本段末端；平均线画不到本段位置，不画线，改由图下注释给出。"""
    spec = {
        "kind": "stacked_bar",
        "orientation": "vertical",
        "reason": "composition",
        "title": None,
        "x": {"column": "month", "label": "月份", "role": "time", "grain": "month"},
        "categories": ["1月", "2月"],
        "series": [
            {
                "name": "A",
                "column": "value",
                "label": "值",
                "axis": 0,
                "mark": "bar",
                "unit": {"kind": "level", "unit": "元"},
                "polarity": None,
                "values": [100, 100],
            },
            {
                "name": "B",
                "column": "value",
                "label": "值",
                "axis": 0,
                "mark": "bar",
                "unit": {"kind": "level", "unit": "元"},
                "polarity": None,
                "values": [20, 30],
            },
        ],
        "kpis": [],
        "others": None,
        "dropped_series": 0,
        "marks": [
            {"type": "max", "series": "value", "series_index": 1, "x": "2月", "value": 30},
            {"type": "average", "series": "value", "series_index": 1, "x": None, "value": 25},
        ],
        "highlight": None,
    }
    font, _ = export.pdf_font()
    drawn = list(shapes(export._spec_chart_data(spec, font)))
    assert not [item for item in drawn if isinstance(item, Line) and item.strokeDashArray == [4, 3]]
    rings = [item for item in drawn if isinstance(item, Circle) and item.r == 4]
    bars = [item for item in drawn if isinstance(item, Rect) and item.fillColor == HexColor(export.VIZ_PALETTE[1])]
    february = max(bars, key=lambda rect: rect.x)
    assert len(rings) == 1 and rings[0].cy == pytest.approx(february.y + february.height)
    assert any("25" in note for note in export._chart_annotation_notes(spec, include_marks=False))
