import { expect, type Page, type Route } from "@playwright/test";

export const ADMIN_USER = "admin";
export const ADMIN_PASSWORD = "Admin-e2e-password-2026!";

export function trackPageErrors(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(`console.error: ${msg.text()}`);
  });
  page.addInitScript(() => {
    window.addEventListener("securitypolicyviolation", (e) => {
      (window as unknown as { __cspViolations?: string[] }).__cspViolations ??= [];
      (window as unknown as { __cspViolations: string[] }).__cspViolations.push(
        `${e.violatedDirective}:${e.blockedURI}`,
      );
    });
  });
  return errors;
}

export async function assertCleanRuntime(page: Page, errors: string[]) {
  const csp = await page.evaluate(
    () => (window as unknown as { __cspViolations?: string[] }).__cspViolations ?? [],
  );
  expect(csp, "Expected zero CSP violations").toEqual([]);
  expect(errors, "Expected zero console/page errors").toEqual([]);
}

export async function setThemeAndLang(
  page: Page,
  theme: "light" | "dark" = "light",
  lang: "zh-CN" | "zh-TW" | "en" = "zh-CN",
) {
  await page.addInitScript(
    ({ th, lg }) => {
      localStorage.setItem("avelis.theme", th);
      localStorage.setItem("avelis.lang", lg);
    },
    { th: theme, lg: lang },
  );
}

export async function jsonOk(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

export function emptyWorkbench() {
  return {
    stages: [
      { key: "collect", label: "采集", value: 0, unit: "个", status: "empty", note: "未接入" },
      { key: "lineage", label: "血缘", value: 0, unit: "条", status: "empty", note: "未解析" },
      { key: "change", label: "变更", value: 0, unit: "项", status: "ok", note: "无待复核" },
      { key: "metric", label: "指标", value: 0, unit: "个", status: "empty", note: "未发布" },
      { key: "ask", label: "问数", value: 0, unit: "次", status: "empty", note: "未开始" },
    ],
    datasources: [],
    my_metrics: [],
    recent_changes: [],
    todos: [],
    blocked_answers: [],
    cadence: [],
  };
}

export async function stubShellDefaults(page: Page) {
  await page.route("**/api/settings", (route) =>
    jsonOk(route, {
      business_timezone: "Asia/Shanghai",
      self_review_policy: "allow",
      number_style: "cn",
    }),
  );
  await page.route("**/api/datasources", (route) => {
    if (route.request().method() === "GET") return jsonOk(route, []);
    return route.continue();
  });
  await page.route("**/api/datasources/*/runs*", (route) => jsonOk(route, []));
  await page.route("**/api/engines", (route) =>
    jsonOk(route, [
      {
        id: "starrocks",
        label: "StarRocks",
        capabilities: ["collect", "query"],
        options: [],
      },
    ]),
  );
  await page.route("**/api/analysis/tasks", (route) => {
    if (route.request().method() === "GET") return jsonOk(route, []);
    return route.continue();
  });
  await page.route("**/api/analysis/runs", (route) => {
    if (route.request().method() === "GET") return jsonOk(route, []);
    return route.continue();
  });
  await page.route("**/api/analysis/runs/*/stream*", (route) =>
    route.fulfill({
      status: 200,
      headers: { "Content-Type": "text/event-stream; charset=utf-8" },
      body: ": keepalive\n\n",
    }),
  );
  await page.route((url) => url.pathname === "/api/analysis/conversations", (route) =>
    jsonOk(route, []),
  );
  await page.route((url) => url.pathname.startsWith("/api/analysis/conversations/"), (route) => {
    const id = route.request().url().split("/api/analysis/conversations/")[1]?.split("?")[0] ?? "conv";
    return jsonOk(route, { id, title: "对话", latest_run_id: null, runs: [] });
  });
  await page.route("**/api/analysis/models", (route) =>
    jsonOk(route, {
      models: [
        {
          provider_id: "prov-1",
          provider_name: "OpenAI",
          model_id: "gpt-5",
          efforts: [
            { value: "low", label: "快速" },
            { value: "medium", label: "均衡" },
            { value: "high", label: "深度" },
          ],
          context_window: 128000,
          context_window_source: "config",
        },
      ],
      default: { provider_id: "prov-1", model_id: "gpt-5", reasoning_effort: "medium" },
    }),
  );
  await page.route("**/api/notifications*", (route) => jsonOk(route, []));
  await page.route("**/api/catalog/counts", (route) =>
    jsonOk(route, {
      domains: 1,
      assets: 0,
      metrics: 0,
      join_paths: 0,
      change_sets: 0,
      review_tasks: 0,
    }),
  );
  await page.route("**/api/domains", (route) =>
    jsonOk(route, [{ id: "e2e-domain-1", name: "Default", code: "default", description: "" }]),
  );
  await page.route("**/api/metrics", (route) => {
    if (route.request().method() === "GET") return jsonOk(route, []);
    return route.continue();
  });
  await page.route("**/api/assets*", (route) => {
    if (route.request().method() === "GET") return jsonOk(route, []);
    return route.continue();
  });
  await page.route("**/api/example-questions*", (route) => jsonOk(route, { questions: [] }));
  await page.route((url) => url.pathname.startsWith("/api/skills"), (route) => {
    if (route.request().method() === "GET") return jsonOk(route, []);
    return route.continue();
  });
  await page.route("**/api/change-sets*", (route) => jsonOk(route, []));
  await page.route("**/api/review-tasks*", (route) => jsonOk(route, []));
  await page.route("**/api/workbench", (route) => jsonOk(route, emptyWorkbench()));
}

export async function fastLogin(
  page: Page,
  username = ADMIN_USER,
  password = ADMIN_PASSWORD,
) {
  await page.goto("/login");
  await page.locator('input[autocomplete="username"]').fill(username);
  await page.locator('input[autocomplete="current-password"]').fill(password);
  await page.locator("button.av-login__submit").click();
  await expect(page).toHaveURL(/\/($|\?)/);
  await expect(page.locator("nav.av-sb, aside.av-sb")).toBeVisible();
}
