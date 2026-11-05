// 指标详情：固定对象标题、版本轨道和口径 / 依赖 / 历史；新建指标与版本编辑。
import { Fragment, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";

import { api, ApiError, unwrap, type Schemas } from "../../api/client";
import {
  errorText, useAction, useAsset, useAssetSearch, useAssetsByIds, useDomains, useJoinPaths, useMetrics, useMetricVersion,
  useTimeZone, useUsers, type Metric, type MetricVersion,
} from "../../api/hooks";
import {
  Banner, CARD_LABEL, Code, Crumbs, Drawer, Empty, Hint, Icon, Pill, QueryState, Select, Status, SuggestInput, Switch, shortName, useMessage,
} from "../../components/ui";
import { formatTime } from "../../format";
import { AssetPicker } from "../../components/AssetPicker";
import { useMe, useRoles } from "../../layout/Shell";
import { t } from "../../i18n";

const ROLE_LABEL: Record<string, string> = { measure: t("度量"), time: t("时间"), dimension: t("维度"), filter: t("过滤"), join_key: t("关联键") };
const METRIC_TABS = [{ id: "definition", label: t("口径") }, { id: "dependency", label: t("依赖") }, { id: "history", label: t("历史") },
  { id: "ai", label: t("模型提示") }] as const;
type MetricTab = typeof METRIC_TABS[number]["id"];
type Spec = Schemas["VersionSpec"];
// 口径的时间与量纲语义（ADR 0014）：默认值与原行为一致
const TIME_FORMATS = [["", t("日期 / 时间类型")], ["yyyymmdd", t("编码 yyyymmdd")], ["yyyy-mm-dd", t("文本 yyyy-mm-dd")], ["yyyymm", t("编码 yyyymm（按月）")]] as const;
const DATA_GRAINS = [["day", t("日")], ["week", t("周")], ["month", t("月")], ["quarter", t("季")], ["year", t("年")]] as const;
const TIME_AGGREGATIONS = [["sum", t("按时间累加")], ["last", t("取期末值")], ["avg", t("取期间平均")]] as const;
const VALUE_KINDS = [["", t("按表达式推断")], ["level", t("水平值")], ["ratio", t("比率")], ["count", t("计数")]] as const;
const POLARITIES = [["", t("不区分")], ["up_good", t("越高越好")], ["down_good", t("越低越好")]] as const;
const optionLabel = (options: readonly (readonly [string, string])[], value: string | null | undefined) =>
  options.find(([v]) => v === (value ?? ""))?.[1] ?? value ?? "—";

/** 量纲与期望方向：基础与派生指标共用（只影响显示与判断，不计入依赖指纹） */
function MeasureSemantics({ valueKind, polarity, onChange }: {
  valueKind: string; polarity: string; onChange: (patch: { value_kind?: string; polarity?: string }) => void;
}) {
  return <div className="av-metric-form__row">
    <label className="av-field"><span className="av-label">{t("量纲")} <Hint text={t("比率显示为百分比；不填时按表达式推断")} /></span>
      <Select value={valueKind} onChange={(v) => onChange({ value_kind: v })}>
        {VALUE_KINDS.map(([v, label]) => <option key={v} value={v}>{label}</option>)}</Select></label>
    <label className="av-field"><span className="av-label">{t("期望方向")} <Hint text={t("决定涨跌的颜色；不区分时用中性色")} /></span>
      <Select value={polarity} onChange={(v) => onChange({ polarity: v })}>
        {POLARITIES.map(([v, label]) => <option key={v} value={v}>{label}</option>)}</Select></label>
  </div>;
}

export function MetricDetail({ metricId }: { metricId: string }) {
  const metrics = useMetrics();
  return <QueryState query={metrics} isEmpty={(data) => !data.some((m) => m.id === metricId)}
    emptyText={t("指标不存在")} emptyAction={<Link className="av-btn av-btn--secondary" to="/catalog/metrics">{t("返回指标目录")}</Link>}>
    {(data) => <MetricContent key={metricId} metric={data.find((m) => m.id === metricId)!} />}
  </QueryState>;
}

function MetricContent({ metric }: { metric: Metric }) {
  const roles = useRoles();
  const canEdit = roles.has("steward") || roles.has("admin");
  const [versionId, setVersionId] = useState<string>();
  const [editing, setEditing] = useState<"new" | "edit" | null>(null);
  const [tab, setTab] = useState<MetricTab>("definition");
  const tabId = useId();
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const current = versionId ?? metric.versions[0]?.id;
  const version = useMetricVersion(current);
  const versions = [...metric.versions].reverse();
  // Saving a draft keeps its id; scope local check results to its server snapshot.
  const versionSnapshot = JSON.stringify(version.data);
  const me = useMe();
  // 模型提示与后端同一规则：口径负责人、所在业务域的负责人 / 维护者、系统管理员
  const domain = useDomains().data?.find((d) => d.id === metric.domain_id);
  const myId = me.data?.id;
  const canEditHints = !!myId && (roles.has("admin") || myId === metric.owner.id
    || !!domain?.members.some((m) => m.user_id === myId && (m.role === "maintainer" || m.role === "owner")));

  return <div className="av-metric-detail">
    <header className="av-metric-detail__header">
      <Crumbs back={{ label: t("指标"), to: "/catalog/metrics" }}
        items={[<span className="av-mono">{metric.code}</span>, <>{t("负责人")} {metric.owner.display_name}</>]} />
      <div className="av-pagehd">
        <div className="av-pagehd__title"><h1>{metric.name}</h1>
          {version.isSuccess ? <Status value={version.data.status} /> : null}
          {metric.aliases.map((a) => <span key={a} className="av-tag">{a}</span>)}
        </div>
        <div className="av-actions av-metric-detail__actions">
          <Link className="av-btn av-btn--secondary av-btn--sm"
            to={`/ask?${new URLSearchParams({ metric_id: metric.id, ...(current ? { version_id: current } : {}) })}`}>{t("问数")}</Link>
          {canEdit ? <button type="button" className="av-btn av-btn--primary av-btn--sm"
            disabled={!!current && !version.isSuccess} onClick={() => setEditing("new")}><Icon name="plus" small />{t("新建版本")}</button> : null}
        </div>
      </div>
      {versions.length ? <div className="av-vtrack av-metric-detail__versions" aria-label={t("版本")}>
        {versions.map((v, i) => <Fragment key={v.id}>
          {i > 0 ? <span className="av-vtrack__line" /> : null}
          <button type="button" className={`av-vtrack__step${v.id === current ? " is-current" : ""}`}
            aria-pressed={v.id === current} onClick={() => setVersionId(v.id)}>V{v.version_no} <Status value={v.status} /></button>
        </Fragment>)}
      </div> : null}
    </header>
    <div className="av-tabs av-metric-detail__tabs" role="tablist" aria-label={t("指标详情")}>
      {METRIC_TABS.map((item, i) => <button key={item.id} type="button" className="av-tab" role="tab"
        id={`${tabId}-${item.id}`} aria-controls={`${tabId}-panel`} aria-selected={tab === item.id} tabIndex={tab === item.id ? 0 : -1}
        ref={(node) => { tabRefs.current[i] = node; }} onClick={() => setTab(item.id)}
        onKeyDown={(event) => {
          const next = event.key === "ArrowRight" ? (i + 1) % METRIC_TABS.length
            : event.key === "ArrowLeft" ? (i + METRIC_TABS.length - 1) % METRIC_TABS.length
            : event.key === "Home" ? 0 : event.key === "End" ? METRIC_TABS.length - 1 : -1;
          if (next < 0) return;
          event.preventDefault();
          setTab(METRIC_TABS[next].id);
          tabRefs.current[next]?.focus();
        }}>{item.label}</button>)}
    </div>
    <div className="av-metric-detail__body" role="tabpanel" id={`${tabId}-panel`} aria-labelledby={`${tabId}-${tab}`} tabIndex={0}>
      {tab === "ai" ? <AiContextPanel key={metric.id} metric={metric} canEdit={canEditHints} />
        : !current ? <Empty text={t("还没有版本")} compact action={canEdit
        ? <button type="button" className="av-btn av-btn--primary" onClick={() => setEditing("new")}>{t("新建版本")}</button> : undefined} />
        : version.isError && version.error instanceof ApiError && version.error.status === 404
          ? <Empty text={t("版本不存在")} compact action={<button type="button" className="av-btn av-btn--secondary" onClick={() => setVersionId(undefined)}>{t("返回当前版本")}</button>} />
          : <QueryState query={version}>{(v) => <VersionBody key={versionSnapshot} v={v} tab={tab} canEdit={canEdit} onEdit={() => setEditing("edit")} />}</QueryState>}
    </div>
    {editing && canEdit ? <VersionEditor metricId={metric.id} base={version.data} mode={editing}
      onClose={() => setEditing(null)} onSaved={(id) => { setEditing(null); setVersionId(id); setTab("definition"); }} /> : null}
  </div>;
}

type AiContext = Schemas["MetricAIContextOut"];
const AI_LIMITS = { instructions: 2000, synonyms: 30, synonym: 64, examples: 20, example: 200 };

/** 给模型的提示（ADR 0017 §3）：适用说明、同义词、示例问法。只影响问数检索与澄清，不进入 SQL、不生成新版本。 */
function AiContextPanel({ metric, canEdit }: { metric: Metric; canEdit: boolean }) {
  const ctx: AiContext = metric.ai_context ?? { instructions: null, synonyms: [], examples: [] };
  const [editing, setEditing] = useState(false);
  const [message, setMessage] = useMessage();
  const empty = !ctx.instructions && !ctx.synonyms.length && !ctx.examples.length;
  return <section className="av-panel">
    <div className="av-panel__hd av-metric-panel__header">
      <h2 className="av-panel__title">{t("给模型的提示")} <Hint text={t("问数时用来找到这个口径、判断是否适用；不进入 SQL，修改不生成新版本，会记入审计")} /></h2>
      {canEdit && !editing ? <div className="av-actions av-metric-detail__actions">
        <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => setEditing(true)}>{empty ? t("添加提示") : t("编辑")}</button>
      </div> : null}
    </div>
    <div className="av-panel__bd">
      <Banner message={message} />
      {editing ? <AiContextForm metricId={metric.id} value={ctx} onCancel={() => setEditing(false)}
        onSaved={() => { setEditing(false); setMessage({ ok: true, text: t("已保存") }); }} />
        : empty ? <Empty compact text={t("还没有提示。写下适用范围、常见叫法和典型问法，问数更容易选对口径")} />
        : <dl className="av-kv av-metric-detail__kv">
          <dt>{t("适用说明")}</dt><dd className="av-aictx__text">{ctx.instructions || "—"}</dd>
          <dt>{t("同义词")}</dt><dd className="av-metric-detail__tags">{ctx.synonyms.length ? ctx.synonyms.map((x) => <span key={x} className="av-tag">{x}</span>) : "—"}</dd>
          <dt>{t("示例问法")}</dt><dd>{ctx.examples.length ? <ul className="av-aictx__examples">{ctx.examples.map((x) => <li key={x}>{x}</li>)}</ul> : "—"}</dd>
        </dl>}
    </div>
  </section>;
}

function AiContextForm({ metricId, value, onCancel, onSaved }: {
  metricId: string; value: AiContext; onCancel: () => void; onSaved: () => void;
}) {
  const id = useId();
  const [instructions, setInstructions] = useState(value.instructions ?? "");
  const [synonyms, setSynonyms] = useState<string[]>(value.synonyms);
  const [draft, setDraft] = useState("");
  const [examples, setExamples] = useState(value.examples.join("\n"));
  const [message, setMessage] = useMessage();
  const exampleList = [...new Set(examples.split(/\r?\n/).map((x) => x.trim()).filter(Boolean))];
  const splitSynonyms = (raw: string) => raw.split(/[,，、]/).map((x) => x.trim().slice(0, AI_LIMITS.synonym)).filter(Boolean);
  // 输入框里还没按回车的同义词也算进去：校验和提交都用同一份最终列表
  const finalSynonyms = [...new Set([...synonyms, ...splitSynonyms(draft)])];
  const errors = [
    finalSynonyms.length > AI_LIMITS.synonyms ? t("同义词最多 {{n}} 个", { n: AI_LIMITS.synonyms }) : "",
    exampleList.length > AI_LIMITS.examples ? t("示例问法最多 {{n}} 条", { n: AI_LIMITS.examples }) : "",
    exampleList.some((x) => x.length > AI_LIMITS.example) ? t("每条示例问法最多 {{n}} 字", { n: AI_LIMITS.example }) : "",
  ].filter(Boolean);
  const save = useAction((body: AiContext) => unwrap(api.PUT("/api/metrics/{metric_id}/ai-context", {
    params: { path: { metric_id: metricId } }, body })), ["metrics"]);
  const addSynonym = (raw: string) => {
    const items = splitSynonyms(raw);
    if (items.length) setSynonyms((list) => [...new Set([...list, ...items])]);
    setDraft("");
  };
  return <form className="av-aictx__form" onSubmit={(e) => {
    e.preventDefault();
    if (errors.length || save.isPending) return;
    setSynonyms(finalSynonyms);
    setDraft("");
    save.mutate({ instructions: instructions.trim() || null, synonyms: finalSynonyms, examples: exampleList },
      { onSuccess: onSaved, onError: (err) => setMessage({ ok: false, text: errorText(err) }) });
  }}>
    <label className="av-field" htmlFor={`${id}-instructions`}><span className="av-label">{t("适用说明")} <Hint text={t("什么时候该用、什么时候不该用；拿不准时模型会据此向提问人确认")} /></span>
      <textarea id={`${id}-instructions`} className="av-textarea" rows={4} maxLength={AI_LIMITS.instructions} value={instructions}
        onChange={(e) => setInstructions(e.target.value)} /></label>
    <div className="av-field"><label className="av-label" htmlFor={`${id}-synonyms`}>{t("同义词")} <Hint text={t("回车或逗号添加；与别名一起参与检索")} /></label>
      {synonyms.length ? <div className="av-metric-detail__tags">{synonyms.map((x) => (
        <button type="button" key={x} className="av-btn av-btn--secondary av-btn--sm" aria-label={t("移除同义词 {{name}}", { name: x })}
          onClick={() => setSynonyms(synonyms.filter((y) => y !== x))}>{x}<Icon name="close" small /></button>))}</div> : null}
      <input id={`${id}-synonyms`} className="av-input" value={draft} maxLength={AI_LIMITS.synonym * 4}
        onChange={(e) => { if (/[,，、]/.test(e.target.value)) addSynonym(e.target.value); else setDraft(e.target.value); }}
        onKeyDown={(e) => { if (e.key === "Enter" && !e.nativeEvent.isComposing && e.keyCode !== 229) { e.preventDefault(); addSynonym(draft); } }} /></div>
    <label className="av-field" htmlFor={`${id}-examples`}><span className="av-label">{t("示例问法")} <Hint text={t("每行一条，写提问人通常会怎么问")} /></span>
      <textarea id={`${id}-examples`} className="av-textarea" rows={4} value={examples} onChange={(e) => setExamples(e.target.value)} /></label>
    {errors.length ? <div className="av-banner av-banner--danger">{errors.join(t("；"))}</div> : null}
    <Banner message={message} />
    <div className="av-actions av-aictx__actions">
      <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={save.isPending} onClick={onCancel}>{t("取消")}</button>
      <button type="submit" className="av-btn av-btn--primary av-btn--sm" disabled={save.isPending || errors.length > 0}>{save.isPending ? t("保存中…") : t("保存")}</button>
    </div>
  </form>;
}

function VersionBody({ v, tab, canEdit, onEdit }: { v: MetricVersion; tab: MetricTab; canEdit: boolean; onEdit: () => void }) {
  const tz = useTimeZone();
  const me = useMe();
  const roles = useRoles();
  const users = useUsers();
  const name = (id?: string | null) => id ? users.data?.find((u) => u.id === id)?.display_name ?? id : t("系统");
  const [message, setMessage] = useMessage();
  const [note, setNote] = useState("");
  const path = { params: { path: { version_id: v.id } } };
  const done = (text: string) => ({ onSuccess: () => setMessage({ ok: true, text }), onError: (e: unknown) => setMessage({ ok: false, text: errorText(e) }) });
  const invalidate = ["metrics", "metric-version"];
  const submit = useAction(() => unwrap(api.POST("/api/metric-versions/{version_id}/submit", path)), invalidate);
  const approve = useAction(() => unwrap(api.POST("/api/metric-versions/{version_id}/approve", { ...path, body: { note: note.trim() || null } })), invalidate);
  const reject = useAction(() => unwrap(api.POST("/api/metric-versions/{version_id}/reject", { ...path, body: { note: note.trim() } })), invalidate);
  const check = useAction(() => unwrap(api.POST("/api/metric-versions/{version_id}/check", path)), []);
  const selfReview = me.data && (me.data.id === v.submitted_by || me.data.id === v.created_by);
  const canReview = me.isSuccess && !selfReview && (roles.has("reviewer") || roles.has("admin"));
  const reviewPending = approve.isPending || reject.isPending;
  const pc = (check.data ?? v.publish_check) as Schemas["PublishCheckOut"] | Record<string, never>;

  return <>
    <Banner message={message} />
    {tab === "definition" ? <>
      <section className="av-panel">
        <div className="av-panel__hd av-metric-panel__header">
          <h2 className="av-panel__title">{t("口径 V")}{v.version_no} <Status value={v.status} />{v.kind === "derived" ? <Pill>{t("派生")}</Pill> : null}</h2>
          {canEdit && (v.status === "draft" || v.status === "rejected") ? <div className="av-actions av-metric-detail__actions">
            <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={submit.isPending || check.isPending} onClick={onEdit}>{t("编辑")}</button>
            <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={check.isPending || submit.isPending}
              aria-busy={check.isPending} onClick={() => { if (!check.isPending) check.mutate(undefined, done(t("检查完成"))); }}>{check.isPending ? t("检查中…") : t("发布前检查")}</button>
            <button type="button" className="av-btn av-btn--primary av-btn--sm" disabled={submit.isPending || check.isPending}
              aria-busy={submit.isPending} onClick={() => { if (!submit.isPending) submit.mutate(undefined, done(t("已提交复核"))); }}>{submit.isPending ? t("提交中…") : t("提交复核")}</button>
          </div> : null}
        </div>
        <div className="av-panel__bd">
          {v.status_reason ? <p className="av-caption av-metric-detail__reason">{v.status_reason}</p> : null}
          <dl className="av-kv av-metric-detail__kv">
            <dt>{t("说明")}</dt><dd>{v.description}</dd>
            {v.kind === "derived" ? <>
              <dt>{t("公式")}</dt><dd><pre className="av-code av-metric-derived__formula">{v.expression}</pre></dd>
              <dt>{t("锁定组成版本")}</dt><dd><DerivedComponents components={v.components ?? []} /></dd>
              <dt>{t("共有维度")}</dt><dd className="av-metric-detail__tags">{v.dimensions.length ? v.dimensions.map((d) => <span key={String(d.name)} className="av-tag">{String(d.label ?? d.name)}</span>) : "—"}</dd>
            </> : <>
            <dt>{t("基础资产")}</dt><dd className="av-mono">{v.base_asset ? <Link to={`/catalog/assets/${String(v.base_asset.id)}`}>{String(v.base_asset.qualified)}</Link> : "—"}</dd>
            <dt>{t("表达式")}</dt><dd><Code sql={v.expression} raw /></dd>
            <dt>{t("时间字段")}</dt><dd className="av-mono">{v.time_column} <span className="av-tag">{v.timezone}</span></dd>
            <dt>{t("时间语义")}</dt><dd className="av-metric-detail__tags">
              <span className="av-tag">{optionLabel(TIME_FORMATS, v.time_format)}</span>
              <span className="av-tag">{t("粒度 {{grain}}", { grain: optionLabel(DATA_GRAINS, v.data_grain ?? "day") })}</span>
              <span className="av-tag">{optionLabel(TIME_AGGREGATIONS, v.time_aggregation ?? "sum")}</span></dd>
            <dt>{t("过滤")}</dt><dd className="av-mono">{v.filters || "—"}</dd>
            <dt>{t("允许维度")}</dt><dd className="av-metric-detail__tags">{v.dimensions.length ? v.dimensions.map((d) => <span key={String(d.name)} className="av-tag">{String(d.label ?? d.name)} · {String(d.column)}</span>) : "—"}</dd>
            <dt>{t("统计粒度")}</dt><dd className="av-mono">{v.grain.join(" × ") || "—"}</dd>
            {v.joins.length ? <><dt>{t("关联")}</dt><dd className="av-metric-detail__tags">{v.joins.map((j) => <span key={String(j.alias)} className="av-tag">{String(j.from ?? "b")} → {String(j.alias)}{j.pre_aggregate ? t(" · 先聚合") : ""}</span>)}</dd></> : null}
            {v.dedupe_basis ? <><dt>{t("防重复依据")}</dt><dd>{v.dedupe_basis}</dd></> : null}
            </>}
            <dt>{t("单位")}</dt><dd>{v.unit ? <>{v.unit}{v.unit_scale && v.unit_scale !== 1 ? <span className="av-tag av-mono">×{v.unit_scale}</span> : null}</> : <Pill tone="warn">{t("单位待补")}</Pill>}</dd>
            <dt>{t("量纲 · 方向")}</dt><dd>{optionLabel(VALUE_KINDS, v.value_kind)} · {optionLabel(POLARITIES, v.polarity)}</dd>
          </dl>
        </div>
      </section>
      {v.status === "pending_review" ? <section className="av-panel" aria-busy={reviewPending}>
        <div className="av-panel__hd"><h2 className="av-panel__title">{t("复核")} <Hint text={t("提交人、创建人不能复核同一版本")} /></h2></div>
        <div className="av-panel__bd av-metric-detail__stack">
          <label className="av-field"><span className="av-label">{t("意见（驳回时必填）")}</span>
            <textarea className="av-textarea" rows={2} value={note} onChange={(e) => setNote(e.target.value)} /></label>
          <div className="av-actions av-metric-detail__review-actions">
            {selfReview ? <span className="av-status av-status--warn">{t("不能复核自己创建或提交的版本")}</span> : null}
            <button type="button" className="av-btn av-btn--secondary" disabled={!note.trim() || !canReview || reviewPending}
              onClick={() => { if (!reviewPending) reject.mutate(undefined, done(t("已驳回"))); }}>{reject.isPending ? t("驳回中…") : t("驳回")}</button>
            <button type="button" className="av-btn av-btn--primary" disabled={!canReview || reviewPending}
              onClick={() => { if (!reviewPending) approve.mutate(undefined, done(t("已批准并发布"))); }}>{approve.isPending ? t("批准中…") : t("批准发布")}</button>
          </div>
        </div>
      </section> : null}
      {pc && "ok" in pc ? <section className="av-panel">
        <div className={`av-panel__hd av-metric-panel__header${!pc.blockers.length && !pc.warnings.length && !pc.joins.length ? " av-metric-check--compact" : ""}`}>
          <h2 className="av-panel__title">{t("发布前检查")} {pc.ok ? <Pill tone="ok">{t("通过")}</Pill> : <Pill tone="danger">{t("未通过")}</Pill>}</h2>
          <span className="av-caption av-mono">{formatTime(pc.checked_at, tz)}</span>
        </div>
        {pc.blockers.length || pc.warnings.length || pc.joins.length ? <div className="av-panel__bd av-metric-detail__stack">
          {pc.blockers.map((b) => <div key={b} className="av-banner av-banner--danger">{b}</div>)}
          {pc.warnings.map((w) => <span key={w} className="av-status av-status--warn">{w}</span>)}
          {pc.joins.length ? <div className="av-table-wrap"><table className="av-table av-metric-detail__table">
            <thead><tr><th>{t("关联")}</th><th>{t("路径")}</th><th>{t("基数")}</th><th>{t("依据")}</th></tr></thead>
            <tbody>{pc.joins.map((j) => <tr key={String(j.alias)}>
              <td className="av-mono">{String(j.alias)}</td><td className="av-mono">{String(j.path)}</td>
              <td><span className={`av-tag${["1n", "nn", "unknown"].includes(String(j.cardinality)) ? " av-edge-tag--warn" : ""}`}>{CARD_LABEL[String(j.cardinality)]}</span></td>
              <td className="av-caption">{j.pre_aggregate ? t("右侧先按关联键聚合") : grainText(j.basis as Record<string, unknown>)}</td>
            </tr>)}</tbody>
          </table></div> : null}
        </div> : null}
      </section> : null}
    </> : null}
    {tab === "dependency" && v.kind === "derived" ? <section className="av-panel">
      <div className="av-panel__hd"><h2 className="av-panel__title">{t("锁定组成版本")} <Hint text={t("提交时锁定组成指标版本")} /></h2></div>
      <div className="av-panel__bd"><DerivedComponents components={v.components ?? []} /></div>
    </section> : null}
    {tab === "dependency" && v.kind !== "derived" ? <section className="av-panel">
      <div className="av-panel__hd"><h2 className="av-panel__title">{t("依赖与指纹")} <Hint text={t("提交时记录依赖字段指纹；指纹变化后需重新复核")} /></h2></div>
      {v.dependency.length ? <div className="av-table-wrap"><table className="av-table av-metric-detail__table">
        <thead><tr><th>{t("字段")}</th><th>{t("用途")}</th><th>{t("提交时指纹")}</th><th>{t("当前")}</th></tr></thead>
        <tbody>{v.dependency.map((d) => {
          const drift = (v.drift ?? []).find((x) => x.asset_id === d.asset_id && x.column === d.column);
          return <tr key={`${String(d.asset_id)}${String(d.column)}`}>
            <td className="av-mono">{shortName(String(d.qualified))}.{String(d.column)}</td>
            <td>{ROLE_LABEL[String(d.role)] ?? String(d.role)}</td>
            <td className="av-mono av-muted">{String(d.fingerprint ?? "").slice(0, 10) || "—"}</td>
            <td>{drift ? <Status value={String(drift.current_status) === "ok" ? "affected" : String(drift.current_status)} label={String(drift.current_status) === "ok" ? t("已变化") : undefined} /> : <Status value="ok" label={t("一致")} />}</td>
          </tr>;
        })}</tbody>
      </table></div> : <Empty text={t("暂无依赖记录")} compact />}
    </section> : null}
    {tab === "history" ? <section className="av-panel">
      <div className="av-panel__hd"><h2 className="av-panel__title">{t("状态历史")}</h2></div>
      {v.transitions?.length ? <div className="av-panel__bd"><ol className="av-steps">
        {[...v.transitions].reverse().map((step, i) => <li key={i} className={`av-step${step.to === "published" ? " is-ok" : ["affected", "retired", "rejected"].includes(step.to) ? " is-blocked" : ""}`}>
          <span className="av-step__dot" /><span className="av-step__body">
            <span className="av-step__title"><Status value={step.to} /> {name(step.actor_id)}</span>
            <span className="av-step__meta">{step.reason} · {formatTime(step.at, tz)}</span>
          </span>
        </li>)}
      </ol></div> : <Empty text={t("暂无状态历史")} compact />}
    </section> : null}
  </>;
}

function DerivedComponents({ components }: { components: NonNullable<MetricVersion["components"]> }) {
  const metrics = useMetrics();
  if (!components.length) return <span className="av-caption">{t("提交后锁定组成版本")}</span>;
  return <ul className="av-metric-derived__components">
    {components.map((component, index) => {
      const code = String(component.metric_code ?? "");
      const metric = metrics.data?.find((item) => item.code === code);
      const metricId = typeof component.metric_id === "string" ? component.metric_id : metric?.id;
      const label = `${String(component.metric_name ?? metric?.name ?? code)} · V${String(component.version_no ?? "—")}`;
      return <li key={String(component.version_id ?? `${code}-${index}`)}>
        {metricId ? <Link to={`/catalog/metrics/${metricId}`}>{label}</Link> : <span>{label}</span>}
        <span className="av-caption av-mono">{code}</span>
      </li>;
    })}
  </ul>;
}

function grainText(basis: Record<string, unknown> | undefined): string {
  const right = basis?.right_grain as { columns?: string[]; origin?: string } | null | undefined;
  const left = basis?.left_grain as { columns?: string[] } | null | undefined;
  if (right) return t("右侧粒度 {{v1}}（{{v2}}）", { v1: right.columns?.join(", "), v2: right.origin === "linked" ? t("经同一张表继承") : right.origin });
  if (left) return t("左侧粒度 {{v1}}，右侧不唯一", { v1: left.columns?.join(", ") });
  return t("两侧粒度未知");
}

export function NewMetric({ onClose, onCreated }: { onClose: () => void; onCreated: (id: string) => void }) {
  const roles = useRoles();
  const canEdit = roles.has("steward") || roles.has("admin");
  const domains = useDomains();
  const [form, setForm] = useState({ code: "", name: "", domain_id: "", aliases: "" });
  const domainId = domains.data?.some((d) => d.id === form.domain_id) ? form.domain_id : domains.data?.[0]?.id || "";
  const [message, setMessage] = useMessage();
  const create = useAction(() => unwrap(api.POST("/api/metrics", { body: {
    code: form.code.trim(), name: form.name.trim(), domain_id: domainId,
    aliases: form.aliases.split(/[,，]/).map((s) => s.trim()).filter(Boolean), owner_id: null,
  } })), ["metrics"]);
  return <Drawer title={t("新建指标")} width={480} onClose={onClose} footer={<>
    <button type="button" className="av-btn av-btn--secondary" onClick={onClose}>{t("取消")}</button>
    <button type="submit" form="new-metric-form" className="av-btn av-btn--primary" aria-busy={create.isPending}
      disabled={!canEdit || !form.code.trim() || !form.name.trim() || !domainId || !domains.isSuccess || create.isPending}>{create.isPending ? t("创建中…") : t("创建")}</button>
  </>}>
    <form id="new-metric-form" className="av-metric-form" onSubmit={(event) => {
      event.preventDefault();
      if (!canEdit || create.isPending || !domainId || !form.code.trim() || !form.name.trim()) return;
      create.mutate(undefined, { onSuccess: (m) => onCreated(m.id), onError: (e) => setMessage({ ok: false, text: errorText(e) }) });
    }}>
      <label className="av-field"><span className="av-label">{t("名称")}</span><input required className="av-input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
      <label className="av-field"><span className="av-label">{t("编码")} <Hint text={t("小写字母、数字、下划线")} /></span><input required pattern="[a-z0-9_]+" className="av-input av-mono" value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} /></label>
      <QueryState query={domains} compact isEmpty={(data) => !data.length} emptyText={t("暂无可用业务域")}>
        {(data) => <label className="av-field"><span className="av-label">{t("业务域")} <Hint text={t("负责人默认为该域负责人")} /></span>
          <Select value={domainId} onChange={(v) => setForm({ ...form, domain_id: v })}>
            {data.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
          </Select></label>}
      </QueryState>
      <label className="av-field"><span className="av-label">{t("别名")} <Hint text={t("逗号分隔")} /></span><input className="av-input" value={form.aliases} onChange={(e) => setForm({ ...form, aliases: e.target.value })} /></label>
      <Banner message={message} />
    </form>
  </Drawer>;
}

type Dim = { name: string; column: string; label: string };
type JoinRow = { alias: string; join_path_id: string; from: string; pre_aggregate: boolean };
type VersionEditorProps = {
  metricId: string; base?: MetricVersion; mode: "new" | "edit"; onClose: () => void; onSaved: (id: string) => void;
};

function VersionEditor(props: VersionEditorProps) {
  const [kind, setKind] = useState(props.base?.kind === "derived" ? "derived" : "base");
  const kindControl = props.mode === "new" ? <label className="av-field"><span className="av-label">{t("版本类型")}</span>
    <Select label={t("版本类型")} value={kind} onChange={setKind}>
      <option value="base">{t("基础")}</option><option value="derived">{t("派生")}</option>
    </Select>
  </label> : null;
  const base = (props.base?.kind ?? "base") === kind ? props.base : undefined;
  return kind === "derived"
    ? <DerivedVersionEditor {...props} base={base} kindControl={kindControl} />
    : <BaseVersionEditor {...props} base={base} kindControl={kindControl} />;
}

function DerivedVersionEditor({ metricId, base, mode, onClose, onSaved, kindControl }: VersionEditorProps & { kindControl: ReactNode }) {
  const queryClient = useQueryClient();
  const roles = useRoles();
  const canEdit = roles.has("steward") || roles.has("admin");
  const metrics = useMetrics();
  const editorId = useId();
  const [description, setDescription] = useState(base?.description ?? "");
  const [expression, setExpression] = useState(base?.expression ?? "");
  const [unit, setUnit] = useState(base?.unit ?? "");
  const [unitScale, setUnitScale] = useState(String(base?.unit_scale ?? 1));
  const [measure, setMeasure] = useState({ value_kind: String(base?.value_kind ?? ""), polarity: String(base?.polarity ?? "") });
  const [message, setMessage] = useMessage();
  // Consume Python numeric literals first, including digit separators and base prefixes.
  const codes = [...new Set((expression.match(/0[xX](?:_?[\da-fA-F])+|0[oO](?:_?[0-7])+|0[bB](?:_?[01])+|(?:\d(?:_?\d)*(?:\.(?:\d(?:_?\d)*)?)?|\.\d(?:_?\d)*)(?:[eE][+-]?\d(?:_?\d)*)?|[A-Za-z_][A-Za-z0-9_]*/g) ?? [])
    .filter((token) => /^[A-Za-z_]/.test(token)))];
  const body = { kind: "derived", description: description.trim(), expression: expression.trim(), unit: unit.trim() || null,
    unit_scale: Math.max(1, Math.round(Number(unitScale) || 1)),
    value_kind: (measure.value_kind || null) as Spec["value_kind"], polarity: (measure.polarity || null) as Spec["polarity"] } satisfies Spec;
  const save = useAction(() => mode === "edit" && base
    ? unwrap(api.PUT("/api/metric-versions/{version_id}", { params: { path: { version_id: base.id } }, body }))
    : unwrap(api.POST("/api/metrics/{metric_id}/versions", { params: { path: { metric_id: metricId } }, body })), ["metrics", "metric-version"]);
  const valid = canEdit && !!description.trim() && !!expression.trim();

  return <Drawer title={mode === "edit" ? t("编辑派生草稿 V{{version_no}}", { version_no: base?.version_no }) : t("新建版本（草稿）")} width={640} onClose={onClose} footer={<>
    <button type="button" className="av-btn av-btn--secondary" onClick={onClose}>{t("取消")}</button>
    <button type="submit" form={`${editorId}-form`} className="av-btn av-btn--primary" disabled={!valid || save.isPending}
      aria-busy={save.isPending}>{save.isPending ? t("保存中…") : t("保存草稿")}</button>
  </>}>
    <form id={`${editorId}-form`} className="av-metric-form" onSubmit={(event) => {
      event.preventDefault();
      if (!valid || save.isPending) return;
      save.mutate(undefined, {
        onSuccess: (version) => {
          queryClient.setQueryData(["metric-version", version.id], version);
          onSaved(version.id);
        },
        onError: (error) => setMessage({ ok: false, text: errorText(error) }),
      });
    }}>
      {kindControl}
      <label className="av-field"><span className="av-label">{t("说明")}</span>
        <textarea required className="av-textarea" rows={2} value={description} onChange={(event) => setDescription(event.target.value)} /></label>
      <label className="av-field"><span className="av-label">{t("公式")} <Hint text={t("使用指标编码、数字、加减乘除和括号")} /></span>
        <textarea required className="av-textarea av-mono av-metric-derived__formula-input" rows={3} placeholder="metric_a / metric_b"
          aria-describedby={`${editorId}-references`} spellCheck={false} value={expression} onChange={(event) => setExpression(event.target.value)} /></label>
      <div id={`${editorId}-references`} className="av-metric-derived__references" role="status" aria-live="polite">
        {metrics.isPending ? <span className="av-help">{t("指标目录加载中…")}</span>
          : metrics.isError ? <span className="av-help">{t("指标目录暂不可用，保存时由后端校验")}</span>
          : codes.length ? <div className="av-metric-detail__tags">{codes.map((code) => {
            const metric = metrics.data.find((item) => item.code === code);
            return <Pill key={code} tone={metric ? "ok" : "danger"}>{code} · {metric ? t("已存在") : t("不存在")}</Pill>;
          })}</div> : <span className="av-help">{t("输入公式以查看引用指标")}</span>}
      </div>
      <div className="av-metric-form__row">
        <label className="av-field"><span className="av-label">{t("单位")} <Hint text={t("报告里的数字由系统带上单位，如 元、件、吨、kWh；比率类可不填。缺单位的口径会列为待补")} /></span>
          <input className="av-input" maxLength={16} placeholder={t("如 件、kWh")} value={unit} onChange={(e) => setUnit(e.target.value)} /></label>
        <label className="av-field"><span className="av-label">{t("列的倍数")} <Hint text={t("列本身已按万计（如“万元”“万件”）时填 10000，默认 1")} /></span>
          <input className="av-input av-mono" type="number" min={1} step={1} value={unitScale} onChange={(e) => setUnitScale(e.target.value)} /></label>
      </div>
      <MeasureSemantics valueKind={measure.value_kind} polarity={measure.polarity} onChange={(p) => setMeasure({ ...measure, ...p })} />
      <Banner message={message} />
    </form>
  </Drawer>;
}

function BaseVersionEditor({ metricId, base, mode, onClose, onSaved, kindControl }: VersionEditorProps & { kindControl: ReactNode }) {
  const queryClient = useQueryClient();
  const roles = useRoles();
  const canEdit = roles.has("steward") || roles.has("admin");
  const paths = useJoinPaths();
  // 基础资产只能是可查询数据源上的资产：默认选第一个、核对当前选择是否仍可用，都按服务端过滤
  const firstQueryable = useAssetSearch({ queryable: true, limit: 1 });
  const [form, setForm] = useState({
    description: base?.description ?? "", base_asset_id: String(base?.base_asset?.id ?? ""), expression: base?.expression ?? "",
    // 时区留空表示沿用系统业务时区（设置加载前后可能不同，提交时再取）
    time_column: base?.time_column ?? "", timezone: base?.timezone ?? "", filters: base?.filters ?? "",
    grain: (base?.grain ?? []).join(", "), dedupe_basis: base?.dedupe_basis ?? "",
    time_format: String(base?.time_format ?? ""), data_grain: String(base?.data_grain ?? "day"), time_aggregation: String(base?.time_aggregation ?? "sum"),
    value_kind: String(base?.value_kind ?? ""), polarity: String(base?.polarity ?? ""),
  });
  const businessTz = useTimeZone();
  const [unit, setUnit] = useState(base?.unit ?? "");
  const [unitScale, setUnitScale] = useState(String(base?.unit_scale ?? 1));
  const [dims, setDims] = useState<Dim[]>((base?.dimensions ?? []).map((d) => ({ name: String(d.name), column: String(d.column), label: String(d.label ?? "") })));
  const [joins, setJoins] = useState<JoinRow[]>((base?.joins ?? []).map((j) => ({ alias: String(j.alias), join_path_id: String(j.join_path_id), from: String(j.from ?? "b"), pre_aggregate: !!j.pre_aggregate })));
  const [attempted, setAttempted] = useState(false);
  const editorId = useId();
  const firstAssetId = firstQueryable.data?.[0]?.id;
  const asset = useAsset(form.base_asset_id || undefined);
  const [message, setMessage] = useMessage();
  useEffect(() => {
    if (!form.base_asset_id && firstAssetId) setForm((f) => ({ ...f, base_asset_id: firstAssetId }));
  }, [firstAssetId, form.base_asset_id]);
  const chosen = useAssetsByIds(form.base_asset_id ? [form.base_asset_id] : [], { queryable: true });
  const baseValid = !!chosen.data?.some((a) => a.id === form.base_asset_id);
  const dimsValid = dims.every((d) => d.name.trim() && d.column.trim());
  const joinsValid = joins.every((j) => j.alias.trim() && j.from.trim() && paths.data?.some((p) => p.id === j.join_path_id));
  const valid = canEdit && baseValid && !!form.description.trim() && !!form.expression.trim() && !!form.time_column.trim() && dimsValid && joinsValid;
  const body = {
    ...form, description: form.description.trim(), expression: form.expression.trim(), time_column: form.time_column.trim(),
    timezone: form.timezone.trim() || businessTz, time_format: (form.time_format || null) as Spec["time_format"],
    data_grain: form.data_grain as Spec["data_grain"], time_aggregation: form.time_aggregation as Spec["time_aggregation"],
    value_kind: (form.value_kind || null) as Spec["value_kind"], polarity: (form.polarity || null) as Spec["polarity"],
    filters: form.filters.trim() || null, dedupe_basis: form.dedupe_basis.trim() || null,
    unit: unit.trim() || null, unit_scale: Math.max(1, Math.round(Number(unitScale) || 1)),
    grain: form.grain.split(/[,，×]/).map((s) => s.trim()).filter(Boolean),
    dimensions: dims.map((d) => ({ name: d.name.trim(), column: d.column.trim(), label: d.label.trim() || null })),
    joins: joins.map((j) => ({ ...j, alias: j.alias.trim(), from: j.from.trim() })),
  };
  const save = useAction(() => mode === "edit" && base
    ? unwrap(api.PUT("/api/metric-versions/{version_id}", { params: { path: { version_id: base.id } }, body }))
    : unwrap(api.POST("/api/metrics/{metric_id}/versions", { params: { path: { metric_id: metricId } }, body })), ["metrics", "metric-version"]);
  const columns = asset.data?.columns.map((c) => c.name) ?? [];
  const updateDim = (index: number, patch: Partial<Dim>) => setDims((rows) => rows.map((d, i) => i === index ? { ...d, ...patch } : d));
  const updateJoin = (index: number, patch: Partial<JoinRow>) => setJoins((rows) => rows.map((j, i) => i === index ? { ...j, ...patch } : j));

  return <Drawer title={mode === "edit" ? t("编辑草稿 V{{version_no}}", { version_no: base?.version_no }) : t("新建版本（草稿）")} width={640} onClose={onClose} footer={<>
    <button type="button" className="av-btn av-btn--secondary" onClick={onClose}>{t("取消")}</button>
    <button type="submit" form={`${editorId}-form`} className="av-btn av-btn--primary" disabled={!canEdit || save.isPending || !firstQueryable.isSuccess}
      aria-busy={save.isPending}>{save.isPending ? t("保存中…") : t("保存草稿")}</button>
  </>}>
    <form id={`${editorId}-form`} className="av-metric-form" onSubmit={(event) => {
      event.preventDefault();
      setAttempted(true);
      if (!valid || save.isPending) return;
      save.mutate(undefined, {
        onSuccess: (v) => {
          queryClient.setQueryData(["metric-version", v.id], v);
          onSaved(v.id);
        },
        onError: (e) => setMessage({ ok: false, text: errorText(e) }),
      });
    }}>
      {kindControl}
      <label className="av-field"><span className="av-label">{t("说明")}</span><textarea required className="av-textarea" rows={2} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></label>
      <QueryState query={firstQueryable} rows={1} compact isEmpty={(items) => !items.length && !form.base_asset_id} emptyText={t("暂无支持查询的基础资产")}>
        {() => <label className="av-field"><span className="av-label">{t("基础资产")}</span>
          <AssetPicker queryable value={form.base_asset_id} invalid={attempted && !baseValid} label={t("基础资产")}
            placeholder={t("请选择基础资产")} onChange={(v) => setForm({ ...form, base_asset_id: v })} />
          {form.base_asset_id && chosen.isSuccess && !baseValid ? <span className="av-help av-help--danger">{t("原资产当前不可用，请重新选择")}</span>
            : attempted && !baseValid ? <span className="av-help av-help--danger">{t("请选择可查询的基础资产")}</span> : null}
        </label>}
      </QueryState>
      <label className="av-field"><span className="av-label">{t("表达式")} <Hint text={t("基础资产直接写列名；关联资产写 别名.列")} /></span>
        <input required className="av-input av-mono" placeholder="SUM(measure_col)" value={form.expression} onChange={(e) => setForm({ ...form, expression: e.target.value })} /></label>
      <div className="av-metric-form__columns">
        <label className="av-field"><span className="av-label">{t("时间字段")}</span>
          <SuggestInput required className="av-input av-mono" suggestions={columns} value={form.time_column} onChange={(v) => setForm({ ...form, time_column: v })} /></label>
        <label className="av-field"><span className="av-label">{t("业务时区")} <Hint text={t("留空时使用系统业务时区；查询的日期边界与按时间分组都按它解释")} /></span>
          <input className="av-input av-mono" placeholder={businessTz} value={form.timezone} onChange={(e) => setForm({ ...form, timezone: e.target.value })} /></label>
      </div>
      <div className="av-metric-form__row">
        <label className="av-field"><span className="av-label">{t("时间格式")} <Hint text={t("时间列是整数或文本编码的日期键（如 20260901）时选择对应格式")} /></span>
          <Select value={form.time_format} onChange={(v) => setForm({ ...form, time_format: v, data_grain: v === "yyyymm" && ["day", "week"].includes(form.data_grain) ? "month" : form.data_grain })}>
            {TIME_FORMATS.map(([v, label]) => <option key={v} value={v}>{label}</option>)}</Select></label>
        <label className="av-field"><span className="av-label">{t("数据粒度")} <Hint text={t("数据本身最细到哪一级；按时间分组不能比它更细，覆盖度按它判断缺失")} /></span>
          <Select value={form.data_grain} onChange={(v) => setForm({ ...form, data_grain: v })}>
            {DATA_GRAINS.filter(([v]) => form.time_format !== "yyyymm" || !["day", "week"].includes(v)).map(([v, label]) => <option key={v} value={v}>{label}</option>)}</Select></label>
        <label className="av-field"><span className="av-label">{t("跨时间合成")} <Hint text={t("流量（如产量）按时间累加；存量（如库存、余额、在册人数）取期末或期间平均")} /></span>
          <Select value={form.time_aggregation} onChange={(v) => setForm({ ...form, time_aggregation: v })}>
            {TIME_AGGREGATIONS.map(([v, label]) => <option key={v} value={v}>{label}</option>)}</Select></label>
      </div>
      {form.base_asset_id ? <QueryState query={asset} rows={1}>{() => null}</QueryState> : null}
      <label className="av-field"><span className="av-label">{t("过滤条件（可选）")}</span>
        <input className="av-input av-mono" placeholder="status_col = 'value'" value={form.filters} onChange={(e) => setForm({ ...form, filters: e.target.value })} /></label>
      <section className="av-metric-form__group" aria-label={t("允许维度")}>
        <h3 className="av-metric-form__title">{t("允许维度")}</h3>
        {dims.map((d, i) => <fieldset key={i} className="av-metric-form__fieldset">
          <legend>{t("维度")} {i + 1}</legend>
          <div className="av-metric-form__dimension">
            <label className="av-field"><span className="av-label">{t("名称")}</span><input className="av-input av-mono" value={d.name}
              aria-invalid={attempted && !d.name.trim()} aria-describedby={attempted && !d.name.trim() ? `${editorId}-dim-${i}-error` : undefined}
              onChange={(e) => updateDim(i, { name: e.target.value })} /></label>
            <label className="av-field"><span className="av-label">{t("字段")}</span><SuggestInput className="av-input av-mono" suggestions={columns} value={d.column}
              invalid={attempted && !d.column.trim()} describedBy={attempted && !d.column.trim() ? `${editorId}-dim-${i}-error` : undefined}
              onChange={(v) => updateDim(i, { column: v })} /></label>
            <label className="av-field"><span className="av-label">{t("显示名（可选）")}</span><input className="av-input" value={d.label} onChange={(e) => updateDim(i, { label: e.target.value })} /></label>
            <button type="button" className="av-btn av-btn--ghost av-btn--icon" aria-label={t("移除维度 {{v1}}", { v1: i + 1 })} onClick={() => setDims(dims.filter((_, j) => j !== i))}><Icon name="close" small /></button>
          </div>
          {attempted && (!d.name.trim() || !d.column.trim()) ? <span id={`${editorId}-dim-${i}-error`} className="av-help av-help--danger">{t("请填写维度名称和字段")}</span> : null}
        </fieldset>)}
        <button type="button" className="av-btn av-btn--secondary av-btn--sm av-metric-form__add" onClick={() => setDims([...dims, { name: "", column: "", label: "" }])}><Icon name="plus" small />{t("添加维度")}</button>
      </section>
      <section className="av-metric-form__group" aria-label={t("关联")}>
        <h3 className="av-metric-form__title">{t("关联")} <Hint text={t("使用已登记路径；会放大度量的关联需要先聚合")} /></h3>
        <QueryState query={paths} rows={1}>{() => <>
          {joins.map((j, i) => <fieldset key={i} className="av-metric-form__fieldset" aria-describedby={attempted && (!j.alias.trim() || !j.from.trim() || !paths.data?.some((p) => p.id === j.join_path_id)) ? `${editorId}-join-${i}-error` : undefined}>
            <legend>{t("关联")} {i + 1}</legend>
            <label className="av-field"><span className="av-label">{t("关联路径")}</span>
              <Select label={t("关联 {{v1}} 的路径", { v1: i + 1 })} value={j.join_path_id} onChange={(value) => updateJoin(i, { join_path_id: value })}>
                <option value="">{t("选择关联路径")}</option>
                {j.join_path_id && !paths.data?.some((p) => p.id === j.join_path_id) ? <option value={j.join_path_id} disabled>{t("原路径不可用，请重新选择")}</option> : null}
                {(paths.data ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}（{CARD_LABEL[p.cardinality]} · {p.status === "approved" ? t("已批准") : t("待批准")}）</option>)}
              </Select>
            </label>
            <div className="av-metric-form__columns">
              <label className="av-field"><span className="av-label">{t("来自别名")}</span><input className="av-input av-mono" value={j.from} aria-invalid={attempted && !j.from.trim()} onChange={(e) => updateJoin(i, { from: e.target.value })} /></label>
              <label className="av-field"><span className="av-label">{t("当前别名")}</span><input className="av-input av-mono" value={j.alias} aria-invalid={attempted && !j.alias.trim()} onChange={(e) => updateJoin(i, { alias: e.target.value })} /></label>
            </div>
            {attempted && (!j.alias.trim() || !j.from.trim() || !paths.data?.some((p) => p.id === j.join_path_id)) ? <span id={`${editorId}-join-${i}-error`} className="av-help av-help--danger">{t("请选择有效路径并填写两侧别名")}</span> : null}
            <div className="av-metric-form__join-actions"><span className="av-label">{t("右侧先聚合")}</span><Switch label={t("关联 {{v1}} 的右侧先聚合", { v1: i + 1 })} checked={j.pre_aggregate} onChange={(value) => updateJoin(i, { pre_aggregate: value })} />
              <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={() => setJoins(joins.filter((_, k) => k !== i))}><Icon name="close" small />{t("移除关联")}</button>
            </div>
          </fieldset>)}
          <button type="button" className="av-btn av-btn--secondary av-btn--sm av-metric-form__add" disabled={!paths.data?.length} onClick={() => setJoins([...joins, { alias: "", join_path_id: "", from: "b", pre_aggregate: false }])}><Icon name="plus" small />{t("添加关联")}</button>
          {!paths.data?.length ? <span className="av-help">{t("暂无关联路径")}</span> : null}
        </>}</QueryState>
      </section>
      <label className="av-field"><span className="av-label">{t("统计粒度")}</span><input className="av-input av-mono" placeholder="date_col, dim_col" value={form.grain} onChange={(e) => setForm({ ...form, grain: e.target.value })} /></label>
      <label className="av-field"><span className="av-label">{t("避免重复计数的依据")}</span><input className="av-input" value={form.dedupe_basis} onChange={(e) => setForm({ ...form, dedupe_basis: e.target.value })} /></label>
      <div className="av-metric-form__row">
        <label className="av-field"><span className="av-label">{t("单位")} <Hint text={t("报告里的数字由系统带上单位，如 元、件、吨、kWh；比率类可不填。缺单位的口径会列为待补")} /></span>
          <input className="av-input" maxLength={16} placeholder={t("如 件、kWh")} value={unit} onChange={(e) => setUnit(e.target.value)} /></label>
        <label className="av-field"><span className="av-label">{t("列的倍数")} <Hint text={t("列本身已按万计（如“万元”“万件”）时填 10000，默认 1")} /></span>
          <input className="av-input av-mono" type="number" min={1} step={1} value={unitScale} onChange={(e) => setUnitScale(e.target.value)} /></label>
      </div>
      <MeasureSemantics valueKind={form.value_kind} polarity={form.polarity} onChange={(p) => setForm({ ...form, ...p })} />
      <Banner message={message} />
    </form>
  </Drawer>;
}
