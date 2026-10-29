"""报告数字核验（ADR 0007 第 4 点）：事实与计算类结论中出现的每个数字，必须能在它引用的数据集中找到。

匹配规则（按显示精度）：
- 结论写 1,234.5 → 数据集中某个值四舍五入到 1 位小数等于 1234.5；
- 写 12.3% → 某个值 ×100 后到 1 位小数等于 12.3（变化率以小数保存），或数据集本身就是 12.3；
- 写 12.3 万 / 1.2 亿 → 某个值除以 1 万 / 1 亿后按同样精度相等；
- 写“10 家门店”“279 行” → 引用数据集的行数或某列的不同值个数相等（自己推算的计数，如 10 - 1 = 9，仍标为未核实；
  截断的数据集不是全集，不能用它的行数核实）；
- 正负号必须一致；只有数字直接跟在“下降、减少、负贡献”等表示减少的词后面（如“下降 220”）时，才可以对应数据集中的 -220；
- 区间（2400–3200、8-9 万）的两端都要核验，单位写在末尾时两端共用；
- 中文数字表示的数量（“三家”“五个”“三点五万”“百分之十二”）与大写数字（“壹佰”）不能可靠解析，一律标为未核实；
  系统提示要求模型用阿拉伯数字书写所有数量；
- 只有真实存在的日历日期才当日期排除：完整的 YYYY-MM-DD（或 YYYY/MM/DD）与“M 月 D 日”（可带“YYYY 年”），
  用日历校验（2026-02-31、平年的 2 月 29 日都不是日期）；另外“2026 年”“9 月”“8-9 月”、日历时长（不超过 31 的
  “N 天 / N 周 / N 个月”）与“第 N”“前 N”中的序号不核验。09-15、9/10、2026-09 这类短写法不当日期，按数字（区间）核验；
  阿拉伯数字不看后面的单位，一律提取。金额、件数、家数、行数等全部核验。
事实与计算类结论必须引用存在的数据集，没有引用即未核实。标题、摘要与“未解释”中的数字按同样规则，
对照全部事实 / 计算类结论引用的数据集核验。找不到的数字标为“未核实”，不静默删除。
推断与建议类结论不强制核验，但其中的数字也会列出供人查看。
"""

from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

CHECKED = ("fact", "calc")
SCALE_UNIT = r"%|个百分点|万|亿"
# 真实日期：完整的 YYYY-MM-DD / YYYY/MM/DD，或“[YYYY 年] M 月 D 日”；匹配后再用日历校验
FULL_DATE = re.compile(r"(?<![\d.])((?:19|20)\d{2})([-/])(\d{1,2})\2(\d{1,2})(?![\d.])")
CN_DATE = re.compile(r"(?:(?<![\d.])((?:19|20)\d{2})\s*年\s*)?(?<![\d.])(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_M, _D = r"(?:0?[1-9]|1[0-2])", r"(?:0?[1-9]|[12]\d|3[01])"
CALENDAR_WORDS = re.compile("|".join([
    r"(?<![\d.])(?:19|20)\d{2}\s*年", rf"(?<![\d.]){_M}\s*[-–~至]\s*{_M}\s*月", rf"(?<![\d.]){_M}\s*月(?!\s*\d{{1,2}}\s*[日号])",
    r"(?<![\d.,])(?:[12]?\d|3[01])\s*(?:天|周|个月)", r"第\s*\d+", r"前\s*\d+", r"后\s*\d+", r"Top\s*\d+", r"D\d+",
    r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)"]), re.I)


def _is_date(year: int, month: int, day: int) -> bool:
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def _strip_calendar(text: str) -> str:
    """去掉真实存在的日期与日历词（不需要核验的数字）；不合法的“日期”原样保留，按数字核验。"""
    text = FULL_DATE.sub(lambda m: " " if _is_date(int(m[1]), int(m[3]), int(m[4])) else m[0], text)
    # 不带年份的“2 月 29 日”按闰年判断（年份未知，不能断定它不存在）
    text = CN_DATE.sub(lambda m: " " if _is_date(int(m[1] or 2024), int(m[2]), int(m[3])) else m[0], text)
    return CALENDAR_WORDS.sub(" ", text)
# 前面不能紧跟字母、数字、小数点或千分位逗号；汉字可以紧贴（“订单数10,998单”）。注意 \w 在 Unicode 下包括汉字
_NUM = r"[-+−]?\d[\d,]*(?:\.\d+)?"
NUMBER = re.compile(rf"(?<![0-9A-Za-z_.,])({_NUM})\s*({SCALE_UNIT})?")
RANGE = re.compile(rf"(?<![0-9A-Za-z_.,])({_NUM})\s*({SCALE_UNIT})?\s*(?:-|–|—|~|～|至|到)\s*(\d[\d,]*(?:\.\d+)?)\s*({SCALE_UNIT})?")
# 中文数字表示的数量：后面跟量词、本身以万 / 亿结尾，或写成“百分之……”（“统一”“一致”“之一”不算）。不解析，一律未核实
_CN = "零一二两三四五六七八九十百千万亿点几"
_CN_RUN = rf"[零一二两三四五六七八九十几][{_CN}]*"
_MEASURE = r"(?:家|单|件|笔|台|元|倍|人|次(?!性)|个(?!月)|位|名|条|项|款|种|类|成|张|辆|箱|份|个百分点|%)"
CN_NUMBER = re.compile(rf"(?<![{_CN}第前后])(?:百分之[{_CN}]+|{_CN_RUN}(?=\s*{_MEASURE})|[一二两三四五六七八九十几][{_CN}]*[万亿])")
UPPER = re.compile(r"[壹贰叁肆伍陆柒捌玖拾佰仟]+[万亿]?")
# 表示减少的词：紧跟其后的不带符号的数字是减少的幅度，可以对应数据集中的负值（“下降到 155”是水平，不算）
DECREASE = re.compile(r"(?:下降|减少|降低|下滑|回落|缩减|削减|萎缩|流失|少了|降幅|减幅|跌幅|下跌|负贡献|拖累)"
                      r"(?:了|幅度|为|约|达|超过|近|共|\s)*$")


def _decimal(text: str) -> Decimal:
    return Decimal(text.replace(",", "").replace("−", "-").replace("+", ""))


def _item(raw: str, unit: str, before: str) -> dict[str, Any]:
    value = _decimal(raw)
    places = len(raw.split(".", 1)[1]) if "." in raw else 0
    return {"text": (raw + unit).strip(), "value": value, "places": places, "unit": unit,
            "signed": raw[0] in "-+−", "decrease": bool(DECREASE.search(before[-12:]))}


def extract_numbers(text: str) -> list[dict[str, Any]]:
    """结论中需要核验的数字（按出现顺序）。无法解析的数字 value 为 None。"""
    cleaned = _strip_calendar(text or "")
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
    for pattern, error in ((CN_NUMBER, "中文数字：请用阿拉伯数字书写"), (UPPER, "无法解析的数字")):
        for m in pattern.finditer(rest):
            found.append((m.start(), {"text": m.group(0), "value": None, "places": 0, "unit": "", "signed": False,
                                      "decrease": False, "error": error}))
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
