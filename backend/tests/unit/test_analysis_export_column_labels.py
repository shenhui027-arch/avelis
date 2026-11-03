"""指标归属与完整列标签分开，比较和计算结果的导出表头保留列语义。"""

import json
from copy import deepcopy

import pytest

from avelis.analysis import dataset_projection, export, tools
from avelis.ask import service as ask_service
from tests.unit.test_analysis_toolbox import QUERY, governed_catalog, make_box
from tests.unit.test_overfit_batch3 import _dataset, _Session


@pytest.mark.parametrize("compare", ["previous_period", "previous_calendar", "previous_year", "custom"])
def test_governed_query_saves_metric_names_separately_from_column_labels(monkeypatch, compare):
    catalog, _ = governed_catalog(monkeypatch)
    box = make_box(catalog=catalog, monkeypatch=monkeypatch)
    period = ask_service.PERIOD_LABEL[compare]
    columns = ["指标甲", f"指标甲·{period}", "指标甲·变化", "指标甲·变化率"]

    def fulfil(session, user, intent, answer, scoped):
        answer.status = "answered"
        answer.result = {"columns": columns, "rows": [[20, 10, 10, 1]]}
        answer.sql, answer.metric_version_ids, answer.freshness = "SELECT 1", ["v1"], None

    monkeypatch.setattr(tools.service, "fulfil", fulfil)
    result = json.loads(
        box.query_metric(
            **{**QUERY, "group_by": [], "compare": compare, "compare_start": "2026-08-01", "compare_end": "2026-08-31"}
        )
    )
    assert result["dataset"] == "D1"
    meta = box.datasets()["D1"].meta
    assert meta["measure_labels"] == dict.fromkeys(columns, "指标甲")
    assert meta["column_labels"] == {column: "指标甲·本期" if column == "指标甲" else column for column in columns}


@pytest.mark.parametrize("saved_labels", [False, True])
@pytest.mark.parametrize("format", ["markdown", "html"])
def test_governed_comparison_headers_keep_period_and_change_semantics(saved_labels, format):
    columns = ["指标甲", "指标甲·上一自然期", "指标甲·变化", "指标甲·变化率"]
    meta = {"versions": {"metric_a": "version_a"}, "intent": {"compare": "previous_calendar"}}
    if saved_labels:
        meta["measure_labels"] = dict.fromkeys(columns, "指标甲")  # 旧运行曾把四列都登记成裸指标名。
    dataset = _dataset(columns=columns, meta=meta)
    original = deepcopy(meta)
    projected = dataset_projection.dataset_meta(_Session(), dataset)
    headers = ["指标甲·本期", "指标甲·上一自然期", "指标甲·变化", "指标甲·变化率"]
    assert projected["measure_labels"] == dict.fromkeys(columns, "指标甲")
    assert [projected["column_labels"][column] for column in columns] == headers
    assert dataset.meta == original
    assert export._dataset_title({"kind": "governed", "columns": columns, "meta": projected}) == "指标甲"
    _assert_headers(format, "governed", columns, projected, headers)


@pytest.mark.parametrize("format", ["markdown", "html"])
def test_calculation_headers_translate_generic_columns_with_column_roles(format):
    columns = ["previous", "current", "change", "change_rate"]
    meta = {
        "units": {
            "previous": {"kind": "level"},
            "current": {"kind": "level"},
            "change": {"kind": "delta"},
            "change_rate": {"kind": "rate_change"},
        }
    }
    _assert_headers(format, "calc", columns, meta, ["对比期", "本期", "变化", "变化率"])


def test_explicit_column_labels_win_over_metric_names_and_default_calculation_labels():
    meta = {
        "units": {"current": {"kind": "level"}},
        "measure_labels": {"current": "指标甲"},
        "column_labels": {"current": "指标甲·当月"},
    }
    roles = dataset_projection.build_column_roles(["current"], meta)
    assert roles["current"] == {"role": "measure", "label": "指标甲·当月", "additive": True}
    assert export._column_label({"kind": "calc", "meta": meta, "column_roles": roles}, "current") == "指标甲·当月"
    meta["column_labels"]["current"] = "current"
    assert export._column_label({"kind": "calc", "meta": meta, "column_roles": roles}, "current") == "current"


def _assert_headers(format, kind, columns, meta, headers):
    dataset = {
        "ref": "D1",
        "kind": kind,
        "title": "比较结果",
        "columns": columns,
        "rows": [[20, 10, 10, 1]],  # 无维度的比较结果采用单行 KPI 规格。
        "meta": meta,
        "column_roles": dataset_projection.build_column_roles(columns, meta),
    }
    view = {"report": {"title": "分析报告"}, "datasets": [dataset]}
    render = export.to_markdown if format == "markdown" else export.to_html
    text = render(view, audience="brief", datasets=["D1"])
    if format == "markdown":
        assert "| " + " | ".join(headers) + " |" in text
    else:
        assert all(f'<th scope="col">{header}</th>' in text for header in headers)
