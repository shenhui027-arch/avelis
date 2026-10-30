"""分析报告导出（ADR 0007 第 11 点）：Markdown、PDF（含图表与证据附录）、数据集 CSV。

导出内容与报告页读同一份保存结果：结论标明类型与引用的数据集，未通过的结论与字段同样标出原因，推断 / 建议标为未核实；
附录给出每个数据集的来源（受治理查询的 SQL 与口径版本，或计算工具的输入）与报告里每个数字的来源。
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
from decimal import Decimal, InvalidOperation
from typing import Any

TYPE_LABEL = {"fact": "事实", "calc": "计算", "inference": "推断", "recommendation": "建议"}
STATUS_LABEL = {"verified": "来源已校验", "unverified": "未通过", "unchecked": "推断 / 建议，未核实"}
KIND_LABEL = {"governed": "受治理查询", "calc": "计算", "context": "上下文证据", "explore": "探索查询", "ungoverned": "未治理"}
REPORT_STATUS = {"all_passed": "来源全部通过", "with_inference": "来源全部通过，含未核实的推断",
                 "no_facts": "没有可核实的事实", "failed": "有未通过的事实或字段"}
MEASURE_LABEL = {"level": "水平值", "delta": "差额", "ratio": "比率", "rate_change": "变化率", "share": "占比",
                 "count": "计数", "unknown": "未知"}
SOURCE_LABEL = {"governed": "口径", "explore": "探索", "calc": "计算", "context": "上下文"}
EXPLORE_MARK = "探索查询（未经口径审核）"


REF = re.compile(r"^D[1-9]\d{0,3}$")
FIELD_LABEL = {"title": "标题", "summary": "摘要", "unexplained": "未解释"}


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


def _composition(verification: dict) -> str:
    """数字来源构成（两个维度）：直接来源（口径 / 探索 / 计算 / 上下文），其中含探索来源、假设的数量；日期、标签、
    渲染失败另列。"""
    sources = verification.get("sources") or {}
    direct = sources.get("direct") or {}
    parts = [f"{SOURCE_LABEL[k]} {direct[k]}" for k in SOURCE_LABEL if direct.get(k)]
    extra = [f"{label} {sources[k]}" for k, label in (("with_explore", "其中含探索来源"), ("assumption", "其中假设"),
                                                     ("windows", "日期"), ("labels", "标签"), ("failed", "渲染失败"))
             if sources.get(k)]
    return "、".join(parts + extra) or "无"


def _segments(report: dict) -> list[tuple[str, str, dict]]:
    """报告里全部引用段：（位置，类型，段）。"""
    out = []
    for name in ("title", "summary", "unexplained"):
        for seg in ((report.get("fields") or {}).get(name) or {}).get("segments") or []:
            out.append((FIELD_LABEL[name], "字段", seg))
    for i, claim in enumerate(report.get("claims", [])):
        kind = TYPE_LABEL.get(claim.get("type"), claim.get("type") or "")
        for seg in claim.get("segments") or []:
            out.append((f"结论 {i + 1}", kind, seg))
    for i, cause in enumerate(report.get("causes") or []):
        for seg in (cause.get("contribution") or {}).get("segments") or []:
            out.append((f"原因 {i + 1}", "原因", seg))
        for key, label in (("premise", "前提"), ("validation", "验证方式")):
            for seg in ((claim.get("details") or {}).get(key) or {}).get("segments") or []:
                out.append((f"结论 {i + 1} {label}", kind, seg))
    return [x for x in out if x[2].get("kind") in ("value", "count", "label", "window")]


CAUSE_TYPE = {"member": "成员", "factor": "因子", "calendar": "日历", "governance": "治理事件"}
CONCLUSION = {"ranked_reasons": "有排序的原因", "no_material_change": "没有实质变化", "insufficient_evidence": "证据不足"}


def _cause_lines(report: dict) -> list[str]:
    """原因排序（ADR 0009 第 5 点）：结论类型与按影响大小排列的原因，导出与页面读同一份保存结果。"""
    conclusion, causes = report.get("conclusion"), report.get("causes") or []
    if not conclusion and not causes:
        return []
    lines = ["", "## 原因排序", f"结论类型：{CONCLUSION.get(conclusion, conclusion or '—')}"]
    for i, c in enumerate(causes, start=1):
        e = c.get("entity") or {}
        entity = f"{e.get('dimension', '')}：{e['member']}" if e.get("member") else e.get("factor") or e.get("event") or "日历因素"
        contribution = (c.get("contribution") or {}).get("text")
        bad = "；".join(c.get("errors") or [])
        lines.append(f"{i}. {CAUSE_TYPE.get(c.get('type'), c.get('type') or '原因')} · {entity} · "
                     f"{ {'up': '上升', 'down': '下降'}.get(c.get('direction'), '') }"
                     + (f" · 贡献 {contribution}" if contribution else "")
                     + f"（证据 {'、'.join(c.get('evidence') or []) or '—'}）" + (f" ⚠ {bad}" if bad else ""))
    return lines


def _source_note(seg: dict) -> str:
    raw = seg.get("raw")
    bits = [seg.get("label") or seg.get("path"), f"原值 {' ~ '.join(raw) if isinstance(raw, list) else raw}"]
    if seg.get("measure"):
        bits.append(MEASURE_LABEL.get(seg["measure"], seg["measure"]) + (f"（{seg['unit']}）" if seg.get("unit") else ""))
    if seg.get("explore"):
        bits.append(EXPLORE_MARK)
    if seg.get("role") == "proposal":
        bits.append("建议目标（非观测值）")
    if seg.get("assumption"):
        bits.append("假设计算")
    return " · ".join(str(b) for b in bits if b)


def _bad_fields(verification: dict) -> list[str]:
    return [f"{FIELD_LABEL.get(f['field'], f['field'])}：{'；'.join(f.get('problems') or [])}"
            for f in verification.get("fields", []) if f.get("status") == "unverified"]


def _claims(view: dict) -> list[tuple[dict, dict]]:
    claims = (view.get("report") or {}).get("claims", [])
    checks = {c["index"]: c for c in (view.get("verification") or {}).get("claims", [])}
    return [(c, checks.get(i, {})) for i, c in enumerate(claims)]


def to_markdown(view: dict) -> str:
    report = view.get("report") or {}
    verification = view.get("verification") or {}
    lines = [f"# {report.get('title') or '分析报告'}", "", f"> 问题：{view['question']}", ""]
    if view.get("window", {}).get("start"):
        w = view["window"]
        lines.append(f"- 分析窗口：{w['start']} ~ {w['end']}" +
                     (f"；对比期 {w['compare_start']} ~ {w['compare_end']}" if w.get("compare_start") else ""))
    lines += [f"- 运行：{view['id']}（{view['status']}，模型 {view.get('model_adapter') or '—'}，"
              f"完成于 {view.get('finished_at') or '—'}）",
              "- 核验：" + _verdict(verification),
              "- 数字来源：" + _composition(verification),
              *[f"- ⚠ 未通过（{x}）" for x in _bad_fields(verification)],
              "", "## 结论", "", report.get("summary") or "（无）", ""]
    for claim, check in _claims(view):
        mark = STATUS_LABEL.get(check.get("status"), "")
        bad = check.get("problems") or [] if check.get("status") == "unverified" else []
        refs = _refs(claim)
        lines.append(f"- **{TYPE_LABEL.get(claim['type'], claim['type'])}**（{refs}；{mark}）{claim.get('text') or ''}"
                     + (f" ⚠ {'；'.join(bad)}" if bad else ""))
        if claim.get("premise"):
            lines.append(f"  - 前提：{claim['premise']}")
        if claim.get("validation"):
            lines.append(f"  - 验证方式：{claim['validation']}")
    lines += _cause_lines(report)
    if report.get("unexplained"):
        lines += ["", f"**仍未解释**：{report['unexplained']}"]
    sources = _segments(report)
    if sources:
        lines += ["", "## 附录：数值来源", ""]
        lines += [f"- {where}（{kind}）“{seg.get('text')}”：{_source_note(seg)}" for where, kind, seg in sources]
    lines += ["", "## 附录：数据集", ""]
    for d in view.get("datasets", []):
        lines += [f"### {d['ref']} {d['title']}", "", f"- 类型：{KIND_LABEL.get(d['kind'], d['kind'])}；"
                  f"{d['row_count']} 行" + ("（已截断）" if d.get("truncated") else "")]
        if d.get("inputs"):
            lines.append(f"- 输入：{'、'.join(d['inputs'])}")
        if d.get("metric_version_ids"):
            lines.append(f"- 口径版本：{'、'.join(d['metric_version_ids'])}")
        if d.get("sql"):
            lines += ["", "```sql", d["sql"], "```"]
        lines += ["", "| " + " | ".join(map(str, d["columns"])) + " |", "|" + "---|" * len(d["columns"])]
        lines += ["| " + " | ".join("" if v is None else str(v) for v in row) + " |" for row in d["rows"][:30]]
        if d["row_count"] > 30:
            lines.append(f"\n（只列出前 30 行，完整数据请导出 CSV）")
        lines.append("")
    lines += ["## 附录：步骤", ""]
    for s in view.get("steps", []):
        lines.append(f"{s['seq']}. {s.get('tool') or s['kind']}（{s['status']}）"
                     + (f"：{s['reason']}" if s.get("reason") else "") + (f" → {s['dataset']}" if s.get("dataset") else ""))
    lines += ["", "> 可追溯 ≠ 可复现：保存了 SQL、口径版本与结果，不保证能按当时的数据重算。", ""]
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


SOURCE_COLUMNS = ["位置", "类型", "显示值", "来源标签", "数据集", "取值路径", "量纲", "单位", "格式", "方向", "标记", "原值"]


def sources_csv(view: dict) -> bytes:
    """引用来源表：报告里每个引用段来自哪个数据集的哪个值（ADR 0009）。"""
    rows = []
    for where, kind, s in _segments(view.get("report") or {}):
        raw = s.get("raw")
        marks = [m for m, on in ((EXPLORE_MARK, s.get("explore")), ("建议目标（非观测值）", s.get("role") == "proposal"),
                                 ("假设计算", s.get("assumption")), ("模型声明量纲", s.get("declared"))) if on]
        rows.append([where, kind, s.get("text"), s.get("label"), s.get("ref"), s.get("path"),
                     MEASURE_LABEL.get(s.get("measure"), s.get("measure") or ""), s.get("unit") or "",
                     s.get("format") or "", {"up": "上升", "down": "下降"}.get(s.get("direction"), ""), "；".join(marks),
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
            return "AvelisCJK", True
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    _FONT.update(name="STSong-Light", embedded=False)
    return "STSong-Light", False


def _num(v: Any) -> float | None:
    try:
        return float(Decimal(str(v)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _chart(d: dict, font: str):
    """为数据集选一张图：首列是保存的时间段列（time_column）时画折线（趋势），否则对第一个数值列画柱状（贡献 / 对比）。"""
    from reportlab.graphics.charts.barcharts import HorizontalBarChart
    from reportlab.graphics.charts.lineplots import LinePlot
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.lib import colors

    cols, rows = d["columns"], d["rows"]
    if not rows or len(cols) < 2:
        return None
    trend = bool(d.get("time_column")) and cols[0] == d["time_column"]
    numeric = [i for i in range(len(cols)) if all(_num(r[i]) is not None for r in rows if r[i] is not None)
               and any(_num(r[i]) is not None for r in rows) and not (trend and i == 0)]
    if not numeric:
        return None
    value = next((i for i in numeric if cols[i].endswith("·变化") or cols[i] in ("change", "contribution")),
                 next((i for i in numeric if not cols[i].endswith("变化率") and cols[i] != "change_rate"), numeric[0]))
    drawing = Drawing(460, 200)
    if trend and len(rows) > 2:
        series: dict[str, list[tuple[float, float]]] = {}
        periods = sorted({str(r[0]) for r in rows})
        index = {p: i for i, p in enumerate(periods)}
        for r in rows:
            key = " / ".join(str(x) for x in r[1:value]) or cols[value]
            v = _num(r[value])
            if v is not None:
                series.setdefault(key, []).append((index[str(r[0])], v))
        lp = LinePlot()
        lp.x, lp.y, lp.width, lp.height = 40, 30, 400, 150
        lp.data = [sorted(points) for points in list(series.values())[:6]]
        palette = [colors.HexColor(c) for c in ("#2F6BEA", "#E5484D", "#30A46C", "#F5A524", "#8E4EC6", "#12A594")]
        for i in range(len(lp.data)):
            lp.lines[i].strokeColor = palette[i % len(palette)]
        lp.xValueAxis.valueSteps = list(range(0, len(periods), max(1, len(periods) // 6)))
        lp.xValueAxis.labelTextFormat = lambda x: periods[int(x)][-5:] if 0 <= int(x) < len(periods) else ""
        lp.xValueAxis.labels.fontName = lp.yValueAxis.labels.fontName = font
        drawing.add(lp)
        for i, name in enumerate(list(series)[:6]):
            drawing.add(String(45 + i * 70, 190, name[:8], fontName=font, fontSize=7,
                               fillColor=palette[i % len(palette)]))
        return drawing
    labels = [i for i in range(len(cols)) if i not in numeric][:2]  # 前导的文字列作为类目
    data = [(" / ".join(str(r[i]) for i in labels if r[i] is not None) or str(n + 1), _num(r[value]))
            for n, r in enumerate(rows[:12])]
    data = [(k, v) for k, v in data if v is not None]
    if not data:
        return None
    bc = HorizontalBarChart()
    bc.x, bc.y, bc.width, bc.height = 110, 22, 330, 165
    bc.categoryAxis.labelAxisMode = "low"  # 有负值时类目标签也放在图左侧，不压在柱子上
    bc.data = [[v for _, v in data]]
    bc.categoryAxis.categoryNames = [k[:12] for k, _ in data]
    bc.categoryAxis.labels.fontName = bc.valueAxis.labels.fontName = font
    bc.bars[0].fillColor = colors.HexColor("#2F6BEA")
    drawing.add(bc)
    drawing.add(String(110, 192, cols[value], fontName=font, fontSize=8))
    return drawing


def to_pdf(view: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font, embedded = pdf_font()
    base = ParagraphStyle("base", fontName=font, fontSize=9.5, leading=14, wordWrap="CJK")
    h1 = ParagraphStyle("h1", parent=base, fontSize=16, leading=22, spaceAfter=6)
    h2 = ParagraphStyle("h2", parent=base, fontSize=12, leading=18, spaceBefore=10, spaceAfter=4)
    small = ParagraphStyle("small", parent=base, fontSize=7.5, leading=10, textColor=colors.HexColor("#555555"))
    warn = ParagraphStyle("warn", parent=base, textColor=colors.HexColor("#B42318"))

    def esc(text: Any) -> str:
        return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    report, verification = view.get("report") or {}, view.get("verification") or {}
    story = [Paragraph(esc(report.get("title") or "分析报告"), h1), Paragraph(f"问题：{esc(view['question'])}", base)]
    w = view.get("window") or {}
    if w.get("start"):
        story.append(Paragraph(f"分析窗口：{w['start']} ~ {w['end']}" + (
            f"；对比期 {w['compare_start']} ~ {w['compare_end']}" if w.get("compare_start") else ""), base))
    story.append(Paragraph(f"运行 {view['id']} · {view['status']} · 模型 {esc(view.get('model_adapter') or '—')} · "
                           f"完成于 {view.get('finished_at') or '—'}", small))
    story.append(Paragraph(f"核验：{_verdict(verification)}", base if verification.get("ok") else warn))
    story.append(Paragraph(f"数字来源：{esc(_composition(verification))}", small))
    for x in _bad_fields(verification):
        story.append(Paragraph(f"未通过（{esc(x)}）", warn))
    story += [Paragraph("结论", h2), Paragraph(esc(report.get("summary") or "（无）"), base), Spacer(1, 4)]
    for claim, check in _claims(view):
        bad = check.get("problems") or [] if check.get("status") == "unverified" else []
        text = (f"<b>{esc(TYPE_LABEL.get(claim['type'], claim['type']))}</b>（{esc(_refs(claim))}；"
                f"{STATUS_LABEL.get(check.get('status'), '')}）{esc(claim.get('text') or '')}")
        story.append(Paragraph(text, warn if bad else base))
        if bad:
            story.append(Paragraph(f"未通过：{esc('；'.join(bad))}", warn))
        for key, label in (("premise", "前提"), ("validation", "验证方式")):
            if claim.get(key):
                story.append(Paragraph(f"{label}：{esc(claim[key])}", small))
    causes = _cause_lines(report)
    if causes:  # causes[0:2] 是 Markdown 的空行与标题
        story.append(Paragraph("原因排序", h2))
        story += [Paragraph(esc(line), warn if "⚠" in line else base) for line in causes[2:]]
    if report.get("unexplained"):
        story += [Spacer(1, 4), Paragraph(f"<b>仍未解释</b>：{esc(report['unexplained'])}", base)]
    sources = _segments(report)
    if sources:
        story.append(Paragraph("数值来源", h2))
        story += [Paragraph(f"{esc(where)}（{esc(kind)}）“{esc(seg.get('text'))}”：{esc(_source_note(seg))}", small)
                  for where, kind, seg in sources]

    story.append(Paragraph("图表与数据集", h2))
    for d in view.get("datasets", []):
        block = [Paragraph(f"<b>{d['ref']}</b> {esc(d['title'])}（{KIND_LABEL.get(d['kind'], d['kind'])}，"
                           f"{d['row_count']} 行）", base)]
        chart = _chart(d, font)
        if chart is not None:
            block.append(chart)
        cols = [str(c) for c in d["columns"]][:8]
        table = [[Paragraph(esc(c), small) for c in cols]] + [
            [Paragraph(esc("" if v is None else v), small) for v in r[:8]] for r in d["rows"][:15]]
        t = Table(table, repeatRows=1, colWidths=[170 * mm / max(1, len(cols))] * len(cols))
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CCCCCC")),
                               ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF2FB"))]))
        block += [t, Spacer(1, 6)]
        story.append(KeepTogether(block[:2]) if chart is not None else block[0])
        story += block[2:] if chart is not None else block[1:]

    story.append(Paragraph("附录：证据", h2))
    for d in view.get("datasets", []):
        story.append(Paragraph(f"<b>{d['ref']}</b> {KIND_LABEL.get(d['kind'], d['kind'])}" +
                               (f"；输入 {'、'.join(d['inputs'])}" if d.get("inputs") else "") +
                               (f"；口径版本 {'、'.join(d['metric_version_ids'])}" if d.get("metric_version_ids") else ""),
                               small))
        if d.get("sql"):
            story.append(Paragraph(esc(d["sql"]).replace("\n", "<br/>"), small))
    story.append(Paragraph("步骤", h2))
    for s in view.get("steps", []):
        story.append(Paragraph(f"{s['seq']}. {esc(s.get('tool') or s['kind'])}（{s['status']}）"
                               + (f"：{esc(s['reason'])}" if s.get("reason") else "")
                               + (f" → {s['dataset']}" if s.get("dataset") else ""), small))
    story.append(Spacer(1, 6))
    story.append(Paragraph("可追溯 ≠ 可复现：保存了 SQL、口径版本与结果，不保证能按当时的数据重算。", small))
    if not embedded:
        story.append(Paragraph("未找到可嵌入的中文字体（AVELIS_PDF_FONT）：中文需阅读器安装亚洲字体包才能显示。", small))
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                      bottomMargin=16 * mm, title=report.get("title") or "分析报告", author="Avelis").build(story)
    return buf.getvalue()
