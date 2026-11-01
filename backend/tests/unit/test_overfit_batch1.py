"""过拟合审查第一批：中性数据的离线回归，不连接数据库或模型。"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from types import SimpleNamespace as _Batch1NS

import pytest
import sqlglot
from sqlglot import exp

from avelis.analysis import calc, measures
from avelis.ask import agent as _batch1_ask_agent
from avelis.ask import service
from avelis.ask import service as _batch1_ask_service
from avelis.ask.intent import Intent, MetricCard
from avelis.connectors.base import QueryError
from avelis.governance.derived import evaluate
from avelis.lineage.sql import SqlDialect
from avelis.models import MetricVersion
from tests.unit.test_analysis_toolbox import make_box


def _batch1_box(monkeypatch, expression='SUM("指标甲")', *, kind="base", components=None):
    metric = SimpleNamespace(id="指标甲", name="指标甲", code="metric_a", domain_id="dA")
    version = SimpleNamespace(id="版本甲", version_no=1, kind=kind, status="published", expression=expression,
                              components=components or [], dimensions=[{"name": "维度甲"}])
    box = make_box(monkeypatch=monkeypatch, catalog=service.Catalog([], {"metric_a": (metric, version)}))
    box.run.window = {}
    box.session.store[(MetricVersion, version.id)] = version
    return box


def _batch1_governed(box, rows=None, *, grain=None, units=None, absent=None):
    return box._dataset("governed", "指标甲", ["维度甲", "指标甲", "指标甲·上一期"],
                        rows if rows is not None else [["成员甲", 12, 10], ["成员乙", 8, 5]],
                        meta={"intent": {"metric_codes": ["metric_a"], "time_grain": grain,
                                         "start_date": "2026-01-01", "end_date": "2026-01-03",
                                         "compare": "previous_period"}, "versions": {"metric_a": "版本甲"},
                              "missing_member_rows": absent or [],
                              "units": units or {"指标甲": {"kind": "level"},
                                                 "指标甲·上一期": {"kind": "level"}}})


def test_calendar_unknown_holidays_preserves_small_values():
    out = calc.calendar_adjust("0.00003", "0.00001", (date(2026, 1, 1), date(2026, 1, 3)),
                               (date(2026, 1, 1), date(2026, 1, 2)))
    assert out["current"]["holidays"] is None and out["previous"]["holidays"] is None
    assert out["current"]["days"] == 3 and out["current"]["weekend_days"] == 1
    assert out["current"]["daily"] == Decimal("0.00001")
    assert out["day_count_effect"] == Decimal("0.000005")
    assert out["daily_level_effect"] == Decimal("0.000015")
    assert calc.period_days(date(2026, 1, 1), date(2026, 1, 3), set())["holidays"] == 0


@pytest.mark.parametrize("value, expected", [
    ("103.2258064516129032258064516", "103.225806452"),
    ("-0.0001234567890123456789", "-0.000123456789012"),
    ("1.00001", "1.00001"),
    ("2", "2.0000"),
    ("123456789012345.67", "123456789012345.6700"),
    ("1234567890123.1", "1234567890123.1000"),
    ("123456789012345", "123456789012345.0000"),
    ("123456789012345678901234567890", "123456789012345678901234567890.0000"),
    ("0.00001", "0.00001"),
    ("0.000001", "0.000001"),
    ("0", "0.0000"),
    ("0.9999999999995", "1.0000"),
])
def test_calendar_rounding_preserves_integer_digits_and_fixed_notation(value, expected):
    result = calc._significant12(Decimal(value))
    assert str(result) == expected
    assert "e" not in str(result).lower()


def test_calendar_and_derived_share_rounding_helper():
    from avelis.governance.derived import significant12

    assert calc._significant12 is significant12


def test_calendar_rounds_only_outputs_and_keeps_full_precision_for_effects():
    out = calc.calendar_adjust("3200", "2800", (date(2026, 1, 1), date(2026, 1, 31)),
                               (date(2026, 2, 1), date(2026, 2, 27)))
    assert str(out["current"]["daily"]) == "103.225806452"
    assert str(out["previous"]["daily"]) == "103.703703704"
    assert str(out["day_count_effect"]) == "414.814814815"
    assert str(out["daily_level_effect"]) == "-14.8148148148"
    assert out["daily_change_rate"] == Decimal("-0.0046")


def test_calendar_tool_reports_unknown_holidays(monkeypatch):
    box = _batch1_box(monkeypatch)
    _batch1_governed(box, [["成员甲", "0.00003", "0.00006"]])
    out = json.loads(box.calendar_adjust("D1", "指标甲", "指标甲·上一期"))
    assert out["summary"]["holidays_note"] == "未配置业务日历，节假日未知"
    rows = [dict(zip(out["columns"], row)) for row in out["rows"]]
    assert all(row["holidays"] is None for row in rows)
    assert Decimal(rows[0]["daily"]) == Decimal("0.00001")


@pytest.mark.parametrize("expression", ['AVG("指标甲")', 'COUNT(DISTINCT "维度甲")', 'MAX("指标甲")'])
@pytest.mark.parametrize("operation", ["contribution", "calendar_adjust", "derive"])
def test_governed_nonadditive_metrics_reject_member_sums(monkeypatch, expression, operation):
    box = _batch1_box(monkeypatch, expression)
    _batch1_governed(box)
    if operation == "derive":
        out = box.derive("指标乙", [{"name": "a", "dataset": "D1", "column": "指标甲"}], "a")
    elif operation == "calendar_adjust":
        out = box.calendar_adjust("D1", "指标甲", "指标甲·上一期", {"维度甲": "成员甲"})
    else:
        out = box.contribution("D1", ["维度甲"], "指标甲", "指标甲·上一期")
    assert "不能按成员相加" in json.loads(out)["error"]


def test_governed_ratio_is_not_additive_and_note_offers_no_bypass(monkeypatch):
    component = SimpleNamespace(id="组成版本甲", expression='SUM("指标甲")', kind="base")
    components = [{"metric_code": "a", "version_id": component.id},
                  {"metric_code": "b", "version_id": component.id}]
    box = _batch1_box(monkeypatch, "a / b", kind="derived", components=components)
    box.session.store[(MetricVersion, component.id)] = component
    _batch1_governed(box)
    assert "派生比率" in json.loads(box.contribution("D1", ["维度甲"], "指标甲", "指标甲·上一期"))["error"]
    box.catalog.versions["metric_a"][1].kind = "base"
    box.catalog.versions["metric_a"][1].expression = 'AVG("指标甲")'
    metric, version = box.catalog.versions["metric_a"]
    monkeypatch.setattr(box, "_metric_sources", lambda code: (metric, version, [version], {}))
    out = json.loads(box.explain_change(metric_code="metric_a", start_date=date(2026, 1, 1),
                                       end_date=date(2026, 1, 3), compare_start=date(2025, 12, 29),
                                       compare_end=date(2025, 12, 31)))
    assert not out["supported"] and "不能按成员相加拆解" in out["note"]
    assert "contribution" not in out["note"]


@pytest.mark.parametrize("kind", ["ratio", "rate_change", "share", "unknown"])
def test_explore_units_block_sums_including_exclude(monkeypatch, kind):
    box = _batch1_box(monkeypatch)
    box._dataset("explore", "指标甲", ["维度甲", "指标甲", "指标乙"], [["成员甲", 2, 1], ["成员乙", 3, 2]],
                 meta={"units": {"指标甲": {"kind": kind}, "指标乙": {"kind": kind}}})
    out = json.loads(box.contribution("D1", ["维度甲"], "指标甲", "指标乙"))
    assert "不能按成员相加" in out["error"]
    for exclude in ({}, {"维度甲": "成员乙"}):
        out = json.loads(box.derive("指标丙", [{"name": "a", "dataset": "D1", "column": "指标甲",
                                               "exclude": exclude}], "a"))
        assert "不能按成员相加" in out["error"]


def test_missing_governed_intent_cannot_claim_additivity(monkeypatch):
    box = _batch1_box(monkeypatch)
    ds = _batch1_governed(box)
    ds.meta["intent"] = {}
    assert "无法确认可加性" in json.loads(box.contribution("D1", ["维度甲"], "指标甲", "指标甲·上一期"))["error"]


def test_missing_query_version_cannot_use_current_additivity(monkeypatch):
    box = _batch1_box(monkeypatch)
    ds = _batch1_governed(box)
    ds.meta.pop("versions")
    assert "缺少锁定的查询版本" in json.loads(box.contribution("D1", ["维度甲"], "指标甲", "指标甲·上一期"))["error"]


def test_governed_change_rate_cannot_be_summed(monkeypatch):
    box = _batch1_box(monkeypatch)
    ds = _batch1_governed(box)
    ds.columns.append("指标甲·变化率")
    ds.rows = [row + ["0.1"] for row in ds.rows]
    out = json.loads(box.derive("指标乙", [{"name": "a", "dataset": "D1", "column": "指标甲·变化率"}], "a"))
    assert "不能按成员相加" in out["error"]


@pytest.mark.parametrize("period_value", ["2026-01", "2026-W01", "2026-01-01T00:00:00", "20260101",
                                          "2026-1-1", "2026-02-30", None])
def test_coverage_does_not_invent_daily_gaps(period_value):
    out = calc.coverage([{"维度甲": "成员甲", "期间": period_value}], ["维度甲"], "期间",
                        date(2026, 1, 1), date(2026, 1, 3))
    assert out["summary"]["expected_periods"] is None
    assert out["rows"][0]["missing_count"] is None and out["rows"][0]["missing_periods"] == ""


def test_coverage_daily_values_still_identifies_missing_date():
    out = calc.coverage([{"维度甲": "成员甲", "期间": "2026-01-01"}], ["维度甲"], "期间",
                        date(2026, 1, 1), date(2026, 1, 2))
    assert out["summary"]["expected_periods"] == 2 and out["rows"][0]["missing_periods"] == "2026-01-02"


@pytest.mark.parametrize("grain", [None, "month"])
def test_governed_coverage_refuses_nonday_grain(monkeypatch, grain):
    box = _batch1_box(monkeypatch)
    _batch1_governed(box, grain=grain)
    assert ("没有时间分组" if grain is None else "缺少时间期间列") in json.loads(box.coverage("D1"))["error"]


def test_derived_evaluate_rounds_significant_digits():
    assert evaluate("a / b", {"a": "0.00001", "b": 1}) == Decimal("0.00001")
    assert evaluate("a / b", {"a": 1, "b": 3}) == Decimal("0.333333333333")
    assert evaluate("a / b", {"a": "1234567890123", "b": 1}) == Decimal("1234567890123.0000")


@pytest.mark.parametrize("value, divisor, expected", [
    ("1.00001", 1, "1.00001"),
    (2, 1, "2.0000"),
    ("123456789012345.67", 1, "123456789012345.6700"),
    ("1234567890123.1", 1, "1234567890123.1000"),
    ("0.00001", 1, "0.00001"),
    (1, 3, "0.333333333333"),
    ("-0.000123456789", 1, "-0.000123456789"),
    (0, 1, "0.0000"),
    ("0.00001234", 1, "0.00001234"),
    ("0.000001", 1, "0.000001"),
    ("99999999999999999999.123456", 1, "99999999999999999999.1235"),
])
def test_derived_evaluate_preserves_integer_precision_and_fixed_notation(value, divisor, expected):
    result = evaluate("a / b", {"a": value, "b": divisor})
    assert str(result) == expected
    assert "e" not in str(result).lower()


def test_derived_evaluate_quantizes_large_integer_without_losing_digits():
    value = Decimal("123456789012345678901234567890.123456")
    assert str(evaluate("a", {"a": value})) == "123456789012345678901234567890.1235"


def test_factor_decompose_keeps_small_factor_contribution():
    out = calc.factor_decompose(("0.0001", "0.00012"), {"指标甲": ("0.0001", "0.00012"), "指标乙": (1, 1)})
    parts = {r["factor"]: r["contribution"] for r in out["factors"]}
    assert parts["指标甲"] != 0 and parts["指标乙"] == 0
    assert float(parts["指标甲"]) == pytest.approx(0.00002) and out["unexplained"] == 0


@pytest.mark.parametrize("formula, expected", [("a / b", "1000"), ("a - b", "19980"), ("a", "20000")])
def test_derive_converts_inputs_to_base_units(monkeypatch, formula, expected):
    box = _batch1_box(monkeypatch)
    box._dataset("explore", "指标甲", ["指标甲", "指标乙"], [[2, 20]], meta={"units": {
        "指标甲": {"kind": "level", "unit": "单位甲", "scale": 10000},
        "指标乙": {"kind": "level", "unit": "单位甲", "scale": 1}}})
    picks = [{"name": "a", "dataset": "D1", "column": "指标甲"}]
    if "b" in formula:
        picks.append({"name": "b", "dataset": "D1", "column": "指标乙"})
    out = json.loads(box.derive("指标丙", picks, formula))
    assert Decimal(out["summary"]["value"]) == Decimal(expected)
    assert box.datasets()[out["dataset"]].meta["summary_units"]["value"].get("scale", 1) == 1


def test_formula_unit_uses_normalized_base_scale():
    units = {"a": {"kind": "level", "unit": "单位甲", "scale": "10000"},
             "b": {"kind": "level", "unit": "单位甲", "scale": "1"}}
    assert measures.formula_unit("a/b", units) == {"kind": "ratio"}
    assert measures.formula_unit("a-b", units) == {"kind": "delta", "unit": "单位甲"}
    assert measures.formula_unit("a+b", units).get("scale", 1) == 1 and units["a"]["scale"] == "10000"


@pytest.mark.parametrize("expression, expected", [
    ('COUNT(*) / COUNT("指标甲")', "ratio"),
    ('(COUNT(DISTINCT "维度甲")) / (COUNT(*))', "ratio"),
    ('SUM("指标甲") / SUM("指标甲")', "unknown"),
    ('SUM("指标甲") / COUNT(*)', "unknown"),
    ('COUNT(*) / AVG("指标甲")', "unknown"),
    ('SUM(CASE WHEN "维度甲" = \'成员甲\' THEN 1 ELSE 0 END) / COUNT(*)', "ratio"),
])
@pytest.mark.parametrize("engine", ["postgres", "starrocks"])
def test_exploration_aggregate_division_units(expression, expected, engine):
    from avelis.analysis.exploration import units
    from avelis.analysis.explore import PROFILES, BoundColumn, BoundTable, validate

    table = BoundTable("资产甲", "版本甲", "物理甲", "catalog_a", "schema_a", "table_a", "table",
                       (BoundColumn("指标甲", "decimal(18,8)"), BoundColumn("维度甲", "varchar(64)")))
    validated = validate(f'SELECT {expression} AS "指标乙" FROM schema_a.table_a', PROFILES[engine],
                         lambda catalog, db, name: table, max_rows=10)
    assert units(validated.columns) == {"指标乙": {"kind": expected}}
    assert validated.columns[0].eligible


def test_contribution_keeps_present_null_unknown_and_absence_zero():
    rows = [{"维度甲": "成员甲", "本期": 5, "上期": None}, {"维度甲": "成员乙", "本期": "", "上期": 2},
            {"维度甲": "成员丙", "本期": 3}, {"维度甲": "成员丁", "上期": 4}]
    out = calc.contribution(rows, ["维度甲"], "本期", "上期")
    members = {r["维度甲"]: r for r in out["rows"]}
    assert members["成员甲"]["change"] is None and members["成员甲"]["previous"] is None
    assert members["成员乙"]["change"] is None and members["成员乙"]["current"] is None
    assert members["成员丙"]["change"] == 3 and members["成员丙"]["previous"] == 0
    assert members["成员丁"]["change"] == -4 and members["成员丁"]["current"] == 0
    assert out["summary"]["missing_values"] == 2 and "仅合计有值部分" in out["summary"]["totals_note"]
    assert out["total"]["current"] == 8 and out["total"]["previous"] == 6
    assert [r["维度甲"] for r in out["rows"][:2]] == ["成员丁", "成员丙"]


def test_contribution_tool_uses_query_presence_without_filling_null(monkeypatch):
    box = _batch1_box(monkeypatch)
    _batch1_governed(box, [["成员甲", 5, None], ["成员乙", None, 3]], absent=[["指标甲·上一期"], []])
    out = json.loads(box.contribution("D1", ["维度甲"], "指标甲", "指标甲·上一期"))
    assert out["summary"]["missing_values"] == 1
    rows = {r[0]: dict(zip(out["columns"], r)) for r in out["rows"]}
    assert rows["成员甲"]["change"] == "5" and rows["成员乙"]["change"] is None
    assert box.datasets()["D1"].rows[0][-1] is None


@pytest.mark.parametrize("value", [None, ""])
def test_derive_refuses_missing_value(monkeypatch, value):
    box = _batch1_box(monkeypatch)
    _batch1_governed(box, [["成员甲", value, 2]])
    out = json.loads(box.derive("指标乙", [{"name": "a", "dataset": "D1", "column": "指标甲"}], "a"))
    assert "取值为空，不能当 0 参与计算" in out["error"]


def test_neutral_descriptions_and_calendar_guidance():
    source = Path(__file__).resolve().parents[2] / "src" / "avelis"
    expected = {"analysis/tools.py": ["（如 <维度>）", '{"<维度>":}', "只按自然日天数折算"],
                "analysis/agent.py": ["ColumnName", "节假日未知"], "analysis/calc.py": ["Decimal（精确小数）"],
                "analysis/measures.py": ["水平值 / 计数"], "analysis/grounding.py": ["<单位>/<单位>"],
                "ask/intent.py": ["（例如 YYYY-MM-DD）"],
                "connectors/oracle.py": ["SCHEMA_A.TABLE_A.COL_A"], "lineage/sql.py": ["schema_a.table_a"]}
    for name, phrases in expected.items():
        text = (source / name).read_text(encoding="utf-8")
        assert all(phrase.replace("只按自然日天数折算", "未配置业务日历时节假日未知，配置后给出节假日与工作日折算")
                   in text for phrase in phrases), name



def _batch1_ask_toolbox(monkeypatch, sample):
    dimensions = [{"name": "维度甲", "column": "column_a"}]
    version = _Batch1NS(status="published", kind="atomic", dimensions=dimensions)
    catalog = _batch1_ask_service.Catalog(
        [MetricCard(code="metric_a", name="指标甲", aliases=[], dimensions=dimensions)],
        {"metric_a": (None, version)})
    box = _batch1_ask_agent.Toolbox(None, None, catalog)
    box._values[("metric_a", "维度甲")] = sample
    monkeypatch.setattr(_batch1_ask_agent, "get_settings", lambda: object())
    return box


@pytest.mark.parametrize("existing,accepted", [({"成员甲"}, True), (set(), False), (None, True)])
def test_batch1_full_dimension_sample_checks_exact_value(monkeypatch, existing, accepted):
    sample = [f"成员样本{i}" for i in range(_batch1_ask_service.DIMENSION_VALUES_LIMIT)]
    box = _batch1_ask_toolbox(monkeypatch, sample)
    checked = []
    def check(session, user, version, dimension, values, settings):
        checked.append((dimension, values))
        return existing
    monkeypatch.setattr(_batch1_ask_service, "_existing_dimension_values", check)
    result = json.loads(box.submit_intent(
        ["metric_a"], date(2026, 1, 1), date(2026, 1, 1),
        filters=[{"dimension": "维度甲", "values": ["成员甲"]}]))
    assert result["accepted"] is accepted
    assert checked == [("维度甲", ["成员甲"])]
    assert "样本不完整时，原话中的取值会在提交时精确核验" in json.loads(
        box.match_dimension_values("metric_a", "维度甲", "成员甲"))["note"]


def test_batch1_complete_dimension_sample_rejects_unknown_without_query(monkeypatch):
    box = _batch1_ask_toolbox(monkeypatch, ["成员甲"])
    monkeypatch.setattr(_batch1_ask_service, "_existing_dimension_values",
                        lambda *args: pytest.fail("完整样本不需要精确查询"))
    result = json.loads(box.submit_intent(
        ["metric_a"], date(2026, 1, 1), date(2026, 1, 1),
        filters=[{"dimension": "维度甲", "values": ["成员乙"]}]))
    assert result["accepted"] is False
    assert "成员乙" in result["errors"][0]


def _batch1_dimension_reader(monkeypatch, dialect):
    service = _batch1_ask_service
    asset = _Batch1NS(id="asset_a", current_version_id="version_a", datasource_id="source_a",
                     catalog_name="catalog_a", db_name="schema_a", name="table_a")
    version = _Batch1NS(status="published", kind="atomic",
                       dimensions=[{"name": "维度甲", "column": "column_a"}])
    datasource = object()
    session = _Batch1NS(get=lambda model, identifier: datasource)
    state = {"events": [], "queries": [], "rows": [], "truncated": False, "error": None}
    def execute(sql, **limits):
        state["events"].append("execute")
        state["queries"].append((sql, limits))
        if state["error"]:
            raise state["error"]
        return _Batch1NS(rows=state["rows"], truncated=state["truncated"])
    connector = _Batch1NS(execute_select=execute)
    monkeypatch.setattr(service, "dependency_drift", lambda *args: [])
    monkeypatch.setattr(service, "validate_spec", lambda *args: {"aliases": {"b": asset}})
    monkeypatch.setattr(service, "upstream_closure", lambda *args: {asset.id: asset})
    monkeypatch.setattr(service.access, "domain_ids", lambda *args: {"domain_a"})
    monkeypatch.setattr(service.access, "can_read_asset", lambda *args: True)
    monkeypatch.setattr(service, "engine_of", lambda ds: _Batch1NS(supports=lambda capability: True))
    monkeypatch.setattr(service, "dialect_of", lambda ds: dialect)
    monkeypatch.setattr(service, "query_connector", lambda *args: connector)
    monkeypatch.setattr(service, "_live_check", lambda *args: state["events"].append("live") or [])
    monkeypatch.setattr(service.cache, "client", lambda: pytest.fail("精确核验不读写样本缓存"))
    validate = service.validate_sql
    def validate_query(sql, **kwargs):
        state["events"].append("validate")
        assert kwargs["dialect"] is dialect
        return validate(sql, **kwargs)
    monkeypatch.setattr(service, "validate_sql", validate_query)
    return session, version, state, asset


@pytest.mark.parametrize("dialect", [
    SqlDialect("mysql", "catalog_a"),
    SqlDialect("postgres", "catalog_a", catalog_in_sql=False),
    SqlDialect("tsql", "catalog_a", catalog_in_sql=False, national_strings=True),
])
def test_batch1_exact_dimension_query_uses_literals_and_datasource_dialect(monkeypatch, dialect):
    session, version, state, _ = _batch1_dimension_reader(monkeypatch, dialect)
    member = "成员甲' OR 1=1 --"
    state["rows"] = [[1]]
    assert _batch1_ask_service._existing_dimension_values(
        session, object(), version, "维度甲", [member], object()) == {member}
    sql, limits = state["queries"][0]
    tree = sqlglot.parse_one(sql, read=dialect.name)
    assert [literal.this for literal in tree.find(exp.Where).find(exp.In).expressions] == [member]
    assert len(list(tree.find_all(exp.Max))) == 1
    assert limits == {"timeout_seconds": 10, "mem_limit_bytes": 512 * 1024**2,
                      "max_rows": _batch1_ask_service.DIMENSION_VALUES_LIMIT, "timezone": None}
    assert state["events"] == ["validate", "live", "execute"]
    if dialect.national_strings:
        assert "N'" in sql


@pytest.mark.parametrize("guard", ["status", "drift", "permission", "live", "validation", "timeout", "truncated"])
def test_batch1_exact_dimension_query_returns_unknown_when_guard_fails(monkeypatch, guard):
    service = _batch1_ask_service
    session, version, state, asset = _batch1_dimension_reader(
        monkeypatch, SqlDialect("mysql", "catalog_a"))
    if guard == "status":
        version.status = "affected"
    elif guard == "drift":
        monkeypatch.setattr(service, "dependency_drift", lambda *args: [{"column": "column_a"}])
    elif guard == "permission":
        upstream = _Batch1NS(id="asset_upstream")
        monkeypatch.setattr(service, "upstream_closure",
                            lambda *args: {asset.id: asset, upstream.id: upstream})
        monkeypatch.setattr(service.access, "can_read_asset",
                            lambda session, user, source, allowed: source.id != upstream.id)
    elif guard == "live":
        monkeypatch.setattr(service, "_live_check", lambda *args: [{"summary": "元数据已变化"}])
    elif guard == "validation":
        def invalid(*args, **kwargs):
            raise service.ValidationFailed(["SQL 未通过核对"])
        monkeypatch.setattr(service, "validate_sql", invalid)
    elif guard == "timeout":
        state["error"] = QueryError("timeout", None, "超时")
    else:
        state["truncated"] = True
    assert service._existing_dimension_values(
        session, object(), version, "维度甲", ["成员甲"], object()) is None
    assert len(state["queries"]) == (1 if guard in {"timeout", "truncated"} else 0)


def _batch1_plan_catalog(monkeypatch):
    service = _batch1_ask_service
    dimensions = [{"name": "维度甲", "column": "column_a"}]
    metrics = {code: _Batch1NS(id=code, name=name, domain_id="domain_a")
               for code, name in [("metric_a", "指标甲"), ("metric_b", "指标乙"), ("metric_ratio", "指标丙")]}
    versions = {code: _Batch1NS(id=f"version_{code}", version_no=1, kind="atomic",
                               status="published", dimensions=dimensions)
                for code in metrics}
    versions["metric_ratio"].kind = "derived"
    versions["metric_ratio"].expression = "metric_a / metric_b"
    versions["metric_ratio"].components = [
        {"metric_id": code, "version_id": f"version_{code}", "metric_code": code, "metric_name": metrics[code].name}
        for code in ["metric_a", "metric_b"]]
    stored = {**metrics, **{v.id: v for v in versions.values()}}
    session = _Batch1NS(get=lambda model, identifier: stored.get(identifier))
    catalog = service.Catalog([], {code: (metrics[code], versions[code]) for code in metrics})
    current = {"metric_a": [["成员甲", "2"], ["成员乙", None], ["成员戊", "5"]],
               "metric_b": [["成员甲", "4"], ["成员庚", "6"]]}
    previous = {"metric_a": [["成员甲", "1"], ["成员丙", "4"], ["成员丁", None]],
                "metric_b": [["成员甲", "2"], ["成员己", "9"]]}
    def fulfil(session, user, intent, answer, versions, settings):
        answer.status = "answered"
        answer.result = {"rows": (current if intent.start_date.day == 2 else previous)[intent.metric_code]}
        answer.evidence = {}
        answer.sql = None
        answer.freshness = {}
        answer.cache_hit = False
    monkeypatch.setattr(service, "_fulfil", fulfil)
    monkeypatch.setattr(service.access, "domain_ids", lambda *args: {"domain_a"})
    monkeypatch.setattr(service, "_version_evidence", lambda *args: {})
    monkeypatch.setattr(service, "validate_spec", lambda *args: {"aliases": {}})
    monkeypatch.setattr(service, "_dimension_docs", lambda *args: {})
    return session, catalog


@pytest.mark.parametrize("order,limit,expected", [
    (None, None, ["成员甲", "成员乙", "成员戊", "成员庚", "成员丙", "成员丁", "成员己"]),
    ("asc", 3, ["成员甲", "成员戊", "成员乙"]),
    ("desc", 3, ["成员戊", "成员甲", "成员乙"]),
])
def test_batch1_comparison_union_keeps_current_first_and_missing_last(monkeypatch, order, limit, expected):
    session, catalog = _batch1_plan_catalog(monkeypatch)
    intent = Intent(metric_codes=["metric_a", "metric_b", "metric_ratio"],
                    start_date=date(2026, 1, 2), end_date=date(2026, 1, 2),
                    group_by=["维度甲"], compare="previous_period", order=order, limit=limit)
    answer = _Batch1NS(question="指标甲", model_adapter="offline-unit",
                       metric_version_ids=[], evidence={}, freshness={})
    _batch1_ask_service._fulfil_plan(session, _Batch1NS(id="user_a"), intent, answer, catalog, object())
    assert answer.status == "answered"
    assert [row[0] for row in answer.result["rows"]] == expected
    by_member = {row[0]: row for row in answer.result["rows"]}
    marked = dict(zip(expected, answer.evidence["missing_member_rows"]))
    assert "指标甲" not in marked["成员乙"]  # 有行且为空，不能误判成缺行。
    assert "指标甲·上一期" in marked["成员乙"]
    if order is None:
        assert by_member["成员丙"][1:5] == [None, "4", None, None]
        assert by_member["成员丁"][1:5] == [None, None, None, None]
        assert "指标甲·上一期" not in marked["成员丁"]
        assert "指标甲" in marked["成员丙"]
        assert by_member["成员甲"][9] == by_member["成员甲"][10]  # 派生比率两期都为 0.5。


def test_batch1_empty_comparison_has_no_invented_members(monkeypatch):
    session, catalog = _batch1_plan_catalog(monkeypatch)

    def empty(session, user, intent, answer, versions, settings):
        answer.status, answer.result, answer.evidence = "answered", {"rows": []}, {}
        answer.sql, answer.freshness, answer.cache_hit = None, {}, False

    monkeypatch.setattr(service, "_fulfil", empty)
    intent = Intent(metric_codes=["metric_a"], start_date=date(2026, 1, 2), end_date=date(2026, 1, 2),
                    group_by=["维度甲"], compare="previous_period")
    answer = SimpleNamespace(question="指标甲", model_adapter="offline-unit", metric_version_ids=[],
                             evidence={}, freshness={})
    service._fulfil_plan(session, SimpleNamespace(id="user_a"), intent, answer, catalog, object())
    assert answer.status == "answered" and answer.result["rows"] == []
    assert answer.evidence["missing_member_rows"] == []


@pytest.mark.parametrize("responses,requested,expected", [
    ([set(), {"成员甲"}], ["成员甲"], {"成员甲"}),
    ([{"成员甲"}, {"成员乙"}], ["成员甲", "成员乙"], {"成员甲", "成员乙"}),
    ([set(), set()], ["成员甲"], set()),
    ([set(), None], ["成员甲"], None),
    ([{"成员甲"}, None], ["成员甲"], {"成员甲"}),
    ([{"成员甲"}, None], ["成员甲", "成员乙"], None),
])
def test_batch1_derived_dimension_exact_values_use_component_union(monkeypatch, responses, requested, expected):
    service = _batch1_ask_service
    dimensions = [{"name": "维度甲", "column": "column_a"}]
    components = {f"version_{i}": _Batch1NS(id=f"version_{i}", kind="atomic")
                  for i in range(len(responses))}
    version = _Batch1NS(kind="derived", status="published", dimensions=dimensions,
                       components=[{"version_id": identifier} for identifier in components])
    session = _Batch1NS(get=lambda model, identifier: components[identifier])
    checked = []
    def read(session, user, source, settings, *, exact_values):
        checked.append(source.id)
        result = responses[int(source.id.rsplit("_", 1)[1])]
        return {} if result is None else {"维度甲": [v for v in exact_values["维度甲"] if v in result]}
    monkeypatch.setattr(service, "dependency_drift", lambda *args: [])
    monkeypatch.setattr(service, "_dimension_values", read)
    assert service._existing_dimension_values(
        session, object(), version, "维度甲", requested, object()) == expected
    assert checked == list(components)


@pytest.mark.parametrize("guard", ["status", "drift", "dimension"])
def test_batch1_derived_dimension_exact_values_keep_governance_guards(monkeypatch, guard):
    service = _batch1_ask_service
    version = _Batch1NS(kind="derived", status="published", dimensions=[{"name": "维度甲"}],
                       components=[{"version_id": "version_a"}])
    if guard == "status":
        version.status = "affected"
    if guard == "dimension":
        version.dimensions = []
    monkeypatch.setattr(service, "dependency_drift",
                        lambda *args: [{"column": "column_a"}] if guard == "drift" else [])
    monkeypatch.setattr(service, "_dimension_values", lambda *args, **kwargs: pytest.fail("守卫未通过不能读取"))
    assert service._existing_dimension_values(
        None, object(), version, "维度甲", ["成员甲"], object()) is None


def test_batch1_exact_dimension_preserves_database_match_semantics(monkeypatch):
    session, version, state, _ = _batch1_dimension_reader(monkeypatch, SqlDialect("mysql", "catalog_a"))
    state["rows"] = [[1, 1]]
    requested = ["成员甲A", "01"]
    assert _batch1_ask_service._existing_dimension_values(
        session, object(), version, "维度甲", requested, object()) == set(requested)
    tree = sqlglot.parse_one(state["queries"][0][0], read="mysql")
    predicates = [node.find(exp.In) for node in tree.find_all(exp.Case)]
    assert [node.expressions[0].this for node in predicates] == requested
    assert [literal.this for literal in tree.find(exp.Where).find(exp.In).expressions] == requested


@pytest.mark.parametrize("rows,expected", [
    ([], set()), ([[None]], set()), ([[0]], set()), ([[1]], {"成员甲"}),
    ([["1"]], {"成员甲"}), ([[2]], None), ([[1, 0]], None), ([[1], [0]], None),
])
def test_batch1_exact_dimension_query_checks_aggregate_result_shape(monkeypatch, rows, expected):
    session, version, state, _ = _batch1_dimension_reader(monkeypatch, SqlDialect("mysql", "catalog_a"))
    state["rows"] = rows
    assert _batch1_ask_service._existing_dimension_values(
        session, object(), version, "维度甲", ["成员甲"], object()) == expected


@pytest.mark.parametrize("expression,kind", [
    ('COUNT(DISTINCT "维度甲")', "count"),
    ('AVG("指标甲")', "level"),
    ('MIN("指标甲")', "level"),
    ('MAX("指标甲")', "level"),
    ('MEDIAN("指标甲")', "level"),
    ('PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "指标甲")', "level"),
    ('APPROX_COUNT_DISTINCT("维度甲")', "count"),
    ('APPROX_PERCENTILE("指标甲", 0.5)', "level"),
    ('(AVG("指标甲"))', "level"),
    ('CAST(MAX("指标甲") AS DECIMAL(18, 4))', "level"),
])
@pytest.mark.parametrize("operation", ["contribution", "derive"])
def test_batch1_explore_expression_evidence_blocks_nonadditive_member_sums(monkeypatch, expression, kind, operation):
    box = _batch1_box(monkeypatch)
    box._dataset("explore", "指标甲", ["维度甲", "指标甲", "指标乙"],
                 [["成员甲", 12, 10], ["成员乙", 8, 5]], meta={
                     "units": {"指标甲": {"kind": kind}, "指标乙": {"kind": "level"}},
                     "column_evidence": {"指标甲": {"expression": f'{expression} AS "指标甲"'}}})
    if operation == "derive":
        result = box.derive("指标丙", [{"name": "a", "dataset": "D1", "column": "指标甲"}], "a")
    else:
        result = box.contribution("D1", ["维度甲"], "指标甲", "指标乙")
    assert "不能按成员相加" in json.loads(result)["error"]


@pytest.mark.parametrize("expression,kind", [(None, "level"), (None, "count"),
                                               ('SUM("指标甲")', "level"), ('COUNT(*)', "count")])
def test_batch1_explore_additivity_keeps_unit_fallback_without_expression(monkeypatch, expression, kind):
    box = _batch1_box(monkeypatch)
    box._dataset("explore", "指标甲", ["维度甲", "指标甲", "指标乙"], [["成员甲", 12, 10]], meta={
        "units": {"指标甲": {"kind": kind}, "指标乙": {"kind": "level"}},
        "column_evidence": {"指标甲": {"expression": expression}, "其他列": {"expression": 'AVG("指标乙")'}}})
    result = json.loads(box.contribution("D1", ["维度甲"], "指标甲", "指标乙"))
    assert "dataset" in result and result["summary"]["total"]["change"] == "2"


@pytest.mark.parametrize("missing_column", ["本期", "上期"])
@pytest.mark.parametrize("missing_value", [None, ""])
def test_contribution_with_null_suppresses_total_change_and_all_member_shares(missing_column, missing_value):
    incomplete = {"维度甲": "成员乙", "本期": 3, "上期": 2}
    incomplete[missing_column] = missing_value
    out = calc.contribution([{"维度甲": "成员甲", "本期": 11, "上期": 5}, incomplete],
                            ["维度甲"], "本期", "上期")
    assert out["total"] == {"current": Decimal(11 if missing_column == "本期" else 14),
                            "previous": Decimal(5 if missing_column == "上期" else 7),
                            "change": None, "change_rate": None}
    assert all(row["share_of_change"] is None for row in out["rows"])
    complete = next(row for row in out["rows"] if row["维度甲"] == "成员甲")
    assert complete["change"] == 6 and complete["change_rate"] == Decimal("1.2000")
    assert out["summary"]["missing_values"] == 1
    assert "已知部分合计" in out["summary"]["totals_note"]


@pytest.mark.parametrize("total,factor", [((1000, 1001), (1000, "1000.75")),
                                          ((1001, 1000), ("1000.75", 1000))])
def test_factor_decompose_exposes_subunit_residual_for_integer_totals(total, factor):
    out = calc.factor_decompose(total, {"指标甲": factor, "指标乙": (1, 1)})
    parts = {row["factor"]: row["contribution"] for row in out["factors"]}
    assert abs(out["unexplained"]) == pytest.approx(Decimal("0.25"), abs=Decimal("1e-10"))
    assert parts["未解释差额"] == out["unexplained"]
    assert sum(parts.values()) == out["total"]["change"]
    assert abs(parts["指标甲"]) == pytest.approx(Decimal("0.75"), abs=Decimal("1e-10"))


@pytest.mark.parametrize("total,factors,unexplained", [
    # 总量量级远大于变化、输入完全一致：浮点误差不能报成未解释差额（PR #26 评审）
    (("1000000000000", "1000000000000.001"), {"指标甲": ("1000000000000", "1000000000000.001"), "指标乙": (1, 1)}, 0),
    (("5000", "4400"), {"指标甲": (100, 80), "指标乙": (50, 55)}, 0),
    (("100", "100"), {"指标甲": (10, 20), "指标乙": (10, 5)}, 0),
    # 大额正负贡献相互抵消、净变化很小：尾差校正以精确净变化为准，不能被舍成 0（PR #26 复核）
    (("1000000000000", "1000000000000.001"), {"指标甲": ("1000000", "10000000"), "指标乙": ("1000000", "100000.0000000001")}, 0),
    # 输入不一致：差额只来自总量与因子之积之差，照常单列
    (("1000000000000", "1000000000000.003"), {"指标甲": ("1000000000000", "1000000000000.001"), "指标乙": (1, 1)},
     Decimal("0.002")),
])
def test_factor_decompose_separates_input_mismatch_from_float_error(total, factors, unexplained):
    out = calc.factor_decompose(total, factors)
    parts = {row["factor"]: row["contribution"] for row in out["factors"]}
    assert out["unexplained"] == unexplained
    assert ("未解释差额" in parts) == bool(unexplained)
    assert sum(parts.values()) == out["total"]["change"]  # 贡献 + 未解释差额精确等于总变化
    assert all("E" not in str(v) for v in [*parts.values(), out["unexplained"]])


def _batch1_additive_derived_box(monkeypatch, formula, second_expression='COUNT(*)'):
    components = [{"metric_code": "a", "version_id": "组成版本甲"},
                  {"metric_code": "b", "version_id": "组成版本乙"}]
    box = _batch1_box(monkeypatch, formula, kind="derived", components=components)
    box.session.store[(MetricVersion, "组成版本甲")] = SimpleNamespace(kind="base", expression='SUM("指标甲")')
    box.session.store[(MetricVersion, "组成版本乙")] = SimpleNamespace(kind="base", expression=second_expression)
    _batch1_governed(box)
    return box


@pytest.mark.parametrize("formula", ["a + b", "a - b", "-a", "a - (-b)", "((a + b) - b)"])
@pytest.mark.parametrize("operation", ["contribution", "calendar_adjust", "derive"])
def test_batch1_governed_additive_component_sums_allow_member_sums(monkeypatch, formula, operation):
    from avelis.analysis import attribution

    box = _batch1_additive_derived_box(monkeypatch, formula)
    if operation == "derive":
        result = box.derive("指标乙", [{"name": "a", "dataset": "D1", "column": "指标甲"}], "a")
    elif operation == "calendar_adjust":
        result = box.calendar_adjust("D1", "指标甲", "指标甲·上一期", {"维度甲": "成员甲"})
    else:
        result = box.contribution("D1", ["维度甲"], "指标甲", "指标甲·上一期")
    assert "dataset" in json.loads(result)
    version = box.catalog.versions["metric_a"][1]
    components = {code: box.session.get(MetricVersion, identifier)
                  for code, identifier in (("a", "组成版本甲"), ("b", "组成版本乙"))}
    assert attribution.applicability(version, components)["algorithm"] is None


@pytest.mark.parametrize("formula", ["a * b", "a / b", "a + 1", "a - 0", "a + (b * 1)", "a ** b"])
def test_batch1_governed_other_derived_formulas_still_reject_member_sums(monkeypatch, formula):
    box = _batch1_additive_derived_box(monkeypatch, formula)
    result = json.loads(box.contribution("D1", ["维度甲"], "指标甲", "指标甲·上一期"))
    assert "不能按成员相加" in result["error"]


@pytest.mark.parametrize("second_expression", ['AVG("指标乙")', 'COUNT(DISTINCT "维度甲")', None])
def test_batch1_governed_additive_formula_requires_all_locked_additive_components(monkeypatch, second_expression):
    box = _batch1_additive_derived_box(monkeypatch, "a + b", second_expression)
    if second_expression is None:
        box.session.store.pop((MetricVersion, "组成版本乙"))
    result = json.loads(box.contribution("D1", ["维度甲"], "指标甲", "指标甲·上一期"))
    assert "不能按成员相加" in result["error"]


@pytest.mark.parametrize("grain", ["week", "month", "quarter", "year"])
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("explicit_period", [False, True])
def test_batch1_explore_coverage_refuses_saved_nonday_truncation(monkeypatch, grain, wrapped, explicit_period):
    box = _batch1_box(monkeypatch)
    expression = f'DATE_TRUNC(\'{grain}\', "日期甲")'
    if wrapped:
        expression = f'CAST({expression} AS DATE)'
    box._dataset("explore", "期间甲", ["维度甲", "期间"], [["成员甲", "2026-01-01"]], meta={
        "time_column": "期间", "column_evidence": {"期间": {"expression": f'{expression} AS "期间"'}}})
    result = json.loads(box.coverage("D1", keys=["维度甲"], period="期间" if explicit_period else None,
                                    start_date=date(2026, 1, 1), end_date=date(2026, 1, 3)))
    assert result["error"] == "非日粒度，不能按天判断缺失"


@pytest.mark.parametrize("expression", [None, '"日期甲" AS "期间"',
                                        'DATE_TRUNC(\'day\', "日期甲") AS "期间"'])
def test_batch1_explore_coverage_keeps_daily_or_no_expression_behavior(monkeypatch, expression):
    box = _batch1_box(monkeypatch)
    box._dataset("explore", "期间甲", ["维度甲", "期间"], [["成员甲", "2026-01-01"]], meta={
        "column_evidence": {"期间": {"expression": expression},
                            "其他列": {"expression": 'DATE_TRUNC(\'month\', "日期乙")'}}})
    result = json.loads(box.coverage("D1", keys=["维度甲"], period="期间",
                                    start_date=date(2026, 1, 1), end_date=date(2026, 1, 3)))
    assert result["summary"]["expected_periods"] == 3
    assert result["rows"][0][result["columns"].index("missing_periods")] == "2026-01-02,2026-01-03"


@pytest.mark.parametrize("left,right,expected", [
    ('COUNT(*)', 'COUNT("指标甲")', "ratio"),
    ('COUNT(DISTINCT "维度甲")', 'COUNT(*)', "ratio"),
    ('COUNT(*)', 'COUNT(DISTINCT "维度甲")', "ratio"),
    ('SUM("指标甲")', 'SUM("指标甲")', "unknown"),
    ('SUM("指标甲")', 'COUNT(*)', "unknown"),
    ('COUNT(*)', 'AVG("指标甲")', "unknown"),
    ('MIN("指标甲")', 'MAX("指标甲")', "unknown"),
    ('SUM(CASE WHEN "维度甲" = \'成员甲\' THEN 1 ELSE 0 END)', 'COUNT(*)', "ratio"),
])
@pytest.mark.parametrize("engine", ["postgres", "starrocks"])
def test_batch1_exploration_cte_aggregate_division_uses_inferred_kinds(left, right, expected, engine):
    from avelis.analysis.exploration import units
    from avelis.analysis.explore import PROFILES, BoundColumn, BoundTable, validate

    table = BoundTable("资产甲", "版本甲", "物理甲", "catalog_a", "schema_a", "table_a", "table",
                       (BoundColumn("指标甲", "decimal(18,8)"), BoundColumn("维度甲", "varchar(64)")))
    validated = validate(
        f'WITH base AS (SELECT {left} AS "指标乙", {right} AS "指标丙" FROM schema_a.table_a), '
        'renamed AS (SELECT "指标乙" AS "指标丁", "指标丙" AS "指标戊" FROM base) '
        'SELECT "指标丁" / "指标戊" AS "指标己" FROM renamed',
        PROFILES[engine], lambda catalog, db, name: table, max_rows=10)
    assert units(validated.columns) == {"指标己": {"kind": expected}}
    assert validated.columns[0].eligible and not validated.columns[0].assumption
