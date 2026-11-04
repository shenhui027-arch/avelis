"""执行计划按版本身份复用，派生公式与治理检查都遵守各自锁定的版本。"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from avelis.ask import service
from avelis.ask.intent import Intent
from avelis.models import Metric, MetricVersion


@pytest.fixture
def locked_plan(monkeypatch):
    base = Metric(id="base", code="base", name="基础", domain_id="domain")
    v1 = MetricVersion(id="v1", metric_id=base.id, version_no=1, kind="base", status="published", dependency=[])
    v2 = MetricVersion(id="v2", metric_id=base.id, version_no=2, kind="base", status="published", dependency=[])
    store = {base.id: base, v1.id: v1, v2.id: v2}
    versions = {base.code: (base, v2)}
    for code, pinned, expression in [("old", v1, "base * 2"), ("new", v2, "base + 3"), ("shared", v2, "base * 4")]:
        metric = Metric(id=code, code=code, name=code, domain_id="domain")
        version = MetricVersion(
            id=f"{code}-v1",
            metric_id=code,
            version_no=1,
            kind="derived",
            status="published",
            expression=expression,
            components=[
                {
                    "metric_id": base.id,
                    "metric_code": base.code,
                    "metric_name": base.name,
                    "version_id": pinned.id,
                    "version_no": pinned.version_no,
                    "dependency_fingerprint": "fp",
                }
            ],
        )
        versions[code] = (metric, version)
        store.update({metric.id: metric, version.id: version})
    # 漂移函数仍使用真实的锁定组成状态检查；基础版本无资产依赖，不需要外部数据库。
    session = SimpleNamespace(get=lambda cls, id_: store.get(id_), scalars=lambda stmt: [])
    monkeypatch.setattr(service.access, "domain_ids", lambda *args: {"domain"})
    monkeypatch.setattr(service, "_version_evidence", lambda s, m, v: {"id": v.id})
    monkeypatch.setattr(service, "_dimension_docs", lambda *args: {})
    monkeypatch.setattr(service, "validate_spec", lambda *args: {"aliases": {}})
    monkeypatch.setattr(service, "_blocked_message", lambda s, m, v, u: f"{v.id} blocked")
    calls = []

    def execute(session, user, intent, answer, versions, settings, **kwargs):
        metric, version = versions[intent.metric_code]
        if not service._check_version(session, user, metric, version, answer):
            return
        calls.append((metric.id, version.id, intent.start_date))
        answer.status = "answered" if kwargs.get("execute", True) else "compiled"
        answer.result = {"rows": [[10 if version.id == "v1" else 20]]}

    monkeypatch.setattr(service, "_fulfil", execute)

    def run(codes, **kwargs):
        intent = Intent(metric_codes=codes, start_date=date(2026, 1, 2), end_date=date(2026, 1, 2), **kwargs)
        answer = service._scratch(
            SimpleNamespace(question="版本合查", model_adapter="offline"), SimpleNamespace(id="u")
        )
        service.fulfil(session, SimpleNamespace(id="u"), intent, answer, service.Catalog([], versions))
        return answer

    return SimpleNamespace(run=run, calls=calls, v1=v1, versions=versions)


@pytest.mark.parametrize("codes", [["old", "new"], ["new", "old"], ["base", "old", "new"], ["new", "old", "base"]])
@pytest.mark.parametrize("compare", [None, "previous_period"])
def test_each_derived_formula_reads_its_pinned_version_in_any_order(locked_plan, codes, compare):
    state = locked_plan
    answer = state.run(codes, compare=compare)
    assert answer.status == "answered"
    values = dict(zip(answer.result["columns"], answer.result["rows"][0]))
    assert Decimal(values["old"]) == 20 and Decimal(values["new"]) == 23
    if "base" in codes:
        assert values["基础"] == "20"
    expected_dates = [date(2026, 1, 2)] + ([date(2026, 1, 1)] if compare else [])
    assert sorted(state.calls) == sorted(("base", v, day) for v in ["v1", "v2"] for day in expected_dates)
    assert set(answer.metric_version_ids) == {state.versions[c][1].id for c in codes} | {"v1", "v2"}
    assert len(answer.metric_version_ids) == len(set(answer.metric_version_ids))
    assert {q["metric_version_id"] for q in answer.plan["subqueries"]} == {"v1", "v2"}


def test_shared_base_version_executes_once_per_period(locked_plan):
    answer = locked_plan.run(["base", "new", "shared"], compare="previous_period")
    assert answer.status == "answered"
    assert len(locked_plan.calls) == 2 and all(v == "v2" for _, v, _ in locked_plan.calls)
    assert list(map(Decimal, answer.result["rows"][0][::4])) == [20, 23, 80]
    assert set(answer.metric_version_ids) == {"v2", "new-v1", "shared-v1"}


@pytest.mark.parametrize("codes", [["old"], ["old", "new"], ["new", "old"], ["base", "old"], ["old", "base"]])
def test_retired_locked_dependency_blocks_single_and_combined_before_execution(locked_plan, codes):
    locked_plan.v1.status = "retired"
    answer = locked_plan.run(codes)
    assert (answer.status, answer.error_code) == ("blocked", "dependency_drift")
    assert answer.evidence["drift"][0]["current_status"] == "retired"
    assert locked_plan.calls == []
    assert "v2" not in answer.metric_version_ids  # 尚未执行的组成版本不会伪装为已用依据。


def test_each_derived_version_checks_its_own_drift_even_when_base_is_healthy(locked_plan, monkeypatch):
    original = service.dependency_drift
    monkeypatch.setattr(
        service,
        "dependency_drift",
        lambda s, v: [{"qualified": "锁定依赖", "column": "base"}] if v.id == "old-v1" else original(s, v),
    )
    answer = locked_plan.run(["new", "old"])
    assert (answer.status, answer.error_code) == ("blocked", "dependency_drift")
    assert locked_plan.calls == []
