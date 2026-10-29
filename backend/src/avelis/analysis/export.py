"""分析报告导出（ADR 0007 第 11 点）：Markdown、PDF（含图表与证据附录）、数据集 CSV。

导出内容与报告页一致：结论标明类型与引用的数据集，未核实的数字同样标出；附录给出每个数据集的来源
（受治理查询的 SQL 与口径版本，或计算工具的输入）。
PDF 嵌入中文 TrueType 字体（子集）：AVELIS_PDF_FONT 指定的字体文件，否则在常见位置找系统字体（Windows 等线 / 微软雅黑 /
黑体，Linux 文泉驿 / Noto CJK）；都找不到时退回 reportlab 内置的 STSong-Light（不嵌入，需阅读器有亚洲字体包），并在报告中注明。
"""

from __future__ import annotations

import csv
import io
import os
from decimal import Decimal, InvalidOperation
from typing import Any

TYPE_LABEL = {"fact": "事实", "calc": "计算", "inference": "推断", "recommendation": "建议"}
STATUS_LABEL = {"verified": "已核实", "unverified": "未核实", "unchecked": "不核验"}
KIND_LABEL = {"governed": "受治理查询", "calc": "计算", "context": "元数据", "ungoverned": "未治理"}


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
              "- 核验：" + ("全部通过" if verification.get("ok")
                           else f"有 {verification.get('unverified', 0)} 条事实 / 计算类结论未核实"),
              "", "## 结论", "", report.get("summary") or "（无）", ""]
    for claim, check in _claims(view):
        mark = STATUS_LABEL.get(check.get("status"), "")
        bad = [n["text"] for n in check.get("numbers", []) if not n["verified"]]
        refs = "、".join(claim.get("refs") or []) or "—"
        lines.append(f"- **{TYPE_LABEL.get(claim['type'], claim['type'])}**（{refs}；{mark}）{claim['text']}"
                     + (f" ⚠ 未核实的数字：{'、'.join(bad)}" if bad and claim["type"] in ("fact", "calc") else ""))
        if claim.get("premise"):
            lines.append(f"  - 前提：{claim['premise']}")
        if claim.get("validation"):
            lines.append(f"  - 验证方式：{claim['validation']}")
    if report.get("unexplained"):
        lines += ["", f"**仍未解释**：{report['unexplained']}"]
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


def to_csv(columns: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(columns)
    writer.writerows([["" if v is None else v for v in r] for r in rows])
    return ("﻿" + buf.getvalue()).encode("utf-8")  # Excel 识别 UTF-8


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
    """为数据集选一张图：首列为 period 时画折线（趋势），否则对第一个数值列画柱状（贡献 / 对比）。"""
    from reportlab.graphics.charts.barcharts import HorizontalBarChart
    from reportlab.graphics.charts.lineplots import LinePlot
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.lib import colors

    cols, rows = d["columns"], d["rows"]
    if not rows or len(cols) < 2:
        return None
    numeric = [i for i in range(len(cols)) if all(_num(r[i]) is not None for r in rows if r[i] is not None)
               and any(_num(r[i]) is not None for r in rows) and cols[i] not in ("period",)]
    if not numeric:
        return None
    value = next((i for i in numeric if cols[i].endswith("·变化") or cols[i] in ("change", "contribution")),
                 next((i for i in numeric if not cols[i].endswith("变化率") and cols[i] != "change_rate"), numeric[0]))
    drawing = Drawing(460, 200)
    if cols[0] == "period" and len(rows) > 2:
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
    story.append(Paragraph("核验：全部通过" if verification.get("ok")
                           else f"核验：有 {verification.get('unverified', 0)} 条事实 / 计算类结论未核实",
                           base if verification.get("ok") else warn))
    story += [Paragraph("结论", h2), Paragraph(esc(report.get("summary") or "（无）"), base), Spacer(1, 4)]
    for claim, check in _claims(view):
        bad = [n["text"] for n in check.get("numbers", []) if not n["verified"]]
        refs = "、".join(claim.get("refs") or []) or "—"
        text = (f"<b>{TYPE_LABEL.get(claim['type'], claim['type'])}</b>（{refs}；{STATUS_LABEL.get(check.get('status'), '')}）"
                f"{esc(claim['text'])}")
        story.append(Paragraph(text, warn if bad and claim["type"] in ("fact", "calc") else base))
        if bad and claim["type"] in ("fact", "calc"):
            story.append(Paragraph(f"未核实的数字：{esc('、'.join(bad))}", warn))
        for key, label in (("premise", "前提"), ("validation", "验证方式")):
            if claim.get(key):
                story.append(Paragraph(f"{label}：{esc(claim[key])}", small))
    if report.get("unexplained"):
        story += [Spacer(1, 4), Paragraph(f"<b>仍未解释</b>：{esc(report['unexplained'])}", base)]

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
