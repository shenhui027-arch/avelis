"""新图种沿用已物化规格，SVG、PDF 和 Markdown 导出均保留原始数据。"""

from copy import deepcopy

import pytest
from reportlab.graphics import renderSVG
from reportlab.graphics.shapes import Circle, Line, Polygon, Rect, String
from reportlab.lib.colors import HexColor

from avelis.analysis import export
from avelis.analysis.figures import _shows, report_figures


def series(column="a", values=None, *, mark="bar", axis=0, unit=None):
    return {
        "column": column,
        "label": column,
        "name": column,
        "values": [10, 30, 20] if values is None else values,
        "mark": mark,
        "axis": axis,
        "unit": unit or {"kind": "count", "additive": True},
        "polarity": None,
        "other": False,
    }


def chart(kind):
    spec = {
        "kind": kind,
        "orientation": "vertical",
        "categories": ["Alpha", "Beta", "Gamma"],
        "series": [series()],
        "title": None,
        "reason": "dimension",
        "x": {"column": "member", "label": "成员", "role": "dimension"},
        "kpis": [],
        "marks": None,
        "highlight": None,
        "others": None,
        "omitted_categories": None,
    }
    if kind == "area":
        spec["series"][0]["mark"] = "line"
    elif kind == "combo":
        spec["series"].append(series("b", [0.1, 0.3, 0.2], mark="line", axis=1, unit={"kind": "ratio"}))
    elif kind == "pareto":
        spec["series"][0]["values"] = [30, 20, 10]
        spec["cumulative"] = [0.5, 5 / 6, 1]
    elif kind == "waterfall":
        spec["series"][0].update(values=[10, -5, 20], unit={"kind": "delta"})
        spec["waterfall"] = {"start": {"label": "Previous", "value": 100}, "end": {"label": "Current", "value": 125}}
    elif kind == "heatmap":
        spec["series"][0]["values"] = []
        spec["heatmap"] = {
            "y": {"column": "member", "label": "成员"},
            "y_categories": ["Delta", "Epsilon"],
            "cells": [[0, 0, 1], [1, 0, 2], [2, 0, None], [0, 1, 3], [1, 1, 4], [2, 1, 5]],
            "min": 1,
            "max": 5,
        }
    elif kind == "scatter":
        spec["series"] = [series("x", [-3, -2, -1]), series("y", [1, 2, 3]), series("size", [1, 4, 9])]
        spec["points"] = [
            {"name": name, "x": x, "y": y, "size": size}
            for name, x, y, size in zip(spec["categories"], [-3, -2, -1], [1, 2, 3], [1, 4, 9], strict=True)
        ]
    return spec


def view():
    return {
        "report": {"title": "结果", "summary": "已整理结果。"},
        "verification": {},
        "datasets": [
            {
                "ref": "D1",
                "kind": "calc",
                "title": "结果",
                "columns": ["member", "a"],
                "rows": [["Alpha", 10], ["Beta", 30], ["Gamma", 20]],
                "row_count": 3,
                "meta": {"units": {"a": {"kind": "count"}}},
                "inputs": [],
            }
        ],
    }


@pytest.mark.parametrize("kind", ["area", "combo", "pareto", "waterfall", "heatmap", "scatter"])
@pytest.mark.parametrize("audience", ["brief", "full"])
def test_new_kinds_export_svg_pdf_html_and_markdown_without_mutating_spec(monkeypatch, kind, audience):
    spec = chart(kind)
    original = deepcopy(spec)
    figure = {"ref": "D1", "title": "结果", "chart": spec}
    monkeypatch.setattr(export, "report_figures", lambda *args: [figure])
    drawing = export._spec_chart(spec, "Helvetica")
    assert "<svg" in renderSVG.drawToString(drawing)
    assert export.to_pdf(view(), audience=audience).startswith(b"%PDF")
    assert "<svg" in export.to_html(view(), audience=audience)
    assert "|" in export.to_markdown(view(), audience=audience)
    assert spec == original


def test_area_has_translucent_fill_and_keeps_missing_value_gaps():
    spec = chart("area")
    drawing = export._spec_chart_data(spec, "Helvetica")
    fills = [shape for shape in drawing.contents if isinstance(shape, Polygon)]
    assert len(fills) == 2 and all(shape.fillOpacity == 0.15 for shape in fills)
    assert len([shape for shape in drawing.contents if isinstance(shape, Circle)]) == 3
    spec["series"][0]["values"][1] = None
    assert not any(isinstance(shape, Polygon) for shape in export._spec_chart_data(spec, "Helvetica").contents)


@pytest.mark.parametrize("kind", ["combo", "pareto"])
def test_dual_axis_has_bars_line_points_and_percentage_ticks(kind):
    drawing = export._spec_chart_data(chart(kind), "Helvetica")
    assert len([shape for shape in drawing.contents if isinstance(shape, Rect)]) == 3
    points = [shape for shape in drawing.contents if isinstance(shape, Circle)]
    assert len(points) == 3 and points[-1].cy > 45
    assert any("%" in shape.text for shape in drawing.contents if isinstance(shape, String))
    assert any(isinstance(shape, Line) and shape.strokeWidth == 1.5 for shape in drawing.contents)


def test_waterfall_draws_start_end_and_floating_member_bars():
    drawing = export._spec_chart_data(chart("waterfall"), "Helvetica")
    bars = [shape for shape in drawing.contents if isinstance(shape, Rect)]
    assert len(bars) == 5
    assert bars[1].y > bars[0].y and bars[2].y > bars[0].y
    assert bars[2].fillColor != bars[1].fillColor
    assert bars[-1].height > bars[0].height


@pytest.mark.parametrize(
    "polarity,increase,decrease",
    [
        (None, export.POSITIVE, export.NEGATIVE),
        ("up_good", export.POSITIVE, export.NEGATIVE),
        ("down_good", export.NEGATIVE, export.POSITIVE),
    ],
)
def test_waterfall_delta_colors_follow_series_polarity(polarity, increase, decrease):
    spec = chart("waterfall")
    spec["series"][0]["polarity"] = polarity
    drawing = export._spec_chart_data(spec, "Helvetica")
    bars = [shape for shape in drawing.contents if isinstance(shape, Rect)]
    assert [bar.fillColor for bar in bars] == [
        HexColor(export.VIZ_PALETTE[0]),
        HexColor(increase),
        HexColor(decrease),
        HexColor(increase),
        HexColor(export.VIZ_PALETTE[0]),
    ]


def test_heatmap_draws_cells_missing_value_and_separate_member_axis():
    spec = chart("heatmap")
    drawing = export._spec_chart_data(spec, "Helvetica")
    cells = [shape for shape in drawing.contents if isinstance(shape, Rect)]
    assert len(cells) == 6 and cells[0].fillOpacity < cells[-1].fillOpacity
    assert cells[2].fillColor != cells[0].fillColor
    assert _shows(spec, "Delta")


def test_heatmap_export_dims_cells_outside_highlight():
    spec = chart("heatmap")
    spec["highlight"] = [str(spec["categories"][0])]
    drawing = export._spec_chart_data(spec, "Helvetica")
    cells = [shape for shape in drawing.contents if isinstance(shape, Rect)]
    kept = {cell.fillColor for index, cell in enumerate(cells) if spec["heatmap"]["cells"][index][0] == 0}
    dimmed = {cell.fillColor for index, cell in enumerate(cells) if spec["heatmap"]["cells"][index][0] != 0}
    assert HexColor(export.VIZ_DIM) in dimmed and HexColor(export.VIZ_DIM) not in kept


def test_scatter_uses_numeric_axes_and_bubble_size():
    drawing = export._spec_chart_data(chart("scatter"), "Helvetica")
    points = [shape for shape in drawing.contents if isinstance(shape, Circle)]
    assert len(points) == 3
    assert points[0].cx < points[1].cx < points[2].cx
    assert points[0].cy < points[1].cy < points[2].cy
    assert points[0].r < points[1].r < points[2].r


def test_scatter_bubble_size_uses_magnitude_like_browser():
    spec = chart("scatter")
    for point, size in zip(spec["points"], [-100, 1, 4]):
        point["size"] = size
    drawing = export._spec_chart_data(spec, "Helvetica")
    points = [shape for shape in drawing.contents if isinstance(shape, Circle)]
    assert points[0].r > points[2].r > points[1].r


@pytest.mark.parametrize("kind,mark", [("area", "line"), ("pareto", "bar")])
def test_mermaid_supported_fallback_retains_raw_values(kind, mark):
    spec = chart(kind)
    lines = export._mermaid_chart({"chart": spec})
    assert "xychart-beta" in lines
    assert any(line.startswith(f"    {mark} [") for line in lines)
    assert str(spec["series"][0]["values"][0]) in "\n".join(lines)


@pytest.mark.parametrize("kind", ["combo", "waterfall", "heatmap", "scatter"])
def test_mermaid_unsupported_kinds_leave_full_table(kind):
    assert export._mermaid_chart({"chart": chart(kind)}) == []


@pytest.mark.parametrize("kind", ["area", "combo", "pareto", "waterfall", "heatmap", "scatter"])
def test_figures_routes_explicit_new_kind_through_chart_contract(kind, monkeypatch):
    from avelis.analysis import figures

    spec = chart(kind)
    source = view()
    source["report"]["figures"] = [{"dataset": "D1", "kind": kind}]
    monkeypatch.setattr(figures, "dataset_charts", lambda dataset, preference: [spec])
    assert report_figures(source)[0]["chart"] == spec
