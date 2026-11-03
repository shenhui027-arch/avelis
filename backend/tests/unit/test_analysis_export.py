"""管理报告导出：正文聚焦结论，保存的证据另列附录。"""

from __future__ import annotations

import re
from copy import deepcopy

import pytest
from reportlab.graphics.shapes import Circle, Drawing, String
from reportlab.lib.colors import HexColor
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Table

from avelis.analysis import export
from avelis.analysis.figures import dataset_charts, key_datasets


def draw_dataset(dataset, font, *, brief=False):
    specs = dataset_charts(dataset)
    if not specs:
        return None
    figure = export._display_figure(
        {"ref": dataset.get("ref", "D1"), "title": None, "chart": specs[0]}, dataset, brief=brief
    )
    return export._spec_chart(figure["chart"], font)


def _value(ref: str, column: str, raw: str, text: str, measure: str = "delta", *, row: int = 1) -> dict:
    return {
        "kind": "value",
        "ref": ref,
        "path": f"{ref}.{column}[#{row}]",
        "label": f"{ref} · {column}",
        "raw": raw,
        "text": text,
        "measure": measure,
        "unit": "元",
        "source": "calc",
        "precision": 2,
    }


def sample_view() -> dict:
    """中立的已保存结果：18 个数据集、9 条结论、6 条原因；可直接生成样例 PDF。"""
    level, delta = {"kind": "level", "unit": "元"}, {"kind": "delta", "unit": "元"}

    def dataset(seq, title, columns, rows, *, kind="governed", units=None, group_by=(), **extra):
        meta = {"units": units or {}, "intent": {"group_by": list(group_by), "time_grain": None}}
        return {
            "ref": f"D{seq}",
            "seq": seq,
            "kind": kind,
            "title": title,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": False,
            "meta": meta,
            "units": meta["units"],
            "intent": meta["intent"],
            "time_column": None,
            "inputs": [],
            "metric_version_ids": [f"version-{seq}"] if kind == "governed" else [],
            "sql": "SELECT 1253000 AS metric_a, 1400000 AS metric_b" if kind == "governed" else None,
            **extra,
        }

    # 本期 / 对比期角色来自量纲、查询意图与结果列顺序，不能猜列名。
    datasets = [
        dataset(
            1,
            "指标甲总量对比",
            ["观测甲", "观测乙", "差额甲", "相对甲"],
            [["1253000", "1400000", "-147000", "-0.105"]],
            units={"观测甲": level, "观测乙": level, "差额甲": delta, "相对甲": {"kind": "rate_change"}},
        ),
        dataset(
            2,
            "主要成员的变化",
            ["dimension", "member", "contribution"],
            [
                ["dimension_code", "成员甲", "-144700"],
                ["dimension_code", "成员乙", "137000"],
                ["dimension_code", "成员丙", "-139300"],
            ],
            kind="calc",
            units={"contribution": delta},
        ),
        dataset(
            3,
            "指标甲分组对比",
            ["分组", "观测甲", "差额甲", "相对甲"],
            [["分组甲", "1000000", "-120000", "-0.1071"], ["分组乙", "253000", "-27000", "-0.0964"]],
            units={"观测甲": level, "差额甲": delta, "相对甲": {"kind": "rate_change"}},
            group_by=["分组"],
        ),
        dataset(
            4,
            "指标乙对比",
            ["成员", "指标乙"],
            [["成员甲", "1024"], ["成员乙", None]],
            units={"指标乙": level},
            group_by=["成员"],
        ),
        dataset(
            5,
            "另一个可选指标",
            ["成员", "指标丙"],
            [["成员甲", "3000"], ["成员乙", "4000"]],
            units={"指标丙": level},
            group_by=["成员"],
        ),
        dataset(
            6,
            "记录计数",
            ["分组", "数量"],
            [["分组甲", 120], ["分组乙", 80]],
            units={"数量": {"kind": "count"}},
            group_by=["分组"],
        ),
        dataset(
            7,
            "口径检查",
            ["type", "at", "object", "item", "change", "impacts", "note"],
            [],
            kind="context",
            units={"impacts": {"kind": "count"}},
        ),
        dataset(
            8,
            "数据健康",
            ["base_asset", "metric_status", "drift", "last_collected_at", "collection_status", "collection_started_at"],
            [
                ["资产甲", "published", "否", "2026-09-30T01:00:00", "succeeded", "2026-09-30T00:00:00"],
                ["资产乙", "published", "否", "2026-09-30T01:00:00", "succeeded", "2026-09-30T00:00:00"],
            ],
            kind="context",
        ),
        dataset(
            9, "上下文数值", ["检查项", "指标"], [["检查甲", 10], ["检查乙", 20]], kind="context", units={"指标": level}
        ),
        dataset(10, "未登记量纲", ["成员", "值"], [["成员甲", 1], ["成员乙", 2]]),
        dataset(11, "单行指标", ["成员", "值"], [["成员甲", 3]], units={"值": level}),
    ]
    for seq in range(12, 19):
        datasets.append(
            dataset(
                seq,
                f"补充指标 {seq}",
                ["成员", "补充值"],
                [["成员甲", seq * 100], ["成员乙", seq * 200]],
                units={"补充值": level},
                group_by=["成员"],
            )
        )
    datasets[0]["meta"]["window"] = {"start": "2026-09-01", "end": "2026-09-30"}
    datasets[0]["intent"].update(
        {
            "start_date": "2026-09-01",
            "end_date": "2026-09-30",
            "compare": "custom",
            "compare_start": "2026-08-01",
            "compare_end": "2026-08-31",
        }
    )
    datasets[2]["truncated"] = True
    datasets[2]["row_count"] = 25
    datasets[2]["rows"] += [[f"分组{i}", str(1000 * i), str(-100 * i), "-0.1"] for i in range(3, 26)]
    negative = _value("D2", "contribution", "-144700", "-14.47万元")
    positive = _value("D2", "contribution", "137000", "+13.70万元", row=2)
    premise = {
        "segments": [{"kind": "text", "text": "其余成员两期合计为净增长"}, positive, {"kind": "text", "text": "。"}]
    }
    validation = {
        "segments": [{"kind": "text", "text": "复核已保存的分组对比"}, _value("D3", "差额甲", "-120000", "-12.00万元")]
    }
    report = {
        "title": "指标变化分析",
        "summary": "指标甲下降，主要变化来自成员甲；部分变化尚未确认。",
        "unexplained": "成员甲变化的具体触发因素尚未确认；补充指标是否相关仍需核实",
        "conclusion": "offsetting",
        "fields": {
            "summary": {
                "segments": [
                    {"kind": "text", "text": "本期指标甲为"},
                    _value("D1", "观测甲", "1253000", "125.30万元", "level"),
                ]
            },
            "unexplained": {"segments": []},
        },
        "claims": [
            {
                "type": "fact",
                "text": "本期指标甲低于对比期。",
                "refs": ["D1", "D3"],
                "segments": [_value("D1", "差额甲", "-147000", "-14.70万元")],
            },
            {"type": "calc", "text": "成员变化存在相互抵消。", "refs": ["D2", "D3", "D4"], "segments": [negative]},
            {
                "type": "inference",
                "text": "成员甲的变化可能与尚未确认的因素有关。",
                "refs": ["D2", "D3"],
                "segments": [],
                "premise": "其余成员两期合计为净增长。13.70万元",
                "validation": "旧的验证文本",
                "details": {"premise": premise, "validation": validation},
            },
            {
                "type": "recommendation",
                "text": "优先复核主要变化，再检查补充证据。",
                "refs": ["D6", "D7", "D8", "D9", "D10", "D11"],
                "segments": [],
                "premise": "沿用已保存的变化贡献。",
                "details": {
                    "premise": {
                        "segments": [
                            {"kind": "text", "text": "沿用已保存的变化贡献"},
                            negative,
                            {"kind": "text", "text": "。"},
                        ]
                    }
                },
            },
            {"type": "fact", "text": "口径与数据方面，已保存的检查未发现异常。", "refs": ["D7", "D8"], "segments": []},
            {"type": "fact", "text": "指标乙的部分成员没有保存观测值。", "refs": ["D4", "D3"], "segments": []},
            {"type": "calc", "text": "补充指标仍存在成员差异。", "refs": ["D5", "D3"], "segments": []},
            {"type": "inference", "text": "补充指标与主要变化的关系仍需确认。", "refs": ["D12"], "segments": []},
            {
                "type": "recommendation",
                "text": "按成员复核指标甲与指标乙的已保存结果。",
                "refs": ["D3", "D4"],
                "segments": [],
            },
        ],
        "causes": [
            {
                "type": "member",
                "label": label,
                "entity": {"metric_code": "metric_a", "dimension": "dimension_code", "member": label},
                "direction": direction,
                "evidence": ["D2"],
                "contribution": {"text": seg["text"], "segments": [seg]},
                "errors": [],
            }
            for label, direction, seg in [
                ("成员甲", "down", negative),
                ("成员乙", "up", positive),
                ("成员丙", "down", _value("D2", "contribution", "-139300", "-13.93万元", row=3)),
            ]
        ],
    }
    report["causes"] += [
        {
            "type": kind,
            "label": label,
            "entity": entity,
            "direction": "down",
            "evidence": ["D2"],
            "contribution": {"text": negative["text"], "segments": [deepcopy(negative)]},
            "errors": [],
        }
        for kind, label, entity in [
            ("calendar", "本期", {"dimension": "calendar_code", "member": "本期"}),
            ("factor", "指标丁", {"metric_code": "metric_d"}),
            ("governance", "已登记事件", {"dimension": "event_code"}),
        ]
    ]
    return {
        "id": "sample-run-id",
        "question": "比较指标甲的变化与主要原因",
        "status": "succeeded",
        "model_adapter": "sample-model",
        "finished_at": "2026-10-01T02:34:56+00:00",
        "window": {},
        "report": report,
        "datasets": datasets,
        "steps": [
            {"seq": n, "kind": "tool", "tool": "sample_step", "status": "ok", "reason": "过程标识不可导出"}
            for n in range(1, 36)
        ],
        "verification": {
            "ok": True,
            "status": "with_inference",
            "unverified": 0,
            "unverified_fields": 0,
            "fields": [],
            "claims": [{"index": i, "status": "verified" if i < 2 else "unchecked", "problems": []} for i in range(9)],
            "causes": [{"index": i, "status": "verified", "problems": []} for i in range(6)],
            "sources": {"direct": {"governed": 3, "calc": 4}},
        },
    }


def _story_text(items):
    for item in items:
        if isinstance(item, Paragraph):
            yield item.getPlainText()
        elif isinstance(item, KeepTogether):
            yield from _story_text(item._content)
        elif isinstance(item, Table):
            for row in item._cellvalues:
                for value in row:
                    if isinstance(value, (list, tuple)):
                        yield from _story_text(value)
                    elif isinstance(value, Paragraph):
                        yield value.getPlainText()
                    else:
                        yield str(value)


def _flowables(items):
    for item in items:
        yield item
        if isinstance(item, KeepTogether):
            yield from _flowables(item._content)
        elif isinstance(item, Table):
            for row in item._cellvalues:
                for value in row:
                    yield from _flowables(value if isinstance(value, (list, tuple)) else [value])


def _pdf_story(monkeypatch, view, **kwargs):
    captured = []
    monkeypatch.setattr(SimpleDocTemplate, "build", lambda self, flowables, *args, **kw: captured.extend(flowables))
    export.to_pdf(view, **kwargs)
    return captured


def test_header_falls_back_to_core_dataset_window_and_custom_comparison():
    view = sample_view()
    assert view["window"] == {}
    assert len(view["datasets"]) == 18 and len(view["report"]["claims"]) == 9 and len(view["report"]["causes"]) == 6
    # D3 的报告引用更多，核心指标 D1 仍应优先。
    view["datasets"][2]["meta"]["window"] = {"start": "2025-01-01", "end": "2025-01-31"}
    text = export._header(view)
    assert "本期 2026-09-01 ~ 09-30 · 对比 2026-08-01 ~ 08-31" in text
    assert "2025-01" not in text and "生成日期" in text
    view["window"] = {
        "start": "2026-10-01",
        "end": "2026-10-31",
        "compare_start": "2026-09-01",
        "compare_end": "2026-09-30",
    }
    assert export._header(view).startswith("本期 2026-10-01 ~ 10-31 · 对比 2026-09-01 ~ 09-30")


def test_header_previous_period_ignores_saved_custom_comparison_dates():
    view = sample_view()
    view["datasets"][0]["intent"]["compare"] = "previous_period"
    # 旧 custom 区间仍留在 intent，当前 compare 决定实际对比期。
    header = export._header(view)
    assert "对比 2026-08-02 ~ 08-31" in header
    assert "对比 2026-08-01" not in header


@pytest.mark.parametrize(
    ("compare", "start", "end", "expected"),
    [
        ("previous_period", "2026-09-01", "2026-09-30", "对比 2026-08-02 ~ 08-31"),
        ("previous_year", "2024-02-01", "2024-02-29", "对比 2023-02-01 ~ 02-28"),
    ],
)
def test_header_uses_most_referenced_governed_window_without_core_metrics(compare, start, end, expected):
    view = sample_view()
    view["datasets"][0]["kind"] = "calc"
    selected = view["datasets"][2]
    selected["meta"]["window"] = {"start": start, "end": end}
    selected["intent"].update({"start_date": start, "end_date": end, "compare": compare})
    header = export._header(view)
    assert header.startswith(f"本期 {start} ~ {end[5:]}") and expected in header
    for dataset in view["datasets"]:
        dataset["meta"].pop("window", None)
        for key in ("start_date", "end_date", "compare", "compare_start", "compare_end"):
            dataset["intent"].pop(key, None)
    assert export._header(view).startswith("生成日期 ")
    assert "本期" not in export._header(view) and "对比" not in export._header(view)


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        ({"type": "calendar", "label": "本期", "entity": {"member": "本期", "dimension": "calendar_code"}}, "日历因素"),
        ({"type": "calendar", "label": "天数", "entity": {"dimension": "calendar_code"}}, "日历因素（天数）"),
        (
            {"type": "calendar", "label": "本期", "entity": {"member": "天数差异", "dimension": "calendar_code"}},
            "日历因素（天数）",
        ),
        (
            {"type": "factor", "label": "指标丁", "entity": {"metric_code": "metric_d", "dimension": "dimension_code"}},
            "指标丁",
        ),
        ({"type": "governance", "label": "本期", "entity": {"dimension": "event_code"}}, "治理事件"),
        (
            {
                "type": "member",
                "label": "dimension_code",
                "entity": {"dimension": "dimension_code", "member": "成员甲"},
            },
            "成员甲",
        ),
    ],
)
def test_cause_labels_follow_type_and_saved_readable_fields(cause, expected):
    assert export._cause_label(cause) == expected
    assert "code" not in export._cause_label(cause)


def test_context_checks_are_summarized_without_repeating_context_only_claims(monkeypatch):
    view = sample_view()
    claims = [
        {"type": "fact", "text": "仅上下文检查结论。", "refs": ["D7", "D8"], "segments": []},
        {
            "type": "calc",
            "text": "数值段上下文检查结论。",
            "refs": [],
            "segments": [_value("D9", "指标", "10", "10元", "level")],
        },
        {"type": "fact", "text": "混合数据结论。", "refs": ["D7", "D3"], "segments": []},
        {"type": "fact", "text": "没有引用的结论。", "refs": [], "segments": []},
        {"type": "fact", "text": "含未知引用的结论。", "refs": ["D7", "D404"], "segments": []},
        {
            "type": "fact",
            "text": "数值段含业务数据的结论。",
            "refs": ["D7"],
            "segments": [_value("D3", "差额甲", "-120000", "-12万元")],
        },
    ]
    view["report"]["claims"] = claims
    markdown = export.to_markdown(view, audience="full")
    body, appendix = markdown.split("## 附录", 1)
    checks = appendix.split("### 数据检查", 1)[1].split("### 用到的数据", 1)[0].strip()
    assert "已保存范围内未发现变更" in checks and "已保存的采集记录均成功，无口径漂移" in checks
    assert "已保存 2 条检查记录，未保存汇总结论" in checks and "\n" not in checks
    for claim in claims[:2]:
        assert claim["text"] not in markdown
        assert claim["text"] not in export.to_markdown(view, appendix=False)
    for claim in claims[2:]:
        assert claim["text"] in body
    story = _pdf_story(monkeypatch, view, audience="full")
    break_at = next(i for i, item in enumerate(story) if isinstance(item, PageBreak))
    pdf_body = "\n".join(_story_text(story[:break_at]))
    pdf_appendix = "\n".join(_story_text(story[break_at + 1 :]))
    for claim in claims[:2]:
        assert claim["text"] not in pdf_body + pdf_appendix
    assert checks in pdf_appendix
    listing = appendix.split("### 用到的数据", 1)[1].split("### 查询明细", 1)[0]
    assert not {"D7", "D8", "D9"} & set(re.findall(r"^\| (D\d+) \|", listing, re.MULTILINE))
    assert all(claim["text"] in pdf_body for claim in claims[2:])
    claim = {
        "type": "inference",
        "refs": ["D7"],
        "segments": [],
        "details": {"premise": {"segments": [_value("D3", "差额甲", "-120000", "-12万元")]}},
    }
    assert not export._context_claim(claim, view)
    claim["details"]["premise"]["segments"][0]["ref"] = "D8"
    assert export._context_claim(claim, view)


def test_key_datasets_only_deduplicate_first_evidence_with_matching_chart_members():
    view = sample_view()
    view["report"]["causes"] = [view["report"]["causes"][0]]
    assert [d["ref"] for d in key_datasets(view, ["D2", "D3", "D4"])] == ["D3", "D4"]
    view["report"]["causes"][0]["entity"]["member"] = "未入图的成员"
    assert [d["ref"] for d in key_datasets(view, ["D2", "D3", "D4"])] == ["D2", "D3"]
    view["report"]["causes"][0]["entity"]["member"] = "成员甲"
    view["report"]["causes"][0]["entity"]["dimension"] = "another_dimension"
    assert [d["ref"] for d in key_datasets(view, ["D2", "D3", "D4"])] == ["D2", "D3"]
    view["report"]["causes"][0]["entity"]["dimension"] = "dimension_code"
    view["report"]["causes"][0]["evidence"] = ["D3", "D2"]
    assert [d["ref"] for d in key_datasets(view, ["D2", "D3", "D4"])] == ["D2", "D3"]


def test_key_datasets_deduplicate_members_present_in_materialized_spec():
    view = sample_view()
    view["report"]["causes"] = [view["report"]["causes"][0]]
    dataset = view["datasets"][1]
    dataset["rows"] = [["dimension_code", "成员甲", None], ["dimension_code", "成员乙", -10]]
    assert dataset_charts(dataset)[0]["kind"] == "bar"  # 两个维度合成一个类目
    assert [d["ref"] for d in key_datasets(view, ["D2", "D3", "D4"])] == ["D3", "D4"]


@pytest.mark.parametrize(
    ("values", "kind", "expected_chart"),
    [
        ([100, 100], "level", True),
        ([100, 109], "level", True),
        ([0, 0], "level", True),
        ([100, 150], "level", True),
        ([-100, 100], "level", True),
        ([-100, -109], "level", True),
        ([100, 109], "delta", True),
    ],
)
def test_two_nearby_levels_follow_recommendation(values, kind, expected_chart, monkeypatch):
    view = sample_view()
    selected = view["datasets"][4]
    selected["rows"] = [["本期", values[0]], ["上期", values[1]]]
    selected["units"]["指标丙"]["kind"] = kind
    font, _ = export.pdf_font()
    assert (draw_dataset(selected, font) is not None) is expected_chart
    story = _pdf_story(monkeypatch, view, appendix=False, datasets=["D5"])
    key_block = next(item for item in story if isinstance(item, KeepTogether))
    assert sum(isinstance(item, Drawing) for item in key_block._content) == int(expected_chart)
    assert sum(isinstance(item, Table) for item in key_block._content) == 1


def test_nearby_level_dataset_with_delta_still_has_one_chart_and_one_table(monkeypatch):
    view = sample_view()
    selected = view["datasets"][4]
    selected["columns"].append("变化")
    selected["rows"] = [["本期", 100, -1], ["上期", 109, 2]]
    selected["units"]["变化"] = {"kind": "delta", "unit": "元"}
    story = _pdf_story(monkeypatch, view, appendix=False, datasets=["D5"])
    block = next(item for item in story if isinstance(item, KeepTogether))
    assert sum(isinstance(item, Drawing) for item in block._content) == 1
    assert sum(isinstance(item, Table) for item in block._content) == 1


def test_source_rows_omit_dataset_prefix_but_csv_retains_raw_path():
    view = sample_view()
    segment = view["report"]["claims"][0]["segments"][0]
    segment["path"] = "D1.指标甲·变化[成员=成员甲]"
    row = next(row for row in export._source_rows(view["report"]) if row[0] == "结论 1")
    assert row[2] == "D1" and row[3] == "指标甲·变化[成员=成员甲]"
    assert segment["path"].encode("utf-8") in export.sources_csv(view)


def test_pdf_core_levels_are_dark_and_only_changes_use_sign_color(monkeypatch):
    view = sample_view()
    view["datasets"][0]["rows"] = [["1253000", "1400000", "147000", "-0.105"]]
    story = _pdf_story(monkeypatch, view, appendix=False)
    paragraphs = [item for item in _flowables(story) if isinstance(item, Paragraph)]
    for text, color in [
        ("125.30万元", "#101828"),
        ("140.00万元", "#101828"),
        ("+14.70万元", export.POSITIVE),
        ("-10.5%", export.NEGATIVE),
    ]:
        metric = next(item for item in paragraphs if item.getPlainText() == text)
        assert any(getattr(frag, "textColor", None) == HexColor(color) for frag in metric.frags)


def test_pdf_reasons_have_only_chart_with_small_gray_references(monkeypatch):
    story = _pdf_story(monkeypatch, sample_view(), audience="full")
    start = next(
        i for i, item in enumerate(story) if isinstance(item, Paragraph) and item.getPlainText().startswith("主要原因")
    )
    end = next(
        i
        for i, item in enumerate(story[start + 1 :], start + 1)
        if isinstance(item, Paragraph) and item.getPlainText() == "关键发现"
    )
    cause_items = story[start + 1 : end]
    assert not list(_story_text(cause_items))  # 数值原因没有第二份文字列表。
    chart = next(item for item in cause_items if isinstance(item, Drawing))
    strings = [item for item in chart.contents if isinstance(item, String)]
    numbers = [item for item in strings if item.text == "-14.47万元"]
    references = [item for item in strings if "D2" in item.text]
    assert numbers and len(references) == 6
    for ref in references:
        assert ref.fillColor == HexColor(export.GRAY)
        number = next(
            item for item in strings if item.y == ref.y and item.text in ("-14.47万元", "+13.70万元", "-13.93万元")
        )
        assert ref.x > number.x and ref.fontSize < number.fontSize


def test_pdf_reason_without_numeric_contribution_and_errors_remain_visible(monkeypatch):
    view = sample_view()
    view["report"]["causes"][0]["errors"] = ["保存的贡献值尚未核实"]
    view["report"]["causes"][1]["contribution"] = {"text": "尚未量化", "segments": []}
    story = _pdf_story(monkeypatch, view, audience="full")
    text = "\n".join(_story_text(story))
    assert "保存的贡献值尚未核实" in text and "成员乙" in text and "尚未量化" in text
    assert not re.search(r"[1-6]\. (成员甲|成员乙|成员丙|日历因素|指标丁|治理事件)", text)


def test_pdf_findings_are_compact_bullets_and_references_wrap_as_one_group(monkeypatch):
    view = sample_view()
    claim = {
        "type": "fact",
        "text": "比较已保存的指标结果，主要成员的变化仍需结合各项证据进行核对。",
        "refs": ["D13", "D16"],
        "segments": [],
    }
    view["report"]["claims"] = [claim]
    story = _pdf_story(monkeypatch, view, audience="full")
    paragraph = next(
        item for item in _flowables(story) if isinstance(item, Paragraph) and claim["text"] in item.getPlainText()
    )
    assert paragraph.bulletText or paragraph.getPlainText().startswith("•")
    assert paragraph.style.leading < 14
    block = next(
        item
        for item in _flowables(story)
        if isinstance(item, Table)
        and len(item._cellvalues) == 1
        and len(item._cellvalues[0]) == 2
        and item._cellvalues[0][0] is paragraph
    )
    citation = block._cellvalues[0][1]
    assert isinstance(citation, Paragraph)
    reference_fragments = [
        frag for frag in citation.frags if "D13" in getattr(frag, "text", "") or "D16" in getattr(frag, "text", "")
    ]
    assert reference_fragments and all(
        frag.fontSize == 7 and frag.textColor == HexColor(export.GRAY) for frag in reference_fragments
    )
    for available_width in (150, 170, 210, 240, 280):
        block.wrap(available_width, 1000)
        body_cell, ref_cell = block._cellStyles[0]
        assert body_cell.valign == ref_cell.valign == "BOTTOM"
        body_width, ref_width = block._colWidths
        paragraph.wrap(body_width - body_cell.leftPadding - body_cell.rightPadding, 1000)
        citation.wrap(ref_width - ref_cell.leftPadding - ref_cell.rightPadding, 1000)
        lines = [
            "".join(getattr(word, "text", str(word)) for word in line.words)
            if hasattr(line, "words")
            else "".join(line[1])
            for line in citation.blPara.lines
        ]
        assert len(lines) == 1 and "D13" in lines[0] and "D16" in lines[0]
        last_line = paragraph.blPara.lines[-1]
        remaining = last_line.extraSpace if hasattr(last_line, "extraSpace") else last_line[0]
        body_end = body_width - body_cell.rightPadding - paragraph.style.rightIndent - remaining
        reference_start = body_width + ref_cell.leftPadding
        assert reference_start - body_end == pytest.approx(2)  # 引用贴在正文末行句末之后。


def test_renderer_preserves_model_segment_order_even_when_sentence_is_awkward():
    view = sample_view()
    claim = view["report"]["claims"][2]
    claim["details"]["premise"]["segments"] = [
        {"kind": "text", "text": "其余成员两期合计为净增长。"},
        _value("D2", "contribution", "137000", "13.70万元", row=2),
    ]
    assert "前提：其余成员两期合计为净增长。13.70万元" in export.to_markdown(view, audience="full")


def test_markdown_has_executive_sections_and_separate_appendix():
    view = sample_view()
    view["report"]["method"] = "按已保存的对比结果检查指标与成员变化。"
    for dataset in view["datasets"]:
        if dataset["kind"] == "governed":
            dataset["sql"] = f"SELECT {dataset['seq']} AS saved_dataset"
    text = export.to_markdown(view, audience="full")
    body, appendix = text.split("## 附录", 1)
    headings = [
        "# 指标变化分析",
        "## 核心指标",
        "## 结论",
        "## 主要原因",
        "## 关键发现",
        "## 判断与建议",
        "## 还没查清",
        "## 关键数据",
        "## 附录",
    ]
    assert [text.index(h) for h in headings] == sorted(text.index(h) for h in headings)
    for expected in (
        "125.30万元",
        "140.00万元",
        "-14.70万元",
        "-10.5%",
        "本期",
        "对比期",
        "变化率",
        "2026-09-01",
        "09-30",
        "2026-08-01",
        "08-31",
        "生成日期",
    ):
        assert expected in text
    assert "sample-run-id" not in text and "sample-model" not in text and "02:34:56" not in text
    assert "sample_step" not in text and "过程标识不可导出" not in text and "## 步骤" not in text
    assert "成员 ·" not in text and "dimension_code：" not in text
    assert "推断，未核实" in body
    assert appendix.lstrip().startswith("**分析方法**：按已保存的对比结果检查指标与成员变化。")
    assert re.findall(r"^### (.+)$", appendix, re.MULTILINE) == ["数据检查", "用到的数据", "查询明细"]
    assert "数字来源" not in text and "来源构成" not in text and "可追溯 ≠ 可复现" not in text
    assert "来源全部通过" not in body and "来源全部通过" in appendix
    listing, queries = appendix.split("### 用到的数据", 1)[1].split("### 查询明细", 1)
    assert re.findall(r"^\| (D\d+) \|", listing, re.MULTILINE) == [
        "D1",
        "D2",
        "D3",
        "D4",
        "D5",
        "D6",
        "D10",
        "D11",
        "D12",
    ]
    assert "| 编号 | 名称 | 来源 | 行数 | 是否截断 |" in listing
    assert "| D2 | 指标明细 | 计算 | 3 | 否 |" in listing
    assert "| D3 | 观测甲 · 按分组 | 指标口径 | 25 | 是 |" in listing
    assert "指标甲分组对比" not in listing
    query_refs = re.findall(r"^#### (D\d+) ", queries, re.MULTILINE)
    assert query_refs == ["D1", "D3", "D4", "D5", "D6", "D10", "D11", "D12"]
    for seq in (1, 3, 4, 5, 6, 10, 11, 12):
        assert f"SELECT {seq} AS saved_dataset" in queries and f"口径版本：version-{seq}\n" in queries
    for seq in range(13, 19):
        assert f"SELECT {seq} AS saved_dataset" not in queries
    assert "已保存范围内未发现变更" in text and "已保存的采集记录均成功，无口径漂移" in text
    assert "共 25 行，显示前 10 行" in text and "结果被截断" in text
    assert "—" in export.to_markdown(view, datasets=["D4"])  # 表格仍保留空值


def test_key_datasets_use_saved_units_references_and_limit():
    view = sample_view()
    chosen = key_datasets(view, None)
    assert [d["ref"] for d in chosen] == ["D3", "D1"]  # 单行总量的 KPI 也是合法默认规格。
    assert not any(d["kind"] == "context" for d in chosen)
    assert not {"D6", "D7", "D8", "D9", "D10", "D11"} & {d["ref"] for d in chosen}
    assert [d["ref"] for d in key_datasets(view, ["D5", "D4"])] == ["D5", "D4"]
    assert [d["ref"] for d in key_datasets(view, ["D2", "D5", "D5", "D4", "D3"])] == ["D5", "D4"]
    text = export.to_markdown(view, audience="full", datasets=["D5"]).split("## 附录", 1)[0]
    assert "另一个可选指标" in text and "指标甲分组对比" not in text
    assert "SELECT " not in text and "用到的数据" not in text and "sample_step" not in text
    brief = export.to_markdown(view, appendix=False, datasets=["D5"])
    assert "## 附录" not in brief and "SELECT " not in brief and "用到的数据" not in brief
    full = export.to_markdown(view, audience="full")
    body, appendix = full.split("## 附录", 1)
    assert len(re.findall(r"^### D\d+ ", body, re.MULTILINE)) == 2
    assert "另一个可选指标" not in body + appendix
    assert "| D5 | 指标丙 · 按成员 | 指标口径 | 2 | 否 |" in appendix


def test_core_metrics_require_governed_ungrouped_single_row_and_registered_roles():
    view = sample_view()
    assert [x["label"] for x in export._core_metrics(view)] == ["本期", "对比期", "变化", "变化率"]
    for change in (
        {"kind": "calc"},
        {"rows": [[1, 2, 3, 4], [2, 3, 4, 5]], "row_count": 2},
        {"units": {}, "meta": {"units": {}, "intent": {}}},
    ):
        altered = deepcopy(view)
        altered["datasets"][0].update(change)
        assert export._core_metrics(altered) == []
    grouped = deepcopy(view)
    grouped["datasets"][0]["intent"]["group_by"] = ["group_code"]
    assert export._core_metrics(grouped) == []


def test_core_metrics_keep_three_values_when_zero_comparison_has_no_rate():
    view = sample_view()
    view["datasets"][0]["rows"] = [["1", "0", "1", None]]
    metrics = export._core_metrics(view)
    assert [item["label"] for item in metrics] == ["本期", "对比期", "变化"]
    assert [item["raw"] for item in metrics] == [1, 0, 1]


def test_key_tables_format_saved_values_and_limit_rows_and_columns():
    view = sample_view()
    selected = view["datasets"][4]
    selected["columns"] = ["成员", "指标丙", "比率", "字段甲", "字段乙", "字段丙", "第七列"]
    selected["rows"] = [
        [f"成员{i}", "1234.5" if i != 9 else None, "-0.125", "甲", "乙", "丙", "不应输出"] for i in range(1, 13)
    ]
    selected["row_count"] = 12
    selected["units"]["比率"] = {"kind": "rate_change"}
    text = export.to_markdown(view, appendix=False, datasets=["D5"])
    assert "| 成员 | 指标丙 | 比率 | 字段甲 | 字段乙 | 字段丙 |" in text
    assert "第七列" not in text and "不应输出" not in text
    assert "1234.5" in text and "1,234.5" not in text and "-12.5%" in text and "—" in text
    assert "| 成员10 |" in text and "| 成员11 |" not in text
    assert "共 12 行，显示前 10 行" in text


def test_segments_keep_claim_details_once_and_in_claim_order():
    segments = export._segments(sample_view()["report"])
    details = [(where, seg["raw"]) for where, _, seg in segments if "前提" in where or "验证方式" in where]
    assert details == [("结论 3 前提", "137000"), ("结论 3 验证方式", "-120000"), ("结论 4 前提", "-144700")]


def test_detail_text_is_rendered_from_segments_before_legacy_flat_text():
    text = export.to_markdown(sample_view(), appendix=False)
    assert "其余成员两期合计为净增长+13.70万元。" in text
    assert "其余成员两期合计为净增长。13.70万元" not in text
    assert "复核已保存的分组对比-12.00万元" in text and "旧的验证文本" not in text


def test_unchecked_inference_reference_errors_remain_visible_in_body(monkeypatch):
    captured = []
    original = SimpleDocTemplate.build

    def record(self, flowables, *args, **kwargs):
        captured.append(list(flowables))
        return original(self, flowables, *args, **kwargs)

    monkeypatch.setattr(SimpleDocTemplate, "build", record)
    for in_detail, reason in [(False, "引用数据集 D404 不存在"), (True, "前提引用数据集 D405 不存在")]:
        view = sample_view()
        claim = view["report"]["claims"][2]
        claim["errors"] = [] if in_detail else [reason]
        claim["details"]["premise"]["errors"] = [reason] if in_detail else []
        view["report"]["claims"] = [claim]
        view["verification"]["claims"] = [{"index": 0, "status": "unchecked", "problems": []}]
        assert reason in export.to_markdown(view, audience="full")
        assert export.to_pdf(view, audience="full").startswith(b"%PDF")
        assert reason in "\n".join(_story_text(captured[-1]))


def test_chart_preserves_materialized_categories_and_missing_values():
    font, _ = export.pdf_font()
    dataset = {
        "columns": ["member", "value"],
        "rows": [["成员甲", -5], ["空值成员", None], ["成员乙", 3]],
        "meta": {"units": {"value": {"kind": "level", "unit": "元"}}},
    }
    specs = dataset_charts(dataset)
    chart = export._spec_chart(specs[0], font)
    labels = [c.text for c in chart.contents if isinstance(c, String)]
    assert {"成员甲", "成员乙", "空值成员"} <= set(labels)
    assert specs[0]["series"][0]["values"] == [3, -5, None]


def test_trend_counts_are_measures_and_missing_values_leave_gaps():
    font, _ = export.pdf_font()
    dataset = {
        "columns": ["at", "rows", "value"],
        "time_column": "at",
        "units": {"rows": {"kind": "count"}, "value": {"kind": "level", "unit": "元"}},
        "rows": [["2026-09-01", 2, 10], ["2026-09-02", 5, None], ["2026-09-03", 8, 30]],
    }
    spec = dataset_charts(dataset)[0]
    assert spec["kind"] == "multiples" and len(spec["series"]) == 2
    assert spec["series"][1]["values"] == [10, None, 30]
    chart = export._spec_chart({**spec, "kind": "line", "series": [spec["series"][1]]}, font)
    assert len([item for item in chart.contents if isinstance(item, Circle)]) == 2


def test_pdf_generates_compact_body_and_appendix_starts_with_page_break(monkeypatch):
    captured = []
    original = SimpleDocTemplate.build

    def record(self, flowables, *args, **kwargs):
        captured.append(list(flowables))
        return original(self, flowables, *args, **kwargs)

    monkeypatch.setattr(SimpleDocTemplate, "build", record)
    pdf = export.to_pdf(sample_view(), audience="full")
    assert pdf.startswith(b"%PDF")
    story = captured[-1]
    break_at = next(i for i, item in enumerate(story) if isinstance(item, PageBreak))
    body, appendix = "\n".join(_story_text(story[:break_at])), "\n".join(_story_text(story[break_at + 1 :]))
    assert all(title in body for title in ("指标变化分析", "核心指标", "结论", "主要原因", "还没查清"))
    assert "附录" in appendix and "用到的数据" in appendix and "查询明细" in appendix
    assert "数字来源" not in body + appendix and "数据集清单" not in body + appendix
    assert all(f"version-{seq}" not in appendix for seq in range(13, 19))
    assert "version-1" in appendix and "version-3" in appendix
    assert "其余成员两期合计为净增长+13.70万元。" in body
    assert "sample_step" not in body + appendix and "sample-run-id" not in body
    assert len(re.findall(rb"/Type\s*/Page\b", pdf)) <= 8
    compact = export.to_pdf(sample_view(), appendix=False)
    assert not any(isinstance(item, PageBreak) for item in captured[-1])
    assert "附录" not in list(_story_text(captured[-1]))
    assert len(re.findall(rb"/Type\s*/Page\b", compact)) <= 3
