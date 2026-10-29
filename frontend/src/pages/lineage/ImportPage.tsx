// 导入血缘：原文编辑器（识别的语句行 is-hl，模型推断的片段 is-ai）| 识别出的数据流与候选边。
// 解析边直接写入（可修改）；模型候选必须接受、修改或驳回后才进入血缘，并记录处理人和理由。
import { useQueryClient } from "@tanstack/react-query";
import { useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { api, token, unwrap } from "../../api/client";
import { errorText, useAction, useDatasources, useImport, useImports, useTimeZone, type ImportJob } from "../../api/hooks";
import {
  Banner, Count, Empty, Hint, Icon, PaneHeader, RELATION_LABEL, Seg, Select, Status, Tabs, highlight, shortName, useMessage,
} from "../../components/ui";
import { formatTime } from "../../format";

export function ImportPage() {
  const { jobId } = useParams();
  const imports = useImports();
  const tz = useTimeZone();
  return (
    <div className="av-fill">
      <div className="av-pagehd">
        <div className="av-pagehd__title"><h1>导入血缘</h1>
          <Hint text="上传 SQL 文件、粘贴 SQL，或上传 Python / Shell 代码。确定性解析的边直接写入；解析不了的片段由模型提出候选，需人工确认" /></div>
        <div className="av-actions">
          <Link className="av-btn av-btn--secondary" to="/lineage"><Icon name="lineage" />血缘</Link>
          <Link className="av-btn av-btn--secondary" to="/lineage/import"><Icon name="plus" />新导入</Link>
        </div>
      </div>
      <div className="av-workspace" style={{ gridTemplateColumns: "240px minmax(0, 1fr) 520px" }}>
        <section className="av-pane av-pane--rail" aria-label="导入记录">
          <PaneHeader title="导入记录" count={imports.data?.length ?? 0} />
          <div className="av-scroll">
            {(imports.data ?? []).map((j) => (
              <Link key={j.id} to={`/lineage/import/${j.id}`} className={`av-row${j.id === jobId ? " is-active" : ""}`}
                style={{ "--row-cols": "minmax(0, 1fr) auto", minHeight: 52 } as React.CSSProperties}>
                <span className="av-row__main"><span className="av-row__title av-mono">{j.filename}</span>
                  <span className="av-row__meta">{formatTime(j.created_at, tz).slice(5)}</span></span>
                <Status value={j.status} />
              </Link>
            ))}
          </div>
        </section>
        {jobId ? <JobView key={jobId} jobId={jobId} /> : <NewImport />}
      </div>
    </div>
  );
}

function NewImport() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const sources = useDatasources();
  const [mode, setMode] = useState<"paste" | "file">("paste");
  const [content, setContent] = useState("");
  const [filename, setFilename] = useState("粘贴的 SQL.sql");
  const [datasource, setDatasource] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useMessage();
  const fileInput = useRef<HTMLInputElement>(null);
  const ds = datasource || sources.data?.find((d) => d.kind === "starrocks")?.id || "";

  async function submit() {
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
        job = await unwrap(api.POST("/api/imports", { body: { datasource_id: ds, filename, content, source_type: null, default_db: null, params: {} } }));
      }
      qc.invalidateQueries({ queryKey: ["imports"] });
      qc.invalidateQueries({ queryKey: ["graph"] });
      if (job.duplicate) setMessage({ ok: true, text: "相同内容已导入过，打开原任务（不会重复写入）" });
      navigate(`/lineage/import/${job.id}`);
    } catch (e) {
      setMessage({ ok: false, text: errorText(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <section className="av-pane" aria-label="原文">
        <PaneHeader title="原文">
          <Seg value={mode} onChange={setMode} label="来源" options={[{ value: "paste", label: "粘贴 SQL" }, { value: "file", label: "上传文件" }]} />
        </PaneHeader>
        {mode === "paste" ? (
          <textarea className="av-editor" style={{ flex: 1, border: "none", padding: 12, color: "var(--text-primary)", resize: "none", outline: "none", display: "block" }}
            aria-label="SQL" placeholder={"INSERT INTO ads.daily_net\nSELECT order_date, region, SUM(paid_amount - refund_amount) AS net\nFROM shop.orders GROUP BY order_date, region;"}
            value={content} onChange={(e) => setContent(e.target.value)} />
        ) : (
          <div className="av-empty">
            <input ref={fileInput} type="file" accept=".sql,.py,.sh,.bash" style={{ display: "none" }} onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
            <span>{file ? file.name : "支持 .sql、.py、.sh，单个文件不超过 2 MB"}</span>
            <button type="button" className="av-btn av-btn--secondary" onClick={() => fileInput.current?.click()}><Icon name="upload" />选择文件</button>
          </div>
        )}
      </section>
      <section className="av-pane" aria-label="设置">
        <PaneHeader title="解析设置" />
        <div className="av-stack">
          <label className="av-field"><span className="av-label">数据源 <Hint text="用该数据源已采集的表结构解析列，并把来源对应到资产" /></span>
            <Select value={ds} onChange={setDatasource}>{(sources.data ?? []).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}</Select></label>
          {mode === "paste" ? <label className="av-field"><span className="av-label">文件名</span><input className="av-input av-mono" value={filename} onChange={(e) => setFilename(e.target.value)} /></label> : null}
          <Banner message={message} />
          <div className="av-actions" style={{ justifyContent: "flex-end" }}>
            <button type="button" className="av-btn av-btn--primary av-btn--lg" disabled={busy || !ds || (mode === "paste" ? !content.trim() : !file)} onClick={submit}>
              {busy ? "解析中" : "解析"}</button>
          </div>
        </div>
      </section>
    </>
  );
}

function JobView({ jobId }: { jobId: string }) {
  const job = useImport(jobId);
  const [tab, setTab] = useState<"statements" | "candidates" | "payload">("candidates");
  const [focus, setFocus] = useState<string | null>(null);
  const j = job.data;
  const lines = useMemo(() => (j?.content ?? "").split("\n"), [j?.content]);
  if (!j) return <section className="av-pane" />;
  const hl = new Set<number>();
  const ai = new Set<number>();
  for (const s of j.statements ?? []) {
    for (let n = s.lines[0]; n <= s.lines[1]; n++) (s.method === "unrecognized" ? ai : hl).add(n);
  }
  const focused = (j.statements ?? []).find((s) => s.id === focus);
  const pending = (j.candidates ?? []).filter((c) => c.status === "pending").length;
  return (
    <>
      <section className="av-pane" aria-label="原文">
        <PaneHeader title={<><span className="av-mono">{j.filename}</span><span className="av-tag">{j.language}</span></>}>
          <Status value={j.status} />
        </PaneHeader>
        <div className="av-editor" style={{ flex: 1 }}>
          <div className="av-editor__gutter">{lines.map((_, i) => `${i + 1}\n`).join("")}</div>
          <div className="av-editor__code">
            {lines.map((line, i) => {
              const n = i + 1;
              const inFocus = focused && n >= focused.lines[0] && n <= focused.lines[1];
              return <span key={i} className={ai.has(n) ? "is-ai" : hl.has(n) ? "is-hl" : undefined}
                style={inFocus ? { boxShadow: "inset 2px 0 0 var(--signal)" } : undefined}>{highlight(line)}{" "}</span>;
            })}
          </div>
        </div>
      </section>
      <section className="av-pane" aria-label="识别结果">
        <div style={{ padding: "0 16px" }}>
          <Tabs value={tab} onChange={setTab} options={[
            { value: "candidates", label: <>候选边 {pending ? <Count n={pending} /> : null}</> },
            { value: "statements", label: <>语句 <Count n={j.statements?.length ?? 0} /></> },
            { value: "payload", label: "发给模型" },
          ]} />
        </div>
        <div className="av-scroll">
          {tab === "statements" ? <Statements j={j} onFocus={setFocus} /> : null}
          {tab === "candidates" ? <Candidates j={j} onFocus={setFocus} /> : null}
          {tab === "payload" ? <Payload j={j} /> : null}
        </div>
      </section>
    </>
  );
}

const METHOD: Record<string, string> = { sql: "SQL", python_ast: "Python 内嵌 SQL", shell: "Shell 内嵌 SQL", unrecognized: "未识别片段" };

function Statements({ j, onFocus }: { j: ImportJob; onFocus: (id: string) => void }) {
  const placeholders = [...new Set((j.statements ?? []).filter((s) => s.status === "needs_params").flatMap((s) => s.placeholders))];
  const [params, setParams] = useState<Record<string, string>>({});
  const [message, setMessage] = useMessage();
  const save = useAction(() => unwrap(api.PUT("/api/imports/{job_id}/params", { params: { path: { job_id: j.id } }, body: { params } })), ["import", "imports", "graph"]);
  return (
    <>
      {placeholders.length ? (
        <div className="av-stack" style={{ borderBottom: "1px solid var(--line-subtle)" }}>
          <span className="av-section-label">动态 SQL 需要参数 <Hint text="不猜测变量取值；补齐后重新解析这些语句" /></span>
          {placeholders.map((p) => (
            <label key={p} className="av-field"><span className="av-label av-mono">{p}</span>
              <input className="av-input av-mono" value={params[p] ?? ""} placeholder="例如 2026-09-15" onChange={(e) => setParams({ ...params, [p]: e.target.value })} /></label>
          ))}
          <Banner message={message} />
          <div className="av-actions" style={{ justifyContent: "flex-end" }}>
            <button type="button" className="av-btn av-btn--primary" disabled={placeholders.some((p) => !params[p])}
              onClick={() => save.mutate(undefined, { onSuccess: () => setMessage({ ok: true, text: "已补参数并重新解析" }), onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>重新解析</button>
          </div>
        </div>
      ) : null}
      <div className="av-list">
        {(j.statements ?? []).map((s) => (
          <button key={s.id} type="button" className="av-row" onClick={() => onFocus(s.id)}
            style={{ "--row-cols": "56px minmax(0, 1fr) auto", width: "100%", border: "none", borderBottom: "1px solid var(--line-subtle)", background: "transparent", textAlign: "left", cursor: "pointer" } as React.CSSProperties}>
            <span className="av-mono av-caption">{s.lines[0]}–{s.lines[1]}</span>
            <span className="av-row__main">
              <span className="av-row__title">{s.statement_type ?? METHOD[s.method]}{s.target ? <span className="av-mono"> → {shortName(s.target)}</span> : null}</span>
              <span className="av-row__meta" style={{ whiteSpace: "normal" }}>{METHOD[s.method]}{s.reason ? ` · ${s.reason}` : ""}</span>
            </span>
            <Status value={s.status} />
          </button>
        ))}
      </div>
    </>
  );
}

function Candidates({ j, onFocus }: { j: ImportJob; onFocus: (id: string) => void }) {
  const hasModel = (j.candidates ?? []).some((c) => c.origin === "model");
  const [filter, setFilter] = useState<"model" | "parsed">(hasModel ? "model" : "parsed");
  const [editing, setEditing] = useState<string | null>(null);
  const [edit, setEdit] = useState({ relation: "", target_column: "", reason: "" });
  const [reason, setReason] = useState("");
  const [message, setMessage] = useMessage();
  const decide = useAction((v: { id: string; decision: "accept" | "reject" | "modify"; reason: string; relation?: string; target_column?: string }) =>
    unwrap(api.POST("/api/candidates/{candidate_id}/decision", { params: { path: { candidate_id: v.id } },
      body: { decision: v.decision, reason: v.reason, relation: v.relation || null, target_column: v.target_column || null } })), ["import", "imports", "graph"]);
  const list = (j.candidates ?? []).filter((c) => (filter === "model" ? c.origin === "model" : c.origin !== "model"));
  const done = (text: string) => ({ onSuccess: () => { setMessage({ ok: true, text }); setEditing(null); }, onError: (e: unknown) => setMessage({ ok: false, text: errorText(e) }) });
  return (
    <>
      <div className="av-stack" style={{ paddingBottom: 8 }}>
        <Seg value={filter} onChange={setFilter} label="来源" options={[
          { value: "model", label: <>模型候选 <Count n={(j.candidates ?? []).filter((c) => c.origin === "model").length} /></> },
          { value: "parsed", label: <>解析 <Count n={(j.candidates ?? []).filter((c) => c.origin !== "model").length} /></> },
        ]} />
        {filter === "model" ? <label className="av-field"><span className="av-label">处理理由（接受 / 驳回时必填）</span>
          <input className="av-input" value={reason} onChange={(e) => setReason(e.target.value)} /></label> : null}
        <Banner message={message} />
      </div>
      {list.map((c) => (
        <div key={c.id}>
          <div className="av-cand" style={{ gridTemplateColumns: "minmax(0, 1fr) 64px 72px auto" }} onMouseEnter={() => c.statement_id && onFocus(c.statement_id)}>
            <span className="av-cand__edge">
              <span>{shortName(c.source_ref)}{c.source_column ? `.${c.source_column}` : ""} → {shortName(c.target_ref)}{c.target_column ? `.${c.target_column}` : ""}</span>
              <span>{String((c.evidence as { rationale?: string }).rationale ?? c.expression ?? "")}{(c.evidence as { lines?: number[] }).lines ? ` · 第 ${(c.evidence as { lines: number[] }).lines.join("–")} 行` : ""}</span>
            </span>
            <span className="av-tag">{RELATION_LABEL[c.relation] ?? c.relation}</span>
            {c.confidence != null ? <span className="av-conf"><i><b style={{ width: `${Math.round(c.confidence * 100)}%` }} /></i>{c.confidence.toFixed(2)}</span> : <Status value={c.status} />}
            <span className="av-actions" style={{ justifyContent: "flex-end" }}>
              {c.status === "pending" ? <>
                <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={!reason} onClick={() => decide.mutate({ id: c.id, decision: "reject", reason }, done("已驳回"))}>驳回</button>
                <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={() => { setEditing(c.id); setEdit({ relation: c.relation, target_column: c.target_column ?? "", reason: "" }); }}>修改</button>
                <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={!reason} onClick={() => decide.mutate({ id: c.id, decision: "accept", reason }, done("已接受，写入血缘"))}>接受</button>
              </> : c.status === "written" ? (
                <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={() => { setEditing(c.id); setEdit({ relation: c.relation, target_column: c.target_column ?? "", reason: "" }); }}>修改</button>
              ) : <Status value={c.status} />}
            </span>
          </div>
          {editing === c.id ? (
            <div className="av-stack" style={{ background: "var(--bg-raised)", borderBottom: "1px solid var(--line-subtle)" }}>
              <div className="av-formrow">
                <label className="av-field"><span className="av-label">关系</span>
                  <Select value={edit.relation} onChange={(v) => setEdit({ ...edit, relation: v })}>
                    {Object.entries(RELATION_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</Select></label>
                <label className="av-field"><span className="av-label">目标列</span><input className="av-input av-mono" value={edit.target_column} onChange={(e) => setEdit({ ...edit, target_column: e.target.value })} /></label>
              </div>
              <label className="av-field"><span className="av-label">理由</span><input className="av-input" value={edit.reason} onChange={(e) => setEdit({ ...edit, reason: e.target.value })} /></label>
              <div className="av-actions" style={{ justifyContent: "flex-end" }}>
                <button type="button" className="av-btn av-btn--secondary" onClick={() => setEditing(null)}>取消</button>
                <button type="button" className="av-btn av-btn--primary" disabled={!edit.reason}
                  onClick={() => decide.mutate({ id: c.id, decision: "modify", reason: edit.reason, relation: edit.relation, target_column: edit.target_column }, done("已修改并写入"))}>保存修改</button>
              </div>
            </div>
          ) : null}
        </div>
      ))}
      {!list.length ? <Empty text={filter === "model" ? "没有模型候选" : "没有解析出的边"} /> : null}
    </>
  );
}

function Payload({ j }: { j: ImportJob }) {
  if (!j.model_payload?.length) return <Empty text="没有需要模型推断的片段" />;
  return (
    <div className="av-stack">
      {j.model_payload.map((p, i) => (
        <section key={i} className="av-panel">
          <div className="av-panel__hd"><h2 className="av-panel__title">第 {(p.lines as number[]).join("–")} 行</h2>
            <span className={`av-status ${String(p.sent_to).startsWith("未发送") ? "" : "av-status--warn"}`}>{String(p.sent_to)}</span></div>
          <div className="av-panel__bd" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <span className="av-caption">已脱敏 {String(p.redactions)} 处{p.note ? ` · ${String(p.note)}` : ""}{p.error ? ` · ${String(p.error)}` : ""}</span>
            <pre className="av-code">{String(p.text)}</pre>
          </div>
        </section>
      ))}
    </div>
  );
}
