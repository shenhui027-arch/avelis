// 数据集：证据栏里的卡片可以在主区域打开，看完整表格（分页）、图表、SQL 与口径版本；受治理数据集可以换时间窗口重跑。
// 地址带 ?dataset=D3，可以直接分享到具体数据集。
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router";

import { api, unwrap, type Schemas } from "../../api/client";
import { errorText, useAction } from "../../api/hooks";
import { Code, Drawer, Empty, Icon, Pill } from "../../components/ui";
import { BarChart, LineChart } from "./charts";
import { t } from "../../i18n";

type Detail = Schemas["AnalysisDatasetDetailOut"];
type Chartable = { columns: string[]; rows: unknown[][]; time_column?: string | null };

export const DATASET_KIND: Record<string, [string, "ok" | "warn" | "info"]> = { governed: [t("受治理"), "ok"], calc: [t("计算"), "info"],
  context: [t("上下文"), "info"], explore: [t("探索"), "warn"], ungoverned: [t("未治理"), "warn"] };
const PAGE = 100;

function numeric(v: unknown): number | null {
  const n = typeof v === "number" ? v : typeof v === "string" && v.trim() !== "" ? Number(v) : NaN;
  return Number.isFinite(n) ? n : null;
}

/** 首列是保存的时间段列（time_column）时画趋势；否则对 change / contribution（或第一个数值列）画贡献条。空值不画。 */
export function DatasetChart({ d, limit = 12 }: { d: Chartable; limit?: number }) {
  const cols = d.columns;
  const trend = !!d.time_column && cols[0] === d.time_column;
  const numCols = cols.map((_, i) => i).filter((i) => !(trend && i === 0) && d.rows.some((r) => numeric(r[i]) !== null)
    && d.rows.every((r) => r[i] == null || numeric(r[i]) !== null));
  if (!d.rows.length || !numCols.length) return null;
  const value = numCols.find((i) => cols[i].endsWith("·变化") || cols[i] === "change" || cols[i] === "contribution")
    ?? numCols.find((i) => !cols[i].endsWith("变化率") && cols[i] !== "change_rate") ?? numCols[0];
  if (trend && d.rows.length > 2) {
    const series = new Map<string, { x: string; y: number }[]>();
    for (const r of d.rows) {
      const key = r.slice(1, value).map(String).join(" / ") || cols[value];
      const y = numeric(r[value]);
      if (y !== null) series.set(key, [...(series.get(key) ?? []), { x: String(r[0]), y }]);
    }
    return <LineChart series={[...series].slice(0, 6).map(([name, points]) => ({ name, points }))} label={cols[value]} />;
  }
  if (d.rows.length < 2) return null;
  const labelCols = cols.map((_, i) => i).filter((i) => !numCols.includes(i)).slice(0, 2);
  const bars = d.rows.slice(0, limit).flatMap((r, i) => {
    const v = numeric(r[value]);
    return v === null ? [] : [{ label: labelCols.map((c) => String(r[c] ?? "")).join(" / ") || String(i + 1), value: v }];
  });
  return bars.length > 1 ? <BarChart bars={bars} label={cols[value]} /> : null;
}

/** 当前打开的数据集（地址里的 ?dataset=）与打开、关闭的方法。 */
export function useOpenDataset() {
  const [params, setParams] = useSearchParams();
  const ref = params.get("dataset");
  const set = (next: string | null) => setParams((prev) => {
    const p = new URLSearchParams(prev);
    if (next) p.set("dataset", next); else p.delete("dataset");
    return p;
  });
  return { ref, open: (r: string) => set(r), close: () => set(null) };
}

function useDetail(runId: string, ref: string, offset: number) {
  return useQuery({
    queryKey: ["analysis-dataset", runId, ref, offset], placeholderData: (prev) => prev,
    queryFn: () => unwrap(api.GET("/api/analysis/runs/{run_id}/datasets/{ref}", {
      params: { path: { run_id: runId, ref }, query: { offset, limit: PAGE } } })) as Promise<Detail>,
  });
}

export function DatasetView({ runId, refId, onClose, onOpen, onDownload, canRerun }: {
  runId: string; refId: string; onClose: () => void; onOpen: (ref: string) => void;
  onDownload: (seq: number, ref: string) => void; canRerun: boolean;
}) {
  const [offset, setOffset] = useState(0);
  const [rerun, setRerun] = useState(false);
  const detail = useDetail(runId, refId, offset);
  const d = detail.data;
  const forbidden = detail.isError && (detail.error as { status?: number }).status === 403;
  const [label, tone]: [string, "ok" | "warn" | "info"] = d ? DATASET_KIND[d.kind] ?? [d.kind, "info"] : ["", "info"];
  const w = (d?.window ?? {}) as { start?: string; end?: string };
  const pages = d ? Math.max(1, Math.ceil(d.total / PAGE)) : 1;
  return (
    <div className="av-dsview">
      <header className="av-dsview__hd">
        <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={onClose}>{t("← 返回报告")}</button>
        <div className="av-dsview__title">
          <span className="av-tag av-mono">{refId}</span>
          <h1 title={d?.title}>{d?.title ?? t("数据集")}</h1>
          {d ? <Pill tone={tone}>{label}</Pill> : null}
          {d?.explore ? <Pill tone="warn">{t("含探索来源")}</Pill> : null}
          {d?.assumption ? <Pill tone="warn">{t("含假设")}</Pill> : null}
          {d?.partial ? <Pill tone="warn">{t("部分结果")}</Pill> : null}
        </div>
        {d ? <div className="av-actions">
          {d.rerunnable && canRerun ? <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => setRerun(true)}>
            <Icon name="refresh" small />{t("换时间重跑")}</button> : null}
          <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={() => onDownload(Number(refId.slice(1)), refId)}>
            <Icon name="download" small />CSV</button>
        </div> : null}
      </header>
      {forbidden ? <Empty text={t("你已没有该数据的访问权限")} /> : detail.isError ? <div className="av-banner av-banner--danger">{errorText(detail.error)}</div> : null}
      {d ? <div className="av-dsview__bd">
        <p className="av-caption av-dsview__meta">
          {t("共 {{count}} 行", { count: d.total })}{d.truncated ? t(" · 结果被截断，不代表来源全集") : ""}
          {w.start ? t(" · 窗口 {{start}} ~ {{end}}", { start: w.start, end: w.end }) : ""}
          {d.inputs.length ? t(" · 输入 {{v1}}", { v1: d.inputs.join(t("、")) }) : ""}
          {d.source ? <> {t("· 由")} <button type="button" className="av-dsview__link" onClick={() => onOpen(d.source!.ref)}>{d.source.ref}</button> {t("换时间重跑")}</> : null}
        </p>
        {d.purpose ? <p className="av-dsview__purpose">{d.purpose}</p> : null}
        {offset === 0 ? <div className="av-dsview__chart"><DatasetChart d={d} limit={20} /></div> : null}
        <div className="av-table-wrap av-dsview__table"><table className="av-table">
          <thead><tr>{d.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
          <tbody>{d.rows.map((row, i) => <tr key={offset + i}>{row.map((v, j) => <td key={j} className="av-mono">{v == null ? "—" : String(v)}</td>)}</tr>)}</tbody>
        </table></div>
        {pages > 1 ? <div className="av-dsview__pager">
          <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={offset === 0} onClick={() => setOffset(offset - PAGE)}>{t("上一页")}</button>
          <span className="av-caption">{t("第 {{page}} / {{pages}} 页", { page: offset / PAGE + 1, pages })}</span>
          <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={offset + PAGE >= d.total} onClick={() => setOffset(offset + PAGE)}>{t("下一页")}</button>
        </div> : null}
        {d.sql ? <details className="av-fold"><summary>{t("SQL 与口径版本")} <span className="av-caption">{d.metric_version_ids.length ? t("{{count}} 个口径版本", { count: d.metric_version_ids.length }) : t("探索查询")}</span></summary>
          {d.metric_version_ids.length ? <p className="av-caption av-mono">{d.metric_version_ids.join(t("、"))}</p> : null}
          <Code sql={d.sql} /></details> : null}
      </div> : null}
      {rerun && d ? <RerunDrawer runId={runId} d={d} onClose={() => setRerun(false)} onDone={(ref) => { setRerun(false); onOpen(ref); }} /> : null}
    </div>
  );
}

/** 换时间重跑：按当前可查询的口径和你现在的权限重新取数，结果作为本次分析里的新数据集；报告不变。 */
function RerunDrawer({ runId, d, onClose, onDone }: { runId: string; d: Detail; onClose: () => void; onDone: (ref: string) => void }) {
  const intent = (d.meta.intent ?? {}) as { compare?: string | null };
  const w = d.window as { start?: string; end?: string };
  const custom = intent.compare === "custom";
  const [form, setForm] = useState({ start: w.start ?? "", end: w.end ?? "", cs: "", ce: "" });
  const [error, setError] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const go = useAction(() => unwrap(api.POST("/api/analysis/runs/{run_id}/datasets/{ref}/rerun", {
    params: { path: { run_id: runId, ref: d.ref } },
    body: { window: { start: form.start, end: form.end, ...(custom ? { compare_start: form.cs, compare_end: form.ce } : {}) } } })),
    ["analysis-run"]);
  const ready = form.start && form.end && form.start <= form.end && (!custom || (form.cs && form.ce));
  const field = (key: keyof typeof form, label: string) => (
    <label className="av-field"><span className="av-label">{label}</span>
      <input className="av-input" type="date" value={form[key]} onChange={(e) => setForm({ ...form, [key]: e.target.value })} /></label>
  );
  return (
    <Drawer title={t("换时间重跑 {{ref}}", { ref: d.ref })} onClose={onClose} footer={<div className="av-actions">
      <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={onClose}>{t("取消")}</button>
      <button type="button" className="av-btn av-btn--primary av-btn--sm" disabled={!ready || go.isPending}
        onClick={() => go.mutate(undefined, {
          onSuccess: (out) => { void queryClient.invalidateQueries({ queryKey: ["analysis-dataset", runId] }); onDone((out as Detail).ref); },
          onError: (e) => setError(errorText(e)) })}>{go.isPending ? t("取数中…") : t("重跑")}</button>
    </div>}>
      {error ? <div className="av-banner av-banner--danger">{error}</div> : null}
      <p className="av-caption">{t("同样的口径、维度和筛选，换一个时间窗口重新取数。按现在可用的口径版本和你当前的权限执行，结果作为新的数据集保存在这次分析里，报告不变。")}</p>
      <div className="av-dsview__dates">{field("start", t("开始"))}{field("end", t("结束"))}</div>
      {custom ? <div className="av-dsview__dates">{field("cs", t("对比开始"))}{field("ce", t("对比结束"))}</div>
        : intent.compare ? <p className="av-caption">{intent.compare === "previous_year" ? t("对比期按新窗口自动推算（同比）。") : t("对比期按新窗口自动推算（环比）。")}</p> : null}
    </Drawer>
  );
}
