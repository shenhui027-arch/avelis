// 问数：对话模式 —— 会话线程 | 依据栏（口径、来源、SQL、步骤）。追问以候选按钮回答，同一会话保留上下文。
import { useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { api, ApiError, unwrap } from "../../api/client";
import { errorText, useAction, useExampleQuestions, useNumberStyle, useThread, useThreads, useTimeZone, type Answer, type ThreadDetail } from "../../api/hooks";
import { consumeSse } from "../../api/sse";
import { Code, CommentDoc, Empty, Hint, Icon, Pill, Status, Tabs } from "../../components/ui";
import { Banner, useMessage } from "../../components/ui";
import { submitOnEnter } from "../../components/shortcuts";
import { formatClock, formatShortTime, formatTime } from "../../format";
import { cellText, changeTone, columnLabel, columnRole, dateRange, PERIOD_LABEL, planQueries, sqlSections, type AskIntent, type AskResult, type DerivedMetric, type PlanQuery } from "./presentation";
import { t } from "../../i18n";
import { ChartPanel } from "../../components/charts/ChartPanel";
import { FilterAlignmentNote } from "../../components/FilterAlignmentNote";
import { Insights, Narrative } from "../../components/AnswerInsights";
import { AnswerEvidenceBadges, AnswerFeedback, GroundingWarning, parseAnswerEvidence, SaveAsVqrButton } from "../../components/AnswerActions";

const STEP_LABEL: Record<string, string> = { understand: t("理解"), clarify: t("追问"), govern: t("版本与权限"), explain: t("解释") };

/** 每一步按它自己的结果着色：成功为绿，拦截或失败为红，追问、未执行、未知保持中性——不用最终答案代替步骤状态。 */
function stepClass(s: Record<string, unknown>, a: { status: string }): string {
  switch (s.node) {
    case "understand":
      return s.result === "intent" ? "is-ok" : "";
    case "govern":
      return s.status === "answered" ? "is-ok" : s.status === "blocked" || s.status === "denied" || s.status === "error"
        ? "is-blocked" : "";
    case "explain":
      return a.status === "answered" ? "is-ok" : "";
    default:
      return "";
  }
}

type PendingAsk = {
  question: string;
  phase: string | null;
  status: string | null;
  tool: string | null;
  steps: Record<string, unknown>[];
  thinking: string;
  explanation: string;
};

class StreamRejectedError extends Error {}

export function AskPage() {
  const tz = useTimeZone();
  const qc = useQueryClient();
  const { threadId } = useParams();
  const navigate = useNavigate();
  const thread = useThread(threadId);
  const threads = useThreads();
  const [text, setText] = useState("");
  const examples = (useExampleQuestions("ask").data?.questions ?? []).map((q) => q.text);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [compactMsg, setCompactMsg] = useMessage();
  const [showHistory, setShowHistory] = useState(false);
  const [pending, setPending] = useState<PendingAsk | null>(null);
  const streamAbort = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const ask = useAction((question: string) => unwrap(api.POST("/api/ask", { body: { question, thread_id: threadId ?? null } })),
    ["thread", "threads"]);
  const compact = useAction(() => unwrap(api.POST("/api/ask/threads/{thread_id}/compact", {
    params: { path: { thread_id: threadId! } },
  })), ["thread", "threads"]);
  const answers = thread.data?.answers ?? [];
  const current = answers.find((a) => a.id === selected) ?? [...answers].reverse().find((a) => a.status !== "clarify") ?? answers[answers.length - 1];
  const isBusy = !!pending || ask.isPending;

  useEffect(() => () => { streamAbort.current?.abort(); }, []);
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [answers.length, isBusy, pending?.steps.length, pending?.thinking.length, pending?.explanation.length]);

  const finishAnswer = (a: Answer) => {
    qc.setQueryData<ThreadDetail>(["thread", a.thread_id], (prev) => {
      if (!prev) return prev;
      const exists = prev.answers.some((item) => item.id === a.id);
      return { ...prev, answers: exists ? prev.answers.map((item) => (item.id === a.id ? a : item)) : [...prev.answers, a] };
    });
    void qc.invalidateQueries({ queryKey: ["thread", a.thread_id] });
    void qc.invalidateQueries({ queryKey: ["threads"] });
    setText("");
    setSelected(a.id);
    if (!threadId && a.thread_id) navigate(`/ask/${a.thread_id}`);
  };

  async function send(question: string) {
    const trimmed = question.trim();
    if (!trimmed || isBusy) return;
    setError(null);
    streamAbort.current?.abort();
    const ctrl = new AbortController();
    streamAbort.current = ctrl;
    setPending({
      question: trimmed,
      phase: "understand",
      status: "running",
      tool: null,
      steps: [],
      thinking: "",
      explanation: "",
    });
    let completed = false;
    let streamOpened = false;
    try {
      const res = await consumeSse<Answer>("/api/ask/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: trimmed, thread_id: threadId ?? null }),
        signal: ctrl.signal,
        onEvent: (ev) => {
          streamOpened = true;
          if (ctrl.signal.aborted) return;
          const payload = (ev.data && typeof ev.data === "object" ? ev.data : {}) as Record<string, unknown>;
          if (ev.event === "status") {
            setPending((prev) => (prev ? {
              ...prev,
              phase: typeof payload.phase === "string" ? payload.phase : prev.phase,
              status: typeof payload.status === "string" ? payload.status : prev.status,
              tool: typeof payload.tool === "string" ? payload.tool : null,
            } : prev));
          } else if (ev.event === "step") {
            const step = payload.step as Record<string, unknown> | undefined;
            if (step) setPending((prev) => (prev ? { ...prev, steps: [...prev.steps, step] } : prev));
          } else if (ev.event === "thinking") {
            const content = typeof payload.content === "string" ? payload.content : undefined;
            const delta = typeof payload.delta === "string" ? payload.delta : "";
            const phase = typeof payload.phase === "string" ? payload.phase : undefined;
            setPending((prev) => (prev ? {
              ...prev,
              phase: phase ?? prev.phase,
              thinking: content !== undefined ? content : `${prev.thinking}${delta}`,
            } : prev));
          } else if (ev.event === "delta") {
            const content = typeof payload.content === "string" ? payload.content : undefined;
            const delta = typeof payload.delta === "string" ? payload.delta : "";
            const phase = typeof payload.phase === "string" ? payload.phase : undefined;
            setPending((prev) => (prev ? {
              ...prev,
              phase: phase ?? prev.phase,
              explanation: content !== undefined ? content : `${prev.explanation}${delta}`,
            } : prev));
          } else if (ev.event === "error") {
            const msg = typeof payload.detail === "string" ? payload.detail
              : typeof payload.message === "string" ? payload.message : t("操作失败");
            throw new StreamRejectedError(msg);
          } else if (ev.event === "done") {
            completed = true;
            setPending(null);
            finishAnswer(ev.data as Answer);
          }
        },
      });
      if (ctrl.signal.aborted) return;
      if (res.mode === "json" && res.data) {
        completed = true;
        setPending(null);
        finishAnswer(res.data);
        return;
      }
      if (!completed) throw new Error("stream_incomplete");
    } catch (e) {
      if (ctrl.signal.aborted) return;
      if (e instanceof StreamRejectedError) {
        setPending(null);
        setError(e.message);
        return;
      }
      if (e instanceof ApiError && ![404, 405, 406, 501].includes(e.status)) {
        setPending(null);
        setError(errorText(e));
        return;
      }
      if (streamOpened) {
        // 流式已开始执行后中途中断，不自动重发避免重复执行与双重扣费
        setPending(null);
        setError(t("流式连接已中断，请刷新查看结果或重试"));
        return;
      }
      // 仅在流式端点不可用（未实际开始执行）时回退到普通 JSON 问数接口
      setPending(null);
      ask.mutate(trimmed, {
        onSuccess: (a) => finishAnswer(a),
        onError: (err) => setError(errorText(err)),
      });
    }
  }
  const pendingQuestion = isBusy ? (pending?.question ?? ask.variables ?? null) : null;
  const model = current?.evidence?.model as { adapter?: string; is_demo?: boolean } | undefined;

  // 高度与窄屏单列堆叠移入 pages.css，内联间距与列宽统一提取为语义类名
  return (
    <div className="av-split av-ask-page">
      <div className="av-ask-page__main">
        <div className="av-pagehd av-ask-page__hd">
          <div className="av-pagehd__title"><h1>{t("问数")}</h1>
            {model ? (model.is_demo ? <Pill tone="warn" title={t("意图解析使用演示规则")}>{t("演示模型")}</Pill> : <Pill tone="info">{model.adapter}</Pill>) : null}
            <Hint text={t("只按已发布口径回答；口径受影响时拦截，不会悄悄改用其他口径")} /></div>
          <div className="av-actions">
            {threadId && answers.length >= 2 ? (
              <button
                type="button"
                className="av-btn av-btn--ghost"
                disabled={compact.isPending || isBusy}
                onClick={() => {
                  setCompactMsg(null);
                  compact.mutate(undefined, {
                    onSuccess: (res) => setCompactMsg({
                      ok: true,
                      text: res.compacted
                        ? t("已压缩上下文（{{before}} → {{after}} 条消息）", { before: res.before_messages, after: res.after_messages })
                        : t("当前上下文较短，暂无需压缩"),
                    }),
                    onError: (e) => setCompactMsg({ ok: false, text: errorText(e) }),
                  });
                }}
              >
                <Icon name="fit" small />
                {compact.isPending ? t("压缩中…") : t("压缩上下文")}
              </button>
            ) : null}
            <Link to="/analysis" className="av-btn av-btn--ghost">{t("前往统一分析工作区")}</Link>
            <button type="button" className="av-btn av-btn--ghost" onClick={() => setShowHistory(!showHistory)}><Icon name="audit" />{t("会话")}</button>
            <button type="button" className="av-btn av-btn--secondary" onClick={() => { setSelected(null); navigate("/analysis"); }}><Icon name="plus" />{t("新建会话")}</button>
          </div>
        </div>
        <Banner message={compactMsg} />
        {showHistory ? (
          <div className="av-ask-page__history">
            {(threads.data ?? []).map((thread) => (
              <Link key={thread.id} to={`/ask/${thread.id}`} className="av-row av-ask-page__thread-row" onClick={() => setShowHistory(false)}>
                <span className="av-row__title">{thread.title}</span><Status value={thread.status} />
                <span className="av-row__meta av-mono">{formatShortTime(thread.updated_at, tz)}</span>
              </Link>
            ))}
          </div>
        ) : null}
        <div className="av-ask-page__scroll">
          <div className="av-thread">
            {thread.data?.compacted_summary || thread.data?.compacted_at ? (
              <details className="av-fold av-ask-compact">
                <summary>
                  <Icon name="fit" small />
                  <span>{t("已压缩早期上下文")}</span>
                  {thread.data.compacted_at ? (
                    <span className="av-caption av-mono av-fold__aside">{formatShortTime(thread.data.compacted_at, tz)}</span>
                  ) : null}
                </summary>
                {thread.data.compacted_summary ? (
                  <pre className="av-ask-compact__summary">{thread.data.compacted_summary}</pre>
                ) : null}
              </details>
            ) : null}
            {!answers.length && !pendingQuestion ? (
              <Empty text={t("选择一个示例，或输入你的问题")} />
            ) : null}
            {answers.map((a) => (
              <Fragment key={a.id}>
                <div className="av-ask">{a.question}</div>
                <AnswerCard a={a} active={a.id === current?.id} onSelect={() => setSelected(a.id)} onPick={send} disabled={isBusy} />
              </Fragment>
            ))}
            {pendingQuestion ? <PendingAnswerCard question={pendingQuestion} pending={pending} /> : null}
            <div ref={bottom} />
          </div>
        </div>
        <div className="av-ask-page__foot">
          {error ? <div className="av-banner av-banner--danger av-ask-page__error" role="alert">{error}</div> : null}
          {!answers.length && !pendingQuestion ? (
            <div className="av-ask-examples" aria-label={t("问题示例")}>
              {examples.map((example) => <button key={example} type="button" className="av-btn av-btn--ghost av-btn--sm"
                onClick={() => { setText(example); document.getElementById("q")?.focus(); }}>{example}</button>)}
            </div>
          ) : null}
          <form className="av-composer" onSubmit={(e) => { e.preventDefault(); void send(text); }}>
            <label htmlFor="q" className="av-sr">{t("问题")}</label>
            <textarea id="q" rows={2} value={text} placeholder={thread.data?.status === "waiting" ? t("回答上面的追问") : t("问一个问题")}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => { if (submitOnEnter(e, "mod-enter")) { e.preventDefault(); void send(text); } }} />
            <div className="av-actions"><span className="av-kbd">Ctrl ↵</span>
              <button className="av-btn av-btn--primary" type="submit" disabled={isBusy || !text.trim()}><Icon name="send" />{t("发送")}</button></div>
          </form>
        </div>
      </div>
      <aside className="av-inspector av-ask-page__inspector" aria-label={t("依据")}>
        {current ? <Evidence a={current} /> : <Empty text={t("回答的依据显示在这里")} />}
      </aside>
    </div>
  );
}

function PendingAnswerCard({ question, pending }: { question: string; pending: PendingAsk | null }) {
  const phase = pending?.phase ?? "understand";
  const phaseLabel = pending?.status === "failover"
    ? t("正在切换备选模型…")
    : phase === "govern"
      ? t("校验口径与权限中…")
      : phase === "explain"
        ? t("生成解读中…")
        : t("理解中");
  return (
    <>
      <div className="av-ask">{question}</div>
      <article className="av-answer is-streaming" aria-live="polite">
        <div className="av-answer__hd">
          <span className="av-status av-status--info">
            <span className="av-spinner" aria-hidden />
            {phaseLabel}
          </span>
          {pending?.tool ? <span className="av-tag av-mono">{pending.tool}</span> : null}
        </div>
        {pending?.thinking ? (
          <p className="av-thought__text">
            {pending.thinking}
            {!pending.explanation ? <span className="av-caret" aria-hidden /> : null}
          </p>
        ) : null}
        {pending?.explanation ? (
          <div className="av-runlog__said">
            {pending.explanation}
            <span className="av-caret" aria-hidden />
          </div>
        ) : null}
        <div className="av-trace">
          {(pending?.steps ?? []).map((s, i) => (
            <span key={i} className="is-ok">
              {STEP_LABEL[String(s.node)] ?? String(s.node)}
            </span>
          ))}
          {!pending?.steps.some((s) => String(s.node) === phase) ? (
            <span className="is-ok">{STEP_LABEL[phase] ?? phaseLabel}</span>
          ) : null}
        </div>
      </article>
    </>
  );
}

function AnswerCard({ a, active, onSelect, onPick, disabled }: {
  a: Answer; active: boolean; onSelect: () => void; onPick: (q: string) => void; disabled: boolean;
}) {
  const result = a.result as AskResult;
  const intent = a.intent as AskIntent;
  const style = useNumberStyle();
  const queries = planQueries(a);
  const mv = a.evidence?.metric_version as { metric_code?: string; metric_name?: string; version_no?: number } | undefined;
  const clarification = a.evidence?.clarification as { question?: string; options?: string[] } | undefined;
  const blocked = a.status === "blocked" || a.status === "denied" || a.status === "error";
  const steps = (a.steps ?? []).filter((s) => s.node !== "clarify");
  const single = result?.rows?.length === 1 && result.columns?.length === 1;
  const firstMetric = result.columns?.[intent.group_by?.length ?? 0];
  const firstCode = intent.metric_codes?.[0] ?? intent.metric_code;
  const evidenceMetricName = !mv?.metric_code || !firstCode || mv.metric_code === firstCode ? mv?.metric_name : undefined;
  const firstMetricName = firstMetric && firstMetric !== "value" ? firstMetric : evidenceMetricName ?? firstCode ?? t("指标");
  const partiallyCompleted = !a.executed_at && queries.some((q) => q.status === "answered");
  const comparisonRanges = [...new Set(queries.filter((q) => q.period === intent.compare).map((q) => dateRange(q.range)))];
  const warning = a.evidence?.warning;
  const { grounding } = parseAnswerEvidence(a);
  const showModelNarrative = !!(a.explanation && a.explanation_source === "model");
  return (
    <article className={`av-answer${blocked ? " is-blocked" : ""}${active ? " is-active" : ""}`} onClick={onSelect}>
      <div className="av-answer__hd">
        <Status value={a.status} />
        {mv ? <span className="av-tag">{mv.metric_name} V{mv.version_no}</span> : null}
        <AnswerEvidenceBadges a={a} />
        <BasisState a={a} />
        <span className="av-caption av-mono av-answer__duration">{a.duration_ms ? `${(a.duration_ms / 1000).toFixed(2)} s` : ""}</span>
      </div>
      {intent.order || intent.compare ? (
        <div className="av-answer-meta">
          {intent.order ? <span className="av-tag">{intent.limit ? t("前 {{limit}} · ", { limit: intent.limit }) : ""}{t("按")} {firstMetricName} {intent.order === "asc" ? t("从低到高") : t("从高到低")}</span> : null}
          {intent.compare ? <span className="av-tag">{t("对比")} {PERIOD_LABEL[intent.compare]}</span> : null}
          {intent.compare && comparisonRanges.length ? <span className="av-caption av-mono">{comparisonRanges.join(" · ")}</span> : null}
        </div>
      ) : null}
      {single ? (
        <div className="av-answer__single">
          <span className="av-answer__value">{cellText(result, result.columns![0], result.rows![0][0], false, style)}</span>
          <span className="av-caption">{a.message}</span>
        </div>
      ) : <span className="av-answer__text">{a.status === "clarify" ? clarification?.question ?? a.message : a.message}</span>}
      <FilterAlignmentNote items={a.filter_alignment} />
      {result?.insights?.length && !single ? <Insights items={result.insights} /> : null}
      {showModelNarrative ? <Narrative text={a.explanation!} onPick={onPick} disabled={disabled} grounding={grounding} />
        : grounding?.ok === false ? <GroundingWarning grounding={grounding} /> : null}
      {result?.rows && !single && result.rows.length ? (
        <ChartPanel charts={result.charts ?? []} title={a.message ?? a.question}>
          <ResultTable result={result} dimensions={intent.group_by ?? []} />
        </ChartPanel>
      ) : null}
      {result?.truncated ? <span className="av-caption av-answer-warning">{typeof warning === "string" ? warning : t("结果已截断，仅展示返回的数据")}</span> : null}
      {a.status === "clarify" && clarification?.options?.length ? (
        <div className="av-answer__options">
          {clarification.options.map((o) => (
            <button key={o} type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={disabled}
              onClick={(e) => { e.stopPropagation(); onPick(o); }}>{o}</button>
          ))}
        </div>
      ) : null}
      {a.status === "blocked" && a.error_code === "metric_affected" ? (
        <div className="av-row av-answer__blocked">
          <span className="av-row__meta">{partiallyCompleted ? t("部分子查询已完成，后续查询已拦截") : t("后端已拦截，未执行查询")}</span>
          <span className="av-row__end"><Link className="av-btn av-btn--secondary av-btn--sm" to="/changes">{t("查看变更")}</Link></span>
        </div>
      ) : null}
      <div className="av-answer__ft">
      <div className="av-trace">
        {steps.map((s, i) => (
          <span key={i} className={stepClass(s, a)}>
            {STEP_LABEL[String(s.node)] ?? String(s.node)}{s.node === "understand" && Array.isArray(s.tool_calls) && s.tool_calls.length ? t(" · {{count}} 次工具调用", { count: s.tool_calls.length }) : ""}
          </span>
        ))}
        {a.sql ? <span className={a.status === "error" && a.executed_at ? "is-blocked" : a.executed_at ? "is-ok" : ""}>{t("只读执行")}</span> : null}
      </div>
      <div className="av-actions av-answer__actions">
        <AnswerFeedback answer={a} />
        <SaveAsVqrButton answer={a} />
        {a.status === "answered" && result?.rows?.length ? <DeepenButton a={a} result={result} dimensions={intent.group_by ?? []} /> : null}
      </div>
      </div>
    </article>
  );
}

/** 深入分析：把同一个问题交给分析智能体，产出带归因与依据的报告。 */
function DeepenButton({ a, result, dimensions }: { a: Answer; result: AskResult; dimensions: string[] }) {
  const navigate = useNavigate();
  // 追问（如“按地区呢？”）只有一句片段：带上这条回答已解析出的期间、筛选、口径与维度，分析智能体才有完整的问题
  const head = (a.message ?? "").split("：")[0].trim();
  const dims = dimensions.length ? t("，按 {{dims}}", { dims: dimensions.map((c) => columnLabel(result, c)).join("、") }) : "";
  const question = head ? `${a.question}（${head}${dims}）` : a.question;
  const start = useAction(() => unwrap(api.POST("/api/analysis/runs", { body: { question, confirmation_mode: "auto" } as never })), ["analysis-runs"]);
  return (
    <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={start.isPending}
      onClick={(e) => { e.stopPropagation(); start.mutate(undefined, { onSuccess: (r) => navigate(`/analysis/runs/${r.id}`) }); }}>
      <Icon name="analysis" small />{start.isPending ? t("正在开始…") : t("深入分析")}</button>
  );
}

export function ResultTable({ result, dimensions }: { result: AskResult; dimensions: string[] }) {
  const style = useNumberStyle();
  const columns = result.columns ?? [];
  const [sort, setSort] = useState<{ index: number; desc: boolean } | null>(null);
  const rows = sortedRows(result.rows ?? [], sort);
  return (
    <div className={`av-panel av-answer-result${rows.length > 20 ? " av-answer-result--scroll" : ""}`}
      role="region" aria-label={t("问数结果")} tabIndex={0}>
      <table className="av-table av-answer-table">
        <thead><tr>{columns.map((column, index) => {
          const dimension = index < dimensions.length;
          const order = sort?.index === index ? (sort.desc ? "descending" : "ascending") : "none";
          return <th key={`${column}-${index}`} scope="col" aria-sort={order}
            className={dimension ? "av-answer-table__dimension" : "av-answer-table__number"}>
            <button type="button" className="av-answer-table__sort" title={t("按此列排序")}
              onClick={() => setSort(nextSort(sort, index, !dimension))}>
              <span className="av-answer-table__head">{columnLabel(result, column)}</span>
              {order !== "none" ? <span aria-hidden="true">{order === "descending" ? "↓" : "↑"}</span> : null}
            </button>
            {dimension ? <CommentDoc doc={result.column_docs?.[column]} /> : null}</th>;
        })}</tr></thead>
        <tbody>{rows.map((row, index) => <tr key={index}>{columns.map((column, columnIndex) => {
          const dimension = columnIndex < dimensions.length;
          const value = row[columnIndex];
          const role = columnRole(result, column);
          const tone = !dimension && (role === "change" || role === "rate") ? changeTone(value, result.polarity?.[column]) : "";
          return <td key={columnIndex} className={`${dimension ? "av-answer-table__dimension" : "av-answer-table__number"}${tone ? ` av-answer-change--${tone}` : ""}`}>
            {cellText(result, column, value, dimension, style)}
          </td>;
        })}</tr>)}</tbody>
      </table>
    </div>
  );
}

/** 表头的排序循环：数值列先降序、维度列先升序；再点一次反向，第三次恢复原顺序。 */
function nextSort(sort: { index: number; desc: boolean } | null, index: number, firstDesc: boolean) {
  if (sort?.index !== index) return { index, desc: firstDesc };
  return sort.desc === firstDesc ? { index, desc: !firstDesc } : null;
}

/** 点表头排序：数值列按数值，其余按文字。 */
function sortedRows(rows: unknown[][], sort: { index: number; desc: boolean } | null): unknown[][] {
  if (!sort) return rows;
  const key = (v: unknown) => (v == null ? null : typeof v === "number" ? v : Number.isFinite(Number(v)) && String(v).trim() !== "" ? Number(v) : String(v));
  return [...rows].sort((a, b) => {
    const x = key(a[sort.index]), y = key(b[sort.index]);
    if (x === null || y === null) return x === y ? 0 : x === null ? 1 : -1;
    const c = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y));
    return sort.desc ? -c : c;
  });
}

/** 答案当前的依据状态：上游变化让所用口径待复核时标出来；历史数值保留，只是不再代表当前结构。 */
export function BasisState({ a }: { a: Answer }) {
  return a.recheck_status === "needs_recheck" ? <Pill tone="warn">{t("依据已受影响")}</Pill> : null;
}

export function Evidence({ a }: { a: Answer }) {
  const tz = useTimeZone();
  const [tab, setTab] = useState<"source" | "sql" | "steps">("source");
  const ev = a.evidence ?? {};
  const mv = ev.metric_version as { metric_name?: string; version_no?: number; owner?: string; reviewed_by?: string; expression?: string; published_at?: string; status?: string } | undefined;
  const sources = (ev.sources as { asset_id?: string; qualified: string; kind: string; datasource: string; version_no?: number; is_test_adapter?: boolean }[] | undefined) ?? [];
  const lineage = (ev.lineage as { column: string; role: string; sources: { source: string; relation: string }[] }[] | undefined) ?? [];
  const limits = ev.limits as { query_timeout_seconds?: number; max_rows?: number } | undefined;
  const fresh = a.freshness as { tables?: Record<string, string | null>; last_collected_at?: string; note?: string };
  const queries = planQueries(a);
  const partiallyCompleted = !a.executed_at && queries.some((q) => q.status === "answered");
  const derived = (ev.derived as DerivedMetric[] | undefined) ?? [];
  const { vqrRefs, semanticCache } = parseAnswerEvidence(a);
  return (
    <>
      <div className="av-inspector__hd">
        <div className="av-evidence-hd__top">
          <span className="av-section-label">{t("依据")}</span><span className="av-mono av-caption">{a.id.slice(0, 8)}</span></div>
        <span className="av-evidence-hd__question">{a.question}</span>
      </div>
      <div className="av-evidence-tabs">
        <Tabs value={tab} onChange={setTab} options={[{ value: "source", label: t("口径与来源") }, { value: "sql", label: "SQL" }, { value: "steps", label: t("步骤") }]} />
      </div>
      <div className="av-inspector__bd">
        {tab === "source" ? (
          <>
            {mv ? (
              <div className="av-evidence-section">
                <span className="av-section-label">{t("口径")}</span>
                <div className="av-evidence-tags">
                  <span className="av-tag">{mv.metric_name} V{mv.version_no}</span>
                  <Status value={mv.status} />
                </div>
                <dl className="av-kv">
                  <dt>{t("负责人")}</dt><dd>{mv.owner}</dd><dt>{t("复核")}</dt><dd>{mv.reviewed_by ?? "—"}</dd>
                  <dt>{t("表达式")}</dt><dd className="av-mono">{mv.expression}</dd><dt>{t("发布于")}</dt><dd className="av-mono">{formatTime(mv.published_at, tz)}</dd>
                </dl>
              </div>
            ) : null}
            {derived.map((metric) => (
              <section key={metric.metric_code} className="av-evidence-section" aria-label={t("派生指标 {{metric_code}}", { metric_code: metric.metric_code })}>
                <span className="av-section-label">{t("派生指标")} <span className="av-mono">{metric.metric_code}</span></span>
                <code className="av-derived-formula">{metric.formula}</code>
                <span className="av-caption">{t("锁定的组成版本")}</span>
                <ul className="av-derived-components">
                  {metric.components.map((component) => <li key={`${component.metric_code}-${component.version_id}`}>
                    {component.metric_id ? <Link to={`/catalog/metrics/${component.metric_id}`}>{component.metric_name} · V{component.version_no}</Link>
                      : <span>{component.metric_name} · V{component.version_no}</span>}
                    <span className="av-caption av-mono">{component.metric_code}</span>
                  </li>)}
                </ul>
              </section>
            ))}
            {lineage.length ? (
              <div className="av-evidence-section">
                <span className="av-section-label">{t("字段来源")}</span>
                {lineage.filter((l) => l.role === "measure").map((l) => (
                  <div key={l.column} className="av-chain">
                    <div className="av-chain__item"><span>{l.column.split(".").slice(-2).join(".")}</span></div>
                    {l.sources.filter((s) => s.relation.startsWith("value")).map((s) => (
                      <Fragment key={s.source}><div className="av-chain__link" />
                        <div className="av-chain__item"><span>{s.source.split(".").slice(-2).join(".")}</span></div></Fragment>
                    ))}
                  </div>
                ))}
              </div>
            ) : null}
            {sources.length ? (
              <div className="av-evidence-section">
                <span className="av-section-label">{t("读取的来源")} <Hint text={t("沿血缘与“同一张表”找到的全部来源；每个都需要业务域权限")} /></span>
                {sources.map((s) => (
                  <span key={s.qualified} className="av-chain__item"><span className="av-plat">{s.datasource}</span><span>{s.asset_id ? <Link to={`/catalog/assets/${s.asset_id}`}>{s.qualified}</Link> : s.qualified}</span>
                    {s.version_no ? <span className="av-tag av-evidence-source__ver">v{s.version_no}</span> : null}</span>
                ))}
              </div>
            ) : null}
            {vqrRefs.length ? (
              <div className="av-evidence-section">
                <span className="av-section-label">{t("参考标准查询")}</span>
                <div className="av-evidence-tags">
                  {vqrRefs.map((ref) => (
                    <Link key={ref.id} to={`/catalog/queries?id=${encodeURIComponent(ref.id)}`} className="av-pill av-pill--info">
                      <Icon name="check" small />{ref.title}
                    </Link>
                  ))}
                </div>
              </div>
            ) : null}
            <dl className="av-kv">
              <dt>{t("执行")}</dt><dd className="av-mono">{a.executed_at ? formatTime(a.executed_at, tz) : partiallyCompleted ? t("部分子查询已完成") : t("未执行")}{a.cache_hit ? (semanticCache?.hit ? t(" · 语义缓存") : t(" · 缓存")) : ""}</dd>
              <dt>{t("限制")}</dt><dd>{limits ? t("只读 · 超时 {{query_timeout_seconds}}s · ≤{{max_rows}} 行", { query_timeout_seconds: limits.query_timeout_seconds, max_rows: limits.max_rows }) : "—"}</dd>
              <dt>{t("元数据")}</dt><dd className="av-mono">{fresh?.last_collected_at ? formatTime(fresh.last_collected_at, tz) : "—"}</dd>
              <dt>{t("数据")}</dt><dd className="av-mono">{Object.entries(fresh?.tables ?? {}).map(([t, v]) => `${t} ${v ?? ""}`).join(" · ") || "—"}</dd>
              <dt>{t("模型")}</dt><dd>{String((ev.model as { adapter?: string })?.adapter ?? "")}</dd>
            </dl>
            <span className="av-caption">{String(ev.traceability ?? t("可追溯 ≠ 可复现：不保存数据快照。"))}</span>
          </>
        ) : null}
        {tab === "sql" ? (a.sql ? <div className="av-sql-sections">
          {sqlSections(a.sql).map((section, index) => <section key={index} className="av-sql-section">
            {section.title ? <span className="av-section-label">{section.title}</span> : null}
            <Code sql={section.sql} />
          </section>)}
        </div> : <span className="av-muted">{t("没有生成或执行 SQL")}{a.message ? `：${a.message}` : ""}</span>) : null}
        {tab === "steps" ? (
          <>
          {queries.length ? <QueryPlan queries={queries} /> : null}
          <ol className="av-steps">
            {(a.steps ?? []).map((s, i) => (
              <li key={i} className={`av-step ${stepClass(s, a)}`}>
                <span className="av-step__dot" />
                <span className="av-step__body">
                  <span className="av-step__title">{STEP_LABEL[String(s.node)] ?? String(s.node)} {s.result ? `· ${String(s.result)}` : ""}{s.status ? ` · ${String(s.status)}` : ""}</span>
                  <span className="av-step__meta">{Array.isArray(s.tool_calls) ? (s.tool_calls as { tool: string }[]).map((c) => c.tool).join(" → ") : ""} {formatClock(String(s.at ?? ""), tz)}</span>
                </span>
              </li>
            ))}
          </ol>
          </>
        ) : null}
      </div>
    </>
  );
}

function QueryPlan({ queries }: { queries: PlanQuery[] }) {
  return (
    <section className="av-evidence-section" aria-label={t("执行计划")}>
      <span className="av-section-label">{t("执行计划 · {{count}} 个子查询", { count: queries.length })}</span>
      <ol className="av-query-plan">
        {queries.map((query, index) => <li key={`${query.metric_code}-${query.period}-${index}`} className="av-query-plan__item">
          <div className="av-query-plan__header">
            <span className="av-mono">{query.metric_code}</span>
            <span className="av-tag">{PERIOD_LABEL[query.period] ?? query.period}</span>
            <Status value={query.status} />
          </div>
          <div className="av-query-plan__meta">
            <span className="av-mono">{dateRange(query.range)}</span>
            <span>{query.cache_hit === true ? t("命中缓存") : query.cache_hit === false ? t("未命中缓存") : t("缓存状态未知")}</span>
          </div>
        </li>)}
      </ol>
    </section>
  );
}
