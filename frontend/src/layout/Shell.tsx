import { useQuery } from "@tanstack/react-query";
import { Link, Navigate, NavLink, Outlet, useLocation, useNavigate } from "react-router";

import { api, logout, token, unwrap } from "../api/client";
import { useAppSettings, useDatasources } from "../api/hooks";
import { Icon } from "../components/Icon";
import { Plat } from "../components/ui";

const NAV = [
  { to: "/", label: "工作台", icon: "home", end: true },
  { to: "/lineage", label: "血缘", icon: "lineage" },
  { to: "/catalog", label: "目录", icon: "catalog" },
  { to: "/changes", label: "变更", icon: "change" },
  { to: "/ask", label: "问数", icon: "ask" },
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

export function Shell() {
  const location = useLocation();
  const navigate = useNavigate();
  const me = useMe();
  const settings = useAppSettings();
  const sources = useDatasources();
  if (!token.get()) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  const crumbs = CRUMBS.find(([re]) => re.test(location.pathname))?.[1] ?? [];
  const primary = sources.data?.find((d) => d.engine.capabilities.includes("query")) ?? sources.data?.[0];
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
        {me.data ? (
          <span className="av-crumb" title={me.data.roles.join("、")}>
            <span className="av-avatar">{me.data.display_name.slice(0, 1)}</span>
            {me.data.display_name}
          </span>
        ) : null}
        <button className="av-btn av-btn--ghost av-btn--sm av-btn--icon" type="button" title="退出登录"
          aria-label="退出登录" onClick={async () => { await logout(); navigate("/login"); }}>
          <Icon name="logout" />
        </button>
      </header>
      <main className="av-main">
        <Outlet />
      </main>
    </div>
  );
}

export function SettingsTabs({ current }: { current: "datasources" | "models" | "users" }) {
  const navigate = useNavigate();
  const items = [["datasources", "数据源"], ["models", "模型配置"], ["users", "用户"]] as const;
  return (
    <div className="av-seg" role="group" aria-label="设置分区">
      {items.map(([key, label]) => (
        <button key={key} type="button" aria-pressed={key === current} onClick={() => navigate(`/settings/${key}`)}>
          {label}
        </button>
      ))}
    </div>
  );
}
