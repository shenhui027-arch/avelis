"""分布统计与业务合计共用可加性规则，整体比率只使用已有的同版本组成列。"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from avelis.analysis import calc
from avelis.ask.service import Catalog
from avelis.models import MetricVersion
from tests.unit.test_analysis_toolbox import governed_meta, make_box


@pytest.mark.parametrize(
    "expression,kind",
    [("SUM(value)", "ratio"), ("AVG(value)", "level"), ("COUNT(DISTINCT id)", "count")],
)
def test_nonadditive_describe_retains_distribution_without_business_total(monkeypatch, expression, kind):
    box = make_box(monkeypatch=monkeypatch)
    version = SimpleNamespace(kind="base", expression=expression, value_kind=kind)
    meta = {
        **governed_meta(box, "value", version=version),
        "units": {"value": {"kind": kind}},
    }
    source = box._dataset("governed", "distribution", ["value"], [["0.5"], ["0.5"]], meta=meta)

    result = json.loads(box.describe(f"D{source.seq}", "value"))

    assert "error" not in result, result
    assert "sum" not in result["columns"] and "sum" not in result["summary"]
    assert result["summary"]["mean"] == result["summary"]["median"] == "0.5"
    assert result["summary"]["count"] == 2
    assert "未生成合计" in result["summary"]["note"]
    assert "未加权平均" in result["summary"]["note"]
    assert "分子、分母分别汇总后重算" in result["summary"]["note"]
    output = box.datasets()[result["dataset"]]
    assert "sum" not in output.meta["units"] and "sum" not in output.meta["summary_units"]
    assert output.meta["summary_units"]["mean"]["additive"] is False


def test_segment_nonadditive_measures_are_not_summed_while_additive_values_are_unchanged(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    source = box._dataset(
        "context",
        "mixed statistics",
        ["group", "ratio", "average", "amount"],
        [["a", "0.5", "10", "4"], ["a", "0.5", "30", "6"]],
        meta={
            "units": {"ratio": {"kind": "ratio"}, "average": {"kind": "level"}, "amount": {"kind": "level"}},
            "column_evidence": {"average": {"expression": "AVG(amount)"}},
        },
    )

    result = json.loads(box.segment_compare(f"D{source.seq}", "group", ["ratio", "average", "amount"]))

    assert "error" not in result, result
    values = dict(zip(result["columns"], result["rows"][0]))
    assert "ratio_sum" not in values and "average_sum" not in values
    assert values["ratio_mean"] == "0.5" and values["average_mean"] == "20"
    assert values["amount_sum"] == "10" and values["amount_mean"] == values["amount_median"] == "5"
    assert "ratio" in result["summary"]["note"] and "average" in result["summary"]["note"]
    assert "不可加聚合" in result["summary"]["note"]
    units = box.datasets()[result["dataset"]].meta["units"]
    assert "ratio_sum" not in units and "average_sum" not in units
    assert units["amount_mean"]["additive"] is False


def test_additive_describe_matches_existing_values_and_mean_cannot_be_resummed(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    source = box._dataset(
        "context",
        "additive",
        ["amount"],
        [["4"], ["6"]],
        meta={"units": {"amount": {"kind": "level", "unit": "元"}}},
    )
    result = json.loads(box.describe(f"D{source.seq}", "amount"))
    expected = calc.describe(["4", "6"])
    assert result["columns"] == list(expected)
    assert result["summary"] == {k: str(v) if isinstance(v, Decimal) else v for k, v in expected.items()}
    assert box.datasets()[result["dataset"]].meta["summary_units"]["sum"] == {"kind": "level", "unit": "元"}
    averaged = json.loads(box.describe(result["dataset"], "mean"))
    assert "sum" not in averaged["columns"]
    assert "已登记为不可加" in averaged["summary"]["note"]


def ratio_source(monkeypatch, *, include_components=True, matching_version=True, zero=False, missing=False):
    metric_names = {"ratio": "conversion", "numerator": "successes", "denominator": "attempts"}
    versions = {
        code: SimpleNamespace(
            id=f"{code}-v1", kind="base", expression="SUM(value)", value_kind="count", components=None
        )
        for code in metric_names
    }
    versions["ratio"] = SimpleNamespace(
        id="ratio-v1",
        kind="derived",
        expression="numerator / denominator",
        value_kind="ratio",
        components=[{"metric_code": code, "version_id": f"{code}-v1"} for code in ("numerator", "denominator")],
    )
    catalog = Catalog(
        [],
        {
            code: (SimpleNamespace(id=code, code=code, name=name, domain_id="dA"), versions[code])
            for code, name in metric_names.items()
        },
    )
    box = make_box(monkeypatch=monkeypatch, catalog=catalog)
    for version in versions.values():
        box.session.store[(MetricVersion, version.id)] = version
    codes = list(metric_names) if include_components else ["ratio"]
    pinned = {code: versions[code].id for code in codes}
    if not matching_version:
        replacement = SimpleNamespace(**{**vars(versions["numerator"]), "id": "numerator-v2"})
        box.session.store[(MetricVersion, replacement.id)] = replacement
        pinned["numerator"] = replacement.id
    columns = ["group", *[metric_names[code] for code in codes]]
    rows = [["a", "0.5", "1", "2"], ["a", "0.9", None if missing else "9", "-2" if zero else "10"]]
    source = box._dataset(
        "governed",
        "same query",
        columns,
        [row[: len(columns)] for row in rows],
        meta={
            "intent": {"metric_codes": codes},
            "versions": pinned,
            "units": {metric_names[code]: {"kind": versions[code].value_kind} for code in codes},
        },
    )
    return box, f"D{source.seq}"


def test_segment_ratio_recalculates_from_existing_matching_component_columns(monkeypatch):
    box, source = ratio_source(monkeypatch)
    result = json.loads(box.segment_compare(source, "group", ["conversion"]))
    values = dict(zip(result["columns"], result["rows"][0]))
    assert "conversion_sum" not in values
    assert values["conversion_mean"] == "0.7"
    assert Decimal(values["conversion_ratio"]) == Decimal(10) / Decimal(12)
    assert "successes" in result["summary"]["note"] and "attempts" in result["summary"]["note"]


@pytest.mark.parametrize("include_components,matching_version", [(False, True), (True, False)])
def test_segment_ratio_does_not_infer_missing_or_different_version_components(
    monkeypatch, include_components, matching_version
):
    box, source = ratio_source(monkeypatch, include_components=include_components, matching_version=matching_version)
    result = json.loads(box.segment_compare(source, "group", ["conversion"]))
    assert "conversion_sum" not in result["columns"] and "conversion_ratio" not in result["columns"]
    assert "分子、分母分别汇总后重算" in result["summary"]["note"]
    assert "缺少可重算的同版本分子、分母列，已省略整体比率" in result["summary"]["note"]


@pytest.mark.parametrize("zero,missing", [(True, False), (False, True)])
def test_segment_recalculated_ratio_is_unknown_for_zero_denominator_or_missing_components(monkeypatch, zero, missing):
    box, source = ratio_source(monkeypatch, zero=zero, missing=missing)
    result = json.loads(box.segment_compare(source, "group", ["conversion"]))
    assert result["rows"][0][result["columns"].index("conversion_ratio")] is None
    assert "分母为 0 或组成值缺失" in result["summary"]["note"]


def test_calc_distribution_opt_out_preserves_precision():
    values = ["9007199254740993.00004", "-9007199254740993", "0.00002"]
    distribution = calc.describe(values, include_sum=False)
    assert "sum" not in distribution
    assert distribution["mean"] == Decimal("0.00002")
    grouped = calc.segment_compare([{"group": "a", "ratio": "0.5"}] * 2, "group", ["ratio"], additive=set())
    assert grouped == [{"group": "a", "count": 2, "ratio_mean": Decimal("0.5"), "ratio_median": Decimal("0.5")}]
