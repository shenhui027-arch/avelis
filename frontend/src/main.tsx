import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import "@fontsource-variable/noto-sans-sc";
import { MotionConfig } from "motion/react";
import { lazy, StrictMode, Suspense, type ComponentType, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider, useLocation } from "react-router";

import { keepSessionAlive } from "./api/client";
import { TooltipLayer } from "./components/Tooltip";
import "./i18n";
import { RequireLogin, Shell } from "./layout/Shell";
import { Forbidden, NotFound, RouteError } from "./pages/PageState";
import "./theme";
import "./styles/tokens.css";
import "./styles/avelis-ui.css";
import "./styles/app.css";
import "./styles/pages.css";
import "./styles/kit.css";
import "./styles/dossier.css";
import "./styles/skills.css";
import "./styles/motion.css";

function lazyPage<T extends Record<string, unknown>, K extends keyof T>(
  loader: () => Promise<T>,
  name: K,
) {
  return lazy(() => loader().then((m) => ({ default: m[name] as ComponentType })));
}

const Login = lazyPage(() => import("./pages/Login"), "Login");
const ChangePassword = lazyPage(() => import("./pages/account/ChangePassword"), "ChangePassword");
const OAuthConsent = lazyPage(() => import("./pages/account/OAuthConsent"), "OAuthConsent");
const SecurityPage = lazyPage(() => import("./pages/account/SecurityPage"), "SecurityPage");
const Workbench = lazyPage(() => import("./pages/Workbench"), "Workbench");
const LineagePage = lazyPage(() => import("./pages/lineage/LineagePage"), "LineagePage");
const ImportPage = lazyPage(() => import("./pages/lineage/ImportPage"), "ImportPage");
const CatalogPage = lazyPage(() => import("./pages/catalog/CatalogPage"), "CatalogPage");
const ChangesPage = lazyPage(() => import("./pages/changes/ChangesPage"), "ChangesPage");
const SkillsPage = lazyPage(() => import("./pages/skills/SkillsPage"), "SkillsPage");
const AskPage = lazyPage(() => import("./pages/ask/AskPage"), "AskPage");
const AnalysisPage = lazyPage(() => import("./pages/analysis/AnalysisPage"), "AnalysisPage");
const DatasourcesPage = lazyPage(() => import("./pages/settings/DatasourcesPage"), "DatasourcesPage");
const ModelSettings = lazyPage(() => import("./pages/settings/ModelSettings"), "ModelSettings");
const UsersPage = lazyPage(() => import("./pages/settings/UsersPage"), "UsersPage");
const CalendarPage = lazyPage(() => import("./pages/settings/CalendarPage"), "CalendarPage");
const McpPage = lazyPage(() => import("./pages/settings/McpPage"), "McpPage");
const WebhooksPage = lazyPage(() => import("./pages/settings/WebhooksPage"), "WebhooksPage");
const AuthSettingsPage = lazyPage(() => import("./pages/settings/AuthSettingsPage"), "AuthSettingsPage");
const AuditCompliancePage = lazyPage(() => import("./pages/settings/AuditCompliancePage"), "AuditCompliancePage");

function standalone(element: ReactNode) {
  return <Suspense fallback={null}>{element}</Suspense>;
}

function AskRedirect() {
  const { search, hash } = useLocation();
  return <Navigate to={`/analysis${search}${hash}`} replace />;
}

export const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 10_000 } },
});

const router = createBrowserRouter([
  { path: "/login", element: standalone(<Login />) },
  { path: "/account/password", element: standalone(<RequireLogin><ChangePassword /></RequireLogin>) },
  // MCP 授权确认：未登录时先去登录页，登录后带着票据回到这里
  { path: "/oauth/consent", element: standalone(<RequireLogin><OAuthConsent /></RequireLogin>) },
  {
    path: "/",
    element: <Shell />,
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Workbench /> },
      { path: "lineage", element: <LineagePage /> },
      { path: "lineage/import", element: <ImportPage /> },
      { path: "lineage/import/:jobId", element: <ImportPage /> },
      { path: "catalog", element: <Navigate to="/catalog/metrics" replace /> },
      { path: "catalog/:section", element: <CatalogPage /> },
      { path: "catalog/:section/:id", element: <CatalogPage /> },
      { path: "changes", element: <ChangesPage /> },
      { path: "changes/:id", element: <ChangesPage /> },
      { path: "skills", element: <Navigate to="/skills/market" replace /> },
      { path: "skills/:section", element: <SkillsPage /> },
      // 问数入口统一到分析页（取数类问题在那里直接给出答案）；旧会话链接仍可查看
      { path: "ask", element: <AskRedirect /> },
      { path: "ask/:threadId", element: <AskPage /> },
      { path: "analysis", element: <AnalysisPage /> },
      { path: "analysis/runs/:runId", element: <AnalysisPage /> },
      { path: "analysis/:section", element: <AnalysisPage /> },
      { path: "settings", element: <Navigate to="/settings/datasources" replace /> },
      { path: "settings/datasources", element: <DatasourcesPage /> },
      { path: "settings/datasources/:id", element: <DatasourcesPage /> },
      { path: "settings/models", element: <ModelSettings /> },
      { path: "settings/users", element: <UsersPage /> },
      { path: "settings/calendar", element: <CalendarPage /> },
      { path: "settings/mcp", element: <McpPage /> },
      { path: "settings/webhooks", element: <WebhooksPage /> },
      { path: "settings/auth", element: <AuthSettingsPage /> },
      { path: "settings/audit", element: <AuditCompliancePage /> },
      { path: "account/security", element: <SecurityPage /> },
      { path: "forbidden", element: <Forbidden /> },
      { path: "*", element: <NotFound /> },
    ],
  },
]);

keepSessionAlive();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {/* 动效遵循系统“减少动态效果”；全局一条缓动曲线（与 motion.css 的 --ease-out 一致） */}
    <MotionConfig reducedMotion="user" transition={{ duration: 0.36, ease: [0.22, 1, 0.36, 1] }}>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
        <TooltipLayer />
      </QueryClientProvider>
    </MotionConfig>
  </StrictMode>,
);
