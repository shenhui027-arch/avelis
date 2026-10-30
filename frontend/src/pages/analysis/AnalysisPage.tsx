// 分析：多步分析的报告、计划与步骤 | 数据集（图表、表格、SQL）。结论标明类型与引用，未通过检查的结论醒目标出。
// 报告里的数字由后端按引用渲染（ADR 0009）：按数值段显示，悬停可看来源数据集、取值路径与原值。
import { useQuery } from "@tanstack/react-query";
import { createContext, useContext, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { api, token, unwrap, type Schemas } from "../../api/client";
import { errorText, useAction, useExampleQuestions, useTimeZone } from "../../api/hooks";
import { Code, Empty, Hint, Icon, Pill, Status, Tabs } from "../../components/ui";
import { formatTime } from "../../format";
import { BarChart, LineChart } from "./charts";

type Run = Schemas["AnalysisRunOut"];
type Dataset = Schemas["AnalysisDatasetOut"];
type Task = Schemas["AnalysisTaskOut"];
type Segment = { kind: "text" | "value" | "count" | "label" | "window" | "error"; text: string; ref?: string;
  path?: string; label?: string; format?: string; raw?: string | string[]; measure?: string; unit?: string;
  direction?: string; role?: string; source?: string; explore?: boolean; assumption?: boolean; declared?: boolean;
  error?: string };
type FieldRecord = { parts?: unknown; text: string; segments?: Segment[]; status?: string; errors?: string[] };
type Claim = { type: string; text: string; segments?: Segment[]; refs?: string[]; premise?: string | null;
  validation?: string | null; details?: Record<string, FieldRecord> };
type Check = { index: number; status: string; problems?: string[]; missing_refs?: string[] };
type FieldCheck = { field: string; status: string; problems?: string[] };
type WindowKind = "none" | "last_month" | "fixed";

const TYPE_LABEL: Record<string, string> = { fact: "事实", calc: "计算", inference: "推断", recommendation: "建议" };
const TYPE_TONE: Record<string, "ok" | "info" | "warn" | ""> = { fact: "ok", calc: "info", inference: "", recommendation: "warn" };
const TOOL_LABEL: Record<string, string> = {
  submit_plan: "计划", search_metrics: "检索口径", describe_metric: "口径详情", search_assets: "检索资产",
  describe_asset: "资产详情", lineage: "血缘", changes: "变更记录", data_health: "数据健康", query_metric: "受治理查询",
  explain_change: "自动归因", contribution: "差额拆解", factor_decompose: "因子拆解", calendar_adjust: "日历折算", correlate: "相关性",
  segment_compare: "分组对比", outliers: "异常值", coverage: "覆盖度", submit_report: "提交报告",
};

const useRuns = () => useQuery({ queryKey: ["analysis-runs"], queryFn: () => unwrap(api.GET("/api/analysis/runs")) });
const useTasks = () => useQuery({ queryKey: ["analysis-tasks"], queryFn: () => unwrap(api.GET("/api/analysis/tasks")) });
const useRun = (id?: string) => useQuery({
  queryKey: ["analysis-run", id], enabled: !!id,
  queryFn: () => unwrap(api.GET("/api/analysis/runs/{run_id}", { params: { path: { run_id: id! } } })),
  refetchInterval: (q) => (q.state.data && ["queued", "running"].includes(q.state.data.status) ? 2000 : false),
});

async function download(path: string, filename: string) {
  const res = await fetch(path, { headers: { Authorization: `Bearer ${token.get()}` } });
  if (!res.ok) throw new Error((await res.json().catch(() => ({ detail: "导出失败" }))).detail ?? "导出失败");
  const url = URL.createObjectURL(await res.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

function windowBody(kind: WindowKind, fixed: { start: string; end: string; cs: string; ce: string }) {
  if (kind === "none") return undefined;
  if (kind === "last_month") return { kind };
  return { kind, start: fixed.start, end: fixed.end, compare_start: fixed.cs || undefined, compare_end: fixed.ce || undefined };
}

export function AnalysisPage() {
  const { runId, section } = useParams();
  const navigate = useNavigate();
  const runs = useRuns();
  const tab = section === "tasks" ? "tasks" : "runs";
  return (
    <div className="av-split av-analysis" style={{ height: "100%" }}>
      <div className="av-analysis__main">
        <div className="av-analysis__hd">
          <h1>分析 <Hint text="多步分析：模型规划并调用受治理查询与计算工具，每个数字都可追溯到数据集与 SQL" /></h1>
          <Tabs value={tab} options={[{ value: "runs", label: "分析记录" }, { value: "tasks", label: "定时任务" }]}
            onChange={(v) => navigate(v === "tasks" ? "/analysis/tasks" : "/analysis")} />
        </div>
        {tab === "tasks" ? <TaskList /> : <>
          <Composer onStarted={(id) => navigate(`/analysis/runs/${id}`)} />
          {runId ? <RunDetail id={runId} /> : (
            <div className="av-analysis__list">
              {(runs.data ?? []).map((r) => (
                <Link key={r.id} to={`/analysis/runs/${r.id}`} className="av-row">
                  <span className="av-row__main"><span className="av-row__title">{r.title ?? r.question}</span>
                    <span className="av-row__meta">{r.question}</span></span>
                  <RunBadge run={r} />
                </Link>
              ))}
              {runs.isSuccess && !runs.data.length ? <Empty text="还没有分析记录" compact /> : null}
            </div>
          )}
        </>}
      </div>
      <aside className="av-inspector av-analysis__aside" aria-label="数据集">
        {runId && tab === "runs" ? <Datasets id={runId} /> : <Empty text="分析中的数据集显示在这里" />}
      </aside>
    </div>
  );
}

function RunBadge({ run }: { run: Run }) {
  if (run.status !== "succeeded") return <Status value={run.status} />;
  return run.verified ? <Pill tone="ok">核验通过</Pill> : <Pill tone="warn">有未核实数字</Pill>;
}

function Composer({ onStarted }: { onStarted: (id: string) => void }) {
  const [text, setText] = useState("");
  const examples = (useExampleQuestions("analysis").data?.questions ?? []).map((q) => q.text);
  const [kind, setKind] = useState<WindowKind>("none");
  const [fixed, setFixed] = useState({ start: "2026-09-01", end: "2026-09-30", cs: "2026-08-01", ce: "2026-08-31" });
  const [cron, setCron] = useState("");
  const [error, setError] = useState<string | null>(null);
  const start = useAction(() => unwrap(api.POST("/api/analysis/runs", {
    body: { question: text, window: windowBody(kind, fixed) } as never })), ["analysis-runs"]);
  const save = useAction(() => unwrap(api.POST("/api/analysis/tasks", {
    body: { question: text, title: "", window: windowBody(kind, fixed), schedule_cron: cron || null } as never })),
  ["analysis-tasks"]);
  return (
    <section className="av-analysis__composer">
      {error ? <div className="av-banner av-banner--danger">{error}</div> : null}
      <div className="av-ask-examples" aria-label="问题示例">
        {examples.map((e) => <button key={e} type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={() => setText(e)}>{e}</button>)}
      </div>
      <form className="av-composer" onSubmit={(e) => {
        e.preventDefault();
        setError(null);
        start.mutate(undefined, { onSuccess: (r) => onStarted(r.id), onError: (err) => setError(errorText(err)) });
      }}>
        <label htmlFor="aq" className="av-sr">分析问题</label>
        <textarea id="aq" rows={2} value={text} placeholder="提一个需要多步分析的问题：为什么变了、什么关系、该怎么做"
          onChange={(e) => setText(e.target.value)} />
        <div className="av-actions">
          <button className="av-btn av-btn--primary" type="submit" disabled={start.isPending || !text.trim()}>
            <Icon name="play" />{start.isPending ? "提交中…" : "开始分析"}</button>
        </div>
      </form>
      <div className="av-analysis__options">
        <label className="av-field av-field--inline"><span className="av-label">时间窗口</span>
          <select className="av-input" value={kind} onChange={(e) => setKind(e.target.value as WindowKind)}>
            <option value="none">按问题理解</option><option value="last_month">上个月 对比 再上个月</option>
            <option value="fixed">指定日期</option>
          </select></label>
        {kind === "fixed" ? <>
          {(["start", "end", "cs", "ce"] as const).map((k) => (
            <label key={k} className="av-field av-field--inline"><span className="av-label">
              {{ start: "本期起", end: "本期止", cs: "对比起", ce: "对比止" }[k]}</span>
              <input className="av-input av-mono" type="date" value={fixed[k]} onChange={(e) => setFixed({ ...fixed, [k]: e.target.value })} /></label>
          ))}
        </> : null}
        <label className="av-field av-field--inline"><span className="av-label">定时 <Hint text="5 段 cron（业务时区），如 0 9 1 * * 为每月 1 日 9:00；留空只保存不定时" /></span>
          <input className="av-input av-mono" value={cron} placeholder="0 9 1 * *" onChange={(e) => setCron(e.target.value)} style={{ width: 120 }} /></label>
        <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={save.isPending || !text.trim()}
          onClick={() => save.mutate(undefined, { onError: (err) => setError(errorText(err)), onSuccess: () => setError(null) })}>
          <Icon name="refresh" />保存为任务</button>
        {save.isSuccess ? <span className="av-caption">已保存，见“定时任务”</span> : null}
      </div>
    </section>
  );
}

const FIELD_LABEL: Record<string, string> = { title: "标题", summary: "摘要", unexplained: "未解释" };
const REPORT_STATUS: Record<string, [string, "ok" | "warn" | "danger"]> = {
  all_passed: ["来源全部通过", "ok"], with_inference: ["来源全部通过 · 含未核实的推断", "warn"],
  no_facts: ["没有可核实的事实", "warn"], failed: ["有未通过的事实或字段", "danger"],
};
const SOURCE_LABEL: Record<string, string> = { governed: "口径", explore: "探索", calc: "计算", context: "上下文" };
const MEASURE_LABEL: Record<string, string> = { level: "水平值", delta: "差额", ratio: "比率", rate_change: "变化率",
  share: "占比", count: "计数", unknown: "量纲未知" };

// 报告里的引用段：数字旁显示来源标签（悬停看原值、量纲、格式），探索数据标“探索查询（未经口径审核）”
// 数据集编号 → 实际执行的 SQL：探索查询的数字悬停可看 SQL（ADR 0009 第 6 点）
const DatasetSql = createContext<Record<string, string>>({});

function Segments({ segments, text }: { segments?: Segment[]; text?: string | null }) {
  const sqlOf = useContext(DatasetSql);
  if (!segments?.length) return <>{text}</>;
  return <>{segments.map((s, i) => {
    if (s.kind === "text") return <span key={i}>{s.text}</span>;
    if (s.kind === "error") return <span key={i} className="av-num is-error" title={s.error}>{s.text}</span>;
    const raw = Array.isArray(s.raw) ? s.raw.join(" ~ ") : s.raw;
    const title = [s.label ?? s.path, `原值 ${raw}`,
      s.measure ? `${MEASURE_LABEL[s.measure] ?? s.measure}${s.unit ? `（${s.unit}）` : ""}${s.declared ? " · 模型声明" : ""}` : "",
      s.format ? `格式 ${s.format}` : "", s.direction ? `方向 ${s.direction === "up" ? "上升" : "下降"}` : "",
      s.explore ? (s.source === "explore" && s.ref && sqlOf[s.ref] ? `探索查询（未经口径审核）\nSQL：${sqlOf[s.ref]}`
        : "来源含探索查询（未经口径审核），SQL 见数据集附录") : "", s.assumption ? "假设计算" : "",
      s.role === "proposal" ? "建议目标（非观测值）" : ""].filter(Boolean).join(" · ");
    const cls = ["av-num", s.kind === "label" ? "" : "av-mono", s.role === "proposal" ? "is-proposal" : "",
      s.explore ? "is-explore" : ""].filter(Boolean).join(" ");
    // 数字旁的来源标签：数据集编号（悬停看完整来源），探索数据与建议目标另标
    const tag = [s.ref && s.ref !== "window" ? s.ref : "", s.explore ? "探索" : "", s.role === "proposal" ? "建议目标" : ""]
      .filter(Boolean).join(" · ");
    return <span key={i} className={cls} title={title}>{s.text}{tag ? <sup>{tag}</sup> : null}</span>;
  })}</>;
}

function RunDetail({ id }: { id: string }) {
  const run = useRun(id);
  const tz = useTimeZone();
  const [error, setError] = useState<string | null>(null);
  if (!run.data) return run.isError ? <Empty text={errorText(run.error)} /> : <Empty text="加载中…" compact />;
  const r = run.data;
  const report = (r.report ?? {}) as { title?: string; summary?: string; claims?: Claim[]; unexplained?: string;
    notes?: string[]; fields?: Record<string, FieldRecord> };
  const verification = (r.verification ?? {}) as { ok?: boolean; status?: string; unverified?: number; claims?: Check[];
    fields?: FieldCheck[]; sources?: { direct?: Record<string, number>; with_explore?: number; assumption?: number;
      windows?: number; labels?: number; failed?: number } };
  const reportStatus = verification.status ? REPORT_STATUS[verification.status] : undefined;
  // 数字来源构成（两个维度）：直接来源，其中含探索来源、假设的数量；日期、标签、渲染失败另列
  const sources = verification.sources ?? {};
  const composition = [
    ...Object.entries(sources.direct ?? {}).filter(([, n]) => n > 0).map(([k, n]) => `${SOURCE_LABEL[k] ?? k} ${n}`),
    ...([["with_explore", "其中含探索"], ["assumption", "其中假设"], ["windows", "日期"], ["labels", "标签"], ["failed", "渲染失败"]] as const)
      .filter(([k]) => sources[k]).map(([k, label]) => `${label} ${sources[k]}`),
  ].join(" · ");
  const badFields = (verification.fields ?? []).filter((f) => f.status === "unverified");
  const checks = new Map((verification.claims ?? []).map((c) => [c.index, c]));
  const plan = (r.plan ?? {}) as { hypotheses?: string[]; steps?: string[] };
  const budget = (r.budget ?? {}) as { limits?: Record<string, number>; used?: Record<string, number> };
  const running = r.status === "queued" || r.status === "running";
  return (
    <article className="av-analysis__run">
      <header className="av-analysis__run-hd">
        <div>
          <h2>{report.title ? <Segments segments={report.fields?.title?.segments} text={report.title} /> : r.question}</h2>
          <span className="av-caption">{r.question}</span>
        </div>
        <div className="av-actions">
          <RunBadge run={r} />
          {r.status === "succeeded" ? <>
            <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => download(
              `/api/analysis/runs/${r.id}/export?format=pdf`, `analysis-${r.id}.pdf`).catch((e) => setError(errorText(e)))}>
              <Icon name="download" />PDF</button>
            <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => download(
              `/api/analysis/runs/${r.id}/export?format=md`, `analysis-${r.id}.md`).catch((e) => setError(errorText(e)))}>
              <Icon name="download" />Markdown</button>
            <button type="button" className="av-btn av-btn--secondary av-btn--sm" title="报告中每个数字的来源数据集、取值路径与原值"
              onClick={() => download(`/api/analysis/runs/${r.id}/export?format=csv`, `analysis-${r.id}-sources.csv`)
                .catch((e) => setError(errorText(e)))}>
              <Icon name="download" />引用来源</button>
          </> : null}
        </div>
      </header>
      {error ? <div className="av-banner av-banner--danger">{error}</div> : null}
      <DatasetSql.Provider value={Object.fromEntries((r.datasets ?? []).filter((d) => d.sql).map((d) => [d.ref, d.sql as string]))}>
      <div className="av-analysis__meta av-caption">
        {r.window && (r.window as { start?: string }).start ? <span className="av-mono">
          {(r.window as { start: string; end: string }).start} ~ {(r.window as { end: string }).end}
          {(r.window as { compare_start?: string }).compare_start ? ` 对比 ${(r.window as { compare_start: string }).compare_start} ~ ${(r.window as { compare_end: string }).compare_end}` : ""}</span> : null}
        <span>模型 {r.model_adapter ?? "—"}</span>
        {budget.used ? <span>工具 {budget.used.tool_calls}/{budget.limits?.tool_calls} · 查询 {budget.used.queries}/{budget.limits?.queries} · {budget.used.seconds}s</span> : null}
        <span>{formatTime(r.created_at, tz)}</span>
      </div>
      {running ? <div className="av-banner">分析进行中：已完成 {r.steps?.length ?? 0} 步</div> : null}
      {r.status === "failed" ? <div className="av-banner av-banner--danger">分析失败：{r.error}</div> : null}
      {r.status === "succeeded" ? (
        <section className="av-analysis__report" aria-label="报告">
          <div className="av-analysis__verdict">
            {reportStatus ? <Pill tone={reportStatus[1]}>{reportStatus[0]}</Pill> : null}
            {composition ? <span className="av-caption">数字来源 {composition}</span> : null}
          </div>
          <p className="av-analysis__summary"><Segments segments={report.fields?.summary?.segments} text={report.summary} /></p>
          {verification.ok === false && verification.unverified ? <div className="av-banner av-banner--danger">有 {verification.unverified} 条事实 / 计算类结论未通过来源校验，已标出原因</div> : null}
          {badFields.map((f) => <div key={f.field} className="av-banner av-banner--danger">
            {FIELD_LABEL[f.field] ?? f.field}未通过检查：{(f.problems ?? []).join("；")}</div>)}
          <ol className="av-claims">
            {(report.claims ?? []).map((c, i) => {
              const check = checks.get(i);
              const strict = c.type === "fact" || c.type === "calc";
              const bad = strict && check?.status === "unverified" ? check.problems ?? [] : [];
              return (
                <li key={i} className={`av-claim${strict && check?.status === "unverified" ? " is-unverified" : ""}`}>
                  <Pill tone={TYPE_TONE[c.type] ?? ""}>{TYPE_LABEL[c.type] ?? c.type}</Pill>
                  <div className="av-claim__body">
                    <span><Segments segments={c.segments} text={c.text} /></span>
                    {c.premise ? <span className="av-caption">前提：<Segments segments={c.details?.premise?.segments} text={c.premise} /></span> : null}
                    {c.validation ? <span className="av-caption">验证方式：<Segments segments={c.details?.validation?.segments} text={c.validation} /></span> : null}
                    {!strict ? <span className="av-caption">推断 / 建议，未核实</span> : check?.status === "verified" ? <span className="av-caption">来源已校验</span> : null}
                    {bad.map((p, j) => <span key={j} className="av-claim__bad">{p}</span>)}
                  </div>
                  <span className="av-claim__refs">{(c.refs ?? []).map((ref) => (
                    <a key={ref} className="av-tag av-mono" href={`#dataset-${ref}`}>{ref}</a>))}</span>
                </li>
              );
            })}
          </ol>
          {report.unexplained ? <p className="av-caption"><b>仍未解释：</b><Segments segments={report.fields?.unexplained?.segments} text={report.unexplained} /></p> : null}
          {report.notes?.length ? <p className="av-caption">{report.notes.join("；")}</p> : null}
        </section>
      ) : null}
      {plan.hypotheses?.length ? (
        <section className="av-analysis__plan"><h3>计划</h3>
          <ul>{plan.hypotheses.map((h, i) => <li key={i}>{h}</li>)}</ul>
        </section>
      ) : null}
      <section className="av-analysis__steps" aria-label="步骤"><h3>步骤 <span className="av-caption">{r.steps?.length ?? 0}</span></h3>
        <ol className="av-timeline">
          {(r.steps ?? []).map((s) => (
            <li key={s.seq} className={`av-timeline__item is-${s.status}`}>
              <span className="av-timeline__tool">{TOOL_LABEL[s.tool ?? ""] ?? s.tool ?? s.kind}</span>
              {s.reason ? <span className="av-timeline__reason">{s.reason}</span> : null}
              {s.dataset ? <a className="av-tag av-mono" href={`#dataset-${s.dataset}`}>{s.dataset}</a> : null}
              {s.status !== "ok" ? <span className="av-caption av-timeline__err">{String((s.output as { error?: string; message?: string }).error
                ?? (s.output as { message?: string }).message ?? s.status)}</span> : null}
            </li>
          ))}
        </ol>
      </section>
      </DatasetSql.Provider>
    </article>
  );
}

function Datasets({ id }: { id: string }) {
  const run = useRun(id);
  const [error, setError] = useState<string | null>(null);
  const datasets = run.data?.datasets ?? [];
  if (!datasets.length) return <Empty text="还没有数据集" />;
  return (
    <div className="av-analysis__datasets">
      {error ? <div className="av-banner av-banner--danger">{error}</div> : null}
      {datasets.map((d) => (
        <section key={d.ref} id={`dataset-${d.ref}`} className="av-dataset">
          <div className="av-dataset__hd">
            <span className="av-tag av-mono">{d.ref}</span>
            <span className="av-dataset__title">{d.title}</span>
            <Pill tone={d.kind === "governed" ? "ok" : d.kind === "ungoverned" || d.kind === "explore" ? "warn" : "info"}>
              {{ governed: "受治理", calc: "计算", context: "上下文证据", explore: "探索查询（未经口径审核）", ungoverned: "未治理" }[d.kind] ?? d.kind}</Pill>
            <button type="button" className="av-btn av-btn--ghost av-btn--sm" title="导出 CSV" onClick={() => download(
              `/api/analysis/runs/${id}/datasets/${d.seq}/csv`, `analysis-${id}-${d.ref}.csv`).catch((e) => setError(errorText(e)))}>
              <Icon name="download" /></button>
          </div>
          <DatasetChart d={d} />
          <div className="av-table-wrap"><table className="av-table av-dataset__table">
            <thead><tr>{d.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
            <tbody>{d.rows.slice(0, 12).map((row, i) => <tr key={i}>{row.map((v, j) => <td key={j} className="av-mono">{v == null ? "—" : String(v)}</td>)}</tr>)}</tbody>
          </table></div>
          {d.row_count > 12 ? <span className="av-caption">共 {d.row_count} 行，完整数据请导出 CSV</span> : null}
          {d.inputs?.length ? <span className="av-caption">输入：{d.inputs.join("、")}</span> : null}
          {d.sql ? <details><summary className="av-caption">SQL 与口径版本</summary><Code sql={d.sql} /></details> : null}
        </section>
      ))}
    </div>
  );
}

function numeric(v: unknown): number | null {
  const n = typeof v === "number" ? v : typeof v === "string" && v.trim() !== "" ? Number(v) : NaN;
  return Number.isFinite(n) ? n : null;
}

/** 首列是保存的时间段列（time_column）时画趋势；否则对 change / contribution（或第一个数值列）画贡献条。 */
function DatasetChart({ d }: { d: Dataset }) {
  const cols = d.columns;
  const trend = !!d.time_column && cols[0] === d.time_column;
  const numCols = cols.map((_, i) => i).filter((i) => !(trend && i === 0) && d.rows.some((r) => numeric(r[i]) !== null)
    && d.rows.every((r) => r[i] == null || numeric(r[i]) !== null));
  if (!d.rows.length || !numCols.length) return null;
  // 优先画变化额（“·变化”、change、contribution），其次画第一个数值列；不画变化率
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
  const labelCols = cols.map((_, i) => i).filter((i) => !numCols.includes(i)).slice(0, 2);
  const bars = d.rows.slice(0, 12).map((r, i) => ({ label: labelCols.map((c) => String(r[c] ?? "")).join(" / ") || String(i + 1), value: numeric(r[value]) ?? 0 }));
  return <BarChart bars={bars} label={cols[value]} />;
}

function TaskList() {
  const tasks = useTasks();
  const tz = useTimeZone();
  const [error, setError] = useState<string | null>(null);
  const patch = useAction((v: { id: string; status: string }) => unwrap(api.PATCH("/api/analysis/tasks/{task_id}", {
    params: { path: { task_id: v.id } }, body: { status: v.status } as never })), ["analysis-tasks"]);
  const runNow = useAction((id: string) => unwrap(api.POST("/api/analysis/tasks/{task_id}/run", {
    params: { path: { task_id: id } } })), ["analysis-tasks", "analysis-runs", "notifications"]);
  const list = tasks.data ?? [];
  return (
    <section className="av-analysis__tasks">
      {error ? <div className="av-banner av-banner--danger">{error}</div> : null}
      {tasks.isSuccess && !list.length ? <Empty text="还没有分析任务：在“分析记录”中提问后保存为任务" compact /> : null}
      {list.map((t: Task) => (
        <div key={t.id} className="av-row">
          <span className="av-row__main"><span className="av-row__title">{t.title}</span>
            <span className="av-row__meta">{t.question}</span>
            <span className="av-caption av-mono">{t.schedule_cron ?? "仅手动"}{t.next_run_at ? ` · 下次 ${formatTime(t.next_run_at, tz)}` : ""}
              {(t.window as { kind?: string }).kind === "last_month" ? " · 上个月对比再上个月" : ""}</span>
            {t.paused_reason ? <span className="av-caption av-claim__bad">已暂停：{t.paused_reason}</span> : null}</span>
          <span className="av-actions">
            <Status value={t.status === "active" ? "active" : "paused"} label={t.status === "active" ? "启用" : "暂停"} />
            {t.last_run_id ? <Link className="av-btn av-btn--ghost av-btn--sm" to={`/analysis/runs/${t.last_run_id}`}>最近一次</Link> : null}
            <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={runNow.isPending}
              onClick={() => runNow.mutate(t.id, { onError: (e) => setError(errorText(e)), onSuccess: () => setError(null) })}>
              <Icon name="play" />{runNow.isPending ? "运行中…" : "立即运行"}</button>
            <button type="button" className="av-btn av-btn--ghost av-btn--sm"
              onClick={() => patch.mutate({ id: t.id, status: t.status === "active" ? "paused" : "active" }, { onError: (e) => setError(errorText(e)) })}>
              {t.status === "active" ? "暂停" : "启用"}</button>
          </span>
        </div>
      ))}
    </section>
  );
}
