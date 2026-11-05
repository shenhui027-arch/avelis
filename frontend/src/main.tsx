import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import "@fontsource-variable/noto-sans-sc";
import { MotionConfig } from "motion/react";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router";

import { keepSessionAlive } from "./api/client";
import { TooltipLayer } from "./components/Tooltip";
import "./i18n";
import { RequireLogin, Shell } from "./layout/Shell";
import { ChangePassword } from "./pages/account/ChangePassword";
import { OAuthConsent } from "./pages/account/OAuthConsent";
import { AskPage } from "./pages/ask/AskPage";
import { AnalysisPage } from "./pages/analysis/AnalysisPage";
import { CatalogPage } from "./pages/catalog/CatalogPage";
import { ChangesPage } from "./pages/changes/ChangesPage";
import { ImportPage } from "./pages/lineage/ImportPage";
import { LineagePage } from "./pages/lineage/LineagePage";
import { Login } from "./pages/Login";
import { Forbidden, NotFound, RouteError } from "./pages/PageState";
import { DatasourcesPage } from "./pages/settings/DatasourcesPage";
import { ModelSettings } from "./pages/settings/ModelSettings";
import { UsersPage } from "./pages/settings/UsersPage";
import { CalendarPage } from "./pages/settings/CalendarPage";
import { McpPage } from "./pages/settings/McpPage";
import { WebhooksPage } from "./pages/settings/WebhooksPage";
import { Workbench } from "./pages/Workbench";
import "./theme";
import "./styles/tokens.css";
import "./styles/avelis-ui.css";
import "./styles/app.css";
import "./styles/pages.css";
import "./styles/motion.css";

export const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 10_000 } },
});

const router = createBrowserRouter([
  { path: "/login", element: <Login /> },
  { path: "/account/password", element: <RequireLogin><ChangePassword /></RequireLogin> },
  // MCP 授权确认：未登录时先去登录页，登录后带着票据回到这里
  { path: "/oauth/consent", element: <RequireLogin><OAuthConsent /></RequireLogin> },
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
      { path: "ask", element: <AskPage /> },
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
