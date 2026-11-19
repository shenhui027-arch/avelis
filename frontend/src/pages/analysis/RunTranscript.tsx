// 分析过程流（ADR 0013）：像对话一样实时展开模型每一步在做什么——理解、查了哪个口径或哪张表、写了什么 SQL、
// 每轮的思路；运行中可以随时插话纠偏（下一次调用模型前生效）或停止。完成后折叠在报告下方作为“分析过程”。
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { api, unwrap, type Schemas } from "../../api/client";
import { errorText, useAction } from "../../api/hooks";
import { Code, Icon, Pill, shortName } from "../../components/ui";
import { EvidenceLink } from "./DatasetView";
import { ContextMeter } from "./ModelPicker";
import { t } from "../../i18n";

type Run = Schemas["AnalysisRunOut"];
type Step = NonNullable<Run["steps"]>[number];
type Message = { id: string; text: string; created_at: string; consumed_at?: string | null };
type Filter = { dimension?: string; values?: unknown[] };

const CALC: Record<string, string> = { contribution: t("差额拆解"), factor_decompose: t("因子拆解"), derive: t("派生计算"),
  calendar_adjust: t("日历折算"), correlate: t("相关性"), segment_compare: t("分组对比"), outliers: t("异常值"), coverage: t("覆盖度"),
  describe: t("描述统计"), chart: t("生成图表"), view_dataset: t("查看数据集"), sample_values: t("查看取值样例") };

const filters = (f?: Filter[] | null) => (f ?? []).map((x) => `${x.dimension}=${(x.values ?? []).join("/")}`).join(t("，"));
const period = (a: Record<string, unknown>) => a.start_date ? `${a.start_date} ~ ${a.end_date}${a.compare_start ? t(" 对比 {{compare_start}} ~ {{compare_end}}", { compare_start: a.compare_start, compare_end: a.compare_end }) : ""}` : "";
const join = (...parts: unknown[]) => parts.filter((p) => p !== undefined && p !== null && p !== "").join(" · ");

/** 一步在做什么：标题、要点、可展开的代码或思路。 */
function describe(s: Step): { icon: string; title: string; detail?: string; code?: string; thinking?: string; user?: string } {
  const a = (s.args ?? {}) as Record<string, unknown>;
  const o = (s.output ?? {}) as Record<string, unknown>;
  switch (s.tool) {
    case "understand": {
      const scope = (o.scope ?? a) as Record<string, unknown>;
      const ask = scope.clarification as string | null;
      return { icon: "search", title: ask ? t("需要确认范围") : t("理解问题"),
        detail: ask ?? join(t("指标 {{v1}}", { v1: ((scope.metric_codes as string[]) ?? []).join(t("、")) || t("未确定") }),
          filters(scope.filters as Filter[]), period(scope)) };
    }
    case "describe_metric": return { icon: "chart", title: t("查看口径"), detail: join(a.code as string, o.name as string) };
    case "search_metrics": case "search_assets": {
      const hits = ((o.metrics ?? o.assets) as unknown[] | undefined)?.length;
      return { icon: "search", title: s.tool === "search_assets" ? t("检索表") : t("检索口径"),
        detail: join(a.keyword ? `“${a.keyword}”` : "", hits !== undefined ? t("{{count}} 个结果", { count: hits }) : "") };
    }
    case "describe_asset": return { icon: "table", title: t("查看表结构"), detail: shortName(a.qualified as string) };
    case "lineage": return { icon: "lineage", title: t("查看血缘"), detail: shortName(a.qualified as string) };
    case "changes": return { icon: "change", title: t("检查口径变更"), detail: join(a.metric_code as string, period(a)) };
    case "data_health": return { icon: "shield", title: t("检查数据健康"), detail: join(a.metric_code as string, period(a)) };
    case "explain_change": return { icon: "analysis", title: t("自动归因"),
      detail: join(a.metric_code as string, filters(a.filters as Filter[]), (a.dimensions as string[])?.join(t("、"))) };
    case "query_metric": return { icon: "db", title: t("受治理查询"),
      detail: join(a.metric_code as string, filters(a.filters as Filter[]),
        (a.group_by as string[])?.length ? t("按 {{v1}}", { v1: (a.group_by as string[]).join(t("、")) }) : "", a.time_grain ? t("按{{v1}}", { v1: a.time_grain === "day" ? t("日") : a.time_grain }) : "",
        period(a)) };
    case "query_sql": return { icon: "db", title: t("探索查询"), code: a.sql as string };
    case "load_skill":
      return { icon: "skill", title: t("加载技能"), detail: String(a.name ?? o.name ?? "") || undefined };
    case "read_skill_file": {
      const name = String(a.name ?? o.name ?? "");
      const path = String(a.path ?? o.path ?? "");
      return { icon: "skill", title: t("读取技能文件"), detail: name && path ? `${name} / ${path}` : (name || path || undefined) };
    }
    case "submit_proposal": {
      const items = ((o.items as { source: string }[]) ?? []);
      return { icon: "check", title: t("提出 {{count}} 项分析依据", { count: items.length }),
        detail: t("{{count}} 项是推断，需要确认", { count: items.filter((i) => i.source === "inferred").length }) };
    }
    case "assumptions": {
      const d = ((a.decisions as { decision: string }[]) ?? []);
      return { icon: "check", title: t("你确认了分析依据"),
        detail: join(t("确认 {{count}} 项", { count: d.filter((x) => x.decision === "confirmed").length }),
          d.some((x) => x.decision === "rejected") ? t("否决 {{count}} 项", { count: d.filter((x) => x.decision === "rejected").length }) : "",
          a.supplement ? t("补充：{{supplement}}", { supplement: a.supplement }) : "") };
    }
    case "message": return { icon: "send", title: t("补充指令"), user: String(o.text ?? a.text ?? "") };
    case "compact": {
      const kt = (n: unknown) => (typeof n === "number" ? `${(n / 1000).toFixed(1)}k` : "?");
      return { icon: "fit", title: o.reason === "manual" ? t("按你的要求压缩了上下文") : t("上下文接近上限，已自动压缩"),
        detail: t("{{v1}} → {{v2}} · 早期 {{v3}} 条过程换成数据集清单与步骤摘要", { v1: kt(o.before_tokens), v2: kt(o.after_tokens), v3: o.summarized_messages ?? "?" }) };
    }
    case "submit_report": return { icon: "check", title: t("提交报告"),
      detail: typeof a.title === "string" ? a.title : undefined };
    case "model": {
      const tools = (o.tools as string[]) ?? [];
      return { icon: "chip", title: t("推理"),
        detail: s.status === "skipped" ? t("预算不足，未请求模型")
          : tools.length ? t("决定：{{v1}}", { v1: tools.map((tool) => TOOL_NAME[tool] ?? tool).join(t("、")) }) : t("整理结论"),
        thinking: (o.thinking as string) || undefined, user: undefined, code: undefined,
        ...(o.text ? { detail: `${o.text}` } : {}) };
    }
    default:
      return { icon: "chart", title: CALC[s.tool ?? ""] ?? s.tool ?? s.kind, detail: join(a.label as string,
        a.dataset ? t("基于 {{dataset}}", { dataset: a.dataset }) : "", (a.datasets as string[])?.join(t("、"))) };
  }
}

const TOOL_NAME: Record<string, string> = { query_metric: t("受治理查询"), query_sql: t("探索查询"), explain_change: t("自动归因"),
  describe_asset: t("查看表结构"), search_assets: t("检索表"), search_metrics: t("检索口径"), lineage: t("查看血缘"), submit_report: t("提交报告"),
  submit_proposal: t("提出依据"), submit_scope: t("理解问题"), load_skill: t("加载技能"), read_skill_file: t("读取技能文件"), ...CALC };

type Live = Schemas["AnalysisLiveOut"];
type Dataset = NonNullable<Run["datasets"]>[number];

const reducedMotion = () => typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
const secs = (ms: unknown) => (Number(ms ?? 0) / 1000).toFixed(1);
const firstLine = (text: string) => text.trim().split("\n").find((l) => l.trim())?.trim() ?? "";
// 参数与结果里不展示的工程字段（哈希、校验码、内部编号）
const HIDDEN = /(hash|fingerprint|checksum|digest|_id|^id)$/i;

/** 参数、结果的一层字段：标量原样，列表显示为一行，嵌套对象压成一行。 */
function fields(obj: unknown, skip: string[] = []): [string, string][] {
  if (!obj || typeof obj !== "object") return [];
  const short = (v: string) => (v.length > 200 ? `${v.slice(0, 200)}…` : v);
  return Object.entries(obj as Record<string, unknown>).flatMap(([k, v]): [string, string][] => {
    if (skip.includes(k) || HIDDEN.test(k) || v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length)) return [];
    if (Array.isArray(v)) return [[k, v.every((x) => typeof x !== "object") ? short(v.join(t("、"))) : short(JSON.stringify(v))]];
    return [[k, short(typeof v === "object" ? JSON.stringify(v) : String(v))]];
  });
}

function Fields({ label, rows }: { label: string; rows: [string, string][] }) {
  if (!rows.length) return null;
  return (
    <div className="av-runlog__fields">
      <span className="av-runlog__label">{label}</span>
      <dl>{rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>
    </div>
  );
}

/** 每秒刷新一次（只在运行中），让“已用 N 秒”走起来。 */
function useTicker(active: boolean) {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, [active]);
}

/** 两次轮询（约 2 秒）之间新到的字逐字补上，像流式输出；减少动态效果时直接显示。 */
function useReveal(text: string) {
  const [shown, setShown] = useState(text.length);
  const prev = useRef(text);
  useEffect(() => {
    const before = prev.current;
    prev.current = text;
    if (reducedMotion() || !text.startsWith(before) || text.length - before.length > 4000) { setShown(text.length); return; }
    const from = before.length;
    const start = performance.now();
    let frame = 0;
    const step = (now: number) => {
      const k = Math.min(1, (now - start) / 1800);
      setShown(Math.round(from + (text.length - from) * k));
      if (k < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [text]);
  return text.slice(0, shown);
}

type Mark = "run" | "ok" | "bad" | "skip" | "think";
const MARK_ICON: Record<Exclude<Mark, "run">, string> = { ok: "check", bad: "close", skip: "more", think: "chip" };

/** 一行可折叠条目：状态标记、标题、摘要、数据集角标、耗时；展开看详情。 */
function Item({ mark, title, detail, time, extra, body, open: initial = false, live }: {
  mark: Mark; title: string; detail?: string; time?: string; extra?: ReactNode; body?: ReactNode; open?: boolean; live?: boolean;
}) {
  const [open, setOpen] = useState(initial);
  const head = <>
    {body ? <Icon name="chevron" small /> : <span className="av-runlog__nochev" />}
    {mark === "run" ? <span className="av-spinner" aria-hidden />
      : <span className={`av-runlog__mark is-${mark}`} aria-hidden><Icon name={MARK_ICON[mark]} small /></span>}
    <span className={`av-runlog__title${live ? " av-shimmer" : ""}`}>{title}</span>
    {detail ? <span className="av-runlog__detail">{detail}</span> : null}
  </>;
  return (
    <li className={`av-runlog__item is-${mark}`}>
      <div className="av-runlog__row">
        {body
          ? <button type="button" className="av-runlog__toggle" aria-expanded={open} onClick={() => setOpen(!open)}>{head}</button>
          : <span className="av-runlog__toggle">{head}</span>}
        {extra}
        {time ? <span className="av-runlog__time">{time}</span> : null}
      </div>
      {body && open ? <div className="av-runlog__body">{body}</div> : null}
    </li>
  );
}

function StepRow({ s, runId, datasets }: { s: Step; runId: string; datasets: Dataset[] }) {
  const d = describe(s);
  const bad = s.status !== "ok" && s.status !== "skipped";
  const o = (s.output ?? {}) as Record<string, unknown>;
  const time = s.duration_ms ? `${secs(s.duration_ms)}s` : undefined;
  if (d.user !== undefined) return (
    <li className="av-runlog__user"><span className="av-runlog__bubble">{d.user}</span><span className="av-caption">{t("已生效")}</span></li>);
  const error = bad ? <div className="av-runlog__err">{String(o.error ?? o.message ?? s.status)}</div> : null;
  if (s.kind === "model") {
    const thinking = typeof o.thinking === "string" ? o.thinking : "";
    const text = typeof o.text === "string" ? o.text : "";
    const tools = (o.tools as string[] | undefined) ?? [];
    const decided = tools.length ? t("决定：{{v1}}", { v1: tools.map((tool) => TOOL_NAME[tool] ?? tool).join(t("、")) }) : "";
    const body = thinking || text || bad ? <>
      {thinking ? <p className="av-thought__text">{thinking}</p> : null}
      {text ? <div className="av-runlog__said">{text}</div> : null}
      {decided ? <span className="av-caption">{decided}</span> : null}
      {error}
    </> : undefined;
    return <Item mark={bad ? "bad" : s.status === "skipped" ? "skip" : "think"} title={thinking ? t("已思考") : t("推理")}
      detail={firstLine(thinking) || firstLine(text) || d.detail} time={time} body={body} />;
  }
  const dataset = s.dataset ? datasets.find((x) => x.ref === s.dataset) : undefined;
  const result: [string, string][] = dataset
    ? [[t("数据集"), `${dataset.ref} · ${dataset.title}`], [t("行数"), String(dataset.row_count)]]
    : fields(o, ["error", "message", "dataset"]);
  const body = <>
    {d.detail ? <div className="av-runlog__desc">{d.detail}</div> : null}
    {s.reason ? <div className="av-runlog__desc is-reason">{s.reason}</div> : null}
    {d.code ? <Code sql={d.code} /> : null}
    <Fields label={t("参数")} rows={fields(s.args, ["sql", "purpose"])} />
    <Fields label={t("结果")} rows={result} />
    {error}
  </>;
  return <Item mark={bad ? "bad" : s.status === "skipped" ? "skip" : "ok"} title={d.title} detail={d.detail ?? s.reason ?? undefined}
    time={time} body={body} extra={s.dataset ? <EvidenceLink runId={runId} refId={s.dataset} /> : null} />;
}

/** 正在进行的一步：流式到达的推理文字，或正在执行的工具。 */
function LiveRow({ live, polledAt }: { live: Live; polledAt: number }) {
  const time = `${secs(Number(live.elapsed_ms ?? 0) + Math.max(0, Date.now() - polledAt))}s`;
  const thinking = useReveal(live.thinking ?? "");
  const text = useReveal(live.text ?? "");
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => { if (box.current) box.current.scrollTop = box.current.scrollHeight; }, [thinking, text]);
  if (live.phase === "tool") {
    const d = describe({ seq: live.after_seq + 1, kind: "tool", tool: live.tool, status: "running", args: live.args ?? {}, output: {} } as Step);
    const args = fields(live.args, ["sql", "purpose"]);
    return <Item mark="run" live title={d.title} detail={d.detail} time={time}
      body={args.length || d.code ? <>{d.code ? <Code sql={d.code} /> : null}<Fields label={t("参数")} rows={args} /></> : undefined} />;
  }
  return <Item mark="run" live open title={t("思考中")} time={time}
    body={<div className="av-runlog__stream" ref={box}>
      {thinking ? <p className="av-thought__text">{thinking}<span className="av-caret" aria-hidden /></p>
        : <span className="av-caption">{live.restricted ? t("推理内容可能涉及你当前无权查看的数据，运行中不显示") : t("等待模型返回…")}</span>}
      {text ? <div className="av-runlog__said">{text}</div> : null}
    </div>} />;
}

/** 思考与过程（逐条时间线）：每段推理、每次工具调用各是一行可折叠条目；运行中那一行带转圈，推理文字准实时显示。
 *  运行中整体展开并跟到最新一步（用户往上翻时不打扰）；完成后折叠为“已思考（用时 N 秒）”一行。
 *  插话与停止在页面底部的固定输入框里。 */
export function RunTranscript({ run, live, footer, autoFollow = true }: { run: Run; live?: boolean; footer?: ReactNode; autoFollow?: boolean }) {
  const [open, setOpen] = useState(!!live);
  useEffect(() => { setOpen(!!live); }, [live]);
  const steps = run.steps ?? [];
  const messages = ((run as Run & { messages?: Message[] }).messages ?? []).filter((m) => !m.consumed_at);
  const running = !!live && run.status === "running";
  // 实时过程随运行详情一起轮询（与步骤同一套当前权限投影）；记下收到的时刻，让耗时在两次轮询之间继续走
  const data = running ? run.live ?? null : null;
  const received = useRef({ live: data, at: Date.now() });
  if (received.current.live !== data) received.current = { live: data, at: Date.now() };
  const polledAt = received.current.at;
  // 已经出现更新的步骤时，这条实时状态已过时（那一步完成并写入了步骤记录）
  const current = data && !steps.some((s) => s.seq > data.after_seq) ? data : null;
  useTicker(running);
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
  // 跟到对话区最底部（等待确认时确认面板在过程流下方，也要露出来；推理文字增长时也跟）
  const streamed = (current?.thinking?.length ?? 0) + (current?.text?.length ?? 0);
  useEffect(() => {
    if (!live || !follow) return;
    const scroller = end.current?.closest(".av-analysis__scroll");
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
  }, [live, follow, steps.length, messages.length, run.status, streamed, current?.phase]);
  const spent = steps.reduce((sum, s) => sum + Number(s.duration_ms ?? 0), 0);
  const thoughts = steps.filter((s) => s.kind === "model" && (s.output as { thinking?: string } | null)?.thinking).length;
  const calls = steps.filter((s) => s.kind === "tool").length;
  const failed = steps.filter((s) => s.kind === "tool" && s.status !== "ok" && s.status !== "skipped").length;
  const liveMs = current ? Number(current.elapsed_ms ?? 0) + Math.max(0, Date.now() - polledAt) : 0;
  const summary = [thoughts ? t("{{count}} 段推理", { count: thoughts }) : "", calls ? t("{{count}} 次工具调用", { count: calls }) : "",
    failed ? t("{{count}} 次失败", { count: failed }) : ""].filter(Boolean).join(" · ");
  const last = steps.length ? describe(steps[steps.length - 1]).title : "";
  return (
    <section className={`av-runlog${live ? " is-live" : ""}`} aria-label={t("思考与过程")}>
      <button type="button" className="av-think__hd" aria-expanded={open} onClick={() => setOpen(!open)}>
        <Icon name="chevron" small />
        {running ? <span className="av-spinner" aria-hidden /> : live ? <span className="av-dot av-dot--flow" /> : <Icon name="clock" small />}
        <span className={running ? "av-shimmer" : undefined}>
          {live ? (run.status === "waiting" ? t("思考暂停，等你确认") : t("思考中…"))
            : t("已思考（用时 {{seconds}} 秒）", { seconds: Math.round(spent / 1000) })}</span>
        <span className="av-caption">
          {live ? t("第 {{count}} 步 · 已用 {{seconds}} 秒", { count: steps.length + 1, seconds: Math.round((spent + liveMs) / 1000) }) : summary}
        </span>
      </button>
      <span className="av-sr" aria-live="polite">{live ? last : ""}</span>
      {open ? <>
      <ol className="av-runlog__steps">
        {steps.map((s) => <StepRow key={s.seq} s={s} runId={run.id} datasets={run.datasets ?? []} />)}
        {messages.map((m) => (
          <li key={m.id} className="av-runlog__user"><span className="av-runlog__bubble">{m.text}</span>
            <Pill tone="warn">{t("待生效")}</Pill></li>))}
        {current ? <LiveRow key={`${current.phase}-${current.after_seq}`} live={current} polledAt={polledAt} />
          : live && run.status !== "waiting" ? <li className="av-runlog__item is-run"><div className="av-runlog__row">
            <span className="av-runlog__toggle"><span className="av-runlog__nochev" /><span className="av-spinner" aria-hidden />
              {run.status === "queued" ? <Queued /> : <span className="av-caption">{t("正在进行下一步…")}</span>}</span></div></li> : null}
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
        {/* 运行中补充输入改为严谨书面短语 */}
        <label htmlFor={`int-${run.id}`} className="av-sr">{t("补充指令")}</label>
        <textarea id={`int-${run.id}`} rows={2} maxLength={2000} value={text}
          placeholder={t("补充说明或修正方向，将在下一步生效，例如：改用编码关联而非名称")}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); } }} />
        <div className="av-compose__bar">
          <span className="av-compose__spacer" />
          <ContextMeter run={run} live />
          <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={stop.isPending}
            onClick={() => stop.mutate(undefined, { onError: (e) => setError(errorText(e)) })}>{t("停止")}</button>
          <button type="button" className="av-btn av-btn--primary av-btn--sm" disabled={send.isPending || !text.trim()} onClick={submit}
            title={t("Enter 发送 · Shift+Enter 换行")}>
            <Icon name="send" />{t("发送")}</button>
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
    return <span className="av-runlog__queued"><span className="av-caption">{t("排队中")}</span>
      <span className="av-banner av-banner--warn"><Icon name="info" small />{t("后台执行器没有在运行，任务会在它启动后自动开始。请联系管理员检查 worker 服务。")}</span></span>;
  }
  return <span className="av-caption">{t("排队中，等待后台执行器领取")}</span>;
}
