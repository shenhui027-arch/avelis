"""报告数字核验（ADR 0007 第 4 点）：事实与计算类结论中出现的每个数字，必须能在它引用的数据集中找到。

匹配规则（按显示精度）：
- 结论写 1,234.5 → 数据集中某个值四舍五入到 1 位小数等于 1234.5；
- 写 12.3% → 某个值 ×100 后到 1 位小数等于 12.3（变化率以小数保存），或数据集本身就是 12.3；
- 写 12.3 万 / 1.2 亿 → 某个值除以 1 万 / 1 亿后按同样精度相等；
- 写“10 家门店”“279 行”“三家门店” → 引用数据集的行数或某列的不同值个数相等（自己推算的计数，如 10 - 1 = 9，仍标为未核实；
  截断的数据集不是全集，不能用它的行数核实）；
- 正负号必须一致；只有数字直接跟在“下降、减少、负贡献”等表示减少的词后面（如“下降 220”）时，才可以对应数据集中的 -220；
- 区间（2400–3200、8-9 万）的两端都要核验，单位写在末尾时两端共用；
- 无法解析的数字（大写数字“壹佰”等）一律标为未核实；
- 日期、年月（2026-09-15、09-15、9/10、9 月、2026 年）、日历时长（不超过 31 的“N 天 / N 周 / N 个月”）
  与“第 N”“前 N”中的序号不当作需要核验的数字；金额、件数、家数、行数等仍全部核验。
事实与计算类结论必须引用存在的数据集，没有引用即未核实。标题、摘要与“未解释”中的数字按同样规则，
对照全部事实 / 计算类结论引用的数据集核验。找不到的数字标为“未核实”，不静默删除。
推断与建议类结论不强制核验，但其中的数字也会列出供人查看。
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

CHECKED = ("fact", "calc")
SCALE_UNIT = r"%|个百分点|万|亿"
# 跟在这些单位后面的“10-12”“9/10”是数量区间，不是日期
_QTY = r"(?!\s*(?:%|个百分点|万|亿|元|家|单|件|笔|台|人|次|倍|个))"
DATE_LIKE = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}|\d{4}-\d{1,2}" + _QTY + r"|\d{4}/\d{1,2}/\d{1,2}|(?<![\d.])\d{1,2}/\d{1,2}(?![\d.])" + _QTY +
    r"|(?<![\d.-])\d{2}-\d{2}(?![\d.-])" + _QTY + r"|\d{4}\s*年|\d{1,2}\s*[-–~至]\s*\d{1,2}\s*月|\d{1,2}\s*月(?:\s*\d{1,2}\s*[日号])?|"
    r"\d{1,2}\s*[日号]|(?<![\d.,])(?:[12]?\d|3[01])\s*(?:天|周|个月)|第\s*\d+|前\s*\d+|后\s*\d+|Top\s*\d+|D\d+|\d{1,2}:\d{2}", re.I)
# 前面不能紧跟字母、数字、小数点或千分位逗号；汉字可以紧贴（“订单数10,998单”）。注意 \w 在 Unicode 下包括汉字
_NUM = r"[-+−]?\d[\d,]*(?:\.\d+)?"
NUMBER = re.compile(rf"(?<![0-9A-Za-z_.,])({_NUM})\s*({SCALE_UNIT})?")
RANGE = re.compile(rf"(?<![0-9A-Za-z_.,])({_NUM})\s*({SCALE_UNIT})?\s*(?:-|–|—|~|～|至|到)\s*(\d[\d,]*(?:\.\d+)?)\s*({SCALE_UNIT})?")
# 中文小写数字：后面跟量词或本身以万 / 亿结尾时才算数量（“统一”“一致”不算）
_CN = "零一二两三四五六七八九十百千万亿"
_CN_RUN = rf"[一二两三四五六七八九十][{_CN}]*"
CN_NUMBER = re.compile(rf"(?<![{_CN}第前后])(?:(百分之)({_CN_RUN})|({_CN_RUN})(?=\s*(?:家|单|件|笔|台|元|倍|人|次|个百分点|%))"
                       rf"|([一二两三四五六七八九十][{_CN}]*[万亿]))")
UPPER = re.compile(r"[壹贰叁肆伍陆柒捌玖拾佰仟]+[万亿]?")
# 表示减少的词：紧跟其后的不带符号的数字是减少的幅度，可以对应数据集中的负值（“下降到 155”是水平，不算）
DECREASE = re.compile(r"(?:下降|减少|降低|下滑|回落|缩减|削减|萎缩|流失|少了|降幅|减幅|跌幅|下跌|负贡献|拖累)"
                      r"(?:了|幅度|为|约|达|超过|近|共|\s)*$")


def _decimal(text: str) -> Decimal:
    return Decimal(text.replace(",", "").replace("−", "-").replace("+", ""))


def _cn_value(text: str) -> Decimal | None:
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    small = {"十": 10, "百": 100, "千": 1000}
    total, section, num = 0, 0, 0
    for ch in text:
        if ch in digits:
            num = digits[ch]
        elif ch in small:
            section += (num or 1) * small[ch]
            num = 0
        elif ch in ("万", "亿"):
            total = (total + section + num) * (10000 if ch == "万" else 100000000)
            section, num = 0, 0
        else:
            return None
    return Decimal(total + section + num)


def _item(raw: str, unit: str, before: str) -> dict[str, Any]:
    value = _decimal(raw)
    places = len(raw.split(".", 1)[1]) if "." in raw else 0
    return {"text": (raw + unit).strip(), "value": value, "places": places, "unit": unit,
            "signed": raw[0] in "-+−", "decrease": bool(DECREASE.search(before[-12:]))}


def extract_numbers(text: str) -> list[dict[str, Any]]:
    """结论中需要核验的数字（按出现顺序）。无法解析的数字 value 为 None。"""
    cleaned = DATE_LIKE.sub(" ", text or "")
    found: list[tuple[int, dict[str, Any]]] = []
    spans: list[tuple[int, int]] = []
    for m in RANGE.finditer(cleaned):
        end_unit = m.group(4) or ""
        found.append((m.start(1), _item(m.group(1), m.group(2) or end_unit, cleaned[:m.start(1)])))
        found.append((m.start(3), _item(m.group(3), end_unit, cleaned[:m.start(1)])))
        spans.append(m.span())
    rest = "".join(" " if any(a <= i < b for a, b in spans) else ch for i, ch in enumerate(cleaned))
    for m in NUMBER.finditer(rest):
        try:
            found.append((m.start(), _item(m.group(1), m.group(2) or "", rest[:m.start()])))
        except InvalidOperation:
            continue
    for m in CN_NUMBER.finditer(rest):
        run = m.group(2) or m.group(3) or m.group(4)
        unit = "%" if m.group(1) or rest[m.end():].lstrip().startswith("%") else ""
        if run[-1] in "万亿" and len(run) > 1:  # “三万”与“3 万”同样按显示精度核验
            run, unit = run[:-1], run[-1]
        value = _cn_value(run)
        found.append((m.start(), {"text": m.group(0), "value": value, "places": 0, "unit": unit, "signed": False,
                                  "decrease": bool(DECREASE.search(rest[:m.start()][-12:]))}))
    for m in UPPER.finditer(rest):
        found.append((m.start(), {"text": m.group(0), "value": None, "places": 0, "unit": "", "signed": False,
                                  "decrease": False, "error": "无法解析的数字"}))
    return [item for _, item in sorted(found, key=lambda x: x[0])]


def _round(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _candidates(datasets: list[dict]) -> list[Decimal]:
    """引用数据集中的全部数值，外加行数与各列的不同值个数（“10 家门店”“279 行”这类计数可以核实）。"""
    values = []
    for ds in datasets:
        rows = ds.get("rows", [])
        if not ds.get("truncated"):  # 截断的数据集不是全集：行数与不同值个数不能用来核实总数
            values.append(Decimal(len(rows)))
        if rows and isinstance(rows[0], dict) and not ds.get("truncated"):
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
    if target is None:
        return False
    scales = {"": [Decimal(1)], "%": [Decimal(100), Decimal(1)], "个百分点": [Decimal(100), Decimal(1)],
              "万": [Decimal("0.0001")], "亿": [Decimal("0.00000001")]}[unit]
    # 只有“下降 220”这类不带符号、紧跟减少词的数字，才能对应数据集中的 -220
    magnitude = number.get("decrease") and not number.get("signed")
    for v in values:
        for scale in scales:
            scaled = v * scale
            if _round(scaled, places) == target or (magnitude and scaled < 0 and _round(-scaled, places) == target):
                return True
    return False


def _check(text: str, values: list[Decimal]) -> list[dict[str, Any]]:
    return [{"text": n["text"], "verified": matches(n, values)} for n in extract_numbers(text)]


def verify_report(claims: list[dict[str, Any]], datasets: dict[str, dict],
                  fields: dict[str, str | None] | None = None) -> dict[str, Any]:
    """claims：[{type: fact|calc|inference|recommendation, text, refs: ["D1", ...]}]；datasets：编号 → {rows, summary}；
    fields：标题、摘要、未解释等报告文字，对照全部事实 / 计算类结论引用的数据集核验。"""
    results, unverified, cited = [], 0, set()
    for i, claim in enumerate(claims):
        given = list(claim.get("refs") or [])
        refs = [r for r in given if r in datasets]
        missing_refs = [r for r in given if r not in datasets]
        checked = claim.get("type") in CHECKED
        items = _check(claim.get("text", ""), _candidates([datasets[r] for r in refs]))
        status = "unchecked" if not checked else (
            "unverified" if any(not x["verified"] for x in items) or missing_refs or not refs else "verified")
        if checked:
            cited.update(refs)
        unverified += status == "unverified"
        results.append({"index": i, "type": claim.get("type"), "status": status, "numbers": items,
                        "missing_refs": missing_refs, **({"no_refs": True} if checked and not given else {})})
    pool = _candidates([datasets[r] for r in sorted(cited)])
    field_results = []
    for name, text in (fields or {}).items():
        items = _check(text or "", pool)
        field_results.append({"field": name, "status": "unverified" if any(not x["verified"] for x in items)
                              else "verified", "numbers": items})
    unverified_fields = sum(f["status"] == "unverified" for f in field_results)
    # unverified：未核实的事实 / 计算类结论条数；unverified_fields：数字未核实的标题、摘要等
    return {"ok": unverified == 0 and unverified_fields == 0, "unverified": unverified, "claims": results,
            "fields": field_results, "unverified_fields": unverified_fields}
