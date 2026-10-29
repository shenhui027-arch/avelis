// 工作台：页头 + 闭环条 + 工作区（待办、负责的指标、被拦截的提问 | 数据源、采集节奏、最近变更）。
import { Link } from "react-router";

import { useTimeZone, useWorkbench } from "../api/hooks";
import { Empty, Icon, Plat, QueryState, Seg, Status, statusOf } from "../components/ui";
import { formatTime } from "../format";
import { useRoles } from "../layout/Shell";
import { useState } from "react";

const STAGE_ICON: Record<string, string> = { collect: "db", lineage: "lineage", change: "change", metric: "chart", ask: "ask" };
const STAGE_LINK: Record<string, string> = { collect: "/settings/datasources", lineage: "/lineage", change: "/changes", metric: "/catalog/metrics", ask: "/ask" };
const TODO_STATUS: Record<string, string> = { review: "已阻止", approve: "待批准", join_path: "待批准", import: "待确认" };

export function Workbench() {
  const wb = useWorkbench();
  const tz = useTimeZone();
  const roles = useRoles();
  const [scope, setScope] = useState<"todo" | "blocked">("todo");
  const canCreate = roles.has("steward") || roles.has("admin");
  return (
    <div className="av-fill av-workbench">
      <div className="av-pagehd">
        <div className="av-pagehd__title"><h1>工作台</h1><span className="av-tag">{tz}</span></div>
        <div className="av-actions">
          <Link to="/lineage/import" className="av-btn av-btn--secondary">导入血缘</Link>
          <Link to="/settings/datasources" className="av-btn av-btn--secondary">数据源</Link>
          {canCreate ? <Link to="/catalog/metrics?create=1" className="av-btn av-btn--primary">新建指标</Link>
            : <Link to="/ask" className="av-btn av-btn--primary">问数</Link>}
        </div>
      </div>

      <QueryState query={wb}>{(d) => <>
      <nav className="av-stages" aria-label="治理闭环">
        {d.stages.map((s) => {
          const [tone] = statusOf(s.status);
          return (
            <Link key={s.key} to={STAGE_LINK[s.key]} className="av-stage">
              <span className="av-stage__hd"><Icon name={STAGE_ICON[s.key]} />{s.label}<Icon name="arrow" small /></span>
              <span className="av-stage__val">{s.value}<small>{s.unit}</small></span>
              <span className={`av-status${tone ? ` av-status--${tone}` : ""}`}>{s.note}</span>
            </Link>
          );
        })}
      </nav>

      <section aria-label="工作区" className="av-workspace av-workbench__workspace">
        <div className="av-pane">
          <div className="av-pane__body">
            <div className="av-section">
              <div className="av-pane-hd">
                <h2>{scope === "todo" ? "待办" : "被拦截的提问"} <span className="av-count">{scope === "todo" ? d.todos.length : d.blocked_answers.length}</span></h2>
                <Seg value={scope} onChange={setScope} label="范围" options={[{ value: "todo", label: "待我处理" }, { value: "blocked", label: "被拦截的提问" }]} />
              </div>
              {scope === "todo" ? (
                <div className="av-list">
                  {d.todos.map((t, i) => (
                    <Link key={i} to={t.link} className="av-row av-workbench__todo">
                      <span className={`av-status av-status--${t.status}`}>{TODO_STATUS[t.kind] ?? t.kind}</span>
                      <span className="av-row__main"><span className="av-row__title">{t.title}</span><span className="av-row__meta">{t.meta}</span></span>
                      <span className="av-row__meta av-mono">{t.ref}</span>
                      <span className="av-row__meta av-mono">{formatTime(t.at, tz).slice(11)}</span>
                      <span className="av-row__end"><span className={`av-btn av-btn--sm ${t.kind === "review" ? "av-btn--primary" : "av-btn--secondary"}`}>{t.kind === "review" ? "处理" : "查看"}</span></span>
                    </Link>
                  ))}
                  {!d.todos.length ? <Empty compact icon="check" text="没有待办" action={<Link to="/changes" className="av-btn av-btn--secondary av-btn--sm">查看变更</Link>} /> : null}
                </div>
              ) : (
                <div className="av-list">
                  {d.blocked_answers.map((a) => (
                    <Link key={a.id} to={a.thread_id ? `/ask/${a.thread_id}` : "/ask"} className="av-row av-workbench__blocked">
                      <Status value={a.status} />
                      <span className="av-row__main"><span className="av-row__title">{a.question}</span><span className="av-row__meta">{a.message}</span></span>
                      <span className="av-row__meta av-mono">{formatTime(a.asked_at, tz).slice(11)}</span>
                      <span className="av-row__end"><span className="av-btn av-btn--secondary av-btn--sm">查看</span></span>
                    </Link>
                  ))}
                  {!d.blocked_answers.length ? <Empty compact text="没有被拦截的提问" action={<Link to="/ask" className="av-btn av-btn--secondary av-btn--sm">开始问数</Link>} /> : null}
                </div>
              )}
            </div>
            <div className="av-section">
              <div className="av-pane-hd"><h2>我负责的指标 <span className="av-count">{d.my_metrics.length}</span></h2>
                <Link to="/catalog/metrics" className="av-btn av-btn--ghost av-btn--sm">目录</Link></div>
              <div className="av-table-wrap">
                <table className="av-table av-workbench__metrics">
                  <thead><tr><th>指标</th><th>版本</th><th>状态</th><th>粒度</th><th className="num">本周问数</th><th className="end"><span className="av-workbench__sr-only">操作</span></th></tr></thead>
                  <tbody>{d.my_metrics.map((m) => (
                    <tr key={String(m.id)}>
                      <td>{String(m.name)}</td>
                      <td>{m.version_no ? <span className="av-tag">V{String(m.version_no)}</span> : null}</td>
                      <td><Status value={String(m.status)} /></td>
                      <td className="av-mono">{(m.grain as string[]).join(" × ")}</td>
                      <td className="num">{String(m.asks_week)}</td>
                      <td className="end"><Link to={`/catalog/metrics/${String(m.id)}`} className="av-btn av-btn--secondary av-btn--sm">查看</Link></td>
                    </tr>))}</tbody>
                </table>
                {!d.my_metrics.length ? <Empty compact icon="chart" text="你还没有负责的指标" action={<div className="av-actions"><Link to="/catalog/metrics" className="av-btn av-btn--secondary av-btn--sm">浏览指标</Link>{canCreate ? <Link to="/catalog/metrics?create=1" className="av-btn av-btn--primary av-btn--sm">新建指标</Link> : null}</div>} /> : null}
              </div>
            </div>
          </div>
        </div>

        <div className="av-pane av-pane--rail">
          <div className="av-pane__body">
            <div className="av-section">
              <div className="av-pane-hd"><h2>数据源 <span className="av-count">{d.datasources.length}</span></h2></div>
              <div className="av-list">
                {d.datasources.map((s) => (
                  <Link key={String(s.id)} to={`/settings/datasources/${String(s.id)}`} className="av-row av-workbench__source">
                    <Plat kind={String(s.kind)} /><span className="av-row__main"><span className="av-row__title">{String(s.name)}</span><span className="av-row__meta">每 {String(s.interval_minutes)} 分钟 · {s.last_run_at ? formatTime(s.last_run_at as string, tz).slice(11) : "尚未采集"}</span></span>
                    <span className="av-row__end"><Status value={String(s.status) === "ok" ? "ok" : String(s.status)} /></span>
                  </Link>
                ))}
                {!d.datasources.length ? <Empty compact text="尚未配置数据源" action={<Link to="/settings/datasources" className="av-btn av-btn--secondary av-btn--sm">查看数据源</Link>} /> : null}
              </div>
            </div>
            <div className="av-section">
              <div className="av-pane-hd"><h2>采集节奏</h2><span className="av-tag">今日</span></div>
              <div className="av-workbench__cadence">
                <div className="av-cadence" aria-label="今日每小时的采集">
                  {d.cadence.map((c) => (
                    <i key={Number(c.hour)} title={`${String(c.hour)} 点：${String(c.runs)} 次采集${c.changed ? "，发现变化" : ""}`}
                      className={c.future ? "is-future" : c.changed ? "is-change" : !c.runs ? "is-idle" : undefined} />
                  ))}
                </div>
                <div className="av-cadence-axis"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>
              </div>
            </div>
            <div className="av-section">
              <div className="av-pane-hd"><h2>最近变更</h2><Link to="/changes" className="av-btn av-btn--ghost av-btn--sm">全部</Link></div>
              <ol className="av-steps av-workbench__changes">
                {d.recent_changes.map((c) => (
                  <li key={String(c.id)} className={`av-step${Number(c.impacts) ? " is-blocked" : c.status === "resolved" ? " is-ok" : ""}`}>
                    <span className="av-step__dot" />
                    <Link to={`/changes/${String(c.id)}`} className="av-step__body av-workbench__change-link">
                      <span className="av-step__title">{String(c.asset)}</span>
                      <span className="av-step__meta">{String(c.kind)} · {Number(c.impacts) ? `${String(c.impacts)} 项影响` : "不影响口径"} · {formatTime(c.at as string, tz).slice(11)}</span>
                    </Link>
                  </li>
                ))}
              </ol>
              {!d.recent_changes.length ? <Empty compact text="暂无最近变更" action={<Link to="/changes" className="av-btn av-btn--secondary av-btn--sm">查看变更</Link>} /> : null}
            </div>
          </div>
        </div>
      </section>
      </>}</QueryState>
    </div>
  );
}
