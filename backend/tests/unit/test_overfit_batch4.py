"""过拟合审查第四批：使用中性数据和会话替身，不连接数据库或模型。"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from avelis import config, configfile
from avelis.analysis import agent, calc, investigation
from avelis.api import app as api_app, deps, schemas
from avelis.ask import agent as ask_agent, examples, intent, service
from avelis.ask.intent import DemoIntentModel, MetricCard
from avelis.governance import business_calendar
from avelis.models import AppSetting, AuditEvent
from tests.unit.test_analysis_investigation import (
    CLOSURE,
    WINDOW,
    attribution_dataset,
    dataset,
    event_dataset,
    report,
    trend_dataset,
    uniform_distribution,
)
from tests.unit.test_overfit_batch1 import _batch1_box, _batch1_governed

SECRET = "offline-unit-test-secret-0123456789"
CALENDAR_PATH = "/api/settings/business-calendar"


class CalendarSession:
    """仅保存设置与审计对象的会话替身。"""

    def __init__(self):
        self.row = None
        self.events = []
        self.roles = set()

    def get(self, model, key):
        assert model is AppSetting and key == "business_calendar"
        return self.row

    def add(self, obj):
        if isinstance(obj, AppSetting):
            self.row = obj
        else:
            assert isinstance(obj, AuditEvent)
            self.events.append(obj)

    def scalars(self, _stmt):
        return self.roles


def test_business_calendar_schema_normalizes_lists():
    value = schemas.BusinessCalendarIn(weekend_days=[6, 5, 6], holidays=["2024-02-29", "2024-01-01", "2024-02-29"],
                                       workdays=["2024-01-07", "2024-01-06", "2024-01-06"])
    assert value.model_dump() == {"weekend_days": [5, 6], "holidays": ["2024-01-01", "2024-02-29"],
                                  "workdays": ["2024-01-06", "2024-01-07"]}
    assert schemas.BusinessCalendarIn(weekend_days=[]).weekend_days == []


@pytest.mark.parametrize("value", [
    {"weekend_days": [-1]}, {"weekend_days": [7]}, {"weekend_days": [True]}, {"weekend_days": ["5"]},
    {"holidays": ["2026-02-29"]}, {"holidays": ["2026-1-01"]}, {"holidays": ["20260101"]},
    {"workdays": ["2026-01-01T00:00:00"]}, {"holidays": [date(2026, 1, 1)]},
    {"holidays": ["2026-01-01"], "workdays": ["2026-01-01"]}, {"configured": True},
    {"weekend_days": [5] * 2001}, {"holidays": ["2026-01-01"] * 2001}, {"workdays": ["2026-01-02"] * 2001},
])
def test_business_calendar_schema_rejects_invalid_values(value):
    with pytest.raises(ValidationError):
        schemas.BusinessCalendarIn(**value)


def test_business_calendar_unconfigured_defaults():
    session = CalendarSession()
    assert business_calendar.get_business_calendar(session) is None
    value = business_calendar.business_calendar_view(session)
    assert value == {"configured": False, "weekend_days": [5, 6], "holidays": [], "workdays": []}
    value["weekend_days"].clear()
    assert business_calendar.business_calendar_view(session)["weekend_days"] == [5, 6]


def test_business_calendar_save_updates_and_audits():
    session = CalendarSession()
    actor = SimpleNamespace(id="user_a", username="用户甲")
    first = {"weekend_days": [5, 6], "holidays": ["2026-01-01"], "workdays": []}
    second = {"weekend_days": [6], "holidays": [], "workdays": ["2026-01-04"]}
    assert business_calendar.save_business_calendar(session, actor, first) == {"configured": True, **first}
    original_row = session.row
    assert session.row.key == "business_calendar" and session.row.updated_by == actor.id
    assert business_calendar.save_business_calendar(session, actor, second) == {"configured": True, **second}
    assert session.row is original_row and session.row.updated_at is not None
    assert business_calendar.get_business_calendar(session) == second
    read_copy = business_calendar.get_business_calendar(session)
    read_copy["weekend_days"].clear()
    assert session.row.value == second
    assert len(session.events) == 2
    assert session.events[0].detail == {"before": None, "after": first}
    assert session.events[1].detail == {"before": first, "after": second}
    assert all(e.actor_id == actor.id and e.actor_name == actor.username and e.action == "business_calendar.update"
               and e.object_type == "app_setting" and e.object_id == "business_calendar" for e in session.events)


def test_business_calendar_api_login_and_admin_access(monkeypatch):
    monkeypatch.setattr(api_app, "get_settings", lambda: config.build_settings({"AVELIS_SECRET": SECRET}))
    app = api_app.create_app()
    session = CalendarSession()
    app.dependency_overrides[deps.get_session] = lambda: session
    value = {"weekend_days": [6, 5, 6], "holidays": ["2026-01-01"], "workdays": []}
    with TestClient(app) as client:
        assert client.get(CALENDAR_PATH).status_code == 401
        assert client.put(CALENDAR_PATH, json=value).status_code == 401
        app.dependency_overrides[deps.current_user] = lambda: SimpleNamespace(id="user_a", username="用户甲")
        assert client.get(CALENDAR_PATH).json() == {"configured": False, "weekend_days": [5, 6],
                                                   "holidays": [], "workdays": []}
        for role in (set(), {"analyst"}, {"reviewer"}, {"steward"}):
            session.roles = role
            assert client.put(CALENDAR_PATH, json=value).status_code == 403
        assert session.row is None and not session.events
        session.roles = {"admin"}
        response = client.put(CALENDAR_PATH, json=value)
        assert response.status_code == 200
        expected = {**value, "configured": True, "weekend_days": [5, 6]}
        assert response.json() == expected and client.get(CALENDAR_PATH).json() == expected
        assert len(session.events) == 1
        invalid = client.put(CALENDAR_PATH, json={"holidays": ["2026-02-29"]})
        assert invalid.status_code == 422 and len(session.events) == 1
        assert client.get(CALENDAR_PATH).json() == expected
        assert client.put(CALENDAR_PATH, json={"weekend_days": []}).json() == {
            "configured": True, "weekend_days": [], "holidays": [], "workdays": []}


def test_business_calendar_api_openapi_schemas(monkeypatch):
    monkeypatch.setattr(api_app, "get_settings", lambda: config.build_settings({"AVELIS_SECRET": SECRET}))
    schema = api_app.create_app().openapi()
    paths = schema["paths"][CALENDAR_PATH]
    for method in ("get", "put"):
        assert paths[method]["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/BusinessCalendarOut"}
        assert paths[method]["security"]
    assert paths["put"]["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/BusinessCalendarIn"}
    fields = schema["components"]["schemas"]["BusinessCalendarOut"]["properties"]
    assert set(fields) == {"configured", "weekend_days", "holidays", "workdays"}
    assert fields["holidays"]["items"]["format"] == "date"


def test_calendar_counts_weekend_holiday_overlap_and_makeup_workdays():
    out = calc.calendar_adjust(50, 40, (date(2026, 1, 5), date(2026, 1, 11)),
                               (date(2025, 12, 29), date(2026, 1, 4)),
                               holidays={date(2026, 1, 6), date(2026, 1, 10)}, workdays={date(2026, 1, 11)})
    assert out["current"]["holidays"] == 2 and out["current"]["weekend_days"] == 2
    assert out["current"]["business_days"] == 5 and out["previous"]["business_days"] == 5
    assert out["business_daily"] == {"current": Decimal(10), "previous": Decimal(8)}
    assert out["business_day_effect"] == 0 and out["business_level_effect"] == 10
    # 调休覆盖节假日时只计工作日；接口会拒绝交集，计算函数也保留确定的优先级。
    counts = calc.period_days(date(2026, 1, 5), date(2026, 1, 11), {date(2026, 1, 5)},
                              weekend_days={0, 6}, workdays={date(2026, 1, 5)})
    assert counts == {"days": 7, "weekend_days": 2, "holidays": 0, "business_days": 6}


def test_calendar_business_effects_use_full_precision_and_significant12():
    out = calc.calendar_adjust("0.00032", "0.00028", (date(2026, 1, 1), date(2026, 1, 3)),
                               (date(2025, 12, 30), date(2025, 12, 31)), holidays=set(), weekend_days=set())
    assert out["business_daily"] == {"current": Decimal("0.000106666666667"), "previous": Decimal("0.00014")}
    assert out["business_day_effect"] == Decimal("0.00014")
    assert out["business_level_effect"] == Decimal("-0.00010")
    assert out["current"]["business_days"] == 3 and out["previous"]["business_days"] == 2


@pytest.mark.parametrize("zero_period", ["current", "previous", "both"])
def test_calendar_zero_business_days_remain_unknown(zero_period):
    current, previous = date(2026, 1, 2), date(2026, 1, 1)
    holidays = {d for name, d in (("current", current), ("previous", previous))
                if zero_period in (name, "both")}
    out = calc.calendar_adjust(20, 10, (current, current), (previous, previous), holidays=holidays)
    for name in ("current", "previous"):
        assert (out["business_daily"][name] is None) == (zero_period in (name, "both"))
    assert (out["business_day_effect"] is None) == (zero_period in ("previous", "both"))
    assert (out["business_level_effect"] is None) == (zero_period in ("previous", "both"))
    assert "工作日数为 0" in out["business_days_note"]
    assert out["current"]["daily"] == 20 and out["previous"]["daily"] == 10


@pytest.mark.parametrize("configured", [False, True])
def test_calendar_tool_reads_setting_and_preserves_unconfigured_output(monkeypatch, configured):
    box = _batch1_box(monkeypatch)
    _batch1_governed(box, [["成员甲", 30, 10]])
    if configured:
        box.session.store[(AppSetting, "business_calendar")] = AppSetting(
            key="business_calendar", value={"weekend_days": [5, 6], "holidays": ["2026-01-01"],
                                            "workdays": ["2026-01-03"]})
    out = json.loads(box.calendar_adjust("D1", "指标甲", "指标甲·上一期"))
    assert "error" not in out
    rows = [dict(zip(out["columns"], row)) for row in out["rows"]]
    if configured:
        assert "holidays_note" not in out["summary"]
        assert rows[0]["business_days"] == 2 and rows[0]["holidays"] == 1
        assert rows[1]["business_days"] == 3 and rows[1]["holidays"] == 0
        assert Decimal(out["summary"]["business_daily"]["current"]) == 15
        assert Decimal(out["summary"]["business_day_effect"]) == calc._significant12(-Decimal(10) / 3)
        assert box.datasets()["D2"].meta["summary_units"]["business_day_effect"]["kind"] == "delta"
    else:
        assert out["columns"] == ["period", "days", "weekend_days", "holidays", "value", "daily"]
        assert all(row["holidays"] is None for row in rows)
        assert out["summary"]["holidays_note"] == "未配置业务日历，节假日未知"
        assert "business_daily" not in out["summary"]


@pytest.mark.parametrize("today,start,end", [
    (date(2026, 1, 1), date(2025, 12, 1), date(2025, 12, 31)),
    (date(2024, 3, 1), date(2024, 2, 1), date(2024, 2, 29)),
])
def test_generated_examples_use_business_timezone_previous_month(monkeypatch, today, start, end):
    zones = []
    monkeypatch.setattr(examples, "business_today", lambda zone: zones.append(zone) or today)
    monkeypatch.setattr(examples, "get_settings", lambda: SimpleNamespace(business_timezone="Pacific/Honolulu"))
    dimensions = [{"name": "dimension_a", "label": "维度甲"}]
    catalog = SimpleNamespace(versions={
        "metric_a": (SimpleNamespace(name="指标甲"), SimpleNamespace(dimensions=dimensions)),
        "metric_b": (SimpleNamespace(name="指标乙"), SimpleNamespace(dimensions=dimensions)),
    })
    monkeypatch.setattr(service, "visible_catalog", lambda *_args, **_kwargs: catalog)
    cards = [MetricCard(code=code, name=metric.name, aliases=[], dimensions=version.dimensions)
             for code, (metric, version) in catalog.versions.items()]
    for mode in ("ask", "analysis"):
        generated = examples.generated(None, None, mode)
        assert generated
        for item in generated:
            assert f"{start.isoformat()} 至 {end.isoformat()}" in item["text"]
            parsed = DemoIntentModel().parse(item["text"], cards, today=today)
            assert parsed.clarification is None
            assert (parsed.start_date, parsed.end_date) == (start, end)
            if mode == "analysis":
                assert "相比上一期" in item["text"] and parsed.compare == "previous_period"
    assert zones == ["Pacific/Honolulu", "Pacific/Honolulu"]


def test_business_today_respects_timezone_at_month_boundary(monkeypatch):
    instant = datetime(2026, 3, 1, 0, 30, tzinfo=timezone.utc)

    class Clock:
        @staticmethod
        def now(zone):
            return instant.astimezone(zone)

    monkeypatch.setattr(intent, "datetime", Clock)
    assert intent.business_today("Pacific/Honolulu") == date(2026, 2, 28)
    assert intent.business_today("Asia/Tokyo") == date(2026, 3, 1)


@pytest.mark.parametrize("shared", [False, True])
def test_generated_relation_requires_shared_dimension(monkeypatch, shared):
    monkeypatch.setattr(examples, "business_today", lambda _zone: date(2026, 3, 1))
    monkeypatch.setattr(examples, "get_settings", lambda: SimpleNamespace(business_timezone="UTC"))
    catalog = SimpleNamespace(versions={
        "metric_a": (SimpleNamespace(name="指标甲"), SimpleNamespace(dimensions=[
            {"name": "dimension_a", "label": "维度甲"}, {"name": "common", "label": "共有维度"}])),
        "metric_b": (SimpleNamespace(name="指标乙"), SimpleNamespace(dimensions=[
            {"name": "common" if shared else "dimension_b", "label": "维度乙"}])),
    })
    monkeypatch.setattr(service, "visible_catalog", lambda *_args, **_kwargs: catalog)
    relations = [q for q in examples.generated(None, None, "analysis") if "有什么关系" in q["text"]]
    assert len(relations) == int(shared)
    if shared:
        assert "按共有维度" in relations[0]["text"]


@pytest.mark.parametrize("values", [["SKU1", "SKU10", "成员甲", "成员甲乙"], ["成员甲乙", "成员甲", "SKU10", "SKU1"]])
@pytest.mark.parametrize("text,expected", [
    ("SKU10 与成员甲乙", ["SKU10", "成员甲乙"]),
    ("SKU10 与 SKU1，成员甲乙与成员甲", ["SKU1", "SKU10", "成员甲", "成员甲乙"]),
    ("SKU10 SKU10", ["SKU10"]),
    ("无对应成员", []),
])
def test_demo_and_tool_member_values_longest_per_position(values, text, expected):
    card = MetricCard(code="metric_a", name="指标甲", aliases=[], dimensions=[{"name": "dimension_a", "label": "维度甲"}],
                      dimension_values={"dimension_a": values})
    parsed = DemoIntentModel().parse(f"指标甲 2026-01-01 至 2026-01-02 {text}", [card], today=date(2026, 2, 1))
    assert parsed.clarification is None
    assert (parsed.filters[0].values if parsed.filters else []) == expected
    box = ask_agent.Toolbox(None, None, service.Catalog([card], {}))
    box._values[("metric_a", "dimension_a")] = values
    assert json.loads(box.match_dimension_values("metric_a", "dimension_a", text))["matched"] == expected
    assert intent.matched_dimension_values(text, [*values, ""]) == expected


def test_analysis_threshold_defaults_toml_and_environment_precedence(monkeypatch, tmp_path):
    settings = config.build_settings({"AVELIS_SECRET": SECRET})
    assert settings.analysis_uniform_tolerance == settings.analysis_spike_share == 0.5
    path = tmp_path / "thresholds.toml"
    path.write_text("[analysis]\nuniform_tolerance = 0.25\nspike_share = 0.75\n", encoding="utf-8")
    monkeypatch.setattr(config.os, "environ", {"AVELIS_SECRET": SECRET, "AVELIS_CONFIG": str(path),
                                              "AVELIS_ENV_FILE": str(tmp_path / "absent.env")})
    settings = config.build_settings()
    assert settings.analysis_uniform_tolerance == 0.25 and settings.analysis_spike_share == 0.75
    monkeypatch.setenv("AVELIS_ANALYSIS_UNIFORM_TOLERANCE", "1")
    monkeypatch.setenv("AVELIS_ANALYSIS_SPIKE_SHARE", "0.9")
    settings = config.build_settings()
    assert settings.analysis_uniform_tolerance == 1 and settings.analysis_spike_share == 0.9


@pytest.mark.parametrize("key", ["AVELIS_ANALYSIS_UNIFORM_TOLERANCE", "AVELIS_ANALYSIS_SPIKE_SHARE"])
@pytest.mark.parametrize("value", ["0", "-0.1", "1.01", "nan", "inf", "-inf", "invalid"])
def test_analysis_thresholds_reject_out_of_range_and_nonfinite(key, value):
    with pytest.raises(config.ConfigError) as caught:
        config.build_settings({"AVELIS_SECRET": SECRET, key: value})
    assert key in str(caught.value) and "(0, 1]" in str(caught.value)


@pytest.mark.parametrize("key", ["uniform_tolerance", "spike_share"])
def test_analysis_threshold_toml_validation_keeps_source(tmp_path, key):
    path = tmp_path / "thresholds.toml"
    path.write_text(f"[analysis]\n{key} = nan\n", encoding="utf-8")
    values = configfile.read_config({"AVELIS_CONFIG": str(path)})
    values["AVELIS_SECRET"] = SECRET
    with pytest.raises(config.ConfigError) as caught:
        config.build_settings(values)
    assert key in str(caught.value) and "来源：配置文件" in str(caught.value)


def test_investigation_uses_configured_uniform_and_spike_thresholds(monkeypatch):
    data = attribution_dataset(((100, 110), (100, 120), (100, 130)))
    trend = trend_dataset((100, 100, 100, 110, 115, 125))
    settings = SimpleNamespace(analysis_uniform_tolerance=0.5, analysis_spike_share=0.5)
    monkeypatch.setattr(investigation, "get_settings", lambda: settings)
    before = investigation.distribution(data, trend, window=WINDOW)
    assert before["dimensions"]["维度甲"]["uniform"] is True and before["time"]["concentrated"] is True
    settings.analysis_uniform_tolerance, settings.analysis_spike_share = 0.25, 0.6
    after = investigation.distribution(data, trend, window=WINDOW)
    assert after["dimensions"]["维度甲"]["uniform"] is False and after["time"]["concentrated"] is False
    settings.analysis_uniform_tolerance = 0.125
    assert "12.5%" in investigation.distribution_block(before)


@pytest.mark.parametrize("dialect", [None, "", "unknown-dialect"])
def test_investigation_skips_unknown_dialects(monkeypatch, dialect):
    records = event_dataset(dialect=dialect)
    calls = []
    original = investigation.sqlglot.parse

    def parse(sql, **kwargs):
        calls.append(kwargs["read"])
        return original(sql, **kwargs)

    monkeypatch.setattr(investigation.sqlglot, "parse", parse)
    assert investigation.event_datasets({"D7": records}, CLOSURE) == []
    assert investigation.needs_revision(report(), {"D7": records}, CLOSURE, uniform_distribution()) is None
    assert all(call == "unknown-dialect" for call in calls)
    assert "starrocks" not in calls


@pytest.mark.parametrize("kind", ["explore", "context", "injected"])
@pytest.mark.parametrize("value", ["2026-09-02", date(2026, 9, 2), datetime(2026, 9, 2, 12, 30)])
def test_investigation_known_dialect_preserves_record_detection(kind, value):
    records = event_dataset(kind=kind, value=value, dialect="postgres")
    assert investigation.event_datasets({"D7": records}, CLOSURE) == ["D7"]
    records.rows = []
    records.meta["units"] = {"record_date": {"kind": "date"}}
    assert investigation.event_datasets({"D7": records}, CLOSURE) == ["D7"]


@pytest.mark.parametrize("cause_type", ["member", "factor"])
def test_investigation_known_dialect_preserves_revision_and_used_evidence(cause_type):
    records = event_dataset(dialect="postgres")
    result = report(cause_type=cause_type)
    feedback = investigation.needs_revision(result, {"D7": records}, CLOSURE, uniform_distribution())
    assert feedback and "D7" in feedback and "unexplained" in feedback
    result["claims"] = [{"parts": [{"dataset": "D8", "column": "value_a"}]}]
    derived = dataset(["value_a"], [[1]], meta={"closure": ["D7"]})
    assert investigation.needs_revision(result, {"D7": records, "D8": derived}, CLOSURE, uniform_distribution()) is None
    result["causes"][0]["entity"] = {"evidence_id": "D7#1"}
    assert investigation.needs_revision(result, {"D7": records}, CLOSURE, uniform_distribution()) is None


@pytest.mark.parametrize("change,direction", [(60, "up"), (-60, "down")])
def test_investigation_known_dialect_preserves_offsetting_direction(change, direction):
    result = report(cause_type="calendar", direction="down" if direction == "up" else "up")
    result["conclusion"] = "offsetting"
    result["causes"].append(report(direction=direction)["causes"][0])
    records = {"D7": event_dataset(dialect="postgres")}
    assert investigation.needs_revision(result, records, CLOSURE, uniform_distribution(change=change))
    result["causes"][1]["type"] = "governance"
    assert investigation.needs_revision(result, records, CLOSURE, uniform_distribution(change=change)) is None


@pytest.mark.parametrize("metadata", [{}, {"engine": "unknown-engine"}, {"datasource_id": "source_a"}])
def test_analysis_feedback_never_guesses_dialect(monkeypatch, metadata):
    box = _batch1_box(monkeypatch)
    _batch1_governed(box, [["成员甲", 30, 10]])
    box.precheck_distribution = uniform_distribution()
    box.report = report()
    records = box._dataset("explore", "记录占位", ["record_date", "value_a"], [["2026-01-02", "成员甲"]],
                           meta={"sql": "SELECT * FROM cat.db.records_c", **metadata})
    monkeypatch.setattr(agent.service, "version_sources", lambda *_args: {
        "base": SimpleNamespace(catalog_name="cat", db_name="db", name="base_a")})
    calls = []
    monkeypatch.setattr(investigation.sqlglot, "parse", lambda *args, **kwargs: calls.append(kwargs) or [])
    assert agent._investigation_feedback(box) is None
    assert not calls and "dialect" not in records.meta


def test_missing_secret_error_is_product_neutral():
    with pytest.raises(config.ConfigError) as caught:
        config.build_settings({})
    message = str(caught.value)
    assert "AVELIS_SECRET" in message and "[security] secret" in message
    assert "scripts/demo.py" not in message
