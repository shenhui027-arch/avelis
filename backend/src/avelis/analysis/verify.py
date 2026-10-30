"""报告核验（ADR 0009 第 2 点）：模型把数字写成引用片段（见 grounding），后端渲染；核验只做结构检查，不再从正文里提取数字。

适用于事实、计算类结论的 parts，以及标题、摘要、“未解释”字段：
1. 引用必须能解析（数据集存在、列存在、恰好选中一行、格式适用），否则未核实并写明原因；
2. 文字片段里不能有阿拉伯数字（半角、全角都算，日期也不例外：日期用 window 引用或数据集里的日期单元格）；
3. 文字片段里不能有中文数字字符（零〇一二两三四五六七八九十百千万亿与大写数字）。白名单冻结为下面的非数量词、
   “两期 / 两者”对比用语与明确的金额单位说明，不再为了让模型通过而扩充；误判时由修订提示让模型改写，
   确需修改白名单时同步修改 ADR 0009；
4. 方向与符号一致：abs 为真的数值段，前方 8 个字以内的增长类词与负的原值、减少类词与正的原值不能同时出现；
5. 事实、计算类结论至少引用一个数据集（显式 refs 或经引用片段；引用 = 两者的并集），且引用的数据集必须存在。
另外，role=proposal（建议目标值）只能出现在推断、建议类结论中。
推断、建议类结论及 premise、validation：引用照常渲染，文字片段里允许出现数字（假设或方案，不是事实），不核验。
"""

from __future__ import annotations

import re
from typing import Any

from avelis.analysis.grounding import number, render

CHECKED = ("fact", "calc")
FIELDS = ("title", "summary", "unexplained")
DIGITS = re.compile(r"[0-9０-９][0-9０-９.,:/\-]*")
# 中文数字字符（含大写数字）；白名单冻结（ADR 0009 第 2 点第 3 条）
CN_NUMERAL = re.compile(r"[零〇一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟]")
# 指代对比双方的固定说法：只在后面跟对比用语、标点或句末时不算数量（“两家门店”“两段管道”“两期基本持平”仍算）
COMPARISON = re.compile(r"两者|(?:两期|两个时期)(?=\s*(?:的|之间|之差|之比|相比|对比|差|合计|共|各|均|[，。；：、,.;:!?！？）)]|$))")
# 明确的金额单位说明：（万元）、(亿元)、以万元为单位、以亿元为单位
FIXED_WORDS = re.compile(r"[（(]\s*[万亿]元\s*[)）]|以\s*[万亿]元为单位")
NON_QUANTITY = re.compile("|".join([
    "一致", "统一", "之一", "一些", "一起", "一直", "一般", "一定", "一旦", "进一步", "唯一", "单一", "一体", "一样",
    "一下", "一方面", "一系列", r"十分(?!\s*之)", "几乎", "万一", "一同", "一律", "一并", "一再", "一向", "一切", "同一",
    "一贯", "一度", "一时", "不一", "一边", "一部分", "一方", r"[第前后]\s*[零〇一二两三四五六七八九十百千]+",
    "百分点", "百分比"]).replace("之一", r"(?<!分)之一"))
INCREASE = ("增长", "上升", "提高", "增加")
DECREASE = ("下降", "减少", "降低", "下滑")
DIRECTION_WINDOW = 8


def _outside(segments: list[dict]) -> str:
    """文字片段；各段之间用空格隔开，避免跨过引用拼出词。"""
    return " ".join(s["text"] for s in segments if s["kind"] == "text")


def _digit_problems(segments: list[dict]) -> list[str]:
    found = list(dict.fromkeys(DIGITS.findall(_outside(segments))))
    return [f"文字片段里出现数字“{d}”：数字请写成引用片段（日期用 window 引用或数据集里的日期）" for d in found[:5]]


def _chinese_problems(segments: list[dict]) -> list[str]:
    left = NON_QUANTITY.sub(" ", FIXED_WORDS.sub(" ", COMPARISON.sub(" ", _outside(segments))))
    problems = []
    for m in list(CN_NUMERAL.finditer(left))[:3]:
        snippet = left[max(0, m.start() - 4):m.end() + 4].strip()
        problems.append(f"文字片段里出现中文数字“{m.group(0)}”（…{snippet}…）：数量请写成引用片段，或改写为不含数字的说法")
    return problems


def _direction_problems(segments: list[dict]) -> list[str]:
    problems, before = [], ""
    for s in segments:
        if s["kind"] == "value" and s.get("abs"):
            value, near = number(s.get("raw")), before[-DIRECTION_WINDOW:]
            hits = [(near.rfind(w), w) for w in INCREASE + DECREASE if w in near]
            if value is not None and hits:
                word = max(hits)[1]
                if (word in INCREASE and value < 0) or (word in DECREASE and value > 0):
                    problems.append(f"方向与数值符号不一致：“{word} {s['text']}”的原值是 {s['raw']}（{s['path']}）")
        before += s["text"]
    return problems


def _proposal_problems(segments: list[dict]) -> list[str]:
    return [f"{s['path']} 标为建议目标值（proposal），只能用在推断、建议类结论中"
            for s in segments if s["kind"] == "value" and s.get("role") == "proposal"]


def check_text(segments: list[dict], errors: list[str]) -> list[str]:
    """结构检查第 1–4 条（外加 proposal 只能用在推断、建议中）：返回问题列表，空表示通过。"""
    return [*errors, *_digit_problems(segments), *_chinese_problems(segments), *_direction_problems(segments),
            *_proposal_problems(segments)]


def ground_report(report: dict[str, Any], datasets: dict[str, dict],
                  window: dict | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """渲染模型提交的报告并做结构检查，返回（渲染后的报告，核验结果）。
    report：模型提交的 {title, summary, unexplained（片段列表或文字）, claims: [{type, parts, refs, premise, validation}]}；
    datasets：编号 → {kind, columns, rows（list[dict]）, summary, truncated}；window：本次运行的窗口。"""
    if report.get("incomplete"):  # 后端生成的“未完成”报告：没有引用，原样保留，不核验
        return report, {"ok": False, "unverified": 0, "claims": [], "fields": [], "unverified_fields": 0}
    rendered: dict[str, Any] = {k: v for k, v in report.items() if k not in (*FIELDS, "claims")}
    rendered["parts"], rendered["segments"] = {}, {}
    field_results = []
    for name in FIELDS:
        parts = report.get(name)
        rendered["parts"][name] = parts
        if parts is None:
            rendered[name] = None
            continue
        r = render(parts, datasets, window)
        rendered[name], rendered["segments"][name] = r["text"], r["segments"]
        problems = check_text(r["segments"], r["errors"])
        field_results.append({"field": name, "status": "unverified" if problems else "verified", "problems": problems})
    claims, results, unverified = [], [], 0
    for i, claim in enumerate(report.get("claims", [])):
        out, errors, notes = _render_claim(claim, datasets, window)
        claims.append(out)
        missing = [ref for ref in out["refs"] if ref not in datasets]
        if claim.get("type") in CHECKED:
            problems = check_text(out["segments"], errors)
            if missing:
                problems.append(f"引用了不存在的数据集 {'、'.join(missing)}")
            if not out["refs"]:
                problems.append("没有引用数据集：事实与计算类结论必须引用（显式 refs 或引用片段）")
            status = "unverified" if problems else "verified"
        else:  # 推断 / 建议：只列出引用解析问题，不核验
            problems, status = [*errors, *notes], "unchecked"
        unverified += status == "unverified"
        results.append({"index": i, "type": claim.get("type"), "status": status, "problems": problems,
                        "missing_refs": missing})
    rendered["claims"] = claims
    unverified_fields = sum(f["status"] == "unverified" for f in field_results)
    verification = {"ok": unverified == 0 and unverified_fields == 0, "unverified": unverified, "claims": results,
                    "fields": field_results, "unverified_fields": unverified_fields}
    return rendered, verification


def _render_claim(claim: dict, datasets: dict[str, dict],
                  window: dict | None) -> tuple[dict[str, Any], list[str], list[str]]:
    """渲染一条结论：保存 parts、text、segments；refs = 显式引用与引用片段涉及的数据集的并集。"""
    parts = claim.get("parts") or []
    r = render(parts, datasets, window)
    explicit = [str(x) for x in claim.get("refs") or []]
    out = {"type": claim.get("type"), "parts": parts, "text": r["text"], "segments": r["segments"],
           "refs": list(dict.fromkeys(explicit + r["refs"])), "explicit_refs": explicit}
    notes: list[str] = []
    for key in ("premise", "validation"):  # 引用照常渲染，文字片段里的数字允许
        out[key] = claim.get(key)
        if claim.get(key):
            extra = render(claim[key], datasets, window)
            out[key], notes = extra["text"], notes + extra["errors"]
    return out, r["errors"], notes
