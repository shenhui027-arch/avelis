import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, Navigate, NavLink, Outlet, useLocation, useNavigate } from "react-router";

import { api, logout, token, unwrap } from "../api/client";
import { useAppSettings, useDatasources } from "../api/hooks";
import { Icon } from "../components/Icon";
import { LogoMark } from "../components/Logo";
import { Plat } from "../components/ui";

const NAV = [
  { to: "/", label: "工作台", icon: "home", end: true },
  { to: "/lineage", label: "血缘", icon: "lineage" },
  { to: "/catalog", label: "目录", icon: "catalog" },
  { to: "/changes", label: "变更", icon: "change" },
  { to: "/ask", label: "问数", icon: "ask" },
  { to: "/analysis", label: "分析", icon: "analysis" },
];

const CRUMBS: [RegExp, string[]][] = [
  [/^\/lineage\/import/, ["血缘", "导入"]],
  [/^\/lineage/, ["血缘"]],
  [/^\/catalog\/metrics/, ["目录", "指标"]],
  [/^\/catalog\/assets/, ["目录", "资产"]],
  [/^\/catalog\/domains/, ["目录", "业务域"]],
  [/^\/catalog\/joins/, ["目录", "关联路径"]],
  [/^\/changes/, ["变更"]],
  [/^\/ask/, ["问数"]],
  [/^\/settings\/datasources/, ["设置", "数据源"]],
  [/^\/settings\/models/, ["设置", "模型配置"]],
  [/^\/settings\/users/, ["设置", "用户"]],
  [/^\/$/, ["工作台"]],
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

function UserMenu() {
  const me = useMe();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
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
  if (!me.data) return null;
  const u = me.data;
  return (
    <div className="av-usermenu" ref={ref}>
      <button type="button" className="av-usermenu__trigger" aria-haspopup="menu" aria-expanded={open}
        onClick={() => setOpen(!open)}>
        <span className="av-avatar">{u.display_name.slice(0, 1)}</span>
        <span className="av-usermenu__name">{u.display_name}</span>
        <Icon name="chevron" small />
      </button>
      {open ? (
        <div className="av-menu" role="menu">
          <div className="av-menu__head">
            <b>{u.display_name}</b>
            <span className="av-mono">{u.username}</span>
            <span>{u.roles.map((r) => ROLE_LABEL[r] ?? r).join(" · ")}</span>
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

export function Shell() {
  const location = useLocation();
  const me = useMe();
  const settings = useAppSettings();
  const sources = useDatasources();
  if (!token.get()) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  // 首次登录或密码被重置：先设置新密码
  if (me.data?.must_change_password) return <Navigate to="/account/password" replace state={{ from: location.pathname }} />;
  const crumbs = CRUMBS.find(([re]) => re.test(location.pathname))?.[1] ?? [];
  const primary = sources.data?.find((d) => d.engine.capabilities.includes("query")) ?? sources.data?.[0];
  return (
    <div className="av-root av-app">
      <nav className="av-rail" aria-label="主导航">
        <Link to="/" className="av-rail__logo" aria-label="Avelis 工作台"><LogoMark size={20} /></Link>
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={n.end}>
            <Icon name={n.icon} />
            {n.label}
          </NavLink>
        ))}
        <div className="av-rail__spacer" />
        <NavLink to="/settings">
          <Icon name="settings" />
          设置
        </NavLink>
      </nav>
      <header className="av-top">
        <div className="av-crumb">
          Avelis
          {crumbs.map((c, i) => (i === crumbs.length - 1
            ? <span key={c}>/ <b>{c}</b></span> : <span key={c}>/ {c}</span>))}
        </div>
        <span style={{ marginLeft: "auto" }} />
        {primary ? (
          <Link className="av-pill" to={`/settings/datasources/${primary.id}`} title="数据源与最近一次采集">
            <Plat kind={primary.kind} />{primary.name}
            {primary.last_run ? <span className="av-mono">· {primary.last_run.status === "succeeded" ? "正常" : "失败"}</span> : null}
          </Link>
        ) : null}
        <span className="av-tag" title="业务时区">{settings.data?.business_timezone ?? ""}</span>
        <UserMenu />
      </header>
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
