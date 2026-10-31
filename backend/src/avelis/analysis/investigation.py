"""变化分布与证据解释的确定性辅助计算。"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import traverse_scope

from avelis.analysis import attribution

# 至少三个上一期值为正的成员，其变化率都在总体变化率的 ±50% 以内，才判为均匀。
UNIFORM_TOLERANCE = Decimal("0.5")
# 最大单日绝对偏差达到本期总偏差绝对值的 50%，记为集中在某个时间点。
SPIKE_SHARE = Decimal("0.5")


def _number(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _rows(dataset: Any) -> list[dict]:
    return [r if isinstance(r, dict) else dict(zip(dataset.columns, r)) for r in dataset.rows or []]


def _dataset_map(datasets: Any) -> dict[str, Any]:
    if isinstance(datasets, dict):
        return datasets
    return {f"D{d.seq}": d for d in datasets or []}


def _empty_dimension(reason: str) -> dict:
    return {"members": None, "top1_share": None, "top3_share": None, "uniform": None, "reason": reason}


def _input_members(dataset: Any, datasets: Any, summary: dict) -> tuple[dict, Decimal | None, Decimal | None]:
    """归因行只保留了选中的成员：从已查询的完整输入重建，复用原归因算法。"""
    known = _dataset_map(datasets)
    inputs = [known.get(ref) for ref in dataset.inputs or []]
    total = inputs[0] if inputs else None
    labels = summary.get("scanned") or []
    failed = {label: "完整归因输入缺失" for label in labels}
    if total is None or total.kind != "governed" or total.truncated:
        return failed, None, None
    names = [c for c in total.columns if f"{c}·对比期" in total.columns]
    ratio = summary.get("algorithm") == "ratio"
    if len(names) != (2 if ratio else 1) or len(total.rows or []) != 1:
        return failed, None, None
    totals = [_number(_rows(total)[0].get(c)) for n in names for c in (f"{n}·对比期", n)]
    if any(v is None for v in totals) or (ratio and (not totals[2] or not totals[3])):
        return failed, None, None
    previous = totals[0] / totals[2] if ratio else totals[0]
    current = totals[1] / totals[3] if ratio else totals[1]
    change = current - previous
    codes = (total.meta or {}).get("intent", {}).get("metric_codes")
    result = {}
    for label in labels:
        candidates = [d for d in inputs[1:] if d is not None and d.kind == "governed"
                      and "×".join((d.meta or {}).get("intent", {}).get("group_by") or []) == label
                      and (d.meta or {}).get("intent", {}).get("metric_codes") == codes]
        compared = next((d for d in candidates if (d.meta or {}).get("intent", {}).get("compare")), None)
        if compared is None or compared.truncated:
            result[label] = "成员输入缺失或被截断，不能判断完整分布"
            continue
        group = compared.meta["intent"]["group_by"]
        members = {}
        invalid = False
        for row in _rows(compared):
            key = tuple(row.get(c) for c in group)
            if key in members:
                invalid = True
            cells = [row.get(c) for n in names for c in (f"{n}·对比期", n)]
            invalid |= any(v is not None and _number(v) is None for v in cells)
            members[key] = [_number(v) for v in cells]
        if not all(attribution.reconciles([v[i] for v in members.values() if v[i] is not None], totals[i])
                   for i in ([0, 2] if ratio else [0])):
            before = next((d for d in candidates if not (d.meta or {}).get("intent", {}).get("compare")), None)
            if before is None or before.truncated:
                result[label] = "缺少完整的对比期成员，不能判断分布"
                continue
            seen = set()
            for row in _rows(before):
                key = tuple(row.get(c) for c in group)
                invalid |= key in seen
                seen.add(key)
                if key not in members:
                    values = [row.get(n) for n in names]
                    invalid |= any(v is not None and _number(v) is None for v in values)
                    members[key] = [cell for v in values for cell in (_number(v), None)]
        if invalid or not all(attribution.reconciles([v[i] for v in members.values() if v[i] is not None], t)
                              for i, t in enumerate(totals)):
            result[label] = "成员合计与总量对不上或数值无效，不能判断分布"
            continue
        if ratio:
            values = {k: (v[0], v[2], v[1], v[3]) for k, v in members.items()}
            result[label], _ = attribution.ratio_members(values, (totals[0], totals[2], totals[1], totals[3]), change)
            # 原归因展示值保留四位小数；分布判断用输入精度，避免小比率被舍入为零。
            for row, (a0, b0, a1, b1) in zip(result[label], values.values()):
                row["previous"] = (a0 or Decimal(0)) / b0 if b0 else None
                row["current"] = (a1 or Decimal(0)) / b1 if b1 else None
                if row["contribution"] is not None:
                    row["contribution"] = (a1 or Decimal(0)) / totals[3] - (a0 or Decimal(0)) / totals[2]
        else:
            result[label] = attribution.additive_members({k: (v[0], v[1]) for k, v in members.items()}, change)
    return result, previous, change


def _dimension(rows: list[dict], previous: Decimal | None, change: Decimal | None, additive: bool) -> dict:
    result = _empty_dimension("")
    result["members"] = len(rows)
    contributions = [_number(r.get("contribution") if r.get("contribution") is not None else r.get("member_change"))
                     for r in rows]
    if not rows:
        result["reason"] = "成员贡献缺失或无效"
        return result
    if any(v is None for v in contributions):
        result["reason"] = "成员贡献缺失或无效，不能计算集中度"
    elif change:
        shares = sorted((abs(v) / abs(change) for v in contributions), reverse=True)
        result.update(top1_share=float(shares[0]), top3_share=float(sum(shares[:3], Decimal(0))))
    else:
        result["reason"] = "总变化为零或缺失，不能计算集中度"
    pairs = [(_number(r.get("previous")), _number(r.get("current"))) for r in rows]
    if additive and any(p is None or c is None for p, c in pairs):
        result["reason"] = "成员两期数值缺失或无效，不能判断均匀"
        return result
    if additive and previous is not None and change is not None and (
        not attribution.reconciles([p for p, _ in pairs], previous)
        or not attribution.reconciles([c for _, c in pairs], previous + change)
    ):
        result["reason"] = "成员合计与总量对不上，不能判断均匀"
        return result
    eligible = [(p, c) for p, c in pairs if p is not None and p > 0]
    if len(eligible) < 3:
        result.update(uniform=False, reason="上一期值为正的成员不足三个，不判为均匀")
    elif any(c is None for _, c in eligible):
        result["reason"] = "正基数成员本期数值缺失或无效，不能判断均匀"
    elif previous is None or previous == 0 or change is None:
        result["reason"] = "总体变化率不可计算，不能判断均匀"
    else:
        rate = change / previous
        result["uniform"] = all(abs((c - p) / p - rate) <= abs(rate) * UNIFORM_TOLERANCE for p, c in eligible)
    return result


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _time_distribution(dataset: Any, scope: dict) -> dict:
    result = {"max_day_share": None, "date": None, "concentrated": None, "reason": "没有完整的受治理日趋势"}
    if dataset is None or dataset.kind != "governed" or dataset.truncated:
        return result
    meta = dataset.meta or {}
    intent = meta.get("intent") or {}
    if intent.get("time_grain") != "day" or intent.get("group_by") or len(intent.get("metric_codes") or []) > 1:
        return result
    start, end, before, bend = (_date(scope.get(k)) for k in ("start", "end", "compare_start", "compare_end"))
    if None in (start, end, before, bend) or start > end or before > bend:
        result["reason"] = "两期日期窗口缺失或无效"
        return result
    if max(start, before) <= min(end, bend):
        result["reason"] = "两期日期窗口重叠，不能计算时间集中度"
        return result
    time_column = meta.get("time_column") or "period"
    columns = [c for c in dataset.columns if c != time_column]
    if time_column not in dataset.columns or len(columns) != 1:
        result["reason"] = "日趋势缺少日期列或单指标数值列"
        return result
    days = {}
    for row in _rows(dataset):
        day, value = _date(row.get(time_column)), _number(row.get(columns[0]))
        if day is None or value is None or day in days:
            result["reason"] = "日趋势日期重复、缺失或数值无效"
            return result
        days[day] = value
    current_days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    before_days = [before + timedelta(days=i) for i in range((bend - before).days + 1)]
    if any(day not in days for day in current_days + before_days):
        result["reason"] = "两期日趋势有缺天，不能计算时间集中度"
        return result
    baseline = sum((days[d] for d in before_days), Decimal(0)) / len(before_days)
    deviations = [(d, days[d] - baseline) for d in current_days]
    total = sum((v for _, v in deviations), Decimal(0))
    if not total:
        result["reason"] = "本期总偏差为零，不能计算时间集中度"
        return result
    day, value = max(deviations, key=lambda item: abs(item[1]))
    share = abs(value) / abs(total)
    return {"max_day_share": float(share), "date": day.isoformat(), "concentrated": share >= SPIKE_SHARE, "reason": ""}


def distribution(attribution_dataset: Any, trend_dataset: Any, datasets: Any = None,
                 window: dict | None = None) -> dict:
    """仅使用已有预检数据计算分布；缺失、截断和对账失败都保留为未知。"""
    summary = ((attribution_dataset.meta or {}).get("summary") or {}) if attribution_dataset is not None else {}
    total = summary.get("total") or {}
    previous, change = _number(total.get("previous")), _number(total.get("change"))
    grouped = {}
    if attribution_dataset is not None:
        if getattr(attribution_dataset, "inputs", None) or summary.get("scanned"):
            grouped, previous, change = _input_members(attribution_dataset, datasets, summary)
        else:
            for row in _rows(attribution_dataset):
                grouped.setdefault(str(row.get("dimension", "")), []).append(row)
        if attribution_dataset.truncated:
            grouped = {label: "归因数据被截断，不能判断完整分布" for label in grouped}
    dimensions = {label: _empty_dimension(rows) if isinstance(rows, str) else _dimension(
        rows, previous, change, summary.get("algorithm") != "ratio")
                  for label, rows in grouped.items()}
    scope = summary.get("scope") or window or {}
    scope = {**scope, "start": scope.get("start") or scope.get("start_date"),
             "end": scope.get("end") or scope.get("end_date")}
    return {"total_change": float(change) if change is not None else None, "dimensions": dimensions,
            "time": _time_distribution(trend_dataset, scope)}


def distribution_block(value: dict, limit: int = 600) -> str:
    """上下文中的短块也计入总字符预算，保留不可计算的原因。"""
    lines = ["变化的分布特征（均匀：各成员变化率都在总体变化率的 ±50% 以内，至少三个正基数成员）"]
    if not value.get("dimensions"):
        lines.append("成员分布不可计算：没有可用的完整归因维度")
    for label, dim in value.get("dimensions", {}).items():
        shares = [f"{dim[k]:.1%}" if dim.get(k) is not None else "未知" for k in ("top1_share", "top3_share")]
        uniform = {True: "均匀", False: "不判为均匀", None: "均匀程度未知"}[dim.get("uniform")]
        lines.append(f"{label}：成员数 {dim.get('members')}，前 1 / 前 3 占总变化 {shares[0]} / {shares[1]}；{uniform}"
                     + (f"；{dim['reason']}" if dim.get("reason") else ""))
    time = value.get("time") or {}
    if time.get("max_day_share") is not None:
        lines.append(f"时间：{time['date']} 的偏差占总偏差 {time['max_day_share']:.1%}；"
                     + ("集中在某个时间点" if time["concentrated"] else "未集中在某个时间点"))
    else:
        lines.append(f"时间：{time.get('reason') or '不可计算'}")
    text = "\n".join(lines)
    suffix = "…（已省略）"
    return text if len(text) <= limit else text[:max(0, limit - len(suffix))] + suffix[:max(0, limit)]


def _has_dates(dataset: Any) -> bool:
    meta = dataset.meta or {}
    types = [v.get("kind", "") for v in (meta.get("units") or {}).values()]
    types += list((meta.get("column_types") or {}).values())
    if any(re.match(r"^(date|datetime|timestamp)\b", str(t).strip(), re.I) for t in types):
        return True
    for row in _rows(dataset):
        for value in row.values():
            if isinstance(value, date):
                return True
            if (isinstance(value, str) and re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}(?:$|[ T])", value)
                    and _date(value[:10].replace("/", "-")) is not None):
                return True
    return False


def event_datasets(datasets: dict[str, Any], closure_tables: set[str]) -> list[str]:
    """记录类证据只按已保存的 SQL 表源与日期识别，不依据表名猜测含义。"""
    closure = {tuple(p.strip('`"[]').casefold() for p in name.split(".")) for name in closure_tables}
    found = []
    for ref, dataset in datasets.items():
        meta = dataset.meta or {}
        if not meta.get("sql") or not _has_dates(dataset):
            continue
        try:
            # 调用方补入数据源的真实方言；无法取得时按 StarRocks 解析。
            trees = sqlglot.parse(meta["sql"], read=meta.get("dialect") or "starrocks")
            if len(trees) != 1 or not isinstance(trees[0], exp.Query):
                continue
            # 作用域中的物理表不包含 CTE 名称，避免把内部别名误作外部表。
            tables = {tuple(p.name.casefold() for p in source.parts)
                      for scope in traverse_scope(trees[0]) for source in scope.sources.values()
                      if isinstance(source, exp.Table)}
        except (SqlglotError, ValueError, TypeError):
            continue  # 无法确定表源时不触发修订，SQL 校验仍由既有管线负责。
        if tables and all(not any(t[-min(len(t), len(c)):] == c[-min(len(t), len(c)):]
                                  for c in closure) for t in tables):
            found.append(ref)
    return sorted(found, key=lambda ref: (int(ref[1:]) if ref[1:].isdigit() else float("inf"), ref))


def _used_datasets(report: dict, datasets: dict[str, Any]) -> set[str]:
    used: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("refs", "evidence") and isinstance(item, list):
                    used.update(ref for ref in item if isinstance(ref, str) and ref in datasets)
                elif key in ("dataset", "ref") and isinstance(item, str) and item in datasets:
                    used.add(item)
                elif key == "evidence_id" and isinstance(item, str):
                    used.add(item.split("#", 1)[0])
                elif key == "window" and isinstance(item, str) and item.startswith("dataset:"):
                    used.add(item.removeprefix("dataset:"))
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(report)
    pending = list(used)
    while pending:
        ref = pending.pop()
        dataset = datasets.get(ref)
        if dataset is None:
            continue
        for parent in [*(dataset.inputs or []), *((dataset.meta or {}).get("closure") or [])]:
            if parent not in used:
                used.add(parent)
                pending.append(parent)
    return used


def needs_revision(report: dict, datasets: dict[str, Any], closure_tables: set[str], distribution: dict) -> str | None:
    """三项条件同时成立才给反馈；不改变报告协议、核验结论或修订次数。"""
    causes = report.get("causes") or []
    if not causes or not distribution:
        return None
    primary = causes[0]
    if report.get("conclusion") == "offsetting":
        change = _number(distribution.get("total_change"))
        if not change:
            return None
        primary = next((c for c in causes[:2] if c.get("direction") == ("up" if change > 0 else "down")), {})
    if primary.get("type") not in ("member", "factor"):
        return None
    if not any(d.get("uniform") is True for d in (distribution.get("dimensions") or {}).values()):
        return None
    events = event_datasets(datasets, closure_tables)
    evidence_id = (primary.get("entity") or {}).get("evidence_id")
    if evidence_id and evidence_id.split("#", 1)[0] in events:
        return None
    unused = [ref for ref in events if ref not in _used_datasets(report, datasets)]
    if not unused:
        return None
    return ("变化在成员之间分布均匀，首要原因写成成员 / 因子只是贡献定位；请用已有的记录类数据集（"
            + "、".join(unused) + "）检验驱动因素；检验不了的，结论类型仍按系统判定，在 unexplained 里写明未能检验的驱动因素与理由。")
