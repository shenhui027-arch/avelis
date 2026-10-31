// 分析过程流（ADR 0013）：像对话一样实时展开模型每一步在做什么——理解、查了哪个口径或哪张表、写了什么 SQL、
// 每轮的思路；运行中可以随时插话纠偏（下一次调用模型前生效）或停止。完成后折叠在报告下方作为“分析过程”。
import { useEffect, useRef, useState } from "react";

import { api, unwrap, type Schemas } from "../../api/client";
import { errorText, useAction } from "../../api/hooks";
import { Code, Icon, Pill, shortName } from "../../components/ui";

type Run = Schemas["AnalysisRunOut"];
type Step = NonNullable<Run["steps"]>[number];
type Message = { id: string; text: string; created_at: string; consumed_at?: string | null };
type Filter = { dimension?: string; values?: unknown[] };

const CALC: Record<string, string> = { contribution: "差额拆解", factor_decompose: "因子拆解", derive: "派生计算",
  calendar_adjust: "日历折算", correlate: "相关性", segment_compare: "分组对比", outliers: "异常值", coverage: "覆盖度" };

const filters = (f?: Filter[] | null) => (f ?? []).map((x) => `${x.dimension}=${(x.values ?? []).join("/")}`).join("，");
const period = (a: Record<string, unknown>) => a.start_date ? `${a.start_date} ~ ${a.end_date}${a.compare_start ? ` 对比 ${a.compare_start} ~ ${a.compare_end}` : ""}` : "";
const join = (...parts: unknown[]) => parts.filter((p) => p !== undefined && p !== null && p !== "").join(" · ");

/** 一步在做什么：标题、要点、可展开的代码或思路。 */
function describe(s: Step): { icon: string; title: string; detail?: string; code?: string; thinking?: string; user?: string } {
  const a = (s.args ?? {}) as Record<string, unknown>;
  const o = (s.output ?? {}) as Record<string, unknown>;
  switch (s.tool) {
    case "understand": {
      const scope = (o.scope ?? a) as Record<string, unknown>;
      const ask = scope.clarification as string | null;
      return { icon: "search", title: ask ? "需要确认范围" : "理解问题",
        detail: ask ?? join(`指标 ${((scope.metric_codes as string[]) ?? []).join("、") || "未确定"}`,
          filters(scope.filters as Filter[]), period(scope)) };
    }
    case "describe_metric": return { icon: "chart", title: "查看口径", detail: join(a.code as string, o.name as string) };
    case "search_metrics": case "search_assets": {
      const hits = ((o.metrics ?? o.assets) as unknown[] | undefined)?.length;
      return { icon: "search", title: s.tool === "search_assets" ? "检索表" : "检索口径",
        detail: join(a.keyword ? `“${a.keyword}”` : "", hits !== undefined ? `${hits} 个结果` : "") };
    }
    case "describe_asset": return { icon: "table", title: "查看表结构", detail: shortName(a.qualified as string) };
    case "lineage": return { icon: "lineage", title: "查看血缘", detail: shortName(a.qualified as string) };
    case "changes": return { icon: "change", title: "检查口径变更", detail: join(a.metric_code as string, period(a)) };
    case "data_health": return { icon: "shield", title: "检查数据健康", detail: join(a.metric_code as string, period(a)) };
    case "explain_change": return { icon: "analysis", title: "自动归因",
      detail: join(a.metric_code as string, filters(a.filters as Filter[]), (a.dimensions as string[])?.join("、")) };
    case "query_metric": return { icon: "db", title: "受治理查询",
      detail: join(a.metric_code as string, filters(a.filters as Filter[]),
        (a.group_by as string[])?.length ? `按 ${(a.group_by as string[]).join("、")}` : "", a.time_grain ? `按${a.time_grain === "day" ? "日" : a.time_grain}` : "",
        period(a)) };
    case "query_sql": return { icon: "db", title: "探索查询", code: a.sql as string };
    case "submit_proposal": {
      const items = ((o.items as { source: string }[]) ?? []);
      return { icon: "check", title: `提出 ${items.length} 项分析依据`,
        detail: `${items.filter((i) => i.source === "inferred").length} 项是推断，需要确认` };
    }
    case "assumptions": {
      const d = ((a.decisions as { decision: string }[]) ?? []);
      return { icon: "check", title: "你确认了分析依据",
        detail: join(`确认 ${d.filter((x) => x.decision === "confirmed").length} 项`,
          d.some((x) => x.decision === "rejected") ? `否决 ${d.filter((x) => x.decision === "rejected").length} 项` : "",
          a.supplement ? `补充：${a.supplement}` : "") };
    }
    case "message": return { icon: "send", title: "你的插话", user: String(o.text ?? a.text ?? "") };
    case "submit_report": return { icon: "check", title: "提交报告",
      detail: typeof a.title === "string" ? a.title : undefined };
    case "model": {
      const tools = (o.tools as string[]) ?? [];
      return { icon: "chip", title: "推理",
        detail: s.status === "skipped" ? "预算不足，未请求模型"
          : tools.length ? `决定：${tools.map((t) => TOOL_NAME[t] ?? t).join("、")}` : "整理结论",
        thinking: (o.thinking as string) || undefined, user: undefined, code: undefined,
        ...(o.text ? { detail: `${o.text}` } : {}) };
    }
    default:
      return { icon: "chart", title: CALC[s.tool ?? ""] ?? s.tool ?? s.kind, detail: join(a.label as string,
        a.dataset ? `基于 ${a.dataset}` : "", (a.datasets as string[])?.join("、")) };
  }
}

const TOOL_NAME: Record<string, string> = { query_metric: "受治理查询", query_sql: "探索查询", explain_change: "自动归因",
  describe_asset: "查看表结构", search_assets: "检索表", search_metrics: "检索口径", lineage: "查看血缘", submit_report: "提交报告",
  submit_proposal: "提出依据", submit_scope: "理解问题", ...CALC };

function StepRow({ s }: { s: Step }) {
  const d = describe(s);
  const [open, setOpen] = useState(false);
  const bad = s.status !== "ok" && s.status !== "skipped";
  const err = (s.output as { error?: string; message?: string } | null);
  if (d.user !== undefined) return (
    <li className="av-runlog__user"><span className="av-runlog__bubble">{d.user}</span><span className="av-caption">已生效</span></li>);
  return (
    <li className={`av-runlog__step is-${s.kind}${bad ? " is-bad" : ""}`}>
      <span className="av-runlog__icon"><Icon name={d.icon} small /></span>
      <div className="av-runlog__body">
        <div className="av-runlog__line">
          <span className="av-runlog__title">{d.title}</span>
          {s.reason && s.kind !== "model" ? <span className="av-runlog__reason">{s.reason}</span> : null}
          {s.dataset ? <a className="av-tag av-mono" href={`#dataset-${s.dataset}`}>{s.dataset}</a> : null}
          {s.duration_ms ? <span className="av-runlog__time">{(Number(s.duration_ms) / 1000).toFixed(1)}s</span> : null}
        </div>
        {d.detail ? <div className={`av-runlog__detail${s.kind === "model" ? " is-thought" : ""}`}>{d.detail}</div> : null}
        {d.code ? <Code sql={d.code} /> : null}
        {d.thinking ? <>
          <button type="button" className="av-runlog__more" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? "收起思考过程" : "查看思考过程"}</button>
          {open ? <pre className="av-runlog__thinking">{d.thinking}</pre> : null}
        </> : null}
        {bad ? <div className="av-runlog__err">{String(err?.error ?? err?.message ?? s.status)}</div> : null}
      </div>
    </li>
  );
}

/** 过程流；live 时底部可以插话与停止。 */
export function RunTranscript({ run, live }: { run: Run; live?: boolean }) {
  const steps = run.steps ?? [];
  const messages = ((run as Run & { messages?: Message[] }).messages ?? []).filter((m) => !m.consumed_at);
  const end = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  useEffect(() => {
    if (!live) return;
    const scroller = end.current?.closest(".av-analysis__center");
    if (!scroller) return;
    const onScroll = () => setFollow(scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 120);
    scroller.addEventListener("scroll", onScroll);
    return () => scroller.removeEventListener("scroll", onScroll);
  }, [live]);
  useEffect(() => { if (live && follow) end.current?.scrollIntoView({ block: "end" }); }, [live, follow, steps.length, messages.length]);
  return (
    <section className={`av-runlog${live ? " is-live" : ""}`} aria-label="分析过程" aria-live={live ? "polite" : undefined}>
      <div className="av-runlog__user is-question"><span className="av-runlog__bubble">{run.question}</span></div>
      <ol className="av-runlog__steps">
        {steps.map((s) => <StepRow key={s.seq} s={s} />)}
        {messages.map((m) => (
          <li key={m.id} className="av-runlog__user"><span className="av-runlog__bubble">{m.text}</span>
            <Pill tone="warn">待生效</Pill></li>))}
        {live && run.status !== "waiting" ? <li className="av-runlog__step is-pending"><span className="av-spinner" aria-hidden />
          <span className="av-caption">{run.status === "queued" ? "排队中" : "正在进行下一步…"}</span></li> : null}
      </ol>
      <div ref={end} />
      {live ? <Interject run={run} /> : null}
    </section>
  );
}

function Interject({ run }: { run: Run }) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const send = useAction(() => unwrap(api.POST("/api/analysis/runs/{run_id}/messages" as never, {
    params: { path: { run_id: run.id } }, body: { text } } as never)), ["analysis-run"]);
  const stop = useAction(() => unwrap(api.POST("/api/analysis/runs/{run_id}/stop" as never, {
    params: { path: { run_id: run.id } } } as never)), ["analysis-run", "analysis-runs"]);
  const submit = () => {
    if (!text.trim()) return;
    setError(null);
    send.mutate(undefined, { onSuccess: () => setText(""), onError: (e) => setError(errorText(e)) });
  };
  return (
    <div className="av-runlog__composer">
      {error ? <div className="av-banner av-banner--danger">{error}</div> : null}
      <div className="av-compose av-compose--inline">
        <label htmlFor={`int-${run.id}`} className="av-sr">插话</label>
        <textarea id={`int-${run.id}`} rows={2} maxLength={2000} value={text}
          placeholder="发现方向不对？直接说，下一步就按你说的调整，例如：不要用名称关联，改用编码"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }} />
        <div className="av-compose__bar">
          <span className="av-caption">Enter 发送 · Shift+Enter 换行</span>
          <span className="av-compose__spacer" />
          <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={stop.isPending}
            onClick={() => stop.mutate(undefined, { onError: (e) => setError(errorText(e)) })}>停止</button>
          <button type="button" className="av-btn av-btn--primary av-btn--sm" disabled={send.isPending || !text.trim()} onClick={submit}>
            <Icon name="send" />发送</button>
        </div>
      </div>
    </div>
  );
}
