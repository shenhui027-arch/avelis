"""防过拟合增量的请求语言回归：业务名称与稳定列键保持原文。"""

import json
from datetime import date
from types import SimpleNamespace

import pytest

from avelis.analysis import calc, export
from avelis.analysis.dataset_projection import dataset_meta
from avelis.ask import compiler, examples, service
from avelis.ask.intent import Intent
from avelis.governance.errors import Invalid
from avelis.i18n import Msg, locale_context, translate
from avelis.models import Metric, MetricVersion


@pytest.mark.parametrize("locale", ["zh-TW", "en"])
def test_compiler_grain_error_preserves_template_and_dynamic_grain(locale):
    with pytest.raises(Invalid) as caught:
        compiler.compile_metric_query(
            version=SimpleNamespace(dimensions=[], data_grain="business_grain_a"),
            aliases={},
            steps=[],
            intent=SimpleNamespace(time_grain=None),
            max_rows=10,
            time_type="date",
            dialect="postgres",
        )
    message = caught.value.message
    assert isinstance(message, Msg)
    assert message.template == "未知数据粒度 {data_grain}"
    with locale_context(locale):
        rendered = translate(message)
    assert "business_grain_a" in rendered and rendered != str(message)


@pytest.mark.parametrize("locale", ["zh-TW", "en"])
def test_fulfil_renders_governance_error_before_saving_answer(monkeypatch, locale):
    def reject(*_):
        raise Invalid(Msg("未知时间编码格式 {time_format}", time_format="business_format_a"))

    monkeypatch.setattr(service, "_fulfil", reject)
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace())
    answer = SimpleNamespace()
    catalog = SimpleNamespace(versions={"metric_a": (None, SimpleNamespace(kind="base"))})
    with locale_context(locale):
        service.fulfil(None, None, Intent(metric_code="metric_a"), answer, catalog)
    assert answer.status == "error" and answer.error_code == "governance"
    assert isinstance(answer.message, str) and not isinstance(answer.message, Msg)
    assert "business_format_a" in answer.message and "未知时间编码格式" not in answer.message


@pytest.mark.parametrize("locale", ["zh-TW", "en"])
@pytest.mark.parametrize(
    "case,source",
    [
        ("unknown_grain", "时间粒度未知，无法判断期间覆盖度"),
        ("reversed_window", "覆盖度的开始日期不能晚于结束日期"),
        ("mismatched_period", "期间列与已声明的时间粒度不一致，无法判断覆盖度"),
    ],
)
def test_coverage_diagnostics_translate_before_saving_tool_steps(monkeypatch, locale, case, source):
    from tests.unit.test_analysis_toolbox import make_box

    box = make_box(monkeypatch=monkeypatch)

    def reject():
        if case == "unknown_grain":
            return calc.period_labels(date(2026, 1, 1), date(2026, 1, 31), "hour")
        if case == "reversed_window":
            return calc.period_labels(date(2026, 2, 1), date(2026, 1, 31), "day")
        return calc.coverage(
            [{"period": "2026-01-15"}], [], "period", date(2026, 1, 1), date(2026, 1, 31), grain="month"
        )

    with locale_context(locale):
        output = json.loads(box._guard("coverage", {}, reject))
    step = box.session.added[-1]
    assert step.status == "error" and step.output["error"] == output["error"]
    assert output["error"] != source


@pytest.mark.parametrize("locale", ["zh-TW", "en"])
def test_coverage_missing_version_keeps_business_code_in_saved_translation(monkeypatch, locale):
    from tests.unit.test_analysis_toolbox import make_box

    box = make_box(monkeypatch=monkeypatch)
    dataset = box._dataset(
        "governed",
        "业务指标甲",
        ["period", "value"],
        [["2026-01-01", 10]],
        meta={"intent": {"metric_codes": ["业务指标甲"], "time_grain": "day"}, "versions": {}},
    )
    with locale_context(locale):
        output = json.loads(box.coverage(dataset=f"D{dataset.seq}"))
    assert "业务指标甲" in output["error"]
    assert output["error"] != "指标 业务指标甲 缺少锁定的查询版本，无法确认数据粒度"
    assert box.session.added[-1].output["error"] == output["error"]


@pytest.mark.parametrize("locale", ["zh-TW", "en"])
def test_dynamic_governance_error_translates_at_tool_exit(monkeypatch, locale):
    from tests.unit.test_analysis_toolbox import make_box

    box = make_box(monkeypatch=monkeypatch)

    def reject():
        raise Invalid(Msg("未知时间编码格式 {time_format}", time_format="业务编码格式甲"))

    with locale_context(locale):
        output = json.loads(box._guard("query_metric", {}, reject))
    assert "业务编码格式甲" in output["error"]
    assert output["error"] != "未知时间编码格式 业务编码格式甲"
    assert box.session.added[-1].output["error"] == output["error"]


@pytest.mark.parametrize("locale", ["zh-TW", "en"])
def test_ratio_rejection_translates_fixed_reason_and_preserves_column(monkeypatch, locale):
    from tests.unit.test_overfit_batch1 import _batch1_box, _batch1_governed

    box = _batch1_box(monkeypatch)
    _batch1_governed(box)
    monkeypatch.setattr(box, "_metric", lambda code: box.catalog.versions[code])
    box.catalog.versions["metric_a"][1].value_kind = "ratio"
    with locale_context(locale):
        output = json.loads(box._guard("contribution", {}, lambda: box._require_additive("D1", ["指标甲"])))
    assert "指标甲" in output["error"]
    assert "口径声明为比率量纲" not in output["error"]
    assert box.session.added[-1].output["error"] == output["error"]


def test_generated_examples_translate_template_and_keep_business_labels(monkeypatch):
    monkeypatch.setattr(examples, "business_today", lambda _: date(2026, 3, 1))
    monkeypatch.setattr(examples, "get_settings", lambda: SimpleNamespace(business_timezone="UTC"))
    version = SimpleNamespace(dimensions=[{"name": "维度甲", "label": "业务地区甲"}])
    catalog = SimpleNamespace(versions={"metric_a": (SimpleNamespace(name="业务指标甲"), version)})
    monkeypatch.setattr(service, "visible_catalog", lambda *_, **__: catalog)
    with locale_context("en"):
        question = examples.generated(None, None, "analysis")[0]["text"]
    assert "业务指标甲" in question and "业务地区甲" in question
    assert "2026-02-01" in question and "2026-02-28" in question
    assert "why" in question.lower() and "相比上一期为什么变化" not in question


def test_answer_cache_keeps_request_language_separate(monkeypatch):
    from tests.unit.test_overfit_batch2 import svc_state

    state = svc_state(monkeypatch, "sum", rows=[["成员甲", 8]])
    with locale_context("zh-CN"):
        original = state.run()
    with locale_context("en"):
        english = state.run()
    with locale_context("zh-CN"):
        repeated = state.run()
    assert original.status == english.status == repeated.status == "answered"
    assert not original.cache_hit and not english.cache_hit and repeated.cache_hit
    assert len(state.calls) == 2
    assert repeated.evidence["query_warnings"] == original.evidence["query_warnings"]


@pytest.mark.parametrize("locale", ["zh-TW", "en"])
def test_zero_business_days_note_follows_request_language(locale):
    with locale_context(locale):
        output = calc.calendar_adjust(
            10, 10, (date(2026, 1, 3), date(2026, 1, 4)), (date(2025, 12, 27), date(2025, 12, 28)), holidays=set()
        )
    assert "0" in output["business_days_note"]
    assert "工作日数为 0" not in output["business_days_note"]
    assert "上期工作日均值不可得" not in output["business_days_note"]


def test_dataset_labels_translate_system_suffixes_without_changing_saved_columns():
    metric = SimpleNamespace(name="业务指标甲")
    version = SimpleNamespace(
        metric_id="metric_a", dimensions=[], kind="base", unit="元", unit_scale=1, value_kind="level", polarity=None
    )
    store = {(MetricVersion, "version_a"): version, (Metric, "metric_a"): metric}
    columns = ["业务指标甲", "业务指标甲·上一自然期", "业务指标甲·变化", "业务指标甲·变化率"]
    saved = {column: "业务指标甲·本期" if column == metric.name else column for column in columns}
    dataset = SimpleNamespace(
        kind="governed",
        columns=columns,
        meta={
            "versions": {"metric_a": "version_a"},
            "intent": {"compare": "previous_calendar"},
            "column_labels": saved,
        },
    )
    with locale_context("en"):
        meta = dataset_meta(SimpleNamespace(get=lambda model, key: store.get((model, key))), dataset)
    assert dataset.columns == columns and dataset.meta["column_labels"] == saved
    assert meta["column_labels"][metric.name].startswith("业务指标甲")
    assert meta["column_labels"][metric.name] != "业务指标甲·本期"
    assert all("业务指标甲" in label for label in meta["column_labels"].values())
    assert meta["column_labels"][columns[1]] != columns[1]
    with locale_context("en"):
        assert export._column_label({"kind": "governed", "meta": meta}, columns[1]) == meta["column_labels"][columns[1]]


def test_export_api_passes_language_projection_to_metadata_renderer(monkeypatch):
    from avelis.api import analysis as api

    metric = SimpleNamespace(name="业务指标甲")
    version = SimpleNamespace(
        metric_id="metric_a", dimensions=[], kind="base", unit="元", unit_scale=1, value_kind="level", polarity=None
    )
    store = {(MetricVersion, "version_a"): version, (Metric, "metric_a"): metric}
    dataset = SimpleNamespace(
        seq=1,
        kind="governed",
        columns=[metric.name],
        rows=[[10]],
        meta={
            "versions": {"metric_a": "version_a"},
            "intent": {"compare": "previous_calendar"},
            "units": {metric.name: {"kind": "level"}},
            "column_labels": {metric.name: "业务指标甲·本期"},
        },
    )
    session = SimpleNamespace(get=lambda model, key: store.get((model, key)), scalars=lambda _: [dataset])
    monkeypatch.setattr(api.analysis, "visible_run", lambda *_: SimpleNamespace(id="run_a", status="succeeded"))
    monkeypatch.setattr(api.analysis, "run_view", lambda *_a, **_k: {"datasets": [{"ref": "D1"}]})
    rendered = []

    def export_metadata(_session, _user, meta):
        rendered.append(meta)
        return meta

    monkeypatch.setattr(api, "_export_metadata", export_metadata)
    monkeypatch.setattr(
        api.export, "to_markdown", lambda view, **_: export._column_label(view["datasets"][0], metric.name)
    )
    with locale_context("en"):
        response = api.export_run("run_a", format="md", audience="brief", user=None, session=session)
    assert len(rendered) == 1
    assert rendered[0]["column_labels"][metric.name] != "业务指标甲·本期"
    assert response.body.decode("utf-8") == rendered[0]["column_labels"][metric.name]
    assert "业务指标甲" in response.body.decode("utf-8")
    assert dataset.meta["column_labels"][metric.name] == "业务指标甲·本期"
