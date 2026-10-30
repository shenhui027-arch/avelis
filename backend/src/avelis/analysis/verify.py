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
  用日历校验（2026-02-31、平年的 2 月 29 日都不是日期）；另外“2026 年”“9 月”“9 月份”“九月”“8-9 月”、单独的
  “15 日 / 15 号”（后面是元、分店、门店、仓等时不算）、“3 季度 / Q3”、带时间窗前缀的“近 / 最近 / 过去 / 每
  N 周 / N 个月”、“第 N”“前 N”中的序号、独立的数据集编号 DN 不核验。“N 天”与不带前缀的“3 个月”“3 周”按数量核验。
  09-15、9/10、2026-09 这类短写法不当日期，按数字（区间）核验；
  阿拉伯数字不看后面的单位、也不看前面是否紧贴字母（“GMV100万”“USD100元”），一律提取。金额、件数、家数、行数等全部核验；
- 豁免只收窄：“两者”，以及后面跟“的 / 之间 / 相比 / 对比 / 差 / 合计 / 共 / 各 / 均”、标点或句末的“两期”“两个时期”
  不是数量；“两家门店”“两段管道”“两个周期”“两期基本持平”仍按数量。副词“千万（不要）”、“单位：万元”“以万元计”不是数量；
  中文数字可以以百、千、万开头（“千万元”）；分数“十分之一”“3 分之 1”一律无法解析，未核实；
- 以失败为默认：去掉日期、日历词、序号与已提取的数字后，文本里仍有阿拉伯数字或中文数字字符
  （零〇一二两三四五六七八九十百千万亿，常见的非数量词如“一致”“统一”“之一”除外）时，记一个无法识别的数字，未核实；
  引用数据集文本型维度列里原样出现的名称（“广州四号店”，须含数字字符以外的汉字或字母）先去掉，不当作数字；
  名称前面紧挨着数字时不去（“营收100万达标”里的“万达”）。数据集中的 NaN、Infinity 不作候选值。
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
_CN_MONTH = r"(?<![零〇一二两三四五六七八九十百千万亿几第])(?:十[一二]|[一二三四五六七八九十])"
# 日期与期间写法（不是数量）：YYYY 年、N 月 / N 月份 / 九月、单独的 N 日 / N 号（后面不是元、分店、门店、仓等）、
# N 季度 / QN、第 N、前 N、带时间窗前缀的“近 / 最近 / 过去 / 每 N 周 / N 个月”、独立的数据集编号 DN。
# “N 天”与不带前缀的“3 个月”“3 周”不在这里：按普通数量核验
CALENDAR_WORDS = re.compile("|".join([
    r"(?<![\d.])(?:19|20)\d{2}\s*年", rf"(?<![\d.]){_M}\s*[-–~至]\s*{_M}\s*月",
    rf"(?<![\d.]){_M}\s*月(?!\s*\d{{1,2}}\s*[日号])",
    rf"{_CN_MONTH}\s*月", rf"(?<![\d.月])(?<!月\s){_D}\s*[日号](?!\s*(?:元|分店|门店|店|仓|库|楼|线|门|均|车|机))",
    r"(?<![\d.])[1-4]\s*季度", r"(?<![零〇一二两三四五六七八九十百千万亿几])[一二三四]\s*季度", r"(?<![A-Za-z])Q[1-4](?!\d)",
    r"(?:近|最近|过去|每)\s*(?:[12]?\d|3[01])\s*(?:周|个月)", r"第\s*\d+", r"前\s*\d+", r"后\s*\d+", r"Top\s*\d+",
    r"(?<![A-Za-z0-9])D\d+(?!\d)",
    r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)"]), re.I)
# 指代对比双方的固定说法（“两期差额”“两期相比”），不是数量；只在后面跟对比用语、标点或句末时豁免。
# “两家门店”“两段管道”“两个周期”仍是数量
COMPARISON = re.compile(r"两者|(?:两期|两个时期)(?=\s*(?:的|之间|相比|对比|差|合计|共|各|均|[，。；：、,.;:!?！？）)]|$))")
# 不是数量的固定说法：副词“千万（不要）”、金额单位说明“单位：万元”“以万元计”
FIXED_WORDS = re.compile(r"千万(?=\s*(?:不|别|要|注意))|单位\s*[:：为是]?\s*[万亿]?元|以\s*[万亿]元计")
# 分数“X 分之 Y”（十分之一、3 分之 1）不能可靠解析：整体判为无法解析的数字（“百分之……”见 CN_NUMBER）
FRACTION = re.compile(r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十][零〇一二两三四五六七八九十百千万亿点]*)\s*分之\s*"
                      r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿点几]+)")


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
# 前面只排除数字、小数点与千分位逗号（不拆开 3.5、1,234）；字母与汉字可以紧贴（“GMV100万”“订单数10,998单”）
_NUM = r"[-+−]?\d[\d,]*(?:\.\d+)?"
NUMBER = re.compile(rf"(?<![0-9.,])({_NUM})\s*({SCALE_UNIT})?")
RANGE = re.compile(rf"(?<![0-9.,])({_NUM})\s*({SCALE_UNIT})?\s*(?:-|–|—|~|～|至|到)\s*(\d[\d,]*(?:\.\d+)?)\s*({SCALE_UNIT})?")
# 中文数字表示的数量：后面跟量词、本身以万 / 亿结尾，或写成“百分之……”（“统一”“一致”“之一”不算）。不解析，一律未核实
_CN = "零〇一二两三四五六七八九十百千万亿点几"
_CN_RUN = rf"[零〇一二两三四五六七八九十百千万几][{_CN}]*"
_MEASURE = r"(?:家|单|件|笔|台|元|倍|人|次(?!性)|个(?!月)|位|名|条|项|款|种|类|成|张|辆|箱|份|个百分点|%)"
CN_NUMBER = re.compile(rf"(?<![{_CN}第前后])(?:百分之[{_CN}]+|{_CN_RUN}(?=\s*{_MEASURE})|[零〇一二两三四五六七八九十百千万几][{_CN}]*[万亿])")
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


# 兜底检查：数字字符（阿拉伯数字与中文数字）；常见的非数量词与中文序号不算
NUMERAL = re.compile(r"[0-9０-９零〇一二两三四五六七八九十百千万亿]")
NON_QUANTITY = re.compile("|".join([
    "一致", "统一", "之一", "一些", "一起", "一直", "一般", "一定", "一旦", "进一步", "唯一", "单一", "一体", "一样",
    "一下", "一方面", "一系列", r"十分(?!\s*之)", "几乎", "万一", "一同", "一律", "一并", "一再", "一向", "一切", "同一",
    "一贯", "一度", "一时", "不一", "一边", "一部分", "一方", r"[第前后]\s*[零〇一二两三四五六七八九十百千]+",
    "百分点", "百分比"]).replace("之一", r"(?<!分)之一"))


def _residual(rest: str, matched: list[tuple[int, int]]) -> str | None:
    """去掉已提取的部分与非数量词后仍剩下的数字字符所在的片段；没有则为 None。"""
    left = "".join(" " if any(a <= i < b for a, b in matched) else ch for i, ch in enumerate(rest))
    left = NON_QUANTITY.sub(" ", left)
    m = NUMERAL.search(left)
    return None if m is None else left[max(0, m.start() - 4):m.end() + 4].strip()


def _strip_names(text: str, names: list[str]) -> str:
    """去掉原样出现的数据集名称（“广州四号店”里的“四”是名称的一部分，不是数量）；长的先去。
    名称前面紧挨着阿拉伯数字、小数点、千分位或中文数字时不去（“营收100万达标”里的“万达”不是门店名）。"""
    for name in sorted(names, key=len, reverse=True):
        text = re.sub(rf"(?<![0-9.,０-９{_CN}]){re.escape(name)}", " ", text)
    return text


def extract_numbers(text: str, names: list[str] = ()) -> list[dict[str, Any]]:
    """结论中需要核验的数字（按出现顺序）。无法解析的数字 value 为 None。
    names：引用数据集中带数字字符的名称（见 _names），原样出现时不当作数字。"""
    cleaned = FIXED_WORDS.sub(" ", COMPARISON.sub(" ", _strip_calendar(_strip_names(text or "", list(names)))))
    found: list[tuple[int, dict[str, Any]]] = []
    spans: list[tuple[int, int]] = []
    for m in FRACTION.finditer(cleaned):
        found.append((m.start(), _unparseable(m.group(0), "分数：无法解析的数字")))
        spans.append(m.span())
    cleaned = "".join(" " if any(a <= i < b for a, b in spans) else ch for i, ch in enumerate(cleaned))
    for m in RANGE.finditer(cleaned):
        end_unit = m.group(4) or ""
        found.append((m.start(1), _item(m.group(1), m.group(2) or end_unit, cleaned[:m.start(1)])))
        found.append((m.start(3), _item(m.group(3), end_unit, cleaned[:m.start(1)])))
        spans.append(m.span())
    rest = "".join(" " if any(a <= i < b for a, b in spans) else ch for i, ch in enumerate(cleaned))
    matched: list[tuple[int, int]] = []
    for m in NUMBER.finditer(rest):
        matched.append(m.span())
        try:
            found.append((m.start(), _item(m.group(1), m.group(2) or "", rest[:m.start()])))
        except InvalidOperation:
            found.append((m.start(), _unparseable(m.group(0), "无法解析的数字")))
    for pattern, error in ((UPPER, "无法解析的数字"), (CN_NUMBER, "中文数字：请用阿拉伯数字书写")):
        for m in pattern.finditer(rest):
            if any(a < m.end() and m.start() < b for a, b in matched):  # “壹佰万”里的“万”不再单独记一次
                continue
            matched.append(m.span())
            found.append((m.start(), _unparseable(m.group(0), error)))
    # 以失败为默认：还有没被识别的数字字符，就不能因为“没提取到数字”而通过
    leftover = _residual(rest, matched)
    if leftover:
        found.append((len(rest), _unparseable(leftover, "包含无法识别的数字")))
    return [item for _, item in sorted(found, key=lambda x: x[0])]


def _unparseable(text: str, error: str) -> dict[str, Any]:
    return {"text": text, "value": None, "places": 0, "unit": "", "signed": False, "decrease": False, "error": error}


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
                values.extend(_finite(v))
        for v in _flat_numbers(ds.get("summary")):
            values.append(v)
    return values


def _finite(v: Any) -> list[Decimal]:
    """可以作为候选值的有限数；字符串、NaN、Infinity 都不是。"""
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return []
    return [d] if d.is_finite() else []


# 名称必须含有数字字符以外的汉字或字母：纯数字的“三”、日期样的“2026-09”不算名称，照常核验
_NAME_CHAR = re.compile(r"[A-Za-z一-鿿]")


def _names(datasets: list[dict]) -> list[str]:
    """引用数据集文本型维度列（每个非空值都是不能解析为数字的字符串）里含数字字符的值（门店名、渠道名等）。"""
    names = set()
    for ds in datasets:
        rows = [r for r in ds.get("rows", []) if isinstance(r, dict)]
        for col in (rows[0] if rows else {}):
            values = [r.get(col) for r in rows if r.get(col) is not None]
            if not values or not all(isinstance(v, str) and not _finite(v) and v.strip().lower() not in
                                     ("nan", "infinity", "-infinity", "inf", "-inf") for v in values):
                continue
            names.update(v.strip() for v in values if NUMERAL.search(v)
                         and any(_NAME_CHAR.match(ch) and not NUMERAL.match(ch) for ch in v))
    return [n for n in names if n]


def _flat_numbers(obj: Any) -> list[Decimal]:
    if obj is None or isinstance(obj, bool):
        return []
    if isinstance(obj, dict):
        return [v for x in obj.values() for v in _flat_numbers(x)]
    if isinstance(obj, (list, tuple)):
        return [v for x in obj for v in _flat_numbers(x)]
    return _finite(obj)


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
            try:
                scaled = v * scale
                if _round(scaled, places) == target or (magnitude and scaled < 0 and _round(-scaled, places) == target):
                    return True
            except InvalidOperation:  # 无法量化的候选值（超出精度等）：不匹配，不中断核验
                continue
    return False


def _check(text: str, datasets: list[dict], lookup: dict[str, list[Decimal]] | None = None) -> list[dict[str, Any]]:
    """lookup：本次运行全部数据集的候选值（编号 → 值）。未核实的数字若出现在别的数据集里，记在 found_in，
    只用来提示补引用（核验结果不变，不自动补引用）。"""
    values = _candidates(datasets)
    items = []
    for n in extract_numbers(text, _names(datasets)):
        item = {"text": n["text"], "verified": matches(n, values), **({"error": n["error"]} if n.get("error") else {})}
        found = [ref for ref, cands in (lookup or {}).items() if matches(n, cands)] if not item["verified"] else []
        if found:
            item["found_in"] = found
        items.append(item)
    return items


def verify_report(claims: list[dict[str, Any]], datasets: dict[str, dict],
                  fields: dict[str, str | None] | None = None) -> dict[str, Any]:
    """claims：[{type: fact|calc|inference|recommendation, text, refs: ["D1", ...]}]；datasets：编号 → {rows, summary}；
    fields：标题、摘要、未解释等报告文字，对照全部事实 / 计算类结论引用的数据集核验。"""
    results, unverified, cited = [], 0, set()
    lookup = {ref: _candidates([ds]) for ref, ds in datasets.items()}
    for i, claim in enumerate(claims):
        given = list(claim.get("refs") or [])
        refs = [r for r in given if r in datasets]
        missing_refs = [r for r in given if r not in datasets]
        checked = claim.get("type") in CHECKED
        items = _check(claim.get("text", ""), [datasets[r] for r in refs], lookup)
        status = "unchecked" if not checked else (
            "unverified" if any(not x["verified"] for x in items) or missing_refs or not refs else "verified")
        if checked:
            cited.update(refs)
        unverified += status == "unverified"
        results.append({"index": i, "type": claim.get("type"), "status": status, "numbers": items,
                        "missing_refs": missing_refs, **({"no_refs": True} if checked and not given else {})})
    pool = [datasets[r] for r in sorted(cited)]
    field_results = []
    for name, text in (fields or {}).items():
        items = _check(text or "", pool, lookup)
        field_results.append({"field": name, "status": "unverified" if any(not x["verified"] for x in items)
                              else "verified", "numbers": items})
    unverified_fields = sum(f["status"] == "unverified" for f in field_results)
    # unverified：未核实的事实 / 计算类结论条数；unverified_fields：数字未核实的标题、摘要等
    return {"ok": unverified == 0 and unverified_fields == 0, "unverified": unverified, "claims": results,
            "fields": field_results, "unverified_fields": unverified_fields}
