"""分图面板上限、响应校验及八面板导出的离线回归。"""

import re
from copy import deepcopy
from xml.etree import ElementTree

import pytest
from pydantic import ValidationError
from reportlab.graphics.shapes import Drawing

from avelis.analysis import export, service
from avelis.analysis.figures import dataset_charts
from avelis.api.analysis import _dataset_detail
from avelis.api.schemas import AnalysisDatasetDetailOut, AnalysisDatasetOut, AnalysisRunOut, ChartSeries
from avelis.charts import MAX_PANELS
from tests.unit.test_chart_review_followups import figure_view, monthly_dataset
from tests.unit.test_overfit_batch3 import _dataset, _run, _Session


def panel_dataset(measures, members):
    data = monthly_dataset()
    columns = [f"指标{i}" for i in range(measures)]
    data["columns"] = ["月份", "区域", *columns]
    data["units"] = {column: {"kind": "count", "additive": True} for column in columns}
    data["meta"]["units"] = data["units"]
    # 保留非字典序的输入成员顺序，防止轮流分配时另行排序。
    data["rows"] = [
        [period, f"区域{member}", *[member + i for i in range(measures)]]
        for period in ("2026-02", "2026-01")
        for member in reversed(range(members))
    ]
    # 从 meta 重新推导列契约，不沿用旧指标的 column_roles。
    del data["column_roles"]
    return data


@pytest.mark.parametrize("measures,members,count,dropped", [(2, 3, 6, 0), (3, 4, 8, 4), (10, 3, 8, 22)])
def test_chart_panel_round_robin_and_response_schemas(measures, members, count, dropped):
    data = panel_dataset(measures, members)
    before = deepcopy(data)
    chart = dataset_charts(data)[0]
    assert chart["kind"] == "multiples"
    assert len(chart["series"]) == count and chart["dropped_series"] == dropped
    assert [s["axis"] for s in chart["series"]] == list(range(count))
    assert [s["name"] for s in chart["series"]] == [
        f"指标{i} / 区域{member}" for member in reversed(range(members)) for i in range(measures)
    ][:MAX_PANELS]
    assert {s["column"] for s in chart["series"]} == set(data["columns"][2 : 2 + MAX_PANELS])
    for series in chart["series"]:
        assert ChartSeries.model_validate(series).axis == series["axis"]

    # 使用实际视图函数的结果，覆盖 run 的 datasets / figures 及数据集详情响应。
    stored = _dataset(kind="calc", columns=data["columns"], meta=data["meta"])
    stored.rows = data["rows"]
    session = _Session(datasets=[stored])
    run = AnalysisRunOut.model_validate(service.run_view(session, _run(figure_view(data)["report"])))
    summary = AnalysisDatasetOut.model_validate(run.datasets[0])
    detail = AnalysisDatasetDetailOut.model_validate(_dataset_detail(stored, session=session))
    assert summary.charts[0].model_dump() == detail.charts[0].model_dump() == run.figures[0].chart.model_dump()
    assert [s.axis for s in detail.charts[0].series] == list(range(count))
    assert summary.columns == detail.columns == data["columns"]
    assert summary.rows == detail.rows == data["rows"]
    assert data == before


@pytest.mark.parametrize("axis", [-1, MAX_PANELS, MAX_PANELS + 1])
def test_chart_series_schema_rejects_axis_outside_panel_limit(axis):
    series = dataset_charts(panel_dataset(2, 3))[0]["series"][0]
    with pytest.raises(ValidationError, match="axis"):
        ChartSeries.model_validate({**series, "axis": axis})


@pytest.mark.parametrize("audience", ["brief", "full"])
def test_eight_chart_panels_export_within_pdf_pages_and_svg_bounds(monkeypatch, audience):
    data = panel_dataset(3, 4)
    view = figure_view(data)
    chart = dataset_charts(data)[0]
    drawn = []
    original = Drawing.drawOn

    def draw_on_page(self, canvas, x, y, *args, **kwargs):
        page_width, page_height = canvas._pagesize
        assert 0 <= x <= page_width - self.width
        assert 0 <= y <= page_height - self.height
        drawn.append((self, canvas.getPageNumber()))
        return original(self, canvas, x, y, *args, **kwargs)

    monkeypatch.setattr(Drawing, "drawOn", draw_on_page)
    pdf = export.to_pdf(view, audience=audience, datasets=["D1"])
    assert pdf.startswith(b"%PDF")
    assert len(drawn) == 8 and len({page for _, page in drawn}) >= 3
    assert len(re.findall(rb"/Type\s*/Page\b", pdf)) >= 3

    font, _ = export.pdf_font()
    drawing = export._spec_chart(chart, font, title=data["title"])
    panels = [item for item in drawing.contents if isinstance(item, Drawing)]
    assert len(panels) == 8
    assert drawing.height == sum(panel.height for panel in panels) + 22
    for panel in panels:
        left, bottom, right, top = panel.getBounds()
        assert 0 <= left < right <= drawing.width
        assert 0 <= bottom < top <= drawing.height
    markup = export.to_html(view, audience=audience, datasets=["D1"])
    svg = re.search(r"<svg\b.*?</svg>", markup, re.S).group()
    root = ElementTree.fromstring(svg)
    assert list(map(float, root.attrib["viewBox"].split())) == [0, 0, drawing.width, drawing.height]
    assert all(series["name"] in svg for series in chart["series"])
    assert all(f'<th scope="col">{column}</th>' in markup for column in data["columns"])
