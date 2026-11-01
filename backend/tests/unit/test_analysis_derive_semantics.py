"""derive 的引用取值、量纲登记与报告显示回归，不连接数据库。"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from avelis.analysis.verify import ground_report
from tests.unit.test_analysis_toolbox import governed_meta, make_box


def source(box, columns, values, units, *, summary=None, summary_units=None, assumption=False):
    meta = {
        **governed_meta(box, columns[0]),
        "units": units,
        "summary": summary or {},
        "summary_units": summary_units or {},
        "assumption": assumption,
    }
    ds = box._dataset("governed", "登记量纲的输入", columns, [values], meta=meta)
    return f"D{ds.seq}"


def derive(box, formula, values):
    reply = json.loads(box.dispatch("derive", {"label": "派生结果", "formula": formula, "values": values}))
    assert "error" not in reply, reply
    ds = box.datasets()[reply["dataset"]]
    return reply, ds.meta["summary_units"]["value"]


def rendered_result(box, ref, *, claim_type="calc", direction=None):
    datasets = {
        key: {
            "kind": ds.kind,
            "columns": ds.columns,
            "rows": [dict(zip(ds.columns, row)) for row in ds.rows],
            "truncated": ds.truncated,
            **ds.meta,
        }
        for key, ds in box.datasets().items()
    }
    return ground_report(
        {
            "title": "派生计算",
            "summary": "结果见计算结论",
            "conclusion": "insufficient_evidence",
            "claims": [
                {"type": claim_type, "parts": ["结果为", {"dataset": ref, "summary": "value", "direction": direction}]}
            ],
        },
        datasets,
        None,
    )


def test_period_difference_retains_registered_unit_and_renders_as_delta(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    ref = source(
        box, ["本期", "前期"], ["10412", "9000"], {name: {"kind": "level", "unit": "单"} for name in ("本期", "前期")}
    )
    result, unit = derive(
        box,
        "sep - aug",
        [
            {"name": "sep", "dataset": ref, "column": "本期"},
            {"name": "aug", "dataset": ref, "column": "前期"},
        ],
    )
    assert Decimal(result["summary"]["value"]) == Decimal("1412")
    assert unit == {"kind": "delta", "unit": "单"}
    report, verification = rendered_result(box, result["dataset"], direction="up")
    assert report["claims"][0]["text"] == "结果为1412单"
    assert verification["ok"] and verification["status"] == "all_passed"


def test_ratio_of_nested_same_unit_differences_renders_as_percentage(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    ref = source(
        box, list("abcd"), ["1843", "1000", "3000", "2000"], dict.fromkeys("abcd", {"kind": "level", "unit": "单"})
    )
    result, unit = derive(box, "(a - b) / (c - d)", [{"name": name, "dataset": ref, "column": name} for name in "abcd"])
    assert Decimal(result["summary"]["value"]) == Decimal("0.843")
    assert unit == {"kind": "ratio"}
    report, verification = rendered_result(box, result["dataset"])
    assert report["claims"][0]["text"] == "结果为84.3%"
    assert verification["ok"]


def test_ratio_with_zero_minus_registered_delta_matches_observed_formula(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    ref = source(
        box,
        ["前期水平", "本期水平", "线上变化"],
        ["10000", "9157", "-1000"],
        {
            "前期水平": {"kind": "level", "unit": "单"},
            "本期水平": {"kind": "level", "unit": "单"},
            "线上变化": {"kind": "delta", "unit": "单"},
        },
    )
    result, unit = derive(
        box,
        "(aug - sep) / (0 - online_change)",
        [
            {"name": "aug", "dataset": ref, "column": "前期水平"},
            {"name": "sep", "dataset": ref, "column": "本期水平"},
            {"name": "online_change", "dataset": ref, "column": "线上变化"},
        ],
    )
    assert unit == {"kind": "ratio"}
    assert not box.datasets()[result["dataset"]].meta["assumption"]
    report, verification = rendered_result(box, result["dataset"])
    assert report["claims"][0]["text"] == "结果为84.3%" and verification["ok"]


@pytest.mark.parametrize("kind", ["ratio", "share"])
@pytest.mark.parametrize("formula", ["r * x", "x * r"])
def test_registered_nested_summary_ratio_times_level_preserves_unit(kind, formula, monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    ref = source(
        box,
        ["水平"],
        ["2000"],
        {"水平": {"kind": "level", "unit": "单"}},
        summary={"nested": {"ratio": "0.843"}},
        summary_units={"nested.ratio": {"kind": kind}},
    )
    result, unit = derive(
        box,
        formula,
        [
            {"name": "r", "dataset": ref, "summary": "nested.ratio"},
            {"name": "x", "dataset": ref, "column": "水平"},
        ],
    )
    assert unit == {"kind": "level", "unit": "单"}
    report, verification = rendered_result(box, result["dataset"])
    assert report["claims"][0]["text"] == "结果为1686单" and verification["ok"]


@pytest.mark.parametrize("selection", ["column", "summary"])
def test_input_scale_is_applied_once_and_not_reapplied_to_chained_summary(selection, monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    scaled = {"kind": "level", "unit": "单", "scale": "1000"}
    ref = source(
        box,
        ["本期", "前期"],
        ["1.899", "487"],
        {"本期": scaled, "前期": {"kind": "level", "unit": "单"}},
        summary={"period": {"current": "1.899"}},
        summary_units={"period.current": scaled},
    )
    current = {"column": "本期"} if selection == "column" else {"summary": "period.current"}
    result, unit = derive(
        box,
        "sep - aug",
        [
            {"name": "sep", "dataset": ref, **current},
            {"name": "aug", "dataset": ref, "column": "前期"},
        ],
    )
    assert Decimal(result["summary"]["value"]) == Decimal("1412")
    assert unit == {"kind": "delta", "unit": "单"}
    chained, chained_unit = derive(
        box,
        "x + 0",
        [
            {"name": "x", "dataset": result["dataset"], "summary": "value"},
        ],
    )
    assert Decimal(chained["summary"]["value"]) == Decimal("1412")
    assert chained_unit == unit
    report, verification = rendered_result(box, chained["dataset"])
    assert report["claims"][0]["text"] == "结果为+1412单" and verification["ok"]


def test_unregistered_column_units_are_not_guessed_from_labels(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    ref = source(box, ["订单数", "订单数·对比期"], ["2000", "1000"], {})
    result, unit = derive(
        box,
        "sep - aug",
        [
            {"name": "sep", "dataset": ref, "column": "订单数"},
            {"name": "aug", "dataset": ref, "column": "订单数·对比期"},
        ],
    )
    assert unit == {"kind": "unknown"}
    report, verification = rendered_result(box, result["dataset"])
    assert report["claims"][0]["text"] == "结果为1000" and verification["ok"]


@pytest.mark.parametrize(
    "selection, problem",
    [
        ({"summary": "hidden"}, "没有登记汇总路径"),
        ({"summary": "missing"}, "汇总里没有"),
        ({"summary": "nested.value", "match": {"分类": "甲"}}, "汇总值不能带 match 或 exclude"),
        ({"summary": "nested.value", "exclude": {"分类": "甲"}}, "汇总值不能带 match 或 exclude"),
        ({"column": "数值", "summary": "nested.value"}, "column 与 summary 必须且只能写一个"),
        ({}, "column 与 summary 必须且只能写一个"),
        ({"column": "缺失列"}, "没有列"),
        ({"column": "数值", "match": {"分类": "乙"}}, "没有选出行"),
    ],
)
def test_invalid_input_selection_is_rejected_without_creating_dataset(selection, problem, monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    ref = source(
        box,
        ["数值", "分类"],
        ["1899", "甲"],
        {"数值": {"kind": "level", "unit": "单"}},
        summary={"nested": {"value": "1899"}, "hidden": "1899"},
        summary_units={"nested.value": {"kind": "level", "unit": "单"}, "missing": {"kind": "level", "unit": "单"}},
    )
    result = json.loads(
        box.dispatch(
            "derive",
            {
                "label": "无效输入",
                "formula": "x",
                "values": [{"name": "x", "dataset": ref, **selection}],
            },
        )
    )
    assert problem in result["error"]
    assert list(box.datasets()) == [ref]


@pytest.mark.parametrize("value", [None, "NaN", "Infinity", "不是数字"])
def test_registered_summary_must_still_be_a_finite_number(value, monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    ref = source(
        box, ["数值"], ["1"], {}, summary={"value": value}, summary_units={"value": {"kind": "level", "unit": "单"}}
    )
    result = json.loads(
        box.dispatch(
            "derive",
            {
                "label": "无效汇总",
                "formula": "x",
                "values": [{"name": "x", "dataset": ref, "summary": "value"}],
            },
        )
    )
    assert "不是有限数值" in result["error"]
    assert list(box.datasets()) == [ref]


def test_non_whitelisted_constants_remain_assumptions_and_cannot_be_facts(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    ref = source(box, ["数值"], ["1000"], {"数值": {"kind": "level", "unit": "单"}})
    result, unit = derive(box, "x * 0.3", [{"name": "x", "dataset": ref, "column": "数值"}])
    assert unit == {"kind": "unknown"}
    assert box.datasets()[result["dataset"]].meta["assumption"]
    report, verification = rendered_result(box, result["dataset"])
    assert verification["status"] == "failed" and not verification["ok"]
    assert any("假设计算" in problem for problem in report["claims"][0]["errors"])
    _, inference_verification = rendered_result(box, result["dataset"], claim_type="inference")
    assert inference_verification["ok"]


def test_assumption_flag_survives_reusing_a_registered_derived_summary(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    ref = source(box, ["数值"], ["1000"], {"数值": {"kind": "level", "unit": "单"}})
    assumption, _ = derive(box, "x * 0.3", [{"name": "x", "dataset": ref, "column": "数值"}])
    result, unit = derive(box, "x + 0", [{"name": "x", "dataset": assumption["dataset"], "summary": "value"}])
    assert unit == {"kind": "unknown"} and box.datasets()[result["dataset"]].meta["assumption"]
    _, verification = rendered_result(box, result["dataset"])
    assert not verification["ok"]


@pytest.mark.parametrize("formula, expected", [("x / 1000", "1.899"), ("x * 100", "189900")])
def test_whitelisted_scaling_constants_keep_unit_without_assumption(formula, expected, monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    ref = source(box, ["数值"], ["1899"], {"数值": {"kind": "level", "unit": "单"}})
    result, unit = derive(box, formula, [{"name": "x", "dataset": ref, "column": "数值"}])
    assert Decimal(result["summary"]["value"]) == Decimal(expected)
    assert unit == {"kind": "level", "unit": "单"}
    assert not box.datasets()[result["dataset"]].meta["assumption"]
