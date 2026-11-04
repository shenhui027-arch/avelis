"""报告插图的公共投影：页面与导出使用同一套完整数据和选图规格。"""

from __future__ import annotations

import re
from collections import Counter

from avelis.analysis.dataset_projection import build_column_roles
from avelis.analysis.grounding import number
from avelis.charts import recommend
from avelis.i18n import gettext


def category_omission_note(chart: dict) -> str:
    omission = chart.get("omitted_categories")
    if not omission:
        return ""
    return gettext(
        "图表因展示上限省略了 {count} 个类目；完整数据请查看原数据集。"
        if omission["reason"] == "display_limit"
        else "图表按 Top N 设置省略了 {count} 个类目；完整数据请查看原数据集。",
        count=omission["count"],
    )


def dataset_charts(dataset: dict, preference: dict | None = None) -> list[dict]:
    if dataset.get("restricted"):
        return []
    meta = {**(dataset.get("meta") or {})}
    for key in ("units", "polarity", "intent", "time_column"):
        if key in dataset:
            meta[key] = dataset[key]
    columns = dataset.get("columns") or []
    roles = dataset.get("column_roles")
    if roles is None:
        roles = meta.get("column_roles")
    if roles is None:
        roles = build_column_roles(columns, meta)
    return recommend(
        columns,
        dataset.get("rows") or [],
        roles,
        meta.get("units") or {},
        meta.get("polarity") or {},
        meta.get("intent"),
        preference=preference,
    )


def reference_counts(report: dict) -> Counter:
    counts: Counter = Counter()
    records = list((report.get("fields") or {}).values())
    for claim in report.get("claims") or []:
        details = list((claim.get("details") or {}).values())
        refs = set(claim.get("refs") or [])
        refs.update(s.get("ref") for r in [claim, *details] for s in r.get("segments") or [])
        counts.update(r for r in refs if r and re.fullmatch(r"D[1-9]\d*", r))
        records.extend([claim, *details])
    for cause in report.get("causes") or []:
        contribution = cause.get("contribution") or {}
        refs = set(cause.get("evidence") or [])
        refs.update(s.get("ref") for s in contribution.get("segments") or [])
        counts.update(r for r in refs if r and re.fullmatch(r"D[1-9]\d*", r))
        records.append(contribution)
    counts.update(
        s["ref"]
        for r in records
        for s in r.get("segments") or []
        if s.get("kind") in ("value", "count", "label", "window") and re.fullmatch(r"D[1-9]\d*", s.get("ref") or "")
    )
    return counts


def _shows(chart: dict, member) -> bool:
    """图里是否画了这个成员：类目或系列名等于它，或是含它的多维度组合（“甲 / 乙”）。"""
    names = [*chart["categories"], *(s["name"] for s in chart["series"])]
    return any(name == member or str(name) == str(member) or str(member) in str(name).split(" / ") for name in names)


def _duplicates_cause_chart(dataset: dict, report: dict, chart: dict) -> bool:
    """仅排除原因图已展示的成员；其他维度与时间趋势仍可入选。"""
    x = chart.get("x") or {}
    if x.get("role") != "dimension":
        return False
    columns, rows = dataset.get("columns") or [], dataset.get("rows") or []
    dimension = x.get("column")
    for cause in report.get("causes") or []:
        evidence, entity = cause.get("evidence") or [], cause.get("entity") or {}
        contribution = cause.get("contribution") or {}
        raw = number(contribution.get("raw"))
        if raw is None:
            raw = next(
                (number(s.get("raw")) for s in contribution.get("segments") or [] if number(s.get("raw")) is not None),
                None,
            )
        if cause.get("type") != "member" or not evidence or evidence[0] != dataset["ref"] or raw is None:
            continue
        member = entity.get("member")
        if "member" in columns and "dimension" in columns:
            if any(r[columns.index("dimension")] != entity.get("dimension") for r in rows):
                continue
            members = [r[columns.index("member")] for r in rows]
            if member in members and _shows(chart, member):
                return True
            continue
        if dimension != entity.get("dimension"):
            continue
        if member is not None and _shows(chart, member):
            return True
    return False


def key_datasets(view: dict, datasets: list[str] | None = None) -> list[dict]:
    report = view.get("report") or {}
    counts = reference_counts(report)
    eligible = {}
    for dataset in view.get("datasets") or []:
        if dataset.get("restricted") or dataset.get("kind") == "context":
            continue
        options = dataset_charts(dataset)
        if options and not _duplicates_cause_chart(dataset, report, options[0]):
            eligible[dataset["ref"]] = dataset
    if datasets is not None:
        return [eligible[r] for r in dict.fromkeys(datasets) if r in eligible][:2]
    return sorted((d for r, d in eligible.items() if counts[r]), key=lambda d: -counts[d["ref"]])[:2]


def report_figures(view: dict, datasets: list[str] | None = None) -> list[dict]:
    """调用方显式指定数据集（导出的 datasets）时以指定为准；否则用模型选的插图，
    旧报告或空列表退回引用最多的两个可画数据集。指定的数据集若正是模型选过的插图，沿用它的图类型与标题。"""
    saved = (view.get("report") or {}).get("figures") or []
    if datasets is None and saved:
        requests = saved
    else:
        chosen = {figure.get("dataset"): figure for figure in saved}
        requests = [
            chosen.get(d["ref"]) or {"dataset": d["ref"], "title": d.get("title")} for d in key_datasets(view, datasets)
        ]
    known = {d["ref"]: d for d in view.get("datasets") or []}
    result = {}
    for request in requests:
        ref = request.get("dataset")
        dataset = known.get(ref)
        if dataset is None or dataset.get("restricted"):
            continue
        explicit = "kind" in request
        preference = {k: request[k] for k in ("kind", "title", "top_n") if k in request} if explicit else None
        options = dataset_charts(dataset, preference)
        requested_kind = request.get("kind")
        if (
            options
            and requested_kind in ("line", "stacked_bar", "grouped_bar")
            and options[0]["kind"] == "multiples"
            and options[0]["reason"] == "time_by_dimension"
        ):
            # 旧报告曾保存只含首个指标的图种；沿用新分图并保留标题，避免升级后整张插图消失。
            requested_kind = "multiples"
            options = dataset_charts(dataset, {**preference, "kind": requested_kind})
        if not options or (explicit and (requested_kind == "kpi" or options[0]["kind"] != requested_kind)):
            continue
        result[ref] = {"ref": ref, "title": request.get("title"), "chart": options[0]}
        if len(result) == 4:
            break
    return list(result.values())
