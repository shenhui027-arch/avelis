// 变更：收件箱模式 —— 变更列表 | 差异与影响 | 复核处理；“记录”视图按条件检索、导出全部留底的变更（ADR 0008）。
import { useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import { api, unwrap } from "../../api/client";
import {
  errorText, useAction, useAssets, useChangeSet, useChangeSets, useMetrics, useTimeZone, useUsers, type ChangeSet, type Task,
} from "../../api/hooks";
import { Banner, Count, DiffCode, Dot, Empty, Hint, Pill, Seg, Select, Status, Tabs, shortName, useMessage } from "../../components/ui";
import { dayKey, formatClock, formatDate, formatTime } from "../../format";
import { useMe } from "../../layout/Shell";
import { Actor, ChangeRecords, EVENT_LABEL as EVENT } from "./ChangeRecords";
import { t } from "../../i18n";

const OBJECT: Record<string, string> = { metric_version: t("指标版本"), asset: t("资产"), join_path: t("关联路径"), answer: t("历史回答") };

export function ChangesPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const view = params.get("view") === "records" ? "records" : "sets";
  const tz = useTimeZone();
  const sets = useChangeSets();
  const [filter, setFilter] = useState<"open" | "all" | "resolved">("all");
  const list = (sets.data ?? []).filter((c) => filter === "all" || c.status === filter);
  const current = id ?? list[0]?.id;
  const byDay = useMemo(() => {
    const groups = new Map<string, ChangeSet[]>();
    for (const c of list) {
      const day = dayKey(c.created_at, tz);
      groups.set(day, [...(groups.get(day) ?? []), c]);
    }
    return [...groups.entries()];
  }, [list, tz]);
  const openCount = (sets.data ?? []).filter((c) => c.status === "open").length;

  return (
    <div className="av-main" style={{ height: "calc(100vh - 48px)", display: "grid", gridTemplateColumns: "320px minmax(0, 1fr) 360px", gridTemplateRows: "minmax(0, 1fr)" }}>
      <section className="av-pane av-pane--rail" style={{ borderRight: "1px solid var(--line-subtle)" }} aria-label={t("变更列表")}>
        <div style={{ display: "flex", flexDirection: "column", gap: 12, padding: "16px 16px 0" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
            <h1 style={{ margin: 0, font: "600 18px/26px var(--font-sans)" }}>{t("变更")} <Hint text={t("每次采集发现的变化归为一组；只有字段推导变化影响了口径或关联路径时才需要复核")} /></h1>
            <Seg label={t("视图")} value={view} onChange={(v) => setParams(v === "records" ? { view: "records" } : {})}
              options={[{ value: "sets", label: t("变更集") }, { value: "records", label: t("记录") }]} />
          </div>
          <Tabs value={filter} onChange={setFilter} options={[
            { value: "open", label: <>{t("待处理")} <Count n={openCount} /></> },
            { value: "all", label: <>{t("全部")} <Count n={sets.data?.length ?? 0} /></> },
            { value: "resolved", label: t("已处理") },
          ]} />
        </div>
        <div className="av-pane__body">
          {byDay.map(([day, items]) => (
            <div key={day}>
              <div className="av-tree__label" style={{ padding: "12px 16px 4px" }}>{formatDate(items[0].created_at, tz)}</div>
              <div className="av-list">
                {items.map((c) => <ChangeRow key={c.id} c={c} active={view === "sets" && c.id === current} onClick={() => navigate(`/changes/${c.id}`)} />)}
              </div>
            </div>
          ))}
          {sets.isSuccess && !list.length ? <Empty text={t("没有变更")} /> : null}
        </div>
      </section>
      {view === "records" ? <ChangeRecords /> : current ? <Detail key={current} id={current} /> : <section className="av-pane" />}
    </div>
  );
}

function ChangeRow({ c, active, onClick }: { c: ChangeSet; active: boolean; onClick: () => void }) {
  const tz = useTimeZone();
  const summary = c.summary as Record<string, number>;
  const kinds = Object.entries(summary).filter(([k]) => k.startsWith("event:")).map(([k, v]) => `${EVENT[k.slice(6)] ?? k.slice(6)} ${v}`);
  const impacts = summary.impacts ?? 0;
  return (
    <button type="button" className={`av-row${active ? " is-active" : ""}`} onClick={onClick}
      style={{ "--row-cols": "8px minmax(0, 1fr) auto", minHeight: 58, width: "100%", border: "none", borderBottom: "1px solid var(--line-subtle)", background: active ? "var(--bg-active)" : "transparent", textAlign: "left", cursor: "pointer" } as React.CSSProperties}>
      <Dot tone={c.status === "resolved" ? (impacts ? "ok" : "") : impacts ? "danger" : "warn"} />
      <span className="av-row__main">
        <span className="av-row__title">{impacts ? t("{{count}} 项影响", { count: impacts }) : t("无影响口径")}</span>
        <span className="av-row__meta">{kinds.slice(0, 3).join(" · ")}</span>
      </span>
      <span className="av-row__meta av-mono">{formatClock(c.created_at, tz)}</span>
    </button>
  );
}

function Detail({ id }: { id: string }) {
  const tz = useTimeZone();
  const cs = useChangeSet(id);
  const metrics = useMetrics();
  const assets = useAssets();
  const assetName = (id: string) => shortName(assets.data?.find((a) => a.id === id)?.qualified) || id.slice(0, 8);
  const c = cs.data;
  if (!c) return <section className="av-pane" />;
  const events = c.events ?? [];
  const def = events.find((e) => e.kind === "definition_changed");
  const direct = events.filter((e) => !["derivation_changed", "upstream_broken", "identity_link_changed"].includes(e.kind));
  const derived = events.filter((e) => e.kind === "derivation_changed" || e.kind === "upstream_broken");
  // 身份映射变化（外部表与他库真实表的对应关系）单独成组，不能从列表中漏掉
  const identity = events.filter((e) => e.kind === "identity_link_changed");
  const metricName = (versionId: string) => {
    for (const m of metrics.data ?? []) {
      const v = m.versions.find((x) => x.id === versionId);
      if (v) return { name: `${m.name} V${v.version_no}`, id: m.id, status: v.status };
    }
    return null;
  };
  const task = (c.tasks ?? []).find((item) => item.status === "open") ?? (c.tasks ?? [])[0];

  return (
    <>
      <section className="av-pane" aria-label={t("变更详情")}>
        <div className="av-pane__body">
          <div className="av-page">
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <span className="av-caption av-mono">{t("变更集 {{id}} · 批次 {{run}}", { id: c.id.slice(0, 8), run: c.run_id.slice(0, 8) })}</span>
              <div className="av-pagehd"><div className="av-pagehd__title">
                <h1 className="av-mono">{def?.asset ? shortName(def.asset) : direct[0]?.asset ? shortName(direct[0].asset) : t("变更")}</h1>
                {(c.impacts ?? []).some((i) => i.object_type === "metric_version") ? <Pill tone="danger">{t("影响口径")}</Pill> : <Pill>{t("不影响口径")}</Pill>}
                <Status value={c.status} />
              </div>
                {def?.asset_id ? <div className="av-actions"><Link className="av-btn av-btn--secondary av-btn--sm" to={`/catalog/assets/${def.asset_id}`}>{t("资产")}</Link>
                  <Link className="av-btn av-btn--secondary av-btn--sm" to={`/lineage?asset=${def.asset_id}`}>{t("血缘")}</Link></div> : null}
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 20, fontSize: 12, color: "var(--text-tertiary)" }}>
                <span>{t("发现")} <span className="av-mono" style={{ color: "var(--text-secondary)" }}>{formatTime(c.created_at, tz)}</span></span>
                <span>{t("生效于")} <span className="av-mono" style={{ color: "var(--text-secondary)" }}>{formatTime(String(c.effective_window.after ?? ""), tz) || t("首次采集")} – {formatClock(String(c.effective_window.before), tz)}</span> <Hint text={String(c.effective_window.note ?? "")} /></span>
                <span>{t("结构")} <span style={{ color: "var(--text-secondary)" }}>{events.some((e) => e.kind.startsWith("column_")) ? t("有变化") : t("无变化")}</span></span>
              </div>
            </div>

            {def ? (
              <section className="av-panel">
                <div className="av-panel__hd"><h2 className="av-panel__title">{t("定义对比")}</h2><span className="av-caption">{t("左：变化前 · 右：变化后")}</span></div>
                <div className="av-panel__bd"><DiffCode before={String(def.before.canonical_sql ?? "")} after={String(def.after.canonical_sql ?? "")} /></div>
              </section>
            ) : null}

            <section className="av-panel">
              <div className="av-panel__hd"><h2 className="av-panel__title">{t("变化")} <Count n={events.length} /></h2></div>
              <table className="av-table">
                <thead><tr><th style={{ paddingLeft: 16 }}>{t("对象")}</th><th>{t("变化")}</th><th>{t("详情")}</th><th>{t("数据库用户")}</th></tr></thead>
                <tbody>{[...direct, ...identity, ...derived].map((e) => (
                  <tr key={e.id}>
                    <td className="av-mono" style={{ paddingLeft: 16 }}>{shortName(e.asset)}{e.column ? `.${e.column}` : ""}</td>
                    <td><Status value={e.kind === "upstream_broken" ? "broken" : e.kind.includes("derivation") ? "affected" : "open"} label={EVENT[e.kind] ?? e.kind} /></td>
                    <td className="av-caption" style={{ whiteSpace: "normal" }}>{eventDetail(e)}</td>
                    <td><Actor e={e} /></td>
                  </tr>))}</tbody>
              </table>
            </section>

            <section className="av-panel">
              <div className="av-panel__hd"><h2 className="av-panel__title">{t("影响范围")} <Count n={c.impacts?.length ?? 0} /></h2></div>
              <table className="av-table">
                <thead><tr><th style={{ paddingLeft: 16 }}>{t("对象")}</th><th>{t("原因")}</th><th>{t("状态")}</th><th className="end" /></tr></thead>
                <tbody>{(c.impacts ?? []).map((i) => {
                  const m = i.object_type === "metric_version" ? metricName(i.object_id) : null;
                  return (
                    <tr key={i.id}>
                      <td style={{ paddingLeft: 16 }}>{m ? m.name : <span className="av-mono">{i.object_type === "asset" ? `${assetName(i.object_id)}${assetFromPath(i.path)}` : OBJECT[i.object_type] ?? i.object_type}</span>}</td>
                      <td className="av-caption" style={{ whiteSpace: "normal" }}>{i.reason}</td>
                      <td>{m ? <Status value={m.status} /> : <Status value={i.level === "affected" ? "affected" : "unknown"} label={i.level === "affected" ? t("受影响") : t("可能受影响")} />}</td>
                      <td className="end">{m ? <Link className="av-btn av-btn--secondary av-btn--sm" to={`/catalog/metrics/${m.id}`}>{t("查看")}</Link> : null}</td>
                    </tr>);
                })}</tbody>
              </table>
            </section>
          </div>
        </div>
      </section>
      <aside className="av-pane" style={{ borderLeft: "1px solid var(--line-subtle)", background: "var(--bg-surface)" }} aria-label={t("复核")}>
        {task ? <ReviewPanel key={task.id} task={task} /> : <Empty text={t("这组变化不需要复核")} />}
      </aside>
    </>
  );
}

function assetFromPath(path: unknown[]): string {
  const last = path[path.length - 1] as { column?: string } | undefined;
  return last?.column ? `.${last.column}` : "";
}

function eventDetail(e: { kind: string; before: Record<string, unknown>; after: Record<string, unknown>; column?: string | null }): string {
  switch (e.kind) {
    case "column_type_changed": return `${String(e.before.data_type)} → ${String(e.after.data_type)}`;
    case "column_added": return String(e.after.data_type ?? "");
    case "column_removed": return String(e.before.data_type ?? "");
    case "column_comment_changed": case "comment_changed": return `${String(e.before.comment ?? t("（无）"))} → ${String(e.after.comment ?? t("（无）"))}`;
    case "renamed": case "rename_candidate": return `${String(e.before.name)} → ${String(e.after.name)}${e.after.basis ? `（${String(e.after.basis)}）` : ""}`;
    case "derivation_changed": case "upstream_broken": return String(e.after.reason ?? t("上游字段、表达式或条件变化"));
    case "definition_changed": return t("规范化指纹 {{v1}} → {{v2}}", { v1: String(e.before.hash ?? "").slice(0, 8), v2: String(e.after.hash ?? "").slice(0, 8) });
    case "identity_link_changed": return `${String(e.before.status ?? "—")} → ${String(e.after.status)}${e.after.reason ? `：${String(e.after.reason)}` : ""}`;
    default: return "";
  }
}

function ReviewPanel({ task }: { task: Task }) {
  const tz = useTimeZone();
  const me = useMe();
  const users = useUsers();
  const metrics = useMetrics();
  const [resolution, setResolution] = useState<"restored" | "new_version" | "retired">("restored");
  const [linked, setLinked] = useState("");
  const [note, setNote] = useState("");
  const [message, setMessage] = useMessage();
  const resolve = useAction(() => unwrap(api.POST("/api/review-tasks/{task_id}/resolve", {
    params: { path: { task_id: task.id } }, body: { resolution, note, linked_metric_version_id: linked || null } })),
  ["change-sets", "change-set", "metrics", "metric-version", "tasks"]);
  const assignee = users.data?.find((u) => u.id === task.assignee_id);
  const resolver = users.data?.find((u) => u.id === task.resolved_by);
  const mine = me.data?.id === task.assignee_id || me.data?.roles.includes("admin");
  const published = (metrics.data ?? []).flatMap((m) => m.versions.filter((v) => v.status === "published" && v.id !== task.target.id)
    .map((v) => ({ id: v.id, label: `${m.name} V${v.version_no}` })));
  const options = [
    { id: "restored" as const, title: t("恢复原定义"), meta: t("数据库已改回原定义并重新采集；指纹一致后恢复发布") },
    { id: "new_version" as const, title: t("发布新口径"), meta: t("改用另一个已发布的指标版本；原版本失效并保留") },
    { id: "retired" as const, title: t("停用指标"), meta: t("不提供替代口径") },
  ];
  return (
    <>
      <div className="av-inspector__hd">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
          <span className="av-section-label">{t("复核")} {task.id.slice(0, 8)}</span><Status value={task.status} />
        </div>
        <span className="body-strong" style={{ fontWeight: 500 }}>{String(task.target.metric_name ?? "")} V{String(task.target.version_no ?? "")} <Status value={String(task.target.status ?? "")} /></span>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 12, color: "var(--text-tertiary)" }}>
          <span className="av-avatar" style={{ width: 20, height: 20, fontSize: 10 }}>{assignee?.display_name.slice(0, 1)}</span>{assignee?.display_name} {t("· 指标负责人")}</span>
      </div>
      <div className="av-pane__body">
        <div className="av-inspector__bd" style={{ gap: 12 }}>
          {task.status === "open" ? (
            <>
              <fieldset style={{ border: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 10 }}>
                <legend className="av-sr">{t("处理结论")}</legend>
                {options.map((o) => (
                  <label key={o.id} className="av-option">
                    <input type="radio" name="resolution" checked={resolution === o.id} onChange={() => setResolution(o.id)} disabled={!mine} />
                    <span><span className="av-option__title">{o.title}</span><br /><span className="av-option__meta">{o.meta}</span></span>
                  </label>
                ))}
              </fieldset>
              {resolution === "new_version" ? (
                <label className="av-field"><span className="av-label">{t("承接的口径")}</span>
                  <Select label={t("承接的口径")} value={linked} onChange={setLinked}>
                    <option value="">{t("选择已发布的指标版本")}</option>
                    {published.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
                  </Select></label>
              ) : null}
              <label className="av-field"><span className="av-label">{t("理由（必填）")}</span>
                <textarea className="av-textarea" rows={3} value={note} disabled={!mine} onChange={(e) => setNote(e.target.value)} /></label>
              <Banner message={message} />
              <div className="av-actions" style={{ justifyContent: "flex-end" }}>
                {!mine ? <span className="av-status av-status--warn">{t("只有负责人可以处理")}</span> : null}
                <button type="button" className="av-btn av-btn--primary av-btn--lg" disabled={!mine || !note || resolve.isPending}
                  onClick={() => resolve.mutate(undefined, { onSuccess: () => setMessage({ ok: true, text: t("已提交结论") }), onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>{t("提交结论")}</button>
              </div>
            </>
          ) : (
            <dl className="av-kv">
              <dt>{t("结论")}</dt><dd>{options.find((o) => o.id === task.resolution)?.title ?? task.resolution}</dd>
              <dt>{t("处理人")}</dt><dd>{resolver?.display_name}</dd>
              <dt>{t("时间")}</dt><dd className="av-mono">{formatTime(task.resolved_at, tz)}</dd>
              <dt>{t("理由")}</dt><dd>{task.note}</dd>
            </dl>
          )}
          <div style={{ height: 1, background: "var(--line-subtle)", margin: "8px 0" }} />
          <ol className="av-steps">
            {task.resolved_at ? <li className="av-step is-ok"><span className="av-step__dot" /><span className="av-step__body"><span className="av-step__title">{t("{{name}} 提交结论", { name: resolver?.display_name })}</span><span className="av-step__meta">{formatTime(task.resolved_at, tz)}</span></span></li> : null}
            <li className="av-step is-blocked"><span className="av-step__dot" /><span className="av-step__body"><span className="av-step__title">{t("系统拦截问数，生成复核")}</span><span className="av-step__meta">{formatTime(task.created_at, tz)} · {task.status === "open" ? task.note : ""}</span></span></li>
          </ol>
        </div>
      </div>
    </>
  );
}
