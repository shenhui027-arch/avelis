import { useQuery } from "@tanstack/react-query";
import { Navigate, NavLink, Outlet, useLocation, useNavigate } from "react-router";

import { api, logout, token, unwrap } from "../api/client";
import { Icon } from "../components/Icon";

const NAV = [
  { to: "/", label: "工作台", icon: "home", end: true },
  { to: "/lineage", label: "血缘", icon: "lineage" },
  { to: "/catalog", label: "目录", icon: "catalog" },
  { to: "/changes", label: "变更", icon: "change" },
  { to: "/ask", label: "问数", icon: "ask" },
];

const CRUMBS: Record<string, string[]> = {
  "/settings/models": ["设置", "模型配置"],
};

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => unwrap(api.GET("/api/me")), enabled: !!token.get() });
}

export function Shell() {
  const location = useLocation();
  const navigate = useNavigate();
  const me = useMe();
  const settings = useQuery({
    queryKey: ["settings"], enabled: !!token.get(),
    queryFn: () => unwrap(api.GET("/api/settings")) as Promise<{ business_timezone?: string }>,
  });
  if (!token.get()) return <Navigate to="/login" replace />;
  const crumbs = CRUMBS[location.pathname] ?? [NAV.find((n) => n.to === location.pathname)?.label ?? ""];
  return (
    <div className="av-root av-app">
      <nav className="av-rail" aria-label="主导航">
        <div className="av-rail__logo">A</div>
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={n.end}>
            <Icon name={n.icon} />
            {n.label}
          </NavLink>
        ))}
        <div className="av-rail__spacer" />
        <NavLink to="/settings/models">
          <Icon name="settings" />
          设置
        </NavLink>
      </nav>
      <header className="av-top">
        <div className="av-crumb">
          {crumbs.map((c, i) => (i === crumbs.length - 1 ? <b key={c}>{c}</b> : <span key={c}>{c} /</span>))}
        </div>
        <span className="av-caption av-mono" style={{ marginLeft: "auto" }} title="业务时区">
          {settings.data?.business_timezone ?? ""}
        </span>
        {me.data ? (
          <span className="av-crumb" title={me.data.roles.join("、")}>
            <span className="av-avatar">{me.data.display_name.slice(0, 1)}</span>
            {me.data.display_name}
          </span>
        ) : null}
        <button className="av-btn av-btn--ghost av-btn--sm av-btn--icon" type="button" title="退出登录"
          onClick={async () => { await logout(); navigate("/login"); }}>
          <Icon name="logout" />
        </button>
      </header>
      <main className="av-main">
        <Outlet />
      </main>
    </div>
  );
}
