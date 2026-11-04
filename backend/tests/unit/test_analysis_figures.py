"""报告插图工具、完整行投影与规格导出；仅用会话替身，不连接数据库。"""

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from reportlab.graphics.shapes import Rect, String
from reportlab.lib.colors import HexColor

from avelis.analysis import dataset_access, export, service
from avelis.analysis.figures import dataset_charts, report_figures
from avelis.api.schemas import ReportFigure
from avelis.models import AnalysisDataset
from tests.unit.test_analysis_toolbox import make_box


def dataset(ref="D1", *, rows=None, time=False):
    return {
        "ref": ref,
        "kind": "calc",
        "title": "销售额",
        "columns": ["category", "value"],
        "rows": rows if rows is not None else [["甲", 100], ["乙", 20]],
        "row_count": 2,
        "inputs": [],
        "meta": {
            "units": {"value": {"kind": "level", "unit": "元", "scale": 10000, "additive": True}},
            **({"time_column": "category"} if time else {}),
        },
    }


def view(data=None, figures=None):
    report = {"title": "分析", "summary": "结果", "claims": [{"type": "fact", "text": "结论", "refs": ["D1"]}]}
    if figures is not None:
        report["figures"] = figures
    return {"report": report, "datasets": [data or dataset()]}


def choice(ref="D1", kind="bar", title="甲贡献最多", top_n=None):
    return {"dataset": ref, "kind": kind, "title": title, "top_n": top_n}


def test_chart_tool_records_overwrites_and_submits_figures(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    monkeypatch.setattr(dataset_access, "can_read_dataset", lambda *a: True)
    data = dataset()
    box._dataset("calc", data["title"], data["columns"], data["rows"], meta=data["meta"])
    assert json.loads(box.dispatch("chart", choice())) == {"ok": True}
    assert box.figures == [choice()]
    assert box.budget.used_queries == 0
    assert box.session.added[-1].tool == "chart"
    assert box.session.added[-1].status == "ok"
    assert json.loads(box.chart("D1", "donut", "甲占比最高", 2)) == {"ok": True}
    assert box.figures == [choice(kind="donut", title="甲占比最高", top_n=2)]
    assert json.loads(box.submit_report("报告", "结论", [])) == {"ok": True}
    assert box.report["figures"] == box.figures
    box.figures.clear()
    assert box.report["figures"]  # 保存的是快照。


def test_chart_tool_errors_and_four_figure_limit(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    monkeypatch.setattr(dataset_access, "can_read_dataset", lambda *a: True)
    for _ in range(5):
        data = dataset()
        box._dataset("calc", data["title"], data["columns"], data["rows"], meta=data["meta"])
    error = json.loads(box.chart("D1", "line", "趋势"))["error"]
    assert "可选 kind" in error and "bar" in error and "donut" in error
    assert box.figures == []
    assert "不存在" in json.loads(box.chart("D9", "bar", "排名"))["error"]
    for index in range(1, 5):
        assert json.loads(box.chart(f"D{index}", "bar", "排名"))["ok"]
    assert "最多 4" in json.loads(box.chart("D5", "bar", "排名"))["error"]
    assert json.loads(box.chart("D1", "donut", "构成"))["ok"]
    assert len(box.figures) == 4
    assert json.loads(box.dispatch("chart", choice(title="长" * 41))).get("error")
    assert json.loads(box.dispatch("chart", choice(top_n=21))).get("error")


def test_chart_tool_denies_unreadable_dataset_and_empty_report_has_no_figures(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    data = dataset()
    box._dataset("calc", data["title"], data["columns"], data["rows"], meta=data["meta"])
    monkeypatch.setattr(dataset_access, "can_read_dataset", lambda *a: False)
    assert json.loads(box.chart("D1", "bar", "排名"))["status"] == "denied"
    assert box.figures == []
    box.submit_report("报告", "结论", [])
    assert box.report["figures"] == []


def test_figures_resolve_preference_and_skip_restricted_or_unavailable():
    data = dataset(rows=[[f"成员{i}", i] for i in range(25)])
    source = view(data, [choice(kind="donut", title="构成")])
    assert report_figures(source) == []  # 此数据没有 donut 候选，不能静默换成默认图。
    source["report"]["figures"] = [choice(top_n=2)]
    original = deepcopy(source)
    figure = report_figures(source)[0]
    assert ReportFigure.model_validate(figure).chart.kind == "bar"
    assert figure["chart"]["title"] == figure["title"] == "甲贡献最多"
    assert len(figure["chart"]["categories"]) == 3 and figure["chart"]["others"]["count"] == 23
    assert source == original
    data["restricted"] = True
    assert report_figures(source) == []
    assert "甲贡献最多" not in export.to_html(source)
    del data["restricted"]
    data["meta"]["units"] = {}
    assert report_figures(source) == []


@pytest.mark.parametrize("saved", [None, []])
def test_legacy_figures_share_key_dataset_selection(saved):
    source = view(figures=saved)
    source["datasets"].extend([dataset("D2"), dataset("D3")])
    source["report"]["claims"].extend([{"refs": ["D2"]}, {"refs": ["D2"]}, {"refs": ["D3"]}])
    figures = report_figures(source)
    assert [f["ref"] for f in figures] == ["D2", "D1"]
    assert figures[0]["title"] == "销售额"
    source["datasets"][1]["restricted"] = True
    assert [f["ref"] for f in report_figures(source)] == ["D1", "D3"]


@pytest.mark.parametrize("saved", [True, False])
@pytest.mark.parametrize("restricted", [True, False])
def test_run_view_resolves_figures_from_all_rows(monkeypatch, saved, restricted):
    data = dataset(rows=[[f"2026-{i:03}", i] for i in range(80)], time=True)
    now = datetime.now(timezone.utc)
    stored = SimpleNamespace(
        id="d1",
        seq=1,
        kind="calc",
        title=data["title"],
        columns=data["columns"],
        rows=data["rows"],
        meta=data["meta"],
        answer_id=None,
        inputs=[],
        truncated=False,
    )
    run = SimpleNamespace(
        id="r1",
        conversation_id=None,
        task_id=None,
        parent_run_id=None,
        confirmation_mode="auto",
        question="趋势",
        trigger="manual",
        status="succeeded",
        window={},
        budget={},
        model_adapter=None,
        reasoning_effort=None,
        model_choice=None,
        error=None,
        created_at=now,
        started_at=None,
        finished_at=None,
        checkpoint={},
        report=view(figures=[choice(kind="line")] if saved else None)["report"],
        verification={},
        plan={},
    )

    extra = SimpleNamespace(**{**vars(stored), "id": "d2", "seq": 2})
    if saved and restricted:
        run.report["claims"].append({"type": "fact", "text": "其他数据", "refs": ["D2"]})

    class Session:
        def scalars(self, statement):
            entity = statement.column_descriptions[0]["entity"]
            return ([stored, extra] if saved and restricted else [stored]) if entity is AnalysisDataset else []

    monkeypatch.setattr(
        dataset_access, "can_read_dataset", lambda session, user, ds, known=None: not restricted or ds.id == "d2"
    )
    projected = service.run_view(Session(), run, user=SimpleNamespace(id="u"))
    assert len(projected["datasets"][0]["rows"]) == (0 if restricted else 50)
    if restricted:
        assert projected["figures"] == []
        assert "甲贡献最多" not in json.dumps(projected["report"], ensure_ascii=False)
        assert report_figures(projected) == []
    else:
        figure = projected["figures"][0]
        assert len(figure["chart"]["categories"]) == 80
        assert figure["title"] == ("甲贡献最多" if saved else "销售额")
        ReportFigure.model_validate(figure)


def all_specs():
    specs = {s["kind"]: s for s in dataset_charts(dataset())}
    for data in [
        dataset(time=True),
        {
            **dataset(),
            "columns": ["t", "d", "value"],
            "rows": [["01", "a", 2], ["01", "b", 3], ["02", "a", 4], ["02", "b", 5]],
            "meta": {"time_column": "t", "units": {"value": {"kind": "level", "additive": True}}},
        },
        {
            **dataset(),
            "columns": ["category", "a", "b"],
            "rows": [["甲", 2, 0.1], ["乙", 3, 0.2]],
            "meta": {"units": {"a": {"kind": "count"}, "b": {"kind": "ratio"}}},
        },
        {**dataset(), "columns": ["value"], "rows": [[3]]},
        {
            **dataset(),
            "columns": ["category", "a", "a·上期", "a·变化", "a·变化率"],
            "rows": [["甲", 10, 20, -10, -0.5], ["乙", 20, 10, 10, 1]],
            "meta": {
                "units": {
                    "a": {"kind": "level"},
                    "a·上期": {"kind": "level"},
                    "a·变化": {"kind": "delta"},
                    "a·变化率": {"kind": "rate_change"},
                }
            },
        },
    ]:
        specs.update({s["kind"]: s for s in dataset_charts(data)})
    return specs


@pytest.mark.parametrize(
    "kind", ["line", "bar", "grouped_bar", "stacked_bar", "diverging_bar", "donut", "multiples", "kpi"]
)
@pytest.mark.parametrize("audience", ["brief", "full"])
def test_pdf_html_render_each_materialized_kind(monkeypatch, kind, audience):
    spec = all_specs()[kind]
    monkeypatch.setattr(export, "report_figures", lambda *a: [{"ref": "D1", "title": "结论标题", "chart": spec}])
    assert export.to_pdf(view(), audience=audience).startswith(b"%PDF")
    markup = export.to_html(view(), audience=audience)
    assert "<svg" in markup


@pytest.mark.parametrize(
    "kind", ["line", "bar", "donut", "grouped_bar", "stacked_bar", "diverging_bar", "multiples", "kpi"]
)
def test_markdown_line_bar_and_multiples_use_raw_values(kind):
    spec = all_specs()[kind]
    spec = {**spec, "series": spec["series"][:1]}  # 多系列没有图例可表达，另有用例覆盖回退
    lines = export._mermaid_chart({"chart": spec, "title": "结论"})
    if kind in ("line", "bar", "multiples"):
        assert "xychart-beta" in lines
        title = spec["series"][0]["name"] if kind == "multiples" else "结论"
        assert f'    title "{title}"' in lines
        assert str(spec["series"][0]["values"][0]) in "\n".join(lines)
    else:
        assert lines == []


def test_markdown_export_includes_mermaid_and_table():
    markup = export.to_markdown(view(figures=[choice()]))
    assert "```mermaid\nxychart-beta" in markup and 'title "甲贡献最多"' in markup
    assert "100.0" in markup and "1000000" not in markup
    assert "|" in markup


def test_diverging_polarity_and_units():
    spec = all_specs()["diverging_bar"]
    font, _ = export.pdf_font()
    for polarity, expected in [
        (None, [export.VIZ_NEUTRAL] * 2),
        ("up_good", [export.POSITIVE, export.NEGATIVE]),
        ("down_good", [export.NEGATIVE, export.POSITIVE]),
    ]:
        spec["series"][0]["polarity"] = polarity
        drawing = export._spec_chart(spec, font)
        assert [s.fillColor for s in drawing.contents if isinstance(s, Rect)] == [HexColor(c) for c in expected]
    scaled = dataset_charts(dataset())[0]
    drawing = export._spec_chart(scaled, font)
    assert any("万元" in s.text for s in drawing.contents if isinstance(s, String))


def test_palette_matches_frontend_light_tokens():
    tokens = json.loads(
        (Path(__file__).resolve().parents[3] / "frontend/design/tokens.json").read_text(encoding="utf-8")
    )
    found = {}

    def collect(node):
        if isinstance(node, dict):
            if node.get("name", "").startswith("viz-"):
                found[node["name"]] = node["value"]["light"]
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(tokens)
    assert export.VIZ_PALETTE == tuple(found[f"viz-{i}"] for i in range(1, 9))
    assert export.VIZ_NEUTRAL == found["viz-neutral"]


@pytest.mark.parametrize("kind", ["bar", "grouped_bar", "stacked_bar"])
def test_bar_orientations_render_without_changing_materialized_values(kind):
    spec = all_specs()[kind]
    original = deepcopy(spec)
    font, _ = export.pdf_font()
    for orientation in ("horizontal", "vertical"):
        drawing = export._spec_chart({**spec, "orientation": orientation}, font)
        assert any(isinstance(shape, Rect) for shape in drawing.contents)
    assert spec == original


def test_four_multiples_panels_paginate_and_keep_figure_title(monkeypatch):
    data = dataset(rows=[[str(i), i, i / 100, i * 2, i / 10] for i in range(20)])
    data["columns"] = ["category", "a", "b", "c", "d"]
    data["meta"]["units"] = {
        "a": {"kind": "count"},
        "b": {"kind": "ratio"},
        "c": {"kind": "level", "unit": "元"},
        "d": {"kind": "level", "unit": "人"},
    }
    from tests.unit.test_analysis_export import _pdf_story, _story_text

    source = view(data, [choice(kind="multiples", title="多个指标分别变化")])
    assert export.to_pdf(source).startswith(b"%PDF")
    story = _pdf_story(monkeypatch, source)
    assert "多个指标分别变化" in "\n".join(_story_text(story))
    markup = export.to_html(source)
    assert "多个指标分别变化" in markup and "<svg" in markup


def test_mermaid_missing_values_keep_table_and_escape_code_fences():
    spec = all_specs()["bar"]
    figure = {"chart": spec, "title": "结论```<script>"}
    assert "\\u0060" in "\n".join(export._mermaid_chart(figure))
    spec["series"][0]["values"][0] = None
    assert export._mermaid_chart(figure) == []


def test_markdown_multi_series_line_falls_back_to_table():
    spec = deepcopy(all_specs()["line"])
    spec["series"] = [spec["series"][0], {**spec["series"][0], "name": "另一个系列"}]
    assert export._mermaid_chart({"chart": spec, "title": "结论"}) == []


@pytest.mark.parametrize(
    ("title", "ok"),
    [
        ("华东贡献几乎全部降幅", True),
        ("9 月华东下滑最多", True),
        ("2026-09 线上回落", True),
        ("第 3 季度持平", True),
        ("收入增长999%", False),
        ("前四均超108万元", False),
    ],
)
def test_chart_title_rejects_quantities(title, ok):
    from pydantic import ValidationError

    from avelis.analysis.tools import ChartArgs

    if ok:
        assert ChartArgs(dataset="D1", kind="bar", title=title).title == title
    else:
        with pytest.raises(ValidationError):
            ChartArgs(dataset="D1", kind="bar", title=title)


def test_explicit_export_selection_overrides_saved_figures():
    from avelis.analysis.figures import report_figures
    from tests.unit.test_analysis_export import sample_view

    view = sample_view()
    view["report"]["figures"] = [{"dataset": "D4", "kind": "bar", "title": "已选", "top_n": None}]
    assert [f["ref"] for f in report_figures(view, ["D5"])] == ["D5"]
    assert report_figures(view, []) == []
    assert [f["ref"] for f in report_figures(view)] == ["D4"]
