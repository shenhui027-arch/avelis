"""PR #96 后续评审：多指标时间分图及导出百分点坐标轴，使用内存数据。"""

import re
from copy import deepcopy
from types import SimpleNamespace

import pytest
from reportlab.graphics.shapes import Drawing, String

from avelis.analysis import export, grounding
from avelis.analysis.dataset_projection import build_column_roles
from avelis.analysis.figures import dataset_charts, report_figures
from avelis.api.ask import _answer_result
from avelis.api.schemas import ChartSpec
from avelis.i18n import gettext, locale_context
from tests.unit.test_analysis_export import _flowables, _pdf_story, sample_view


def monthly_dataset():
    units = {
        "成交额": {"kind": "level", "unit": "元", "scale": 10000, "additive": True},
        "转化率": {"kind": "ratio", "additive": False},
    }
    meta = {"time_column": "月份", "intent": {"time_grain": "month", "group_by": ["区域"]}, "units": units}
    columns = ["月份", "区域", "成交额", "转化率"]
    return {
        "ref": "D1",
        "seq": 1,
        "title": "每月各区域成交额与转化率",
        "kind": "governed",
        "columns": columns,
        "column_roles": build_column_roles(columns, meta),
        "rows": [["2026-02", "北", 3, 0.15], ["2026-01", "南", 2, 0.2], ["2026-01", "北", 1, 0.1]],
        "units": units,
        "meta": meta,
    }


def figure_view(dataset):
    view = sample_view()
    view["datasets"] = [dataset]
    view["report"] = {"title": dataset["title"], "claims": [{"type": "fact", "text": "趋势", "refs": ["D1"]}]}
    return view


def test_monthly_region_multi_measure_ask_and_report_keep_all_table_series():
    dataset = monthly_dataset()
    before = deepcopy(dataset)
    answer = SimpleNamespace(status="answered", result=dataset, intent=dataset["meta"]["intent"], evidence={})
    result = _answer_result(answer, None)
    spec = result["charts"][0]
    assert spec == dataset_charts(dataset)[0] == report_figures(figure_view(dataset))[0]["chart"]
    assert ChartSpec.model_validate(spec).model_dump(mode="json") == spec
    assert spec["kind"] == "multiples" and spec["dropped_series"] == 0
    assert spec["categories"] == ["2026-01", "2026-02"]
    # SpecTable 按 series 生成表头及数值列；默认规格必须同时带两个指标及各区域。
    assert [s["name"] for s in spec["series"]] == ["成交额 / 北", "转化率 / 北", "成交额 / 南", "转化率 / 南"]
    assert [s["values"] for s in spec["series"]] == [[1, 3], [0.1, 0.15], [2, None], [0.2, None]]
    assert [s["axis"] for s in spec["series"]] == list(range(4))
    assert all(s["unit"] == dataset["units"][s["column"]] for s in spec["series"])
    assert set(result["columns"]) == set(export._table_data(dataset)[0]) == set(dataset["columns"])
    assert dataset == before


@pytest.mark.parametrize("same_units", [False, True])
def test_monthly_region_caps_panels_but_keeps_each_measure_even_with_same_units(same_units):
    dataset = monthly_dataset()
    if same_units:
        dataset["units"]["转化率"] = dataset["units"]["成交额"]
    for i in range(5):
        column = f"金额{i}"
        dataset["columns"].append(column)
        dataset["column_roles"][column] = {"role": "measure", "label": column}
        dataset["units"][column] = dataset["units"]["成交额"]
        for row in dataset["rows"]:
            row.append(i)
    spec = dataset_charts(dataset)[0]
    assert {s["column"] for s in spec["series"]} == set(dataset["columns"][2:])
    assert len(spec["series"]) == 8 and spec["dropped_series"] == 6
    assert [s["column"] for s in spec["series"][:7]] == dataset["columns"][2:]


def test_monthly_region_multi_measure_counts_actual_member_omissions():
    dataset = monthly_dataset()
    dataset["rows"] = [[period, f"区域{i}", i, i / 10] for period in ("2026-01", "2026-02") for i in range(9)]
    spec = dataset_charts(dataset)[0]
    # 18 个原始“指标 / 成员”只展示 8 个，截掉的“其他”不再覆盖其两个成员。
    assert spec["others"] is None and spec["dropped_series"] == 10
    assert len(spec["series"]) == 8
    assert [s["name"] for s in spec["series"]] == [
        f"{column} / 区域{i}" for i in range(8, 4, -1) for column in ("成交额", "转化率")
    ]
    assert all(not s["other"] for s in spec["series"])


def test_monthly_region_single_measure_keeps_original_candidates():
    dataset = monthly_dataset()
    dataset["columns"].remove("转化率")
    dataset["rows"] = [row[:3] for row in dataset["rows"]]
    default, candidate = dataset_charts(dataset)
    assert [default["kind"], candidate["kind"]] == ["line", "stacked_bar"]
    assert [s["name"] for s in default["series"]] == ["北", "南"]
    assert [s["values"] for s in default["series"]] == [[1, 3], [2, None]]


@pytest.mark.parametrize("kind", ["line", "stacked_bar", "grouped_bar", "multiples"])
def test_saved_monthly_multi_measure_figures_keep_title_and_all_measures(kind):
    view = figure_view(monthly_dataset())
    request = {"dataset": "D1", "kind": kind, "title": "保留的标题"}
    view["report"]["figures"] = [request]
    before = deepcopy(view)
    for selection in (None, ["D1"]):
        figure = report_figures(view, selection)[0]
        assert figure["chart"]["kind"] == "multiples"
        assert figure["chart"]["title"] == figure["title"] == request["title"]
        assert {s["column"] for s in figure["chart"]["series"]} == {"成交额", "转化率"}
    assert view == before


@pytest.mark.parametrize("audience", ["brief", "full"])
def test_monthly_multi_measure_pdf_panels_and_html_table_cover_both_units(monkeypatch, audience):
    dataset = monthly_dataset()
    view = figure_view(dataset)
    story = _pdf_story(monkeypatch, view, audience=audience, datasets=["D1"])
    panels = [item for item in _flowables(story) if isinstance(item, Drawing)]
    assert len(panels) == 4
    texts = [{s.text for s in panel.contents if isinstance(s, String)} for panel in panels]
    assert "成交额 / 北" in texts[0] and "1.00万元" in texts[0]
    assert "转化率 / 北" in texts[1] and "10.0%" in texts[1]
    markup = export.to_html(view, audience=audience, datasets=["D1"])
    svg = re.search(r"<svg\b.*?</svg>", markup, re.S).group()
    assert all(name in svg for name in ("成交额 / 北", "成交额 / 南", "转化率 / 北", "转化率 / 南"))
    assert '<th scope="col">成交额</th>' in markup and '<th scope="col">转化率</th>' in markup


@pytest.mark.parametrize("locale", ["zh-CN", "zh-TW", "en"])
@pytest.mark.parametrize("value", [0.05, -0.05, 0])
def test_ratio_delta_axis_reuses_grounding_in_each_locale(locale, value):
    meta = {"kind": "delta", "of_ratio": True, "scale": 10000}
    with locale_context(locale):
        source = grounding.display(grounding.number(value), grounding._unit(meta)).removeprefix("+")
        expected = source.replace("个百分点", gettext("个百分点"))
        assert export._axis_label(value, meta) == expected
        assert source == {0.05: "5.0个百分点", -0.05: "-5.0个百分点", 0: "0.0个百分点"}[value]


@pytest.mark.parametrize(
    "value,meta,expected",
    [
        (0.05, {"kind": "ratio"}, "5%"),
        (0.05, {"kind": "rate_change", "unit": "元", "scale": 10000}, "5%"),
        (0.05, {"kind": "share", "scale": 10000}, "5%"),
        (0.05, {"kind": "ratio", "unit": "元/人", "scale": 10000}, "0.05"),
        (0.05, {"kind": "delta", "of_ratio": True, "unit": "元/人", "scale": 100}, "5"),
        (0.05, {"kind": "delta", "scale": 100}, "5"),
        (0.05, {"kind": "level", "unit": "元", "scale": 10000}, "500"),
        (12, {"kind": "count"}, "12"),
        (0.05, {"kind": "unknown"}, "0.05"),
    ],
)
def test_export_other_axis_units_keep_original_format(value, meta, expected):
    assert export._axis_label(value, meta) == expected


@pytest.mark.parametrize("audience", ["brief", "full"])
def test_ratio_comparison_pdf_and_html_axes_match_point_labels(monkeypatch, audience):
    columns = ["区域", "转化率", "转化率·上一期", "转化率·变化", "转化率·变化率"]
    units = {
        "转化率": {"kind": "ratio"},
        "转化率·上一期": {"kind": "ratio"},
        "转化率·变化": {"kind": "delta", "of_ratio": True},
        "转化率·变化率": {"kind": "rate_change"},
    }
    dataset = {
        "ref": "D1",
        "seq": 1,
        "title": "区域转化率对比",
        "kind": "governed",
        "columns": columns,
        "rows": [["北", 0.2, 0.15, 0.05, 1 / 3], ["南", 0.1, 0.15, -0.05, -1 / 3]],
        "meta": {"units": units, "intent": {"group_by": ["区域"], "compare": "previous_period"}},
    }
    view = figure_view(dataset)
    story = _pdf_story(monkeypatch, view, audience=audience, datasets=["D1"])
    chart = next(item for item in _flowables(story) if isinstance(item, Drawing))
    ticks = {s.text for s in chart.contents if isinstance(s, String) and s.y == 31}
    labels = {s.text for s in chart.contents if isinstance(s, String) and s.fontSize == 6}
    assert {"-5.0个百分点", "0.0个百分点", "5.0个百分点"} <= ticks
    assert {"-5.0个百分点", "+5.0个百分点"} <= labels
    assert "0.05" not in ticks and "-0.05" not in ticks
    markup = export.to_html(view, audience=audience, datasets=["D1"])
    svg = re.search(r"<svg\b.*?</svg>", markup, re.S).group()
    assert ">5.0个百分点</text>" in svg and ">+5.0个百分点</text>" in svg
    assert ">-5.0个百分点</text>" in svg and ">0.05</text>" not in svg
