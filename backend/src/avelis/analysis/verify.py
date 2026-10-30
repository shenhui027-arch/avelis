"""报告核验（ADR 0009 第 2 点）：数字写成引用片段（见 grounding），后端渲染；核验只做结构检查，不再从正文里提取数字。

核验层保证“数值来源”：每个事实、计算类数字都来自本次运行的某个数据集单元格或登记的汇总值，量纲与格式匹配，
渲染过程没有拼接或改写（界面叫“来源已校验”）。它不保证句子语义正确（取错列、错成员、错期间），那部分交给评测。

适用于事实、计算类结论，以及标题、摘要、“未解释”：
1. 引用必须能解析：数据集、列或汇总路径存在；恰好选中一行；值是有限数值；量纲允许所用的格式；方向与正负一致；
2. 数值段不能拼接：两个数值段直接相邻，或中间只隔着空白与 . , e E + - ×，判错；数值段后面紧跟的文字不能以
   单位或数量后缀开头（%、倍、成、元、万、亿、个百分点、折），单位只能由格式器输出；
3. 文字片段里不能有阿拉伯数字（半角、全角）；日期用 WindowRef 或数据集里的日期单元格；
4. 文字片段里不能有中文数量：中文数字字符与财务大写，以及数量词（倍、翻番、翻倍、成倍、半数、一半、减半、折）；
   白名单是 ADR 0009 列出的最终清单（WHITELIST），修改必须同步修改 ADR；误判时由修订提示让模型改写；
5. 事实、计算类结论必须引用至少一个数据集（显式 refs 或引用片段），引用的数据集必须存在。
另外：role=proposal（建议目标值）与“假设计算”（derive 用了非白名单常量）的数据集只能被推断、建议类结论引用。
推断、建议类结论及 premise、validation：引用照常渲染，文字片段里允许出现数字（假设或方案，不是观测值），不核验。

报告整体状态：all_passed（来源全部通过）、with_inference（含未核实的推断）、no_facts（没有可核实的事实）；
有事实、计算类结论或字段没通过时为 failed。另统计数字来源构成：口径、探索、计算、上下文各有多少个数字。
"""

from __future__ import annotations

import re
from typing import Any

from avelis.analysis.grounding import render

CHECKED = ("fact", "calc")
FIELDS = ("title", "summary", "unexplained")
DETAILS = ("premise", "validation")
NUMERIC = ("value", "count")
DIGITS = re.compile(r"[0-9０-９][0-9０-９.,:/\-]*")
CN_NUMERAL = re.compile(r"[零〇一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟]")
QUANTITY_WORDS = re.compile(r"翻番|翻倍|成倍|半数|一半|减半|倍|折")
# 白名单（最终清单，见 ADR 0009 第 2 点第 4 条）：这些词不算数量
WHITELIST = re.compile("|".join([
    "一致", "统一", r"(?<!分)之一", "一些", "一起", "一直", "一般", "一定", "一旦", "进一步", "唯一", "单一", "一体",
    "一样", "一下", "一方面", "一系列", r"十分(?!\s*之)", "几乎", "万一", "一同", "一律", "一并", "一再", "一向",
    "一切", "同一", "一贯", "一时", "不一", "一边", "一部分", "一方", "两者",
    r"(?:两期|两个时期)(?=\s*(?:的|之间|之差|之比|相比|对比|差|合计|共|各|均|[，。；：、,.;:!?！？）)]|$))"]))
JOINER = re.compile(r"^[\s.,eE+\-×]*$")
SUFFIX = re.compile(r"^\s*(?:%|％|倍|成|元|万|亿|个百分点|折)")
STATUS_LABEL = {"all_passed": "来源全部通过", "with_inference": "含未核实的推断", "no_facts": "没有可核实的事实",
                "failed": "有未通过的事实或字段"}


def _concat_problems(segments: list[dict]) -> list[str]:
    problems, last, between = [], None, ""
    for s in segments:
        if s["kind"] in NUMERIC:
            if last is not None and JOINER.match(between):
                problems.append(f"第 {last['part']}、{s['part']} 个片段：两个数值段之间只隔着“{between}”，会拼成新的数字")
            last, between = s, ""
            continue
        if s["kind"] == "text" and last is not None and between == "" and SUFFIX.match(s["text"]):
            problems.append(f"第 {s['part']} 个片段：数值段后面紧跟单位或数量后缀“{SUFFIX.match(s['text']).group(0).strip()}”"
                            "，单位只能由格式（format）输出")
        between += s["text"] if s["kind"] == "text" else "\0"  # 维度文本、日期等隔开数值段
    return problems


def _text_problems(segments: list[dict]) -> list[str]:
    problems = []
    for s in segments:
        if s["kind"] != "text":
            continue
        for d in list(dict.fromkeys(DIGITS.findall(s["text"])))[:3]:
            problems.append(f"第 {s['part']} 个片段（文字）出现数字“{d}”：数字请写成引用对象（日期用 WindowRef 或数据集里的日期）")
        for w in list(dict.fromkeys(QUANTITY_WORDS.findall(s["text"])))[:3]:
            problems.append(f"第 {s['part']} 个片段（文字）出现数量词“{w}”：倍数请用 x 格式的比率引用，或改写为不含数量的说法")
        left = WHITELIST.sub(" ", QUANTITY_WORDS.sub(" ", s["text"]))
        for m in list(CN_NUMERAL.finditer(left))[:3]:
            snippet = left[max(0, m.start() - 4):m.end() + 4].strip()
            problems.append(f"第 {s['part']} 个片段（文字）出现中文数字“{m.group(0)}”（…{snippet}…）："
                            "数量请写成引用对象，或改写为不含数字的说法")
    return problems


def _restricted_problems(segments: list[dict]) -> list[str]:
    problems = []
    for s in segments:
        if s.get("role") == "proposal":
            problems.append(f"第 {s['part']} 个片段（{s['path']}）标为建议目标值（proposal），只能用在推断、建议类结论中")
        if s.get("assumption"):
            problems.append(f"第 {s['part']} 个片段（{s['path']}）来自假设计算（公式含非白名单常量），只能被推断、建议类结论引用")
    return problems


def check(segments: list[dict], errors: list[str]) -> list[str]:
    """事实、计算类结论与标题等字段的结构检查：返回问题列表，空表示通过。"""
    return [*errors, *_concat_problems(segments), *_text_problems(segments), *_restricted_problems(segments)]


def _record(parts: Any, datasets: dict[str, dict], window: dict | None, *, checked: bool) -> tuple[dict, list[str]]:
    """一个字段的保存结果：parts、text、segments、status、errors。"""
    r = render(parts, datasets, window)
    problems = check(r["segments"], r["errors"]) if checked else list(r["errors"])
    status = ("unverified" if problems else "verified") if checked else "unchecked"
    return {"parts": parts, "text": r["text"], "segments": r["segments"], "status": status, "errors": problems}, r["refs"]


def ground_report(report: dict[str, Any], datasets: dict[str, dict],
                  window: dict | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """渲染模型提交的报告并做结构检查，返回（渲染后的报告，核验结果）。页面与导出都读这份保存结果。
    report：{title, summary, unexplained（片段列表或文字）, claims: [{type, parts, refs, premise, validation}]}；
    datasets：见 grounding.resolve；window：本次运行的窗口。"""
    if report.get("incomplete"):  # 后端生成的“未完成”报告：没有引用，原样保留，不核验
        return report, {"ok": False, "status": "no_facts", "unverified": 0, "claims": [], "fields": [],
                        "unverified_fields": 0, "sources": {}}
    rendered: dict[str, Any] = {k: v for k, v in report.items() if k not in (*FIELDS, "claims")}
    rendered["fields"], field_results = {}, []
    for name in FIELDS:
        if report.get(name) is None:
            rendered[name] = None
            continue
        record, _ = _record(report[name], datasets, window, checked=True)
        rendered["fields"][name], rendered[name] = record, record["text"]
        field_results.append({"field": name, "status": record["status"], "problems": record["errors"]})
    claims, results, unverified = [], [], 0
    for i, claim in enumerate(report.get("claims", [])):
        checked = claim.get("type") in CHECKED
        record, refs = _record(claim.get("parts") or [], datasets, window, checked=checked)
        explicit = [str(x) for x in claim.get("refs") or []]
        out = {"type": claim.get("type"), **record, "refs": list(dict.fromkeys(explicit + refs)),
               "explicit_refs": explicit, "details": {}}
        for key in DETAILS:  # 引用照常渲染，文字片段里的数字允许
            if claim.get(key):
                detail, _ = _record(claim[key], datasets, window, checked=False)
                out["details"][key], out[key] = detail, detail["text"]
            else:
                out[key] = None
        missing = [ref for ref in out["refs"] if ref not in datasets]
        if checked:
            if missing:
                out["errors"].append(f"引用了不存在的数据集 {'、'.join(missing)}")
            if not out["refs"]:
                out["errors"].append("没有引用数据集：事实与计算类结论必须引用（显式 refs 或引用片段）")
            out["status"] = "unverified" if out["errors"] else "verified"
        else:
            out["errors"] += [e for key in DETAILS for e in out["details"].get(key, {}).get("errors", [])]
        unverified += out["status"] == "unverified"
        claims.append(out)
        results.append({"index": i, "type": out["type"], "status": out["status"], "problems": out["errors"],
                        "missing_refs": missing})
    rendered["claims"] = claims
    unverified_fields = sum(f["status"] == "unverified" for f in field_results)
    ok = unverified == 0 and unverified_fields == 0
    facts = [c for c in claims if c["type"] in CHECKED]
    status = ("failed" if not ok else "no_facts" if not facts else
              "with_inference" if any(c["type"] not in CHECKED for c in claims) else "all_passed")
    # ok 只表示没有未通过的事实或字段（决定是否修订）；“只有推断、没有事实”由 status=no_facts 表示，界面不显示“全部通过”
    verification = {"ok": ok, "status": status, "unverified": unverified, "claims": results,
                    "fields": field_results, "unverified_fields": unverified_fields,
                    "sources": _sources(rendered)}
    return rendered, verification


def _sources(rendered: dict) -> dict[str, int]:
    """数字来源构成：口径（governed）、探索（explore，含以探索数据为输入的计算）、计算（calc）、上下文（context）。"""
    counts = {"governed": 0, "explore": 0, "calc": 0, "context": 0}
    records = [*rendered.get("fields", {}).values()]
    for c in rendered.get("claims", []):
        records += [c, *c.get("details", {}).values()]
    for record in records:
        for s in record.get("segments", []):
            if s["kind"] in NUMERIC:
                key = "explore" if s.get("explore") else s.get("source")
                if key in counts:
                    counts[key] += 1
    return counts
