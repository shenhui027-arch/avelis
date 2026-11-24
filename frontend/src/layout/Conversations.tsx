// 左侧“最近对话”与“已归档”：一段对话一项（续问都在同一项里）。右键或悬停的“⋯”打开菜单：重命名、归档、删除；
// 重命名只改显示名，不影响分析上下文；归档后底部提示可以撤销；删除先确认。已归档页可以打开、恢复或删除。
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from "react";
import { createPortal } from "react-dom";
import { Link, useLocation, useNavigate } from "react-router";

import { api, unwrap, type Schemas } from "../api/client";
import { errorText, useTimeZone } from "../api/hooks";
import { Icon } from "../components/Icon";
import { getEnabledMenuItems, handleMenuKeyDown, useOverlay } from "../components/overlay";
import { ConfirmDialog, Dot, Drawer, Empty, QueryState, statusOf, type Tone } from "../components/ui";
import { formatTime } from "../format";
import { t } from "../i18n";

type Conversation = Schemas["AnalysisConversationOut"] & { archived_at?: string | null };

const RUN_TONE: Record<string, Tone> = { running: "info", queued: "info", waiting: "warn", failed: "danger", stopped: "" };
const LIVE = new Set(["queued", "running", "waiting"]);

export const useConversations = (archived = false) => useQuery({
  queryKey: ["analysis-conversations", archived], refetchInterval: archived ? false : 15_000,
  queryFn: ({ signal }) => unwrap(api.GET("/api/analysis/conversations", { params: { query: { archived } }, signal })) as Promise<Conversation[]>,
});

const titleOf = (c: Conversation) => c.display_title || c.title || c.question;

/** 归档、恢复、删除、重命名：成功后刷新列表与对话。 */
function useConversationActions() {
  const qc = useQueryClient();
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["analysis-conversations"] });
    void qc.invalidateQueries({ queryKey: ["analysis-conversation"] });
    void qc.invalidateQueries({ queryKey: ["analysis-runs"] });
  };
  const call = async (p: Promise<unknown>) => { await p; refresh(); };
  return {
    rename: (id: string, title: string) => call(unwrap(api.PATCH("/api/analysis/conversations/{conversation_id}", {
      params: { path: { conversation_id: id } }, body: { display_title: title } }))),
    archive: (id: string) => call(unwrap(api.POST("/api/analysis/conversations/{conversation_id}/archive", {
      params: { path: { conversation_id: id } } }))),
    restore: (id: string) => call(unwrap(api.POST("/api/analysis/conversations/{conversation_id}/restore", {
      params: { path: { conversation_id: id } } }))),
    remove: (id: string) => call(unwrap(api.DELETE("/api/analysis/conversations/{conversation_id}", {
      params: { path: { conversation_id: id } } }))),
  };
}

type Menu = { c: Conversation; x: number; y: number };

/** 右键 / “⋯” 菜单，跟随鼠标位置，点外部或 Esc 关闭。 */
function ContextMenu({ menu, onClose, items }: {
  menu: Menu; onClose: () => void; items: { label: string; icon: string; danger?: boolean; disabled?: string; run: () => void }[];
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [opener] = useState(() => document.activeElement as HTMLElement | null);
  const closeAndRestore = () => {
    onClose();
    opener?.focus?.();
  };
  useOverlay(true, closeAndRestore);
  useEffect(() => {
    const enabled = getEnabledMenuItems(ref.current);
    (enabled[0] ?? ref.current)?.focus();
  }, []);
  useEffect(() => {
    const outside = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) onClose();
    };
    window.addEventListener("mousedown", outside);
    window.addEventListener("blur", onClose);
    return () => { window.removeEventListener("mousedown", outside); window.removeEventListener("blur", onClose); };
  }, [onClose]);
  const y = Math.min(menu.y, window.innerHeight - 140);
  return createPortal(
    <div ref={ref} className="av-root av-menu av-ctxmenu" role="menu" tabIndex={-1} style={{ left: menu.x, top: y }}
      onKeyDown={(e) => handleMenuKeyDown(e, ref.current, closeAndRestore)}>
      {items.map((it) => (
        <button key={it.label} type="button" role="menuitem" className={`av-menu__item${it.danger ? " is-danger" : ""}`}
          disabled={!!it.disabled} title={it.disabled} onClick={() => { onClose(); it.run(); }}>
          <Icon name={it.icon} small />{it.label}
        </button>
      ))}
    </div>, document.body);
}

/** 顶部提示，8 秒后消失（鼠标停在上面时暂停）；可以带一个“撤销”。 */
function Toast({ text, action, onDone }: { text: string; action?: { label: string; run: () => void }; onDone: () => void }) {
  const [paused, setPaused] = useState(false);
  const done = useRef(onDone);
  done.current = onDone;
  // 只随提示内容与暂停状态重新计时，列表刷新引起的重渲染不重置
  useEffect(() => {
    if (paused) return;
    const t = setTimeout(() => done.current(), 8000);
    return () => clearTimeout(t);
  }, [text, paused]);
  return createPortal(
    <div className="av-root av-toast" role="status" onMouseEnter={() => setPaused(true)} onMouseLeave={() => setPaused(false)}>
      <span>{text}</span>
      {action ? <button type="button" onClick={() => { action.run(); onDone(); }}>{action.label}</button> : null}
    </div>, document.body);
}

/** 左侧最近对话（未归档）。 */
export function RecentConversations() {
  const list = useConversations(false);
  const archived = useConversations(true);
  const location = useLocation();
  const navigate = useNavigate();
  const actions = useConversationActions();
  const [menu, setMenu] = useState<Menu | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<Conversation | null>(null);
  const [allOpen, setAllOpen] = useState(false);
  const [allQuery, setAllQuery] = useState("");
  const [toast, setToast] = useState<{ text: string; action?: { label: string; run: () => void } } | null>(null);
  const items = list.data ?? [];
  const currentRun = location.pathname.match(/^\/analysis\/runs\/([^/]+)/)?.[1];
  const isCurrent = (c: Conversation) => currentRun === c.latest_run_id || currentRun === c.id;
  const open = (e: ReactMouseEvent, c: Conversation) => { e.preventDefault(); e.stopPropagation(); setMenu({ c, x: e.clientX, y: e.clientY }); };
  const leaveIfCurrent = (c: Conversation) => { if (isCurrent(c)) navigate("/analysis"); };
  if (!items.length && !archived.data?.length) return null;
  const qLower = allQuery.trim().toLowerCase();
  const filteredAll = items.filter((c) => !qLower || titleOf(c).toLowerCase().includes(qLower) || c.question.toLowerCase().includes(qLower));
  return (
    <>
      <div className="av-sb__sec-row av-sb__label">
        <span>{t("最近对话")}</span>
        {items.length > 0 ? (
          <button type="button" className="av-sb__sec-link" onClick={() => { setAllQuery(""); setAllOpen(true); }}>
            {t("查看全部")}
          </button>
        ) : null}
      </div>
      <div className="av-sb__recent">
        {items.map((c) => {
          const tone: Tone = c.status === "succeeded" ? "ok" : RUN_TONE[c.status] ?? "";
          // 最近对话状态点：附带状态提示与读屏文字
          const stateLabel = c.status === "waiting" ? t("待确认") : statusOf(c.status)[1];
          return renaming === c.id ? (
            <RenameField key={c.id} initial={titleOf(c)} onCancel={() => setRenaming(null)}
              onSave={async (t) => { await actions.rename(c.id, t); setRenaming(null); }} />
          ) : (
            <div key={c.id} className="av-sb__rc-row" onContextMenu={(e) => open(e, c)}>
              <Link to={`/analysis/runs/${c.latest_run_id}`} className="av-sb__rc" aria-current={isCurrent(c) ? "page" : undefined}
                title={titleOf(c)}>
                <Dot tone={tone} label={stateLabel} className={LIVE.has(c.status) ? "is-live" : undefined} />
                <span>{titleOf(c)}</span>
              </Link>
              <button type="button" className="av-sb__rc-more" aria-label={t("{{title}} 的更多操作", { title: titleOf(c) })} onClick={(e) => open(e, c)}>
                <Icon name="more" small /></button>
            </div>
          );
        })}
        {archived.data?.length ? <Link to="/analysis/archived" className="av-sb__rc av-sb__archived"
          aria-current={location.pathname === "/analysis/archived" ? "page" : undefined}>
          <Icon name="archive" small /><span>{t("已归档 · {{count}}", { count: archived.data.length })}</span></Link> : null}
      </div>
      {menu ? <ContextMenu menu={menu} onClose={() => setMenu(null)} items={[
        { label: t("重命名"), icon: "edit", run: () => setRenaming(menu.c.id) },
        { label: t("归档"), icon: "archive", disabled: LIVE.has(menu.c.status) ? t("正在分析或等待确认，先停止再归档") : undefined,
          run: () => {
            const c = menu.c;
            void actions.archive(c.id).then(() => {
              leaveIfCurrent(c);
              setToast({ text: t("已归档“{{title}}”", { title: titleOf(c) }), action: { label: t("撤销"), run: () => void actions.restore(c.id) } });
            }).catch((e) => setToast({ text: errorText(e) }));
          } },
        { label: t("删除"), icon: "trash", danger: true, disabled: LIVE.has(menu.c.status) ? t("正在分析或等待确认，先停止再删除") : undefined,
          run: () => setDeleting(menu.c) },
      ]} /> : null}
      {deleting ? <DeleteDialog c={deleting} onCancel={() => setDeleting(null)} onDone={() => {
        leaveIfCurrent(deleting); setToast({ text: t("已删除“{{title}}”", { title: titleOf(deleting) }) }); setDeleting(null);
      }} remove={actions.remove} /> : null}
      {allOpen ? (
        <Drawer title={t("全部对话")} onClose={() => setAllOpen(false)}>
          <div className="av-stack">
            <div className="av-archived__bar">
              <span className="av-archived__search">
                <Icon name="search" small />
                <input
                  type="search"
                  value={allQuery}
                  placeholder={t("搜索对话…")}
                  aria-label={t("搜索对话…")}
                  onChange={(e) => setAllQuery(e.target.value)}
                />
              </span>
              {archived.data?.length ? (
                <Link to="/analysis/archived" className="av-btn av-btn--ghost av-btn--sm" onClick={() => setAllOpen(false)}>
                  <Icon name="archive" small />{t("已归档 · {{count}}", { count: archived.data.length })}
                </Link>
              ) : null}
            </div>
            {filteredAll.length === 0 ? <Empty text={t("没有匹配的对话")} compact /> : (
              <div className="av-analysis__list">
                {filteredAll.map((c) => (
                  <Link
                    key={c.id}
                    to={`/analysis/runs/${c.latest_run_id}`}
                    className="av-row av-archived__row"
                    onClick={() => setAllOpen(false)}
                  >
                    <span className="av-row__main">
                      <span className="av-row__title">{titleOf(c)}</span>
                      <span className="av-row__meta">{t("{{count}} 轮", { count: c.turn_count })}</span>
                    </span>
                    <Dot tone={c.status === "succeeded" ? "ok" : RUN_TONE[c.status] ?? ""} label={statusOf(c.status)[1]} />
                  </Link>
                ))}
              </div>
            )}
          </div>
        </Drawer>
      ) : null}
      {toast ? <Toast text={toast.text} action={toast.action} onDone={() => setToast(null)} /> : null}
    </>
  );
}

function DeleteDialog({ c, onCancel, onDone, remove }: {
  c: Conversation; onCancel: () => void; onDone: () => void; remove: (id: string) => Promise<void>;
}) {
  return <ConfirmDialog title={t("删除这段对话？")} danger confirm={t("删除")}
    body={t("“{{title}}”的 {{count}} 轮分析、过程记录和数据集会被永久删除，不能恢复。已保存的个人经验和提交的关联路径不受影响。", { title: titleOf(c), count: c.turn_count })}
    onCancel={onCancel} onConfirm={async () => { await remove(c.id); onDone(); }} />;
}

/** 原地重命名：回车保存，Esc 或失焦取消；留空恢复默认标题。 */
function RenameField({ initial, onSave, onCancel }: { initial: string; onSave: (t: string) => Promise<void>; onCancel: () => void }) {
  const [text, setText] = useState(initial);
  const [busy, setBusy] = useState(false);
  return (
    <form className="av-sb__rename" onSubmit={async (e) => {
      e.preventDefault();
      if (busy) return;
      setBusy(true);
      try { await onSave(text.trim()); } catch { setBusy(false); }
    }}>
      <input autoFocus value={text} maxLength={80} aria-label={t("对话名称")} disabled={busy}
        onFocus={(e) => e.currentTarget.select()} onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.nativeEvent.isComposing || e.keyCode === 229) return;
          if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onCancel(); }
        }} onBlur={() => { if (!busy) onCancel(); }} />
    </form>
  );
}

/** 已归档页：可以打开查看、恢复或删除。 */
export function ArchivedConversations() {
  const list = useConversations(true);
  const tz = useTimeZone();
  const actions = useConversationActions();
  const [q, setQ] = useState("");
  const [deleting, setDeleting] = useState<Conversation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const items = (list.data ?? []).filter((c) => titleOf(c).toLowerCase().includes(q.trim().toLowerCase()));
  return (
    <section className="av-archived">
      <div className="av-archived__bar">
        <span className="av-archived__search"><Icon name="search" small />
          <input type="search" value={q} placeholder={t("搜索已归档的对话")} aria-label={t("搜索已归档的对话")} onChange={(e) => setQ(e.target.value)} /></span>
      </div>
      {error ? <div className="av-banner av-banner--danger" role="alert">{error}</div> : null}
      {/* 已归档列表接入统一加载骨架与错误重试状态 */}
      <QueryState query={list} rows={3} isEmpty={(data) => data.length === 0}
        emptyText={t("没有已归档的对话。在左侧对话上右键可以归档。")}>
        {() => !items.length ? <Empty text={t("没有匹配的对话")} compact /> : (
          <div className="av-analysis__list">
            {items.map((c) => (
              <div key={c.id} className="av-row av-archived__row">
                <Link to={`/analysis/runs/${c.latest_run_id}`} className="av-row__main">
                  <span className="av-row__title">{titleOf(c)}</span>
                  <span className="av-row__meta">{t("{{count}} 轮 · 最近 {{time}}", { count: c.turn_count, time: formatTime(c.updated_at, tz) })}
                    {c.archived_at ? t(" · 归档于 {{time}}", { time: formatTime(c.archived_at, tz) }) : ""}</span>
                </Link>
                <span className="av-actions">
                  <button type="button" className="av-btn av-btn--secondary av-btn--sm"
                    onClick={() => actions.restore(c.id).catch((e) => setError(errorText(e)))}><Icon name="refresh" small />{t("恢复")}</button>
                  <button type="button" className="av-btn av-btn--ghost av-btn--sm av-archived__delete" onClick={() => setDeleting(c)}>
                    <Icon name="trash" small />{t("删除")}</button>
                </span>
              </div>
            ))}
          </div>
        )}
      </QueryState>
      {deleting ? <DeleteDialog c={deleting} onCancel={() => setDeleting(null)} onDone={() => setDeleting(null)} remove={actions.remove} /> : null}
    </section>
  );
}
