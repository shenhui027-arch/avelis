import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, Navigate, NavLink, Outlet, useLocation, useNavigate } from "react-router";

import { api, logout, token, unwrap } from "../api/client";
import { useAnalysisRuns, useAnalysisTasks, useAppSettings, useDatasources } from "../api/hooks";
import { Icon } from "../components/Icon";
import { Logo, LogoMark } from "../components/Logo";
import { Plat } from "../components/ui";
import { formatTime } from "../format";
import { setThemePref, useThemePref, type ThemePref } from "../theme";

// 侧边栏：品牌、数据源、新问题、主导航、我的（经验与定时任务）、最近对话，底部是设置、通知与用户菜单。
// 收起后只留图标（72px），偏好存在本机浏览器。

const NAV = [
  { to: "/", label: "工作台", icon: "home", end: true },
  { to: "/analysis", label: "问数", icon: "ask" },
  { to: "/lineage", label: "血缘", icon: "lineage" },
  { to: "/catalog", label: "目录", icon: "catalog" },
  { to: "/changes", label: "变更", icon: "change" },
];

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => unwrap(api.GET("/api/me")), enabled: !!token.get() });
}

export function useRoles(): Set<string> {
  return new Set(useMe().data?.roles ?? []);
}

/** 未登录时去登录页，并记住要访问的页面。 */
export function RequireLogin({ children }: { children: ReactNode }) {
  const location = useLocation();
  if (!token.get()) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  return <>{children}</>;
}

const ROLE_LABEL: Record<string, string> = { admin: "管理员", steward: "维护者", reviewer: "复核者", analyst: "查询者" };
const THEMES: [ThemePref, string][] = [["system", "跟随系统"], ["dark", "深色"], ["light", "浅色"]];

/** 点外部或按 Esc 关闭的弹出菜单。 */
function usePopover() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === "Escape" : !ref.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", close);
    return () => { window.removeEventListener("mousedown", close); window.removeEventListener("keydown", close); };
  }, [open]);
  return { open, setOpen, ref };
}

function UserMenu() {
  const me = useMe();
  const tz = useAppSettings().data?.business_timezone;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const theme = useThemePref();
  const { open, setOpen, ref } = usePopover();
  if (!me.data) return null;
  const u = me.data;
  return (
    <div className="av-usermenu av-sb__user" ref={ref}>
      <button type="button" className="av-sb__me" aria-haspopup="menu" aria-expanded={open} title={u.display_name}
        onClick={() => setOpen(!open)}>
        <span className="av-avatar">{u.display_name.slice(0, 1)}</span>
        <span className="av-sb__label av-sb__who"><b>{u.display_name}</b>
          <span>{u.roles.map((r) => ROLE_LABEL[r] ?? r).join(" · ")}</span></span>
      </button>
      {open ? (
        <div className="av-menu av-menu--up" role="menu">
          <div className="av-menu__head">
            <b>{u.display_name}</b>
            <span className="av-mono">{u.username}</span>
            {tz ? <span>业务时区 {tz}</span> : null}
          </div>
          <div className="av-menu__group" role="group" aria-label="主题">
            {THEMES.map(([value, label]) => (
              <button key={value} type="button" role="menuitemradio" aria-checked={theme === value}
                className="av-menu__item" onClick={() => setThemePref(value)}>
                <Icon name={theme === value ? "check" : "palette"} />{label}
              </button>
            ))}
          </div>
          <button type="button" role="menuitem" className="av-menu__item"
            onClick={() => { setOpen(false); navigate("/account/password"); }}>
            <Icon name="lock" />修改密码
          </button>
          <button type="button" role="menuitem" className="av-menu__item"
            onClick={async () => {
              await logout();
              // 先取消进行中的查询再清缓存：下一个登录的账号不会看到上一个账号的数据
              await queryClient.cancelQueries();
              queryClient.clear();
              navigate("/login", { replace: true });
            }}>
            <Icon name="logout" />退出登录
          </button>
        </div>
      ) : null}
    </div>
  );
}

/** 站内通知（定时分析完成、失败、任务暂停及原因）：未读计数，点开后标为已读并跳到链接。 */
function NotificationBell() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { open, setOpen, ref } = usePopover();
  const list = useQuery({ queryKey: ["notifications"], queryFn: () => unwrap(api.GET("/api/notifications")),
    enabled: !!token.get(), refetchInterval: 60_000 });
  const items = list.data ?? [];
  const unread = items.filter((n) => !n.read_at).length;
  return (
    <div className="av-usermenu av-notices" ref={ref}>
      <button type="button" className="av-sb__icon" aria-haspopup="menu" aria-expanded={open}
        aria-label={unread ? `通知（${unread} 条未读）` : "通知"} onClick={() => setOpen(!open)}>
        <Icon name="bell" />
        {unread ? <span className="av-notices__count">{unread > 99 ? "99+" : unread}</span> : null}
      </button>
      {open ? (
        <div className="av-menu av-menu--up" role="menu">
          {items.length === 0 ? <div className="av-menu__head">没有通知</div> : null}
          {items.slice(0, Math.max(30, unread)).map((n) => ( // 后端未读优先：未读的都能看到
            <button key={n.id} type="button" role="menuitem" className="av-menu__item av-notice"
              data-unread={!n.read_at} onClick={async () => {
                setOpen(false);
                if (!n.read_at) {
                  await api.POST("/api/notifications/{notification_id}/read",
                    { params: { path: { notification_id: n.id } } });
                  await queryClient.invalidateQueries({ queryKey: ["notifications"] });
                }
                if (n.link) navigate(n.link);
              }}>
              <b>{n.title}</b>
              {n.body ? <span>{n.body}</span> : null}
              <span>{formatTime(n.created_at)}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

const RUN_TONE: Record<string, string> = { queued: "flow", running: "flow", waiting: "warn", failed: "danger", clarify: "warn" };

function RecentRuns() {
  const runs = useAnalysisRuns();
  const location = useLocation();
  const items = runs.data ?? [];
  if (!items.length) return null;
  return (
    <>
      <div className="av-sb__sec av-sb__label">最近对话</div>
      <div className="av-sb__recent">
        {items.map((r) => {
          const tone = r.status === "succeeded" ? (r.verified ? "ok" : "warn") : RUN_TONE[r.status] ?? "";
          return (
            <Link key={r.id} to={`/analysis/runs/${r.id}`} className="av-sb__rc"
              aria-current={location.pathname === `/analysis/runs/${r.id}` ? "page" : undefined}>
              <span className={`av-dot${tone ? ` av-dot--${tone}` : ""}`} />
              <span>{r.status === "succeeded" ? r.title ?? r.question : r.question}</span>
            </Link>
          );
        })}
      </div>
    </>
  );
}

// 没有保存过偏好时，窄屏（1100px 及以下）默认收起
function readCollapsed() {
  try {
    const saved = localStorage.getItem("avelis.sidebar");
    if (saved) return saved === "collapsed";
  } catch {
    // 拿不到存储时按屏宽决定
  }
  return window.innerWidth <= 1100;
}

function Sidebar() {
  const navigate = useNavigate();
  const location = useLocation();
  const sources = useDatasources();
  const tasks = useAnalysisTasks();
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const primary = sources.data?.find((d) => d.engine.capabilities.includes("query")) ?? sources.data?.[0];
  const ok = primary?.last_run ? primary.last_run.status === "succeeded" : null;
  const toggle = () => {
    setCollapsed(!collapsed);
    try {
      localStorage.setItem("avelis.sidebar", collapsed ? "open" : "collapsed");
    } catch {
      // 拿不到存储时只在本次生效
    }
  };
  return (
    <nav className={`av-sb${collapsed ? " is-collapsed" : ""}`} aria-label="主导航">
      <div className="av-sb__brand">
        <Link to="/" aria-label="Avelis 工作台">{collapsed ? <LogoMark size={28} /> : <Logo size={28} />}</Link>
        <button type="button" className="av-sb__icon av-sb__toggle" onClick={toggle}
          aria-label={collapsed ? "展开侧边栏" : "收起侧边栏"} aria-expanded={!collapsed}>
          <Icon name="sidebar" />
        </button>
      </div>
      {primary ? (
        <Link className="av-sb__ws" to={`/settings/datasources/${primary.id}`} title="数据源与最近一次采集">
          <Plat kind={primary.kind} />
          <span className="av-sb__label">{primary.name}
            <small><span className={`av-dot${ok === null ? "" : ok ? " av-dot--ok" : " av-dot--danger"}`} />
              {ok === null ? "尚未采集" : ok ? "采集正常" : "采集失败"}</small></span>
        </Link>
      ) : null}
      <button type="button" className="av-sb__new" onClick={() => navigate("/analysis")} title="新问题">
        <Icon name="plus" /><span className="av-sb__label">新问题</span>
      </button>
      {NAV.map((n) => n.to === "/analysis" ? (
        // 问数下的经验与定时任务归“我的”，这里只在对话相关页面高亮
        <Link key={n.to} to={n.to} className="av-sb__nv" title={n.label}
          aria-current={/^\/analysis\/?(runs(\/.*)?)?$/.test(location.pathname) ? "page" : undefined}>
          <Icon name={n.icon} /><span className="av-sb__label">{n.label}</span>
        </Link>
      ) : (
        <NavLink key={n.to} to={n.to} end={n.end} className="av-sb__nv" title={n.label}>
          <Icon name={n.icon} /><span className="av-sb__label">{n.label}</span>
        </NavLink>
      ))}
      <div className="av-sb__sec av-sb__label">我的</div>
      <NavLink to="/analysis/experiences" className="av-sb__nv" title="个人经验">
        <Icon name="chip" /><span className="av-sb__label">个人经验</span>
      </NavLink>
      <NavLink to="/analysis/tasks" className="av-sb__nv" title="定时任务">
        <Icon name="refresh" /><span className="av-sb__label">定时任务</span>
        {tasks.data?.length ? <i className="av-sb__label">{tasks.data.length}</i> : null}
      </NavLink>
      {collapsed ? null : <RecentRuns />}
      <span className="av-sb__spacer" />
      <NavLink to="/settings" className="av-sb__nv" title="设置">
        <Icon name="settings" /><span className="av-sb__label">设置</span>
      </NavLink>
      <div className="av-sb__foot">
        <UserMenu />
        <NotificationBell />
      </div>
    </nav>
  );
}

export function Shell() {
  const location = useLocation();
  const me = useMe();
  if (!token.get()) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  // 首次登录或密码被重置：先设置新密码
  if (me.data?.must_change_password) return <Navigate to="/account/password" replace state={{ from: location.pathname }} />;
  return (
    <div className="av-root av-app">
      <Sidebar />
      <main className="av-main">
        <Outlet />
      </main>
    </div>
  );
}

export function SettingsTabs({ current }: { current: "datasources" | "models" | "users" }) {
  const items = [["datasources", "数据源"], ["models", "模型配置"], ["users", "用户"]] as const;
  // 设置分区是路由导航：用链接与 aria-current，而不是按钮组
  return (
    <nav className="av-seg" aria-label="设置分区">
      {items.map(([key, label]) => (
        <Link key={key} to={`/settings/${key}`} aria-current={key === current ? "page" : undefined}
          >{label}</Link>
      ))}
    </nav>
  );
}
