// 工作台：页头 + 闭环条 + 工作区（待办、负责的指标、被拦截的提问 | 数据源、采集节奏、最近变更）。
import { motion } from "motion/react";
import { Link, useNavigate } from "react-router";

import { CountUp, MotionLink, RevealGroup, revealItem } from "../components/Motion";

import { useTimeZone, useWorkbench } from "../api/hooks";
import { Empty, Parts, Plat, QueryState, Seg, Status, statusOf, Tile, type KitTone } from "../components/ui";
import { statusParts } from "./statusParts";
import { RunStrip } from "../components/RunStrip";
import { formatClock } from "../format";
import { t } from "../i18n";
import { useRoles } from "../layout/Shell";
import { useState } from "react";

const STAGE_ICON: Record<string, string> = { collect: "db", lineage: "lineage", change: "change", metric: "chart", ask: "ask" };
const STAGE_MOD: Record<string, KitTone> = { collect: "collect", lineage: "lineage", change: "change", metric: "catalog", ask: "ask" };
const STAGE_LINK: Record<string, string> = { collect: "/settings/datasources", lineage: "/lineage", change: "/changes", metric: "/catalog/metrics", ask: "/analysis" };
const TODO_STATUS: Record<string, string> = { review: t("已阻止"), approve: t("待批准"), join_path: t("待批准"), import: t("待确认") };

export function Workbench() {
  const wb = useWorkbench();
  const navigate = useNavigate();
  const tz = useTimeZone();
  const roles = useRoles();
  const [scope, setScope] = useState<"todo" | "blocked">("todo");
  const canCreate = roles.has("steward") || roles.has("admin");
  return (
    <div className="av-fill av-workbench">
      <div className="av-pagehd">
        <div className="av-pagehd__title"><h1>{t("工作台")}</h1></div>
        <div className="av-actions">
          <Link to="/lineage/import" className="av-btn av-btn--secondary">{t("导入血缘")}</Link>
          {canCreate ? <Link to="/catalog/metrics?create=1" className="av-btn av-btn--primary">{t("新建指标")}</Link>
            : <Link to="/analysis" className="av-btn av-btn--primary">{t("问数")}</Link>}
        </div>
      </div>

      <QueryState query={wb}>{(d) => { const maxRuns = Math.max(1, ...d.cadence.map((c) => Number(c.runs) || 0));
        const todayRuns = d.cadence.reduce((sum, c) => sum + (Number(c.runs) || 0), 0);
        const changedHours = d.cadence.filter((c) => c.changed).length; return <>
      <RevealGroup as="nav" className="av-stages" aria-label={t("治理闭环")}>
        {d.stages.map((s) => {
          const [tone] = statusOf(s.status);
          return (
            <MotionLink key={s.key} to={STAGE_LINK[s.key]} className={`av-stage tone-${STAGE_MOD[s.key] ?? "collect"}`} variants={revealItem}>
              <span className="av-stage__hd"><Tile icon={STAGE_ICON[s.key] ?? "chart"} tone={STAGE_MOD[s.key] ?? "collect"} size="sm" />{s.label}</span>
              <span className="av-stage__val"><CountUp value={s.value} /><small>{s.unit}</small></span>
              <span className={`av-status${tone ? ` av-status--${tone}` : ""}`}>{s.note}</span>
            </MotionLink>
          );
        })}
      </RevealGroup>

      <motion.section aria-label={t("工作区")} className="av-workspace av-workbench__workspace"
        initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.42, delay: 0.18 }}>
        <div className="av-workbench__col">
          <div className="av-pane av-workbench__card">
              <div className="av-pane-hd">
                <h2>{scope === "todo" ? t("待办") : t("被拦截的提问")} <span className="av-count">{scope === "todo" ? d.todos.length : d.blocked_answers.length}</span></h2>
                <Seg value={scope} onChange={setScope} label={t("范围")} options={[{ value: "todo", label: t("待我处理") }, { value: "blocked", label: t("被拦截的提问") }]} />
              </div>
              <div className="av-pane__body">
              {scope === "todo" ? (
                <div className="av-list">
                  {d.todos.map((todo, i) => (
                    <Link key={i} to={todo.link} className="av-row av-workbench__todo">
                      <span className={`av-status av-status--${todo.status}`}>{TODO_STATUS[todo.kind] ?? todo.kind}</span>
                      <span className="av-row__main"><span className="av-row__title">{todo.title}</span><span className="av-row__meta">{todo.meta}</span></span>
                      <span className="av-row__meta av-mono">{todo.ref}</span>
                      <span className="av-row__meta av-mono">{formatClock(todo.at, tz)}</span>
                      {todo.kind === "review" ? <span className="av-row__end"><span className="av-btn av-btn--sm av-btn--primary">{t("处理")}</span></span> : null}
                    </Link>
                  ))}
                  {!d.todos.length ? <Empty compact icon="check" text={t("没有待办")} action={<Link to="/changes" className="av-btn av-btn--secondary av-btn--sm">{t("查看变更")}</Link>} /> : null}
                </div>
              ) : (
                <div className="av-list">
                  {d.blocked_answers.map((a) => (
                    <Link key={a.id} to={a.thread_id ? `/ask/${a.thread_id}` : "/analysis"} className="av-row av-workbench__blocked">
                      <Status value={a.status} />
                      <span className="av-row__main"><span className="av-row__title">{a.question}</span><span className="av-row__meta">{a.message}</span></span>
                      <span className="av-row__meta av-mono">{formatClock(a.asked_at, tz)}</span>
                    </Link>
                  ))}
                  {!d.blocked_answers.length ? <Empty compact text={t("没有被拦截的提问")} action={<Link to="/analysis" className="av-btn av-btn--secondary av-btn--sm">{t("开始问数")}</Link>} /> : null}
                </div>
              )}
              </div>
          </div>
          <div className="av-pane av-workbench__card av-workbench__card--metrics">
              <div className="av-pane-hd"><h2>{t("我负责的指标")} <span className="av-count">{d.my_metrics.length}</span></h2>
                <Link to="/catalog/metrics" className="av-btn av-btn--ghost av-btn--sm">{t("全部")}</Link></div>
              {d.my_metrics.length ? <div className="av-workbench__parts"><Parts label={t("口径状态构成")}
                parts={statusParts(d.my_metrics.map((m) => String(m.status)), (status) => statusOf(status)[1], t("其他"))} /></div> : null}
              <div className="av-pane__body">
                <table className="av-table av-workbench__metrics">
                  <thead><tr><th>{t("指标")}</th><th>{t("版本")}</th><th>{t("状态")}</th><th>{t("粒度")}</th><th className="num">{t("本周问数")}</th></tr></thead>
                  <tbody>{d.my_metrics.map((m) => (
                    <tr key={String(m.id)} className="is-link" onClick={(e) => { if (!(e.target as Element).closest("a")) navigate(`/catalog/metrics/${String(m.id)}`); }}>
                      <td><span className="av-cell"><Tile icon="chart" tone="catalog" size="sm" /><Link to={`/catalog/metrics/${String(m.id)}`}>{String(m.name)}</Link></span></td>
                      <td>{m.version_no ? <span className="av-tag">V{String(m.version_no)}</span> : null}</td>
                      <td><Status value={String(m.status)} /></td>
                      <td className="av-mono">{(m.grain as string[]).join(" × ")}</td>
                      <td className="num">{String(m.asks_week)}</td>
                    </tr>))}</tbody>
                </table>
                {!d.my_metrics.length ? <Empty compact icon="chart" text={t("你还没有负责的指标")} action={<div className="av-actions"><Link to="/catalog/metrics" className="av-btn av-btn--secondary av-btn--sm">{t("浏览指标")}</Link>{canCreate ? <Link to="/catalog/metrics?create=1" className="av-btn av-btn--primary av-btn--sm">{t("新建指标")}</Link> : null}</div>} /> : null}
              </div>
          </div>
        </div>

        <div className="av-pane av-pane--rail">
          <div className="av-pane__body">
            <div className="av-section">
              <div className="av-pane-hd"><h2>{t("数据源")} <span className="av-count">{d.datasources.length}</span></h2></div>
              <div className="av-list">
                {d.datasources.map((s) => (
                  <Link key={String(s.id)} to={`/settings/datasources/${String(s.id)}`} className="av-row av-workbench__source">
                    <Plat kind={String(s.kind)} /><span className="av-row__main"><span className="av-row__title">{String(s.name)}</span><span className="av-row__meta">{t("每 {{count}} 分钟", { count: Number(s.interval_minutes) })} · {s.last_run_at ? formatClock(s.last_run_at as string, tz) : t("尚未采集")}</span>
                      <RunStrip id={String(s.id)} count={20} tz={tz} /></span>
                    <span className="av-row__end"><Status value={String(s.status) === "ok" ? "ok" : String(s.status)} /></span>
                  </Link>
                ))}
                {!d.datasources.length ? <Empty compact text={t("尚未配置数据源")} action={<Link to="/settings/datasources" className="av-btn av-btn--secondary av-btn--sm">{t("查看数据源")}</Link>} /> : null}
              </div>
            </div>
            <div className="av-section">
              <div className="av-hero-card">
                <div className="av-hero-card__hd"><span>{t("采集节奏")}</span><span className="av-tag">{t("今日")}</span></div>
                <div className="av-hero-card__val"><CountUp value={todayRuns} /><small>{t("次采集")}</small></div>
                <div className="av-hero-card__meta">{changedHours ? t("{{count}} 个小时发现变化", { count: changedHours }) : t("今日没有发现变化")}</div>
                <div className="av-heat" role="img" aria-label={t("今日每小时的采集")}>
                  {d.cadence.map((c) => (
                    <i key={Number(c.hour)} data-tip={t("{{hour}} 点：{{runs}} 次采集", { hour: String(c.hour), runs: String(c.runs) }) + (c.changed ? t("，发现变化") : "")}
                      data-level={c.future || !Number(c.runs) ? 0 : Math.max(1, Math.ceil((Number(c.runs) / maxRuns) * 4))}
                      className={c.future ? "is-future" : c.changed ? "is-change" : undefined} />
                  ))}
                </div>
                <div className="av-cadence-axis"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>
              </div>
            </div>
            <div className="av-section">
              <div className="av-pane-hd"><h2>{t("最近变更")}</h2><Link to="/changes" className="av-btn av-btn--ghost av-btn--sm">{t("全部")}</Link></div>
              <ol className="av-steps av-workbench__changes">
                {d.recent_changes.map((c) => (
                  <li key={String(c.id)} className={`av-step${Number(c.impacts) ? " is-blocked" : c.status === "resolved" ? " is-ok" : ""}`}>
                    <span className="av-step__dot" />
                    <Link to={`/changes/${String(c.id)}`} className="av-step__body av-workbench__change-link">
                      <span className="av-step__title">{String(c.asset)}</span>
                      <span className="av-step__meta">{String(c.kind)} · {Number(c.impacts) ? t("{{count}} 项影响", { count: Number(c.impacts) }) : t("不影响口径")} · {formatClock(c.at as string, tz)}</span>
                    </Link>
                  </li>
                ))}
              </ol>
              {!d.recent_changes.length ? <Empty compact text={t("暂无最近变更")} action={<Link to="/changes" className="av-btn av-btn--secondary av-btn--sm">{t("查看变更")}</Link>} /> : null}
            </div>
          </div>
        </div>
      </motion.section>
      </>; }}</QueryState>
    </div>
  );
}
