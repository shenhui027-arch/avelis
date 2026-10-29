// 问数：对话模式 —— 会话线程 | 依据栏（口径、来源、SQL、步骤）。追问以候选按钮回答，同一会话保留上下文。
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { api, unwrap } from "../../api/client";
import { errorText, useAction, useThread, useThreads, useTimeZone, type Answer } from "../../api/hooks";
import { Code, Empty, Hint, Icon, Pill, Status, Tabs } from "../../components/ui";
import { formatTime } from "../../format";

const STEP_LABEL: Record<string, string> = { understand: "理解", clarify: "追问", govern: "版本与权限", explain: "解释" };

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
  const threads = useThreads();
  const [text, setText] = useState("");
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
    <div className="av-split" style={{ height: "calc(100vh - 48px)" }}>
      <div style={{ display: "flex", flexDirection: "column", minWidth: 0, height: "100%" }}>
        <div className="av-pagehd" style={{ padding: "16px 24px", borderBottom: "1px solid var(--line-subtle)" }}>
          <div className="av-pagehd__title"><h1>问数</h1>
            {model ? (model.is_demo ? <Pill tone="warn" title="意图解析使用演示规则">演示模型</Pill> : <Pill tone="info">{model.adapter}</Pill>) : null}
            <Hint text="只按已发布口径回答；口径受影响时拦截，不会悄悄改用其他口径" /></div>
          <div className="av-actions">
            <button type="button" className="av-btn av-btn--ghost" onClick={() => setShowHistory(!showHistory)}><Icon name="audit" />会话</button>
            <button type="button" className="av-btn av-btn--secondary" onClick={() => { setSelected(null); navigate("/ask"); }}><Icon name="plus" />新会话</button>
          </div>
        </div>
        {showHistory ? (
          <div style={{ borderBottom: "1px solid var(--line-subtle)", maxHeight: 220, overflow: "auto" }}>
            {(threads.data ?? []).map((t) => (
              <Link key={t.id} to={`/ask/${t.id}`} className="av-row" onClick={() => setShowHistory(false)}
                style={{ "--row-cols": "minmax(0, 1fr) auto auto", minHeight: 40 } as React.CSSProperties}>
                <span className="av-row__title">{t.title}</span><Status value={t.status} />
                <span className="av-row__meta av-mono">{formatTime(t.updated_at, "Asia/Shanghai").slice(5)}</span>
              </Link>
            ))}
          </div>
        ) : null}
        <div style={{ flex: 1, minHeight: 0, overflow: "auto", padding: "20px 24px" }}>
          <div className="av-thread" style={{ maxWidth: 820, margin: "0 auto" }}>
            {!answers.length && !pendingQuestion ? (
              <Empty text="例如：2026-09-15 华东区实付销售额是多少？" />
            ) : null}
            {answers.map((a) => (
              <div key={a.id} style={{ display: "contents" }}>
                <div className="av-ask">{a.question}</div>
                <AnswerCard a={a} active={a.id === current?.id} onSelect={() => setSelected(a.id)} onPick={send} disabled={ask.isPending} />
              </div>
            ))}
            {pendingQuestion ? <><div className="av-ask">{pendingQuestion}</div>
              <article className="av-answer"><div className="av-trace"><span className="is-ok">理解中</span></div></article></> : null}
            <div ref={bottom} />
          </div>
        </div>
        <div style={{ padding: "12px 24px 20px" }}>
          {error ? <div className="av-banner av-banner--danger" style={{ maxWidth: 820, margin: "0 auto 8px" }}>{error}</div> : null}
          <form className="av-composer" style={{ maxWidth: 820, margin: "0 auto" }} onSubmit={(e) => { e.preventDefault(); send(text); }}>
            <label htmlFor="q" className="av-sr">问题</label>
            <textarea id="q" rows={2} value={text} placeholder={thread.data?.status === "waiting" ? "回答上面的追问" : "问一个问题"}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send(text); }} />
            <div className="av-actions"><span className="av-kbd">Ctrl ↵</span>
              <button className="av-btn av-btn--primary" type="submit" disabled={ask.isPending || !text.trim()}><Icon name="send" />发送</button></div>
          </form>
        </div>
      </div>
      <aside className="av-inspector" style={{ height: "100%", overflow: "auto" }} aria-label="依据">
        {current ? <Evidence a={current} /> : <Empty text="回答的依据显示在这里" />}
      </aside>
    </div>
  );
}

function AnswerCard({ a, active, onSelect, onPick, disabled }: {
  a: Answer; active: boolean; onSelect: () => void; onPick: (q: string) => void; disabled: boolean;
}) {
  const result = a.result as { columns?: string[]; rows?: unknown[][]; truncated?: boolean };
  const mv = a.evidence?.metric_version as { metric_name?: string; version_no?: number } | undefined;
  const clarification = a.evidence?.clarification as { question?: string; options?: string[] } | undefined;
  const blocked = a.status === "blocked" || a.status === "denied" || a.status === "error";
  const steps = (a.steps ?? []).filter((s) => s.node !== "clarify");
  const single = result?.rows?.length === 1 && result.columns?.length === 1;
  return (
    <article className={`av-answer${blocked ? " is-blocked" : ""}`} onClick={onSelect}
      style={active ? { borderColor: "var(--line-default)" } : undefined}>
      <div className="av-answer__hd">
        <Status value={a.status} />
        {mv ? <span className="av-tag">{mv.metric_name} V{mv.version_no}</span> : null}
        {a.cache_hit ? <span className="av-tag">缓存</span> : null}
        {a.recheck_status === "needs_recheck" ? <Pill tone="warn">依据已受影响</Pill> : null}
        <span className="av-caption av-mono" style={{ marginLeft: "auto" }}>{a.duration_ms ? `${(a.duration_ms / 1000).toFixed(2)} s` : ""}</span>
      </div>
      {single ? (
        <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
          <span style={{ font: "500 28px/36px var(--font-mono)" }}>{String(result.rows![0][0] ?? "—")}</span>
          <span className="av-caption">{a.message}</span>
        </div>
      ) : <span style={{ whiteSpace: "pre-wrap" }}>{a.status === "clarify" ? clarification?.question ?? a.message : a.message}</span>}
      {result?.rows && !single && result.rows.length ? (
        <div className="av-panel" style={{ borderRadius: "var(--radius-md)" }}>
          <table className="av-table">
            <thead><tr>{result.columns?.map((c) => <th key={c} className={c === "value" ? "num" : undefined}>{c === "value" ? "值" : c}</th>)}</tr></thead>
            <tbody>{result.rows.map((r, i) => <tr key={i}>{r.map((v, j) => <td key={j} className={result.columns?.[j] === "value" ? "num" : "av-mono"}>{String(v ?? "—")}</td>)}</tr>)}</tbody>
          </table>
        </div>
      ) : null}
      {a.status === "clarify" && clarification?.options?.length ? (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {clarification.options.map((o) => (
            <button key={o} type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={disabled}
              onClick={(e) => { e.stopPropagation(); onPick(o); }}>{o}</button>
          ))}
        </div>
      ) : null}
      {a.explanation && a.explanation_source === "model" ? <span className="av-caption">模型解释：{a.explanation}</span> : null}
      {a.status === "blocked" && a.error_code === "metric_affected" ? (
        <div className="av-row" style={{ "--row-cols": "minmax(0, 1fr) auto", minHeight: 0, padding: 0, border: "none", background: "none" } as React.CSSProperties}>
          <span className="av-row__meta">后端已拦截，未执行查询</span>
          <span className="av-row__end"><Link className="av-btn av-btn--secondary av-btn--sm" to="/changes">查看变更</Link></span>
        </div>
      ) : null}
      <div className="av-trace">
        {steps.map((s, i) => (
          <span key={i} className={stepClass(s, a)}>
            {STEP_LABEL[String(s.node)] ?? String(s.node)}{s.node === "understand" && Array.isArray(s.tool_calls) && s.tool_calls.length ? ` · ${s.tool_calls.length} 次工具调用` : ""}
          </span>
        ))}
        {a.sql ? <span className={a.status === "error" && a.executed_at ? "is-blocked" : a.executed_at ? "is-ok" : ""}>只读执行</span> : null}
      </div>
    </article>
  );
}

function Evidence({ a }: { a: Answer }) {
  const tz = useTimeZone();
  const [tab, setTab] = useState<"source" | "sql" | "steps">("source");
  const ev = a.evidence ?? {};
  const mv = ev.metric_version as { metric_name?: string; version_no?: number; owner?: string; reviewed_by?: string; expression?: string; published_at?: string; status?: string } | undefined;
  const sources = (ev.sources as { qualified: string; kind: string; datasource: string; version_no?: number; is_test_adapter?: boolean }[] | undefined) ?? [];
  const lineage = (ev.lineage as { column: string; role: string; sources: { source: string; relation: string }[] }[] | undefined) ?? [];
  const limits = ev.limits as { query_timeout_seconds?: number; max_rows?: number } | undefined;
  const fresh = a.freshness as { tables?: Record<string, string | null>; last_collected_at?: string; note?: string };
  return (
    <>
      <div className="av-inspector__hd">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
          <span className="av-section-label">依据</span><span className="av-mono av-caption">{a.id.slice(0, 8)}</span></div>
        <span style={{ fontWeight: 500 }}>{a.question}</span>
      </div>
      <div style={{ padding: "0 16px" }}>
        <Tabs value={tab} onChange={setTab} options={[{ value: "source", label: "口径与来源" }, { value: "sql", label: "SQL" }, { value: "steps", label: "步骤" }]} />
      </div>
      <div className="av-inspector__bd">
        {tab === "source" ? (
          <>
            {mv ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <span className="av-section-label">口径</span>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  <span className="av-tag">{mv.metric_name} V{mv.version_no}</span>
                  <Status value={mv.status} />
                </div>
                <dl className="av-kv">
                  <dt>负责人</dt><dd>{mv.owner}</dd><dt>复核</dt><dd>{mv.reviewed_by ?? "—"}</dd>
                  <dt>表达式</dt><dd className="av-mono">{mv.expression}</dd><dt>发布于</dt><dd className="av-mono">{formatTime(mv.published_at, tz)}</dd>
                </dl>
              </div>
            ) : null}
            {lineage.length ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <span className="av-section-label">字段来源</span>
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
                <span className="av-section-label">读取的来源 <Hint text="沿血缘与“同一张表”找到的全部来源；每个都需要业务域权限" /></span>
                {sources.map((s) => (
                  <span key={s.qualified} className="av-chain__item"><span className="av-plat">{s.datasource.includes("MySQL") ? "MY" : "SR"}</span><span>{s.qualified}</span>
                    {s.version_no ? <span className="av-tag" style={{ marginLeft: "auto" }}>v{s.version_no}</span> : null}</span>
                ))}
              </div>
            ) : null}
            <dl className="av-kv">
              <dt>执行</dt><dd className="av-mono">{a.executed_at ? formatTime(a.executed_at, tz) : "未执行"}{a.cache_hit ? " · 缓存" : ""}</dd>
              <dt>限制</dt><dd>{limits ? `只读 · 超时 ${limits.query_timeout_seconds}s · ≤${limits.max_rows} 行` : "—"}</dd>
              <dt>元数据</dt><dd className="av-mono">{fresh?.last_collected_at ? formatTime(fresh.last_collected_at, tz) : "—"}</dd>
              <dt>数据</dt><dd className="av-mono">{Object.entries(fresh?.tables ?? {}).map(([t, v]) => `${t} ${v ?? ""}`).join(" · ") || "—"}</dd>
              <dt>模型</dt><dd>{String((ev.model as { adapter?: string })?.adapter ?? "")}</dd>
            </dl>
            <span className="av-caption">{String(ev.traceability ?? "可追溯 ≠ 可复现：不保存数据快照。")}</span>
          </>
        ) : null}
        {tab === "sql" ? (a.sql ? <Code sql={a.sql} /> : <span className="av-muted">没有生成或执行 SQL{a.message ? `：${a.message}` : ""}</span>) : null}
        {tab === "steps" ? (
          <ol className="av-steps">
            {(a.steps ?? []).map((s, i) => (
              <li key={i} className={`av-step ${stepClass(s, a)}`}>
                <span className="av-step__dot" />
                <span className="av-step__body">
                  <span className="av-step__title">{STEP_LABEL[String(s.node)] ?? String(s.node)} {s.result ? `· ${String(s.result)}` : ""}{s.status ? ` · ${String(s.status)}` : ""}</span>
                  <span className="av-step__meta">{Array.isArray(s.tool_calls) ? (s.tool_calls as { tool: string }[]).map((c) => c.tool).join(" → ") : ""} {formatTime(String(s.at ?? ""), tz).slice(11)}</span>
                </span>
              </li>
            ))}
          </ol>
        ) : null}
      </div>
    </>
  );
}
