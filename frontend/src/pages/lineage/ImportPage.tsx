// 批量导入血缘：后台解析，解析边直接写入，中间表折叠，模型候选经人工确认后进入血缘（ADR 0016）。
import { useQueryClient } from "@tanstack/react-query";
import { useDeferredValue, useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { ApiError, api, token, unwrap } from "../../api/client";
import {
  errorText, useAction, useDatasources, useImport, useImportCandidates, useImportContent, useImportPayload, useImports,
  useImportStatements, useSqlFunctions, useTimeZone, type ImportBatch, type ImportCandidate, type ImportStatement,
} from "../../api/hooks";
import {
  Banner, Count, Empty, Hint, Icon, PaneHeader, QueryState, RELATION_LABEL, Seg, Select, Status, Switch, Tabs, highlight,
  shortName, useMessage,
} from "../../components/ui";
import { formatShortTime, formatTime } from "../../format";
import { t } from "../../i18n";

type WorkspaceTab = "source" | "result";
type Focus = { fileId: string; lines: number[] } | null;
const MB = 1024 * 1024;
const MAX_FILE_BYTES = 20 * MB;
const MAX_BATCH_BYTES = 100 * MB;
const FILE_PATTERN = /\.(sql|py|sh|bash|zip)$/i;
const CODE_PATTERN = /\.(py|sh|bash)$/i;
const busy = (status?: string) => status === "queued" || status === "running";
type Batch = ImportBatch & { files: NonNullable<ImportBatch["files"]> };

export function ImportPage() {
  const { jobId } = useParams();
  const imports = useImports();
  const tz = useTimeZone();
  const [historyOpen, setHistoryOpen] = useState(false);
  return (
    <div className="av-fill av-import-page">
      <div className="av-pagehd">
        <div className="av-pagehd__title"><h1>{t("导入血缘")}</h1><Hint text={t("按语法树解析；中间表自动折叠；模型候选需人工确认")} /></div>
        <div className="av-actions av-import-actions">
          <button type="button" className="av-btn av-btn--secondary av-import-history-toggle" aria-expanded={historyOpen}
            aria-controls="import-history" onClick={() => setHistoryOpen(!historyOpen)}>{t("导入记录")}</button>
          <Link className="av-btn av-btn--secondary" to="/lineage"><Icon name="lineage" />{t("血缘")}</Link>
          <Link className="av-btn av-btn--primary" to="/lineage/import"><Icon name="plus" />{t("新导入")}</Link>
        </div>
      </div>
      <div className="av-workspace av-import-workspace" data-history-open={historyOpen}
        onKeyDown={(e) => { if (e.key === "Escape") setHistoryOpen(false); }}>
        <section id="import-history" className="av-pane av-pane--rail av-import-history" aria-label={t("导入记录")}>
          <PaneHeader title={t("导入记录")} count={imports.isSuccess ? imports.data.length : undefined}>
            <button type="button" className="av-btn av-btn--ghost av-btn--sm av-import-history-toggle" onClick={() => setHistoryOpen(false)}>{t("收起")}</button>
          </PaneHeader>
          <div className="av-scroll"><QueryState query={imports} isEmpty={(data) => !data.length} emptyText={t("还没有导入记录")} compact>
            {(data) => data.map((b) => <Link key={b.id} to={`/lineage/import/${b.id}`} onClick={() => setHistoryOpen(false)}
              aria-current={b.id === jobId ? "page" : undefined} className={`av-row av-import-history__row${b.id === jobId ? " is-active" : ""}`}>
              <span className="av-row__main"><span className="av-row__title av-mono">{b.filename ?? "—"}</span>
                <span className="av-row__meta">{formatShortTime(b.created_at, tz)}
                  {b.file_count > 1 ? ` · ${t("{{n}} 个文件", { n: b.file_count })}` : ""}{b.statement_count ? ` · ${t("{{n}} 条", { n: b.statement_count })}` : ""}</span></span>
              <Status value={b.status} />
            </Link>)}
          </QueryState></div>
        </section>
        {jobId ? <BatchView key={jobId} batchId={jobId} /> : <NewImport />}
      </div>
    </div>
  );
}

function WorkspaceTabs({ value, onChange, isNew = false }: { value: WorkspaceTab; onChange: (value: WorkspaceTab) => void; isNew?: boolean }) {
  return <div className="av-import-mobile-tabs"><Tabs value={value} onChange={onChange}
    options={[{ value: "source", label: t("原文") }, { value: "result", label: isNew ? t("解析设置") : t("识别结果") }]} /></div>;
}

const sizeText = (bytes: number) => bytes >= MB ? `${(bytes / MB).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;

function fileProblem(files: File[]): string {
  const bad = files.find((f) => !FILE_PATTERN.test(f.name));
  if (bad) return t("不支持 {{name}}：请选择 .sql、.py、.sh、.bash 或 .zip", { name: bad.name });
  const big = files.find((f) => f.size > (/\.zip$/i.test(f.name) ? MAX_BATCH_BYTES : MAX_FILE_BYTES));
  if (big) return t("{{name}} 超过大小上限（单文件 20 MB，zip 100 MB）", { name: big.name });
  if (files.some((f) => !f.size)) return t("不能上传空文件");
  if (files.reduce((n, f) => n + f.size, 0) > MAX_BATCH_BYTES) return t("合计超过 100 MB");
  return "";
}

function NewImport() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const sources = useDatasources();
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>("source");
  const [mode, setMode] = useState<"paste" | "file">("paste");
  const [content, setContent] = useState("");
  const [filename, setFilename] = useState(t("粘贴的 SQL.sql"));
  const [datasource, setDatasource] = useState("");
  const [defaultDb, setDefaultDb] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [assistFailed, setAssistFailed] = useState(false);
  const [assistCode, setAssistCode] = useState(false);
  const [functions, setFunctions] = useState("");
  const [busyState, setBusy] = useState(false);
  const [message, setMessage] = useMessage();
  const fileInput = useRef<HTMLInputElement>(null);
  const submitting = useRef(false);
  const availableSources = (sources.data ?? []).filter((d) => d.engine.capabilities.includes("view_lineage"));
  const ds = availableSources.some((d) => d.id === datasource) ? datasource : availableSources[0]?.id || "";
  const saved = useSqlFunctions(ds || undefined);
  const fileError = fileProblem(files);
  const ready = mode === "paste" ? !!content.trim() && !!filename.trim() : !!files.length && !fileError;
  const codeChars = mode === "paste" ? (CODE_PATTERN.test(filename) ? content.length : 0)
    : files.filter((f) => CODE_PATTERN.test(f.name)).reduce((n, f) => n + f.size, 0);
  const hasZip = mode === "file" && files.some((f) => /\.zip$/i.test(f.name));

  function addFiles(list: FileList | null) {
    if (!list?.length) return;
    const added = Array.from(list);
    setFiles((current) => [...current, ...added.filter((f) => !current.some((x) => x.name === f.name && x.size === f.size))]);
    if (fileInput.current) fileInput.current.value = "";
  }
  function drop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    addFiles(e.dataTransfer.files);
  }

  async function submit() {
    if (submitting.current || !sources.isSuccess || !ds || !ready) return;
    submitting.current = true;
    setBusy(true);
    setMessage(null);
    const modelAssist = { failed_sql: assistFailed, code: assistCode };
    const sqlFunctions = functions.split(/[\s,，]+/).map((s) => s.trim()).filter(Boolean);
    try {
      let batch: ImportBatch;
      if (mode === "file") {
        const form = new FormData();
        form.append("datasource_id", ds);
        for (const f of files) form.append("files", f);
        if (defaultDb.trim()) form.append("default_db", defaultDb.trim());
        form.append("model_assist", JSON.stringify(modelAssist));
        form.append("sql_functions", JSON.stringify(sqlFunctions));
        const resp = await fetch("/api/imports/upload", { method: "POST", body: form, headers: { Authorization: `Bearer ${token.get()}` } });
        const json = await resp.json();
        if (!resp.ok) throw new Error(typeof json.detail === "string" ? json.detail : json.detail?.message ?? t("上传失败"));
        batch = json;
      } else {
        batch = await unwrap(api.POST("/api/imports", { body: {
          datasource_id: ds, filename: filename.trim(), content, source_type: null, default_db: defaultDb.trim() || null, params: {},
          model_assist: modelAssist, sql_functions: sqlFunctions,
        } }));
      }
      qc.invalidateQueries({ queryKey: ["imports"] });
      navigate(`/lineage/import/${batch.id}`);
    } catch (e) {
      setMessage({ ok: false, text: errorText(e) });
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }

  return <>
    <WorkspaceTabs value={workspaceTab} onChange={setWorkspaceTab} isNew />
    <section className="av-pane av-import-source" aria-label={t("原文")} data-active={workspaceTab === "source"}>
      <PaneHeader title={t("原文")}><Seg value={mode} onChange={setMode} label={t("来源")}
        options={[{ value: "paste", label: t("粘贴 SQL") }, { value: "file", label: t("上传文件") }]} /></PaneHeader>
      {mode === "paste" ? <textarea className="av-editor av-editor--input" aria-label={t("SQL 原文")}
        placeholder={"INSERT INTO target_db.daily_summary\nSELECT event_date, category, SUM(amount) AS total\nFROM source_db.events GROUP BY event_date, category;"}
        value={content} onChange={(e) => setContent(e.target.value)} /> : <div className="av-import-drop" data-dragging={dragging}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={drop}>
        <input ref={fileInput} type="file" multiple className="av-import-file-input" accept=".sql,.py,.sh,.bash,.zip"
          aria-label={t("上传原文文件")} aria-describedby="import-file-hint" onChange={(e) => addFiles(e.target.files)} />
        {files.length ? <ul className="av-import-filelist" aria-label={t("待导入文件")}>{files.map((f) => <li key={`${f.name}:${f.size}`}>
          <Icon name={/\.zip$/i.test(f.name) ? "view" : "audit"} small /><span className="av-mono av-import-filename">{f.name}</span>
          <span className="av-caption">{sizeText(f.size)}</span>
          <button type="button" className="av-btn av-btn--ghost av-btn--sm" aria-label={t("移除 {{name}}", { name: f.name })}
            onClick={() => setFiles((current) => current.filter((x) => x !== f))}>{t("移除")}</button>
        </li>)}</ul> : <div className="av-empty av-import-upload"><Icon name="upload" /><span>{t("拖入文件或 zip")}</span></div>}
        <div className="av-import-drop__foot">
          <span id="import-file-hint" className="av-caption">{t("SQL / Python / Shell / zip · UTF-8 · 单文件 20 MB · 合计 100 MB")}</span>
          {fileError ? <span className="av-import-field-error" role="alert">{fileError}</span> : null}
          <button type="button" className="av-btn av-btn--secondary" onClick={() => fileInput.current?.click()}><Icon name="upload" />{t("选择文件")}</button>
        </div>
      </div>}
      <div className="av-import-mobile-next av-actions"><button type="button" className="av-btn av-btn--primary"
        disabled={!ready} onClick={() => setWorkspaceTab("result")}>{t("解析设置")}</button></div>
    </section>
    <section className="av-pane av-import-results" aria-label={t("解析设置")} data-active={workspaceTab === "result"}>
      <PaneHeader title={t("解析设置")} />
      <div className="av-scroll"><div className="av-stack">
        <QueryState query={sources} isEmpty={() => !availableSources.length} emptyText={t("没有可导入血缘的数据源")} compact
          emptyAction={<Link className="av-btn av-btn--secondary" to="/settings/datasources">{t("查看数据源")}</Link>}>
          {() => <label className="av-field"><span className="av-label">{t("数据源")} <Hint text={t("按它的方言切分与解析，用已采集的表结构对列")} /></span>
            <Select label={t("数据源")} value={ds} onChange={setDatasource}>{availableSources.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}</Select></label>}
        </QueryState>
        {mode === "paste" ? <label className="av-field"><span className="av-label">{t("文件名")} <Hint text={t("同名再次导入会替换上一版")} /></span>
          <input className="av-input av-mono" value={filename} maxLength={255} onChange={(e) => setFilename(e.target.value)} /></label> : null}
        <label className="av-field"><span className="av-label">{t("默认库")} <Hint text={t("语句里没写库名时使用；脚本里的 USE 会切换")} /></span>
          <input className="av-input av-mono" value={defaultDb} maxLength={128} placeholder={t("可不填")} onChange={(e) => setDefaultDb(e.target.value)} /></label>
        <div className="av-field"><span className="av-label">{t("模型辅助")} <Hint text={t("只生成待确认的候选；发送前脱敏，受模型配置的发送策略约束")} /></span>
          <div className="av-import-switches">
            <label className="av-import-switch"><span>{t("解析失败的 SQL")}</span><Switch checked={assistFailed} onChange={setAssistFailed} label={t("解析失败的 SQL 交给模型")} /></label>
            <label className="av-import-switch"><span>{t("不规范的代码")}</span><Switch checked={assistCode} onChange={setAssistCode} label={t("不规范的 Python / Shell 交给模型识别")} /></label>
          </div>
          {assistCode && (codeChars || hasZip) ? <span className="av-caption">
            {hasZip ? t("将发送约 {{n}} 字符（zip 内的代码文件另计），按函数分块，会产生模型费用", { n: codeChars.toLocaleString() })
              : t("将发送约 {{n}} 字符，按函数分块，会产生模型费用", { n: codeChars.toLocaleString() })}</span> : null}
          {assistFailed ? <span className="av-caption">{t("只发送解析失败的语句")}</span> : null}
        </div>
        <label className="av-field"><span className="av-label">{t("执行 SQL 的函数")} <Hint text={t("代码里自己封装的执行函数，如 run_sql；按规则抽取其中的 SQL")} /></span>
          <input className="av-input av-mono" value={functions} placeholder={saved.data?.sql_functions.length ? t("已保存：{{names}}", { names: saved.data.sql_functions.join(", ") }) : "run_sql, db.submit"}
            onChange={(e) => setFunctions(e.target.value)} /></label>
        <Banner message={message} />
        <div className="av-actions av-import-actions" aria-busy={busyState}>
          <button type="button" className="av-btn av-btn--primary av-btn--lg" disabled={busyState || !sources.isSuccess || !ds || !ready}
            onClick={submit}>{busyState ? t("提交中…") : t("解析")}</button>
        </div>
      </div></div>
    </section>
  </>;
}

function BatchView({ batchId }: { batchId: string }) {
  const batch = useImport(batchId);
  if (batch.isError && batch.error instanceof ApiError && batch.error.status === 404) return <section className="av-pane av-import-query-state">
    <Empty text={t("导入批次不存在")} action={<Link className="av-btn av-btn--primary" to="/lineage/import">{t("新导入")}</Link>} /></section>;
  if (!batch.isSuccess) return <section className="av-pane av-import-query-state" aria-label={t("导入批次")}><QueryState query={batch}>{() => null}</QueryState></section>;
  return <BatchContent b={{ ...batch.data, files: batch.data.files ?? [] }} />;
}

type Stats = {
  statements?: Record<string, number>; statement_count?: number; active_edges?: number; folded_edges?: number; intermediate_edges?: number;
  pending_candidates?: number; replaced_edges?: number; invalidated_decisions?: number; inherited_decisions?: number; suggested_sql_functions?: string[];
};
type Progress = { phase?: string; files_total?: number; files_done?: number; statements_done?: number; current_file?: string | null };
type Unmatched = { ref: string; role: string; count: number; statement_ids: string[] };
type ResultTab = "statements" | "candidates" | "tables" | "files" | "payload";

function BatchContent({ b }: { b: Batch }) {
  const stats = (b.stats ?? {}) as Stats;
  const progress = (b.progress ?? {}) as Progress;
  const running = busy(b.status);
  const unmatched = (b.unmatched_tables ?? []) as Unmatched[];
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>("result");
  const [tab, setTab] = useState<ResultTab>((stats.pending_candidates ?? 0) > 0 ? "candidates" : "statements");
  const [fileId, setFileId] = useState(b.files[0]?.id ?? "");
  const [focus, setFocus] = useState<Focus>(null);
  const [query, setQuery] = useState("");
  const version = `${b.status}:${b.updated_at ?? ""}`;
  function locate(target: { file_id?: string | null; lines?: number[] | null }) {
    if (!target.file_id || !target.lines?.length) return;
    setFileId(target.file_id);
    setFocus({ fileId: target.file_id, lines: target.lines });
    setWorkspaceTab("source");
  }
  const replaced = stats.replaced_edges ?? 0;
  const summary = [
    { label: t("文件"), n: b.files.length },
    { label: t("语句"), n: stats.statement_count },
    { label: t("有效边"), n: stats.active_edges },
    { label: t("折叠"), n: stats.folded_edges },
    { label: t("待确认"), n: stats.pending_candidates },
    { label: t("未对上"), n: unmatched.length },
    ...(replaced ? [{ label: t("替换旧边"), n: replaced }] : []),
  ];
  const done = progress.statements_done ?? 0;
  return <>
    <div className="av-import-summary" aria-label={t("导入结果摘要")}>
      {running ? <div className="av-import-progress" role="status" aria-live="polite">
        <span className="av-import-progress__bar"><i style={{ width: `${progressPercent(progress)}%` }} /></span>
        <span className="av-caption">{PHASE[progress.phase ?? "queued"] ?? t("处理中")}{done ? ` · ${t("{{n}} 条", { n: done.toLocaleString() })}` : ""}
          {progress.files_total ? ` · ${t("文件 {{done}}/{{total}}", { done: progress.files_done ?? 0, total: progress.files_total })}` : ""}
          {progress.current_file ? ` · ${progress.current_file}` : ""}</span>
      </div> : <dl>{summary.map(({ label, n }) => <div key={label}><dt>{label}</dt><dd>{n ?? "—"}</dd></div>)}</dl>}
      <Status value={b.status} label={running ? t("解析中") : undefined} />
    </div>
    <WorkspaceTabs value={workspaceTab} onChange={setWorkspaceTab} />
    <SourcePane b={b} fileId={fileId} onFile={setFileId} focus={focus} active={workspaceTab === "source"} />
    <section className="av-pane av-import-results" aria-label={t("识别结果")} data-active={workspaceTab === "result"}>
      {b.error ? <div className="av-import-error"><Banner message={{ ok: false, text: b.error }} /></div> : null}
      <div className="av-import-result-tabs"><Tabs value={tab} onChange={setTab} options={[
        { value: "statements", label: <>{t("语句")} {stats.statement_count ? <Count n={stats.statement_count} /> : null}</> },
        { value: "candidates", label: <>{t("边")} {stats.pending_candidates ? <Count n={stats.pending_candidates} /> : null}</> },
        { value: "tables", label: <>{t("未对上")} {unmatched.length ? <Count n={unmatched.length} /> : null}</> },
        { value: "files", label: t("文件") }, { value: "payload", label: t("发给模型") },
      ]} /></div>
      <div className="av-scroll">
        {running && tab !== "files" ? <Empty text={t("解析完成后显示结果")} compact /> : <>
          {tab === "statements" ? <Statements b={b} version={version} query={query} onQuery={setQuery} onLocate={locate} /> : null}
          {tab === "candidates" ? <Candidates b={b} version={version} onLocate={locate} /> : null}
          {tab === "tables" ? <UnmatchedTables items={unmatched} onShow={(ref) => { setQuery(ref.split(".").pop() ?? ref); setTab("statements"); }} /> : null}
          {tab === "payload" ? <Payload b={b} version={version} onLocate={locate} /> : null}
        </>}
        {tab === "files" ? <Files b={b} stats={stats} /> : null}
      </div>
    </section>
  </>;
}

const PHASE: Record<string, string> = { queued: t("排队中"), parse: t("解析中"), write: t("写入中"), model: t("模型辅助中") };

function progressPercent(p: Progress): number {
  if (p.phase === "model") return 95;
  if (p.phase === "write") return 85;
  if (!p.files_total) return 5;
  return Math.max(5, Math.min(80, Math.round(((p.files_done ?? 0) / p.files_total) * 80)));
}

function SourcePane({ b, fileId, onFile, focus, active }: { b: Batch; fileId: string; onFile: (id: string) => void; focus: Focus; active: boolean }) {
  const content = useImportContent(b.id, fileId || undefined);
  const lines = useMemo(() => (content.data?.content ?? "").split("\n"), [content.data]);
  const lineRefs = useRef(new Map<number, HTMLSpanElement>());
  const file = b.files.find((f) => f.id === fileId);
  const range = focus && focus.fileId === fileId ? focus.lines : null;
  useEffect(() => {
    if (!range || !content.isSuccess) return;
    const line = lineRefs.current.get(range[0]);
    line?.scrollIntoView({ block: "center", inline: "nearest" });
    line?.focus({ preventScroll: true });
  }, [range, content.isSuccess, active]);
  return <section className="av-pane av-import-source" aria-label={t("原文")} data-active={active}>
    <PaneHeader title={b.files.length > 1 ? <Select label={t("文件")} value={fileId} onChange={onFile} style={{ width: "auto", maxWidth: "100%" }}>
      {b.files.map((f) => <option key={f.id} value={f.id}>{f.source_key}</option>)}</Select>
      : <span className="av-mono av-import-filename">{file?.source_key ?? "—"}</span>}>
      {file ? <span className="av-tag">{file.language}</span> : null}
      {file && file.revision > 1 ? <span className="av-tag" title={t("同一来源再次导入，已替换上一版写入的边")}>{t("第 {{n}} 版", { n: file.revision })}</span> : null}
    </PaneHeader>
    {!file ? <Empty text={t("没有文件")} compact /> : <QueryState query={content}>{() =>
      <div className="av-editor av-import-editor" tabIndex={0} aria-label={t("原文与行号")}>
        <div className="av-editor__gutter">{lines.map((_, i) => `${i + 1}\n`).join("")}</div>
        <div className="av-editor__code">{lines.map((line, i) => {
          const n = i + 1;
          const inFocus = !!range && n >= range[0] && n <= range[range.length - 1];
          return <span key={i} tabIndex={-1} className={inFocus ? "is-hl is-focused" : undefined}
            ref={(element) => { if (element) lineRefs.current.set(n, element); else lineRefs.current.delete(n); }}>{highlight(line)}{" "}</span>;
        })}</div>
      </div>}</QueryState>}
  </section>;
}

const METHOD: Record<string, string> = {
  sql: "SQL", python_ast: t("Python 内嵌 SQL"), shell: t("Shell 内嵌 SQL"), unrecognized: t("未识别片段"), model_sql: t("模型还原的 SQL"),
};
const GROUPS: { value: string; label: string }[] = [
  { value: "", label: t("全部") }, { value: "parsed", label: t("已解析") }, { value: "partial", label: t("部分解析") },
  { value: "pending_review", label: t("待确认") }, { value: "needs_params", label: t("需要参数") }, { value: "failed", label: t("失败") },
  { value: "unsupported", label: t("不支持") }, { value: "skipped", label: t("跳过") },
];
function Statements({ b, version, query, onQuery, onLocate }: {
  b: Batch; version: string; query: string; onQuery: (q: string) => void; onLocate: (s: ImportStatement) => void;
}) {
  const [group, setGroup] = useState("");
  const q = useDeferredValue(query.trim());
  const page = useImportStatements(b.id, { status: group || null, q: q || null }, version);
  const counts = page.data?.pages[0]?.counts ?? {};
  const total = Object.entries(counts).filter(([k]) => k !== "pending_review").reduce((n, [, v]) => n + v, 0);
  const names = useMemo(() => new Map(b.files.map((f) => [f.id, f.source_key])), [b.files]);
  return <>
    {counts.needs_params ? <ParamsForm b={b} version={version} /> : null}
    <div className="av-import-filters">
      <div className="av-import-chips" role="group" aria-label={t("按状态")}>
        {GROUPS.filter((g) => !g.value || counts[g.value]).map((g) => <button key={g.value} type="button" className="av-import-chip"
          aria-pressed={group === g.value} onClick={() => setGroup(g.value)}>
          {g.label}<span className="av-import-chip__n">{g.value ? counts[g.value] : total}</span></button>)}
      </div>
      <input className="av-input av-input--sm" type="search" aria-label={t("搜索语句")} placeholder={t("搜索表名或 SQL")} value={query}
        onChange={(e) => onQuery(e.target.value)} />
    </div>
    <QueryState query={page} isEmpty={(d) => !d.pages[0]?.items.length} emptyText={q ? t("没有匹配的语句") : t("没有语句")} compact>
      {(d) => <><div className="av-list">{d.pages.flatMap((p) => p.items).map((s) => <button type="button" key={s.id} className="av-row av-import-statement" onClick={() => onLocate(s)}>
        <span className="av-mono av-caption">{s.lines[0]}–{s.lines[1]}</span>
        <span className="av-row__main">
          <span className="av-row__title">{s.statement_type ?? METHOD[s.method] ?? s.method}{s.target ? <span className="av-mono"> → {shortName(s.target)}</span> : null}</span>
          <span className="av-row__meta">{[b.files.length > 1 ? names.get(s.file_id ?? "") : null, s.method !== "sql" ? METHOD[s.method] ?? s.method : null, s.reason]
            .filter(Boolean).join(" · ")}</span>
        </span>
        <Status value={s.status} />
      </button>)}</div>
        <More pages={d.pages} onMore={() => page.fetchNextPage()} loading={page.isFetchingNextPage} /></>}
    </QueryState>
  </>;
}

function More({ pages, onMore, loading }: { pages: { items: unknown[]; total: number }[]; onMore: () => void; loading: boolean }) {
  const shown = pages.reduce((n, p) => n + p.items.length, 0);
  const total = pages[0]?.total ?? 0;
  if (shown >= total) return null;
  return <div className="av-import-more"><span className="av-caption">{shown} / {total}</span>
    <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={loading} onClick={onMore}>{loading ? t("加载中…") : t("加载更多")}</button></div>;
}

function ParamsForm({ b, version }: { b: Batch; version: string }) {
  const navigate = useNavigate();
  const waiting = useImportStatements(b.id, { status: "needs_params" }, version, 500);
  const placeholders = [...new Set((waiting.data?.pages ?? []).flatMap((p) => p.items).flatMap((s) => s.placeholders))];
  const [params, setParams] = useState<Record<string, string>>({ ...(b.params as Record<string, string>) });
  const [message, setMessage] = useMessage();
  const save = useAction(() => unwrap(api.PUT("/api/imports/{batch_id}/params", { params: { path: { batch_id: b.id } }, body: { params } })), ["imports"]);
  if (!placeholders.length) return null;
  return <div className="av-stack av-import-params">
    <span className="av-section-label">{t("需要参数")} <Hint text={t("补齐后整批重新解析，生成新的导入记录")} /></span>
    <div className="av-import-param-grid">{placeholders.map((p) => <label key={p} className="av-field"><span className="av-label av-mono">{p}</span>
      <input className="av-input av-mono" value={params[p] ?? ""} placeholder={t("参数值")} onChange={(e) => setParams({ ...params, [p]: e.target.value })} /></label>)}</div>
    <Banner message={message} />
    <div className="av-actions av-import-actions" aria-busy={save.isPending}>
      <button type="button" className="av-btn av-btn--primary" disabled={save.isPending || placeholders.some((p) => !params[p]?.trim())}
        onClick={() => save.mutate(undefined, { onSuccess: (next) => navigate(`/lineage/import/${next.id}`), onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>
        {save.isPending ? t("提交中…") : t("重新解析")}</button>
    </div>
  </div>;
}

type EdgeView = "pending" | "folded" | "parsed" | "model" | "model_sql" | "intermediate";
// 模型候选分两种来源：模型直接给出的边（model），和模型还原 SQL 后由语法树算出的边（model_sql）
const EDGE_VIEWS: Record<EdgeView, { status?: string; origin?: string; label: string }> = {
  pending: { status: "pending", label: t("待确认") }, folded: { origin: "folded", label: t("折叠") },
  parsed: { origin: "parsed", label: t("解析") }, model: { origin: "model", label: t("模型") },
  model_sql: { origin: "model_sql", label: t("模型还原") }, intermediate: { status: "intermediate", label: t("中间段") },
};
const ORIGIN_LABEL: Record<string, string> = { model: t("模型"), model_sql: t("模型还原"), folded: t("折叠"), parsed: t("解析"), manual: t("人工") };
type Hop = { source_ref: string; source_column?: string | null; target_ref: string; target_column?: string | null; relation: string };
type Evidence = { rationale?: string; lines?: number[]; restored_sql?: string; original_code?: string; comparison?: string; note?: string };

const edgeText = (ref: string, column?: string | null) => `${shortName(ref)}${column ? `.${column}` : ""}`;

function Candidates({ b, version, onLocate }: { b: Batch; version: string; onLocate: (c: { file_id?: string | null; lines?: number[] | null }) => void }) {
  const pendingTotal = ((b.stats ?? {}) as Stats).pending_candidates ?? 0;
  const [view, setView] = useState<EdgeView>(pendingTotal ? "pending" : "folded");
  const filter = EDGE_VIEWS[view];
  const page = useImportCandidates(b.id, { status: filter.status ?? null, origin: filter.origin ?? null }, version);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [reason, setReason] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [edit, setEdit] = useState({ relation: "", target_column: "", reason: "" });
  const [message, setMessage] = useMessage();
  const qc = useQueryClient();
  const refresh = () => { for (const key of ["import", "imports", "graph"]) qc.invalidateQueries({ queryKey: [key] }); };
  const bulk = useAction((v: { ids: string[]; decision: "accept" | "reject" }) =>
    unwrap(api.POST("/api/candidates/decisions", { body: { ids: v.ids, decision: v.decision, reason: reason.trim() } })), ["import", "imports", "graph"]);
  const single = useAction((v: { id: string; decision: "accept" | "reject" | "modify"; reason: string; relation?: string; target_column?: string }) =>
    unwrap(api.POST("/api/candidates/{candidate_id}/decision", { params: { path: { candidate_id: v.id } },
      body: { decision: v.decision, reason: v.reason.trim(), relation: v.relation || null, target_column: v.target_column || null } })), ["import", "imports", "graph"]);
  const items = useMemo(() => (page.data?.pages ?? []).flatMap((p) => p.items), [page.data]);
  const selectable = items.filter((c) => c.status === "pending");
  const allSelected = selectable.length > 0 && selectable.every((c) => selected.has(c.id));
  useEffect(() => { setSelected(new Set()); }, [view]);

  function toggle(id: string) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id); else next.add(id);
    setSelected(next);
  }
  async function decideMany(decision: "accept" | "reject") {
    setMessage(null);
    try {
      const out = await bulk.mutateAsync({ ids: [...selected], decision });
      setMessage({ ok: true, text: decision === "accept" ? t("已接受 {{n}} 条", { n: out.processed }) : t("已驳回 {{n}} 条", { n: out.processed }) });
      setSelected(new Set());
    } catch (e) {
      const detail = (e instanceof ApiError ? (e.body as { detail?: { message?: string; candidates?: { reason: string }[] } })?.detail : undefined);
      setMessage({ ok: false, text: detail?.message ? `${detail.message}：${detail.candidates?.map((c) => c.reason).slice(0, 3).join("；")}` : errorText(e) });
      refresh();
    }
  }
  async function decideOne(v: Parameters<typeof single.mutateAsync>[0]) {
    setMessage(null);
    try {
      await single.mutateAsync(v);
      setMessage({ ok: true, text: v.decision === "reject" ? t("已驳回") : v.decision === "modify" ? t("已修改并写入") : t("已接受并写入") });
      setEditing(null);
    } catch (e) { setMessage({ ok: false, text: errorText(e) }); }
  }

  return <>
    <div className="av-stack av-import-candidate-tools">
      <Seg value={view} onChange={setView} label={t("边")} options={(Object.keys(EDGE_VIEWS) as EdgeView[]).map((k) => ({
        value: k, label: k === "pending" && pendingTotal ? <>{EDGE_VIEWS[k].label} <Count n={pendingTotal} /></> : EDGE_VIEWS[k].label }))} />
      {view === "pending" && selectable.length ? <div className="av-import-bulk">
        <label className="av-import-check"><input type="checkbox" checked={allSelected} aria-label={t("全选本页")}
          onChange={() => setSelected(allSelected ? new Set() : new Set(selectable.map((c) => c.id)))} />{selected.size ? t("已选 {{n}}", { n: selected.size }) : t("全选")}</label>
        <input className="av-input av-input--sm" value={reason} placeholder={t("处理理由（必填）")} aria-label={t("处理理由")} onChange={(e) => setReason(e.target.value)} />
        <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={!selected.size || !reason.trim() || bulk.isPending} onClick={() => decideMany("reject")}>{t("驳回")}</button>
        <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={!selected.size || !reason.trim() || bulk.isPending} onClick={() => decideMany("accept")}>{t("接受")}</button>
      </div> : null}
      {view === "intermediate" ? <span className="av-caption">{t("中间表上的原始段，只用于查证，不进血缘图")}</span> : null}
      <Banner message={message} />
    </div>
    <QueryState query={page} isEmpty={(d) => !d.pages[0]?.items.length} emptyText={view === "pending" ? t("没有待确认的边") : t("没有这类边")} compact>
      {(d) => <>{items.map((c) => {
        const evidence = c.evidence as Evidence;
        const hops = (c.hops ?? []) as Hop[];
        const lines = evidence.lines ?? null;
        const hasDetail = hops.length > 0 || !!evidence.restored_sql || !!evidence.rationale;
        return <div key={c.id} className="av-import-candidate">
          <div className="av-cand av-cand--review">
            <div className="av-import-candidate-head">
              {c.status === "pending" ? <input type="checkbox" className="av-import-candidate-check" checked={selected.has(c.id)}
                aria-label={t("选择这条候选")} onChange={() => toggle(c.id)} /> : null}
              <span className="av-cand__edge">
                <span>{edgeText(c.source_ref, c.source_column)} → {edgeText(c.target_ref, c.target_column)}</span>
                <span>{c.via?.length ? t("经 {{tables}}", { tables: c.via.map(shortName).join(" → ") }) : evidence.note ?? evidence.rationale ?? c.expression ?? ""}
                  {lines?.length ? ` · ${t("第 {{lines}} 行", { lines: lines[1] && lines[1] !== lines[0] ? `${lines[0]}–${lines[1]}` : lines[0] })}` : ""}</span>
              </span>
            </div>
            <div className="av-import-candidate-meta"><span className="av-tag">{RELATION_LABEL[c.relation] ?? c.relation}</span>
              <span className="av-tag">{ORIGIN_LABEL[c.origin] ?? c.origin}</span>
              {evidence.comparison === "conflict" ? <span className="av-status av-status--warn">{t("与规则不一致")}</span> : null}
              {c.confidence != null ? <span className="av-conf" aria-label={t("置信度 {{value}}", { value: c.confidence.toFixed(2) })}><i><b style={{ width: `${Math.round(c.confidence * 100)}%` }} /></i>{c.confidence.toFixed(2)}</span> : null}
              <Status value={c.status} label={c.status === "pending" ? t("待确认") : undefined} />
            </div>
            <div className="av-actions av-import-candidate-actions">
              {hasDetail ? <button type="button" className="av-btn av-btn--ghost av-btn--sm" aria-expanded={open === c.id} onClick={() => setOpen(open === c.id ? null : c.id)}>{t("依据")}</button> : null}
              <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={!lines?.length} onClick={() => onLocate({ file_id: c.file_id, lines })}>{t("定位原文")}</button>
              {c.status === "pending" || c.status === "written" ? <button type="button" className="av-btn av-btn--ghost av-btn--sm"
                onClick={() => { setEditing(c.id); setEdit({ relation: c.relation, target_column: c.target_column ?? "", reason: "" }); }}>{t("修改")}</button> : null}
            </div>
          </div>
          {open === c.id ? <EdgeDetail hops={hops} evidence={evidence} /> : null}
          {editing === c.id ? <EditEdge c={c} edit={edit} onEdit={setEdit} pending={single.isPending} onCancel={() => setEditing(null)}
            onReject={() => decideOne({ id: c.id, decision: "reject", reason: edit.reason })}
            onSave={() => decideOne({ id: c.id, decision: "modify", reason: edit.reason, relation: edit.relation, target_column: edit.target_column })} /> : null}
        </div>;
      })}
      <More pages={d.pages} onMore={() => page.fetchNextPage()} loading={page.isFetchingNextPage} /></>}
    </QueryState>
  </>;
}

function EdgeDetail({ hops, evidence }: { hops: Hop[]; evidence: Evidence }) {
  return <div className="av-import-candidate-detail">
    {hops.length ? <ol className="av-import-hops" aria-label={t("组成路径")}>{hops.map((h, i) => <li key={i}>
      <span className="av-mono">{edgeText(h.source_ref, h.source_column)} → {edgeText(h.target_ref, h.target_column)}</span>
      <span className="av-tag">{RELATION_LABEL[h.relation] ?? h.relation}</span></li>)}</ol> : null}
    {evidence.original_code ? <><span className="av-section-label">{t("原始代码")}</span><pre className="av-code">{evidence.original_code}</pre></> : null}
    {evidence.restored_sql ? <><span className="av-section-label">{t("模型还原的 SQL")}</span><pre className="av-code">{evidence.restored_sql}</pre></> : null}
    {evidence.rationale ? <span className="av-caption">{evidence.rationale}</span> : null}
  </div>;
}

function EditEdge({ c, edit, onEdit, pending, onCancel, onReject, onSave }: {
  c: ImportCandidate; edit: { relation: string; target_column: string; reason: string }; onEdit: (v: { relation: string; target_column: string; reason: string }) => void;
  pending: boolean; onCancel: () => void; onReject: () => void; onSave: () => void;
}) {
  return <div className="av-stack av-import-candidate-edit">
    <div className="av-formrow"><label className="av-field"><span className="av-label">{t("关系")}</span>
      <Select value={edit.relation} onChange={(v) => onEdit({ ...edit, relation: v })}>{Object.entries(RELATION_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</Select></label>
      <label className="av-field"><span className="av-label">{t("目标列")}</span><input className="av-input av-mono" value={edit.target_column} onChange={(e) => onEdit({ ...edit, target_column: e.target.value })} /></label></div>
    <label className="av-field"><span className="av-label">{t("理由")}</span><input className="av-input" value={edit.reason} onChange={(e) => onEdit({ ...edit, reason: e.target.value })} /></label>
    <div className="av-actions av-import-actions">
      <button type="button" className="av-btn av-btn--secondary" disabled={pending} onClick={onCancel}>{t("取消")}</button>
      <button type="button" className="av-btn av-btn--secondary" disabled={pending || !edit.reason.trim()} onClick={onReject}>{c.status === "written" ? t("撤销这条边") : t("驳回")}</button>
      <button type="button" className="av-btn av-btn--primary" disabled={pending || !edit.reason.trim()} onClick={onSave}>{pending ? t("保存中…") : t("保存修改")}</button>
    </div>
  </div>;
}

const occurrences = (item: Unmatched) => item.count;

function UnmatchedTables({ items, onShow }: { items: Unmatched[]; onShow: (ref: string) => void }) {
  if (!items.length) return <Empty text={t("语句中的表都对上了已采集资产或本批次产生的表")} compact />;
  // 读取来源没对上是血缘缺口，排在前面；写入目标多是尚未建表的设计时目标
  const sorted = [...items].sort((a, b) => Number(a.role === "target") - Number(b.role === "target") || b.count - a.count);
  return <>
    <p className="av-caption av-import-tables-hint">{t("既不是已采集资产、也不是本批次建出的表。读取来源请先采集，或检查库名与默认库。")}</p>
    <div className="av-list">{sorted.map((item) => <div key={`${item.ref}:${item.role}`} className="av-row av-import-table">
      <span className="av-row__main"><span className="av-row__title av-mono" title={item.ref}>{shortName(item.ref)}</span>
        <span className="av-row__meta">{item.role === "target" ? t("写入目标") : t("读取来源")} · {t("{{n}} 处", { n: occurrences(item) })}</span></span>
      <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={() => onShow(item.ref)}>{t("查看语句")}</button>
    </div>)}</div>
  </>;
}

function Files({ b, stats }: { b: Batch; stats: Stats }) {
  const qc = useQueryClient();
  const saved = useSqlFunctions(b.datasource_id);
  const [message, setMessage] = useMessage();
  const suggested = (stats.suggested_sql_functions ?? []).filter((n) => !saved.data?.sql_functions.includes(n));
  // 逐个原子追加：已保存规则未加载或读取失败时也不会被覆盖。
  const add = useAction(async (names: string[]) => {
    for (const name of names) await unwrap(api.POST("/api/imports/sql-functions/{datasource_id}", {
      params: { path: { datasource_id: b.datasource_id } }, body: { sql_function: name } }));
  }, ["import-sql-functions"]);
  const tz = useTimeZone();
  return <div className="av-stack">
    {suggested.length ? <div className="av-import-suggest">
      <span>{t("模型发现这些函数在执行 SQL：")}<span className="av-mono">{suggested.join(", ")}</span></span>
      <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={add.isPending}
        onClick={() => add.mutate(suggested, { onSuccess: () => { setMessage({ ok: true, text: t("已加入规则，下次导入按规则抽取") }); qc.invalidateQueries({ queryKey: ["import-sql-functions"] }); },
          onError: (e) => { setMessage({ ok: false, text: errorText(e) }); qc.invalidateQueries({ queryKey: ["import-sql-functions"] }); } })}>{t("加入规则")}</button>
    </div> : null}
    <Banner message={message} />
    <div className="av-list">{b.files.map((f) => {
      const counts = (f.stats as { statements?: Record<string, number>; replaced_edges?: number; invalidated_decisions?: number; inherited_decisions?: number }) ?? {};
      return <div key={f.id} className="av-row av-import-file">
        <span className="av-row__main"><span className="av-row__title av-mono">{f.source_key}</span>
          <span className="av-row__meta">{f.language} · {t("{{n}} 条", { n: f.statement_count })}
            {f.revision > 1 ? ` · ${t("第 {{n}} 版，替换上一版 {{edges}} 条边", { n: f.revision, edges: counts.replaced_edges ?? 0 })}` : ""}
            {counts.inherited_decisions ? ` · ${t("沿用人工决定 {{n}}", { n: counts.inherited_decisions })}` : ""}
            {counts.invalidated_decisions ? ` · ${t("{{n}} 条人工决定因原文变化失效", { n: counts.invalidated_decisions })}` : ""}</span></span>
        <Status value={f.superseded_by ? "superseded" : f.status} />
      </div>;
    })}</div>
    {b.notes?.length ? <div className="av-import-notes"><span className="av-section-label">{t("说明")}</span>
      <ul>{b.notes.map((n, i) => <li key={i} className="av-caption">{n}</li>)}</ul></div> : null}
    <span className="av-caption">{t("提交于 {{time}}", { time: formatTime(b.created_at, tz) })}</span>
  </div>;
}

type PayloadItem = { file_id?: string; filename?: string; lines?: number[]; kind?: string; text?: string; redactions?: number; sent_to?: string;
  note?: string; error?: string; reused?: boolean; skipped?: string | boolean; comparisons?: { lines: number[]; note: string }[]; dropped?: string[] };

function Payload({ b, version, onLocate }: { b: Batch; version: string; onLocate: (c: { file_id?: string | null; lines?: number[] | null }) => void }) {
  const payload = useImportPayload(b.id, version);
  return <QueryState query={payload} isEmpty={(d) => !d.length} emptyText={t("没有发给模型的内容")} compact>
    {(d) => <div className="av-stack">{(d as PayloadItem[]).map((p, i) => {
      const sent = !!p.sent_to && !String(p.sent_to).startsWith("未发送");
      return <section key={i} className="av-panel">
        <div className="av-panel__hd"><h2 className="av-panel__title">
          {b.files.length > 1 && p.filename ? `${p.filename} · ` : ""}{t("第 {{lines}} 行", { lines: (p.lines ?? []).join("–") })}</h2>
          <span className={`av-status ${sent ? "av-status--warn" : ""}`}>{p.sent_to ? String(p.sent_to) : t("未发送")}</span></div>
        <div className="av-panel__bd av-import-payload">
          <span className="av-caption">{t("已脱敏 {{n}} 处", { n: p.redactions ?? 0 })}{p.reused ? ` · ${t("复用上次结果")}` : ""}{p.note ? ` · ${p.note}` : ""}{p.error ? ` · ${p.error}` : ""}</span>
          {p.comparisons?.length ? <span className="av-caption">{p.comparisons.map((c) => `${t("第 {{lines}} 行", { lines: c.lines.join("–") })} ${c.note}`).join("；")}</span> : null}
          {p.dropped?.length ? <span className="av-caption">{t("已丢弃：")}{p.dropped.slice(0, 3).join("；")}{p.dropped.length > 3 ? ` ${t("等 {{n}} 条", { n: p.dropped.length })}` : ""}</span> : null}
          <pre className="av-code">{p.text}</pre>
          {p.lines?.length ? <div className="av-actions"><button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={() => onLocate({ file_id: p.file_id, lines: p.lines })}>{t("定位原文")}</button></div> : null}
        </div>
      </section>;
    })}</div>}
  </QueryState>;
}
