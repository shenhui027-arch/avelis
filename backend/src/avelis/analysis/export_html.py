"""自包含 HTML 报告；内容筛选、单位和图表与 PDF 共用保存结果的导出规则。"""

from __future__ import annotations

import re
from html import escape
from xml.etree import ElementTree

from avelis.analysis import export as content
from avelis.i18n import current_locale, gettext

FONT_STACK = "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif"

CSS = f"""
:root {{ color-scheme: light; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #fff; color: {content.DARK}; font-family: {FONT_STACK};
  font-size: 15px; line-height: 1.65; overflow-wrap: anywhere; }}
main {{ max-width: 880px; margin: 0 auto; padding: 40px 28px 48px; }}
h1 {{ margin: 0 0 8px; font-size: 30px; line-height: 1.4; }}
h2 {{ color: {content.BLUE}; font-size: 19px; line-height: 1.5; margin: 28px 0 14px;
  padding-top: 15px; border-top: 1px solid {content.LINE}; }}
h3 {{ font-size: 16px; margin: 20px 0 10px; }}
p {{ margin: 7px 0; }}
.meta, .note, .reference, .detail {{ color: {content.GRAY}; font-size: 12px; }}
.metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
.metric {{ padding: 15px; background: #F8FAFF; border: 1px solid {content.LINE}; border-radius: 9px;
  break-inside: avoid; page-break-inside: avoid; }}
.metric-label {{ color: {content.GRAY}; font-size: 13px; }}
.metric-value {{ display: block; font-size: 23px; line-height: 1.5; font-weight: 650; }}
.positive {{ color: {content.POSITIVE}; }}
.negative, .warning {{ color: {content.NEGATIVE}; }}
.neutral {{ color: {content.GRAY}; }}
.summary {{ margin: 0; padding: 16px 18px; border-radius: 8px; background: #EFF4FF; font-size: 17px; }}
.section-tag {{ color: {content.GRAY}; font-size: 12px; font-weight: 400; }}
ul {{ margin: 10px 0; padding-left: 22px; }}
li {{ padding-left: 2px; margin: 8px 0; }}
.findings li {{ margin: 9px 0; }}
.reference {{ margin-left: 4px; white-space: nowrap; font-size: 11px; }}
.detail {{ margin: 3px 0; }}
.unresolved {{ padding: 10px 16px 10px 34px; background: #F9FAFB; border-left: 3px solid {content.BLUE}; }}
figure {{ margin: 12px 0; }}
svg {{ display: block; width: 100%; height: auto; max-width: 100%; }}
.dataset {{ margin-bottom: 22px; break-inside: avoid; page-break-inside: avoid; }}
.table-scroll {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin: 10px 0; }}
th, td {{ text-align: left; padding: 7px 8px; border-bottom: 1px solid {content.LINE}; vertical-align: top; }}
th {{ background: #F2F4F7; font-weight: 600; }}
pre {{ margin: 8px 0 20px; padding: 12px; background: #F9FAFB; border: 1px solid {content.LINE};
  white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; line-height: 1.55; }}
code {{ font-family: ui-monospace, 'SFMono-Regular', Consolas, monospace; }}
footer {{ margin-top: 26px; }}
@media (max-width: 600px) {{
  main {{ padding: 22px 16px 32px; }}
  h1 {{ font-size: 25px; }}
  .metrics {{ grid-template-columns: 1fr; }}
  .metric {{ padding: 12px 15px; }}
  .metric-value {{ font-size: 22px; }}
  th, td {{ padding: 6px; }}
}}
@media print {{
  @page {{ margin: 15mm; }}
  body {{ font-size: 11pt; }}
  main {{ max-width: none; margin: 0; padding: 0; }}
  body, .metric, .summary, .unresolved, th, pre {{ background: transparent; }}
  h1, h2, h3 {{ break-after: avoid; page-break-after: avoid; }}
  .metric, .dataset, figure, tr {{ break-inside: avoid; page-break-inside: avoid; }}
  .table-scroll {{ overflow: visible; }}
  thead {{ display: table-header-group; }}
  .appendix {{ break-before: page; page-break-before: always; }}
}}
"""


def _text(value) -> str:
    return escape(str(value), quote=True).replace("\n", "<br>")


def _table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    header = "".join(f'<th scope="col">{_text(value)}</th>' for value in rows[0])
    body = "".join("<tr>" + "".join(f"<td>{_text(value)}</td>" for value in row) + "</tr>" for row in rows[1:])
    return f'<div class="table-scroll"><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div>'


def _svg(drawing, identifier: str) -> str:
    """仅序列化程序生成的图形；移除 DTD，隔离图形 ID，并使用阅读设备的系统字体。"""
    from reportlab.graphics import renderSVG

    if drawing is None:
        return ""
    markup = renderSVG.drawToString(drawing)
    root = ElementTree.fromstring(markup[markup.index("<svg") :])
    ElementTree.register_namespace("", "http://www.w3.org/2000/svg")
    identifiers = {
        item.attrib["id"]: f"{identifier}-{item.attrib['id']}" for item in root.iter() if "id" in item.attrib
    }
    for item in root.iter():
        for name, value in list(item.attrib.items()):
            if name == "id":
                item.set(name, identifiers[value])
            elif name == "style":
                value = re.sub(r"font-family:[^;]+", f"font-family: {FONT_STACK}", value)
                item.set(name, re.sub(r"url\(#([^)]*)\)", lambda m: f"url(#{identifiers.get(m[1], m[1])})", value))
            elif value.startswith("#") and value[1:] in identifiers:
                item.set(name, "#" + identifiers[value[1:]])
    root.set("width", "100%")
    root.set("role", "img")
    root.set("aria-label", gettext("已保存数据图表"))
    return "<figure>" + ElementTree.tostring(root, encoding="unicode") + "</figure>"


def _claim(claim: dict, check: dict, *, brief: bool) -> str:
    bad = [] if brief else content._claim_errors(claim, check)
    text = (claim.get("text") or content._rendered(claim)) + (
        " " + gettext("未通过：{error}", error="；".join(bad)) if bad else ""
    )
    value = _text(text)
    if claim.get("type") in ("inference", "recommendation"):
        value += '<small class="note"> ' + gettext("待验证" if brief else "推断，未核实") + "</small>"
    if not brief:
        value += '<sup class="reference">' + _text(content._refs(claim)) + "</sup>"
    for key, label in (
        ("premise", "依据" if brief else "前提"),
        ("validation", "建议这样验证" if brief else "验证方式"),
    ):
        if detail := content._detail(claim, key):
            value += (
                '<p class="detail">' + _text(gettext("{label}：{detail}", label=gettext(label), detail=detail)) + "</p>"
            )
    return ('<li class="warning">' if bad else "<li>") + value + "</li>"


def to_html(
    view: dict,
    *,
    audience: content.Audience | None = None,
    appendix: bool | None = None,
    datasets: list[str] | None = None,
) -> str:
    view = content._export_view(view)
    brief = content.resolve_audience(audience, appendix) == "brief"
    notes = content._brief_footer(view, datasets) if brief else []
    if brief:
        view = content._brief_view(view)
    report, verification = view.get("report") or {}, view.get("verification") or {}
    title = content._field(report, "title") or gettext("分析报告")
    parts = [
        "<!doctype html>",
        f'<html lang="{current_locale()}"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>" + escape(str(title), quote=True) + "</title><style>" + CSS + "</style></head><body><main>",
        "<header><h1>" + _text(title) + '</h1><p class="meta">' + _text(content._header(view)) + "</p></header>",
    ]
    font, _ = content.pdf_font()
    chart_number = 0

    def chart(drawing):
        nonlocal chart_number
        chart_number += 1
        return _svg(drawing, f"chart-{chart_number}")

    metrics = content._core_metrics(view)
    if metrics:
        parts += ["<section><h2>" + _text(gettext("核心指标")) + '</h2><div class="metrics">']
        for metric in metrics:
            sign = ""
            if metric["measure"] in ("delta", "rate_change"):
                sign = "negative" if metric["raw"] < 0 else "positive" if metric["raw"] > 0 else "neutral"
            parts.append(
                '<div class="metric"><span class="metric-label">'
                + _text(metric["label"])
                + f'</span><strong class="metric-value {sign}">'
                + _text(metric["text"])
                + "</strong></div>"
            )
        parts += ["</div></section>"]
    parts += [
        "<section><h2>"
        + _text(gettext("结论"))
        + '</h2><p class="summary">'
        + _text(content._field(report, "summary") or gettext("（无）"))
        + "</p>"
    ]
    if not brief:
        parts += [
            '<p class="warning">' + _text(gettext("未通过（{error}）", error=value)) + "</p>"
            for value in content._bad_fields(verification)
        ]
        parts += [
            '<p class="warning">' + _text(gettext("结论未通过：{error}", error=value)) + "</p>"
            for value in (verification.get("conclusion") or {}).get("problems") or []
        ]
    parts += ["</section>"]
    if report.get("causes"):
        tag = gettext(content.CONCLUSION.get(report.get("conclusion"), ""))
        tag_html = ' <span class="section-tag">' + _text(tag) + "</span>" if tag else ""
        parts += ["<section><h2>" + _text(gettext("主要原因")) + tag_html + "</h2>"]
        numeric = [cause for cause in report["causes"] if content._cause_value(cause)[0] is not None]
        saved = {dataset["ref"]: dataset for dataset in view.get("datasets") or []}
        data = [
            (content._cause_label(cause), float(raw), text)
            for cause in numeric
            for raw, text in [content._cause_value(cause, saved)]
        ]
        parts.append(
            chart(
                content._bar_chart(
                    data,
                    font,
                    references=None if brief else [content._refs({"refs": cause.get("evidence")}) for cause in numeric],
                )
            )
        )
        fallback = []
        for cause in report["causes"]:
            bad = (cause.get("errors") or []) if not brief else []
            if content._cause_value(cause)[0] is None:
                text = content._cause_text(cause) + (
                    " " + gettext("未通过：{error}", error="；".join(bad)) if bad else ""
                )
                citation = (
                    ""
                    if brief
                    else ('<sup class="reference">' + _text(content._refs({"refs": cause.get("evidence")})) + "</sup>")
                )
                fallback.append(('<li class="warning">' if bad else "<li>") + _text(text) + citation + "</li>")
            elif bad:
                fallback.append(
                    '<li class="warning">'
                    + _text(content._cause_label(cause) + " " + gettext("未通过：{error}", error="；".join(bad)))
                    + "</li>"
                )
        if fallback:
            parts += ["<ul>" + "".join(fallback) + "</ul>"]
        parts += ["</section>"]
    for heading, types in (("关键发现", ("fact", "calc")), ("判断与建议", ("inference", "recommendation"))):
        claims = [
            (claim, check)
            for claim, check in content._claims(view)
            if claim.get("type") in types and not content._context_claim(claim, view)
        ]
        if claims:
            parts += [
                "<section><h2>" + _text(gettext(heading)) + '</h2><ul class="findings">',
                *[_claim(claim, check, brief=brief) for claim, check in claims],
                "</ul></section>",
            ]
    if unexplained := content._field(report, "unexplained"):
        parts += [
            "<section><h2>" + _text(gettext("还没查清")) + '</h2><ul class="unresolved">',
            *["<li>" + _text(item.strip()) + "</li>" for item in unexplained.split("；") if item.strip()],
            "</ul></section>",
        ]
    selected = content._key_datasets(view, datasets)
    if selected:
        parts += ["<section><h2>" + _text(gettext("关键数据")) + "</h2>"]
        for dataset in selected:
            dataset = content._brief_dataset(dataset, view) if brief else dataset
            name = content._dataset_title(dataset) if brief else f"{dataset['ref']} {dataset['title']}"
            parts += [
                '<article class="dataset"><h3>' + _text(name) + "</h3>",
                chart(content._chart(dataset, font, brief=brief)),
                _table(content._table_data(dataset, brief=brief)),
                '<p class="note">' + _text(content._row_note(dataset)) + "</p></article>",
            ]
        parts += ["</section>"]
    if brief:
        parts += ["<footer>" + "".join('<p class="note">' + _text(note) + "</p>" for note in notes) + "</footer>"]
    else:
        used = content._appendix_datasets(view, selected)
        parts += ['<section class="appendix"><h2>' + _text(gettext("附录")) + "</h2>"]
        if method := content._field(report, "method"):
            parts += ['<p class="note">' + _text(gettext("分析方法：{method}", method=method)) + "</p>"]
        parts += [
            "<h3>"
            + _text(gettext("数据检查"))
            + '</h3><p class="note">'
            + _text(content._data_check_note(view))
            + "</p>",
            "<h3>" + _text(gettext("用到的数据")) + "</h3>",
            _table(content._dataset_listing(used)),
            "<h3>" + _text(gettext("查询明细")) + "</h3>",
        ]
        for dataset in used:
            if dataset.get("restricted") or dataset.get("kind") not in ("governed", "explore"):
                continue
            versions = dataset.get("metric_version_ids") or content._meta(dataset).get("metric_version_ids") or []
            parts += [
                '<p class="note">'
                + _text(
                    f"{dataset['ref']} {content._dataset_title(dataset)} · "
                    + gettext("口径版本：{versions}", versions="、".join(versions) or gettext("未登记"))
                )
                + "</p>",
                "<pre><code>"
                + escape(
                    str(dataset.get("sql") or content._meta(dataset).get("sql") or gettext("（未保存 SQL）")),
                    quote=True,
                )
                + "</code></pre>",
            ]
        parts += ["</section>"]
    return "\n".join([*parts, "</main></body></html>"])
