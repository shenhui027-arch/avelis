// 问数：对话（问题、过程、回答、追问）| 右侧证据（数据集、图表、SQL）；新建、最近对话、经验与定时任务在应用侧边栏。
// 报告里的数字由后端按引用渲染（ADR 0009）：数字下划线可悬停看来源数据集、取值路径与原值；人机协作见 AnalysisInteractions（ADR 0013）。
import { useQuery } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router";

import { api, token, unwrap, type Schemas } from "../../api/client";
import { errorText, useAction, useAnalysisTasks, useExampleQuestions, useNumberStyle, useTimeZone, type Answer } from "../../api/hooks";
import { Code, DateInput, Drawer, Empty, Hint, Icon, Pill, QueryState, Seg, Status } from "../../components/ui";
import { formatTime, lastMonths } from "../../format";
import { FilterAlignmentNote } from "../../components/FilterAlignmentNote";
import { CandidatesPanel, ClarifyPanel, type ClarifyItem, ConfirmPanel, ExperienceList, FollowupForm, SourcesPanel, useContinue, type ProposalItem } from "./AnalysisInteractions";
import { BasisState, Evidence, ResultTable } from "../ask/AskPage";
import { cellText, type AskIntent, type AskResult } from "../ask/presentation";
import { ChartPanel } from "../../components/charts/ChartPanel";
import { Insights, Narrative } from "../../components/AnswerInsights";
import type { ChartSpec } from "../../components/charts/chartOption";
import { ArchivedConversations } from "../../layout/Conversations";
import { datasetCell, datasetLabel, datasetNumeric, DatasetView, EvidenceLink, useOpenDataset } from "./DatasetView";
import { ChipMenu, SkillPickerTrigger, SkillTags, useSkillPicker } from "./ComposeOptions";
import { motion } from "motion/react";
import { RevealGroup, revealItem } from "../../components/Motion";
import { CronPicker, describeCron } from "./CronPicker";
import { ModelPicker, useModelChoice } from "./ModelPicker";
import { Interject, RunTranscript } from "./RunTranscript";
import { currentLanguage, t } from "../../i18n";

type Run = Schemas["AnalysisRunOut"];
type Task = Schemas["AnalysisTaskOut"];
type Segment = { kind: "text" | "value" | "count" | "label" | "window" | "error"; text: string; ref?: string;
  path?: string; label?: string; precision?: number | null; raw?: string | string[]; measure?: string; unit?: string;
  direction?: string; role?: string; source?: string; explore?: boolean; assumption?: boolean;
  error?: string };
type FieldRecord = { parts?: unknown; text: string; segments?: Segment[]; status?: string; errors?: string[] };
type Claim = { type: string; text: string; segments?: Segment[]; refs?: string[]; premise?: string | null;
  validation?: string | null; details?: Record<string, FieldRecord> };
type Check = { index: number; status: string; problems?: string[]; missing_refs?: string[] };
type FieldCheck = { field: string; status: string; problems?: string[] };
type CauseView = { index: number; type?: string; label?: string | null;
  entity?: { metric_code?: string; dimension?: string; dimension_label?: string; member?: string; factor_metric_code?: string; evidence_id?: string };
  direction?: string; contribution?: { text: string; segments?: Segment[] } | null; evidence?: string[]; status: string;
  errors?: string[] };
type WindowKind = "none" | "last_month" | "fixed";

const CAUSE_TYPE: Record<string, string> = { member: t("成员"), factor: t("因子"), calendar: t("日历"), governance: t("治理事件") };
const CONCLUSION: Record<string, string> = { ranked_reasons: t("有排序的原因"), offsetting: t("成员变化相互抵消"), no_material_change: t("没有实质变化"),
  insufficient_evidence: t("证据不足") };
const TYPE_LABEL: Record<string, string> = { fact: t("事实"), calc: t("计算"), inference: t("推断"), recommendation: t("建议") };
const FIELD_LABEL: Record<string, string> = { title: t("标题"), summary: t("摘要"), unexplained: t("未解释"), method: t("分析方法") };
const REPORT_STATUS: Record<string, [string, "ok" | "warn" | "danger"]> = {
  all_passed: [t("来源全部通过"), "ok"], with_inference: [t("来源通过 · 含推断"), "warn"],
  no_facts: [t("没有可核实的事实"), "warn"], failed: [t("有未通过的结论"), "danger"],
};
const SOURCE_LABEL: Record<string, string> = { governed: t("口径"), explore: t("探索"), calc: t("计算"), context: t("上下文") };
const MEASURE_LABEL: Record<string, string> = { level: t("水平值"), delta: t("差额"), ratio: t("比率"), rate_change: t("变化率"),
  share: t("占比"), count: t("计数"), unknown: t("量纲未知") };
const KIND_LABEL: Record<string, [string, "ok" | "warn" | "info"]> = { governed: [t("受治理"), "ok"], calc: [t("计算"), "info"],
  context: [t("上下文"), "info"], explore: [t("探索"), "warn"], ungoverned: [t("未治理"), "warn"] };

const useTasks = useAnalysisTasks;
const useRun = (id?: string) => useQuery({
  queryKey: ["analysis-run", id], enabled: !!id,
  // 接口类型里的元组（热力图单元格）经请求库的类型推导会被放宽成普通数组，这里按响应模型标回去
  queryFn: async () => (await unwrap(api.GET("/api/analysis/runs/{run_id}", { params: { path: { run_id: id! } } }))) as Run,
  refetchInterval: (q) => (q.state.data && ["queued", "running"].includes(q.state.data.status) ? 2000 : false),
});

async function download(path: string, filename: string) {
  const res = await fetch(path, { headers: { Authorization: `Bearer ${token.get()}`, "Accept-Language": currentLanguage() } });
  if (!res.ok) throw new Error((await res.json().catch(() => ({ detail: t("导出失败") }))).detail ?? t("导出失败"));
  const url = URL.createObjectURL(await res.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

/** 2026-09-01 → 9/1，用在胶囊上，完整日期在菜单里 */
const shortDate = (iso: string) => iso ? `${Number(iso.slice(5, 7))}/${Number(iso.slice(8, 10))}` : "…";

function windowBody(kind: WindowKind, fixed: { start: string; end: string; cs: string; ce: string }) {
  if (kind === "none") return undefined;
  if (kind === "last_month") return { kind };
  return { kind, start: fixed.start, end: fixed.end, compare_start: fixed.cs || undefined, compare_end: fixed.ce || undefined };
}

export function AnalysisPage() {
  const { runId, section } = useParams();
  const view = section === "tasks" || section === "experiences" || section === "archived" ? section : "runs";
  const inRun = view === "runs" && !!runId;
  const dataset = useOpenDataset();
  const run = useRun(inRun ? runId : undefined);
  // 取数类回答且无数据集时不显示右侧证据空栏，让主区占满
  const lookupEmpty = (run.data?.report as { kind?: string } | null)?.kind === "lookup" && !run.data?.datasets?.length;
  const evidence = inRun && !!run.data && !lookupEmpty;
  return (
    <div className={`av-analysis${evidence ? " has-evidence" : ""}`}>
      <main className="av-analysis__center">
        {runId && view === "runs" && dataset.ref ? <div className="av-analysis__scroll">
          <DatasetView key={dataset.ref} runId={runId} refId={dataset.ref} onClose={dataset.close} onOpen={dataset.open}
            canRerun={!!run.data && !["queued", "running", "waiting"].includes(run.data.status)}
            onDownload={(seq, ref) => download(`/api/analysis/runs/${runId}/datasets/${seq}/csv`, `analysis-${runId}-${ref}.csv`)} />
        </div> : runId && view === "runs" ? <RunDetail id={runId} /> : <div className="av-analysis__scroll">
          {view === "tasks" ? <div className="av-analysis__page"><PageHead title={t("定时任务")} /><TaskList /></div>
            : view === "archived" ? <div className="av-analysis__page"><PageHead title={t("已归档的对话")} hint={t("归档的对话可以打开查看、恢复到左侧，或永久删除")} /><ArchivedConversations /></div>
            : view === "experiences" ? <ExperiencesPage />
            : <Composer />}
        </div>}
      </main>
      {evidence ? <aside className="av-analysis__evidence" aria-label={t("证据")}><Datasets key={runId} id={runId!} /></aside> : null}
    </div>
  );
}

function PageHead({ title, hint, children }: { title: string; hint?: string; children?: ReactNode }) {
  // 页头复用 .av-pagehd 结构，右侧可放分段控件或操作项
  return <header className="av-pagehd av-analysis__pagehd"><h1>{title}{hint ? <Hint text={hint} /> : null}</h1>{children}</header>;
}

function ExperiencesPage() {
  const [all, setAll] = useState(false);
  // 将个人经验的“有效 / 全部”分段筛选移入页头右侧插槽
  return (
    <div className="av-analysis__page">
      <PageHead title={t("个人经验")} hint={t("在分析中确认、否决或补充后保存；之后的分析按口径与资产自动召回")}>
        <Seg label={t("显示范围")} value={all ? "all" : "active"} onChange={(v) => setAll(v === "all")}
          options={[{ value: "active", label: t("有效") }, { value: "all", label: t("全部") }]} />
      </PageHead>
      <ExperienceList all={all} />
    </div>
  );
}

/** 状态：一个点加两三个字。 */
function RunState({ run }: { run: Run }) {
  if (run.status === "waiting") return <Status value="waiting" label={t("待确认")} />;
  if (run.status === "stopped") return <Status value="stopped" label={t("已停止")} />;
  if (run.status !== "succeeded") return <Status value={run.status} />;
  return run.verified ? <Status value="verified" label={t("核验通过")} /> : <Status value="unverified" label={t("有未核实")} />;
}

function Composer() {
  const navigate = useNavigate();
  const [text, setText] = useState("");
  // 问数与分析合并为一个入口：两类示例都给，系统按问题自己判断走取数还是多步分析
  const askExamples = useExampleQuestions("ask").data?.questions ?? [];
  const analysisExamples = useExampleQuestions("analysis").data?.questions ?? [];
  const examples = [...new Set([...askExamples.slice(0, 2), ...analysisExamples.slice(0, 3)].map((q) => q.text))];
  const [kind, setKind] = useState<WindowKind>("none");
  const tz = useTimeZone();
  // 用户改过日期之前，默认值随业务时区重算（设置加载前后时区可能不同）
  const [edited, setFixed] = useState<ReturnType<typeof lastMonths> | null>(null);
  const fixed = edited ?? lastMonths(tz);
  const [mode, setMode] = useState<"auto" | "always">("auto");
  const [scheduling, setScheduling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const picker = useModelChoice();
  const skillPicker = useSkillPicker(text, setText);
  const start = useAction(() => unwrap(api.POST("/api/analysis/runs", {
    body: {
      question: text,
      window: windowBody(kind, fixed),
      confirmation_mode: mode,
      ...(skillPicker.skills.length ? { skills: skillPicker.skills } : {}),
      ...picker.body,
    } as never,
  })), ["analysis-runs"]);
  return (
    <RevealGroup as="section" className="av-analysis__compose">
      <motion.h1 className="av-compose__title" variants={revealItem}>{t("想知道什么？")}</motion.h1>
      {error ? <div className="av-banner av-banner--danger">{error}</div> : null}
      <motion.form className="av-compose" variants={revealItem} onSubmit={(e) => {
        e.preventDefault();
        setError(null);
        start.mutate(undefined, {
          onSuccess: (r) => { skillPicker.clear(); navigate(`/analysis/runs/${r.id}`); },
          onError: (err) => setError(errorText(err)),
        });
      }}>
        <SkillTags skills={skillPicker.skills} onRemove={skillPicker.remove} />
        <div className="av-compose__row">
          <SkillPickerTrigger state={skillPicker} />
          <label htmlFor="aq" className="av-sr">{t("分析问题")}</label>
          <textarea
            ref={skillPicker.textareaRef}
            id="aq"
            rows={3}
            value={text}
            placeholder={t("问一个数，或者问为什么变了、有什么关系")}
            {...skillPicker.textareaProps}
            onKeyDown={(e) => {
              if (skillPicker.onTextareaKeyDown(e)) return;
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !e.nativeEvent.isComposing && e.keyCode !== 229) {
                e.currentTarget.form?.requestSubmit();
              }
            }}
          />
        </div>
        <div className="av-compose__bar">
          <ChipMenu down icon="calendar" title={t("时间范围")} value={kind} onChange={setKind} keepOpenFor="fixed"
            display={kind === "fixed" ? `${shortDate(fixed.start)}–${shortDate(fixed.end)}` : undefined}
            options={[{ value: "none", label: t("按问题理解"), hint: t("从问题中识别时间，未指定时询问") },
              { value: "last_month", label: t("上月对比前月"), hint: t("上一个自然月，对比它的前一个月") },
              { value: "fixed", label: t("指定日期"), hint: t("自己选本期与对比期") }]}>
            {kind === "fixed" ? <div className="av-chip__dates">
              {([["start", t("本期起")], ["end", t("本期止")], ["cs", t("对比起")], ["ce", t("对比止")]] as const).map(([k, label]) => (
                <label key={k}><span>{label}</span>
                  <DateInput label={label} value={fixed[k]} clearable={false} onChange={(v) => setFixed({ ...fixed, [k]: v })} /></label>))}
            </div> : null}
          </ChipMenu>
          <ChipMenu down icon="shield" title={t("确认依据")} value={mode} onChange={setMode}
            display={mode === "auto" ? t("自动确认") : t("每次确认")}
            options={[{ value: "auto", label: t("自动"), hint: t("只有模型推断的依据才需要你确认") },
              { value: "always", label: t("每次"), hint: t("预检后先确认再分析") }]} />
          <span className="av-compose__spacer" />
          <ModelPicker state={picker} />
          <button type="button" className="av-chip__btn" disabled={!text.trim()} onClick={() => setScheduling(true)}
            title={t("按时间自动重跑这个问题")}><Icon name="clock" small />{t("定时")}</button>
          <button className="av-compose__send" type="submit" disabled={start.isPending || !text.trim()}
            aria-label={t("提问")} title={t("提问（Ctrl + Enter）")}>
            {start.isPending ? <span className="av-compose__spin" aria-hidden /> : <Icon name="send" />}</button>
        </div>
      </motion.form>
      {examples.length ? <motion.div className="av-compose__examples" role="group" aria-label={t("问题示例")} variants={revealItem}>
        {examples.map((e) => <button key={e} type="button" className="av-compose__example"
          onClick={() => { setText(e); document.getElementById("aq")?.focus(); }}>{e}</button>)}
      </motion.div> : null}
      {scheduling ? <ScheduleDrawer body={{ question: text, window: windowBody(kind, fixed) }} onClose={() => setScheduling(false)} /> : null}
    </RevealGroup>
  );
}

function ScheduleDrawer({ body, onClose }: { body: Record<string, unknown>; onClose: () => void }) {
  const navigate = useNavigate();
  const [cron, setCron] = useState(""); // 不预设节奏：留空只保存、不定时
  const [error, setError] = useState<string | null>(null);
  const save = useAction(() => unwrap(api.POST("/api/analysis/tasks", {
    body: { ...body, title: "", schedule_cron: cron || null } as never })), ["analysis-tasks"]);
  return (
    <Drawer title={t("保存为定时任务")} onClose={onClose} footer={<div className="av-actions">
      <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={onClose}>{t("取消")}</button>
      <button type="button" className="av-btn av-btn--primary av-btn--sm" disabled={save.isPending}
        onClick={() => save.mutate(undefined, { onSuccess: () => navigate("/analysis/tasks"), onError: (e) => setError(errorText(e)) })}>{t("保存")}</button>
    </div>}>
      {error ? <div className="av-banner av-banner--danger">{error}</div> : null}
      <div className="av-field"><span className="av-label">{t("什么时候运行")}</span><CronPicker value={cron} onChange={setCron} /></div>
      <span className="av-caption">{t("定时运行时不会停下来等你确认依据，结果在“定时任务”里查看。")}</span>
    </Drawer>
  );
}

// 报告里的引用段：数字带下划线，右上角只标数据集编号（悬停看原值、量纲、来源）；探索数据用虚线与琥珀色区分
const DatasetSql = createContext<Record<string, string>>({});

function Segments({ segments, text }: { segments?: Segment[]; text?: string | null }) {
  const sqlOf = useContext(DatasetSql);
  if (!segments?.length) return <>{text}</>;
  return <>{segments.map((s, i) => {
    if (s.kind === "text") return <span key={i}>{s.text}</span>;
    if (s.kind === "error") return <span key={i} className="av-num is-error" title={s.error}>{s.text}</span>;
    const raw = Array.isArray(s.raw) ? s.raw.join(" ~ ") : s.raw;
    const title = [s.label ?? s.path, t("原值 {{raw}}", { raw }),
      s.measure ? `${MEASURE_LABEL[s.measure] ?? s.measure}${s.unit ? `（${s.unit}）` : ""}` : "",
      s.precision != null ? t("{{precision}} 位小数", { precision: s.precision }) : "", s.direction ? t("方向 {{v1}}", { v1: s.direction === "up" ? t("上升") : t("下降") }) : "",
      s.explore ? (s.source === "explore" && s.ref && sqlOf[s.ref] ? t("探索查询（未经口径审核）\nSQL：{{v1}}", { v1: sqlOf[s.ref] })
        : t("来源含探索查询（未经口径审核）")) : "", s.assumption ? t("假设计算") : "",
      s.role === "proposal" ? t("建议目标（非观测值）") : ""].filter(Boolean).join(" · ");
    const cls = ["av-num", s.kind === "label" ? "" : "av-mono", s.role === "proposal" ? "is-proposal" : "",
      s.explore ? "is-explore" : ""].filter(Boolean).join(" ");
    return <span key={i} className={cls} title={title}>{s.text}
      {s.ref && s.ref !== "window" ? <sup>{s.ref}</sup> : null}</span>;
  })}</>;
}

function ExportMenu({ run, onError }: { run: Run; onError: (e: string) => void }) {
  const [open, setOpen] = useState(false);
  const [full, setFull] = useState(false); // 默认导出给决策者看的简报；勾选后带上依据附录（完整版）
  const ref = useRef<HTMLSpanElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  // 支持点外部或按 Esc 键关闭导出菜单
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent) {
        if (e.key !== "Escape") return;
        setOpen(false);
        if (ref.current?.contains(document.activeElement)) trigger.current?.focus();
      } else if (!ref.current?.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", close);
    return () => { window.removeEventListener("mousedown", close); window.removeEventListener("keydown", close); };
  }, [open]);
  const item = (label: string, format: string, ext: string) => (
    <button type="button" role="menuitem" className="av-menu__item" onClick={() => {
      setOpen(false);
      const audience = full ? "full" : "brief";
      download(`/api/analysis/runs/${run.id}/export?format=${format}&audience=${audience}`,
        `analysis-${run.id}${full ? "-full" : ""}.${ext}`).catch((e) => onError(errorText(e)));
    }}>{label}</button>);
  return (
    <span className="av-analysis__menu" ref={ref}>
      <button ref={trigger} type="button" className="av-btn av-btn--secondary av-btn--sm" aria-haspopup="menu" aria-expanded={open}
        onClick={() => setOpen(!open)}><Icon name="download" />{t("导出")}<Icon name="chevron" small /></button>
      {open ? <div className="av-menu" role="menu">
        <div className="av-menu__group" role="group" aria-label={t("导出")}>
          {item("PDF", "pdf", "pdf")}{item("HTML", "html", "html")}{item("Markdown", "md", "md")}
        </div>
        {/* 去掉原生复选框，改用与其他菜单项左对齐的 menuitemcheckbox */}
        <button type="button" role="menuitemcheckbox" aria-checked={full} className="av-menu__item av-analysis__export-full"
          onClick={() => setFull(!full)}>
          <span>{t("附带依据（数据来源与查询明细）")}</span>
          <span className="av-analysis__export-check" aria-hidden>{full ? <Icon name="check" small /> : null}</span>
        </button>
      </div> : null}
    </span>
  );
}

/** 原因排序（ADR 0009 第 5 点）：第一项是首要原因；每项标明实体、方向、贡献与证据。 */
/** 还没查清、可以让模型继续查的点：报告里“仍未解释”的每一条，加上未通过来源校验的结论。
 *  点“继续查”把它放进底部输入框，用户可以改了再发。 */
function NextSteps({ text, unverified, onPick }: { text?: string; unverified: number; onPick?: (text: string) => void }) {
  const items = (text ?? "").split(/[；;]\s*/).map((s) => s.trim().replace(/[。.]$/, "")).filter(Boolean);
  if (unverified) items.push(t("有 {{count}} 条事实或计算未通过来源校验，需要重新核对", { count: unverified }));
  if (!items.length) return null;
  return (
    <section className="av-next" aria-label={t("待查明")}>
      <h2><Icon name="search" />{t("待查明 · 可继续分析")}</h2>
      <ul>
        {items.map((item) => (
          <li key={item}>
            <span>{item}</span>
            <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={!onPick}
              title={onPick ? t("在这个对话里接着分析这一条") : t("等当前这一轮完成后再继续")} onClick={() => onPick?.(t("继续分析：{{item}}", { item }))}>
              {t("继续分析")}</button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Causes({ runId, causes, conclusion }: { runId: string; causes?: CauseView[]; conclusion?: string }) {
  if (!conclusion && !causes?.length) return null;
  return <section className="av-doc__section" aria-label={t("原因排序")}>
    <h2>{t("原因排序")} {conclusion && conclusion !== "ranked_reasons" ? <Pill tone="warn">{CONCLUSION[conclusion] ?? conclusion}</Pill> : null}</h2>
    {causes?.length ? <ol className="av-causes">
      {causes.map((c, n) => {
        const e = c.entity ?? {};
        const entity = e.member ? `${e.dimension_label ?? e.dimension ?? ""} · ${c.label ?? e.member}`
          : c.label ?? e.factor_metric_code ?? e.evidence_id ?? (c.type === "calendar" ? t("日历因素") : t("未标注实体"));
        return <li key={c.index} className={`av-cause${c.status === "unverified" ? " is-unverified" : ""}${n === 0 ? " is-primary" : ""}`}>
          <span className="av-cause__rank">{n + 1}</span>
          <span className="av-cause__main">
            <span className="av-cause__entity">{entity}</span>
            <span className="av-cause__type">{CAUSE_TYPE[c.type ?? ""] ?? c.type ?? t("原因")}{n === 0 ? t(" · 首要原因") : ""}</span>
            {(c.errors ?? []).map((p, j) => <span key={j} className="av-claim__bad">{p}</span>)}
          </span>
          <span className={`av-cause__dir is-${c.direction}`}>{c.direction === "up" ? "↑" : c.direction === "down" ? "↓" : ""}</span>
          <span className="av-cause__value">{c.contribution ? <Segments segments={c.contribution.segments} text={c.contribution.text} /> : null}</span>
          <span className="av-claim__refs">{(c.evidence ?? []).map((ref) => <EvidenceLink key={ref} runId={runId} refId={ref} />)}</span>
        </li>;
      })}
    </ol> : <p className="av-caption">{conclusion === "no_material_change" ? t("两期相比没有实质变化，不给出原因") : t("证据不足，不给出原因排序")}</p>}
  </section>;
}

type Figure = { ref: string; title?: string | null; chart: ChartSpec };

/** 报告插图：模型用 chart 工具选进报告的数据集（没选时为被引用最多的关键数据集），规格由后端校验并物化。 */
function Figures({ runId, figures }: { runId: string; figures: Figure[] }) {
  if (!figures.length) return null;
  return <section className="av-doc__section av-figures" aria-label={t("图表")}>
    {figures.map((f) => <figure key={f.ref} className="av-figure">
      {!f.chart.title && f.title ? <h3 className="av-chartpanel__title">{f.title}</h3> : null}
      <ChartPanel charts={[f.chart]} title={f.chart.title ?? f.title ?? f.ref} />
      {/* EvidenceLink 已含“数据”文案，去掉前置重复文字 */}
      <figcaption className="av-caption"><EvidenceLink runId={runId} refId={f.ref} /></figcaption>
    </figure>)}
  </section>;
}

/** 取数类问题（ADR 0013 合并入口）：由问数引擎直接给出受治理查询结果，不展开多步分析。 */
/** onContinue：对话级的续问入口（接在最新一轮之后）；最新一轮没结束或已有续问在途时为空，追问按钮随之禁用。 */
function LookupAnswer({ a, onContinue }: { a: Answer; onContinue?: (text: string) => void }) {
  const result = (a.result ?? {}) as AskResult;
  const intent = (a.intent ?? {}) as AskIntent;
  const single = result.rows?.length === 1 && result.columns?.length === 1;
  const style = useNumberStyle();
  return (
    <section className="av-doc__section" aria-label={t("结果")}>
      {a.recheck_status === "needs_recheck"
        ? <div className="av-banner av-banner--warn" role="status">{t("所用口径因上游变化待复核，下面的数值按当时的口径计算")}</div> : null}
      {single ? <div className="av-lookup__value"><span>{cellText(result, result.columns![0], result.rows![0][0], false, style)}</span>
        <span className="av-caption">{a.message}</span></div>
        : <p className="av-doc__lead">{a.message}</p>}
      {!single && result.insights?.length ? <Insights items={result.insights} /> : null}
      {a.explanation && a.explanation_source === "model" ? <Narrative text={a.explanation} onPick={(text) => onContinue?.(text)} disabled={!onContinue} /> : null}
      {!single && result.rows?.length ? <ChartPanel charts={result.charts ?? []} title={a.message ?? a.question}>
        <ResultTable result={result} dimensions={intent.group_by ?? []} /></ChartPanel> : null}
      <details className="av-disclosure av-doc__more"><summary>{t("口径、来源与 SQL")}</summary><div className="av-doc__evidence"><Evidence a={a} /></div></details>
    </section>
  );
}

/** 一段对话：所有轮次按时间顺序在同一页，底部输入框接着最新一轮继续；打开某一轮的地址时定位到那一轮。 */
function RunDetail({ id }: { id: string }) {
  const run = useRun(id);
  const convId = (run.data as (Run & { conversation_id?: string }) | undefined)?.conversation_id;
  const conv = useQuery({
    queryKey: ["analysis-conversation", convId], enabled: !!convId,
    queryFn: () => unwrap(api.GET("/api/analysis/conversations/{conversation_id}", { params: { path: { conversation_id: convId! } } })),
  });
  const latestId = conv.data?.latest_run_id ?? id;
  const latest = useRun(latestId);
  const continueWith = useContinue(latestId);
  const focus = useRef<HTMLDivElement>(null);
  const ids = conv.data?.runs.map((t) => t.id) ?? [id];
  const total = ids.length;
  // 目标轮挂载后定位到视口顶部；前序轮次由骨架加载为正文撑开高度时，在用户主动滚动前保持对齐
  useEffect(() => {
    const el = focus.current;
    const scroller = el?.closest(".av-analysis__scroll");
    if (total < 2 || !run.isSuccess || !el || !scroller) return;
    const box = el.getBoundingClientRect();
    const view = scroller.getBoundingClientRect();
    if (!(box.bottom > view.top && box.top < view.bottom)) {
      el.scrollIntoView({ block: "start" });
    }
    const idx = ids.indexOf(id);
    if (idx <= 0 || typeof ResizeObserver === "undefined") return;
    const before = Array.from(scroller.querySelectorAll(":scope > .av-turn")).slice(0, idx);
    if (!before.length) return;
    let userMoved = false;
    const stop = () => { userMoved = true; };
    const heights = new Map(before.map((node) => [node, node.getBoundingClientRect().height]));
    const ro = new ResizeObserver(() => {
      if (userMoved) return;
      let changed = false;
      for (const node of before) {
        const h = node.getBoundingClientRect().height;
        if (Math.abs(h - (heights.get(node) ?? 0)) >= 1) {
          heights.set(node, h);
          changed = true;
        }
      }
      if (changed) el.scrollIntoView({ block: "start" });
    });
    before.forEach((node) => ro.observe(node));
    scroller.addEventListener("wheel", stop, { passive: true });
    scroller.addEventListener("touchmove", stop, { passive: true });
    scroller.addEventListener("pointerdown", stop, { passive: true });
    return () => {
      ro.disconnect();
      scroller.removeEventListener("wheel", stop);
      scroller.removeEventListener("touchmove", stop);
      scroller.removeEventListener("pointerdown", stop);
    };
  }, [total, id, run.isSuccess]);
  // 报告详情加载与出错状态接入 QueryState，避免闪现空态插画
  return (
    <QueryState query={run}>
      {(runData) => {
        const r = latest.data ?? runData;
        const live = ["queued", "running", "waiting"].includes(r.status);
        const done = r.status === "succeeded";
        const onContinue = done && !continueWith.pending ? (text: string) => continueWith.send(text) : undefined;
        return (
          <>
            <div className="av-analysis__scroll">
              {ids.map((tid, i) => <div key={tid} ref={tid === id ? focus : undefined} className="av-turn">
                <TurnById id={tid} turn={i + 1} total={total} onContinue={onContinue} autoFollow={id === latestId} /></div>)}
              {continueWith.error ? <div className="av-banner av-banner--danger av-turn__error">{continueWith.error}</div> : null}
            </div>
            {live || done ? <div className="av-dock">{live ? <Interject run={r} /> : <FollowupForm run={r} />}</div> : null}
          </>
        );
      }}
    </QueryState>
  );
}

/** 按 id 取一轮的完整详情（报告、过程、数据集）再渲染；对话接口只给各轮概要。 */
function TurnById({ id, turn, total, onContinue, autoFollow }: { id: string; turn: number; total: number; onContinue?: (text: string) => void;
  autoFollow?: boolean }) {
  const run = useRun(id);
  // 对话单轮加载与出错状态接入 QueryState
  return (
    <QueryState query={run} rows={3}>
      {(data) => <RunTurn r={data} turn={turn} total={total} onContinue={onContinue} autoFollow={autoFollow} />}
    </QueryState>
  );
}

/** 对话里的一轮：问题、过程、报告。导出与状态按这一轮。 */
function RunTurn({ r, turn, total, onContinue, autoFollow }: { r: Run; turn: number; total: number; onContinue?: (text: string) => void;
  autoFollow?: boolean }) {
  const tz = useTimeZone();
  const [error, setError] = useState<string | null>(null);
  const report = (r.report ?? {}) as { title?: string; summary?: string; claims?: Claim[]; unexplained?: string; method?: string;
    notes?: string[]; fields?: Record<string, FieldRecord>; conclusion?: string; causes?: CauseView[];
    sources?: Parameters<typeof SourcesPanel>[0]["items"]; experience_versions?: { id: string; version: number }[];
    skill_versions?: Parameters<typeof SourcesPanel>[0]["skills"] };
  const answer = (r as Run & { answer?: Answer | null }).answer;
  const lookup = (report as { kind?: string }).kind === "lookup";
  // 对话改图的一轮：只换上一轮数据的画法，没有结论与依据，只显示说明与改好的图
  const chartEdit = (report as { kind?: string }).kind === "chart";
  const hitl = r as Run & { parent_run_id?: string | null; proposal?: { items: ProposalItem[] } | null;
    pending?: { kind?: string; question?: string; options?: string[]; dimension?: string; items?: ClarifyItem[] } | null;
    experience_candidates?: Parameters<typeof CandidatesPanel>[0]["candidates"] };
  const verification = (r.verification ?? {}) as { ok?: boolean; status?: string; unverified?: number; claims?: Check[];
    fields?: FieldCheck[]; sources?: { direct?: Record<string, number>; with_explore?: number; assumption?: number;
      windows?: number; labels?: number; failed?: number } };
  const reportStatus = verification.status ? REPORT_STATUS[verification.status] : undefined;
  const sources = verification.sources ?? {};
  const composition = [
    ...Object.entries(sources.direct ?? {}).filter(([, n]) => n > 0).map(([k, n]) => `${SOURCE_LABEL[k] ?? k} ${n}`),
    ...([["with_explore", t("含探索")], ["assumption", t("假设")], ["failed", t("渲染失败")]] as const)
      .filter(([k]) => sources[k]).map(([k, label]) => `${label} ${sources[k]}`),
  ].join(" · ");
  const badFields = (verification.fields ?? []).filter((f) => f.status === "unverified");
  const checks = new Map((verification.claims ?? []).map((c) => [c.index, c]));
  const budget = (r.budget ?? {}) as { limits?: Record<string, number>; used?: Record<string, number> };
  const running = r.status === "queued" || r.status === "running";
  const done = r.status === "succeeded";
  const stopped = r.status === "stopped";
  const win = r.window as { start?: string; end?: string; compare_start?: string; compare_end?: string } | null;
  const live = running || r.status === "waiting";
  return (
    <article className="av-doc">
      <header className="av-doc__hd">
        <div className="av-doc__titles">
          <h1>{report.title && done ? <Segments segments={report.fields?.title?.segments} text={report.title} /> : r.question}</h1>
          <p className="av-doc__sub">
            {win?.start ? <span className="av-mono">{win.start} ~ {win.end}{win.compare_start ? t("　对比 {{compare_start}} ~ {{compare_end}}", { compare_start: win.compare_start, compare_end: win.compare_end }) : ""}</span> : null}
            <span>{formatTime(r.created_at, tz)}</span>
            {total > 1 ? <span className="av-turn__no">{t("第 {{turn}} 轮", { turn })}</span> : null}
          </p>
        </div>
        <div className="av-doc__actions">
          <RunState run={r} />
          {lookup && answer ? <BasisState a={answer} /> : null}
          {done && !lookup && !chartEdit ? <ExportMenu run={r} onError={setError} /> : null}
        </div>
      </header>
      {error ? <div className="av-banner av-banner--danger">{error}</div> : null}
      <DatasetSql.Provider value={Object.fromEntries((r.datasets ?? []).filter((d) => d.sql).map((d) => [d.ref, d.sql as string]))}>
      <div className="av-runlog__user is-question">
        <span className="av-runlog__bubble">{r.question}</span>
        {r.skills?.length ? (
          <div className="av-runlog__skills">
            {r.skills.map((name) => (
              <span key={name} className="av-tag av-runlog__skill-tag">
                <Icon name="skill" small />{name}
              </span>
            ))}
          </div>
        ) : null}
      </div>
      {r.steps?.length || live ? <RunTranscript run={r} live={live} autoFollow={autoFollow} footer={<>
        {report.method ? <p className="av-doc__method"><Segments segments={report.fields?.method?.segments} text={report.method} /></p> : null}
        <p className="av-caption av-doc__meta">
          <span>{t("模型")} {r.model_adapter ?? "—"}</span>
          {budget.used ? <span>{t("模型请求")} {budget.used.model_requests}/{budget.limits?.model_requests} {t("· 工具")} {budget.used.tool_calls}/{budget.limits?.tool_calls} {t("· 查询")} {budget.used.queries}/{budget.limits?.queries} · {budget.used.seconds}s</span> : null}
        </p></>} /> : null}
      {r.status === "failed" ? <div className="av-banner av-banner--danger">{t("分析失败：")}{r.error}</div> : null}
      {r.status === "clarify" ? <div className="av-banner av-banner--info">{t("需要澄清：")}{report.summary}</div> : null}
      {stopped ? <div className="av-banner av-banner--info">{t("已停止，下面是停止前已经得到的部分结果")}</div> : null}
      {r.status === "waiting" && hitl.pending?.kind === "clarify" ? <ClarifyPanel key={r.id} runId={r.id} question={hitl.pending.question ?? ""} items={hitl.pending.items ?? (hitl.pending.options?.length && hitl.pending.dimension
          ? [{ dimension: hitl.pending.dimension, original: "", options: hitl.pending.options }] : undefined)} /> : null}
      {r.status === "waiting" && hitl.pending?.kind !== "clarify"
        ? <ConfirmPanel key={r.id} runId={r.id} items={hitl.proposal?.items ?? []} /> : null}
      {done ? <FilterAlignmentNote items={(r as Run & { filter_alignment?: Parameters<typeof FilterAlignmentNote>[0]["items"] }).filter_alignment} /> : null}
      {done && lookup && answer ? <>
        <LookupAnswer a={answer} onContinue={onContinue} />
        <SourcesPanel skills={report.skill_versions} />
      </> : null}
      {done && chartEdit ? <>
        {report.summary ? <p className={verification.ok === false ? "av-banner av-banner--info" : "av-doc__method"}>{report.summary}</p> : null}
        <Figures runId={r.id} figures={(r as Run & { figures?: Figure[] }).figures ?? []} />
      </> : null}
      {(done || stopped) && !lookup && !chartEdit ? <>
        <section className="av-doc__summary" aria-label={t("摘要")}>
          <p><Segments segments={report.fields?.summary?.segments} text={report.summary} /></p>
          <div className="av-doc__verdict">
            {reportStatus ? <Pill tone={reportStatus[1]}>{reportStatus[0]}</Pill> : null}
            {composition ? <span className="av-caption">{t("数字来源")} {composition}</span> : null}
          </div>
        </section>
        {verification.ok === false && verification.unverified ? <div className="av-banner av-banner--danger">{t("有 {{count}} 条事实或计算未通过来源校验，已在“结论依据”里标出", { count: verification.unverified })}</div> : null}
        {badFields.map((f) => <details key={f.field} className="av-banner av-banner--warn av-disclosure av-fieldcheck">
          <summary>{t("“{{field}}”里有内容没能核实来源（例如直接写了数字），这部分不算核验通过", { field: FIELD_LABEL[f.field] ?? f.field })}<span className="av-fieldcheck__more">{t("查看原因")}</span></summary>
          <ul>{(f.problems ?? []).map((p, i) => <li key={i}>{p}</li>)}</ul>
        </details>)}
        <Figures runId={r.id} figures={(r as Run & { figures?: Figure[] }).figures ?? []} />
        <Causes runId={r.id} causes={report.causes} conclusion={report.conclusion} />
        <NextSteps text={report.unexplained} unverified={verification.ok === false ? verification.unverified ?? 0 : 0}
          onPick={onContinue ? (text) => onContinue(text) : undefined} />
        <details className="av-fold" open={!!(verification.ok === false && verification.unverified)}>
          <summary>{t("结论依据")} <span className="av-count">{report.claims?.length ?? 0}</span></summary>
          <ol className="av-claims">
            {(report.claims ?? []).map((c, i) => {
              const check = checks.get(i);
              const strict = c.type === "fact" || c.type === "calc";
              const bad = strict && check?.status === "unverified";
              return (
                <li key={i} className={`av-claim is-${c.type}${bad ? " is-unverified" : ""}`}>
                  <span className="av-claim__type" title={strict ? (bad ? t("来源未通过") : t("来源已校验")) : t("推断 / 建议，未核实")}>
                    {TYPE_LABEL[c.type] ?? c.type}</span>
                  <div className="av-claim__body">
                    <span><Segments segments={c.segments} text={c.text} /></span>
                    {c.premise ? <span className="av-caption">{t("前提：")}<Segments segments={c.details?.premise?.segments} text={c.premise} /></span> : null}
                    {c.validation ? <span className="av-caption">{t("验证方式：")}<Segments segments={c.details?.validation?.segments} text={c.validation} /></span> : null}
                    {bad ? (check?.problems ?? []).map((p, j) => <span key={j} className="av-claim__bad">{p}</span>) : null}
                  </div>
                  <span className="av-claim__refs">{(c.refs ?? []).map((ref) => (
                    <EvidenceLink key={ref} runId={r.id} refId={ref} />))}</span>
                </li>
              );
            })}
          </ol>
          {report.notes?.length ? <p className="av-caption">{report.notes.join(t("；"))}</p> : null}
        </details>
        <SourcesPanel items={report.sources} experiences={report.experience_versions} skills={report.skill_versions} />
      </> : null}
      {r.join_candidates?.length || r.checkpoint_warnings?.length ? (
        <section className="av-analysis__joins" aria-label={t("关联路径")}>
          {r.join_candidates?.length ? <p className="av-caption"><Icon name="link" small />{t("你确认的 {{count}} 条关联已提交为关联路径，负责人审核通过后所有分析都能用。", { count: r.join_candidates.length })}<Link to="/catalog/joins">{t("查看")}</Link></p> : null}
          {r.checkpoint_warnings?.map((w) => <div key={w} className="av-banner av-banner--warn" role="status"><Icon name="info" />{w}</div>)}
        </section>
      ) : null}
      {!running && r.status !== "waiting" ? <CandidatesPanel candidates={hitl.experience_candidates} /> : null}
      </DatasetSql.Provider>
    </article>
  );
}

/** 证据面板：数据集默认收起，点报告里的编号会展开并定位。 */
function Datasets({ id }: { id: string }) {
  const run = useRun(id);
  const style = useNumberStyle();
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<Set<string>>(new Set());
  // 证据链接经路由跳转（不触发 hashchange），按路由的 hash 展开并滚到对应数据集
  const { hash } = useLocation();
  useEffect(() => {
    const ref = decodeURIComponent(hash).replace("#dataset-", "");
    if (!ref || ref === hash) return;
    setOpen((prev) => new Set(prev).add(ref));
    requestAnimationFrame(() => document.getElementById(`dataset-${ref}`)?.scrollIntoView({ block: "nearest" }));
  }, [hash, run.data]);
  const datasets = run.data?.datasets ?? [];
  const main = useOpenDataset();
  return (
    <div className="av-evidence">
      <header className="av-evidence__hd"><h2>{t("证据")}</h2><span className="av-caption">{t("{{count}} 个数据集", { count: datasets.length })}</span></header>
      {error ? <div className="av-banner av-banner--danger">{error}</div> : null}
      {!datasets.length ? <Empty text={t("分析产生的数据集显示在这里")} compact /> : null}
      {datasets.map((d) => {
        const [label, tone] = KIND_LABEL[d.kind] ?? [d.kind, "info"];
        const isOpen = open.has(d.ref);
        // 证据预览表数值列右对齐，时间与维度列左对齐
        const numCols = isOpen && !d.restricted ? d.columns.map((_, j) => datasetNumeric(d, j)) : [];
        return (
          <section key={d.ref} id={`dataset-${d.ref}`} className={`av-dataset${isOpen ? " is-open" : ""}`}>
            <button type="button" className="av-dataset__hd" aria-expanded={isOpen} onClick={() => setOpen((prev) => {
              const next = new Set(prev);
              if (next.has(d.ref)) next.delete(d.ref); else next.add(d.ref);
              return next;
            })}>
              <span className="av-tag av-mono">{d.ref}</span>
              <span className="av-dataset__title" title={d.title}>{d.title}</span>
              <Pill tone={tone}>{label}</Pill>
              <Icon name="chevron" small />
            </button>
            {isOpen && d.restricted ? <div className="av-dataset__bd"><span className="av-caption">{t("你已没有该数据的访问权限")}</span></div>
              : isOpen ? <div className="av-dataset__bd">
              {d.charts?.length ? <ChartPanel charts={d.charts as ChartSpec[]} title={d.title} /> : null}
              <div className="av-table-wrap"><table className="av-table av-dataset__table">
                <thead><tr>{d.columns.map((c, j) => <th key={c} className={numCols[j] ? "num" : undefined}>{datasetLabel(d, c)}</th>)}</tr></thead>
                <tbody>{d.rows.slice(0, 12).map((row, i) => <tr key={i}>{row.map((v, j) => <td key={j} className={numCols[j] ? "av-mono num" : "av-mono"}>{datasetCell(d, d.columns[j], v, style)}</td>)}</tr>)}</tbody>
              </table></div>
              <div className="av-dataset__ft">
                <span className="av-caption">{t("{{count}} 行", { count: d.row_count })}{d.inputs?.length ? t(" · 输入 {{v1}}", { v1: d.inputs.join(t("、")) }) : ""}</span>
                <button type="button" className="av-btn av-btn--secondary av-btn--sm" aria-pressed={main.ref === d.ref}
                  onClick={() => main.open(d.ref)}><Icon name="fit" small />{t("展开")}</button>
                <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={() => download(
                  `/api/analysis/runs/${id}/datasets/${d.seq}/csv`, `analysis-${id}-${d.ref}.csv`).catch((e) => setError(errorText(e)))}>
                  <Icon name="download" />CSV</button>
              </div>
              {d.sql ? <details className="av-disclosure"><summary>{t("SQL 与口径版本")}</summary><Code sql={d.sql} /></details> : null}
            </div> : null}
          </section>
        );
      })}
    </div>
  );
}

function TaskList() {
  const tasks = useTasks();
  const tz = useTimeZone();
  const [error, setError] = useState<string | null>(null);
  const patch = useAction((v: { id: string; status: string }) => unwrap(api.PATCH("/api/analysis/tasks/{task_id}", {
    params: { path: { task_id: v.id } }, body: { status: v.status } as never })), ["analysis-tasks"]);
  const runNow = useAction((id: string) => unwrap(api.POST("/api/analysis/tasks/{task_id}/run", {
    params: { path: { task_id: id } } })), ["analysis-tasks", "analysis-runs", "notifications"]);
  return (
    <section className="av-analysis__table">
      {error ? <div className="av-banner av-banner--danger">{error}</div> : null}
      {/* 定时任务列表接入 QueryState 处理骨架加载与失败重试 */}
      <QueryState query={tasks} isEmpty={(list) => !list.length} emptyText={t("暂无定时任务：新建分析时点“定时”保存")} compact rows={3}>
        {(list) => list.map((task: Task) => (
          <div key={task.id} className="av-row av-task">
            <span className="av-row__main"><span className="av-row__title">{task.title}</span>
              <span className="av-row__meta">{task.question}</span>
              <span className="av-caption" title={task.schedule_cron ?? undefined}>{describeCron(task.schedule_cron)}{task.next_run_at ? t(" · 下次 {{v1}}", { v1: formatTime(task.next_run_at, tz) }) : ""}
                {(task.window as { kind?: string }).kind === "last_month" ? t(" · 上月对比前月") : ""}</span>
              {task.paused_reason ? <span className="av-caption av-claim__bad">{t("已暂停：")}{task.paused_reason}</span> : null}</span>
            <Status value={task.status === "active" ? "active" : "paused"} label={task.status === "active" ? t("启用") : t("暂停")} />
            <span className="av-actions">
              {task.last_run_id ? <Link className="av-btn av-btn--ghost av-btn--sm" to={`/analysis/runs/${task.last_run_id}`}>{t("最近一次")}</Link> : null}
              <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={runNow.isPending}
                onClick={() => runNow.mutate(task.id, { onError: (e) => setError(errorText(e)), onSuccess: () => setError(null) })}>
                <Icon name="play" />{runNow.isPending ? t("运行中…") : t("立即运行")}</button>
              <button type="button" className="av-btn av-btn--ghost av-btn--sm"
                onClick={() => patch.mutate({ id: task.id, status: task.status === "active" ? "paused" : "active" }, { onError: (e) => setError(errorText(e)) })}>
                {task.status === "active" ? t("暂停") : t("启用")}</button>
            </span>
          </div>
        ))}
      </QueryState>
    </section>
  );
}
