"""制造演示的离线数据、场景与评测契约；不连接数据库、不读演示状态。"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

import mfg_data
import pytest
import sqlglot
import yaml
from sqlglot import exp

from avelis import demo_setup
from tests.eval import scoring, scoring_v2, scoring_v2_1, scoring_v2_1_1
from tests.pymysql_contract import mogrify

ROOT = Path(__file__).resolve().parents[3]
QUESTIONS = ROOT / "backend/tests/eval/analysis_questions_mfg.yaml"


@pytest.fixture(scope="module")
def dataset():
    return mfg_data.generate()


def _decimal(value) -> Decimal:
    return Decimal(str(value))


def _has_chinese(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", value))


def _records(dataset, table):
    return [dict(zip(mfg_data.TABLE_COLUMNS[table], row, strict=True)) for row in dataset.tables[table]]


def _view_records(dataset, view):
    spec = dataset.truth["views_daily"][view]
    return [dict(zip(spec["columns"], row, strict=True)) for row in spec["rows"]]


def test_generation_is_row_for_row_deterministic(dataset):
    other = mfg_data.generate()
    assert mfg_data.VERSION and isinstance(mfg_data.SEED, int)
    assert mfg_data.START == date(2026, 6, 1) and mfg_data.END == date(2026, 9, 30)
    assert dataset.tables == other.tables
    assert dataset.truth == other.truth
    assert mfg_data.checksum(dataset) == mfg_data.checksum(other)
    assert set(table.split(".")[0] for table in dataset.tables) <= set(mfg_data.DATABASES)
    assert dataset.truth["timezone"] == "Asia/Shanghai"
    assert set(dataset.truth["scenarios"]) == {"material_batch", "planned_maintenance", "new_line", "mid_autumn",
                                                "offsetting", "energy_gap", "stable", "energy_relationship"}
    for scenario in dataset.truth["scenarios"].values():
        assert scenario["before"]["period"] and scenario["after"]["period"]
        assert scenario["primary_entity"] and scenario["mechanism_assets"]


def test_truth_command_only_generates_data(dataset, monkeypatch, capsys):
    import demo

    def unexpected_external_action(*args, **kwargs):
        raise AssertionError("truth 不得读取环境文件或调用数据库操作")

    monkeypatch.setattr(demo, "load_env", unexpected_external_action)
    monkeypatch.setattr(mfg_data, "load", unexpected_external_action)
    monkeypatch.setattr(mfg_data, "check", unexpected_external_action)
    monkeypatch.setattr(sys, "argv", ["mfg_data.py", "truth"])
    mfg_data.main()
    assert json.loads(capsys.readouterr().out) == dataset.truth


def test_all_tables_columns_and_views_have_chinese_comments(dataset):
    assert set(dataset.tables) == set(mfg_data.TABLE_COLUMNS) == set(mfg_data.TABLE_METADATA)
    assert len(mfg_data.TABLES) == len(dataset.tables)
    for table, metadata in mfg_data.TABLE_METADATA.items():
        assert _has_chinese(metadata["comment"]), table
        assert metadata["key"]
        ddl = next(ddl for ddl in mfg_data.TABLES if ddl.startswith(f"CREATE TABLE {table} ("))
        assert f"COMMENT '{metadata['comment']}'" in ddl
        assert [column[0] for column in metadata["columns"]] == mfg_data.TABLE_COLUMNS[table]
        for name, _, comment in metadata["columns"]:
            assert _has_chinese(comment), (table, name)
            assert re.search(rf"\b{re.escape(name)}\s+[^\n]+COMMENT '{re.escape(comment)}'", ddl), (table, name)
    assert set(mfg_data.DB_COMMENTS) == set(mfg_data.DATABASES)
    assert all(_has_chinese(comment) for comment in mfg_data.DB_COMMENTS.values())
    assert {name for name, _, _ in mfg_data.VIEWS} == set(mfg_data.VIEW_COLUMNS)
    for name, comment, sql in mfg_data.VIEWS:
        assert _has_chinese(comment)
        tree = sqlglot.parse_one(sql, dialect="starrocks")
        assert tree.named_selects == [column for column, _ in mfg_data.VIEW_COLUMNS[name]]
        ddl = mfg_data._view_ddl(name, comment, sql)
        for column, text in mfg_data.VIEW_COLUMNS[name]:
            assert _has_chinese(text), (name, column)
            assert f"{column} COMMENT '{text}'" in ddl


def test_primary_keys_and_business_relationships_are_consistent(dataset):
    rows = {table: _records(dataset, table) for table in dataset.tables}
    for table, records in rows.items():
        key = mfg_data.TABLE_METADATA[table]["key"]
        values = [tuple(row[column] for column in key) for row in records]
        assert len(values) == len(set(values)), table
    factories = {r["factory_id"]: r for r in rows["mfg_mes.factory"]}
    lines = {r["line_id"]: r for r in rows["mfg_mes.production_line"]}
    models = {r["model_id"] for r in rows["mfg_mes.equipment_model"]}
    families = {r["product_family_id"] for r in rows["mfg_mes.product_family"]}
    shifts = {r["shift_id"] for r in rows["mfg_mes.shift"]}
    suppliers = {r["supplier_id"] for r in rows["mfg_scm.supplier"]}
    materials = {r["material_id"] for r in rows["mfg_scm.material"]}
    receipts = {r["batch_no"]: r for r in rows["mfg_scm.material_receipt"]}
    assert len(receipts) == len(rows["mfg_scm.material_receipt"])
    assert len(factories) == 3 and len(families) == 4
    assert {r["shift_name"] for r in rows["mfg_mes.shift"]} == {"早", "中", "夜"}
    assert {r["region_name"] for r in factories.values()} == {"华东", "华南", "西南"}
    for factory_id in factories:
        assert 3 <= sum(r["factory_id"] == factory_id for r in lines.values()) <= 4
    for line in lines.values():
        assert line["factory_id"] in factories and line["model_id"] in models
        assert line["product_family_id"] in families
    for receipt in receipts.values():
        assert receipt["supplier_id"] in suppliers and receipt["material_id"] in materials
        assert receipt["factory_id"] in factories and receipt["received_qty"] > 0
    reports = {r["report_id"]: r for r in rows["mfg_mes.production_report"]}
    for report in reports.values():
        receipt = receipts[report["batch_no"]]
        line = lines[report["line_id"]]
        assert receipt["receipt_date"] <= report["production_date"]
        assert receipt["factory_id"] == line["factory_id"]
        assert report["production_date"] >= line["launch_date"]
        assert report["shift_id"] in shifts and report["product_family_id"] in families
        assert report["production_qty"] == report["good_qty"] + report["defect_qty"] > 0
        assert min(report["good_qty"], report["defect_qty"], report["work_hours"]) >= 0
    for inspection in rows["mfg_qms.inspection"]:
        report = reports[inspection["report_id"]]
        assert inspection["line_id"] == report["line_id"]
        assert inspection["shift_id"] == report["shift_id"]
        assert inspection["inspected_qty"] == report["production_qty"]
        assert inspection["defect_qty"] == report["defect_qty"]
    for table, time_column in (("mfg_mes.schedule", "stat_date"), ("mfg_mes.energy_reading", "reading_date")):
        logical_keys = [(r[time_column], r["line_id"], r["shift_id"]) for r in rows[table]]
        assert len(logical_keys) == len(set(logical_keys))
        assert all(r["line_id"] in lines and r["shift_id"] in shifts for r in rows[table])
    assert {r["maintenance_type"] for r in rows["mfg_eam.maintenance_order"]} == {"计划性", "故障性"}


def test_downtime_intervals_are_bounded_nonoverlapping_and_cover_work_orders(dataset):
    orders = {row["order_id"]: row for row in _records(dataset, "mfg_eam.maintenance_order")}
    intervals = defaultdict(list)
    totals = defaultdict(int)
    for row in _records(dataset, "mfg_eam.downtime"):
        order = orders[row["maintenance_order_id"]]
        start, end = row["start_time"], row["end_time"]
        assert start < end and row["duration_minutes"] == int((end - start).total_seconds() // 60)
        assert row["stat_date"] == start.date() and row["duration_minutes"] <= 1440
        assert order["start_time"] <= start < end <= order["end_time"]
        assert row["line_id"] == order["line_id"]
        intervals[row["line_id"]].append((start, end))
        totals[order["order_id"]] += row["duration_minutes"]
    for spans in intervals.values():
        spans.sort()
        assert all(previous[1] <= current[0] for previous, current in zip(spans, spans[1:]))
    for order_id, order in orders.items():
        assert order["duration_minutes"] == int((order["end_time"] - order["start_time"]).total_seconds() // 60)
        assert totals[order_id] == order["duration_minutes"]


def test_daily_truth_reconciles_to_source_facts_without_losing_missing_energy(dataset):
    daily = _view_records(dataset, "dws_production_daily")
    keyed = {(r["stat_date"], r["line_id"]): r for r in daily}
    assert len(keyed) == len(daily) == len(mfg_data.days()) * len(mfg_data.LINES)
    # 每个生产日的报工与计量独立归并，防止多事实表联接造成重复计量。
    for source, day_column, pairs in (
        ("mfg_mes.production_report", "production_date", (("production_qty", "production_qty"), ("good_qty", "good_qty"),
                                                         ("defect_qty", "defect_qty"), ("work_hours", "work_hours"))),
        ("mfg_eam.downtime", "stat_date", (("duration_minutes", "downtime_minutes"),)),
        ("mfg_mes.energy_reading", "reading_date", (("energy_kwh", "energy_kwh"),)),
    ):
        totals = defaultdict(lambda: defaultdict(Decimal))
        for row in _records(dataset, source):
            key = (str(row[day_column]), row["line_id"])
            for column, output in pairs:
                totals[key][output] += _decimal(row[column])
        for key, row in keyed.items():
            for _, output in pairs:
                if output == "energy_kwh" and key not in totals:
                    assert row[output] is None
                else:
                    assert _decimal(row[output]) == totals[key][output]
    actual_readings = Counter((str(row["reading_date"]), row["line_id"])
                              for row in _records(dataset, "mfg_mes.energy_reading"))
    expected_readings = Counter((str(row["stat_date"]), row["line_id"])
                                for row in _records(dataset, "mfg_mes.schedule")
                                if row["schedule_status"] != "未投产")
    downtime_events = Counter((str(row["stat_date"]), row["line_id"])
                             for row in _records(dataset, "mfg_eam.downtime"))
    for key, row in keyed.items():
        assert int(row["energy_reading_count"]) == actual_readings[key]
        assert int(row["expected_reading_count"]) == expected_readings[key]
        assert int(row["downtime_count"]) == downtime_events[key]
    material = _view_records(dataset, "dws_material_daily")
    maintenance = _view_records(dataset, "dws_maintenance_daily")
    assert sum(int(r["batch_count"]) for r in material) == len(dataset.tables["mfg_scm.material_receipt"])
    assert sum(_decimal(r["received_qty"]) for r in material) == sum(r[6] for r in dataset.tables["mfg_scm.material_receipt"])
    assert sum(int(r["order_count"]) for r in maintenance) == len(dataset.tables["mfg_eam.maintenance_order"])
    assert sum(int(r["maintenance_minutes"]) for r in maintenance) == sum(r[8] for r in dataset.tables["mfg_eam.maintenance_order"])
    gap = keyed[("2026-09-18", "SW2")]
    assert gap["energy_kwh"] is None and int(gap["production_qty"]) > 0
    assert int(gap["energy_reading_count"]) == 0 < int(gap["expected_reading_count"]) == 3


def test_production_view_joins_only_unique_grains_and_yield_does_not_include_receipts():
    sql = next(sql for name, _, sql in mfg_data.VIEWS if name == "mfg_dw.dws_production_daily")
    tree = sqlglot.parse_one(sql, dialect="starrocks")
    relations = [tree.args["from_"].this, *(join.this for join in tree.args["joins"])]
    sources = {relation.alias_or_name: relation for relation in relations}
    for alias, date_column in (("s", "stat_date"), ("p", "production_date"), ("d", "stat_date"), ("e", "reading_date")):
        relation = sources[alias]
        assert isinstance(relation, exp.Subquery)
        assert {column.name for column in relation.this.args["group"].expressions} == {date_column, "line_id"}
        if alias != "s":
            join = next(join for join in tree.args["joins"] if join.this.alias_or_name == alias)
            assert join.side == "LEFT"
            pairs = [{(column.table, column.name) for column in equals.find_all(exp.Column)}
                     for equals in join.args["on"].find_all(exp.EQ)]
            assert {(alias, date_column), ("s", "stat_date")} in pairs
            assert {(alias, "line_id"), ("s", "line_id")} in pairs
    # 维表主键完整性由上面的数据测试核对；这里限制多行事实不能直接连入。
    direct_tables = {f"{relation.db}.{relation.name}" for relation in relations if isinstance(relation, exp.Table)}
    assert direct_tables == {"mfg_mes.production_line", "mfg_mes.factory", "mfg_mes.equipment_model", "mfg_mes.product_family"}
    for alias, target in (("l", ("s", "line_id")), ("f", ("l", "factory_id")),
                          ("em", ("l", "model_id")), ("pf", ("l", "product_family_id"))):
        relation = sources[alias]
        primary_key = mfg_data.TABLE_METADATA[f"{relation.db}.{relation.name}"]["key"]
        assert len(primary_key) == 1
        join = next(join for join in tree.args["joins"] if join.this.alias_or_name == alias)
        assert {(column.table, column.name) for column in join.args["on"].find_all(exp.Column)} == {
            (alias, primary_key[0]), target}
    energy_projection = next(item for item in tree.selects if item.alias_or_name == "energy_kwh")
    assert isinstance(energy_projection, exp.Column) and energy_projection.table == "e"
    sources_used = {f"{table.db}.{table.name}" for table in tree.find_all(exp.Table)}
    assert "mfg_scm.material_receipt" not in sources_used
    assert "batch_no" in mfg_data.TABLE_COLUMNS["mfg_scm.material_receipt"]
    assert "batch_no" in mfg_data.TABLE_COLUMNS["mfg_mes.production_report"]
    assert dict((row[0], row[2]) for row in demo_setup.MFG_DERIVED)["mfg_yield"] == "mfg_good / mfg_production"
    metric_views = {metric[2] for metric in demo_setup.MFG_METRICS if metric[0] in ("mfg_good", "mfg_production")}
    assert metric_views == {"dws_production_daily"}


def test_scenario_truth_is_calculated_from_changed_source_rows(dataset):
    changed = mfg_data.Dataset(tables={name: list(rows) for name, rows in dataset.tables.items()})
    columns = mfg_data.TABLE_COLUMNS["mfg_mes.production_report"]
    batch_index, production_index, good_index = (columns.index(column) for column in ("batch_no", "production_qty", "good_qty"))
    rows = changed.tables["mfg_mes.production_report"]
    index = next(i for i, row in enumerate(rows) if row[batch_index] == mfg_data.BAD_BATCH)
    row = list(rows[index])
    row[production_index] += 100
    row[good_index] += 100
    rows[index] = tuple(row)
    original = dataset.truth["scenarios"]["material_batch"]
    updated = mfg_data.truth(changed)["scenarios"]["material_batch"]
    assert updated["batch_production_qty"] == original["batch_production_qty"] + 100
    assert updated["after"]["production_qty"] == original["after"]["production_qty"] + 100
    assert updated["after"]["good_qty"] == original["after"]["good_qty"] + 100
    assert updated["before"] == original["before"]


def test_material_batch_cause_has_large_defect_increase(dataset):
    scenario = dataset.truth["scenarios"]["material_batch"]
    before, after = scenario["before"], scenario["after"]
    assert Decimal("0.97") < _decimal(before["yield_rate"]) < Decimal("0.98")
    assert Decimal("0.84") < _decimal(after["yield_rate"]) < Decimal("0.87")
    assert after["defect_qty"] > 4 * before["defect_qty"]
    assert scenario["primary_entity"] == {"supplier_name": "恒达材料", "batch_no": mfg_data.BAD_BATCH}
    assert scenario["receipt_date"] == "2026-08-20"
    assert scenario["line_names"] == ["华南1号线", "华南2号线"]
    assert scenario["batch_production_qty"] == after["production_qty"]
    assert scenario["defects_by_type"] == {"裂纹": scenario["batch_defect_qty"]}
    assert scenario["outside_yield_lineage"] and "mfg_scm.material_receipt" in scenario["mechanism_assets"]


def test_planned_maintenance_explains_output_loss(dataset):
    scenario = dataset.truth["scenarios"]["planned_maintenance"]
    assert Decimal("0.5") < _decimal(scenario["after"]["production_qty"]) / scenario["before"]["production_qty"] < Decimal("0.8")
    assert scenario["shutdown_period"]["production_qty"] == 0
    assert scenario["planned_downtime_minutes"] == 7 * 24 * 60
    assert scenario["start"] == "2026-09-08 00:00:00" and scenario["end_exclusive"] == "2026-09-15 00:00:00"
    assert scenario["primary_entity"] == "华南3号线" and "mfg_eam.maintenance_order" in scenario["mechanism_assets"]


def test_new_line_growth_and_initial_low_yield(dataset):
    scenario = dataset.truth["scenarios"]["new_line"]
    assert scenario["launch_date"] == "2026-08-01" and scenario["line_july"]["production_qty"] == 0
    assert scenario["line_august"]["production_qty"] > 0
    assert scenario["line_september"]["production_qty"] > scenario["line_august"]["production_qty"] * 1.2
    assert scenario["after"]["production_qty"] > scenario["before"]["production_qty"] * 1.1
    assert scenario["september"]["production_qty"] > scenario["after"]["production_qty"] * 1.01
    assert Decimal("0.88") < _decimal(scenario["first_14_days"]["yield_rate"]) < Decimal("0.91")
    assert _decimal(scenario["later_august"]["yield_rate"]) > Decimal("0.97")


def test_holiday_shutdown_affects_selected_factories_only(dataset):
    scenario = dataset.truth["scenarios"]["mid_autumn"]
    assert scenario["after"]["production_qty"] < scenario["before"]["production_qty"] * 0.4
    assert set(scenario["primary_entity"]) == {"华南工厂", "西南工厂"}
    for factory, periods in scenario["factories"].items():
        if factory in scenario["primary_entity"]:
            assert periods["after"]["production_qty"] == 0 < periods["before"]["production_qty"]
        else:
            assert abs(periods["after"]["production_qty"] / periods["before"]["production_qty"] - 1) < 0.03


def test_opposing_lines_offset_at_factory_level(dataset):
    scenario = dataset.truth["scenarios"]["offsetting"]
    up, down = (scenario["lines"][name] for name in ("华东1号线", "华东2号线"))
    assert up["after"]["production_qty"] > up["before"]["production_qty"] * 1.25
    assert down["after"]["production_qty"] < down["before"]["production_qty"] * 0.75
    assert abs(scenario["after"]["production_qty"] / scenario["before"]["production_qty"] - 1) < 0.01


def test_energy_gap_is_missing_data_with_normal_production(dataset):
    scenario = dataset.truth["scenarios"]["energy_gap"]
    before, after, following = (scenario[key] for key in ("before", "after", "following_day"))
    assert after["energy_kwh"] is None and after["unit_energy"] is None and not after["energy_complete"]
    assert after["energy_reading_count"] == 0 and after["expected_reading_count"] == 3
    assert after["production_qty"] > 0
    assert abs(after["production_qty"] / before["production_qty"] - 1) < 0.05
    assert abs(after["production_qty"] / following["production_qty"] - 1) < 0.05
    assert before["energy_complete"] and following["energy_complete"]


def test_stable_metric_stays_within_noise(dataset):
    scenario = dataset.truth["scenarios"]["stable"]
    before, after = (_decimal(scenario[key]["unit_energy"]) for key in ("before", "after"))
    assert Decimal("0.64") < before < Decimal("0.66")
    assert abs(after / before - 1) < Decimal("0.002")


def test_equipment_energy_relationship_survives_two_periods(dataset):
    scenario = dataset.truth["scenarios"]["energy_relationship"]
    for period in ("before", "after"):
        intensities = {model: _decimal(values["energy_kwh"]) / values["production_qty"]
                       for model, values in scenario[period]["models"].items()}
        assert sorted(intensities, key=intensities.get) == ["精益A型", "标准B型", "重载C型"]
        assert intensities["重载C型"] > 2 * intensities["精益A型"]
    assert "相关" in scenario["interpretation"] and "因果" in scenario["interpretation"]


def test_metric_specs_reference_existing_view_columns():
    for code, _, view, expression, time_column, dims, grain, _ in demo_setup.MFG_METRICS:
        columns = {column for column, _ in mfg_data.VIEW_COLUMNS[f"mfg_dw.{view}"]}
        expression_columns = {column.name for column in sqlglot.parse_one(expression, dialect="starrocks").find_all(exp.Column)}
        assert expression_columns <= columns, code
        assert {time_column, *grain, *(dimension["column"] for dimension in dims)} <= columns, code
        assert demo_setup.MFG_UNITS[code], code
    assert demo_setup.MFG_UNITS["mfg_yield"] is None


def test_question_set_loads_with_generated_truth_and_no_implicit_demo_state(dataset, monkeypatch, tmp_path):
    def unexpected_demo_truth():
        raise AssertionError("制造题集必须使用显式真值，不能访问演示状态")

    monkeypatch.setattr(scoring, "load_truth", unexpected_demo_truth)
    raw = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    loaded = scoring_v2_1_1.load_questions(truth=dataset.truth, spec_path=QUESTIONS)
    questions = {question["id"]: question for question in loaded["questions"]}
    assert set(questions) == {f"m{i:02d}" for i in range(1, 9)} | {"ma01"}
    assert {question["category"] for question in questions.values()} >= {
        "explore", "dimension", "calendar", "governance", "negative", "adversarial"}
    assert questions["m08"]["question"] == raw["questions"]["m08"]["question"]
    assert questions["m08"]["expect"]["conclusion"] == ["ranked_reasons"]
    assert questions["ma01"]["user"] == "sun" and questions["ma01"]["expect"]["denied_ok"]
    assert questions["ma01"]["v2"]["unauthorized"] == {"metric_prefix": "mfg_", "table_db_prefix": "mfg_"}
    for question_id, original in raw["questions"].items():
        assert original["why"] and _has_chinese(original["why"]), question_id
        for mechanism in original.get("mechanism", []):
            assert mechanism["why"], question_id
        if question_id == "ma01":
            continue
        key = original["expect"]["key_value"]
        assert "truth" in key and "value" not in key and "any_of" not in key, question_id
        refs = key["truth"] if isinstance(key["truth"], list) else [key["truth"]]
        bindings = questions[question_id]["v2"]["key_bindings"]
        assert len(refs) == len(bindings) > 0
        for reference, binding in zip(refs, bindings, strict=True):
            assert reference["table"] in dataset.truth
            assert binding["value"] is None
            assert binding["truth"] == scoring_v2.truth_periods(dataset.truth, reference, questions[question_id]["window"])
        assert questions[question_id]["expect"]["key_value"]["any_of"]
    assert questions["m01"]["business"]["direction"] == "down"
    assert questions["m02"]["business"]["direction"] == "down"
    assert questions["m03"]["business"]["direction"] == "up"
    assert questions["m05"]["expect"]["conclusion"] == ["offsetting"]
    assert questions["m07"]["expect"]["conclusion"] == ["no_material_change"]
    assert questions["m01"]["v2_1"]["mechanism"][0]["asset"] == "mfg_scm.material_receipt"
    assert "mfg_scm.material_receipt" not in scoring_v2_1.view_lineage()["mfg_dw.dws_production_daily"]

    # 与运行器用同一入口核对 --questions / --truth 的实际来源绑定。
    import eval_analysis

    truth_path = tmp_path / "mfg-truth.json"
    truth_path.write_text(json.dumps(dataset.truth, ensure_ascii=False), encoding="utf-8")
    data, parallel_data, scorer = eval_analysis.load_question_set(QUESTIONS, truth_path)
    assert data == parallel_data == loaded and scorer is scoring_v2_1_1
    with pytest.raises(ValueError, match="truth"):
        eval_analysis.load_question_set(QUESTIONS)


def test_grant_uses_select_only_for_manufacturing_databases(monkeypatch):
    import demo

    statements = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            mogrify(sql, params)
            statements.append(sql)

    class Connection:
        def cursor(self):
            return Cursor()

    monkeypatch.setattr(demo, "SR_USERS", ("avelis_reader", "avelis_collector"))
    mfg_data.grant(Connection(), {})
    assert len(statements) == len(demo.SR_USERS) * len(mfg_data.DATABASES) * 2
    assert all(re.fullmatch(r"GRANT SELECT ON ALL (TABLES|VIEWS) IN DATABASE mfg_\w+ TO USER '[a-z_]+'@'%'", sql)
               for sql in statements)
    assert all(any(f"DATABASE {database} " in sql for sql in statements) for database in mfg_data.DATABASES)


def test_setup_domain_members_examples_and_datasource_are_seeded_offline(monkeypatch):
    calls = []
    published = []
    assets = [{"qualified": f"default_catalog.mfg_dw.{view}", "id": f"asset-{view}", "domain_id": None}
              for view in {metric[2] for metric in demo_setup.MFG_METRICS}]

    class Api:
        def __init__(self, client, username, password):
            self.username = username

        def __call__(self, method, path, **kwargs):
            calls.append((self.username, method, path, kwargs))
            if method == "GET":
                return assets if path == "/api/assets" else []
            if method == "POST" and path == "/api/domains":
                return {"id": "domain-mfg", **kwargs["json"]}
            if method == "POST" and path == "/api/datasources":
                return {"id": "source-mfg", **kwargs["json"]}

    monkeypatch.setattr(demo_setup, "_Api", Api)
    monkeypatch.setattr(demo_setup, "_publish_mfg_metrics", lambda *args: published.append(args))
    users = {name: f"user-{name}" for name in demo_setup.USERS}
    env = {"SR_QUERY_PORT": "9030", "SR_COLLECTOR_PASSWORD": "collector-test", "SR_READER_PASSWORD": "reader-test"}
    client = object()
    demo_setup._setup_mfg(client, Api(client, "admin", "test"), "test", users, env, metrics=True)

    domain = next(kw["json"] for _, method, path, kw in calls if method == "POST" and path == "/api/domains")
    assert (domain["code"], domain["name"], domain["owner_id"]) == ("mfg", "生产运营", users["zhang"])
    members = [kw["json"]["user_id"] for _, _, path, kw in calls if path.endswith("/members")]
    assert set(members) == {users[name] for name in ("wang", "li", "zhao")}
    assert users["sun"] not in members
    examples = next(kw["json"]["questions"] for _, _, path, kw in calls if path.endswith("/example-questions"))
    assert len(examples) == 3 and all(item["mode"] == "analysis" and item["text"] for item in examples)
    source = next(kw["json"] for _, method, path, kw in calls if method == "POST" and path == "/api/datasources")
    assert source["name"] == "制造数仓（StarRocks）" and source["kind"] == "starrocks"
    assert source["options"]["databases"] == list(mfg_data.DATABASES)
    assert source["collect"]["username"] == "avelis_collector"
    assert source["query"]["username"] == "avelis_reader"
    assert any(path == "/api/datasources/source-mfg/collect?wait=true" for _, _, path, _ in calls)
    assigned = [path.rsplit("/", 1)[1] for _, method, path, kw in calls if method == "PATCH"
                and kw["json"] == {"domain_id": "domain-mfg"}]
    assert set(assigned) == {asset["id"] for asset in assets}
    assert published == [(client, "test", {asset["qualified"]: asset["id"] for asset in assets}, "domain-mfg")]


def test_metric_publisher_approves_base_before_derived_and_carries_units(monkeypatch):
    created, versions, approved = [], [], []
    view_assets = {f"default_catalog.mfg_dw.{metric[2]}": metric[2] for metric in demo_setup.MFG_METRICS}

    class Api:
        def __init__(self, client, username, password):
            self.username = username

        def __call__(self, method, path, **kwargs):
            if method == "GET" and path == "/api/metrics":
                return []
            if method == "POST" and path == "/api/metrics":
                created.append(kwargs["json"])
                return {"id": kwargs["json"]["code"]}
            if path.endswith("/versions"):
                code = path.split("/")[3]
                versions.append((code, kwargs["json"], tuple(approved)))
                return {"id": code}
            if path.endswith("/approve"):
                assert self.username == "li"
                approved.append(path.split("/")[3])

    monkeypatch.setattr(demo_setup, "_Api", Api)
    demo_setup._publish_mfg_metrics(object(), "test", view_assets, "domain-mfg")
    base = [metric[0] for metric in demo_setup.MFG_METRICS]
    derived = [metric[0] for metric in demo_setup.MFG_DERIVED]
    assert [item["code"] for item in created] == base + derived == approved
    assert all(item["domain_id"] == "domain-mfg" for item in created)
    for code, spec, previous_approvals in versions:
        assert spec["unit"] == demo_setup.MFG_UNITS[code]
        assert spec["timezone"] == mfg_data.TIMEZONE
        if code == "mfg_yield":
            assert spec["unit"] is None
        else:
            assert spec["unit"]
        if code in derived:
            dependencies = re.findall(r"\bmfg_\w+", spec["expression"])
            assert spec["kind"] == "derived" and set(dependencies) <= set(previous_approvals)
        else:
            assert spec["dimensions"] and spec["grain"] and spec["time_column"]
