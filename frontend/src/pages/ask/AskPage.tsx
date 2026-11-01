// 问数：对话模式 —— 会话线程 | 依据栏（口径、来源、SQL、步骤）。追问以候选按钮回答，同一会话保留上下文。
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { api, unwrap } from "../../api/client";
import { errorText, useAction, useExampleQuestions, useThread, useThreads, useTimeZone, type Answer } from "../../api/hooks";
import { Code, CommentDoc, Empty, Hint, Icon, Pill, Status, Tabs } from "../../components/ui";
import { formatClock, formatShortTime, formatTime } from "../../format";
import { changeTone, dateRange, PERIOD_LABEL, planQueries, resultCell, sqlSections, type AskIntent, type AskResult, type DerivedMetric, type PlanQuery } from "./presentation";
import { t } from "../../i18n";

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

export function AskPage() {
  const { threadId } = useParams();
  const navigate = useNavigate();
  const thread = useThread(threadId);
  const tz = useTimeZone();
  const threads = useThreads();
  const [text, setText] = useState("");
  const examples = (useExampleQuestions("ask").data?.questions ?? []).map((q) => q.text);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showHistory, setShowHistory] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const ask = useAction((question: string) => unwrap(api.POST("/api/ask", { body: { question, thread_id: threadId ?? null } })),
    ["thread", "threads"]);
  const answers = thread.data?.answers ?? [];
  const current = answers.find((a) => a.id === selected) ?? [...answers].reverse().find((a) => a.status !== "clarify") ?? answers[answers.length - 1];

  useEffect(() => { bottom.current?.scrollIntoView({ block: "end" }); }, [answers.length, ask.isPending]);

  function send(question: string) {
    if (!question.trim()) return;
    setError(null);
    ask.mutate(question.trim(), {
      onSuccess: (a) => { setText(""); setSelected(a.id); if (!threadId && a.thread_id) navigate(`/ask/${a.thread_id}`); },
      onError: (e) => setError(errorText(e)),
    });
  }
  const pendingQuestion = ask.isPending ? ask.variables : null;
  const model = current?.evidence?.model as { adapter?: string; is_demo?: boolean } | undefined;

  return (
    <div className="av-split av-ask-page" style={{ height: "calc(100vh - 48px)" }}>
      <div style={{ display: "flex", flexDirection: "column", minWidth: 0, height: "100%" }}>
        <div className="av-pagehd" style={{ padding: "16px 24px", borderBottom: "1px solid var(--line-subtle)" }}>
          <div className="av-pagehd__title"><h1>{t("问数")}</h1>
            {model ? (model.is_demo ? <Pill tone="warn" title={t("意图解析使用演示规则")}>{t("演示模型")}</Pill> : <Pill tone="info">{model.adapter}</Pill>) : null}
            <Hint text={t("只按已发布口径回答；口径受影响时拦截，不会悄悄改用其他口径")} /></div>
          <div className="av-actions">
            <button type="button" className="av-btn av-btn--ghost" onClick={() => setShowHistory(!showHistory)}><Icon name="audit" />{t("会话")}</button>
            <button type="button" className="av-btn av-btn--secondary" onClick={() => { setSelected(null); navigate("/ask"); }}><Icon name="plus" />{t("新会话")}</button>
          </div>
        </div>
        {showHistory ? (
          <div style={{ borderBottom: "1px solid var(--line-subtle)", maxHeight: 220, overflow: "auto" }}>
            {(threads.data ?? []).map((thread) => (
              <Link key={thread.id} to={`/ask/${thread.id}`} className="av-row" onClick={() => setShowHistory(false)}
                style={{ "--row-cols": "minmax(0, 1fr) auto auto", minHeight: 40 } as React.CSSProperties}>
                <span className="av-row__title">{thread.title}</span><Status value={thread.status} />
                <span className="av-row__meta av-mono">{formatShortTime(thread.updated_at, tz)}</span>
              </Link>
            ))}
          </div>
        ) : null}
        <div style={{ flex: 1, minHeight: 0, overflow: "auto", padding: "20px 24px" }}>
          <div className="av-thread" style={{ maxWidth: 820, margin: "0 auto" }}>
            {!answers.length && !pendingQuestion ? (
              <Empty text={t("选择一个示例，或输入你的问题")} />
            ) : null}
            {answers.map((a) => (
              <div key={a.id} style={{ display: "contents" }}>
                <div className="av-ask">{a.question}</div>
                <AnswerCard a={a} active={a.id === current?.id} onSelect={() => setSelected(a.id)} onPick={send} disabled={ask.isPending} />
              </div>
            ))}
            {pendingQuestion ? <><div className="av-ask">{pendingQuestion}</div>
              <article className="av-answer"><div className="av-trace"><span className="is-ok">{t("理解中")}</span></div></article></> : null}
            <div ref={bottom} />
          </div>
        </div>
        <div style={{ padding: "12px 24px 20px" }}>
          {error ? <div className="av-banner av-banner--danger" style={{ maxWidth: 820, margin: "0 auto 8px" }}>{error}</div> : null}
          {!answers.length && !pendingQuestion ? (
            <div className="av-ask-examples" aria-label={t("问题示例")}>
              {examples.map((example) => <button key={example} type="button" className="av-btn av-btn--ghost av-btn--sm"
                onClick={() => { setText(example); document.getElementById("q")?.focus(); }}>{example}</button>)}
            </div>
          ) : null}
          <form className="av-composer" style={{ maxWidth: 820, margin: "0 auto" }} onSubmit={(e) => { e.preventDefault(); send(text); }}>
            <label htmlFor="q" className="av-sr">{t("问题")}</label>
            <textarea id="q" rows={2} value={text} placeholder={thread.data?.status === "waiting" ? t("回答上面的追问") : t("问一个问题")}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send(text); }} />
            <div className="av-actions"><span className="av-kbd">Ctrl ↵</span>
              <button className="av-btn av-btn--primary" type="submit" disabled={ask.isPending || !text.trim()}><Icon name="send" />{t("发送")}</button></div>
          </form>
        </div>
      </div>
      <aside className="av-inspector" style={{ height: "100%", overflow: "auto" }} aria-label={t("依据")}>
        {current ? <Evidence a={current} /> : <Empty text={t("回答的依据显示在这里")} />}
      </aside>
    </div>
  );
}

function AnswerCard({ a, active, onSelect, onPick, disabled }: {
  a: Answer; active: boolean; onSelect: () => void; onPick: (q: string) => void; disabled: boolean;
}) {
  const result = a.result as AskResult;
  const intent = a.intent as AskIntent;
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
  return (
    <article className={`av-answer${blocked ? " is-blocked" : ""}`} onClick={onSelect}
      style={active ? { borderColor: "var(--line-default)" } : undefined}>
      <div className="av-answer__hd">
        <Status value={a.status} />
        {mv ? <span className="av-tag">{mv.metric_name} V{mv.version_no}</span> : null}
        {a.cache_hit ? <span className="av-tag">{t("缓存")}</span> : null}
        {a.recheck_status === "needs_recheck" ? <Pill tone="warn">{t("依据已受影响")}</Pill> : null}
        <span className="av-caption av-mono" style={{ marginLeft: "auto" }}>{a.duration_ms ? `${(a.duration_ms / 1000).toFixed(2)} s` : ""}</span>
      </div>
      {intent.order || intent.compare ? (
        <div className="av-answer-meta">
          {intent.order ? <span className="av-tag">{intent.limit ? t("前 {{limit}} · ", { limit: intent.limit }) : ""}{t("按")} {firstMetricName} {intent.order === "asc" ? t("从低到高") : t("从高到低")}</span> : null}
          {intent.compare ? <span className="av-tag">{t("对比")} {PERIOD_LABEL[intent.compare]}</span> : null}
          {intent.compare && comparisonRanges.length ? <span className="av-caption av-mono">{comparisonRanges.join(" · ")}</span> : null}
        </div>
      ) : null}
      {single ? (
        <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
          <span style={{ font: "500 28px/36px var(--font-mono)" }}>{resultCell(result.rows![0][0], result.columns![0])}</span>
          <span className="av-caption">{a.message}</span>
        </div>
      ) : <span style={{ whiteSpace: "pre-wrap" }}>{a.status === "clarify" ? clarification?.question ?? a.message : a.message}</span>}
      {result?.rows && !single && result.rows.length ? (
        <ResultTable result={result} dimensions={intent.group_by ?? []} />
      ) : null}
      {result?.truncated ? <span className="av-caption av-answer-warning">{typeof warning === "string" ? warning : t("结果已截断，仅展示返回的数据")}</span> : null}
      {a.status === "clarify" && clarification?.options?.length ? (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {clarification.options.map((o) => (
            <button key={o} type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={disabled}
              onClick={(e) => { e.stopPropagation(); onPick(o); }}>{o}</button>
          ))}
        </div>
      ) : null}
      {a.explanation && a.explanation_source === "model" ? <span className="av-caption">{t("模型解释：")}{a.explanation}</span> : null}
      {a.status === "blocked" && a.error_code === "metric_affected" ? (
        <div className="av-row" style={{ "--row-cols": "minmax(0, 1fr) auto", minHeight: 0, padding: 0, border: "none", background: "none" } as React.CSSProperties}>
          <span className="av-row__meta">{partiallyCompleted ? t("部分子查询已完成，后续查询已拦截") : t("后端已拦截，未执行查询")}</span>
          <span className="av-row__end"><Link className="av-btn av-btn--secondary av-btn--sm" to="/changes">{t("查看变更")}</Link></span>
        </div>
      ) : null}
      <div className="av-trace">
        {steps.map((s, i) => (
          <span key={i} className={stepClass(s, a)}>
            {STEP_LABEL[String(s.node)] ?? String(s.node)}{s.node === "understand" && Array.isArray(s.tool_calls) && s.tool_calls.length ? t(" · {{count}} 次工具调用", { count: s.tool_calls.length }) : ""}
          </span>
        ))}
        {a.sql ? <span className={a.status === "error" && a.executed_at ? "is-blocked" : a.executed_at ? "is-ok" : ""}>{t("只读执行")}</span> : null}
      </div>
    </article>
  );
}

export function ResultTable({ result, dimensions }: { result: AskResult; dimensions: string[] }) {
  const columns = result.columns ?? [];
  const rows = result.rows ?? [];
  return (
    <div className={`av-panel av-answer-result${rows.length > 20 ? " av-answer-result--scroll" : ""}`}
      role="region" aria-label={t("问数结果")} tabIndex={0}>
      <table className="av-table av-answer-table">
        <thead><tr>{columns.map((column, index) => <th key={`${column}-${index}`} scope="col"
          className={index < dimensions.length ? "av-answer-table__dimension" : "av-answer-table__number"}><span className="av-answer-table__head">{column === "value" ? t("值") : column}</span>
          {index < dimensions.length ? <CommentDoc doc={result.column_docs?.[column]} /> : null}</th>)}</tr></thead>
        <tbody>{rows.map((row, index) => <tr key={index}>{columns.map((column, columnIndex) => {
          const dimension = columnIndex < dimensions.length;
          const value = row[columnIndex];
          const tone = !dimension && /·变化(?:率)?$/.test(column) ? changeTone(value) : "";
          return <td key={columnIndex} className={`${dimension ? "av-answer-table__dimension" : "av-answer-table__number"}${tone ? ` av-answer-change--${tone}` : ""}`}>
            {resultCell(value, column, dimension)}
          </td>;
        })}</tr>)}</tbody>
      </table>
    </div>
  );
}

export function Evidence({ a }: { a: Answer }) {
  const tz = useTimeZone();
  const [tab, setTab] = useState<"source" | "sql" | "steps">("source");
  const ev = a.evidence ?? {};
  const mv = ev.metric_version as { metric_name?: string; version_no?: number; owner?: string; reviewed_by?: string; expression?: string; published_at?: string; status?: string } | undefined;
  const sources = (ev.sources as { qualified: string; kind: string; datasource: string; version_no?: number; is_test_adapter?: boolean }[] | undefined) ?? [];
  const lineage = (ev.lineage as { column: string; role: string; sources: { source: string; relation: string }[] }[] | undefined) ?? [];
  const limits = ev.limits as { query_timeout_seconds?: number; max_rows?: number } | undefined;
  const fresh = a.freshness as { tables?: Record<string, string | null>; last_collected_at?: string; note?: string };
  const queries = planQueries(a);
  const partiallyCompleted = !a.executed_at && queries.some((q) => q.status === "answered");
  const derived = (ev.derived as DerivedMetric[] | undefined) ?? [];
  return (
    <>
      <div className="av-inspector__hd">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
          <span className="av-section-label">{t("依据")}</span><span className="av-mono av-caption">{a.id.slice(0, 8)}</span></div>
        <span style={{ fontWeight: 500 }}>{a.question}</span>
      </div>
      <div style={{ padding: "0 16px" }}>
        <Tabs value={tab} onChange={setTab} options={[{ value: "source", label: t("口径与来源") }, { value: "sql", label: "SQL" }, { value: "steps", label: t("步骤") }]} />
      </div>
      <div className="av-inspector__bd">
        {tab === "source" ? (
          <>
            {mv ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <span className="av-section-label">{t("口径")}</span>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
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
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <span className="av-section-label">{t("字段来源")}</span>
                {lineage.filter((l) => l.role === "measure").map((l) => (
                  <div key={l.column} className="av-chain">
                    <div className="av-chain__item"><span className="av-plat">SR</span><span>{l.column.split(".").slice(-2).join(".")}</span></div>
                    {l.sources.filter((s) => s.relation.startsWith("value")).map((s) => (
                      <div key={s.source} style={{ display: "contents" }}><div className="av-chain__link" />
                        <div className="av-chain__item"><span className="av-plat">SR</span><span>{s.source.split(".").slice(-2).join(".")}</span></div></div>
                    ))}
                  </div>
                ))}
              </div>
            ) : null}
            {sources.length ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <span className="av-section-label">{t("读取的来源")} <Hint text={t("沿血缘与“同一张表”找到的全部来源；每个都需要业务域权限")} /></span>
                {sources.map((s) => (
                  <span key={s.qualified} className="av-chain__item"><span className="av-plat">{s.datasource.includes("MySQL") ? "MY" : "SR"}</span><span>{s.qualified}</span>
                    {s.version_no ? <span className="av-tag" style={{ marginLeft: "auto" }}>v{s.version_no}</span> : null}</span>
                ))}
              </div>
            ) : null}
            <dl className="av-kv">
              <dt>{t("执行")}</dt><dd className="av-mono">{a.executed_at ? formatTime(a.executed_at, tz) : partiallyCompleted ? t("部分子查询已完成") : t("未执行")}{a.cache_hit ? t(" · 缓存") : ""}</dd>
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
