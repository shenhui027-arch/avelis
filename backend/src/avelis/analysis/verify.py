"""报告数字核验（ADR 0007 第 4 点）：事实与计算类结论中出现的每个数字，必须能在它引用的数据集中找到。

匹配规则（按显示精度）：
- 结论写 1,234.5 → 数据集中某个值四舍五入到 1 位小数等于 1234.5；
- 写 12.3% → 某个值 ×100 后到 1 位小数等于 12.3（变化率以小数保存），或数据集本身就是 12.3；
- 写 12.3 万 / 1.2 亿 → 某个值除以 1 万 / 1 亿后按同样精度相等；
- 写“10 家门店”“279 行” → 引用数据集的行数或某列的不同值个数相等（自己推算的计数，如 10 - 1 = 9，仍标为未核实）；
- 日期、年月（2026-09-15、09-15、9/10、9 月、2026 年）、日历时长（不超过 31 的“N 天 / N 周 / N 个月”）
  与“第 N”“前 N”中的序号不当作需要核验的数字；金额、件数、家数、行数等仍全部核验。
找不到的数字标为“未核实”，不静默删除。推断与建议类结论不强制核验，但其中的数字也会列出供人查看。
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

CHECKED = ("fact", "calc")
DATE_LIKE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{4}-\d{1,2}|\d{4}/\d{1,2}/\d{1,2}|(?<![\d.])\d{1,2}/\d{1,2}(?![\d.])|(?<![\d.-])\d{2}-\d{2}(?![\d.-])|\d{4}\s*年|\d{1,2}\s*[-–~至]\s*\d{1,2}\s*月|\d{1,2}\s*月(?:\s*\d{1,2}\s*[日号])?|"
                       r"\d{1,2}\s*[日号]|(?<![\d.,])(?:[12]?\d|3[01])\s*(?:天|周|个月)|第\s*\d+|前\s*\d+|后\s*\d+|Top\s*\d+|D\d+|\d{1,2}:\d{2}", re.I)
# 前面不能紧跟字母、数字、小数点或千分位逗号；汉字可以紧贴（“订单数10,998单”）。注意 \w 在 Unicode 下包括汉字
NUMBER = re.compile(r"(?<![0-9A-Za-z_.,])([-+−]?\d[\d,]*(?:\.\d+)?)\s*(%|个百分点|万|亿)?")


def _decimal(text: str) -> Decimal:
    return Decimal(text.replace(",", "").replace("−", "-").replace("+", ""))


def extract_numbers(text: str) -> list[dict[str, Any]]:
    cleaned = DATE_LIKE.sub(" ", text)
    out = []
    for m in NUMBER.finditer(cleaned):
        raw, unit = m.group(1), m.group(2) or ""
        try:
            value = _decimal(raw)
        except InvalidOperation:
            continue
        places = len(raw.split(".", 1)[1]) if "." in raw else 0
        out.append({"text": (raw + unit).strip(), "value": value, "places": places, "unit": unit})
    return out


def _round(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _candidates(datasets: list[dict]) -> list[Decimal]:
    """引用数据集中的全部数值，外加行数与各列的不同值个数（“10 家门店”“279 行”这类计数可以核实）。"""
    values = []
    for ds in datasets:
        rows = ds.get("rows", [])
        values.append(Decimal(len(rows)))
        if rows and isinstance(rows[0], dict):
            for col in rows[0]:
                values.append(Decimal(len({str(r.get(col)) for r in rows})))
        for row in ds.get("rows", []):
            for v in (row.values() if isinstance(row, dict) else row):
                if isinstance(v, bool) or v is None:
                    continue
                try:
                    values.append(Decimal(str(v)))
                except (InvalidOperation, ValueError):
                    continue
        for v in _flat_numbers(ds.get("summary")):
            values.append(v)
    return values


def _flat_numbers(obj: Any) -> list[Decimal]:
    if obj is None or isinstance(obj, bool):
        return []
    if isinstance(obj, dict):
        return [v for x in obj.values() for v in _flat_numbers(x)]
    if isinstance(obj, (list, tuple)):
        return [v for x in obj for v in _flat_numbers(x)]
    try:
        return [Decimal(str(obj))]
    except (InvalidOperation, ValueError):
        return []


def matches(number: dict[str, Any], values: list[Decimal]) -> bool:
    target, places, unit = number["value"], number["places"], number["unit"]
    scales = {"": [Decimal(1)], "%": [Decimal(100), Decimal(1)], "个百分点": [Decimal(100), Decimal(1)],
              "万": [Decimal("0.0001")], "亿": [Decimal("0.00000001")]}[unit]
    for v in values:
        for scale in scales:
            scaled = v * scale
            # 结论中的数字可以省略正负号（“下降 220”对应 -220）
            if _round(scaled, places) == target or _round(abs(scaled), places) == abs(target):
                return True
    return False


def verify_report(claims: list[dict[str, Any]], datasets: dict[str, dict]) -> dict[str, Any]:
    """claims：[{type: fact|calc|inference|recommendation, text, refs: ["D1", ...]}]；datasets：编号 → {rows, summary}。"""
    results, unverified = [], 0
    for i, claim in enumerate(claims):
        refs = [r for r in claim.get("refs", []) if r in datasets]
        missing_refs = [r for r in claim.get("refs", []) if r not in datasets]
        numbers = extract_numbers(claim.get("text", ""))
        values = _candidates([datasets[r] for r in refs])
        checked = claim.get("type") in CHECKED
        items = [{"text": n["text"], "verified": matches(n, values)} for n in numbers]
        status = "unchecked" if not checked else (
            "unverified" if any(not x["verified"] for x in items) or missing_refs or (numbers and not refs)
            else "verified")
        unverified += status == "unverified"
        results.append({"index": i, "type": claim.get("type"), "status": status, "numbers": items,
                        "missing_refs": missing_refs})
    # unverified：未核实的事实 / 计算类结论条数
    return {"ok": unverified == 0, "unverified": unverified, "claims": results}
