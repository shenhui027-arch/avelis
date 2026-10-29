import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router";

import { Shell } from "./layout/Shell";
import { AskPage } from "./pages/ask/AskPage";
import { CatalogPage } from "./pages/catalog/CatalogPage";
import { ChangesPage } from "./pages/changes/ChangesPage";
import { ImportPage } from "./pages/lineage/ImportPage";
import { LineagePage } from "./pages/lineage/LineagePage";
import { Login } from "./pages/Login";
import { DatasourcesPage } from "./pages/settings/DatasourcesPage";
import { ModelSettings } from "./pages/settings/ModelSettings";
import { UsersPage } from "./pages/settings/UsersPage";
import { Workbench } from "./pages/Workbench";
import "./styles/tokens.css";
import "./styles/avelis-ui.css";
import "./styles/app.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 10_000 } },
});

const router = createBrowserRouter([
  { path: "/login", element: <Login /> },
  {
    path: "/",
    element: <Shell />,
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
      { path: "settings", element: <Navigate to="/settings/datasources" replace /> },
      { path: "settings/datasources", element: <DatasourcesPage /> },
      { path: "settings/datasources/:id", element: <DatasourcesPage /> },
      { path: "settings/models", element: <ModelSettings /> },
      { path: "settings/users", element: <UsersPage /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
