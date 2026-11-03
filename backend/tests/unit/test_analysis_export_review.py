"""报告导出评审回归：指标身份、单位、维度文本与趋势系列。"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from reportlab.graphics.shapes import Circle, Drawing, Rect, String
from reportlab.lib.colors import HexColor

from avelis.analysis import export, grounding
from tests.unit.test_analysis_export import _pdf_story, _value, draw_dataset, sample_view


@pytest.mark.parametrize(
    ("metric_code", "first_codes", "expected_ref"),
    [
        ("metric_target", ["metric_other"], "D2"),
        (None, ["metric_other"], "D1"),
        ("metric_target", [], "D1"),
        ("metric_target", ["metric_target"], "D1"),
    ],
)
def test_core_metrics_skip_only_explicitly_other_metrics(metric_code, first_codes, expected_ref):
    view = sample_view()
    first = view["datasets"][0]
    second = deepcopy(first)
    first["meta"]["metric_codes"] = first_codes
    first["rows"] = [["30", "20", "10", "0.5"]]
    second.update(ref="D2", seq=2, rows=[["222", "111", "111", "1"]])
    second["meta"]["metric_codes"] = ["metric_target"]
    view["datasets"] = [first, second]
    view["report"] = {"claims": [{"type": "fact", "refs": ["D1", "D2"]}]}
    if metric_code is not None:
        view["report"]["metric_code"] = metric_code

    metrics = export._core_metrics(view)

    assert {metric["ref"] for metric in metrics} == {expected_ref}
    assert metrics[0]["raw"] == (222 if expected_ref == "D2" else 30)


def test_core_metrics_select_target_group_within_multi_metric_dataset():
    view = sample_view()
    dataset = view["datasets"][0]
    columns = dataset["columns"]
    target_columns = [f"target_{column}" for column in columns]
    dataset["meta"]["units"].update({new: deepcopy(dataset["units"][old]) for old, new in zip(columns, target_columns)})
    dataset["columns"] = columns + target_columns
    dataset["rows"] = [["30", "20", "10", "0.5", "222", "111", "111", "1"]]
    dataset["meta"]["metric_codes"] = ["metric_other", "metric_target"]
    view["report"] = {"metric_code": "metric_target", "claims": [{"refs": ["D1"]}]}
    view["datasets"] = [dataset]

    assert [metric["raw"] for metric in export._core_metrics(view)] == [222, 111, 111, 1]


@pytest.mark.parametrize("audience", ["brief", "full"])
def test_cause_bars_use_each_value_segment_dataset_scale(monkeypatch, audience):
    datasets = [
        {
            "ref": "D1",
            "kind": "calc",
            "title": "原因甲",
            "columns": ["contribution"],
            "rows": [["-2"]],
            "meta": {"units": {"contribution": {"kind": "delta", "unit": "元", "scale": 10000}}},
        },
        {
            "ref": "D2",
            "kind": "calc",
            "title": "原因乙",
            "columns": ["contribution"],
            "rows": [["-10000"]],
            "units": {"contribution": {"kind": "delta", "unit": "元", "scale": 1}},
        },
        {
            "ref": "D3",
            "kind": "calc",
            "title": "原因丙",
            "columns": [],
            "rows": [],
            "meta": {"summary_units": {"total.change": {"kind": "delta", "unit": "元", "scale": 10000}}},
        },
    ]
    causes = []
    for ref, raw, text, label in [
        ("D1", "-2", "-2.00万元", "成员甲"),
        ("D2", "-10000", "-1.00万元", "成员乙"),
        ("D3", "2", "+2.00万元", "成员丙"),
    ]:
        segment = _value(ref, "contribution", raw, text)
        if ref == "D3":
            segment["path"] = "D3.summary.total.change"
        causes.append(
            {
                "type": "member",
                "entity": {"member": label},
                "evidence": ["D2", ref],
                "contribution": {"text": text, "segments": [segment]},
            }
        )
    view = {
        "report": {"title": "指标变化", "summary": "原因影响存在差异。", "causes": causes},
        "datasets": datasets,
        "verification": {"ok": True, "status": "all_passed"},
    }

    story = _pdf_story(monkeypatch, view, audience=audience)
    chart = next(item for item in story if isinstance(item, Drawing))
    bars = [item for item in chart.contents if isinstance(item, Rect)]
    ticks = {item.text for item in chart.contents if isinstance(item, String) and item.y == 5}

    assert len(bars) == 3
    assert bars[0].width == pytest.approx(2 * bars[1].width)
    assert bars[2].width == pytest.approx(bars[0].width)
    assert {"-2万", "0", "2万"} <= ticks
    assert [bar.fillColor for bar in bars] == [
        HexColor(export.NEGATIVE),
        HexColor(export.NEGATIVE),
        HexColor(export.POSITIVE),
    ]


@pytest.mark.parametrize("brief", [False, True])
def test_key_table_preserves_numeric_looking_dimensions_and_formats_registered_values(brief):
    dataset = {
        "kind": "governed",
        "columns": ["code", "scientific_code", "other_code", "amount", "rows", "rate"],
        "rows": [["000123", "1e3", "000045", "1234.5", "001200", "-0.125"]],
        "meta": {"units": {"amount": {"kind": "level"}, "rows": {"kind": "count"}, "rate": {"kind": "rate_change"}}},
    }

    assert export._table_data(dataset, brief=brief)[1] == ["000123", "1e3", "000045", "1234.5", "1200", "-12.5%"]


@pytest.mark.parametrize(
    "style,value,expected",
    [
        ("cn", 1899, "1899"),
        ("intl", 1899, "1899"),
        ("cn", -1899.1256, "-1899.13"),
        ("intl", -1899.1256, "-1899.13"),
        ("cn", 12500, "1.25万"),
        ("intl", 12500, "12.5K"),
        ("cn", 100000000, "1亿"),
        ("intl", 1000000000, "1B"),
    ],
)
def test_chart_axes_share_number_style_without_adding_grouping(style, value, expected, monkeypatch):
    monkeypatch.setattr(grounding, "get_settings", lambda: SimpleNamespace(number_style=style))
    assert export._axis(value) == expected


@pytest.mark.parametrize("column", ["指标[调整]", "summary.change"])
def test_cause_scale_uses_complete_registered_column_name(column):
    cause = {"contribution": {"segments": [_value("D1", column, "2", "+2万元")]}}
    dataset = {
        "meta": {
            "units": {"指标": {"scale": 1}, column: {"kind": "delta", "scale": 10000}},
            "summary_units": {"change": {"scale": 100}},
        }
    }
    assert export._cause_value(cause, {"D1": dataset})[0] == 20000


def test_trend_series_have_distinct_colors_matching_their_legends():
    font, _ = export.pdf_font()
    names = ["成员甲", "成员乙", "成员丙"]
    dataset = {
        "columns": ["at", "member", "value"],
        "time_column": "at",
        "meta": {"units": {"value": {"kind": "level", "unit": "元"}}},
        "rows": [
            [f"2026-09-0{day}", name, day * multiplier]
            for multiplier, name in enumerate(names, 1)
            for day in range(1, 4)
        ],
    }

    chart = draw_dataset(dataset, font)
    colors = list(dict.fromkeys(item.fillColor for item in chart.contents if isinstance(item, Circle)))
    legends = {item.text: item.fillColor for item in chart.contents if isinstance(item, String)}

    assert len(colors) == 3 and len({color.hexval() for color in colors}) == 3
    assert [legends[name] for name in names] == colors


def test_ordinary_bars_use_categorical_palette_for_both_signs():
    font, _ = export.pdf_font()
    dataset = {
        "columns": ["member", "value"],
        "rows": [["成员甲", -2], ["成员乙", 1]],
        "meta": {"units": {"value": {"kind": "delta", "unit": "元"}}},
    }

    chart = draw_dataset(dataset, font)

    assert [item.fillColor for item in chart.contents if isinstance(item, Rect)] == [
        HexColor(export.VIZ_PALETTE[0]),
        HexColor(export.VIZ_PALETTE[0]),
    ]
