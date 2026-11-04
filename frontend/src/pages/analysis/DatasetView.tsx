// 数据集：证据栏里的卡片可以在主区域打开，看完整表格（分页）、图表、SQL 与口径版本；受治理数据集可以换时间窗口重跑。
// 地址带 ?dataset=D3，可以直接分享到具体数据集。
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";

import { api, unwrap, type Schemas } from "../../api/client";
import { errorText, useAction, useNumberStyle } from "../../api/hooks";
import { Code, DateInput, Drawer, Empty, Icon, Pill } from "../../components/ui";
import { ChartPanel } from "../../components/charts/ChartPanel";
import type { ChartSpec } from "../../components/charts/chartOption";
import { t } from "../../i18n";
import { formatValue, type NumberStyle, type UnitMeta } from "../../numbers";

type Detail = Schemas["AnalysisDatasetDetailOut"];
export const DATASET_KIND: Record<string, [string, "ok" | "warn" | "info"]> = { governed: [t("受治理"), "ok"], calc: [t("计算"), "info"],
  context: [t("上下文"), "info"], explore: [t("探索"), "warn"], ungoverned: [t("未治理"), "warn"] };
const PAGE = 100;

type Labelled = { column_roles?: Record<string, { role: string; label?: string }> | null; units?: Record<string, unknown> | null };

/** 数据集列标题：列契约里的显示名（含“<指标>·本期”这类系统翻译过的期间名），没有时用列名。 */
export function datasetLabel(d: Labelled, column: string): string {
  return d.column_roles?.[column]?.label ?? column;
}

/** 数据集单元格：有量纲的数值列按量纲格式化，其余原样。 */
export function datasetCell(d: Labelled, column: string, value: unknown, style: NumberStyle): string {
  if (value == null) return "—";
  const role = d.column_roles?.[column]?.role;
  const unit = d.units?.[column] as UnitMeta | undefined;
  return unit && role !== "time" && role !== "dimension" ? formatValue(value, unit, style) : String(value);
}

/** 证据编号：带上它所属的那一轮。续问会复制并重新编号数据集，同一个 D 编号在不同轮次里指向不同的数据。 */
export function EvidenceLink({ runId, refId }: { runId: string; refId: string }) {
  return <Link className="av-tag av-mono" to={{ pathname: `/analysis/runs/${runId}`, hash: `dataset-${refId}` }}>{refId}</Link>;
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
  onDownload: (seq: number, ref: string) => Promise<void>; canRerun: boolean;
}) {
  const [offset, setOffset] = useState(0);
  const [rerun, setRerun] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const detail = useDetail(runId, refId, offset);
  // 翻页时先保留上一页的行（避免表格闪空），页码跟着“正在显示的数据”走，不跟请求走；新页到达前暂停翻页。
  const [shown, setShown] = useState(0);
  useEffect(() => { if (detail.data && !detail.isPlaceholderData) setShown(offset); }, [detail.data, detail.isPlaceholderData, offset]);
  const paging = detail.isPlaceholderData && detail.isFetching;
  const style = useNumberStyle();
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
          <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={downloading} onClick={() => {
            setDownloading(true); setDownloadError(null);
            onDownload(Number(refId.slice(1)), refId).catch((e) => setDownloadError(errorText(e))).finally(() => setDownloading(false));
          }}><Icon name="download" small />{downloading ? t("下载中…") : "CSV"}</button>
        </div> : null}
      </header>
      {downloadError ? <div className="av-banner av-banner--danger">{downloadError}</div> : null}
      {forbidden ? <Empty text={t("你已没有该数据的访问权限")} /> : detail.isError ? <div className="av-banner av-banner--danger">{errorText(detail.error)}</div> : null}
      {d ? <div className="av-dsview__bd">
        <p className="av-caption av-dsview__meta">
          {t("共 {{count}} 行", { count: d.total })}{d.truncated ? t(" · 结果被截断，不代表来源全集") : ""}
          {w.start ? t(" · 窗口 {{start}} ~ {{end}}", { start: w.start, end: w.end }) : ""}
          {d.inputs.length ? t(" · 输入 {{v1}}", { v1: d.inputs.join(t("、")) }) : ""}
          {d.source ? <> {t("· 由")} <button type="button" className="av-dsview__link" onClick={() => onOpen(d.source!.ref)}>{d.source.ref}</button> {t("换时间重跑")}</> : null}
        </p>
        {d.purpose ? <p className="av-dsview__purpose">{d.purpose}</p> : null}
        {shown === 0 && d.charts?.length ? <div className="av-dsview__chart"><ChartPanel charts={d.charts as ChartSpec[]} title={d.title} /></div> : null}
        <div className="av-table-wrap av-dsview__table" aria-busy={paging}><table className="av-table">
          <thead><tr>{d.columns.map((c) => <th key={c}>{datasetLabel(d, c)}</th>)}</tr></thead>
          <tbody>{d.rows.map((row, i) => <tr key={shown + i}>{row.map((v, j) => <td key={j} className="av-mono">{datasetCell(d, d.columns[j], v, style)}</td>)}</tr>)}</tbody>
        </table></div>
        {pages > 1 ? <div className="av-dsview__pager">
          <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={paging || shown === 0} onClick={() => setOffset(shown - PAGE)}>{t("上一页")}</button>
          <span className="av-caption">{paging ? t("加载第 {{page}} 页…", { page: offset / PAGE + 1 }) : t("第 {{page}} / {{pages}} 页", { page: shown / PAGE + 1, pages })}</span>
          <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={paging || shown + PAGE >= d.total} onClick={() => setOffset(shown + PAGE)}>{t("下一页")}</button>
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
      <DateInput label={label} value={form[key]} onChange={(v) => setForm({ ...form, [key]: v })} /></label>
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
