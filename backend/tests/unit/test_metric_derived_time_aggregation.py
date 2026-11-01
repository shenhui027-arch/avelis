"""派生口径的跨时间聚合必须与执行计划支持的语义一致。"""

from types import SimpleNamespace

import pytest

from avelis.governance import metrics
from avelis.governance.errors import Invalid
from avelis.models import MetricVersion


@pytest.mark.parametrize("method", ["last", "avg"])
@pytest.mark.parametrize("check", ["validate_spec", "publish_check"])
def test_derived_time_aggregation_is_rejected_before_publication(method, check):
    # 即使 A、B 都是 day/sum，C=A-B 也不能把窗口公式结果冒充逐日计算后的 last/avg。
    version = MetricVersion(kind="derived", expression="metric_a - metric_b", data_grain="day", time_aggregation=method)
    with pytest.raises(Invalid, match=f"派生指标不支持 time_aggregation={method}.*逐桶公式结果"):
        if check == "publish_check":
            metrics.publish_check(None, version, None)
        else:
            metrics.validate_spec(None, version)


@pytest.mark.parametrize("method", [None, "sum"])
def test_derived_window_formula_keeps_valid_default_and_explicit_sum(monkeypatch, method):
    dimensions = [{"name": "region", "label": "区域"}]
    components = [
        {
            "metric": SimpleNamespace(code=code, id=code, name=code),
            "version": MetricVersion(
                id=f"{code}-v1",
                version_no=1,
                kind="base",
                status="published",
                expression="SUM(b.value)",
                dimensions=dimensions,
                data_grain="day",
                time_aggregation="sum",
                dependency_fingerprint=code,
            ),
        }
        for code in ("metric_a", "metric_b")
    ]
    monkeypatch.setattr(metrics, "derived_components", lambda *args: components)
    version = MetricVersion(kind="derived", expression="metric_a - metric_b", data_grain="day", time_aggregation=method)
    resolved = metrics.validate_spec(None, version)
    assert resolved["components"] == components
    check = metrics.publish_check(None, version, None)
    assert check["ok"] and check["blockers"] == []
    assert [item["metric_code"] for item in check["components"]] == ["metric_a", "metric_b"]
    assert check["dimensions"] == dimensions
