// 指标详情：版本轨道、口径定义、发布检查、依赖指纹、状态历史；新建指标与版本编辑。
import { useEffect, useState } from "react";
import { Link } from "react-router";

import { api, unwrap, type Schemas } from "../../api/client";
import {
  errorText, useAction, useAsset, useAssets, useDomains, useJoinPaths, useMetrics, useMetricVersion, useTimeZone,
  useUsers, type MetricVersion,
} from "../../api/hooks";
import {
  Banner, CARD_LABEL, Code, Drawer, Hint, Icon, Pill, Select, Status, Switch, shortName, useMessage,
} from "../../components/ui";
import { formatTime } from "../../format";
import { useMe, useRoles } from "../../layout/Shell";

const ROLE_LABEL: Record<string, string> = { measure: "度量", time: "时间", dimension: "维度", filter: "过滤", join_key: "关联键" };

export function MetricDetail({ metricId }: { metricId: string }) {
  const metrics = useMetrics();
  const metric = metrics.data?.find((m) => m.id === metricId);
  const [versionId, setVersionId] = useState<string | undefined>();
  const [editing, setEditing] = useState<"new" | "edit" | null>(null);
  const current = versionId ?? metric?.versions[0]?.id;
  const version = useMetricVersion(current);
  if (!metric) return null;
  const versions = [...metric.versions].reverse();

  return (
    <>
      <div className="av-pane__body">
        <div className="av-page">
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <span className="av-caption av-mono">{metric.code} · 负责人 {metric.owner.display_name}</span>
            <div className="av-pagehd">
              <div className="av-pagehd__title"><h1>{metric.name}</h1>
                {version.data ? <Status value={version.data.status} /> : null}
                {metric.aliases.map((a) => <span key={a} className="av-tag">{a}</span>)}
              </div>
              <div className="av-actions">
                <Link className="av-btn av-btn--secondary av-btn--sm" to={`/ask`}>问数</Link>
                <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => setEditing("new")}>
                  <Icon name="plus" small />新建版本
                </button>
              </div>
            </div>
          </div>
          <div className="av-vtrack" aria-label="版本">
            {versions.map((v, i) => (
              <span key={v.id} style={{ display: "contents" }}>
                {i > 0 ? <span className="av-vtrack__line" /> : null}
                <button type="button" className={`av-vtrack__step${v.id === current ? " is-current" : ""}`}
                  onClick={() => setVersionId(v.id)}>V{v.version_no} <Status value={v.status} /></button>
              </span>
            ))}
          </div>
          {version.data ? <VersionBody v={version.data} onEdit={() => setEditing("edit")} /> : null}
        </div>
      </div>
      {editing ? (
        <VersionEditor metricId={metric.id} base={editing === "edit" ? version.data : version.data ?? undefined}
          mode={editing} onClose={() => setEditing(null)} onSaved={(id) => { setEditing(null); setVersionId(id); }} />
      ) : null}
    </>
  );
}

function VersionBody({ v, onEdit }: { v: MetricVersion; onEdit: () => void }) {
  const tz = useTimeZone();
  const me = useMe();
  const roles = useRoles();
  const users = useUsers();
  const name = (id?: string | null) => users.data?.find((u) => u.id === id)?.display_name ?? "系统";
  const [message, setMessage] = useMessage();
  const [note, setNote] = useState("");
  const path = { params: { path: { version_id: v.id } } };
  const done = (text: string) => ({ onSuccess: () => setMessage({ ok: true, text }), onError: (e: unknown) => setMessage({ ok: false, text: errorText(e) }) });
  const invalidate = ["metrics", "metric-version"];
  const submit = useAction(() => unwrap(api.POST("/api/metric-versions/{version_id}/submit", path)), invalidate);
  const approve = useAction(() => unwrap(api.POST("/api/metric-versions/{version_id}/approve", { ...path, body: { note: note || null } })), invalidate);
  const reject = useAction(() => unwrap(api.POST("/api/metric-versions/{version_id}/reject", { ...path, body: { note } })), invalidate);
  const check = useAction(() => unwrap(api.POST("/api/metric-versions/{version_id}/check", path)), []);
  const selfReview = me.data && (me.data.id === v.submitted_by || me.data.id === v.created_by);
  const pc = (check.data ?? v.publish_check) as Schemas["PublishCheckOut"] | Record<string, never>;

  return (
    <>
      <Banner message={message} />
      <section className="av-panel">
        <div className="av-panel__hd">
          <h2 className="av-panel__title">口径 V{v.version_no} <Status value={v.status} />
            {v.status_reason ? <span className="av-caption">{v.status_reason}</span> : null}</h2>
          <div className="av-actions">
            {v.status === "draft" || v.status === "rejected" ? <>
              <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={onEdit}>编辑</button>
              <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => check.mutate(undefined, done("检查完成"))}>发布前检查</button>
              <button type="button" className="av-btn av-btn--primary av-btn--sm" disabled={submit.isPending}
                onClick={() => submit.mutate(undefined, done("已提交复核"))}>提交复核</button>
            </> : null}
          </div>
        </div>
        <div className="av-panel__bd">
          <dl className="av-kv" style={{ gridTemplateColumns: "104px minmax(0, 1fr)" }}>
            <dt>说明</dt><dd>{v.description}</dd>
            <dt>基础资产</dt><dd className="av-mono">{v.base_asset ? <Link to={`/catalog/assets/${String(v.base_asset.id)}`}>{String(v.base_asset.qualified)}</Link> : "—"}</dd>
            <dt>表达式</dt><dd><Code sql={v.expression} raw /></dd>
            <dt>时间字段</dt><dd className="av-mono">{v.time_column} <span className="av-tag">{v.timezone}</span></dd>
            <dt>过滤</dt><dd className="av-mono">{v.filters || "—"}</dd>
            <dt>允许维度</dt><dd>{v.dimensions.map((d) => <span key={String(d.name)} className="av-tag" style={{ marginRight: 4 }}>{String(d.label ?? d.name)} · {String(d.column)}</span>)}</dd>
            <dt>统计粒度</dt><dd className="av-mono">{v.grain.join(" × ") || "—"}</dd>
            {v.joins.length ? <><dt>关联</dt><dd>{v.joins.map((j) => <span key={String(j.alias)} className="av-tag" style={{ marginRight: 4 }}>{String(j.from ?? "b")} → {String(j.alias)}{j.pre_aggregate ? " · 先聚合" : ""}</span>)}</dd></> : null}
            {v.dedupe_basis ? <><dt>防重复依据</dt><dd>{v.dedupe_basis}</dd></> : null}
          </dl>
        </div>
      </section>

      {v.status === "pending_review" ? (
        <section className="av-panel">
          <div className="av-panel__hd"><h2 className="av-panel__title">复核 <Hint text="禁止自审：提交人、创建人不能批准同一版本" /></h2></div>
          <div className="av-panel__bd" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <label className="av-field"><span className="av-label">意见（驳回时必填）</span>
              <textarea className="av-textarea" rows={2} value={note} onChange={(e) => setNote(e.target.value)} /></label>
            <div className="av-actions" style={{ justifyContent: "flex-end" }}>
              {selfReview ? <span className="av-status av-status--warn">你提交或创建了此版本，不能自审</span> : null}
              <button type="button" className="av-btn av-btn--secondary" disabled={!note || !roles.has("reviewer") && !roles.has("admin")}
                onClick={() => reject.mutate(undefined, done("已驳回"))}>驳回</button>
              <button type="button" className="av-btn av-btn--primary" disabled={!!selfReview || (!roles.has("reviewer") && !roles.has("admin")) || approve.isPending}
                onClick={() => approve.mutate(undefined, done("已批准并发布"))}>批准发布</button>
            </div>
          </div>
        </section>
      ) : null}

      {pc && "ok" in pc ? (
        <section className="av-panel">
          <div className="av-panel__hd"><h2 className="av-panel__title">发布前检查 {pc.ok ? <Pill tone="ok">通过</Pill> : <Pill tone="danger">未通过</Pill>}</h2>
            <span className="av-caption av-mono">{formatTime(pc.checked_at, tz)}</span></div>
          <div className="av-panel__bd" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {pc.blockers.map((b) => <div key={b} className="av-banner av-banner--danger">{b}</div>)}
            {pc.warnings.map((w) => <span key={w} className="av-status av-status--warn">{w}</span>)}
            {pc.joins.length ? (
              <table className="av-table">
                <thead><tr><th>关联</th><th>路径</th><th>基数</th><th>依据</th></tr></thead>
                <tbody>{pc.joins.map((j) => (
                  <tr key={String(j.alias)}>
                    <td className="av-mono">{String(j.alias)}</td><td className="av-mono">{String(j.path)}</td>
                    <td><span className={`av-tag${["1n", "nn", "unknown"].includes(String(j.cardinality)) ? " av-edge-tag--warn" : ""}`} style={{ position: "static" }}>{CARD_LABEL[String(j.cardinality)]}</span></td>
                    <td className="av-caption" style={{ whiteSpace: "normal" }}>{j.pre_aggregate ? "右侧先按关联键聚合" : grainText(j.basis as Record<string, unknown>)}</td>
                  </tr>))}</tbody>
              </table>
            ) : null}
          </div>
        </section>
      ) : null}

      <section className="av-panel">
        <div className="av-panel__hd"><h2 className="av-panel__title">依赖与指纹 <Hint text="提交时记录每个依赖字段的推导指纹；指纹变化即受影响，问数会被拦截" /></h2></div>
        <table className="av-table">
          <thead><tr><th style={{ paddingLeft: 16 }}>字段</th><th>用途</th><th>提交时指纹</th><th>当前</th></tr></thead>
          <tbody>{v.dependency.map((d) => {
            const drift = (v.drift ?? []).find((x) => x.asset_id === d.asset_id && x.column === d.column);
            return (
              <tr key={`${String(d.asset_id)}${String(d.column)}`}>
                <td className="av-mono" style={{ paddingLeft: 16 }}>{shortName(String(d.qualified))}.{String(d.column)}</td>
                <td>{ROLE_LABEL[String(d.role)] ?? String(d.role)}</td>
                <td className="av-mono av-muted">{String(d.fingerprint ?? "").slice(0, 10)}</td>
                <td>{drift ? <Status value={String(drift.current_status) === "ok" ? "affected" : String(drift.current_status)} label={String(drift.current_status) === "ok" ? "已变化" : undefined} /> : <Status value="ok" label="一致" />}</td>
              </tr>);
          })}</tbody>
        </table>
      </section>

      <section className="av-panel">
        <div className="av-panel__hd"><h2 className="av-panel__title">状态历史</h2></div>
        <div className="av-panel__bd">
          <ol className="av-steps">
            {[...(v.transitions ?? [])].reverse().map((t, i) => (
              <li key={i} className={`av-step${t.to === "published" ? " is-ok" : t.to === "affected" || t.to === "retired" || t.to === "rejected" ? " is-blocked" : ""}`}>
                <span className="av-step__dot" />
                <span className="av-step__body">
                  <span className="av-step__title"><Status value={t.to} /> {name(t.actor_id)}</span>
                  <span className="av-step__meta">{t.reason} · {formatTime(t.at, tz)}</span>
                </span>
              </li>
            ))}
          </ol>
        </div>
      </section>
    </>
  );
}

function grainText(basis: Record<string, unknown> | undefined): string {
  const right = basis?.right_grain as { columns?: string[]; origin?: string } | null | undefined;
  const left = basis?.left_grain as { columns?: string[] } | null | undefined;
  if (right) return `右侧粒度 ${right.columns?.join(", ")}（${right.origin === "linked" ? "经同一张表继承" : right.origin}）`;
  if (left) return `左侧粒度 ${left.columns?.join(", ")}，右侧不唯一`;
  return "两侧粒度未知";
}

// --- 新建指标 ---------------------------------------------------------------------

export function NewMetric({ onClose, onCreated }: { onClose: () => void; onCreated: (id: string) => void }) {
  const domains = useDomains();
  const [form, setForm] = useState({ code: "", name: "", domain_id: "", aliases: "" });
  const [message, setMessage] = useMessage();
  const create = useAction(() => unwrap(api.POST("/api/metrics", { body: {
    code: form.code, name: form.name, domain_id: form.domain_id || domains.data?.[0]?.id || "",
    aliases: form.aliases.split(/[,，]/).map((s) => s.trim()).filter(Boolean), owner_id: null,
  } })), ["metrics"]);
  return (
    <Drawer title="新建指标" onClose={onClose} footer={<>
      <button type="button" className="av-btn av-btn--secondary" onClick={onClose}>取消</button>
      <button type="button" className="av-btn av-btn--primary" disabled={!form.code || !form.name}
        onClick={() => create.mutate(undefined, { onSuccess: (m) => onCreated(m.id), onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>创建</button>
    </>}>
      <label className="av-field"><span className="av-label">名称</span><input className="av-input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
      <label className="av-field"><span className="av-label">编码 <Hint text="小写字母、数字、下划线" /></span><input className="av-input av-mono" value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} /></label>
      <label className="av-field"><span className="av-label">业务域 <Hint text="负责人默认为该域负责人" /></span>
        <Select value={form.domain_id || domains.data?.[0]?.id || ""} onChange={(v) => setForm({ ...form, domain_id: v })}>
          {(domains.data ?? []).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
        </Select></label>
      <label className="av-field"><span className="av-label">别名 <Hint text="问数时用于识别指标，逗号分隔" /></span><input className="av-input" value={form.aliases} onChange={(e) => setForm({ ...form, aliases: e.target.value })} /></label>
      <Banner message={message} />
    </Drawer>
  );
}

// --- 版本编辑 ---------------------------------------------------------------------

type Dim = { name: string; column: string; label: string };
type JoinRow = { alias: string; join_path_id: string; from: string; pre_aggregate: boolean };

function VersionEditor({ metricId, base, mode, onClose, onSaved }: {
  metricId: string; base?: MetricVersion; mode: "new" | "edit"; onClose: () => void; onSaved: (id: string) => void;
}) {
  const assets = useAssets();
  const paths = useJoinPaths();
  const [form, setForm] = useState({
    description: base?.description ?? "", base_asset_id: String(base?.base_asset?.id ?? ""), expression: base?.expression ?? "",
    time_column: base?.time_column ?? "", timezone: base?.timezone ?? "Asia/Shanghai", filters: base?.filters ?? "",
    grain: (base?.grain ?? []).join(", "), dedupe_basis: base?.dedupe_basis ?? "",
  });
  const [dims, setDims] = useState<Dim[]>((base?.dimensions ?? []).map((d) => ({ name: String(d.name), column: String(d.column), label: String(d.label ?? "") })));
  const [joins, setJoins] = useState<JoinRow[]>((base?.joins ?? []).map((j) => ({ alias: String(j.alias), join_path_id: String(j.join_path_id), from: String(j.from ?? "b"), pre_aggregate: !!j.pre_aggregate })));
  const asset = useAsset(form.base_asset_id || undefined);
  const [message, setMessage] = useMessage();
  useEffect(() => { if (!form.base_asset_id && assets.data?.length) setForm((f) => ({ ...f, base_asset_id: assets.data![0].id })); }, [assets.data, form.base_asset_id]);

  const body = {
    ...form, filters: form.filters || null, dedupe_basis: form.dedupe_basis || null,
    grain: form.grain.split(/[,，×]/).map((s) => s.trim()).filter(Boolean),
    dimensions: dims.filter((d) => d.name && d.column).map((d) => ({ ...d, label: d.label || null })),
    joins: joins.filter((j) => j.alias && j.join_path_id),
  };
  const save = useAction(() => mode === "edit" && base
    ? unwrap(api.PUT("/api/metric-versions/{version_id}", { params: { path: { version_id: base.id } }, body }))
    : unwrap(api.POST("/api/metrics/{metric_id}/versions", { params: { path: { metric_id: metricId } }, body })), ["metrics", "metric-version"]);
  const columns = asset.data?.columns.map((c) => c.name) ?? [];

  return (
    <Drawer title={mode === "edit" ? `编辑草稿 V${base?.version_no}` : "新建版本（草稿）"} width={560} onClose={onClose} footer={<>
      <button type="button" className="av-btn av-btn--secondary" onClick={onClose}>取消</button>
      <button type="button" className="av-btn av-btn--primary" disabled={!form.description || !form.expression || !form.time_column || save.isPending}
        onClick={() => save.mutate(undefined, { onSuccess: (v) => onSaved(v.id), onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>保存草稿</button>
    </>}>
      <label className="av-field"><span className="av-label">说明</span><textarea className="av-textarea" rows={2} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></label>
      <label className="av-field"><span className="av-label">基础资产</span>
        <Select value={form.base_asset_id} onChange={(v) => setForm({ ...form, base_asset_id: v })}>
          {(assets.data ?? []).filter((a) => a.datasource.kind === "starrocks").map((a) => <option key={a.id} value={a.id}>{a.qualified}</option>)}
        </Select></label>
      <label className="av-field"><span className="av-label">表达式 <Hint text="只能用聚合与基础运算；基础资产的列直接写列名，关联资产写 别名.列" /></span>
        <input className="av-input av-mono" placeholder="SUM(sales_amount)" value={form.expression} onChange={(e) => setForm({ ...form, expression: e.target.value })} /></label>
      <div className="av-formrow">
        <label className="av-field"><span className="av-label">时间字段</span>
          <input className="av-input av-mono" list="metric-cols" value={form.time_column} onChange={(e) => setForm({ ...form, time_column: e.target.value })} /></label>
        <label className="av-field"><span className="av-label">业务时区</span>
          <input className="av-input av-mono" value={form.timezone} onChange={(e) => setForm({ ...form, timezone: e.target.value })} /></label>
      </div>
      <datalist id="metric-cols">{columns.map((c) => <option key={c} value={c} />)}</datalist>
      <label className="av-field"><span className="av-label">过滤条件（可选）</span>
        <input className="av-input av-mono" placeholder="status = 'paid'" value={form.filters} onChange={(e) => setForm({ ...form, filters: e.target.value })} /></label>

      <div className="av-field">
        <span className="av-label">允许维度</span>
        {dims.map((d, i) => (
          <div key={i} className="av-formrow">
            <input className="av-input av-mono" placeholder="名称" value={d.name} onChange={(e) => setDims(dims.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
            <input className="av-input av-mono" placeholder="列" list="metric-cols" value={d.column} onChange={(e) => setDims(dims.map((x, j) => (j === i ? { ...x, column: e.target.value } : x)))} />
            <input className="av-input" placeholder="显示名" value={d.label} onChange={(e) => setDims(dims.map((x, j) => (j === i ? { ...x, label: e.target.value } : x)))} />
            <button type="button" className="av-btn av-btn--ghost av-btn--icon" aria-label="移除维度" onClick={() => setDims(dims.filter((_, j) => j !== i))}><Icon name="close" small /></button>
          </div>
        ))}
        <button type="button" className="av-btn av-btn--secondary av-btn--sm" style={{ alignSelf: "flex-start" }} onClick={() => setDims([...dims, { name: "", column: "", label: "" }])}><Icon name="plus" small />维度</button>
      </div>

      <div className="av-field">
        <span className="av-label">关联 <Hint text="只能使用已登记的关联路径；会放大度量的 1:N 关联需要先聚合" /></span>
        {joins.map((j, i) => (
          <div key={i} className="av-formrow" style={{ alignItems: "center" }}>
            <input className="av-input av-mono" style={{ maxWidth: 64 }} placeholder="来自" value={j.from} onChange={(e) => setJoins(joins.map((x, k) => (k === i ? { ...x, from: e.target.value } : x)))} />
            <Select value={j.join_path_id} onChange={(v) => setJoins(joins.map((x, k) => (k === i ? { ...x, join_path_id: v } : x)))}>
              <option value="">选择关联路径</option>
              {(paths.data ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}（{CARD_LABEL[p.cardinality]} · {p.status === "approved" ? "已批准" : "待批准"}）</option>)}
            </Select>
            <input className="av-input av-mono" style={{ maxWidth: 64 }} placeholder="别名" value={j.alias} onChange={(e) => setJoins(joins.map((x, k) => (k === i ? { ...x, alias: e.target.value } : x)))} />
            <span className="av-caption" style={{ whiteSpace: "nowrap" }}>先聚合</span>
            <Switch label="先聚合" checked={j.pre_aggregate} onChange={(v) => setJoins(joins.map((x, k) => (k === i ? { ...x, pre_aggregate: v } : x)))} />
            <button type="button" className="av-btn av-btn--ghost av-btn--icon" aria-label="移除关联" onClick={() => setJoins(joins.filter((_, k) => k !== i))}><Icon name="close" small /></button>
          </div>
        ))}
        <button type="button" className="av-btn av-btn--secondary av-btn--sm" style={{ alignSelf: "flex-start" }} onClick={() => setJoins([...joins, { alias: "", join_path_id: "", from: "b", pre_aggregate: false }])}><Icon name="plus" small />关联</button>
      </div>

      <div className="av-formrow">
        <label className="av-field"><span className="av-label">统计粒度</span><input className="av-input av-mono" placeholder="order_date, region" value={form.grain} onChange={(e) => setForm({ ...form, grain: e.target.value })} /></label>
      </div>
      <label className="av-field"><span className="av-label">避免重复计数的依据</span><input className="av-input" value={form.dedupe_basis} onChange={(e) => setForm({ ...form, dedupe_basis: e.target.value })} /></label>
      <Banner message={message} />
    </Drawer>
  );
}
