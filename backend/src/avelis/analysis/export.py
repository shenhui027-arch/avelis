"""分析报告导出（ADR 0007 第 11 点）：Markdown、HTML、PDF（含图表与证据附录）、数据集 CSV。

导出内容与报告页读同一份保存结果。默认 brief 只给出正文、可读标签与白话风险提示；full 保留引用编号、
原始检查原因和依据附录。appendix 为旧参数，只有未指定 audience 时才决定受众。
PDF、Markdown 使用渲染后的正文（text）；每个数字的来源（数据集、取值路径、量纲、单位、格式、原值）另有一张
引用来源表（sources_csv，ADR 0009）。来源含探索数据的数字标“探索查询（未经口径审核）”。
PDF 嵌入中文 TrueType 字体（子集）：AVELIS_PDF_FONT 指定的字体文件，否则在常见位置找系统字体（Windows 等线 / 微软雅黑 /
黑体，Linux 文泉驿 / Noto CJK）；都找不到时退回 reportlab 内置的 STSong-Light（不嵌入，需阅读器有亚洲字体包），并在报告中注明。
"""

from __future__ import annotations

import csv
import io
import os
import re
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from avelis.analysis.grounding import _unit, display, number

TYPE_LABEL = {"fact": "事实", "calc": "计算", "inference": "推断", "recommendation": "建议"}
DATA_SOURCE_LABEL = {"governed": "指标口径", "explore": "临时查询，未经口径审核", "calc": "计算"}
REPORT_STATUS = {"all_passed": "来源全部通过", "with_inference": "来源全部通过，含未核实的推断",
                 "no_facts": "没有可核实的事实", "failed": "有未通过的事实或字段"}
MEASURE_LABEL = {"level": "水平值", "delta": "差额", "ratio": "比率", "rate_change": "变化率", "share": "占比",
                 "count": "计数", "unknown": "未知"}
EXPLORE_MARK = "探索查询（未经口径审核）"

Audience = Literal["brief", "full"]
BRIEF_SOURCE_NOTE = "报告中的数字均来自系统内已审核的指标口径，可在 Avelis 中查看计算依据。"
BRIEF_EXPLORE_NOTE = "部分数字来自临时查询，尚未经过指标口径审核。"

# 产品计算工具的固定输出列；业务指标、维度的名称始终从元数据读取。
CALC_COLUMN_LABELS = {
    "period": "期间", "days": "天数", "weekend_days": "周末天数", "holidays": "节假日",
    "holiday_days": "节假日天数", "workdays": "工作日天数", "working_days": "工作日天数",
    "value": "合计", "daily": "日均", "current": "本期", "previous": "对比期",
    "baseline": "对比期", "change": "变化", "delta": "差额", "change_rate": "变化率",
    "rate_change": "变化率", "dimension": "维度", "member": "成员", "contribution": "变化贡献",
    "share": "占比", "rank": "排名", "rows": "行数", "count": "数量", "factor": "因素",
    "effect": "影响", "weight": "权重", "ratio": "比率", "residual": "剩余变化",
    "share_of_change": "变化占比", "factor_metric_code": "指标标识", "x": "变量一", "y": "变量二",
    "n": "样本数", "pearson": "线性相关系数", "spearman": "秩相关系数", "p_value": "显著性概率",
    "significant": "是否显著", "note": "说明", "periods": "期间数", "missing_count": "缺失数",
    "missing_periods": "缺失期间", "name": "名称", "dataset": "来源", "column": "字段",
    "sum": "合计", "mean": "均值", "median": "中位数", "min": "最小值", "max": "最大值",
    "stdev": "标准差", "segment": "分组", "side": "偏离方向", "member_change": "成员变化",
    "ep": "影响幅度", "structure_effect": "结构影响", "rate_effect": "比率影响",
    "change_type": "变化类型", "role": "作用",
}
CALC_PERIOD_LABELS = {"current": "本期", "previous": "对比期", "baseline": "对比期",
                      "previous_period": "上一期", "previous_year": "去年同期", "custom": "对比期"}


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
            before, after = value[:match.start()], value[match.end():]
            contextual = (re.search(r"(?:引用(?:数据集)?|数据集|受治理(?:指标|查询)?|来源已校验|来源已确认)\s*$", before)
                          or re.match(r"\s*的?\s*(?:受治理(?:指标|查询|计算)|探索查询|口径版本)", after))
            return "" if contextual else match[0]

        # 中文也是词字符，不能把“成员D1”里的成员编号当成独立引用。
        value = re.sub(r"(?<!\w)D[1-9]\d*(?!\w)", reference, value)
    for old, new in (("推断，未核实", "待验证"), ("受治理查询", "已审核指标"), ("受治理", "已审核"),
                     ("探索查询", "临时查询"), ("上下文", "相关信息"), ("口径版本", "指标定义"),
                     ("来源已校验", "来源已确认"), ("核验", "检查"), ("计算", "分析"),
                     ("验证方式", "建议这样验证"), ("前提", "依据"), ("SQL", "查询依据")):
        value = value.replace(old, new)
    return value


def _brief_view(view: dict) -> dict:
    text_keys = {"title", "summary", "unexplained", "method", "text", "premise", "validation"}
    report = view.get("report") or {}
    version_ids = sorted({str(version) for d in view.get("datasets") or []
                          for version in [*(d.get("metric_version_ids") or []),
                                          *(_meta(d).get("metric_version_ids") or []),
                                          *(_meta(d).get("versions") or {}).values()]}, key=len, reverse=True)

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
    explore = bool(sources.get("with_explore") or (sources.get("direct") or {}).get("explore")
                   or any(s.get("explore") or s.get("source") == "explore"
                          for _, _, s in _segments(report) if s.get("kind") in ("value", "count")))
    saved = {d["ref"]: d for d in view.get("datasets") or []}
    pending = [*_reference_counts(report), *(d["ref"] for d in _key_datasets(view, datasets))]
    seen = set()
    while pending:
        ref = pending.pop()
        if ref in seen or ref not in saved:
            continue
        seen.add(ref)
        d, meta = saved[ref], _meta(saved[ref])
        explore |= d.get("kind") == "explore" or "explore" in (meta.get("root_kinds") or [])
        pending.extend([*(d.get("inputs") or []), *(meta.get("closure") or [])])
    notes = [BRIEF_EXPLORE_NOTE if explore else BRIEF_SOURCE_NOTE]
    failed = 0
    for group, counter in (("claims", "unverified"), ("fields", "unverified_fields"), ("causes", "unverified_causes")):
        key = "field" if group == "fields" else "index"
        bad = {c.get(key, i) for i, c in enumerate(verification.get(group) or [])
               if c.get("status") == "unverified" or c.get("problems")}
        records = report.get(group) or ([] if group != "fields" else {})
        records = records.items() if isinstance(records, dict) else enumerate(records)
        for index, record in records:
            if (record.get("status") == "unverified" or record.get("errors")
                    or any(d.get("errors") for d in (record.get("details") or {}).values())):
                bad.add(index)
        failed += max(verification.get(counter) or 0, len(bad))
    failed += bool((verification.get("conclusion") or {}).get("problems"))
    if not failed and verification.get("status") == "failed":
        failed = 1
    if failed:
        notes.append(f"有 {failed} 处数字未通过系统校验，请谨慎使用。")
    return notes


REF = re.compile(r"^D[1-9]\d*$")
FIELD_LABEL = {"title": "标题", "summary": "摘要", "unexplained": "未解释", "method": "分析方法"}


def _refs(claim: dict) -> str:
    """引用编号只显示 D + 数字；其余（旧数据或异常输入）不原样输出，只计数。"""
    refs = [str(r) for r in claim.get("refs") or []]
    good = [r for r in refs if REF.match(r)]
    bad = len(refs) - len(good)
    return ("、".join(good) or "—") + (f"（另有 {bad} 个无效引用）" if bad else "")


def _verdict(verification: dict) -> str:
    status = verification.get("status") or ("all_passed" if verification.get("ok") else "failed")
    parts = [REPORT_STATUS.get(status, status)]
    if verification.get("unverified"):
        parts.append(f"{verification['unverified']} 条事实 / 计算类结论未通过")
    if verification.get("unverified_fields"):
        parts.append(f"{verification['unverified_fields']} 处标题 / 摘要 / 未解释未通过")
    return "，".join(parts)


def _segments(report: dict) -> list[tuple[str, str, dict]]:
    """报告里全部引用段：（位置，类型，段）。"""
    out = []
    for name in ("title", "summary", "unexplained", "method"):
        for seg in ((report.get("fields") or {}).get(name) or {}).get("segments") or []:
            out.append((FIELD_LABEL[name], "字段", seg))
    for i, claim in enumerate(report.get("claims", [])):
        kind = TYPE_LABEL.get(claim.get("type"), claim.get("type") or "")
        for seg in claim.get("segments") or []:
            out.append((f"结论 {i + 1}", kind, seg))
        for key, label in (("premise", "前提"), ("validation", "验证方式")):
            for seg in ((claim.get("details") or {}).get(key) or {}).get("segments") or []:
                out.append((f"结论 {i + 1} {label}", kind, seg))
    for i, cause in enumerate(report.get("causes") or []):
        for seg in (cause.get("contribution") or {}).get("segments") or []:
            out.append((f"原因 {i + 1}", "原因", seg))
    return [x for x in out if x[2].get("kind") in ("value", "count", "label", "window")]


CONCLUSION = {"ranked_reasons": "有排序的原因", "offsetting": "成员变化相互抵消", "no_material_change": "没有实质变化",
              "insufficient_evidence": "证据不足"}


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


def _export_view(view: dict) -> dict:
    """受限标记是导出的权限边界；不信任同一对象里残留的行、元数据或已渲染文字。"""
    from avelis.analysis.dataset_projection import restrict_report, restrict_verification

    restricted = {d["ref"] for d in view.get("datasets") or [] if d.get("restricted")}
    if not restricted:
        return view
    report = restrict_report(view.get("report") or {}, restricted)
    fields = {name: record for name, record in (report.get("fields") or {}).items()
              if not record.get("restricted")}
    claims = []
    for claim in report.get("claims") or []:
        if claim.get("restricted"):
            continue
        visible = {**claim, "details": {key: detail for key, detail in (claim.get("details") or {}).items()
                                       if not detail.get("restricted")}}
        # 旧报告同时保存平铺文本和 details，不能回退到已经遮蔽的旧文本。
        for key, detail in (claim.get("details") or {}).items():
            if detail.get("restricted"):
                visible.pop(key, None)
                visible["errors"] = []  # 旧核验错误可能复述该前提中的原值。
        claims.append(visible)
    projected = {**report, "fields": fields,
                 "claims": claims,
                 "causes": [cause for cause in report.get("causes") or [] if not cause.get("restricted")]}
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
    return {**view, "report": projected, "verification": verification,
            "datasets": [{"ref": d["ref"], "kind": d.get("kind"), "restricted": True, "rows": []}
                         if d.get("restricted") else d for d in view.get("datasets") or []]}


def _units(d: dict) -> dict:
    return _meta(d).get("units") or d.get("units") or {}


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
    meta = _meta(d)
    labels = meta.get("export_labels") or meta
    dim = next((dim for dim in labels.get("dimensions") or [] if dim.get("name") == column), {})
    doc = (labels.get("column_docs") or {}).get(column) or {}
    label = (dim.get("label") or dim.get("中文名") or dim.get("comment")
             or (doc if isinstance(doc, str) else doc.get("comment")))
    if not label and d.get("kind") == "calc":
        label = CALC_COLUMN_LABELS.get(column)
        if not label and (match := re.fullmatch(r"(.+)_(sum|mean|median)", column)):
            label = _column_label(d, match[1]) + "·" + CALC_COLUMN_LABELS[match[2]]
    return _brief_text(str(label or column))


def _dataset_title(d: dict) -> str:
    meta = _meta(d)
    names = (meta.get("export_labels") or meta).get("metric_names") or []
    if not names:
        names = list(dict.fromkeys(re.sub(r"·(?:本期|对比期|上一期|去年同期|差额|变化率|变化)$", "", c)
                                   for c in d.get("columns") or [] if c in _units(d)
                                   and (_units(d)[c] or {}).get("kind") in ("level", "ratio")))[:2]
    names = [_column_label(d, name) for name in names]
    args = meta.get("args") or {}
    keys = (meta.get("intent") or {}).get("group_by") or args.get("keys") or (
        [args["segment"]] if args.get("segment") else [])
    return ("、".join(names) or "指标明细") + (" · 按" + "、".join(_column_label(d, k) for k in keys) if keys else "")


def _brief_dataset(d: dict, view: dict) -> dict:
    return {**d, "meta": {**_meta(d), "export_labels": _export_labels(d, view)}}


def _reference_counts(report: dict) -> Counter:
    counts: Counter = Counter()
    for claim in report.get("claims") or []:
        refs = set(claim.get("refs") or [])
        refs.update(s.get("ref") for s in claim.get("segments") or [])
        for detail in (claim.get("details") or {}).values():
            refs.update(s.get("ref") for s in detail.get("segments") or [])
        counts.update(r for r in refs if r and REF.fullmatch(r))
    for cause in report.get("causes") or []:
        refs = set(cause.get("evidence") or [])
        refs.update(s.get("ref") for s in (cause.get("contribution") or {}).get("segments") or [])
        counts.update(r for r in refs if r and REF.fullmatch(r))
    counts.update(s.get("ref") for _, _, s in _segments(report) if REF.fullmatch(s.get("ref") or ""))
    return counts


METRIC_KINDS = {"level", "delta", "ratio", "rate_change", "share"}


def _metric_columns(d: dict) -> list[int]:
    units = _units(d)
    return [i for i, c in enumerate(d.get("columns") or []) if (units.get(c) or {}).get("kind") in METRIC_KINDS]


def _key_datasets(view: dict, datasets: list[str] | None = None) -> list[dict]:
    report = view.get("report") or {}
    counts = _reference_counts(report)
    eligible = {d["ref"]: d for d in view.get("datasets") or []
                if not d.get("restricted") and d.get("kind") != "context" and len(d.get("rows") or []) >= 2
                and any(number(row[i]) is not None for i in _metric_columns(d) for row in d["rows"])
                and not _duplicates_cause_chart(d, report)}
    if datasets is not None:
        return [eligible[r] for r in dict.fromkeys(datasets) if r in eligible][:2]
    return sorted((d for r, d in eligible.items() if counts[r]), key=lambda d: -counts[d["ref"]])[:2]


def _chart_labels(d: dict, numeric: list[int]) -> list[int]:
    cols, meta = d.get("columns") or [], _meta(d)
    keys = (meta.get("args") or {}).get("keys") or (meta.get("intent") or {}).get("group_by") or []
    if "member" in cols:
        keys = ["member"]
    return ([cols.index(c) for c in keys if c in cols]
            or [i for i in range(len(cols)) if i not in numeric and cols[i] not in _units(d)][:2])


def _duplicates_cause_chart(d: dict, report: dict) -> bool:
    """仅跳过主要原因已画出的成员视图；同一证据的其他维度或趋势仍可入选。"""
    cols, rows = d.get("columns") or [], d.get("rows") or []
    numeric = _metric_columns(d)
    if not numeric or ((d.get("time_column") or _meta(d).get("time_column")) in cols and len(rows) > 2):
        return False
    value = next((i for i in numeric if (_units(d).get(cols[i]) or {}).get("kind") == "delta"), numeric[0])
    rows = [r for r in rows if number(r[value]) is not None][:10]
    labels = [cols[i] for i in _chart_labels(d, numeric)]
    for cause in report.get("causes") or []:
        evidence, entity = cause.get("evidence") or [], cause.get("entity") or {}
        if (cause.get("type") != "member" or not evidence or evidence[0] != d["ref"]
                or _cause_value(cause)[0] is None):
            continue
        dimension, member = entity.get("dimension"), entity.get("member")
        if not dimension or member is None:
            continue
        if labels == ["member"] and "dimension" in cols:
            if any(r[cols.index("dimension")] != dimension for r in rows):
                continue
        elif labels != [dimension]:
            continue
        if any(r[cols.index(labels[0])] == member for r in rows):
            return True
    return False


def _formatted(value: Any, meta: dict | None = None) -> str:
    if value is None:
        return "—"
    n = number(value)
    return display(n, _unit(meta)) if n is not None else str(value)


def _table_value(value: Any, meta: dict | None = None) -> str:
    text = _formatted(value, meta)
    if number(value) is not None:
        text = re.sub(r"^([+-]?)(\d{4,})(?=\D|$)", lambda m: m[1] + f"{int(m[2]):,}", text)
    return text


def _core_scope_matches(d: dict, scope: dict) -> bool:
    """只排除已知范围冲突；旧报告或数据集缺少范围信息时沿用原有选择规则。"""
    from avelis.analysis.grounding import scope_key
    from avelis.ask.intent import comparison_range

    meta = _meta(d)
    intent, saved = meta.get("intent") or {}, meta.get("window") or {}
    if scope.get("filters") is not None and intent.get("filters") is not None:
        if scope_key("", scope["filters"], {}) != scope_key("", intent["filters"], {}):
            return False
    window = {"start_date": intent.get("start_date") or saved.get("start"),
              "end_date": intent.get("end_date") or saved.get("end"),
              "compare_start": intent.get("compare_start"), "compare_end": intent.get("compare_end")}
    if intent.get("compare") in ("previous_period", "previous_year") and window["start_date"] and window["end_date"]:
        try:
            start, end = comparison_range(date.fromisoformat(str(window["start_date"])),
                                          date.fromisoformat(str(window["end_date"])), intent["compare"])
        except ValueError:
            pass
        else:
            window.update(compare_start=start, compare_end=end)
    return all(not scope.get(key) or not value or str(scope[key]) == str(value)
               for key, value in window.items())


def _core_metrics(view: dict) -> list[dict]:
    refs = _reference_counts(view.get("report") or {})
    for d in view.get("datasets") or []:
        intent, units = _meta(d).get("intent") or {}, _units(d)
        if (d.get("restricted") or d.get("kind") != "governed" or not refs[d["ref"]] or len(d.get("rows") or []) != 1
                or intent.get("group_by") or intent.get("time_grain") or d.get("time_column")
                or not _core_scope_matches(d, (view.get("report") or {}).get("scope") or {})):
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
            group = cols[start:start + 4]
            if len(group) != 4:
                continue
            a, b, delta, rate = [_unit(units.get(c)) for c in group]
            if (a["kind"] not in ("level", "ratio") or a != b
                    or delta["kind"] != "delta" or rate["kind"] != "rate_change"):
                continue
            values = d["rows"][0][start:start + 4]
            if any(number(v) is None for v in values[:3]):
                continue
            return [{"label": label, "text": _formatted(v, units[c]), "raw": number(v),
                     "measure": units[c]["kind"], "ref": d["ref"]}
                    for label, c, v in zip(("本期", "对比期", "变化", "变化率"), group, values) if number(v) is not None]
    return []


def _header(view: dict) -> str:
    w = view.get("window") or {}
    if not w:
        from avelis.ask.intent import comparison_range

        counts = _reference_counts(view.get("report") or {})
        metrics = _core_metrics(view)
        core_ref = metrics[0]["ref"] if metrics else None
        candidates = sorted((d for d in view.get("datasets") or []
                             if not d.get("restricted") and d.get("kind") == "governed" and counts[d["ref"]]),
                            key=lambda d: (d["ref"] != core_ref, -counts[d["ref"]]))
        for d in candidates:
            meta = _meta(d)
            saved, intent = meta.get("window") or {}, meta.get("intent") or {}
            if not saved.get("start") or not saved.get("end"):
                continue
            w = {"start": saved["start"], "end": saved["end"]}
            if intent.get("compare") == "custom" and intent.get("compare_start") and intent.get("compare_end"):
                w.update(compare_start=intent["compare_start"], compare_end=intent["compare_end"])
            elif intent.get("compare") in ("previous_period", "previous_year"):
                try:
                    start, end = comparison_range(date.fromisoformat(str(w["start"])),
                                                  date.fromisoformat(str(w["end"])), intent["compare"])
                except ValueError:
                    pass
                else:
                    w.update(compare_start=str(start), compare_end=str(end))
            break
    parts = []

    def span(start, end):
        return f"{start} ~ {str(end)[5:] if str(start)[:4] == str(end)[:4] else end}"

    if w.get("start") and w.get("end"):
        parts.append("本期 " + span(w["start"], w["end"]))
    if w.get("compare_start") and w.get("compare_end"):
        parts.append("对比 " + span(w["compare_start"], w["compare_end"]))
    tz = ZoneInfo(os.environ.get("AVELIS_BUSINESS_TIMEZONE", "Asia/Shanghai"))
    today = datetime.now(tz)
    parts.append(f"生成日期 {today.year}年{today.month}月{today.day}日")
    return " · ".join(parts)


def _cause_label(cause: dict) -> str:
    entity, label = cause.get("entity") or {}, cause.get("label") or ""
    kind = cause.get("type")
    if kind == "calendar":
        return "日历因素（天数）" if "天数" in label or any("天数" in str(v) for v in entity.values()) else "日历因素"
    if kind == "governance":
        return "治理事件"
    if kind == "member":
        return entity.get("member") or label or "未命名成员"
    if kind == "factor":
        return label or "因子指标"
    return label or entity.get("member") or "未命名原因"


def _cause_value(cause: dict, datasets: dict[str, dict] | None = None) -> tuple[Decimal | None, str]:
    record = cause.get("contribution") or {}
    seg = next((s for s in record.get("segments") or [] if s.get("kind") == "value"), {})
    raw = number(seg.get("raw"))
    if raw is not None and datasets is not None:
        d = datasets.get(seg.get("ref")) or {}
        path = str(seg.get("path") or "").removeprefix(str(seg.get("ref")) + ".")
        units = _units(d)
        column = next((c for c in sorted(units, key=len, reverse=True)
                       if path == c or path.startswith(c + "[")), None)
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
    direction = {"up": "上升", "down": "下降"}.get(cause.get("direction"), "")
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
            v = CALC_PERIOD_LABELS.get(v, v)
        if c not in units:
            return "—" if v is None else str(v)
        text = _table_value(v, units[c])
        return _brief_text(text) if brief else text

    return [headers] + [[cell(c, v) for c, v in zip(cols, row[:6])] for row in d["rows"][:10]]


def _row_note(d: dict) -> str:
    total = d.get("row_count", len(d.get("rows") or []))
    return f"共 {total} 行，显示前 {min(10, len(d.get('rows') or []))} 行" + (" · 结果被截断" if d.get("truncated") else "")


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
        return ("已保存的采集记录均成功" if collected else "已保存的采集记录含失败或未知状态") + (
            "，无口径漂移" if stable else "，存在口径漂移或未知状态")
    if cols == ["type", "at", "object", "item", "change", "impacts", "note"]:
        return f"已保存范围内记录 {len(rows)} 项变更" if rows else "已保存范围内未发现变更"
    return f"已保存 {d.get('row_count', len(rows))} 条检查记录，未保存汇总结论"


def _source_rows(report: dict) -> list[list[str]]:
    return [[where, str(seg.get("text") or ""), str(seg.get("ref") or "—"),
             re.sub(r"^D[1-9]\d*\.", "", str(seg.get("path") or seg.get("label") or "—")),
             " ~ ".join(map(str, seg["raw"])) if isinstance(seg.get("raw"), list)
             else str(seg["raw"]) if seg.get("raw") is not None else "—"]
            for where, _, seg in _segments(report)]


def _source_marks(seg: dict) -> list[str]:
    return [mark for mark, on in ((EXPLORE_MARK, seg.get("explore")),
                                  ("建议目标（非观测值）", seg.get("role") == "proposal"),
                                  ("假设计算", seg.get("assumption"))) if on]


def _bad_fields(verification: dict) -> list[str]:
    return [f"{FIELD_LABEL.get(f['field'], f['field'])}：{'；'.join(f.get('problems') or [])}"
            for f in verification.get("fields", []) if f.get("status") == "unverified"]


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
        errors += [label + "：" + e for e in ((claim.get("details") or {}).get(key) or {}).get("errors") or []]
    return list(dict.fromkeys(errors))


def _data_check_note(view: dict) -> str:
    notes = dict.fromkeys([_verdict(view.get("verification") or {}),
                           *(_context_note(d).rstrip("。") for d in view.get("datasets") or []
                             if d.get("kind") == "context" and not d.get("restricted"))])
    return "；".join(notes) + "。"


def _appendix_datasets(view: dict, selected: list[dict]) -> list[dict]:
    """附录只列正文实际使用的数据，不展开计算输入或仅用于分析方法的引用。"""
    report = view.get("report") or {}
    body = {**report, "fields": {k: v for k, v in (report.get("fields") or {}).items() if k != "method"},
            "claims": [{**c, "details": {k: v for k, v in (c.get("details") or {}).items()
                                        if k in ("premise", "validation")}}
                       for c in report.get("claims") or []
                       if c.get("type") in TYPE_LABEL and not _context_claim(c, view)]}
    refs = set(_reference_counts(body))
    refs.update(d["ref"] for d in selected)
    refs.update(m["ref"] for m in _core_metrics(view))
    return [d if d.get("restricted") else _brief_dataset(d, view) for d in view.get("datasets") or []
            if d.get("restricted") or (d["ref"] in refs and d.get("kind") in DATA_SOURCE_LABEL)]


def _dataset_listing(datasets: list[dict]) -> list[list[str]]:
    return [["编号", "名称", "来源", "行数", "是否截断"]] + [
        [d["ref"], RESTRICTED_NOTE, "—", "—", "—"] if d.get("restricted") else
        [d["ref"], _dataset_title(d), DATA_SOURCE_LABEL[d["kind"]],
         str(d.get("row_count", len(d.get("rows") or []))), "是" if d.get("truncated") else "否"]
        for d in datasets]


def _markdown_table(rows: list[list[str]]) -> list[str]:
    def cell(value: str) -> str:
        return str(value).replace("|", "\\|").replace("\n", "<br/>")
    return ["| " + " | ".join(map(cell, rows[0])) + " |", "|" + "---|" * len(rows[0]),
            *["| " + " | ".join(map(cell, row)) + " |" for row in rows[1:]]]


def _markdown_claim(claim: dict, check: dict, *, brief: bool = False) -> list[str]:
    text = claim.get("text") or _rendered(claim)
    if not brief and (bad := _claim_errors(claim, check)):
        text += " ⚠ " + "；".join(bad)
    if claim.get("type") in ("inference", "recommendation"):
        text += " <small>" + ("待验证" if brief else "推断，未核实") + "</small>"
    refs = _refs(claim).replace("、", "、\u00a0")
    lines = [f"- {text}" + ("" if brief else f'\u00a0<sup style="color: {GRAY}; white-space: nowrap">{refs}</sup>')]
    for key, label in (("premise", "依据" if brief else "前提"), ("validation", "建议这样验证" if brief else "验证方式")):
        if detail := _detail(claim, key):
            lines.append(f"  - {label}：{detail}")
    return lines


def to_markdown(view: dict, *, audience: Audience | None = None, appendix: bool | None = None,
                datasets: list[str] | None = None) -> str:
    view = _export_view(view)
    brief = resolve_audience(audience, appendix) == "brief"
    notes = _brief_footer(view, datasets) if brief else []
    if brief:
        view = _brief_view(view)
    report, verification = view.get("report") or {}, view.get("verification") or {}
    lines = [f"# {_field(report, 'title') or '分析报告'}", "", _header(view), ""]
    metrics = _core_metrics(view)
    if metrics:
        lines += ["## 核心指标", "", *_markdown_table([[m["label"] for m in metrics], [m["text"] for m in metrics]]), ""]
    lines += ["## 结论", "", "> " + (_field(report, "summary") or "（无）"), ""]
    if not brief:
        lines += [f"- ⚠ 未通过（{x}）" for x in _bad_fields(verification)]
        lines += [f"- ⚠ 结论未通过：{x}" for x in (verification.get("conclusion") or {}).get("problems") or []]
    if report.get("causes"):
        tag = CONCLUSION.get(report.get("conclusion"), report.get("conclusion") or "")
        lines += ["", "## 主要原因" + (f" · {tag}" if tag else ""), ""]
        for i, c in enumerate(report["causes"], 1):
            refs = _refs({"refs": c.get("evidence")})
            bad = "；".join(c.get("errors") or []) if not brief else ""
            lines.append(f"{i}. {_cause_text(c)}" + (f" ⚠ {bad}" if bad else "")
                         + ("" if brief else f" <sup>{refs}</sup>"))
    for title, types in (("关键发现", ("fact", "calc")), ("判断与建议", ("inference", "recommendation"))):
        claims = [(c, check) for c, check in _claims(view) if c.get("type") in types and not _context_claim(c, view)]
        if not claims:
            continue
        lines += ["", "## " + title, ""]
        for c, check in claims:
            lines += _markdown_claim(c, check, brief=brief)
    if unexplained := _field(report, "unexplained"):
        lines += ["", "## 还没查清", ""]
        lines += ["> - " + item.strip() for item in unexplained.split("；") if item.strip()]
    selected = _key_datasets(view, datasets)
    if selected:
        lines += ["", "## 关键数据", ""]
        for d in selected:
            d = _brief_dataset(d, view) if brief else d
            title = _dataset_title(d) if brief else f"{d['ref']} {d['title']}"
            lines += [f"### {title}", "", *_markdown_table(_table_data(d, brief=brief)), "", _row_note(d), ""]
    if brief:
        lines += ["", *[f'<small style="color: {GRAY}">{note}</small>' for note in notes], ""]
    if not brief:
        used = _appendix_datasets(view, selected)
        lines += ["", "## 附录", ""]
        if method := _field(report, "method"):
            lines += [f"**分析方法**：{method}", ""]
        lines += ["### 数据检查", "", _data_check_note(view), "", "### 用到的数据", ""]
        lines += _markdown_table(_dataset_listing(used))
        lines += ["", "### 查询明细", ""]
        for d in used:
            if d.get("restricted") or d.get("kind") not in ("governed", "explore"):
                continue
            versions = d.get("metric_version_ids") or _meta(d).get("metric_version_ids") or []
            lines += [f"#### {d['ref']} {_dataset_title(d)}", "",
                      "口径版本：" + ("、".join(versions) or "未登记"), "",
                      "```sql", d.get("sql") or _meta(d).get("sql") or "（未保存 SQL）", "```", ""]
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


SOURCE_COLUMNS = ["位置", "类型", "显示值", "来源标签", "数据集", "取值路径", "量纲", "单位", "小数位", "方向", "标记", "原值"]


def sources_csv(view: dict) -> bytes:
    """引用来源表：报告里每个引用段来自哪个数据集的哪个值（ADR 0009）。"""
    view = _export_view(view)
    rows = []
    for where, kind, s in _segments(view.get("report") or {}):
        raw = s.get("raw")
        marks = _source_marks(s)
        rows.append([where, kind, s.get("text"), s.get("label"), s.get("ref"), s.get("path"),
                     MEASURE_LABEL.get(s.get("measure"), s.get("measure") or ""), s.get("unit") or "",
                     "" if s.get("precision") is None else s.get("precision"),
                     {"up": "上升", "down": "下降"}.get(s.get("direction"), ""), "；".join(marks),
                     " ~ ".join(raw) if isinstance(raw, list) else raw])
    return to_csv(SOURCE_COLUMNS, rows)


FONT_CANDIDATES = [
    ("C:/Windows/Fonts/Deng.ttf", 0), ("C:/Windows/Fonts/msyh.ttc", 0), ("C:/Windows/Fonts/simhei.ttf", 0),
    ("C:/Windows/Fonts/simsun.ttc", 0), ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0), ("/usr/share/fonts/wqy-microhei/wqy-microhei.ttc", 0),
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
            pdfmetrics.registerFontFamily("AvelisCJK", normal="AvelisCJK",
                                          bold="AvelisCJKBold" if os.path.exists(bold) else "AvelisCJK")
            return "AvelisCJK", True
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    pdfmetrics.registerFontFamily("STSong-Light", normal="STSong-Light", bold="STSong-Light")
    _FONT.update(name="STSong-Light", embedded=False)
    return "STSong-Light", False


BLUE, NEGATIVE, POSITIVE, GRAY, LINE = "#2F6FEB", "#D92D20", "#12B76A", "#667085", "#EAECF0"
DARK = "#101828"


def _num(v: Any) -> float | None:
    n = number(v)
    return float(n) if n is not None else None


def _ellipsis(text: str, font: str, size: float, width: float) -> str:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    if stringWidth(text, font, size) <= width:
        return text
    while text and stringWidth(text + "…", font, size) > width:
        text = text[:-1]
    return text + "…"


def _axis(value: float) -> str:
    scale, suffix = (1e8, "亿") if abs(value) >= 1e8 else (1e4, "万") if abs(value) >= 1e4 else (1, "")
    return (f"{value / scale:,.2f}".rstrip("0").rstrip(".")) + suffix


def _bar_chart(data: list[tuple[str, float, str]], font: str, width: float = 468,
               *, references: list[str] | None = None):
    from reportlab.graphics.shapes import Drawing, Line, Rect, String
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase.pdfmetrics import stringWidth

    if not data:
        return None
    height = max(80, len(data) * 14 + 26)
    drawing = Drawing(width, height)
    references = references or [""] * len(data)
    tail = max(stringWidth(text, font, 7.5) + stringWidth(" · " + ref, font, 6.5) if ref
               else stringWidth(text, font, 7.5) for (_, _, text), ref in zip(data, references))
    left, right = 118, width - max(76, tail + 8)
    lo, hi = min(0, min(v for _, v, _ in data)), max(0, max(v for _, v, _ in data))
    span = hi - lo or 1

    def x(value):
        return left + (value - lo) / span * (right - left)

    drawing.add(Line(x(0), 20, x(0), height - 3, strokeColor=HexColor(LINE), strokeWidth=0.5))
    for tick in sorted({lo, 0, hi}):
        drawing.add(String(x(tick), 5, _axis(tick), fontName=font, fontSize=7,
                           textAnchor="middle", fillColor=HexColor(GRAY)))
    for i, (label, value, text) in enumerate(data):
        y = height - 14 - i * 14
        color = HexColor(NEGATIVE if value < 0 else POSITIVE)
        drawing.add(String(0, y, _ellipsis(label, font, 8, left - 8), fontName=font, fontSize=8,
                           fillColor=HexColor(GRAY)))
        drawing.add(Rect(min(x(0), x(value)), y - 1, abs(x(value) - x(0)), 8,
                         strokeColor=None, fillColor=color))
        # 数字放在条形的末端；零轴两侧留足位置，不与类目标签重叠。
        drawing.add(String(right + 6, y, text, fontName=font, fontSize=7.5, fillColor=color))
        if ref := references[i]:
            drawing.add(String(right + 6 + stringWidth(text, font, 7.5), y, " · " + ref,
                               fontName=font, fontSize=6.5, fillColor=HexColor(GRAY)))
    return drawing


def _chart(d: dict, font: str, *, brief: bool = False):
    """只画已登记指标；旧调用没有 meta 时兼容数值列，空值始终跳过。"""
    from reportlab.graphics.charts.lineplots import LinePlot
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.lib.colors import HexColor

    if d.get("restricted"):
        return None
    cols, rows = d.get("columns") or [], d.get("rows") or []
    if not rows or len(cols) < 2:
        return None
    units = _units(d)
    numeric = _metric_columns(d)
    if not numeric and not units:
        numeric = [i for i in range(len(cols)) if any(_num(r[i]) is not None for r in rows)
                   and all(r[i] is None or _num(r[i]) is not None for r in rows)]
    if not numeric:
        return None
    value = next((i for i in numeric if units.get(cols[i], {}).get("kind") == "delta"), numeric[0])
    if len(rows) == 2 and (units.get(cols[value]) or {}).get("kind") == "level":
        a, b = (number(r[value]) for r in rows)
        # 两行水平值的差距不超过绝对最大值的 10% 时，表格已能直接表达对比。
        if a is not None and b is not None and a * b >= 0 and abs(a - b) <= max(abs(a), abs(b)) * Decimal("0.1"):
            return None
    time_column = d.get("time_column") or _meta(d).get("time_column")
    if time_column in cols and len(rows) > 2:
        ti = cols.index(time_column)
        periods = sorted({str(r[ti]) for r in rows})
        index = {p: i for i, p in enumerate(periods)}
        dimensions = [i for i in range(len(cols)) if i != ti and cols[i] not in units and i not in numeric]
        series: dict[str, list] = {}
        for r in rows:
            n = _num(r[value])
            if n is None:
                continue
            key = " / ".join(str(r[i]) for i in dimensions if r[i] is not None) or (
                _column_label(d, cols[value]) if brief else cols[value])
            key = _brief_text(key) if brief else key
            series.setdefault(key, []).append((index[str(r[ti])], n))
        if not series:
            return None
        drawing = Drawing(468, 115)
        lp = LinePlot()
        lp.x, lp.y, lp.width, lp.height = 48, 22, 404, 70
        lp.data = [sorted(points) for points in list(series.values())[:6]]
        palette = ("#2F6BEA", "#E5484D", "#30A46C", "#F5A524", "#8E4EC6", "#12A594")
        for i in range(len(lp.data)):
            lp.lines[i].strokeColor = HexColor(palette[i % len(palette)])
        lp.xValueAxis.valueSteps = list(range(0, len(periods), max(1, len(periods) // 6)))
        lp.xValueAxis.labelTextFormat = lambda x: periods[int(x)][-5:] if 0 <= int(x) < len(periods) else ""
        lp.yValueAxis.labelTextFormat = lambda x: _axis(x * float(_unit(units.get(cols[value]))["scale"]))
        for axis in (lp.xValueAxis, lp.yValueAxis):
            axis.labels.fontName, axis.labels.fontSize = font, 7
            axis.labels.fillColor, axis.strokeColor = HexColor(GRAY), HexColor(LINE)
        drawing.add(lp)
        for i, name in enumerate(list(series)[:6]):
            drawing.add(String(48 + i * 70, 105, _ellipsis(name, font, 7, 65), fontName=font, fontSize=7,
                               fillColor=lp.lines[i].strokeColor))
        return drawing
    labels = _chart_labels(d, numeric)
    data = []
    for n, r in enumerate(rows):
        raw = _num(r[value])
        if raw is None:
            continue
        label = " / ".join(str(r[i]) for i in labels if r[i] is not None) or str(n + 1)
        if brief:
            if d.get("kind") == "calc" and [cols[i] for i in labels] == ["period"]:
                label = CALC_PERIOD_LABELS.get(label, label)
            label = _brief_text(label)
        data.append((label, raw * float(_unit(units.get(cols[value]))["scale"]), _formatted(r[value], units.get(cols[value]))))
        if len(data) == 10:
            break
    return _bar_chart(data, font)


def to_pdf(view: dict, *, audience: Audience | None = None, appendix: bool | None = None,
           datasets: list[str] | None = None) -> bytes:
    from html import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    view = _export_view(view)
    brief = resolve_audience(audience, appendix) == "brief"
    notes = _brief_footer(view, datasets) if brief else []
    if brief:
        view = _brief_view(view)
    font, embedded = pdf_font()
    base = ParagraphStyle("base", fontName=font, fontSize=10, leading=14, wordWrap="CJK", spaceAfter=3,
                          textColor=colors.HexColor(DARK))
    h1 = ParagraphStyle("h1", parent=base, fontSize=21, leading=28, spaceAfter=7)
    h2 = ParagraphStyle("h2", parent=base, fontSize=11.5, leading=17, spaceBefore=10, spaceAfter=5,
                        textColor=colors.HexColor(BLUE), keepWithNext=True)
    small = ParagraphStyle("small", parent=base, fontSize=7.5, leading=10, textColor=colors.HexColor(GRAY))
    detail = ParagraphStyle("detail", parent=small, leftIndent=12)
    warn = ParagraphStyle("warn", parent=base, textColor=colors.HexColor(NEGATIVE))
    claim_style = ParagraphStyle("claim", parent=base, leading=13, leftIndent=10,
                                bulletIndent=0, bulletFontName=font, spaceAfter=0)
    claim_warn = ParagraphStyle("claim_warn", parent=claim_style, textColor=colors.HexColor(NEGATIVE))
    reference_style = ParagraphStyle("reference", parent=small, wordWrap=None, splitLongWords=False,
                                    fontSize=7, leading=13, spaceAfter=0)
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
        tag = (f' <font size="7" color="{GRAY}">{"待验证" if brief else "推断，未核实"}</font>'
               if claim.get("type") in ("inference", "recommendation") else "")
        text = (claim.get("text") or _rendered(claim)) + (" 未通过：" + "；".join(bad) if bad else "")
        style = claim_warn if bad else claim_style
        body = Paragraph(esc(text) + tag, style, bulletText="•")
        if brief:
            block = [body, Spacer(1, 3)]
        else:
            reference = _refs(claim).replace("、", "、\u00a0")
            ref_width = stringWidth(reference, font, reference_style.fontSize) + 2
            body.wrap(width - ref_width, 10000)
            last = body.blPara.lines[-1] if body.blPara.lines else None
            remaining = ((last.extraSpace if hasattr(last, "extraSpace") else last[0]) if last is not None
                         else width - ref_width - style.leftIndent)
            citation = Paragraph(esc(reference), reference_style)
            item = Table([[body, citation]], colWidths=[width - ref_width, ref_width], hAlign="LEFT",
                         spaceAfter=3, splitInRow=1)
            item.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                                      ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                      ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                      ("TOPPADDING", (0, 0), (-1, -1), 0),
                                      ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                                      # 固定宽度引用格不拆行，并贴到正文末行后面。
                                      ("LEFTPADDING", (1, 0), (1, 0), 2 - remaining)]))
            block = [item]
        for key, label in (("premise", "依据" if brief else "前提"),
                           ("validation", "建议这样验证" if brief else "验证方式")):
            if value := _detail(claim, key):
                block.append(p(f"{label}：{value}", detail))
        return block

    class Section(Paragraph):
        def draw(self):
            self.canv.setStrokeColor(colors.HexColor(LINE))
            self.canv.setLineWidth(0.4)
            self.canv.line(0, self.height + 3, self.width, self.height + 3)
            super().draw()

    def heading(title, tag=""):
        return Section(esc(title) + (f' <font size="8" color="{GRAY}">{esc(tag)}</font>' if tag else ""), h2)

    def table(rows, widths=None):
        result = Table([[p(v, small) for v in row] for row in rows], repeatRows=1,
                       colWidths=widths or [width / len(rows[0])] * len(rows[0]), hAlign="LEFT", splitInRow=1)
        result.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F4F7")),
                                    ("LINEBELOW", (0, 0), (-1, 0), 0.4, colors.HexColor(LINE)),
                                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                    ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                                    ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        return result

    report, verification = view.get("report") or {}, view.get("verification") or {}
    title = _field(report, "title") or "分析报告"
    story = [Paragraph("<b>" + esc(title) + "</b>", h1), p(_header(view), small), Spacer(1, 7)]
    metrics = _core_metrics(view)
    if metrics:
        story.append(heading("核心指标"))
        cards = []
        for m in metrics:
            color = (NEGATIVE if m["raw"] < 0 else POSITIVE if m["raw"] > 0 else GRAY) if m["measure"] in (
                "delta", "rate_change") else DARK
            cards.append([p(m["label"], small), Paragraph(f'<font size="15" color="{color}">{esc(m["text"])}</font>', base)])
        strip = Table([cards], colWidths=[width / len(cards)] * len(cards), splitInRow=1)
        strip.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFF")),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOX", (0, 0), (-1, -1), 0, colors.white),
                                   ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
        story.append(strip)
    story.append(heading("结论"))
    # 与正文引用表格一致，单个段落超过一页时在行内拆分，保留完整内容和每页的底色。
    box = Table([[p(_field(report, "summary") or "（无）", summary)]], colWidths=[width], splitInRow=1)
    box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EFF4FF")),
                             ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                             ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10)]))
    story.append(box)
    if not brief:
        story += [p("未通过（" + x + "）", warn) for x in _bad_fields(verification)]
        story += [p("结论未通过：" + x, warn) for x in (verification.get("conclusion") or {}).get("problems") or []]
    if report.get("causes"):
        story.append(heading("主要原因", CONCLUSION.get(report.get("conclusion"), "")))
        numeric = [c for c in report["causes"] if _cause_value(c)[0] is not None]
        saved = {d["ref"]: d for d in view.get("datasets") or []}
        data = [(_cause_label(c), float(raw), text) for c in numeric for raw, text in [_cause_value(c, saved)]]
        chart = _bar_chart(data, font, references=None if brief else [
            _refs({"refs": c.get("evidence")}) for c in numeric])
        if chart is not None:
            story.append(chart)
        for c in report["causes"]:
            bad = (c.get("errors") or []) if not brief else []
            if _cause_value(c)[0] is None:
                text = _cause_text(c) + (" 未通过：" + "；".join(bad) if bad else "")
                citation = "" if brief else "\u00a0" + refs(_refs({"refs": c.get("evidence")}))
                story.append(Paragraph(esc(text) + citation,
                                       claim_warn if bad else claim_style, bulletText="•"))
            elif bad:
                story.append(p(_cause_label(c) + " 未通过：" + "；".join(bad), warn))
    for section, types in (("关键发现", ("fact", "calc")), ("判断与建议", ("inference", "recommendation"))):
        claims = [(c, check) for c, check in _claims(view) if c.get("type") in types and not _context_claim(c, view)]
        if not claims:
            continue
        story.append(heading(section))
        for c, check in claims:
            story += claim_block(c, check)
    if unexplained := _field(report, "unexplained"):
        story.append(heading("还没查清"))
        unresolved = Table([[p("• " + item.strip())] for item in unexplained.split("；") if item.strip()],
                           colWidths=[width], splitInRow=1)
        unresolved.setStyle(TableStyle([("LINEBEFORE", (0, 0), (0, -1), 2, colors.HexColor(BLUE)),
                                        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F9FAFB")),
                                        ("LEFTPADDING", (0, 0), (-1, -1), 9)]))
        story.append(unresolved)
    selected = _key_datasets(view, datasets)
    if selected:
        for i, d in enumerate(selected):
            d = _brief_dataset(d, view) if brief else d
            chart = _chart(d, font, brief=brief)
            block = ([heading("关键数据")] if i == 0 else []) + [p(_dataset_title(d) if brief else f"{d['ref']} {d['title']}")]
            if chart is not None:
                block.append(chart)
            block += [table(_table_data(d, brief=brief)), p(_row_note(d), small), Spacer(1, 3)]
            story.append(KeepTogether(block))
    story += [Spacer(1, 7)]
    if brief:
        story += [p(note, small) for note in notes]
    if not brief:
        used = _appendix_datasets(view, selected)
        story += [PageBreak(), heading("附录")]
        if method := _field(report, "method"):
            story.append(p("分析方法：" + method, small))
        story += [heading("数据检查"), p(_data_check_note(view), small), heading("用到的数据")]
        story.append(table(_dataset_listing(used), [28, 227, 140, 38, width - 433]))
        story.append(heading("查询明细"))
        for d in used:
            if d.get("restricted") or d.get("kind") not in ("governed", "explore"):
                continue
            versions = d.get("metric_version_ids") or _meta(d).get("metric_version_ids") or []
            story.append(p(f"{d['ref']} {_dataset_title(d)} · 口径版本：" + ("、".join(versions) or "未登记"), small))
            sql = d.get("sql") or _meta(d).get("sql") or "（未保存 SQL）"
            # 布局负责折行，保存的 SQL 文本和字符串字面量保持原样。
            text = esc(sql)
            text = re.sub(r"[^\x00-\x7f]+", lambda m: f'<font name="{font}">{m[0]}</font>', text)
            story.append(Paragraph(text.replace("\n", "<br/>"), code))
    if not embedded:
        story.append(p("未找到可嵌入的中文字体（AVELIS_PDF_FONT）：中文需阅读器安装亚洲字体包才能显示。", small))

    def page_number(canvas, doc):
        canvas.setFont(font, 7)
        canvas.setFillColor(colors.HexColor(GRAY))
        canvas.drawRightString(A4[0] - 20 * mm, 10 * mm, str(doc.page))

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=15 * mm,
                      bottomMargin=15 * mm, title=title, author="Avelis").build(
                          story, onFirstPage=page_number, onLaterPages=page_number)
    return buf.getvalue()


def to_html(view: dict, *, audience: Audience | None = None, appendix: bool | None = None,
            datasets: list[str] | None = None) -> str:
    from avelis.analysis.export_html import to_html as render_html

    return render_html(view, audience=audience, appendix=appendix, datasets=datasets)
