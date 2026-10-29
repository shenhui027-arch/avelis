import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router";

import { Shell } from "./layout/Shell";
import { Login } from "./pages/Login";
import { Placeholder } from "./pages/Placeholder";
import { ModelSettings } from "./pages/settings/ModelSettings";
import "./styles/tokens.css";
import "./styles/avelis-ui.css";
import "./styles/app.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 15_000 } },
});

const router = createBrowserRouter([
  { path: "/login", element: <Login /> },
  {
    path: "/",
    element: <Shell />,
    children: [
      { index: true, element: <Placeholder title="工作台" /> },
      { path: "lineage", element: <Placeholder title="血缘" /> },
      { path: "catalog", element: <Placeholder title="目录" /> },
      { path: "changes", element: <Placeholder title="变更" /> },
      { path: "ask", element: <Placeholder title="问数" /> },
      { path: "settings", element: <Navigate to="/settings/models" replace /> },
      { path: "settings/models", element: <ModelSettings /> },
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
