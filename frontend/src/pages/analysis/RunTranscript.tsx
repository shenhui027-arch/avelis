// 分析过程流（ADR 0013）：像对话一样实时展开模型每一步在做什么——理解、查了哪个口径或哪张表、写了什么 SQL、
// 每轮的思路；运行中可以随时插话纠偏（下一次调用模型前生效）或停止。完成后折叠在报告下方作为“分析过程”。
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { api, unwrap, type Schemas } from "../../api/client";
import { errorText, useAction } from "../../api/hooks";
import { Code, Icon, Pill, shortName } from "../../components/ui";
import { EvidenceLink } from "./DatasetView";
import { ContextMeter } from "./ModelPicker";

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
    case "compact": {
      const kt = (n: unknown) => (typeof n === "number" ? `${(n / 1000).toFixed(1)}k` : "?");
      return { icon: "fit", title: o.reason === "manual" ? "按你的要求压缩了上下文" : "上下文接近上限，已自动压缩",
        detail: `${kt(o.before_tokens)} → ${kt(o.after_tokens)} · 早期 ${o.summarized_messages ?? "?"} 条过程换成数据集清单与步骤摘要` };
    }
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

function StepRow({ s, runId }: { s: Step; runId: string }) {
  const d = describe(s);
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
          {s.dataset ? <EvidenceLink runId={runId} refId={s.dataset} /> : null}
          {s.duration_ms ? <span className="av-runlog__time">{(Number(s.duration_ms) / 1000).toFixed(1)}s</span> : null}
        </div>
        {d.detail ? <div className={`av-runlog__detail${s.kind === "model" ? " is-thought" : ""}`}>{d.detail}</div> : null}
        {d.code ? <Code sql={d.code} /> : null}
        {d.thinking ? <Thought text={d.thinking} /> : null}
        {bad ? <div className="av-runlog__err">{String(err?.error ?? err?.message ?? s.status)}</div> : null}
      </div>
    </li>
  );
}

/** 一段模型推理原文：默认直接显示，较长时先显示前几行，可展开全文。 */
function Thought({ text }: { text: string }) {
  const [full, setFull] = useState(false);
  const long = text.length > 600 || text.split("\n").length > 8;
  return (
    <div className="av-thought">
      <p className={`av-thought__text${long && !full ? " is-clamped" : ""}`}>{text}</p>
      {long ? <button type="button" className="av-runlog__more" onClick={() => setFull(!full)}>{full ? "收起" : "展开全文"}</button> : null}
    </div>
  );
}

/** 思考与过程：运行中展开并跟到最新一步（用户往上翻时不打扰）；完成后标题是“已深度思考（用时）”，可折叠。
 *  插话与停止在页面底部的固定输入框里。 */
export function RunTranscript({ run, live, footer, autoFollow = true }: { run: Run; live?: boolean; footer?: ReactNode; autoFollow?: boolean }) {
  const [open, setOpen] = useState(!!live);
  useEffect(() => { setOpen(!!live); }, [live]);
  const steps = run.steps ?? [];
  const messages = ((run as Run & { messages?: Message[] }).messages ?? []).filter((m) => !m.consumed_at);
  const end = useRef<HTMLDivElement>(null);
  // 打开的是对话里较早的一轮时不抢滚动位置，用户滚到底部后再跟
  const [follow, setFollow] = useState(autoFollow);
  useEffect(() => {
    if (!live) return;
    const scroller = end.current?.closest(".av-analysis__scroll");
    if (!scroller) return;
    const onScroll = () => setFollow(scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 120);
    scroller.addEventListener("scroll", onScroll);
    return () => scroller.removeEventListener("scroll", onScroll);
  }, [live]);
  // 跟到对话区最底部（等待确认时确认面板在过程流下方，也要露出来）
  useEffect(() => {
    if (!live || !follow) return;
    const scroller = end.current?.closest(".av-analysis__scroll");
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
  }, [live, follow, steps.length, messages.length, run.status]);
  const seconds = Math.round(steps.reduce((sum, s) => sum + Number(s.duration_ms ?? 0), 0) / 1000);
  const thoughts = steps.filter((s) => s.kind === "model" && (s.output as { thinking?: string } | null)?.thinking).length;
  return (
    <section className={`av-runlog${live ? " is-live" : ""}`} aria-label="思考与过程" aria-live={live ? "polite" : undefined}>
      <button type="button" className="av-think__hd" aria-expanded={open} onClick={() => setOpen(!open)}>
        {live ? <span className="av-dot av-dot--flow" /> : <Icon name="chip" small />}
        <span>{live ? (run.status === "waiting" ? "思考暂停，等你确认" : "正在思考…") : `已深度思考（用时 ${seconds} 秒）`}</span>
        <span className="av-caption">{steps.length} 步{thoughts ? ` · ${thoughts} 段推理` : ""}</span>
        <Icon name="chevron" small />
      </button>
      {open ? <>
      <ol className="av-runlog__steps">
        {steps.map((s) => <StepRow key={s.seq} s={s} runId={run.id} />)}
        {messages.map((m) => (
          <li key={m.id} className="av-runlog__user"><span className="av-runlog__bubble">{m.text}</span>
            <Pill tone="warn">待生效</Pill></li>))}
        {live && run.status !== "waiting" ? <li className="av-runlog__step is-pending"><span className="av-spinner" aria-hidden />
          {run.status === "queued" ? <Queued /> : <span className="av-caption">正在进行下一步…</span>}</li> : null}
      </ol>
      {footer && !live ? <div className="av-think__ft">{footer}</div> : null}
      </> : null}
      <div ref={end} />
    </section>
  );
}

/** 运行中的插话与停止：固定在对话区下方。 */
export function Interject({ run }: { run: Run }) {
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
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); } }} />
        <div className="av-compose__bar">
          <span className="av-caption">Enter 发送 · Shift+Enter 换行</span>
          <span className="av-compose__spacer" />
          <ContextMeter run={run} live />
          <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={stop.isPending}
            onClick={() => stop.mutate(undefined, { onError: (e) => setError(errorText(e)) })}>停止</button>
          <button type="button" className="av-btn av-btn--primary av-btn--sm" disabled={send.isPending || !text.trim()} onClick={submit}>
            <Icon name="send" />发送</button>
        </div>
      </div>
    </div>
  );
}

/** 排队中：后台执行器（worker）在跑就等它领取；没在跑时说清楚原因，不让人对着“排队中”干等。 */
function Queued() {
  const worker = useQuery({
    queryKey: ["worker-health"], refetchInterval: 10_000,
    queryFn: () => unwrap(api.GET("/api/health/worker")),
  });
  if (worker.data && !worker.data.alive) {
    return <span className="av-runlog__queued"><span className="av-caption">排队中</span>
      <span className="av-banner av-banner--warn"><Icon name="info" small />后台执行器没有在运行，任务会在它启动后自动开始。请联系管理员检查 worker 服务。</span></span>;
  }
  return <span className="av-caption">排队中，等待后台执行器领取</span>;
}
