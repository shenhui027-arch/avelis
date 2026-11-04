// 结果的图：后端给出默认图与可切换的候选（同一份数据），这里只负责切换、渲染与导出。KPI 用 HTML，其余懒加载 ECharts。
import { lazy, Suspense, useRef, useState, type ReactNode } from "react";

import { useNumberStyle } from "../../api/hooks";
import { Icon } from "../Icon";
import { t } from "../../i18n";
import { formatValue } from "../../numbers";
import { changeColorTone, type ChartKind, type ChartKpi, type ChartSpec } from "./chartOption";
import type { EChartHandle } from "./EChart";

const EChart = lazy(() => import("./EChart"));

export const KIND_LABEL: Record<ChartKind, string> = {
  kpi: t("指标卡"), line: t("折线"), bar: t("条形"), grouped_bar: t("分组柱"), stacked_bar: t("堆叠柱"),
  diverging_bar: t("变化"), donut: t("环形"), multiples: t("分图"),
};

/** 图与表切换：view 为 "table" 时只显示 children（表格）。 */
export function ChartPanel({ charts, title, children }: { charts: ChartSpec[]; title: string; children?: ReactNode }) {
  const style = useNumberStyle();
  const [index, setIndex] = useState(0);
  const [view, setView] = useState<"chart" | "table">("chart");
  const canvas = useRef<EChartHandle>(null);
  const spec = charts[Math.min(index, charts.length - 1)];
  if (!spec) return <>{children}</>;
  // 报告插图没有原始结果表：规格里只有画出来的类目，省略了的要去数据集里看
  const table = children ?? <>{spec.omitted_categories ? <p className="av-caption av-chartpanel__note">
    {t("表格只列出图中的类目，完整数据见下方数据集")}</p> : null}<SpecTable spec={spec} /></>;
  if (spec.kind === "kpi") return <KpiCards kpis={spec.kpis} />;
  const download = async () => {
    const url = await canvas.current?.png(textMarks(spec, style) || undefined);
    if (!url) return;
    const a = document.createElement("a");
    a.href = url; a.download = `${title || "chart"}.png`; a.click();
  };
  return (
    <section className="av-chartpanel" aria-label={t("图表")}>
      <div className="av-chartpanel__bar" onClick={(e) => e.stopPropagation()}>
        <div className="av-seg" role="group" aria-label={t("视图")}>
          {charts.map((c, i) => <button key={c.kind} type="button" aria-pressed={view === "chart" && i === index}
            onClick={() => { setIndex(i); setView("chart"); }}>{KIND_LABEL[c.kind]}</button>)}
          <button type="button" aria-pressed={view === "table"} onClick={() => setView("table")}><Icon name="table" small />{t("表格")}</button>
        </div>
        {view === "chart" ? <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={() => void download()} title={t("下载图片")}>
          <Icon name="download" small />PNG</button> : null}
      </div>
      {view === "chart" && spec.title ? <h3 className="av-chartpanel__title">{spec.title}</h3> : null}
      {view === "table" ? table : <>
        <Suspense fallback={<div className="av-chartpanel__loading" />}>
          <EChart ref={canvas} spec={spec} style={style} label={title} />
        </Suspense>
        <ChartNote spec={spec} fullTable={children != null} />
      </>}
    </section>
  );
}

/** 图的表格视图：没有原始结果表时（如报告插图），按规格里的类目与系列列出数值。 */
export function SpecTable({ spec }: { spec: ChartSpec }) {
  const style = useNumberStyle();
  return (
    <div className="av-table-wrap"><table className="av-table av-spec-table">
      <thead><tr><th>{spec.x?.label ?? ""}</th>{spec.series.map((s, i) => <th key={i} className="av-spec-table__num">{s.name}</th>)}</tr></thead>
      <tbody>{spec.categories.map((c, row) => <tr key={row}><td>{c == null ? "—" : String(c)}</td>
        {spec.series.map((s, i) => <td key={i} className="av-spec-table__num">{formatValue(s.values[row], s.unit, style)}</td>)}</tr>)}</tbody>
    </table></div>
  );
}

/** fullTable：表格视图是否为完整结果（问数结果表）；报告插图的表格只含图里的系列，未画出的要去数据集里看。 */
function ChartNote({ spec, fullTable }: { spec: ChartSpec; fullTable: boolean }) {
  const style = useNumberStyle();
  const notes = [
    spec.others ? t("其余 {{count}} 项合并为“其他”", { count: spec.others.count }) : "",
    spec.dropped_series ? (fullTable ? t("另有 {{count}} 个系列未画出，见表格", { count: spec.dropped_series })
      : t("另有 {{count}} 个系列未画出，见下方数据集", { count: spec.dropped_series })) : "",
    omitted(spec, fullTable),
    textMarks(spec, style),
  ].filter(Boolean);
  return notes.length ? <p className="av-caption av-chartpanel__note">{notes.join(" · ")}</p> : null;
}

/** 图上画不出的标注改在图下用文字给出（数值来自规格）：环形图的全部标注，堆叠图的平均值。 */
function textMarks(spec: ChartSpec, style: ReturnType<typeof useNumberStyle>): string {
  if (spec.kind === "stacked_bar") {
    return (spec.marks ?? []).filter((m) => m.type === "average" && m.value != null).map((m) => {
      const s = spec.series[m.series_index];
      return t("{{name}} 平均 {{value}}", { name: s?.name ?? m.series, value: formatValue(m.value, s?.unit, style) });
    }).join(" · ");
  }
  if (spec.kind !== "donut" || !spec.marks?.length) return "";
  const unit = spec.series[0]?.unit;
  return spec.marks.filter((m) => m.value != null).map((m) => m.type === "average"
    ? t("平均 {{value}}", { value: formatValue(m.value, unit, style) })
    : m.type === "max" ? t("最高 {{x}} {{value}}", { x: m.x == null ? "—" : String(m.x), value: formatValue(m.value, unit, style) })
      : t("最低 {{x}} {{value}}", { x: m.x == null ? "—" : String(m.x), value: formatValue(m.value, unit, style) }))
    .join(" · ");
}

/** 类目截断：展示上限截掉的要说出去哪里看全；前 N 是用户自己的设置，只说明还有几项。 */
function omitted(spec: ChartSpec, fullTable: boolean): string {
  const o = spec.omitted_categories;
  if (!o?.count) return "";
  if (o.reason === "top_n") return t("按前 N 项显示，另有 {{count}} 项未列出", { count: o.count });
  return fullTable ? t("另有 {{count}} 个类目未画出，见表格", { count: o.count })
    : t("另有 {{count}} 个类目未画出，见下方数据集", { count: o.count });
}

/** 差额的量纲：比率之差显示为“个百分点”，其余沿用原单位。 */
function deltaUnit(unit: ChartKpi["unit"]): ChartKpi["unit"] {
  return unit.kind === "ratio" && !unit.unit ? { kind: "delta", of_ratio: true } : { ...unit, kind: "delta" };
}

/** 指标卡：本期值为主，有对比期时给出对比值、差额与变化率；颜色只看期望方向，未知时不着色。 */
export function KpiCards({ kpis }: { kpis: ChartKpi[] }) {
  const style = useNumberStyle();
  return (
    <div className="av-kpis">
      {kpis.map((k) => {
        const tone = changeColorTone(k.change, k.polarity);
        return (
          <div key={k.column} className="av-kpi">
            <span className="av-kpi__label">{k.label}</span>
            <span className="av-kpi__value">{formatValue(k.value, k.unit, style)}</span>
            {k.previous != null || k.change != null ? (
              <span className="av-kpi__delta">
                {k.change != null ? <span className={tone ? `is-${tone}` : ""}>
                  {k.change > 0 ? "↑" : k.change < 0 ? "↓" : ""} {formatValue(k.change, deltaUnit(k.unit), style)}
                  {k.rate != null ? ` · ${formatValue(k.rate, { kind: "rate_change" }, style)}` : ""}</span> : null}
                {k.previous != null ? <span className="av-kpi__prev">{k.previous_label ?? t("对比期")} {formatValue(k.previous, k.unit, style)}</span> : null}
              </span>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
