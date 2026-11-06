(
    """分析报告导出（ADR 0007 第 11 点）：Markdown、HTML、PDF（含图表与证据附录）、数据集 CSV。

导出内容与报告页读同一份保存结果。默认 brief 只给出正文、可读标签与白话风险提示；full 保留引用编号、
原始检查原因和依据附录。appendix 为旧参数，只有未指定 audience 时才决定受众。
PDF、Markdown 使用渲染后的正文（text）；每个数字的来源（数据集、取值路径、量纲、单位、格式、原值）另有一张
引用来源表（sources_csv，ADR 0009）。来源含探索数据的数字标“探索查询（未经口径审核）”。
PDF 嵌入中文 TrueType 字体（子集）：AVELIS_PDF_FONT 指定的字体文件，否则在常见位置找系统字体（Windows 等线 / 微软雅黑 /
黑体，Linux 文泉驿 / Noto CJK）；都找不到时退回 reportlab 内置的 STSong-Light（不嵌入，需阅读器有亚洲字体包），"""
    """并在报告中注明。
"""
)

from __future__ import annotations

import csv
import io
import json
import os
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from avelis.analysis.figures import category_omission_note, report_figures
from avelis.analysis.figures import reference_counts as _reference_counts
from avelis.analysis.grounding import _unit, compact, display, number
from avelis.charts import category_label
from avelis.i18n import gettext

TYPE_LABEL = {"fact": "事实", "calc": "计算", "inference": "推断", "recommendation": "建议"}
DATA_SOURCE_LABEL = {"governed": "指标口径", "explore": "临时查询，未经口径审核", "calc": "计算"}
REPORT_STATUS = {
    "all_passed": "来源全部通过",
    "with_inference": "来源全部通过，含未核实的推断",
    "no_facts": "没有可核实的事实",
    "failed": "有未通过的事实或字段",
}
MEASURE_LABEL = {
    "level": "水平值",
    "delta": "差额",
    "ratio": "比率",
    "rate_change": "变化率",
    "share": "占比",
    "count": "计数",
    "unknown": "未知",
}
EXPLORE_MARK = "探索查询（未经口径审核）"

Audience = Literal["brief", "full"]
BRIEF_SOURCE_NOTE = "报告中的数字均来自系统内已审核的指标口径，可在 Avelis 中查看计算依据。"
BRIEF_EXPLORE_NOTE = "部分数字来自临时查询，尚未经过指标口径审核。"

# 产品计算工具的固定输出列；业务指标、维度的名称始终从元数据读取。
CALC_COLUMN_LABELS = {
    "period": "期间",
    "days": "天数",
    "weekend_days": "周末天数",
    "holidays": "节假日",
    "holiday_days": "节假日天数",
    "workdays": "工作日天数",
    "working_days": "工作日天数",
    "value": "合计",
    "daily": "日均",
    "current": "本期",
    "previous": "对比期",
    "baseline": "对比期",
    "change": "变化",
    "delta": "差额",
    "change_rate": "变化率",
    "rate_change": "变化率",
    "dimension": "维度",
    "member": "成员",
    "contribution": "变化贡献",
    "share": "占比",
    "rank": "排名",
    "rows": "行数",
    "count": "数量",
    "factor": "因素",
    "effect": "影响",
    "weight": "权重",
    "ratio": "比率",
    "residual": "剩余变化",
    "share_of_change": "变化占比",
    "factor_metric_code": "指标标识",
    "x": "变量一",
    "y": "变量二",
    "n": "样本数",
    "pearson": "线性相关系数",
    "spearman": "秩相关系数",
    "p_value": "显著性概率",
    "significant": "是否显著",
    "note": "说明",
    "periods": "期间数",
    "missing_count": "缺失数",
    "missing_periods": "缺失期间",
    "name": "名称",
    "dataset": "来源",
    "column": "字段",
    "sum": "合计",
    "mean": "均值",
    "median": "中位数",
    "min": "最小值",
    "max": "最大值",
    "stdev": "标准差",
    "segment": "分组",
    "side": "偏离方向",
    "member_change": "成员变化",
    "ep": "影响幅度",
    "structure_effect": "结构影响",
    "rate_effect": "比率影响",
    "change_type": "变化类型",
    "role": "作用",
}
CALC_PERIOD_LABELS = {
    "current": "本期",
    "previous": "对比期",
    "baseline": "对比期",
    "previous_period": "上一期",
    "previous_calendar": "上一自然期",
    "previous_year": "去年同期",
    "custom": "对比期",
}


def resolve_audience(audience: Audience | None = None, appendix: bool | None = None) -> Audience:
    if audience is not None:
        if audience not in ("brief", "full"):
            raise ValueError("audience 只能是 brief 或 full")
        return audience
    return "full" if appendix else "brief"


def _brief_text(value: str, *, references: bool = False) -> str:
    """旧报告的自由文字也可能带引用和术语；只调整展示，不改保存结果。"""
    value = re.sub(r"```.*?```", "", value, flags=re.S)
    value = re.sub(r"(?im)^.*\bSELECT\b.*$", "", value)
    if references:
        ref_list = r"D[1-9]\d*(?:\s*[、,，·]\s*D[1-9]\d*)*"
        value = re.sub(rf"[（(\[]\s*{ref_list}\s*[）)\]]", "", value)

        def reference(match):
            # refs 只说明记录用过数据集，不能证明正文里的同名成员是引用。
            # 没有片段元数据的旧正文，只清理明确的数据来源语境中的裸引用。
            before, after = value[: match.start()], value[match.end() :]
            contextual = re.search(
                r"(?:引用(?:数据集)?|数据集|受治理(?:指标|查询)?|来源已校验|来源已确认)\s*$", before
            ) or re.match(r"\s*的?\s*(?:受治理(?:指标|查询|计算)|探索查询|口径版本)", after)
            return "" if contextual else match[0]

        # 中文也是词字符，不能把“成员D1”里的成员编号当成独立引用。
        value = re.sub(r"(?<!\w)D[1-9]\d*(?!\w)", reference, value)
    for old, new in (
        ("推断，未核实", "待验证"),
        ("受治理查询", "已审核指标"),
        ("受治理", "已审核"),
        ("探索查询", "临时查询"),
        ("上下文", "相关信息"),
        ("口径版本", "指标定义"),
        ("来源已校验", "来源已确认"),
        ("核验", "检查"),
        ("计算", "分析"),
        ("验证方式", "建议这样验证"),
        ("前提", "依据"),
        ("SQL", "查询依据"),
    ):
        value = value.replace(old, new)
    return value


def _brief_view(view: dict) -> dict:
    text_keys = {"title", "summary", "unexplained", "method", "text", "premise", "validation"}
    report = view.get("report") or {}
    version_ids = sorted(
        {
            str(version)
            for d in view.get("datasets") or []
            for version in [
                *(d.get("metric_version_ids") or []),
                *(_meta(d).get("metric_version_ids") or []),
                *(_meta(d).get("versions") or {}).values(),
            ]
        },
        key=len,
        reverse=True,
    )

    def clean(value, key="", grounded=False):
        if isinstance(value, dict):
            if key == "entity" or value.get("kind") == "label":
                return dict(value)
            if value.get("kind") == "ref":
                return {**value, "text": ""}
            segments = value.get("segments") or []
            # 有落地标签或引用片段时，文字片段已给出位置语义，不能再按编号猜测引用。
            structured = any(segment.get("kind") in ("ref", "label") for segment in segments)
            result = {k: clean(v, k, structured if k == "segments" else grounded) for k, v in value.items()}
            if segments and value.get("text") == _rendered(value):
                result["text"] = _rendered(result)
            return result
        if isinstance(value, list):
            return [clean(v, key, grounded) for v in value]
        if isinstance(value, str) and key in text_keys:
            for version_id in version_ids:
                if version_id:
                    value = value.replace(version_id, "")
            return _brief_text(value, references=not grounded)
        return value

    cleaned = clean(report)
    for name, record in (report.get("fields") or {}).items():
        if record.get("segments") and report.get(name) == _rendered(record):
            cleaned[name] = _rendered(cleaned["fields"][name])
    return {**view, "report": cleaned}


def _brief_footer(view: dict, datasets: list[str] | None = None) -> list[str]:
    report, verification = view.get("report") or {}, view.get("verification") or {}
    sources = verification.get("sources") or {}
    explore = bool(
        sources.get("with_explore")
        or (sources.get("direct") or {}).get("explore")
        or any(
            s.get("explore") or s.get("source") == "explore"
            for _, _, s in _segments(report)
            if s.get("kind") in ("value", "count")
        )
    )
    saved = {d["ref"]: d for d in view.get("datasets") or []}
    pending = [*_reference_counts(report), *(f["ref"] for f in report_figures(view, datasets))]
    seen = set()
    while pending:
        ref = pending.pop()
        if ref in seen or ref not in saved:
            continue
        seen.add(ref)
        d, meta = saved[ref], _meta(saved[ref])
        explore |= d.get("kind") == "explore" or "explore" in (meta.get("root_kinds") or [])
        pending.extend([*(d.get("inputs") or []), *(meta.get("closure") or [])])
    notes = [gettext(BRIEF_EXPLORE_NOTE if explore else BRIEF_SOURCE_NOTE)]
    failed = 0
    for group, counter in (("claims", "unverified"), ("fields", "unverified_fields"), ("causes", "unverified_causes")):
        key = "field" if group == "fields" else "index"
        bad = {
            c.get(key, i)
            for i, c in enumerate(verification.get(group) or [])
            if c.get("status") == "unverified" or c.get("problems")
        }
        records = report.get(group) or ([] if group != "fields" else {})
        records = records.items() if isinstance(records, dict) else enumerate(records)
        for index, record in records:
            if (
                record.get("status") == "unverified"
                or record.get("errors")
                or any(d.get("errors") for d in (record.get("details") or {}).values())
            ):
                bad.add(index)
        failed += max(verification.get(counter) or 0, len(bad))
    failed += bool((verification.get("conclusion") or {}).get("problems"))
    if not failed and verification.get("status") == "failed":
        failed = 1
    if failed:
        notes.append(gettext("有 {failed} 处数字未通过系统校验，请谨慎使用。", failed=failed))
    return notes


REF = re.compile(r"^D[1-9]\d*$")
FIELD_LABEL = {"title": "标题", "summary": "摘要", "unexplained": "未解释", "method": "分析方法"}


def _refs(claim: dict) -> str:
    """引用编号只显示 D + 数字；其余（旧数据或异常输入）不原样输出，只计数。"""
    refs = [str(r) for r in claim.get("refs") or []]
    good = [r for r in refs if REF.match(r)]
    bad = len(refs) - len(good)
    return ("、".join(good) or "—") + (gettext("（另有 {bad} 个无效引用）", bad=bad) if bad else "")


def _verdict(verification: dict) -> str:
    status = verification.get("status") or ("all_passed" if verification.get("ok") else "failed")
    parts = [gettext(REPORT_STATUS[status]) if status in REPORT_STATUS else status]
    if verification.get("unverified"):
        parts.append(gettext("{count} 条事实 / 计算类结论未通过", count=verification["unverified"]))
    if verification.get("unverified_fields"):
        parts.append(gettext("{count} 处报告字段未通过", count=verification["unverified_fields"]))
    return "，".join(parts)


def _segments(report: dict) -> list[tuple[str, str, dict]]:
    """报告里全部引用段：（位置，类型，段）。"""
    out = []
    for name in ("title", "summary", "unexplained", "method"):
        for seg in ((report.get("fields") or {}).get(name) or {}).get("segments") or []:
            out.append((gettext(FIELD_LABEL[name]), gettext("字段"), seg))
    for i, claim in enumerate(report.get("claims", [])):
        kind = gettext(TYPE_LABEL[claim["type"]]) if claim.get("type") in TYPE_LABEL else claim.get("type") or ""
        for seg in claim.get("segments") or []:
            out.append((gettext("结论 {index}", index=i + 1), kind, seg))
        for key, label in (("premise", "前提"), ("validation", "验证方式")):
            for seg in ((claim.get("details") or {}).get(key) or {}).get("segments") or []:
                out.append((gettext("结论 {index} {label}", index=i + 1, label=gettext(label)), kind, seg))
    for i, cause in enumerate(report.get("causes") or []):
        for seg in (cause.get("contribution") or {}).get("segments") or []:
            out.append((gettext("原因 {index}", index=i + 1), gettext("原因"), seg))
    return [x for x in out if x[2].get("kind") in ("value", "count", "label", "window")]


CONCLUSION = {
    "ranked_reasons": "有排序的原因",
    "offsetting": "成员变化相互抵消",
    "no_material_change": "没有实质变化",
    "insufficient_evidence": "证据不足",
}


def _rendered(record: dict | None, fallback: str = "") -> str:
    record = record or {}
    if record.get("segments"):
        return "".join(str(s.get("text") or "") for s in record["segments"])
    return record.get("text") or fallback


def _field(report: dict, name: str) -> str:
    return report.get(name) or _rendered((report.get("fields") or {}).get(name))


def _detail(claim: dict, key: str) -> str:
    return _rendered((claim.get("details") or {}).get(key), claim.get(key) or "")


def _meta(d: dict) -> dict:
    return d.get("meta") or {}


RESTRICTED_NOTE = "你已没有该数据的访问权限"


def _localized_number(text: str, unit: str = "") -> str:
    """只翻译系统数字后缀；数据集的业务单位与自由文字保留原样。"""
    if unit and not text.endswith(unit):
        return text
    numeric = text.removesuffix(unit) if unit else text
    match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)(个百分点|万|亿)", numeric)
    if not match:
        return text
    suffixes = {"个百分点": gettext("个百分点"), "万": gettext("万"), "亿": gettext("亿")}
    return match[1] + suffixes[match[2]] + unit


def _localized_report(report: dict) -> dict:
    """导出副本只调整已落地的数值片段，再同步其保存的拼接文字。"""

    def localize(value):
        if isinstance(value, list):
            return [localize(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: localize(item) for key, item in value.items()}
        if value.get("kind") == "value":
            result["text"] = _localized_number(str(value.get("text") or ""), str(value.get("unit") or ""))
        if value.get("segments") and value.get("text") == _rendered(value):
            result["text"] = _rendered(result)
        for key in ("fields", "details"):
            for name, record in (value.get(key) or {}).items():
                if record.get("segments") and value.get(name) == _rendered(record):
                    result[name] = _rendered(result[key][name])
        return result

    return localize(report)


def _export_view(view: dict) -> dict:
    """受限标记是导出的权限边界；不信任同一对象里残留的行、元数据或已渲染文字。"""
    from avelis.analysis.dataset_projection import restrict_report, restrict_verification

    restricted = {d["ref"] for d in view.get("datasets") or [] if d.get("restricted")}
    if not restricted:
        return {**view, "report": _localized_report(view.get("report") or {})}
    report = restrict_report(view.get("report") or {}, restricted)
    fields = {name: record for name, record in (report.get("fields") or {}).items() if not record.get("restricted")}
    claims = []
    for claim in report.get("claims") or []:
        if claim.get("restricted"):
            continue
        visible = {
            **claim,
            "details": {
                key: detail for key, detail in (claim.get("details") or {}).items() if not detail.get("restricted")
            },
        }
        # 旧报告同时保存平铺文本和 details，不能回退到已经遮蔽的旧文本。
        for key, detail in (claim.get("details") or {}).items():
            if detail.get("restricted"):
                visible.pop(key, None)
                visible["errors"] = []  # 旧核验错误可能复述该前提中的原值。
        claims.append(visible)
    projected = {
        **report,
        "fields": fields,
        "claims": claims,
        "causes": [cause for cause in report.get("causes") or [] if not cause.get("restricted")],
    }
    for name, record in (report.get("fields") or {}).items():
        if record.get("restricted"):
            projected.pop(name, None)
    verification = restrict_verification(view.get("verification") or {}, report)
    if verification.get("conclusion"):
        verification["conclusion"]["problems"] = []  # 全局结论检查可能复述任意受限来源。
    # 过滤后下标会变化，核验记录按原结论和原因的对应关系重新编号。
    for key in ("claims", "causes"):
        checks = {item["index"]: item for item in verification.get(key) or []}
        readable = [i for i, record in enumerate(report.get(key) or []) if not record.get("restricted")]
        verification[key] = [{**checks[old], "index": new} for new, old in enumerate(readable) if old in checks]
    return {
        **view,
        "report": _localized_report(projected),
        "verification": verification,
        "datasets": [
            {"ref": d["ref"], "kind": d.get("kind"), "restricted": True, "rows": []} if d.get("restricted") else d
            for d in view.get("datasets") or []
        ],
    }


def _units(d: dict) -> dict:
    return _meta(d).get("units") or d.get("units") or {}


def _column_roles(d: dict) -> dict | None:
    """空契约也有意义：只有完全没有契约的旧结果才走兼容推断。"""
    if "column_roles" in d:
        return d["column_roles"]
    return _meta(d).get("column_roles")


def _export_labels(d: dict, view: dict) -> dict:
    """计算结果沿输入继承显示元数据，固定列名另由产品映射；不改原始结果列。"""
    saved = {item["ref"]: item for item in view.get("datasets") or []}
    pending, seen = [d], set()
    names, dimensions, docs = [], {}, {}
    while pending:
        item = pending.pop(0)
        if item.get("restricted"):
            continue
        if item.get("ref") in seen:
            continue
        seen.add(item.get("ref"))
        meta = _meta(item)
        labels = meta.get("export_labels") or meta
        names.extend(n for n in labels.get("metric_names") or [] if n not in names)
        for dim in labels.get("dimensions") or []:
            if dim.get("name"):
                dimensions.setdefault(dim["name"], dim)
        for name, doc in (labels.get("column_docs") or {}).items():
            docs.setdefault(name, doc)
        pending.extend(saved[ref] for ref in item.get("inputs") or [] if ref in saved and ref not in seen)
    return {"metric_names": names, "dimensions": list(dimensions.values()), "column_docs": docs}


def _column_label(d: dict, column: str) -> str:
    roles = _column_roles(d)
    meta = _meta(d)
    if label := (meta.get("column_labels") or {}).get(column):
        return str(label)
    label = (roles.get(column) or {}).get("label") if roles is not None else None
    if label and (label != column or d.get("kind") != "calc"):
        return str(label)
    labels = meta.get("export_labels") or meta
    dim = next((dim for dim in labels.get("dimensions") or [] if dim.get("name") == column), {})
    doc = (labels.get("column_docs") or {}).get(column) or {}
    label = (
        dim.get("label")
        or dim.get("中文名")
        or dim.get("comment")
        or (doc if isinstance(doc, str) else doc.get("comment"))
    )
    if not label and d.get("kind") == "calc":
        label = gettext(CALC_COLUMN_LABELS[column]) if column in CALC_COLUMN_LABELS else None
        if not label and (match := re.fullmatch(r"(.+)_(sum|mean|median)", column)):
            label = _column_label(d, match[1]) + "·" + gettext(CALC_COLUMN_LABELS[match[2]])
    return _brief_text(str(label or column))


def _dataset_title(d: dict) -> str:
    meta = _meta(d)
    measure_labels = meta.get("measure_labels") or {}
    names = (meta.get("export_labels") or meta).get("metric_names") or []
    if not names:
        names = list(
            dict.fromkeys(
                measure_labels.get(c) or re.sub(r"·(?:本期|对比期|上一期|去年同期|差额|变化率|变化)$", "", c)
                for c in d.get("columns") or []
                if c in _units(d) and (_units(d)[c] or {}).get("kind") in ("level", "ratio")
            )
        )[:2]
    names = list(dict.fromkeys(measure_labels.get(name) or _column_label(d, name) for name in names))
    args = meta.get("args") or {}
    keys = (
        (meta.get("intent") or {}).get("group_by")
        or args.get("keys")
        or ([args["segment"]] if args.get("segment") else [])
    )
    title = "、".join(names) or gettext("指标明细")
    return title + (
        gettext(" · 按{dimensions}", dimensions="、".join(_column_label(d, k) for k in keys)) if keys else ""
    )


def _brief_dataset(d: dict, view: dict) -> dict:
    return {**d, "meta": {**_meta(d), "export_labels": _export_labels(d, view)}}


def _formatted(value: Any, meta: dict | None = None) -> str:
    if value is None:
        return "—"
    n = number(value)
    unit = _unit(meta)
    return _localized_number(display(n, unit), unit["unit"]) if n is not None else str(value)


def _table_value(value: Any, meta: dict | None = None) -> str:
    return _formatted(value, meta)


def _core_scope_matches(d: dict, scope: dict) -> bool:
    """只排除已知范围冲突；旧报告或数据集缺少范围信息时沿用原有选择规则。"""
    from avelis.analysis.grounding import scope_key
    from avelis.ask.intent import comparison_range
    from avelis.governance.errors import Invalid

    meta = _meta(d)
    intent, saved = meta.get("intent") or {}, meta.get("window") or {}
    if scope.get("filters") is not None and intent.get("filters") is not None:
        if scope_key("", scope["filters"], {}) != scope_key("", intent["filters"], {}):
            return False
    window = {
        "start_date": intent.get("start_date") or saved.get("start"),
        "end_date": intent.get("end_date") or saved.get("end"),
        "compare_start": intent.get("compare_start"),
        "compare_end": intent.get("compare_end"),
    }
    if (
        intent.get("compare") in ("previous_period", "previous_calendar", "previous_year")
        and window["start_date"]
        and window["end_date"]
    ):
        try:
            start, end = comparison_range(
                date.fromisoformat(str(window["start_date"])),
                date.fromisoformat(str(window["end_date"])),
                intent["compare"],
            )
        except (ValueError, Invalid):
            pass
        else:
            window.update(compare_start=start, compare_end=end)
    return all(not scope.get(key) or not value or str(scope[key]) == str(value) for key, value in window.items())


def _core_metrics(view: dict) -> list[dict]:
    refs = _reference_counts(view.get("report") or {})
    for d in view.get("datasets") or []:
        intent, units = _meta(d).get("intent") or {}, _units(d)
        if (
            d.get("restricted")
            or d.get("kind") != "governed"
            or not refs[d["ref"]]
            or len(d.get("rows") or []) != 1
            or intent.get("group_by")
            or intent.get("time_grain")
            or d.get("time_column")
            or not _core_scope_matches(d, (view.get("report") or {}).get("scope") or {})
        ):
            continue
        cols = d.get("columns") or []
        # 受治理结果的列序契约：每个指标依次为本期、对比期、差额、变化率（ask.service）。
        if len(cols) % 4:
            continue
        codes = _meta(d).get("metric_codes") or []
        code = (view.get("report") or {}).get("metric_code")
        if code and codes and code not in codes:
            continue
        offsets = [codes.index(code) * 4] if code in codes else range(0, len(cols), 4)
        for start in offsets:
            group = cols[start : start + 4]
            if len(group) != 4:
                continue
            a, b, delta, rate = [_unit(units.get(c)) for c in group]
            if (
                a["kind"] not in ("level", "ratio", "count")
                or a != b
                or delta["kind"] != "delta"
                or rate["kind"] != "rate_change"
            ):
                continue
            values = d["rows"][0][start : start + 4]
            if any(number(v) is None for v in values[:3]):
                continue
            return [
                {
                    "label": gettext(label),
                    "text": _formatted(v, units[c]),
                    "raw": number(v),
                    "measure": units[c]["kind"],
                    "ref": d["ref"],
                }
                for label, c, v in zip(("本期", "对比期", "变化", "变化率"), group, values)
                if number(v) is not None
            ]
    return []


def _header(view: dict) -> str:
    w = view.get("window") or {}
    if not w:
        from avelis.ask.intent import comparison_range
        from avelis.governance.errors import Invalid

        counts = _reference_counts(view.get("report") or {})
        metrics = _core_metrics(view)
        core_ref = metrics[0]["ref"] if metrics else None
        candidates = sorted(
            (
                d
                for d in view.get("datasets") or []
                if not d.get("restricted") and d.get("kind") == "governed" and counts[d["ref"]]
            ),
            key=lambda d: (d["ref"] != core_ref, -counts[d["ref"]]),
        )
        for d in candidates:
            meta = _meta(d)
            saved, intent = meta.get("window") or {}, meta.get("intent") or {}
            if not saved.get("start") or not saved.get("end"):
                continue
            w = {"start": saved["start"], "end": saved["end"]}
            if intent.get("compare") == "custom" and intent.get("compare_start") and intent.get("compare_end"):
                w.update(compare_start=intent["compare_start"], compare_end=intent["compare_end"])
            elif intent.get("compare") in ("previous_period", "previous_calendar", "previous_year"):
                try:
                    start, end = comparison_range(
                        date.fromisoformat(str(w["start"])), date.fromisoformat(str(w["end"])), intent["compare"]
                    )
                except (ValueError, Invalid):
                    pass
                else:
                    w.update(compare_start=str(start), compare_end=str(end))
            break
    parts = []

    def span(start, end):
        return f"{start} ~ {str(end)[5:] if str(start)[:4] == str(end)[:4] else end}"

    if w.get("start") and w.get("end"):
        parts.append(gettext("本期") + " " + span(w["start"], w["end"]))
    if w.get("compare_start") and w.get("compare_end"):
        parts.append(gettext("对比") + " " + span(w["compare_start"], w["compare_end"]))
    tz = ZoneInfo(os.environ.get("AVELIS_BUSINESS_TIMEZONE", "Asia/Shanghai"))
    today = datetime.now(tz)
    parts.append(gettext("生成日期 {year}年{month}月{day}日", year=today.year, month=today.month, day=today.day))
    return " · ".join(parts)


def _cause_label(cause: dict) -> str:
    entity, label = cause.get("entity") or {}, cause.get("label") or ""
    kind = cause.get("type")
    if kind == "calendar":
        return (
            gettext("日历因素（天数）")
            if "天数" in label or any("天数" in str(v) for v in entity.values())
            else gettext("日历因素")
        )
    if kind == "governance":
        return gettext("治理事件")
    if kind == "member":
        member = entity.get("member")
        if member is not None:
            return (
                gettext("{dimension}：{member}", dimension=entity["dimension_label"], member=member)
                if entity.get("dimension_label")
                else str(member)
            )
        return label or gettext("未标注实体")
    if kind == "factor":
        return label or gettext("因子指标")
    return label or entity.get("member") or gettext("未标注实体")


def _cause_value(cause: dict, datasets: dict[str, dict] | None = None) -> tuple[Decimal | None, str]:
    record = cause.get("contribution") or {}
    seg = next((s for s in record.get("segments") or [] if s.get("kind") == "value"), {})
    raw = number(seg.get("raw"))
    if raw is not None and datasets is not None:
        d = datasets.get(seg.get("ref")) or {}
        path = str(seg.get("path") or "").removeprefix(str(seg.get("ref")) + ".")
        units = _units(d)
        column = next((c for c in sorted(units, key=len, reverse=True) if path == c or path.startswith(c + "[")), None)
        if column is not None:
            meta = units[column]
        elif path.startswith("summary."):
            units = _meta(d).get("summary_units") or d.get("summary_units") or {}
            meta = units.get(path.removeprefix("summary."))
        else:
            meta = None
        raw *= _unit(meta)["scale"]
    return raw, _rendered(record)


def _cause_text(cause: dict) -> str:
    raw, text = _cause_value(cause)
    direction = gettext({"up": "上升", "down": "下降"}.get(cause.get("direction"), ""))
    if direction and raw is not None:
        text = text.lstrip("+-")
    return _cause_label(cause) + (f" · {direction} {text}".rstrip() if text else f" · {direction}" if direction else "")


def _table_data(d: dict, *, brief: bool = False) -> list[list[str]]:
    if d.get("restricted"):
        return []
    cols, units = (d.get("columns") or [])[:6], _units(d)
    headers = [_column_label(d, c) for c in cols] if brief else cols

    def cell(c, v):
        if brief and d.get("kind") == "calc" and c == "period":
            v = gettext(CALC_PERIOD_LABELS[v]) if v in CALC_PERIOD_LABELS else v
        if c not in units:
            return "—" if v is None else str(v)
        text = _table_value(v, units[c])
        return _brief_text(text) if brief else text

    return [headers] + [[cell(c, v) for c, v in zip(cols, row[:6])] for row in d["rows"][:10]]


def _row_note(d: dict) -> str:
    total = d.get("row_count", len(d.get("rows") or []))
    return gettext("共 {total} 行，显示前 {shown} 行", total=total, shown=min(10, len(d.get("rows") or []))) + (
        " · " + gettext("结果被截断") if d.get("truncated") else ""
    )


def _context_note(d: dict) -> str:
    meta = _meta(d)
    saved = meta.get("conclusion") or meta.get("summary") or d.get("summary")
    if isinstance(saved, str):
        return saved
    cols, rows = d.get("columns") or [], d.get("rows") or []
    if "collection_status" in cols and "drift" in cols and rows:
        status, drift = cols.index("collection_status"), cols.index("drift")
        collected = all(r[status] == "succeeded" for r in rows)
        stable = all(r[drift] == "否" for r in rows)
        return gettext("已保存的采集记录均成功" if collected else "已保存的采集记录含失败或未知状态") + gettext(
            "，无口径漂移" if stable else "，存在口径漂移或未知状态"
        )
    if cols == ["type", "at", "object", "item", "change", "impacts", "note"]:
        return (
            gettext("已保存范围内记录 {count} 项变更", count=len(rows)) if rows else gettext("已保存范围内未发现变更")
        )
    return gettext("已保存 {count} 条检查记录，未保存汇总结论", count=d.get("row_count", len(rows)))


def _source_rows(report: dict) -> list[list[str]]:
    return [
        [
            where,
            str(seg.get("text") or ""),
            str(seg.get("ref") or "—"),
            re.sub(r"^D[1-9]\d*\.", "", str(seg.get("path") or seg.get("label") or "—")),
            " ~ ".join(map(str, seg["raw"]))
            if isinstance(seg.get("raw"), list)
            else str(seg["raw"])
            if seg.get("raw") is not None
            else "—",
        ]
        for where, _, seg in _segments(report)
    ]


def _source_marks(seg: dict) -> list[str]:
    return [
        gettext(mark)
        for mark, on in (
            (EXPLORE_MARK, seg.get("explore")),
            ("建议目标（非观测值）", seg.get("role") == "proposal"),
            ("假设计算", seg.get("assumption")),
        )
        if on
    ]


def _bad_fields(verification: dict) -> list[str]:
    return [
        f"{gettext(FIELD_LABEL[f['field']]) if f['field'] in FIELD_LABEL else f['field']}："
        f"{'；'.join(f.get('problems') or [])}"
        for f in verification.get("fields", [])
        if f.get("status") == "unverified"
    ]


def _claims(view: dict) -> list[tuple[dict, dict]]:
    claims = (view.get("report") or {}).get("claims", [])
    checks = {c["index"]: c for c in (view.get("verification") or {}).get("claims", [])}
    return [(c, checks.get(i, {})) for i, c in enumerate(claims)]


def _context_claim(claim: dict, view: dict) -> bool:
    refs = set(claim.get("refs") or [])
    refs.update(s.get("ref") for s in claim.get("segments") or [])
    for detail in (claim.get("details") or {}).values():
        refs.update(s.get("ref") for s in detail.get("segments") or [])
    refs.discard(None)
    refs.discard("window")
    contexts = {d["ref"] for d in view.get("datasets") or [] if d.get("kind") == "context"}
    return bool(refs) and refs <= contexts


def _claim_errors(claim: dict, check: dict) -> list[str]:
    errors = list(check.get("problems") or []) if check.get("status") == "unverified" else []
    errors += claim.get("errors") or []
    for key, label in (("premise", "前提"), ("validation", "验证方式")):
        errors += [gettext(label) + "：" + e for e in ((claim.get("details") or {}).get(key) or {}).get("errors") or []]
    return list(dict.fromkeys(errors))


def _data_check_note(view: dict) -> str:
    notes = dict.fromkeys(
        [
            _verdict(view.get("verification") or {}),
            *(
                _context_note(d).rstrip("。")
                for d in view.get("datasets") or []
                if d.get("kind") == "context" and not d.get("restricted")
            ),
        ]
    )
    return "；".join(notes) + "。"


def _appendix_datasets(view: dict, selected: list[dict]) -> list[dict]:
    """附录只列正文实际使用的数据，不展开计算输入或仅用于分析方法的引用。"""
    report = view.get("report") or {}
    body = {
        **report,
        "fields": {k: v for k, v in (report.get("fields") or {}).items() if k != "method"},
        "claims": [
            {**c, "details": {k: v for k, v in (c.get("details") or {}).items() if k in ("premise", "validation")}}
            for c in report.get("claims") or []
            if c.get("type") in TYPE_LABEL and not _context_claim(c, view)
        ],
    }
    refs = set(_reference_counts(body))
    refs.update(d["ref"] for d in selected)
    refs.update(m["ref"] for m in _core_metrics(view))
    return [
        d if d.get("restricted") else _brief_dataset(d, view)
        for d in view.get("datasets") or []
        if d.get("restricted") or (d["ref"] in refs and d.get("kind") in DATA_SOURCE_LABEL)
    ]


def _dataset_listing(datasets: list[dict]) -> list[list[str]]:
    return [[gettext(label) for label in ("编号", "名称", "来源", "行数", "是否截断")]] + [
        [d["ref"], gettext(RESTRICTED_NOTE), "—", "—", "—"]
        if d.get("restricted")
        else [
            d["ref"],
            _dataset_title(d),
            gettext(DATA_SOURCE_LABEL[d["kind"]]),
            str(d.get("row_count", len(d.get("rows") or []))),
            gettext("是" if d.get("truncated") else "否"),
        ]
        for d in datasets
    ]


def _markdown_table(rows: list[list[str]]) -> list[str]:
    def cell(value: str) -> str:
        return str(value).replace("|", "\\|").replace("\n", "<br/>")

    return [
        "| " + " | ".join(map(cell, rows[0])) + " |",
        "|" + "---|" * len(rows[0]),
        *["| " + " | ".join(map(cell, row)) + " |" for row in rows[1:]],
    ]


def _markdown_claim(claim: dict, check: dict, *, brief: bool = False) -> list[str]:
    text = claim.get("text") or _rendered(claim)
    if not brief and (bad := _claim_errors(claim, check)):
        text += " ⚠ " + "；".join(bad)
    if claim.get("type") in ("inference", "recommendation"):
        text += " <small>" + gettext("待验证" if brief else "推断，未核实") + "</small>"
    refs = _refs(claim).replace("、", "、\u00a0")
    lines = [f"- {text}" + ("" if brief else f'\u00a0<sup style="color: {GRAY}; white-space: nowrap">{refs}</sup>')]
    for key, label in (
        ("premise", "依据" if brief else "前提"),
        ("validation", "建议这样验证" if brief else "验证方式"),
    ):
        if detail := _detail(claim, key):
            lines.append("  - " + gettext("{label}：{detail}", label=gettext(label), detail=detail))
    return lines


def to_markdown(
    view: dict, *, audience: Audience | None = None, appendix: bool | None = None, datasets: list[str] | None = None
) -> str:
    view = _export_view(view)
    figures = report_figures(view, datasets)
    brief = resolve_audience(audience, appendix) == "brief"
    notes = _brief_footer(view, datasets) if brief else []
    if brief:
        view = _brief_view(view)
    report, verification = view.get("report") or {}, view.get("verification") or {}
    lines = [f"# {_field(report, 'title') or gettext('分析报告')}", "", _header(view), ""]
    metrics = _core_metrics(view)
    if metrics:
        lines += [
            "## " + gettext("核心指标"),
            "",
            *_markdown_table([[m["label"] for m in metrics], [m["text"] for m in metrics]]),
            "",
        ]
    lines += ["## " + gettext("结论"), "", "> " + (_field(report, "summary") or gettext("（无）")), ""]
    if not brief:
        lines += ["- ⚠ " + gettext("未通过（{error}）", error=x) for x in _bad_fields(verification)]
        lines += [
            "- ⚠ " + gettext("结论未通过：{error}", error=x)
            for x in (verification.get("conclusion") or {}).get("problems") or []
        ]
    if report.get("causes"):
        code = report.get("conclusion")
        tag = gettext(CONCLUSION[code]) if code in CONCLUSION else code or ""
        lines += ["", "## " + gettext("主要原因") + (f" · {tag}" if tag else ""), ""]
        for i, c in enumerate(report["causes"], 1):
            refs = _refs({"refs": c.get("evidence")})
            bad = "；".join(c.get("errors") or []) if not brief else ""
            lines.append(
                f"{i}. {_cause_text(c)}" + (f" ⚠ {bad}" if bad else "") + ("" if brief else f" <sup>{refs}</sup>")
            )
    for title, types in (("关键发现", ("fact", "calc")), ("判断与建议", ("inference", "recommendation"))):
        claims = [(c, check) for c, check in _claims(view) if c.get("type") in types and not _context_claim(c, view)]
        if not claims:
            continue
        lines += ["", "## " + gettext(title), ""]
        for c, check in claims:
            lines += _markdown_claim(c, check, brief=brief)
    if unexplained := _field(report, "unexplained"):
        lines += ["", "## " + gettext("还没查清"), ""]
        lines += ["> - " + item.strip() for item in unexplained.split("；") if item.strip()]
    selected = _figure_datasets(view, figures)
    if selected:
        lines += ["", "## " + gettext("关键数据"), ""]
        for d, figure in zip(selected, figures, strict=True):
            d = _brief_dataset(d, view) if brief else d
            figure = _display_figure(figure, d, brief=brief)
            title = _dataset_title(d) if brief else f"{d['ref']} {d['title']}"
            lines += [
                f"### {title}",
                "",
                *_mermaid_chart(figure),
                *[note for text in _chart_annotation_notes(figure["chart"]) for note in (text, "")],
                *([category_omission_note(figure["chart"]), ""] if category_omission_note(figure["chart"]) else []),
                *_markdown_table(_table_data(d, brief=brief)),
                "",
                _row_note(d),
                "",
            ]
    if brief:
        lines += ["", *[f'<small style="color: {GRAY}">{note}</small>' for note in notes], ""]
    if not brief:
        used = _appendix_datasets(view, selected)
        lines += ["", "## " + gettext("附录"), ""]
        if method := _field(report, "method"):
            lines += [f"**{gettext('分析方法')}**：{method}", ""]
        lines += ["### " + gettext("数据检查"), "", _data_check_note(view), "", "### " + gettext("用到的数据"), ""]
        lines += _markdown_table(_dataset_listing(used))
        lines += ["", "### " + gettext("查询明细"), ""]
        for d in used:
            if d.get("restricted") or d.get("kind") not in ("governed", "explore"):
                continue
            versions = d.get("metric_version_ids") or _meta(d).get("metric_version_ids") or []
            lines += [
                f"#### {d['ref']} {_dataset_title(d)}",
                "",
                gettext("口径版本：{versions}", versions="、".join(versions) or gettext("未登记")),
                "",
                "```sql",
                d.get("sql") or _meta(d).get("sql") or gettext("（未保存 SQL）"),
                "```",
                "",
            ]
    return "\n".join(lines)


FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r", "\n")
_PLAIN_NUMBER = re.compile(r"^[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$")


def _cell(value: Any) -> Any:
    """防公式注入：以 = + - @ 或制表符、回车开头的文本前加单引号，按文本显示；真实数值（含负数）照常导出。"""
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(FORMULA_PREFIX) and not _PLAIN_NUMBER.match(value):
        return "'" + value
    return value


def to_csv(columns: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([_cell(str(c)) for c in columns])
    writer.writerows([[_cell(v) for v in r] for r in rows])
    return ("﻿" + buf.getvalue()).encode("utf-8")  # Excel 识别 UTF-8


SOURCE_COLUMNS = [
    "位置",
    "类型",
    "显示值",
    "来源标签",
    "数据集",
    "取值路径",
    "量纲",
    "单位",
    "小数位",
    "方向",
    "标记",
    "原值",
]


def sources_csv(view: dict) -> bytes:
    """引用来源表：报告里每个引用段来自哪个数据集的哪个值（ADR 0009）。"""
    view = _export_view(view)
    rows = []
    for where, kind, s in _segments(view.get("report") or {}):
        raw = s.get("raw")
        marks = _source_marks(s)
        rows.append(
            [
                where,
                kind,
                s.get("text"),
                s.get("label"),
                s.get("ref"),
                s.get("path"),
                gettext(MEASURE_LABEL[s["measure"]]) if s.get("measure") in MEASURE_LABEL else s.get("measure") or "",
                s.get("unit") or "",
                "" if s.get("precision") is None else s.get("precision"),
                gettext({"up": "上升", "down": "下降"}.get(s.get("direction"), "")),
                "；".join(marks),
                " ~ ".join(raw) if isinstance(raw, list) else raw,
            ]
        )
    return to_csv([gettext(label) for label in SOURCE_COLUMNS], rows)


FONT_CANDIDATES = [
    ("C:/Windows/Fonts/Deng.ttf", 0),
    ("C:/Windows/Fonts/msyh.ttc", 0),
    ("C:/Windows/Fonts/simhei.ttf", 0),
    ("C:/Windows/Fonts/simsun.ttc", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
    ("/usr/share/fonts/wqy-microhei/wqy-microhei.ttc", 0),
    ("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf", 0),
]
_FONT: dict[str, Any] = {}


def pdf_font() -> tuple[str, bool]:
    """返回（字体名，是否嵌入）。"""
    if "name" in _FONT:
        return _FONT["name"], _FONT["embedded"]
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont

    custom = os.environ.get("AVELIS_PDF_FONT")
    for path, index in ([(custom, 0)] if custom else []) + FONT_CANDIDATES:
        if path and os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont("AvelisCJK", path, subfontIndex=index))
            except Exception:  # noqa: BLE001 - 字体格式不受支持（如 CFF 轮廓），试下一个
                continue
            _FONT.update(name="AvelisCJK", embedded=True)
            bold = os.path.join(os.path.dirname(path), "Dengb.ttf")
            if os.path.exists(bold):
                pdfmetrics.registerFont(TTFont("AvelisCJKBold", bold))
            pdfmetrics.registerFontFamily(
                "AvelisCJK", normal="AvelisCJK", bold="AvelisCJKBold" if os.path.exists(bold) else "AvelisCJK"
            )
            return "AvelisCJK", True
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    pdfmetrics.registerFontFamily("STSong-Light", normal="STSong-Light", bold="STSong-Light")
    _FONT.update(name="STSong-Light", embedded=False)
    return "STSong-Light", False


BLUE, NEGATIVE, POSITIVE, GRAY, LINE = "#2F6FEB", "#D92D20", "#12B76A", "#667085", "#EAECF0"
DARK = "#101828"


def _ellipsis(text: str, font: str, size: float, width: float) -> str:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    if stringWidth(text, font, size) <= width:
        return text
    while text and stringWidth(text + "…", font, size) > width:
        text = text[:-1]
    return text + "…"


def _axis(value: float) -> str:
    return _localized_number(compact(number(value), strip=True))


def _nice_ticks(lo: float, hi: float, count: int = 4) -> list[float]:
    """覆盖 [lo, hi] 的整齐刻度（1、2、2.5、5 乘 10 的幂），包含 0。"""
    import math

    raw = (hi - lo) / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    start, end = math.floor(lo / step), math.ceil(hi / step)
    return [i * step for i in range(start, end + 1)]


def _axis_label(value: float, unit: dict | None) -> str:
    """坐标轴刻度按量纲换算；比率之差复用数据标注的换算与百分点文案。"""
    meta = _unit(unit)
    if meta["kind"] == "delta" and meta["of_ratio"] and not meta["unit"]:
        return _localized_number(display(number(value), meta)).removeprefix("+")
    if meta["kind"] in ("rate_change", "share") or (meta["kind"] == "ratio" and not meta["unit"]):
        return _localized_number(compact(number(value) * 100, 1, strip=True)) + "%"
    return _localized_number(compact(number(value) * (meta["scale"] if meta["kind"] != "ratio" else 1), 2, strip=True))


def _bar_chart(
    data: list[tuple[str, float, str]], font: str, width: float = 468, *, references: list[str] | None = None
):
    from reportlab.graphics.shapes import Drawing, Line, Rect, String
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase.pdfmetrics import stringWidth

    if not data:
        return None
    height = max(80, len(data) * 14 + 26)
    drawing = Drawing(width, height)
    references = references or [""] * len(data)
    tail = max(
        stringWidth(text, font, 7.5) + stringWidth(" · " + ref, font, 6.5) if ref else stringWidth(text, font, 7.5)
        for (_, _, text), ref in zip(data, references)
    )
    left, right = 118, width - max(76, tail + 8)
    lo, hi = min(0, min(v for _, v, _ in data)), max(0, max(v for _, v, _ in data))
    span = hi - lo or 1

    def x(value):
        return left + (value - lo) / span * (right - left)

    drawing.add(Line(x(0), 20, x(0), height - 3, strokeColor=HexColor(LINE), strokeWidth=0.5))
    for tick in sorted({lo, 0, hi}):
        drawing.add(
            String(x(tick), 5, _axis(tick), fontName=font, fontSize=7, textAnchor="middle", fillColor=HexColor(GRAY))
        )
    for i, (label, value, text) in enumerate(data):
        y = height - 14 - i * 14
        color = HexColor(NEGATIVE if value < 0 else POSITIVE)
        drawing.add(
            String(0, y, _ellipsis(label, font, 8, left - 8), fontName=font, fontSize=8, fillColor=HexColor(GRAY))
        )
        drawing.add(Rect(min(x(0), x(value)), y - 1, abs(x(value) - x(0)), 8, strokeColor=None, fillColor=color))
        # 数字放在条形的末端；零轴两侧留足位置，不与类目标签重叠。
        drawing.add(String(right + 6, y, text, fontName=font, fontSize=7.5, fillColor=color))
        if ref := references[i]:
            drawing.add(
                String(
                    right + 6 + stringWidth(text, font, 7.5),
                    y,
                    " · " + ref,
                    fontName=font,
                    fontSize=6.5,
                    fillColor=HexColor(GRAY),
                )
            )
    return drawing


# 浅色主题图表令牌；测试核对 tokens.json，运行时不依赖前端文件。
VIZ_PALETTE = ("#1e9df1", "#00b87a", "#f7b928", "#e0245e", "#8b5cf6", "#14b8c4", "#f97316", "#64748b")
VIZ_NEUTRAL = "#a3adb6"
VIZ_DIM = "#dfe5ea"


def _chart_marks(spec: dict, series_index: int) -> list[dict]:
    """按规格中的系列索引定位；旧的无索引标注只按列名匹配。"""
    column = spec["series"][series_index]["column"]
    return [
        mark
        for mark in spec.get("marks") or []
        if mark.get("series_index") == series_index
        or (mark.get("series_index") is None and mark.get("series") == column)
    ]


def _chart_panel(spec: dict, series_index: int) -> dict:
    series = spec["series"][series_index]
    return {
        **spec,
        "kind": series["mark"],
        "series": [series],
        "title": None,
        "marks": [{**mark, "series_index": 0} for mark in _chart_marks(spec, series_index)] or None,
    }


def _chart_annotation_notes(spec: dict, *, include_marks: bool = True) -> list[str]:
    notes = []
    for index, series in enumerate(spec["series"]):
        for mark in _chart_marks(spec, index):
            value = _formatted(mark["value"], series["unit"])
            if mark["type"] == "average":
                notes.append(gettext("所示值平均：{series} · {value}", series=series["name"], value=value))
            elif include_marks:
                label = gettext("最高点" if mark["type"] == "max" else "最低点")
                notes.append(
                    gettext(
                        "{label}：{series} · {category} · {value}",
                        label=label,
                        series=series["name"],
                        category=mark["x"],
                        value=value,
                    )
                )
    if any(mark["type"] == "average" for mark in spec.get("marks") or []):
        notes.append(gettext("平均标注表示各系列所示非空值的算术平均，不是加权平均。"))
    if spec.get("highlight"):
        notes.append(gettext("突出显示：{categories}；其余类目已弱化。", categories="、".join(spec["highlight"])))
    return notes


def _spec_chart(spec: dict, font: str, *, title: str | None = None, width: float = 468):
    """PDF 与 HTML 共用的图形和图下注释；提示按宽度折行，不裁掉信息。"""
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase.pdfmetrics import stringWidth

    drawing = _spec_chart_data(spec, font, title=title, width=width)
    notes = _chart_annotation_notes(spec, include_marks=spec["kind"] == "donut")
    if note := category_omission_note(spec):
        notes.append(note)
    if not notes:
        return drawing
    lines = []
    for note in notes:
        line = ""
        for character in note:
            if line and stringWidth(line + character, font, 8) > width:
                lines.append(line)
                line = ""
            line += character
        lines.append(line)
    footer = len(lines) * 12 + 8
    result = Drawing(width, drawing.height + footer)
    drawing.translate(0, footer)
    result.add(drawing)
    for index, line in enumerate(lines):
        result.add(String(0, footer - 14 - index * 12, line, fontName=font, fontSize=8, fillColor=HexColor(GRAY)))
    return result


def _spec_chart_data(spec: dict, font: str, *, title: str | None = None, width: float = 468):
    """只渲染已物化规格，不排序、不聚合、不另选数据。"""
    import math

    from reportlab.graphics.shapes import Circle, Drawing, Line, Polygon, Rect, String
    from reportlab.lib.colors import HexColor

    kind, series = spec["kind"], spec["series"]
    title = title if title is not None else spec.get("title")
    header = 22 if title else 0
    if kind == "multiples":
        panels = [
            _spec_chart_data(_chart_panel(spec, index), font, title=s["name"], width=width)
            for index, s in enumerate(series)
        ]
        height = sum(panel.height for panel in panels) + header
        drawing = Drawing(width, height)
        offset = height - header
        for panel in panels:
            offset -= panel.height
            panel.translate(0, offset)
            drawing.add(panel)
    else:
        horizontal = spec["orientation"] == "horizontal" and kind not in (
            "line",
            "area",
            "combo",
            "pareto",
            "waterfall",
            "heatmap",
            "scatter",
        )
        height = (max(100, len(spec["categories"]) * 19 + 60) if horizontal and kind != "donut" else 190) + header
        if kind == "heatmap":
            height = max(190, len(spec["heatmap"]["y_categories"]) * 22 + 75) + header
        if kind == "kpi":
            height = max(45, len(spec["kpis"]) * 22 + 10) + header
        drawing = Drawing(width, height)

    def text(x, y, value, *, color=GRAY, size=7, anchor="start", limit=None):
        value = str(value)
        if limit:
            value = _ellipsis(value, font, size, limit)
        drawing.add(String(x, y, value, fontName=font, fontSize=size, fillColor=HexColor(color), textAnchor=anchor))

    if title:
        text(0, height - 14, title, color=DARK, size=10, limit=width)
    if kind == "multiples":
        return drawing
    if kind == "kpi":
        for i, kpi in enumerate(spec["kpis"]):
            text(
                0,
                height - header - 20 - i * 22,
                f"{kpi['label']}  {_formatted(kpi['value'], kpi['unit'])}",
                size=10,
                color=DARK,
            )
        return drawing
    categories = spec["categories"]
    if not categories or not series:
        return drawing
    colors = [VIZ_NEUTRAL if s.get("other") else VIZ_PALETTE[i % len(VIZ_PALETTE)] for i, s in enumerate(series)]
    highlight = set(spec.get("highlight") or [])

    def category_color(category, color):
        return color if not highlight or category_label(category) in highlight else VIZ_DIM

    if kind == "heatmap":
        heatmap = spec["heatmap"]
        members = heatmap["y_categories"]
        if not members:
            return drawing
        left, right, bottom, top = 105, width - 10, 45, height - header - 25
        cell_width, cell_height = (right - left) / len(categories), (top - bottom) / len(members)
        low, high = heatmap["min"], heatmap["max"]
        for x_index, y_index, value in heatmap["cells"]:
            if not (0 <= x_index < len(categories) and 0 <= y_index < len(members)):
                continue
            color = VIZ_DIM if value is None else VIZ_PALETTE[0]
            cell = Rect(
                left + x_index * cell_width,
                top - (y_index + 1) * cell_height,
                cell_width,
                cell_height,
                fillColor=HexColor(color),
                strokeColor=HexColor("#ffffff"),
                strokeWidth=0.6,
            )
            cell.fillOpacity = 1 if value is None else 0.2 + 0.8 * ((value - low) / (high - low) if high != low else 1)
            drawing.add(cell)
            if len(categories) <= 8:
                text(
                    left + (x_index + 0.5) * cell_width,
                    top - (y_index + 0.5) * cell_height - 2,
                    _formatted(value, series[0]["unit"]),
                    anchor="middle",
                    color=DARK,
                    size=6,
                )
        for index, member in enumerate(members):
            text(left - 7, top - (index + 0.5) * cell_height - 2, member, anchor="end", limit=left - 10)
        for index, category in enumerate(categories):
            if index % max(1, math.ceil(len(categories) / 8)) == 0:
                text(left + (index + 0.5) * cell_width, bottom - 13, category, anchor="middle", limit=55)
        text(left, 15, series[0]["name"], color=VIZ_PALETTE[0], limit=width - left)
        return drawing
    if kind == "scatter":
        points = [point for point in spec["points"] if point["x"] is not None and point["y"] is not None]
        if not points:
            return drawing
        left, right, bottom, top = 64, width - 25, 45, height - header - 25
        axes = []
        for column in ("x", "y"):
            low, high = min(point[column] for point in points), max(point[column] for point in points)
            if low == high:
                low, high = min(0, low), max(0, high) or 1
                if low == high:
                    high = low + 1
            ticks = _nice_ticks(low, high)
            axes.append((ticks[0], ticks[-1], ticks))

        def position(value, axis):
            low, high, _ = axes[axis]
            start, end = (left, right) if axis == 0 else (bottom, top)
            return start + (value - low) / (high - low) * (end - start)

        for axis, (_, _, ticks) in enumerate(axes):
            for tick in ticks:
                pos = position(tick, axis)
                if axis == 0:
                    drawing.add(Line(pos, bottom, pos, top, strokeColor=HexColor(LINE), strokeWidth=0.4))
                    text(pos, bottom - 13, _axis_label(tick, series[0]["unit"]), anchor="middle")
                else:
                    drawing.add(Line(left, pos, right, pos, strokeColor=HexColor(LINE), strokeWidth=0.4))
                    text(left - 5, pos - 2, _axis_label(tick, series[1]["unit"]), anchor="end")
        # 与页面一致：气泡面积按大小度量的绝对值，负值同样按量级画
        max_size = max([0, *(abs(point.get("size") or 0) for point in points)])
        for point in points:
            x, y = position(point["x"], 0), position(point["y"], 1)
            radius = 3 + (7 * math.sqrt(abs(point.get("size") or 0) / max_size) if max_size else 0)
            drawing.add(
                Circle(x, y, radius, fillColor=HexColor(category_color(point["name"], colors[0])), strokeColor=None)
            )
            if len(points) <= 12:
                text(x, y + radius + 3, point["name"], anchor="middle", size=6, limit=65)
        text(left, 15, series[0]["name"], color=colors[0], limit=(right - left) / 2)
        text(right, top + 7, series[1]["name"], anchor="end", color=colors[1], limit=(right - left) / 2)
        return drawing
    if kind == "waterfall":
        endpoints = spec["waterfall"]
        categories = [endpoints["start"]["label"], *categories, endpoints["end"]["label"]]
        levels = [endpoints["start"]["value"]]
        for value in series[0]["values"]:
            levels.append(levels[-1] + (value or 0))
        values = [endpoints["start"]["value"], *series[0]["values"], endpoints["end"]["value"]]
        low, high = min([0, *levels, values[-1]]), max([0, *levels, values[-1]])
        ticks = _nice_ticks(low, high if high != low else low + 1)
        left, right, bottom, top = 64, width - 25, 45, height - header - 25
        band = (right - left) / len(categories)

        def level_position(value):
            return bottom + (value - ticks[0]) / (ticks[-1] - ticks[0]) * (top - bottom)

        level_unit = {**series[0]["unit"], "kind": "level"}
        for tick in ticks:
            y = level_position(tick)
            drawing.add(Line(left, y, right, y, strokeColor=HexColor(LINE), strokeWidth=0.4))
            text(left - 5, y - 2, _axis_label(tick, level_unit), anchor="end")
        for index, (category, value) in enumerate(zip(categories, values, strict=True)):
            value = value or 0
            endpoint = index in (0, len(categories) - 1)
            start = 0 if endpoint else levels[index - 1]
            end = value if endpoint else levels[index]
            x, a, b = left + (index + 0.15) * band, level_position(start), level_position(end)
            good = (value >= 0) != (series[0].get("polarity") == "down_good")
            color = colors[0] if endpoint else POSITIVE if good else NEGATIVE
            drawing.add(Rect(x, min(a, b), band * 0.7, abs(b - a), fillColor=HexColor(color), strokeColor=None))
            text(
                x + band * 0.35,
                max(a, b) + 4,
                _formatted(value, level_unit if endpoint else series[0]["unit"]),
                anchor="middle",
                size=6,
            )
            if index < len(categories) - 1:
                drawing.add(Line(x + band * 0.7, b, x + band, b, strokeColor=HexColor(GRAY), strokeWidth=0.5))
            text(x + band * 0.35, bottom - 13, category, anchor="middle", limit=max(20, band - 3))
        text(left, 15, series[0]["name"], color=colors[0], limit=width - left)
        return drawing
    if kind == "pareto":
        series = [
            series[0],
            {
                **series[0],
                "name": gettext("累计占比"),
                "mark": "line",
                "axis": 1,
                "values": spec["cumulative"],
                "unit": {"kind": "share"},
            },
        ]
        spec = {**spec, "series": series}
        colors = [*colors, VIZ_PALETTE[1]]
    if kind == "donut":
        values = series[0]["values"]
        total = sum(v or 0 for v in values)
        angle, cx, cy, radius = 90, 90, (height - header) / 2, 67
        for i, (category, value) in enumerate(zip(categories, values, strict=True)):
            color = (
                VIZ_NEUTRAL if spec.get("others") and i == len(categories) - 1 else VIZ_PALETTE[i % len(VIZ_PALETTE)]
            )
            color = category_color(category, color)
            if value is not None and value > 0 and total > 0:
                end = angle + 360 * value / total
                steps = max(2, math.ceil((end - angle) / 4))
                points = [cx, cy]
                for step in range(steps + 1):
                    radians = math.radians(angle + (end - angle) * step / steps)
                    points.extend([cx + radius * math.cos(radians), cy + radius * math.sin(radians)])
                drawing.add(Polygon(points, fillColor=HexColor(color), strokeColor=None))
                if any(mark["type"] in ("max", "min") and mark["x"] == category for mark in _chart_marks(spec, 0)):
                    radians = math.radians((angle + end) / 2)
                    drawing.add(
                        Circle(
                            cx + radius * 0.8 * math.cos(radians),
                            cy + radius * 0.8 * math.sin(radians),
                            4,
                            fillColor=HexColor("#ffffff"),
                            strokeColor=HexColor(DARK),
                            strokeWidth=1.5,
                        )
                    )
                angle = end
            text(
                180,
                height - header - 22 - i * 19,
                f"{category}  {_formatted(value, series[0]['unit'])}",
                color=color,
                limit=width - 180,
            )
        drawing.add(Circle(cx, cy, radius * 0.57, fillColor=HexColor("#ffffff"), strokeColor=None))
        return drawing
    # 所有值保持规格的原始单位；刻度与标注统一交给 grounding.display 换算。
    dual_axis = kind in ("combo", "pareto")
    values = [v for s in (series[:1] if dual_axis else series) for v in s["values"] if v is not None]
    lo, hi = min([0, *values]), max([0, *values])
    if kind == "stacked_bar":
        lo = min([0, *(sum(min(0, s["values"][i] or 0) for s in series) for i in range(len(categories)))])
        hi = max([0, *(sum(max(0, s["values"][i] or 0) for s in series) for i in range(len(categories)))])
    if lo == hi:
        hi = lo + 1
    ticks = _nice_ticks(lo, hi)
    lo, hi = ticks[0], ticks[-1]
    labelled = kind not in ("line", "area") and len(categories) <= 20
    if horizontal and labelled:
        # 条形末端外侧要放数值标注：两端各留出一段空白，标注不压到类目名或越出画布。
        pad = (hi - lo) * 0.14
        lo, hi = (lo - pad if lo < 0 else lo), (hi + pad if hi > 0 else hi)
    left, right, bottom, top = (105 if horizontal else 64), width - 65, 45, height - header - 25
    span = hi - lo

    def coordinate(value):
        return (left if horizontal else bottom) + (value - lo) / span * (
            (right - left) if horizontal else (top - bottom)
        )

    secondary_ticks = []
    if dual_axis:
        secondary_values = [v for v in series[1]["values"] if v is not None]
        secondary_lo, secondary_hi = min([0, *secondary_values]), max([0, *secondary_values])
        if secondary_lo == secondary_hi:
            secondary_hi = secondary_lo + 1
        secondary_ticks = _nice_ticks(secondary_lo, secondary_hi)

    def series_coordinate(value, index):
        if not dual_axis or not index:
            return coordinate(value)
        return bottom + (value - secondary_ticks[0]) / (secondary_ticks[-1] - secondary_ticks[0]) * (top - bottom)

    for tick in ticks:
        pos = coordinate(tick)
        label = _axis_label(tick, series[0]["unit"])
        if horizontal:
            drawing.add(Line(pos, bottom, pos, top, strokeColor=HexColor(LINE), strokeWidth=0.4))
            text(pos, bottom - 14, label, anchor="middle")
        else:
            drawing.add(Line(left, pos, right, pos, strokeColor=HexColor(LINE), strokeWidth=0.4))
            text(left - 5, pos - 2, label, anchor="end")
    for tick in secondary_ticks:
        text(right + 5, series_coordinate(tick, 1) - 2, _axis_label(tick, series[1]["unit"]))
    band = ((top - bottom) if horizontal else (right - left)) / len(categories)
    for i, category in enumerate(categories):
        center = top - band * (i + 0.5) if horizontal else left + band * (i + 0.5)
        if horizontal:
            text(left - 7, center - 2, category, anchor="end", limit=left - 10, color=category_color(category, GRAY))
        else:
            # 保留数据点，密集类目仅稀疏显示轴标签。
            if i % max(1, math.ceil(len(categories) / 8)) == 0:
                text(center, bottom - 13, category, anchor="middle", limit=55, color=category_color(category, GRAY))
        positive = negative = 0
        for j, s in enumerate(series):
            value = s["values"][i]
            if value is None:
                continue
            color = colors[j]
            if kind == "diverging_bar":
                polarity = s.get("polarity")
                good = (value >= 0) == (polarity == "up_good")
                color = (POSITIVE if good else NEGATIVE) if polarity else VIZ_NEUTRAL
            color = category_color(category, color)
            if kind in ("line", "area") or (dual_axis and s["mark"] == "line"):
                x, y = center, series_coordinate(value, j)
                if i and s["values"][i - 1] is not None:
                    previous_y = series_coordinate(s["values"][i - 1], j)
                    if kind == "area":
                        fill = Polygon(
                            [x - band, coordinate(0), x - band, previous_y, x, y, x, coordinate(0)],
                            fillColor=HexColor(color),
                            strokeColor=None,
                        )
                        fill.fillOpacity = 0.15
                        drawing.add(fill)
                    drawing.add(
                        Line(
                            x - band,
                            previous_y,
                            x,
                            y,
                            strokeColor=HexColor(category_color(categories[i - 1], color)),
                            strokeWidth=1.5,
                        )
                    )
                drawing.add(Circle(x, y, 2, fillColor=HexColor(color), strokeColor=None))
                # 数值标注要克制：期间不多时逐点标，否则只标最后一个点；文字用文字色，不用系列色。
                if len(categories) <= 12 or i == len(categories) - 1:
                    text(
                        x,
                        y + 5,
                        _formatted(value, s["unit"]),
                        anchor="middle",
                        size=6,
                        color=category_color(category, GRAY),
                    )
                point_x, point_y = x, y
                for mark in _chart_marks(spec, j):
                    if mark["type"] in ("max", "min") and mark["x"] == category:
                        drawing.add(
                            Circle(point_x, point_y, 4, fillColor=None, strokeColor=HexColor(DARK), strokeWidth=1.5)
                        )
                        label = gettext("最高点" if mark["type"] == "max" else "最低点")
                        text(
                            point_x,
                            min(top + 10, point_y + 13) if mark["type"] == "max" else max(bottom + 2, point_y - 13),
                            label,
                            anchor="middle",
                            color=DARK,
                            size=6,
                        )
                continue
            start = 0
            if kind == "stacked_bar":
                start = positive if value >= 0 else negative
                if value >= 0:
                    positive += value
                else:
                    negative += value
                breadth, position = band * 0.65, center - band * 0.325
            else:
                breadth = band * 0.7 / (1 if dual_axis else len(series))
                position = center - band * 0.35 + (0 if dual_axis else j) * breadth
            a, b = series_coordinate(start, j), series_coordinate(start + value, j)
            if horizontal:
                drawing.add(
                    Rect(min(a, b), position, abs(b - a), breadth * 0.9, fillColor=HexColor(color), strokeColor=None)
                )
                if labelled and (len(series) == 1 or dual_axis):
                    text(
                        b + (3 if value >= 0 else -3),
                        position + 2,
                        _formatted(value, s["unit"]),
                        anchor="start" if value >= 0 else "end",
                        size=6,
                        color=category_color(category, GRAY),
                    )
            else:
                drawing.add(
                    Rect(position, min(a, b), breadth * 0.9, abs(b - a), fillColor=HexColor(color), strokeColor=None)
                )
                if labelled and (len(series) == 1 or dual_axis):
                    text(
                        position + breadth / 2,
                        b + (3 if value >= 0 else -8),
                        _formatted(value, s["unit"]),
                        anchor="middle",
                        size=6,
                        color=category_color(category, GRAY),
                    )
            point_x, point_y = (b, position + breadth * 0.45) if horizontal else (position + breadth * 0.45, b)
            for mark in _chart_marks(spec, j):
                if mark["type"] in ("max", "min") and mark["x"] == category:
                    drawing.add(
                        Circle(point_x, point_y, 4, fillColor=None, strokeColor=HexColor(DARK), strokeWidth=1.5)
                    )
                    label = gettext("最高点" if mark["type"] == "max" else "最低点")
                    text(point_x, min(top + 10, point_y + 8), label, anchor="middle", color=DARK, size=6)
    for index, s in enumerate(series):
        for mark in _chart_marks(spec, index):
            # 堆叠图上平均线画不到本段的位置（会落进其他系列）；平均值已在图下注释里列出
            if mark["type"] != "average" or kind == "stacked_bar":
                continue
            position = series_coordinate(mark["value"], index)
            a, b = ((position, bottom), (position, top)) if horizontal else ((left, position), (right, position))
            drawing.add(Line(*a, *b, strokeColor=HexColor(colors[index]), strokeWidth=1, strokeDashArray=[4, 3]))
            label = _formatted(mark["value"], s["unit"])
            trailing = horizontal and position > (left + right) / 2
            text(
                position + (-3 if trailing else 3) if horizontal else right,
                top + 5 if horizontal else min(top + 10, position + 4),
                label,
                anchor="start" if horizontal and not trailing else "end",
                color=colors[index],
                size=6,
                limit=(position - left if trailing else width - position - 3) if horizontal else right - left,
            )
    for i, s in enumerate(series):
        column, row = i % 4, i // 4
        text(left + column * 96, 19 - row * 10, s["name"], color=colors[i], limit=92)
    return drawing


def _figure_datasets(view: dict, figures: list[dict]) -> list[dict]:
    known = {d["ref"]: d for d in view.get("datasets") or []}
    return [known[f["ref"]] for f in figures]


def _display_figure(figure: dict, dataset: dict, *, brief: bool) -> dict:
    """仅沿用导出的可读标签处理，不改变规格的行、数值或图种。"""
    if not brief:
        return figure
    spec = figure["chart"]
    title = _brief_text(figure["title"]) if spec.get("title") and figure.get("title") else _dataset_title(dataset)
    categories = spec["categories"]
    if dataset.get("kind") == "calc" and (spec.get("x") or {}).get("column") == "period":
        categories = [gettext(CALC_PERIOD_LABELS.get(c, c)) for c in categories]
    return {
        **figure,
        "title": title,
        "chart": {
            **spec,
            "categories": categories,
            "marks": [
                {
                    **mark,
                    "x": categories[spec["categories"].index(mark["x"])]
                    if mark["x"] in spec["categories"]
                    else mark["x"],
                }
                for mark in spec.get("marks") or []
            ]
            or None,
            "highlight": [
                categories[spec["categories"].index(category)] if category in spec["categories"] else category
                for category in spec.get("highlight") or []
            ]
            or None,
            "series": [
                {
                    **s,
                    "name": _column_label(dataset, s["column"])
                    if s["name"] in (s["column"], s["label"])
                    else s["name"],
                }
                for s in spec["series"]
            ],
            "kpis": [{**k, "label": _column_label(dataset, k["column"])} for k in spec["kpis"]],
        },
    }


def _mermaid_chart(figure: dict) -> list[str]:
    spec = figure["chart"]
    if spec["kind"] == "area":
        spec = {**spec, "kind": "line"}
    elif spec["kind"] == "pareto":
        # Mermaid 没有双轴；输出原始度量的柱图，累计占比仍可在完整导出中查看。
        spec = {**spec, "kind": "bar"}
    if spec["kind"] == "multiples":
        return [
            line
            for series in spec["series"]
            for line in _mermaid_chart(
                {
                    **figure,
                    "title": series["name"],
                    "chart": {**spec, "kind": "line" if series["mark"] == "line" else "bar", "series": [series]},
                }
            )
        ]
    # xychart-beta 没有图例：多个系列画出来分不清是谁，只在单系列时输出图，否则保留表格。
    if spec["kind"] not in ("line", "bar") or len(spec["series"]) != 1:
        return []
    # Mermaid 不能表示缺测值；保留表格，避免将缺测值画成零。
    values = spec["series"][0]["values"]
    if not values or any(value is None for value in values):
        return []

    def quote(value):
        return json.dumps(str(value), ensure_ascii=False).replace("`", "\\u0060").replace("<", "\\u003c")

    result = ["```mermaid", "xychart-beta"]
    if figure.get("title"):
        result.append("    title " + quote(figure["title"]))
    result.append("    x-axis [" + ", ".join(quote(c) for c in spec["categories"]) + "]")
    result.append("    " + spec["kind"] + " [" + ", ".join(str(v) for v in values) + "]")
    return [*result, "```", ""]


def to_pdf(
    view: dict, *, audience: Audience | None = None, appendix: bool | None = None, datasets: list[str] | None = None
) -> bytes:
    from html import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    view = _export_view(view)
    figures = report_figures(view, datasets)
    brief = resolve_audience(audience, appendix) == "brief"
    notes = _brief_footer(view, datasets) if brief else []
    if brief:
        view = _brief_view(view)
    font, embedded = pdf_font()
    base = ParagraphStyle(
        "base", fontName=font, fontSize=10, leading=14, wordWrap="CJK", spaceAfter=3, textColor=colors.HexColor(DARK)
    )
    h1 = ParagraphStyle("h1", parent=base, fontSize=21, leading=28, spaceAfter=7)
    h2 = ParagraphStyle(
        "h2",
        parent=base,
        fontSize=11.5,
        leading=17,
        spaceBefore=10,
        spaceAfter=5,
        textColor=colors.HexColor(BLUE),
        keepWithNext=True,
    )
    small = ParagraphStyle("small", parent=base, fontSize=7.5, leading=10, textColor=colors.HexColor(GRAY))
    detail = ParagraphStyle("detail", parent=small, leftIndent=12)
    warn = ParagraphStyle("warn", parent=base, textColor=colors.HexColor(NEGATIVE))
    claim_style = ParagraphStyle(
        "claim", parent=base, leading=13, leftIndent=10, bulletIndent=0, bulletFontName=font, spaceAfter=0
    )
    claim_warn = ParagraphStyle("claim_warn", parent=claim_style, textColor=colors.HexColor(NEGATIVE))
    reference_style = ParagraphStyle(
        "reference", parent=small, wordWrap=None, splitLongWords=False, fontSize=7, leading=13, spaceAfter=0
    )
    summary = ParagraphStyle("summary", parent=base, fontSize=11, leading=16)
    code = ParagraphStyle("code", parent=small, fontName="Courier", fontSize=6.5, leading=9, wordWrap="CJK")
    width = 170 * mm - 12  # SimpleDocTemplate 的 Frame 两侧各有 6pt 内边距。

    def esc(value):
        return escape(str(value), quote=False)

    def p(value, style=base):
        return Paragraph(esc(value).replace("\n", "<br/>"), style)

    def refs(value):
        value = value.replace("、", "、\u00a0")
        return f'<nobr><super><font size="7" color="{GRAY}">{esc(value)}</font></super></nobr>'

    def claim_block(claim, check):
        bad = [] if brief else _claim_errors(claim, check)
        tag = (
            f' <font size="7" color="{GRAY}">{gettext("待验证" if brief else "推断，未核实")}</font>'
            if claim.get("type") in ("inference", "recommendation")
            else ""
        )
        text = (claim.get("text") or _rendered(claim)) + (
            " " + gettext("未通过：{error}", error="；".join(bad)) if bad else ""
        )
        style = claim_warn if bad else claim_style
        body = Paragraph(esc(text) + tag, style, bulletText="•")
        if brief:
            block = [body, Spacer(1, 3)]
        else:
            reference = _refs(claim).replace("、", "、\u00a0")
            ref_width = stringWidth(reference, font, reference_style.fontSize) + 2
            body.wrap(width - ref_width, 10000)
            last = body.blPara.lines[-1] if body.blPara.lines else None
            remaining = (
                (last.extraSpace if hasattr(last, "extraSpace") else last[0])
                if last is not None
                else width - ref_width - style.leftIndent
            )
            citation = Paragraph(esc(reference), reference_style)
            item = Table(
                [[body, citation]], colWidths=[width - ref_width, ref_width], hAlign="LEFT", spaceAfter=3, splitInRow=1
            )
            item.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                        ("TOPPADDING", (0, 0), (-1, -1), 0),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                        # 固定宽度引用格不拆行，并贴到正文末行后面。
                        ("LEFTPADDING", (1, 0), (1, 0), 2 - remaining),
                    ]
                )
            )
            block = [item]
        for key, label in (
            ("premise", "依据" if brief else "前提"),
            ("validation", "建议这样验证" if brief else "验证方式"),
        ):
            if value := _detail(claim, key):
                block.append(p(gettext("{label}：{detail}", label=gettext(label), detail=value), detail))
        return block

    class Section(Paragraph):
        def draw(self):
            self.canv.setStrokeColor(colors.HexColor(LINE))
            self.canv.setLineWidth(0.4)
            self.canv.line(0, self.height + 3, self.width, self.height + 3)
            super().draw()

    def heading(title, tag=""):
        return Section(
            esc(gettext(title)) + (f' <font size="8" color="{GRAY}">{esc(gettext(tag))}</font>' if tag else ""), h2
        )

    def table(rows, widths=None):
        result = Table(
            [[p(v, small) for v in row] for row in rows],
            repeatRows=1,
            colWidths=widths or [width / len(rows[0])] * len(rows[0]),
            hAlign="LEFT",
            splitInRow=1,
        )
        result.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F4F7")),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.4, colors.HexColor(LINE)),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]
            )
        )
        return result

    report, verification = view.get("report") or {}, view.get("verification") or {}
    title = _field(report, "title") or gettext("分析报告")
    story = [Paragraph("<b>" + esc(title) + "</b>", h1), p(_header(view), small), Spacer(1, 7)]
    metrics = _core_metrics(view)
    if metrics:
        story.append(heading("核心指标"))
        cards = []
        for m in metrics:
            color = (
                (NEGATIVE if m["raw"] < 0 else POSITIVE if m["raw"] > 0 else GRAY)
                if m["measure"] in ("delta", "rate_change")
                else DARK
            )
            cards.append(
                [p(m["label"], small), Paragraph(f'<font size="15" color="{color}">{esc(m["text"])}</font>', base)]
            )
        strip = Table([cards], colWidths=[width / len(cards)] * len(cards), splitInRow=1)
        strip.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFF")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BOX", (0, 0), (-1, -1), 0, colors.white),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.append(strip)
    story.append(heading("结论"))
    # 与正文引用表格一致，单个段落超过一页时在行内拆分，保留完整内容和每页的底色。
    box = Table([[p(_field(report, "summary") or gettext("（无）"), summary)]], colWidths=[width], splitInRow=1)
    box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EFF4FF")),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    story.append(box)
    if not brief:
        story += [p(gettext("未通过（{error}）", error=x), warn) for x in _bad_fields(verification)]
        story += [
            p(gettext("结论未通过：{error}", error=x), warn)
            for x in (verification.get("conclusion") or {}).get("problems") or []
        ]
    if report.get("causes"):
        story.append(heading("主要原因", CONCLUSION.get(report.get("conclusion"), "")))
        numeric = [c for c in report["causes"] if _cause_value(c)[0] is not None]
        saved = {d["ref"]: d for d in view.get("datasets") or []}
        data = [(_cause_label(c), float(raw), text) for c in numeric for raw, text in [_cause_value(c, saved)]]
        chart = _bar_chart(
            data, font, references=None if brief else [_refs({"refs": c.get("evidence")}) for c in numeric]
        )
        if chart is not None:
            story.append(chart)
        for c in report["causes"]:
            bad = (c.get("errors") or []) if not brief else []
            if _cause_value(c)[0] is None:
                text = _cause_text(c) + (" " + gettext("未通过：{error}", error="；".join(bad)) if bad else "")
                citation = "" if brief else "\u00a0" + refs(_refs({"refs": c.get("evidence")}))
                story.append(Paragraph(esc(text) + citation, claim_warn if bad else claim_style, bulletText="•"))
            elif bad:
                story.append(p(_cause_label(c) + " " + gettext("未通过：{error}", error="；".join(bad)), warn))
    for section, types in (("关键发现", ("fact", "calc")), ("判断与建议", ("inference", "recommendation"))):
        claims = [(c, check) for c, check in _claims(view) if c.get("type") in types and not _context_claim(c, view)]
        if not claims:
            continue
        story.append(heading(section))
        for c, check in claims:
            story += claim_block(c, check)
    if unexplained := _field(report, "unexplained"):
        story.append(heading("还没查清"))
        unresolved = Table(
            [[p("• " + item.strip())] for item in unexplained.split("；") if item.strip()],
            colWidths=[width],
            splitInRow=1,
        )
        unresolved.setStyle(
            TableStyle(
                [
                    ("LINEBEFORE", (0, 0), (0, -1), 2, colors.HexColor(BLUE)),
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F9FAFB")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ]
            )
        )
        story.append(unresolved)
    selected = _figure_datasets(view, figures)
    if selected:
        for i, (d, figure) in enumerate(zip(selected, figures, strict=True)):
            d = _brief_dataset(d, view) if brief else d
            figure = _display_figure(figure, d, brief=brief)
            chart = _spec_chart(figure["chart"], font, title=figure["title"])
            block = ([heading("关键数据")] if i == 0 else []) + [
                p(_dataset_title(d) if brief else f"{d['ref']} {d['title']}")
            ]
            if figure["chart"]["kind"] == "multiples":
                spec = figure["chart"]
                if figure["title"]:
                    block.append(p(figure["title"]))
                block.extend(
                    _spec_chart(
                        {**_chart_panel(spec, index), "omitted_categories": None},
                        font,
                        title=s["name"],
                    )
                    for index, s in enumerate(spec["series"])
                )
                if note := category_omission_note(spec):
                    block.append(p(note, small))
            elif chart is not None:
                block.append(chart)
            block += [table(_table_data(d, brief=brief)), p(_row_note(d), small), Spacer(1, 3)]
            if figure["chart"]["kind"] == "multiples":
                story.extend(block)
            else:
                story.append(KeepTogether(block))
    story += [Spacer(1, 7)]
    if brief:
        story += [p(note, small) for note in notes]
    if not brief:
        used = _appendix_datasets(view, selected)
        story += [PageBreak(), heading("附录")]
        if method := _field(report, "method"):
            story.append(p(gettext("{label}：{detail}", label=gettext("分析方法"), detail=method), small))
        story += [heading("数据检查"), p(_data_check_note(view), small), heading("用到的数据")]
        story.append(table(_dataset_listing(used), [28, 227, 140, 38, width - 433]))
        story.append(heading("查询明细"))
        for d in used:
            if d.get("restricted") or d.get("kind") not in ("governed", "explore"):
                continue
            versions = d.get("metric_version_ids") or _meta(d).get("metric_version_ids") or []
            story.append(
                p(
                    f"{d['ref']} {_dataset_title(d)} · "
                    + gettext("口径版本：{versions}", versions="、".join(versions) or gettext("未登记")),
                    small,
                )
            )
            sql = d.get("sql") or _meta(d).get("sql") or gettext("（未保存 SQL）")
            # 布局负责折行，保存的 SQL 文本和字符串字面量保持原样。
            text = esc(sql)
            text = re.sub(r"[^\x00-\x7f]+", lambda m: f'<font name="{font}">{m[0]}</font>', text)
            story.append(Paragraph(text.replace("\n", "<br/>"), code))
    if not embedded:
        story.append(
            p(gettext("未找到可嵌入的中文字体（AVELIS_PDF_FONT）：中文需阅读器安装亚洲字体包才能显示。"), small)
        )

    def page_number(canvas, doc):
        canvas.setFont(font, 7)
        canvas.setFillColor(colors.HexColor(GRAY))
        canvas.drawRightString(A4[0] - 20 * mm, 10 * mm, str(doc.page))

    buf = io.BytesIO()
    SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=title,
        author="Avelis",
    ).build(story, onFirstPage=page_number, onLaterPages=page_number)
    return buf.getvalue()


def to_html(
    view: dict, *, audience: Audience | None = None, appendix: bool | None = None, datasets: list[str] | None = None
) -> str:
    from avelis.analysis.export_html import to_html as render_html

    return render_html(view, audience=audience, appendix=appendix, datasets=datasets)
