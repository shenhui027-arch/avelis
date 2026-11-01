(
    """报告核验（ADR 0009 第 2 点）：数字写成引用片段（见 grounding），后端渲染；"""
    """核验只做结构检查，不再从正文里提取数字。

核验层保证“数值来源”：每个事实、计算类数字都来自本次运行的某个数据集单元格或登记的汇总值，显示格式由系统按量纲
决定，渲染过程没有拼接或改写（界面叫“来源已校验”）。它不保证句子语义正确（取错列、错成员、错期间），那部分交给评测。

适用于事实、计算类结论，以及标题、摘要、“未解释”、分析方法（方法的问题只作字段级提示）：
1. 引用必须能解析：数据集、列或汇总路径存在；恰好选中一行；值是有限数值；方向与正负一致（水平值不写方向）；
2. 数值段不能拼接：两个数值段直接相邻，或中间只隔着空白与 . , e E + - ×，判错；数值段后面紧跟的文字不能以
   单位或数量后缀开头（%、倍、成、元、万、亿、个百分点、折），单位只由系统输出；
3. 文字片段里不能有阿拉伯数字（半角、全角），完整的 ASCII 字母加数字识别符除外；"""
    """日期用 WindowRef 或数据集里的日期单元格；
4. 文字片段里不能有中文数量表达（ADR 0009 第 7 点调整，不维护白名单与数量词表）：二至九、十、百、千、万、亿、两、
   零〇 组成的数字后面紧跟量词或单位；“百分之……”“……分之……”；财务大写一律判定。“一”（冠词、序数）、序数与时间词
   （“第二”“九月下旬”）、没有量词的说法（“两期”“两渠道”）都不判定；剩下的中文数量风险交给评测衡量；
5. 事实、计算类结论必须引用至少一个数据集（显式 refs 或引用片段），引用的数据集必须存在。
另外：role=proposal（建议目标值）与“假设计算”（derive 用了非白名单常量）的数据集只能被推断、建议类结论引用。
推断、建议类结论及 premise、validation：引用照常渲染，文字片段里允许出现数字（假设或方案，不是观测值），不核验。

报告整体状态：all_passed（来源全部通过）、with_inference（含未核实的推断）、no_facts（没有可核实的事实）；
有事实、计算类结论或标题、摘要、“未解释”没通过时为 failed；分析方法的问题单独提示，不影响整体状态。
另统计数字来源构成：口径、探索、计算、上下文各有多少个数字。
"""
)

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from avelis.analysis.grounding import CONCLUSIONS, Cause, decode_parts, describe_scope, render

CHECKED = ("fact", "calc")
FIELDS = ("title", "summary", "unexplained", "method")
DETAILS = ("premise", "validation")
NUMERIC = ("value", "count")
DIGITS = re.compile(r"[0-9０-９][0-9０-９.,:/\-]*")
# 只豁免完整的字母前缀加数字标记；ASCII 字母数字边界防止吞掉 1e3、V1x 等真实数字的一部分。
# 中文与标点可直接邻接标记；引用对象和显式 refs 的数据集存在性仍由各自的结构检查保证。
IDENTIFIER_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]+[0-9]+(?![A-Za-z0-9])")
# 中文数量表达（第 2 点第 4 条）：数字由二至九、十、百、千、万、亿、两、零〇 组成（“一”只在与它们相连时算进数字，
# 如“一百”“十一”），从整段数字的开头匹配（前面是“第”的是序数），后面紧跟量词或单位才算数量
_CN_DIGIT = "零〇一二两三四五六七八九十百千万亿"
MEASURES = "个百分点|百分点|分钟|小时|[家个件单笔人次条项元块成倍折%％名位户台张份类种款批天周]"
CN_QUANTITY = re.compile(
    rf"(?<![第{_CN_DIGIT}])([{_CN_DIGIT}]*[零〇二两三四五六七八九十百千万亿][{_CN_DIGIT}]*)\s*(?:{MEASURES})"
)
CN_FRACTION = re.compile(rf"百分之|[{_CN_DIGIT}]+\s*分之")
CN_FINANCIAL = re.compile(r"[壹贰叁肆伍陆柒捌玖拾佰仟]")
JOINER = re.compile(r"^[\s.,eE+\-×]*$")
SUFFIX = re.compile(r"^\s*(?:%|％|倍|成|元|万|亿|个百分点|折)")
STATUS_LABEL = {
    "all_passed": "来源全部通过",
    "with_inference": "含未核实的推断",
    "no_facts": "没有可核实的事实",
    "failed": "有未通过的事实或字段",
}


def _concat_problems(segments: list[dict]) -> list[str]:
    problems, last, between = [], None, ""
    for s in segments:
        if s["kind"] in NUMERIC:
            if last is not None and JOINER.match(between):
                problems.append(
                    f"第 {last['part']}、{s['part']} 个片段：两个数值段之间只隔着“{between}”，会拼成新的数字"
                )
            last, between = s, ""
            continue
        if s["kind"] == "text" and last is not None and between == "" and SUFFIX.match(s["text"]):
            problems.append(
                f"第 {s['part']} 个片段：数值段后面紧跟单位或数量后缀“{SUFFIX.match(s['text']).group(0).strip()}”"
                "，单位由系统按量纲输出"
            )
        between += s["text"] if s["kind"] == "text" else "\0"  # 维度文本、日期等隔开数值段
    return problems


def _text_problems(segments: list[dict]) -> list[str]:
    problems = []
    for s in segments:
        if s["kind"] != "text":
            continue
        text = s["text"]
        numeric_text = IDENTIFIER_TOKEN.sub(" ", text)
        for d in list(dict.fromkeys(DIGITS.findall(numeric_text)))[:3]:
            problems.append(
                f"第 {s['part']} 个片段（文字）出现数字“{d}”：数字请写成引用对象（日期用 WindowRef 或数据集里的日期）"
            )
        found = [m.group(0) for pattern in (CN_QUANTITY, CN_FRACTION, CN_FINANCIAL) for m in pattern.finditer(text)]
        for q in list(dict.fromkeys(found))[:3]:
            problems.append(
                f"第 {s['part']} 个片段（文字）出现中文数量“{q.strip()}”：数量请写成引用对象（个数用 CountRef），"
                "或改写为不含数量的说法"
            )
    return problems


def _restricted_problems(segments: list[dict]) -> list[str]:
    problems = []
    for s in segments:
        if s.get("role") == "proposal":
            problems.append(
                f"第 {s['part']} 个片段（{s['path']}）标为建议目标值（proposal），只能用在推断、建议类结论中"
            )
        if s.get("assumption"):
            problems.append(
                f"第 {s['part']} 个片段（{s['path']}）来自假设计算（公式含非白名单常量），只能被推断、建议类结论引用"
            )
    return problems


def check(segments: list[dict], errors: list[str]) -> list[str]:
    """事实、计算类结论与标题等字段的结构检查：返回问题列表，空表示通过。"""
    return [*errors, *_concat_problems(segments), *_text_problems(segments), *_restricted_problems(segments)]


def _record(parts: Any, datasets: dict[str, dict], window: dict | None, *, checked: bool) -> tuple[dict, list[str]]:
    """一个字段的保存结果：parts、text、segments、status、errors。"""
    parts = decode_parts(parts)
    r = render(parts, datasets, window)
    problems = check(r["segments"], r["errors"]) if checked else list(r["errors"])
    status = ("unverified" if problems else "verified") if checked else "unchecked"
    return {"parts": parts, "text": r["text"], "segments": r["segments"], "status": status, "errors": problems}, r[
        "refs"
    ]


def ground_report(
    report: dict[str, Any],
    datasets: dict[str, dict],
    window: dict | None,
    explanations: dict[str, str] | None = None,
    question_type: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    (
        """渲染模型提交的报告并做结构检查，返回（渲染后的报告，核验结果）。页面与导出都读这份保存结果。
    report：{title, summary, unexplained（片段列表或文字）, claims: [{type, parts, refs, premise, validation}],
    conclusion, causes, metric_code, scope, scope_key}；datasets：见 grounding.resolve；window：本次运行的窗口；
    explanations：本次运行里 explain_change 的判定，键是 grounding.scope_key（unsupported 表示不适用），"""
        """结论类型以它为准；
    question_type：理解阶段判出的问题类型，变化类（change）的报告必须写 scope 并有同范围的判定。"""
    )
    if report.get("incomplete"):  # 后端生成的“未完成”报告：没有引用，原样保留，不核验
        return report, {
            "ok": False,
            "status": "no_facts",
            "unverified": 0,
            "claims": [],
            "fields": [],
            "unverified_fields": 0,
            "sources": {},
        }
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
        out = {
            "type": claim.get("type"),
            **record,
            "refs": list(dict.fromkeys(explicit + refs)),
            "explicit_refs": explicit,
            "details": {},
        }
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
        results.append(
            {
                "index": i,
                "type": out["type"],
                "status": out["status"],
                "problems": out["errors"],
                "missing_refs": missing,
            }
        )
    rendered["claims"] = claims
    rendered["conclusion"] = report.get("conclusion")
    rendered["causes"], cause_results, conclusion_problems = _causes(report, datasets, window)
    conclusion_problems += _conclusion_matches(report, explanations or {}, question_type)
    unverified_fields = sum(f["status"] == "unverified" for f in field_results)
    failed_fields = any(f["status"] == "unverified" and f["field"] != "method" for f in field_results)
    unverified_causes = sum(c["status"] == "unverified" for c in cause_results)
    ok = unverified == 0 and not failed_fields and unverified_causes == 0 and not conclusion_problems
    facts = [c for c in claims if c["type"] in CHECKED]
    status = (
        "failed"
        if not ok
        else "no_facts"
        if not facts
        else "with_inference"
        if any(c["type"] not in CHECKED for c in claims)
        else "all_passed"
    )
    # method 的过程描述只保留字段级提示；ok 决定是否修订事实、计算与报告结论。“只有推断、没有事实”由 no_facts 表示。
    verification = {
        "ok": ok,
        "status": status,
        "unverified": unverified,
        "claims": results,
        "fields": field_results,
        "unverified_fields": unverified_fields,
        "causes": cause_results,
        "unverified_causes": unverified_causes,
        "conclusion": {"value": report.get("conclusion"), "problems": conclusion_problems},
        "sources": _sources(rendered),
    }
    return rendered, verification


FACT_SOURCES = ("governed", "calc", "explore")
# 每类原因的实体字段：任一组写全即可（成员、因子没有口径编码时用证据行 evidence_id）
TYPE_FIELDS = {
    "member": (("metric_code", "dimension", "member"), ("evidence_id",)),
    "factor": (("factor_metric_code",), ("evidence_id",)),
    "calendar": ((),),
    "governance": (("evidence_id",),),
}
EXPLAIN_CONCLUSIONS = ("ranked_reasons", "offsetting", "no_material_change")
WITH_CAUSES = ("ranked_reasons", "offsetting")


def _conclusion_matches(report: dict, explanations: dict[str, str], question_type: str | None = None) -> list[str]:
    (
        """结论类型交给确定性引擎（ADR 0009 第 7 点第 3、9 条）："""
        """报告的 scope（所问指标、筛选与窗口）做过 explain_change 时，
    结论类型必须与同一 scope 的判定一致；只和 scope 完全相同的判定比对。做过 explain_change 却没写 scope 的，要求写明；
    断言了变化情况却没有同范围判定的，要求用这个 scope 调用 explain_change。理解阶段判为变化类的问题，不论结论类型，
    都必须写 scope 并有同范围的判定（判定登记为 unsupported 的除外）。explanations：登记键 → 判定。"""
    )
    key, conclusion = report.get("scope_key"), report.get("conclusion")
    explained = {k: v for k, v in explanations.items() if v != "unsupported"}
    change = question_type == "change"
    if not key:
        if explained:
            return [
                f"本次运行做过自动归因（{'；'.join(describe_scope(k) for k in sorted(explained))}）："
                "请在 scope 写明所问的指标、筛选与窗口"
            ]
        if change and not explanations:
            return ["这是“为什么变化”类问题：请在 scope 写明所问的指标、筛选与窗口，并用这个 scope 调用 explain_change"]
        return []
    verdict = explanations.get(key)
    if verdict is None:
        return (
            [
                f"没有与报告 scope {describe_scope(key)} 相同范围的 explain_change 判定：结论类型由它判定，"
                "请用这个 scope（同样的 metric_code、filters 与窗口）调用 explain_change"
            ]
            if (change or conclusion in EXPLAIN_CONCLUSIONS)
            else []
        )
    if verdict in EXPLAIN_CONCLUSIONS and conclusion != verdict:
        return [
            f"explain_change 对 {describe_scope(key)}的判定是 {verdict}，结论类型应为 {verdict}（不是 {conclusion}）"
        ]
    return []


def _causes(report: dict, datasets: dict[str, dict], window: dict | None) -> tuple[list, list, list[str]]:
    (
        """原因项的核验（ADR 0009 第 5 点）：结构合法；证据存在且类型对；贡献引用能解析、方向与正负一致；"""
        """实体在证据里找得到；
    结论类型与原因列表一致（ranked_reasons 至少一项；offsetting 前两项是最大的减少项与增加项，方向一减一增；
    其余两种不能有原因）。"""
    )
    conclusion = report.get("conclusion")
    raw = report.get("causes") or []
    problems: list[str] = []
    if conclusion not in CONCLUSIONS:
        problems.append(f"结论类型应为 {'、'.join(CONCLUSIONS)} 之一")
    elif conclusion == "ranked_reasons" and not raw:
        problems.append("结论为 ranked_reasons 时必须给出至少一个原因")
    elif conclusion == "offsetting" and {
        c.get("direction") if isinstance(c, dict) else c.direction for c in raw[:2]
    } != {"up", "down"}:
        problems.append("结论为 offsetting 时前两项原因应是最大的减少项与增加项（方向一减一增）")
    elif conclusion not in WITH_CAUSES and raw:
        problems.append(f"结论为 {conclusion} 时不能给出原因排序")
    rendered, results = [], []
    for i, item in enumerate(raw):
        errs: list[str] = []
        try:
            cause = item if isinstance(item, Cause) else Cause.model_validate(item)
        except ValidationError as e:
            err = e.errors()[0]
            errs.append(f"原因结构不合法（{'.'.join(str(x) for x in err.get('loc', ()))}：{err['msg']}）")
            rendered.append({"index": i, "raw": item, "status": "unverified", "errors": errs})
            results.append({"index": i, "status": "unverified", "problems": errs})
            continue
        groups = TYPE_FIELDS[cause.type]
        if not any(all(getattr(cause.entity, f) for f in group) for group in groups):
            errs.append(
                f"{cause.type} 类原因的实体需要写 "
                + " 或 ".join("、".join(f"entity.{f}" for f in group) for group in groups if group)
            )
        evidence = list(dict.fromkeys(cause.evidence + ([cause.contribution.dataset] if cause.contribution else [])))
        for ref in evidence:
            ds = datasets.get(ref)
            if ds is None:
                errs.append(f"证据数据集 {ref} 不存在")
            elif cause.type == "governance" and ds.get("kind") != "context":
                errs.append(f"治理事件的证据 {ref} 必须是 changes / data_health 的上下文证据")
            elif cause.type != "governance" and ds.get("kind") not in FACT_SOURCES:
                errs.append(f"{ref} 是 {ds.get('kind')} 数据集，事实类原因的证据必须是受治理查询、计算或探索结果")
        contribution = None
        if cause.contribution is None and cause.type != "governance":
            errs.append("成员、因子、日历类原因必须引用贡献数值（contribution）")
        elif cause.contribution is not None:
            r = render([cause.contribution], datasets, window)
            contribution = {"text": r["text"], "segments": r["segments"]}
            errs += [e.replace("第 1 个片段", "贡献") for e in r["errors"]]
            seg = r["segments"][0] if r["segments"] else {}
            value = _number(seg.get("raw")) if seg.get("kind") == "value" else None
            if (
                seg.get("measure") in ("delta", "rate_change")
                and value is not None
                and value != 0
                and ((value > 0) != (cause.direction == "up"))
            ):
                errs.append(f"方向与贡献的正负不一致：direction={cause.direction}，原值 {seg.get('raw')}")
            if seg.get("assumption"):
                errs.append("贡献来自假设计算，不能作为原因的证据")
        label = None
        if not errs:
            entity_errors, label = _entity_problems(
                cause, {r: datasets[r] for r in evidence if r in datasets}, report.get("metric_code")
            )
            errs += entity_errors
        status = "unverified" if errs else "verified"
        rendered.append(
            {
                "index": i,
                "type": cause.type,
                "entity": cause.entity.model_dump(exclude_none=True),
                "label": label,
                "direction": cause.direction,
                "contribution": contribution,
                "evidence": evidence,
                "status": status,
                "errors": errs,
            }
        )
        results.append({"index": i, "status": status, "problems": errs})
    if conclusion == "offsetting" and not problems:
        problems += _offsetting_extremes(raw[:2], rendered[:2], datasets)
    return rendered, results, problems


def _offsetting_extremes(raw: list, rendered: list[dict], datasets: dict[str, dict]) -> list[str]:
    """相互抵消时前两项必须分别是归因证据里最大的增加项与最大的减少项：按贡献引用所在数据集的同一列核对数值。"""
    out = []
    for item, shown in zip(raw, rendered):
        try:
            cause = item if isinstance(item, Cause) else Cause.model_validate(item)
        except ValidationError:
            continue
        ref = cause.contribution
        segs = (shown.get("contribution") or {}).get("segments") or []
        value = _number(segs[0].get("raw")) if segs else None
        ds = datasets.get(ref.dataset) if ref is not None else None
        if ref is None or ref.column is None or ds is None or value is None:
            out.append(
                f"原因第 {shown['index'] + 1} 项：相互抵消时贡献要引用归因证据里的贡献列，才能核对是不是最大的"
                f"{'增加' if cause.direction == 'up' else '减少'}项"
            )
            continue
        values = [v for v in (_number(r.get(ref.column)) for r in ds.get("rows") or []) if v is not None]
        side = [v for v in values if (v > 0 if cause.direction == "up" else v < 0)]
        best = (max(side) if cause.direction == "up" else min(side)) if side else None
        if best is None or value != best:
            out.append(
                f"原因第 {shown['index'] + 1} 项：相互抵消时前两项应是最大的增加项与最大的减少项，"
                f"{ref.dataset} 里最大的{'增加' if cause.direction == 'up' else '减少'}项贡献是 {best}，不是 {value}"
            )
    return out


def _entity_problems(cause: Cause, evidence: dict[str, dict], asked: str | None) -> tuple[list[str], str | None]:
    """实体按编码在证据里找：返回（问题，显示名称）。"""
    e = cause.entity
    if e.evidence_id:  # 证据行：所在数据集必须在证据里
        ref, row = e.evidence_id.split("#")
        if ref not in evidence:
            return [f"证据行 {e.evidence_id} 所在的数据集 {ref} 不在证据里"], None
        rows = evidence[ref].get("rows") or []
        if int(row) > len(rows):
            return [f"证据行 {e.evidence_id} 不存在（{ref} 只有 {len(rows)} 行）"], None
        values = [v for v in rows[int(row) - 1].values() if isinstance(v, str) and v.strip() and _number(v) is None]
        return [], " · ".join(values[:4]) or e.evidence_id
    rows = [r for ds in evidence.values() for r in ds.get("rows") or []]
    if cause.type == "member":
        codes = {c for ds in evidence.values() for c in ds.get("metric_codes") or []}
        if codes and e.metric_code not in codes:
            return [f"指标 {e.metric_code} 不在证据数据集的口径里（证据涉及：{'、'.join(sorted(codes))}）"], None
        if not codes and e.metric_code != asked:
            return [f"证据里没有口径编码：成员类原因的 metric_code 只能是所问指标 {asked or '（未写）'}"], None
        found = any(
            str(r.get(e.dimension, "")).strip() == e.member
            or (str(r.get("dimension", "")).strip() == e.dimension and str(r.get("member", "")).strip() == e.member)
            for r in rows
        )
        return ([], e.member) if found else ([f"成员 {e.dimension}={e.member} 不在证据数据集里"], None)
    if cause.type == "factor":
        hit = next((r for r in rows if str(r.get("factor_metric_code") or "").strip() == e.factor_metric_code), None)
        if hit is None:
            return [f"因子指标 {e.factor_metric_code} 不在证据数据集里（因子拆解结果的 factor_metric_code 列）"], None
        return [], str(hit.get("factor") or e.factor_metric_code)
    return [], None


def _number(value):
    from avelis.analysis.grounding import number

    return number(value)


def _sources(rendered: dict) -> dict[str, Any]:
    """数字来源构成（ADR 0009 第 6 点），只统计事实、计算类结论与标题、摘要、未解释里的片段，按出现次数计：
    - direct：成功渲染的数值片段按直接来源计，口径查询 / 探索查询 / 计算三者互斥（CountRef 计入计算；
      changes、data_health 的上下文证据另列 context）；
    - with_explore：其中依赖闭包含探索查询的数量；assumption：其中来自假设计算的数量；
    - windows、labels：日期与维度文本另列；failed：渲染失败的引用。"""
    out: dict[str, Any] = {
        "direct": {"governed": 0, "explore": 0, "calc": 0, "context": 0},
        "with_explore": 0,
        "assumption": 0,
        "windows": 0,
        "labels": 0,
        "failed": 0,
    }
    records = [*rendered.get("fields", {}).values(), *(c for c in rendered.get("claims", []) if c["type"] in CHECKED)]
    for record in records:
        for s in record.get("segments", []):
            if s["kind"] == "error":
                out["failed"] += 1
            elif s["kind"] == "window":
                out["windows"] += 1
            elif s["kind"] == "label":
                out["labels"] += 1
            elif s["kind"] in NUMERIC:
                direct = "calc" if s["kind"] == "count" else s.get("source")
                if direct in out["direct"]:
                    out["direct"][direct] += 1
                out["with_explore"] += bool(s.get("explore"))
                out["assumption"] += bool(s.get("assumption"))
    return out
