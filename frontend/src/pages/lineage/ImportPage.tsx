// 解析边直接写入；模型候选经人工确认后进入血缘。
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { ApiError, api, token, unwrap } from "../../api/client";
import { errorText, useAction, useDatasources, useImport, useImports, useTimeZone, type ImportJob } from "../../api/hooks";
import {
  Banner, Count, Empty, Hint, Icon, PaneHeader, QueryState, RELATION_LABEL, Seg, Select, Status, Tabs, highlight, shortName, useMessage,
} from "../../components/ui";
import { formatTime } from "../../format";

type WorkspaceTab = "source" | "result";
type CandidateDecision = { id: string; decision: "accept" | "reject" | "modify"; reason: string; relation?: string; target_column?: string };
const MAX_FILE_BYTES = 2 * 1024 * 1024;

export function ImportPage() {
  const { jobId } = useParams();
  const imports = useImports();
  const tz = useTimeZone();
  const [historyOpen, setHistoryOpen] = useState(false);
  return (
    <div className="av-fill av-import-page">
      <div className="av-pagehd">
        <div className="av-pagehd__title"><h1>导入血缘</h1><Hint text="解析边直接写入；模型候选需人工确认" /></div>
        <div className="av-actions av-import-actions">
          <button type="button" className="av-btn av-btn--secondary av-import-history-toggle" aria-expanded={historyOpen}
            aria-controls="import-history" onClick={() => setHistoryOpen(!historyOpen)}>导入记录</button>
          <Link className="av-btn av-btn--secondary" to="/lineage"><Icon name="lineage" />血缘</Link>
          <Link className="av-btn av-btn--primary" to="/lineage/import"><Icon name="plus" />新导入</Link>
        </div>
      </div>
      <div className="av-workspace av-import-workspace" data-history-open={historyOpen}
        onKeyDown={(e) => { if (e.key === "Escape") setHistoryOpen(false); }}>
        <section id="import-history" className="av-pane av-pane--rail av-import-history" aria-label="导入记录">
          <PaneHeader title="导入记录" count={imports.isSuccess ? imports.data.length : undefined}>
            <button type="button" className="av-btn av-btn--ghost av-btn--sm av-import-history-toggle" onClick={() => setHistoryOpen(false)}>收起</button>
          </PaneHeader>
          <div className="av-scroll"><QueryState query={imports} isEmpty={(data) => !data.length} emptyText="还没有导入记录" compact>
            {(data) => data.map((j) => <Link key={j.id} to={`/lineage/import/${j.id}`} onClick={() => setHistoryOpen(false)}
              aria-current={j.id === jobId ? "page" : undefined} className={`av-row av-import-history__row${j.id === jobId ? " is-active" : ""}`}>
              <span className="av-row__main"><span className="av-row__title av-mono">{j.filename}</span>
                <span className="av-row__meta">{formatTime(j.created_at, tz).slice(5)}</span></span><Status value={j.status} />
            </Link>)}
          </QueryState></div>
        </section>
        {jobId ? <JobView key={jobId} jobId={jobId} /> : <NewImport />}
      </div>
    </div>
  );
}

function WorkspaceTabs({ value, onChange, isNew = false }: { value: WorkspaceTab; onChange: (value: WorkspaceTab) => void; isNew?: boolean }) {
  return <div className="av-import-mobile-tabs"><Tabs value={value} onChange={onChange}
    options={[{ value: "source", label: "原文" }, { value: "result", label: isNew ? "解析设置" : "识别结果" }]} /></div>;
}

function NewImport() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const sources = useDatasources();
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>("source");
  const [mode, setMode] = useState<"paste" | "file">("paste");
  const [content, setContent] = useState("");
  const [filename, setFilename] = useState("粘贴的 SQL.sql");
  const [datasource, setDatasource] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useMessage();
  const fileInput = useRef<HTMLInputElement>(null);
  const submitting = useRef(false);
  const availableSources = (sources.data ?? []).filter((d) => d.engine.capabilities.includes("view_lineage"));
  const ds = availableSources.some((d) => d.id === datasource) ? datasource : availableSources[0]?.id || "";

  function selectFile(selected: File | null) {
    const error = selected && !/\.(sql|py|sh|bash)$/i.test(selected.name) ? "请选择 .sql、.py、.sh 或 .bash 文件"
      : selected && selected.size > MAX_FILE_BYTES ? "文件不能超过 2 MB" : selected && !selected.size ? "文件为空" : "";
    setFileError(error);
    setFile(error ? null : selected);
    if (error && fileInput.current) fileInput.current.value = "";
  }

  async function submit() {
    if (submitting.current || !sources.isSuccess || !ds || (mode === "file" ? !file || !!fileError : !content.trim() || !filename.trim())) return;
    submitting.current = true;
    setBusy(true);
    setMessage(null);
    try {
      let job: ImportJob;
      if (mode === "file" && file) {
        const form = new FormData();
        form.append("datasource_id", ds);
        form.append("file", file);
        const resp = await fetch("/api/imports/upload", { method: "POST", body: form, headers: { Authorization: `Bearer ${token.get()}` } });
        const json = await resp.json();
        if (!resp.ok) throw new Error(json.detail ?? "上传失败");
        job = json;
      } else {
        job = await unwrap(api.POST("/api/imports", { body: { datasource_id: ds, filename: filename.trim(), content, source_type: null, default_db: null, params: {} } }));
      }
      qc.invalidateQueries({ queryKey: ["imports"] });
      qc.invalidateQueries({ queryKey: ["graph"] });
      navigate(`/lineage/import/${job.id}`);
    } catch (e) {
      setMessage({ ok: false, text: errorText(e) });
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }

  return <>
    <WorkspaceTabs value={workspaceTab} onChange={setWorkspaceTab} isNew />
    <section className="av-pane av-import-source" aria-label="原文" data-active={workspaceTab === "source"}>
      <PaneHeader title="原文"><Seg value={mode} onChange={setMode} label="来源"
        options={[{ value: "paste", label: "粘贴 SQL" }, { value: "file", label: "上传文件" }]} /></PaneHeader>
      {mode === "paste" ? <textarea className="av-editor av-editor--input" aria-label="SQL 原文"
        placeholder={"INSERT INTO ads.daily_net\nSELECT order_date, region, SUM(paid_amount - refund_amount) AS net\nFROM shop.orders GROUP BY order_date, region;"}
        value={content} onChange={(e) => setContent(e.target.value)} /> : <div className="av-empty av-import-upload">
        <input ref={fileInput} type="file" className="av-import-file-input" accept=".sql,.py,.sh,.bash"
          aria-label="上传原文文件" aria-describedby="import-file-hint" aria-invalid={!!fileError}
          onChange={(e) => selectFile(e.target.files?.[0] ?? null)} />
        <span className="av-import-filename">{file ? file.name : "选择原文文件"}</span>
        <span id="import-file-hint" className="av-caption">SQL / Python / Shell，UTF-8，最多 2 MB</span>
        {fileError ? <span className="av-import-field-error" role="alert">{fileError}</span> : null}
        <button type="button" className="av-btn av-btn--secondary" onClick={() => fileInput.current?.click()}><Icon name="upload" />选择文件</button>
      </div>}
      <div className="av-import-mobile-next av-actions"><button type="button" className="av-btn av-btn--primary"
        disabled={mode === "paste" ? !content.trim() : !file} onClick={() => setWorkspaceTab("result")}>解析设置</button></div>
    </section>
    <section className="av-pane av-import-results" aria-label="解析设置" data-active={workspaceTab === "result"}>
      <PaneHeader title="解析设置" />
      <div className="av-scroll"><div className="av-stack">
        <QueryState query={sources} isEmpty={() => !availableSources.length} emptyText="没有可导入血缘的数据源" compact
          emptyAction={<Link className="av-btn av-btn--secondary" to="/settings/datasources">查看数据源</Link>}>
          {() => <label className="av-field"><span className="av-label">数据源 <Hint text="使用已采集表结构解析列" /></span>
            <Select label="数据源" value={ds} onChange={setDatasource}>{availableSources.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}</Select></label>}
        </QueryState>
        {mode === "paste" ? <label className="av-field"><span className="av-label">文件名</span><input className="av-input av-mono" value={filename} maxLength={255} onChange={(e) => setFilename(e.target.value)} /></label> : null}
        <Banner message={message} />
        <div className="av-actions av-import-actions" aria-busy={busy}>
          <button type="button" className="av-btn av-btn--primary av-btn--lg"
            disabled={busy || !sources.isSuccess || !ds || (mode === "paste" ? !content.trim() || !filename.trim() : !file || !!fileError)} onClick={submit}>{busy ? "解析中…" : "解析"}</button>
        </div>
      </div></div>
    </section>
  </>;
}

function JobView({ jobId }: { jobId: string }) {
  const job = useImport(jobId);
  if (job.isError && job.error instanceof ApiError && job.error.status === 404) return <section className="av-pane av-import-query-state">
    <Empty text="导入任务不存在" action={<Link className="av-btn av-btn--primary" to="/lineage/import">新导入</Link>} /></section>;
  if (!job.isSuccess) return <section className="av-pane av-import-query-state" aria-label="导入任务"><QueryState query={job}>{() => null}</QueryState></section>;
  return <JobContent j={job.data} />;
}

function JobContent({ j }: { j: ImportJob }) {
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>("result");
  const [tab, setTab] = useState<"statements" | "candidates" | "payload">("candidates");
  const [focus, setFocus] = useState<string | null>(null);
  const sourceLines = useRef(new Map<number, HTMLSpanElement>());
  const lines = useMemo(() => (j.content ?? "").split("\n"), [j.content]);
  const focused = j.statements?.find((s) => s.id === focus);
  const hl = new Set<number>();
  const ai = new Set<number>();
  for (const s of j.statements ?? []) {
    for (let n = s.lines[0]; n <= s.lines[1]; n++) (s.method === "unrecognized" ? ai : hl).add(n);
  }
  useEffect(() => {
    if (focused) sourceLines.current.get(focused.lines[0])?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [focused, workspaceTab]);
  function locate(id: string) {
    setFocus(id);
    setWorkspaceTab("source");
    requestAnimationFrame(() => {
      const firstLine = j.statements?.find((s) => s.id === id)?.lines[0];
      const line = firstLine === undefined ? undefined : sourceLines.current.get(firstLine);
      line?.scrollIntoView({ block: "center", inline: "nearest" });
      line?.focus({ preventScroll: true });
    });
  }
  const pending = j.candidates?.filter((c) => c.status === "pending").length;
  const summary = [
    { label: "语句", n: j.statements?.length },
    { label: "写入", n: j.candidates?.filter((c) => ["written", "accepted", "modified"].includes(c.status)).length },
    { label: "待确认", n: pending },
    { label: "待补参", n: j.statements?.filter((s) => s.status === "needs_params").length },
    { label: "失败", n: j.statements?.filter((s) => ["failed", "error"].includes(s.status)).length },
  ];
  return <>
    <div className="av-import-summary" aria-label="导入结果摘要"><dl>{summary.map(({ label, n }) => <div key={label}><dt>{label}</dt><dd>{n ?? "—"}</dd></div>)}</dl>
      <Status value={j.status} label={j.status === "processing" ? "解析中" : undefined} /></div>
    <WorkspaceTabs value={workspaceTab} onChange={setWorkspaceTab} />
    <section className="av-pane av-import-source" aria-label="原文" data-active={workspaceTab === "source"}>
      <PaneHeader title={<><span className="av-mono av-import-filename">{j.filename}</span><span className="av-tag">{j.language}</span></>} />
      <div className="av-editor av-import-editor" tabIndex={0} aria-label="原文与行号">
        <div className="av-editor__gutter">{lines.map((_, i) => `${i + 1}\n`).join("")}</div>
        <div className="av-editor__code">{lines.map((line, i) => {
          const n = i + 1;
          const inFocus = !!focused && n >= focused.lines[0] && n <= focused.lines[1];
          return <span key={i} tabIndex={-1} ref={(element) => { if (element) sourceLines.current.set(n, element); else sourceLines.current.delete(n); }}
            className={`${ai.has(n) ? "is-ai" : hl.has(n) ? "is-hl" : ""}${inFocus ? " is-focused" : ""}`}>{highlight(line)}{" "}</span>;
        })}</div>
      </div>
    </section>
    <section className="av-pane av-import-results" aria-label="识别结果" data-active={workspaceTab === "result"}>
      <div className="av-import-result-tabs"><Tabs value={tab} onChange={setTab} options={[
        { value: "candidates", label: <>候选边 {pending ? <Count n={pending} /> : null}</> },
        { value: "statements", label: <>语句 <Count n={j.statements?.length ?? 0} /></> }, { value: "payload", label: "发给模型" },
      ]} /></div>
      <div className="av-scroll">
        {tab === "statements" ? <Statements j={j} onFocus={setFocus} onLocate={locate} /> : null}
        {tab === "candidates" ? <Candidates j={j} onFocus={setFocus} onLocate={locate} /> : null}
        {tab === "payload" ? <Payload j={j} /> : null}
      </div>
    </section>
  </>;
}

const METHOD: Record<string, string> = { sql: "SQL", python_ast: "Python 内嵌 SQL", shell: "Shell 内嵌 SQL", unrecognized: "未识别片段" };
type SourceActions = { j: ImportJob; onFocus: (id: string) => void; onLocate: (id: string) => void };

function Statements({ j, onFocus, onLocate }: SourceActions) {
  const placeholders = [...new Set((j.statements ?? []).filter((s) => s.status === "needs_params").flatMap((s) => s.placeholders))];
  const [params, setParams] = useState<Record<string, string>>({});
  const [message, setMessage] = useMessage();
  const save = useAction(() => unwrap(api.PUT("/api/imports/{job_id}/params", { params: { path: { job_id: j.id } }, body: { params } })), ["import", "imports", "graph"]);
  return <>
    {placeholders.length ? <div className="av-stack av-import-params">
      <span className="av-section-label">动态 SQL 需要参数 <Hint text="补齐后重新解析这些语句" /></span>
      {placeholders.map((p) => <label key={p} className="av-field"><span className="av-label av-mono">{p}</span>
        <input className="av-input av-mono" value={params[p] ?? ""} placeholder="例如 2026-09-15" onChange={(e) => setParams({ ...params, [p]: e.target.value })} /></label>)}
      <Banner message={message} />
      <div className="av-actions av-import-actions" aria-busy={save.isPending}>
        <button type="button" className="av-btn av-btn--primary" disabled={save.isPending || placeholders.some((p) => !params[p]?.trim())}
          onClick={() => save.mutate(undefined, { onSuccess: () => setMessage({ ok: true, text: "已补参数并重新解析" }), onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>{save.isPending ? "解析中…" : "重新解析"}</button>
      </div>
    </div> : null}
    {!j.statements?.length ? <Empty text="没有识别出的语句" compact /> : <div className="av-list">{j.statements.map((s) => <div key={s.id}
      className="av-row av-import-statement" onMouseEnter={() => onFocus(s.id)} onFocus={() => onFocus(s.id)}>
      <span className="av-mono av-caption">{s.lines[0]}–{s.lines[1]}</span>
      <span className="av-row__main"><span className="av-row__title">{s.statement_type ?? METHOD[s.method]}{s.target ? <span className="av-mono"> → {shortName(s.target)}</span> : null}</span>
        <span className="av-row__meta">{METHOD[s.method]}{s.reason ? ` · ${s.reason}` : ""}</span></span>
      <Status value={s.status} /><button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={() => onLocate(s.id)}>定位原文</button>
    </div>)}</div>}
  </>;
}

function Candidates({ j, onFocus, onLocate }: SourceActions) {
  const hasModel = (j.candidates ?? []).some((c) => c.origin === "model");
  const [filter, setFilter] = useState<"model" | "parsed">(hasModel ? "model" : "parsed");
  const [editing, setEditing] = useState<string | null>(null);
  const [edit, setEdit] = useState({ relation: "", target_column: "", reason: "" });
  const [reason, setReason] = useState("");
  const [pendingIds, setPendingIds] = useState<Set<string>>(new Set());
  const pendingRef = useRef(new Set<string>());
  const [message, setMessage] = useMessage();
  const decide = useAction((v: CandidateDecision) => unwrap(api.POST("/api/candidates/{candidate_id}/decision", {
    params: { path: { candidate_id: v.id } }, body: { decision: v.decision, reason: v.reason.trim(), relation: v.relation || null, target_column: v.target_column || null },
  })), ["import", "imports", "graph"]);
  const list = (j.candidates ?? []).filter((c) => filter === "model" ? c.origin === "model" : c.origin !== "model");
  async function submit(value: CandidateDecision) {
    if (pendingRef.current.has(value.id) || !value.reason.trim()) return;
    pendingRef.current.add(value.id);
    setPendingIds(new Set(pendingRef.current));
    setMessage(null);
    try {
      await decide.mutateAsync(value);
      setMessage({ ok: true, text: value.decision === "reject" ? "已驳回" : value.decision === "modify" ? "已修改并写入" : "已接受并写入" });
      setEditing((current) => current === value.id ? null : current);
    } catch (e) { setMessage({ ok: false, text: errorText(e) }); }
    finally { pendingRef.current.delete(value.id); setPendingIds(new Set(pendingRef.current)); }
  }
  return <>
    <div className="av-stack av-import-candidate-tools">
      <Seg value={filter} onChange={setFilter} label="来源" options={[
        { value: "model", label: <>模型候选 <Count n={(j.candidates ?? []).filter((c) => c.origin === "model").length} /></> },
        { value: "parsed", label: <>已解析 <Count n={(j.candidates ?? []).filter((c) => c.origin !== "model").length} /></> },
      ]} />
      {filter === "model" ? <label className="av-field"><span className="av-label">处理理由（接受 / 驳回必填）</span>
        <input className="av-input" value={reason} onChange={(e) => setReason(e.target.value)} /></label> : null}
      <Banner message={message} />
    </div>
    {list.map((c) => {
      const pending = pendingIds.has(c.id);
      const statement = c.statement_id;
      const evidence = c.evidence as { rationale?: string; lines?: number[] };
      return <div key={c.id} className="av-import-candidate" aria-busy={pending}>
        <div className="av-cand av-cand--review" tabIndex={0} onMouseEnter={() => { if (statement) onFocus(statement); }}
          onFocus={(e) => { if (statement) onFocus(statement); e.currentTarget.scrollIntoView({ block: "nearest" }); }}>
          <span className="av-cand__edge">
            <span>{shortName(c.source_ref)}{c.source_column ? `.${c.source_column}` : ""} → {shortName(c.target_ref)}{c.target_column ? `.${c.target_column}` : ""}</span>
            <span>{String(evidence.rationale ?? c.expression ?? "")}{evidence.lines?.length ? ` · 第 ${evidence.lines.join("–")} 行` : ""}</span>
          </span>
          <div className="av-import-candidate-meta"><span className="av-tag">{RELATION_LABEL[c.relation] ?? c.relation}</span>
            {c.confidence != null ? <span className="av-conf" aria-label={`置信度 ${c.confidence.toFixed(2)}`}><i><b style={{ width: `${Math.round(c.confidence * 100)}%` }} /></i>{c.confidence.toFixed(2)}</span> : null}
            <Status value={c.status} />{pending ? <span className="av-caption" role="status">处理中…</span> : null}
          </div>
          <div className="av-actions av-import-candidate-actions">
            <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={!statement} onClick={() => { if (statement) onLocate(statement); }}>定位原文</button>
            {c.status === "pending" ? <>
              <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={pending || !reason.trim()} onClick={() => submit({ id: c.id, decision: "reject", reason })}>驳回</button>
              <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={pending} onClick={() => { setEditing(c.id); setEdit({ relation: c.relation, target_column: c.target_column ?? "", reason: "" }); }}>修改</button>
              <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={pending || !reason.trim()} onClick={() => submit({ id: c.id, decision: "accept", reason })}>接受</button>
            </> : c.status === "written" ? <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={pending}
              onClick={() => { setEditing(c.id); setEdit({ relation: c.relation, target_column: c.target_column ?? "", reason: "" }); }}>修改</button> : null}
          </div>
        </div>
        {editing === c.id ? <div className="av-stack av-import-candidate-edit">
          <div className="av-formrow"><label className="av-field"><span className="av-label">关系</span>
            <Select value={edit.relation} onChange={(v) => setEdit({ ...edit, relation: v })}>{Object.entries(RELATION_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</Select></label>
            <label className="av-field"><span className="av-label">目标列</span><input className="av-input av-mono" value={edit.target_column} onChange={(e) => setEdit({ ...edit, target_column: e.target.value })} /></label></div>
          <label className="av-field"><span className="av-label">修改理由</span><input className="av-input" value={edit.reason} onChange={(e) => setEdit({ ...edit, reason: e.target.value })} /></label>
          <div className="av-actions av-import-actions">
            <button type="button" className="av-btn av-btn--secondary" disabled={pending} onClick={() => setEditing(null)}>取消</button>
            <button type="button" className="av-btn av-btn--primary" disabled={pending || !edit.reason.trim()}
              onClick={() => submit({ id: c.id, decision: "modify", reason: edit.reason, relation: edit.relation, target_column: edit.target_column })}>{pending ? "保存中…" : "保存修改"}</button>
          </div>
        </div> : null}
      </div>;
    })}
    {!list.length ? <Empty text={filter === "model" ? "没有模型候选" : "没有解析出的边"} compact
      action={filter === "model" ? <button type="button" className="av-btn av-btn--secondary" onClick={() => setFilter("parsed")}>查看已解析</button> : undefined} /> : null}
  </>;
}

function Payload({ j }: { j: ImportJob }) {
  if (!j.model_payload?.length) return <Empty text="没有需要模型推断的片段" compact />;
  return <div className="av-stack">{j.model_payload.map((p, i) => <section key={i} className="av-panel">
    <div className="av-panel__hd"><h2 className="av-panel__title">第 {(p.lines as number[]).join("–")} 行</h2>
      <span className={`av-status ${String(p.sent_to).startsWith("未发送") ? "" : "av-status--warn"}`}>{String(p.sent_to)}</span></div>
    <div className="av-panel__bd av-import-payload">
      <span className="av-caption">已脱敏 {String(p.redactions)} 处{p.note ? ` · ${String(p.note)}` : ""}{p.error ? ` · ${String(p.error)}` : ""}</span>
      <pre className="av-code">{String(p.text)}</pre>
    </div>
  </section>)}</div>;
}
