"""描述及分组统计保留精确原值，工具 JSON、数据集与数字引用不经过 float。"""

import json
from decimal import Decimal, localcontext

import pytest

from avelis.analysis import calc, grounding
from tests.unit.test_analysis_toolbox import make_box


@pytest.mark.parametrize(
    "values,total,maximum,mean",
    [
        (["9007199254740993", "1"], "9007199254740994", "9007199254740993", "4503599627370497"),
        (["0.00004", "0.00002"], "0.00006", "0.00004", "0.00003"),
        (["0.1", "0.2"], "0.3", "0.2", "0.15"),
        (["9007199254740993.00004", "-9007199254740993", "0.00002"], "0.00006", "9007199254740993.00004", "0.00002"),
        (
            ["123456789012345678901234567890.00004", "-123456789012345678901234567890", "0.00002"],
            "0.00006",
            "123456789012345678901234567890.00004",
            "0.00002",
        ),
    ],
)
def test_describe_and_segment_keep_exact_values(values, total, maximum, mean):
    result = calc.describe([*values, None, ""])
    assert result["count"] == len(values)
    assert result["sum"] == Decimal(total) and result["max"] == Decimal(maximum)
    assert result["min"] == min(map(Decimal, values))
    assert result["mean"] == Decimal(mean)
    assert all(isinstance(v, Decimal) for k, v in result.items() if k != "count")
    rows = [{"group": "a", "value": v} for v in values] + [{"group": "b", "value": "0.00004"}]
    groups = calc.segment_compare(rows, "group", ["value"])
    assert groups[0]["value_sum"] == Decimal(total) and groups[0]["value_mean"] == Decimal(mean)
    assert groups[1]["value_sum"] == groups[1]["value_median"] == Decimal("0.00004")


def test_stdev_uses_decimal_square_root_precision_without_display_rounding():
    result = calc.describe(["0.00002", "0.00004"])
    with localcontext() as ctx:
        ctx.prec = 28
        assert result["stdev"] == Decimal("0.0000000002").sqrt()
    assert calc.describe([None, ""]) == {"count": 0}
    assert "stdev" not in calc.describe(["0.00004"])


@pytest.mark.parametrize(
    "values,total",
    [
        (["9007199254740993", "1"], "9007199254740994"),
        (["0.00004", "0.00002"], "0.00006"),
    ],
)
def test_tool_json_persistence_and_references_preserve_decimal(monkeypatch, values, total):
    box = make_box(monkeypatch=monkeypatch)
    source = box._dataset(
        "context",
        "精确数字",
        ["group", "value"],
        [["a", v] for v in values],
        meta={"units": {"value": {"kind": "level", "unit": "", "scale": 1}}},
    )
    view = json.loads(box.describe(f"D{source.seq}", "value"))
    assert view["rows"][0][view["columns"].index("sum")] == total
    dataset = box.datasets()[view["dataset"]]
    # 数据集与 summary 已经经过真实工具层 JSON 序列化，可继续用现有引用核验与显示。
    assert dataset.meta["summary"]["sum"] == total
    registry = {
        view["dataset"]: {
            "kind": "calc",
            "columns": dataset.columns,
            "rows": [dict(zip(dataset.columns, row)) for row in dataset.rows],
            "summary": dataset.meta["summary"],
            "units": dataset.meta["units"],
            "summary_units": dataset.meta["summary_units"],
        }
    }
    for ref in [
        grounding.NumberRef(dataset=view["dataset"], column="sum", row=1),
        grounding.NumberRef(dataset=view["dataset"], summary="sum"),
    ]:
        raw, _ = grounding.resolve(ref, registry, {})
        assert grounding.number(raw) == Decimal(total)
        assert grounding.render([ref], registry, {})["errors"] == []
    grouped = json.loads(box.segment_compare(f"D{source.seq}", "group", ["value"]))
    assert grouped["rows"][0][grouped["columns"].index("value_sum")] == total
