"""PR #96：受治理可加性从锁定口径传到图表；全程使用内存对象。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from avelis.analysis.dataset_projection import build_column_roles, dataset_meta
from avelis.analysis.measures import governed_units
from avelis.ask.intent import Intent
from avelis.ask.service import _result_contract
from avelis.charts import recommend
from avelis.models import Metric, MetricVersion


def version(expression="AVG(amount)", **changes):
    return SimpleNamespace(
        **{
            "id": "locked",
            "metric_id": "metric",
            "kind": "base",
            "expression": expression,
            "value_kind": None,
            "unit": "元",
            "unit_scale": 1,
            "dimensions": [],
            **changes,
        }
    )


@pytest.mark.parametrize(
    "expression,kind,value_kind,additive",
    [
        ("SUM(amount)", "base", "level", True),
        ("COUNT(*)", "base", "count", True),
        ("AVG(amount)", "base", "level", False),
        ("MIN(amount)", "base", "level", False),
        ("MAX(amount)", "base", "level", False),
        ("COUNT(DISTINCT member)", "base", "count", False),
        ("unknown(amount)", "base", "level", False),
        ("a + b", "derived", "level", False),
        ("a / b", "derived", "ratio", False),
        ("SUM(amount)", "derived", "level", False),
        ("SUM(amount)", "base", "ratio", False),
    ],
)
def test_governed_units_register_additivity_for_every_period(expression, kind, value_kind, additive):
    columns = ["金额", "金额·上一期", "金额·变化", "金额·变化率"]
    units = governed_units(columns, {"金额": version(expression, kind=kind, value_kind=value_kind)}, "上一期")
    roles = build_column_roles(columns, {"units": units})
    for column in columns[:3]:
        assert units[column]["additive"] is additive
        assert roles[column]["additive"] is additive
    assert units[columns[-1]] == {"kind": "rate_change", "additive": False}
    assert roles[columns[-1]]["additive"] is False


@pytest.mark.parametrize("simple", [True, False])
def test_ask_result_contract_registers_avg_as_nonadditive(simple):
    metric = SimpleNamespace(name="金额")
    columns = ["value"] if simple else ["金额", "金额·上一期", "金额·变化", "金额·变化率"]
    result = {"columns": columns, "rows": [[1] * len(columns)]}
    original = deepcopy(result)
    projected = _result_contract(
        result,
        Intent(metric_code="amount", compare=None if simple else "previous_period"),
        [(metric, version())],
        value_column="value" if simple else None,
    )
    assert all(unit["additive"] is False for unit in projected["units"].values())
    assert all(role["additive"] is False for role in projected["column_roles"].values())
    assert result == original


@pytest.mark.parametrize("declared", [None, "level"])
@pytest.mark.parametrize("source", ["versions", "metric_version_ids"])
def test_dataset_meta_backfills_additivity_without_mutating_saved_units(declared, source):
    locked = version(value_kind=declared)
    metric = SimpleNamespace(name="金额")
    store = {(MetricVersion, "locked"): locked, (Metric, "metric"): metric}
    reads = []

    def get(entity, key):
        reads.append((entity, key))
        return store[entity, key]

    columns = ["金额", "金额·上一期", "金额·变化", "金额·变化率"]
    meta = {
        source: {"amount": "locked"} if source == "versions" else ["locked"],
        "intent": {"compare": "previous_period"},
        "units": {"金额": {"kind": "level", "unit": "元", "legacy": "kept"}},
    }
    original = deepcopy(meta)
    dataset = SimpleNamespace(kind="governed", columns=columns, meta=meta)
    projected = dataset_meta(SimpleNamespace(get=get), dataset)
    assert all(unit["additive"] is False for unit in projected["units"].values())
    assert all(role["additive"] is False for role in build_column_roles(columns, projected).values())
    if declared is None:
        assert projected["units"]["金额"]["legacy"] == "kept"
    assert reads == [(MetricVersion, "locked"), (Metric, "metric")]
    assert meta == original


@pytest.mark.parametrize("shape", ["ranking", "composition", "time", "dimensions"])
def test_avg_charts_do_not_merge_others_or_offer_stacked_or_donut(shape):
    columns = ["成员", "金额"]
    rows = [[str(i), i] for i in range(25 if shape == "ranking" else 3)]
    meta = {"units": governed_units(columns, {"金额": version()}, None)}
    if shape == "time":
        columns = ["时间", *columns]
        rows = [[period, str(i), i] for period in ("01", "02") for i in range(9)]
        meta["time_column"] = "时间"
    elif shape == "dimensions":
        columns = ["区域", *columns]
        rows = [["北", *row] for row in rows]
    roles = build_column_roles(columns, meta)
    specs = recommend(columns, rows, roles, meta["units"], {})
    assert specs
    assert roles["金额"]["additive"] is False
    assert all(spec["kind"] not in {"stacked_bar", "donut"} for spec in specs)
    assert all(spec["others"] is None and not any(s["other"] for s in spec["series"]) for spec in specs)
    if shape == "ranking":
        assert len(specs[0]["categories"]) == 20
        assert specs[0]["series"][0]["values"] == list(range(24, 4, -1))
    elif shape == "time":
        assert len(specs[0]["series"]) == 7 and specs[0]["dropped_series"] == 2
        assert specs[1]["kind"] == "grouped_bar"


def test_explicit_additivity_controls_duplicate_cell_aggregation():
    columns = ["成员", "金额"]
    units = governed_units(columns, {"金额": version()}, None)
    roles = build_column_roles(columns, {"units": units})
    specs = recommend(columns, [["北", 10], ["北", 30]], roles, units, {})
    assert specs[0]["series"][0]["values"] == [10.0]


@pytest.mark.parametrize("kind,additive", [("level", False), ("ratio", True)])
def test_explicit_additivity_overrides_kind_in_column_roles(kind, additive):
    roles = build_column_roles(["v"], {"units": {"v": {"kind": kind, "additive": additive}}})
    assert roles["v"]["additive"] is additive
