import { expect, test } from "@playwright/test";

import {
  assertCleanRuntime,
  emptyWorkbench,
  fastLogin,
  jsonOk,
  setThemeAndLang,
  stubShellDefaults,
  trackPageErrors,
} from "./m2-helpers";

test.describe("M2B UX Closures, Client Resilience, CRUD & i18n", () => {
  test("B1: MetricDetail -> /analysis & /ask redirect preserve metric_id/version_id and preset Composer", async ({
    page,
  }) => {
    const errors = trackPageErrors(page);
    await setThemeAndLang(page, "light", "zh-CN");
    await stubShellDefaults(page);

    const metric = {
      id: "m-gmv",
      domain_id: "e2e-domain-1",
      name: "GMV",
      code: "gmv",
      aliases: ["成交总额"],
      description: "订单成交总额",
      owner: { id: "u-1", display_name: "Admin" },
      ai_context: { instructions: null, synonyms: [], examples: ["上月 GMV 环比变化"] },
      versions: [
        { id: "mv-2", version_no: 2, status: "published", created_at: "2026-10-01T00:00:00Z" },
      ],
    };
    const metricVersion = {
      id: "mv-2",
      metric_id: "m-gmv",
      version_no: 2,
      kind: "atomic",
      status: "published",
      description: "订单成交总额",
      created_at: "2026-10-01T00:00:00Z",
      published_at: "2026-10-02T00:00:00Z",
      owner: { id: "u-1", display_name: "Admin" },
      reviewed_by: { id: "u-2", display_name: "Steward" },
      base_asset: { id: "a-1", qualified: "dw.dws_order" },
      expression: "SUM(amt)",
      time_column: "dt",
      timezone: "Asia/Shanghai",
      time_format: "yyyy-mm-dd",
      data_grain: "day",
      time_aggregation: "sum",
      filters: null,
      dimensions: [],
      grain: ["day"],
      joins: [],
      dependency: [],
      unit: "元",
      unit_scale: 1,
      value_kind: "level",
      polarity: "up_good",
      check_result: null,
    };

    await page.route("**/api/metrics", (route) => jsonOk(route, [metric]));
    await page.route("**/api/metric-versions/mv-2", (route) => jsonOk(route, metricVersion));

    let capturedRunBody: Record<string, unknown> | null = null;
    await page.route("**/api/analysis/runs", async (route) => {
      if (route.request().method() === "POST") {
        capturedRunBody = JSON.parse(route.request().postData() ?? "{}");
        return jsonOk(route, { id: "run-b1", status: "queued", question: capturedRunBody?.question });
      }
      return jsonOk(route, []);
    });
    await page.route("**/api/analysis/runs/run-b1*", (route) =>
      jsonOk(route, {
        id: "run-b1",
        status: "queued",
        question: capturedRunBody?.question ?? "GMV",
        conversation_id: "conv-b1",
        created_at: "2026-10-10T00:00:00Z",
      }),
    );

    await fastLogin(page);

    // 1. Direct /ask?metric_id=...&version_id=... redirect preserves query params
    await page.goto("/ask?metric_id=m-gmv&version_id=mv-2");
    await expect(page).toHaveURL(/\/analysis\?metric_id=m-gmv&version_id=mv-2/);

    // 2. MetricDetail "问数" link preserves query params and presets Composer
    await page.goto("/catalog/metrics/m-gmv");
    await page.locator(".av-metric-detail").getByRole("link", { name: "问数" }).click();
    await expect(page).toHaveURL(/\/analysis\?metric_id=m-gmv&version_id=mv-2/);

    const pill = page.locator(".av-compose__metric");
    await expect(pill).toContainText("GMV");
    await expect(pill).toContainText("V2");
    await expect(page.getByRole("button", { name: "上月 GMV 环比变化" })).toBeVisible();

    await page.locator("#aq").fill("按区域拆分看趋势");
    await page.locator("button.av-compose__send").click();
    await expect(page).toHaveURL(/\/analysis\/runs\/run-b1/);
    expect(String(capturedRunBody?.question ?? "")).toContain("GMV");
    expect(String(capturedRunBody?.question ?? "")).toContain("按区域拆分看趋势");

    await assertCleanRuntime(page, errors);
  });

  test("B2: Failed & stopped analysis runs support one-click Retry and inline Edit-and-Resend", async ({
    page,
  }) => {
    const errors = trackPageErrors(page);
    await setThemeAndLang(page, "light", "zh-CN");
    await stubShellDefaults(page);

    const postedBodies: Array<Record<string, unknown>> = [];
    await page.route("**/api/analysis/runs/run-fail", (route) =>
      jsonOk(route, {
        id: "run-fail",
        status: "failed",
        question: "为什么华东GMV下降？",
        error: "timeout",
        confirmation_mode: "auto",
        skills: ["rev-skill"],
        model_choice: { provider_id: "prov-1", model_id: "gpt-5", reasoning_effort: "high" },
        datasets: [],
        steps: [],
        created_at: "2026-10-10T00:00:00Z",
      }),
    );
    await page.route("**/api/analysis/runs/run-stop", (route) =>
      jsonOk(route, {
        id: "run-stop",
        status: "stopped",
        question: "渠道转化分析",
        confirmation_mode: "always",
        datasets: [],
        steps: [],
        created_at: "2026-10-10T00:00:00Z",
      }),
    );
    await page.route("**/api/analysis/runs/run-retry-1", (route) =>
      jsonOk(route, {
        id: "run-retry-1",
        status: "queued",
        question: "为什么华东GMV下降？",
        datasets: [],
        steps: [],
        created_at: "2026-10-10T00:01:00Z",
      }),
    );
    await page.route("**/api/analysis/runs/run-retry-2", (route) =>
      jsonOk(route, {
        id: "run-retry-2",
        status: "queued",
        question: "渠道转化分析（按月）",
        datasets: [],
        steps: [],
        created_at: "2026-10-10T00:02:00Z",
      }),
    );
    await page.route("**/api/analysis/runs", async (route) => {
      if (route.request().method() === "POST") {
        const body = JSON.parse(route.request().postData() ?? "{}") as Record<string, unknown>;
        postedBodies.push(body);
        const nextId = postedBodies.length === 1 ? "run-retry-1" : "run-retry-2";
        return jsonOk(route, { id: nextId, status: "queued", question: body.question });
      }
      return jsonOk(route, []);
    });

    await fastLogin(page);

    // 1. Failed run one-click Retry
    await page.goto("/analysis/runs/run-fail");
    await expect(page.getByRole("button", { name: "重试" })).toBeVisible();
    await page.getByRole("button", { name: "重试" }).click();
    await expect(page).toHaveURL(/\/analysis\/runs\/run-retry-1/);
    expect(postedBodies[0]).toMatchObject({
      question: "为什么华东GMV下降？",
      skills: ["rev-skill"],
      reasoning_effort: "high",
    });

    // 2. Stopped run inline Edit-and-Resend
    await page.goto("/analysis/runs/run-stop");
    await page.getByRole("button", { name: "编辑重发" }).click();
    const editor = page.locator("#retry-run-stop");
    await expect(editor).toHaveValue("渠道转化分析");
    await editor.fill("渠道转化分析（按月）");
    await page.getByRole("button", { name: "重发", exact: true }).click();
    await expect(page).toHaveURL(/\/analysis\/runs\/run-retry-2/);
    expect(postedBodies[1]).toMatchObject({
      question: "渠道转化分析（按月）",
      confirmation_mode: "always",
    });

    await assertCleanRuntime(page, errors);
  });

  test("B3 & B4: LookupAnswer top-level Copy SQL, Export, Deepen + Code Copy button", async ({
    browser,
  }) => {
    const ctx = await browser.newContext({
      permissions: ["clipboard-read", "clipboard-write"],
    });
    const page = await ctx.newPage();
    const errors = trackPageErrors(page);
    await setThemeAndLang(page, "light", "zh-CN");
    await stubShellDefaults(page);

    const lookupSql = "SELECT SUM(amt) FROM dws_order";
    await page.route("**/api/analysis/runs/run-lookup", (route) =>
      jsonOk(route, {
        id: "run-lookup",
        status: "succeeded",
        question: "上月GMV是多少",
        report: { kind: "lookup" },
        datasets: [],
        steps: [],
        created_at: "2026-10-10T00:00:00Z",
        answer: {
          id: "ans-1",
          status: "answered",
          question: "上月GMV是多少",
          message: "上月GMV为 120 万元",
          sql: lookupSql,
          result: {
            columns: ["value"],
            rows: [[1200000]],
            units: { value: { kind: "level", unit: "元" } },
          },
          evidence: {},
          freshness: { tables: {} },
          steps: [],
        },
      }),
    );

    let deepenPayload: Record<string, unknown> | null = null;
    await page.route("**/api/analysis/runs/run-deep-1", (route) =>
      jsonOk(route, {
        id: "run-deep-1",
        status: "queued",
        question: String(deepenPayload?.question ?? "深入分析"),
        datasets: [],
        steps: [],
        created_at: "2026-10-10T00:01:00Z",
      }),
    );
    await page.route("**/api/analysis/runs", async (route) => {
      if (route.request().method() === "POST") {
        deepenPayload = JSON.parse(route.request().postData() ?? "{}");
        return jsonOk(route, { id: "run-deep-1", status: "queued" });
      }
      return jsonOk(route, []);
    });

    await fastLogin(page);
    await page.goto("/analysis/runs/run-lookup");

    // B3: Top-level Copy SQL, Export, Deepen visible before expanding <details>
    const lookupActions = page.locator(".av-lookup__actions");
    await expect(lookupActions.getByRole("button", { name: "复制 SQL" })).toBeVisible();
    await expect(lookupActions.getByRole("button", { name: "导出" })).toBeVisible();
    await expect(lookupActions.getByRole("button", { name: "深入分析" })).toBeVisible();

    await lookupActions.getByRole("button", { name: "复制 SQL" }).click();
    await expect(lookupActions.getByRole("button", { name: "已复制" })).toBeVisible();
    const clipTop = await page.evaluate(() => navigator.clipboard.readText());
    expect(clipTop).toContain("SUM(amt)");

    // Export CSV
    await lookupActions.getByRole("button", { name: "导出" }).click();
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      page.getByRole("menuitem", { name: "CSV" }).click(),
    ]);
    expect(download.suggestedFilename()).toBe("lookup-ans-1.csv");

    // B4: Code component Copy button inside Evidence SQL tab
    await page.locator("details.av-doc__more > summary").click();
    await page.getByRole("tab", { name: "SQL" }).click();
    const codeWrap = page.locator(".av-code-wrap").first();
    const codeCopyBtn = codeWrap.getByRole("button", { name: "复制 SQL" });
    await expect(codeCopyBtn).toBeVisible();
    await codeCopyBtn.click();
    await expect(codeWrap.getByRole("button", { name: "已复制" })).toBeVisible();
    const clipCode = await page.evaluate(() => navigator.clipboard.readText());
    expect(clipCode).toContain("FROM dws_order");

    // B3: Deepen triggers POST /api/analysis/runs and navigates
    await lookupActions.getByRole("button", { name: "深入分析" }).click();
    await expect(page).toHaveURL(/\/analysis\/runs\/run-deep-1/);
    expect(String(deepenPayload?.question ?? "")).toContain("上月GMV是多少");

    await assertCleanRuntime(page, errors);
    await ctx.close();
  });

  test("B5: Command Palette opens via Ctrl+K, preserves LIFO Esc above Drawer, filters & navigates", async ({
    page,
  }) => {
    const errors = trackPageErrors(page);
    await setThemeAndLang(page, "light", "zh-CN");
    await stubShellDefaults(page);

    await fastLogin(page);
    await page.goto("/settings/datasources");

    // Open Add Datasource Drawer first
    await page.getByRole("button", { name: "添加数据源" }).first().click();
    const dsDrawer = page.getByRole("dialog", { name: "添加数据源" });
    await expect(dsDrawer).toBeVisible();

    // Open Command Palette above Drawer via Ctrl+K
    await page.keyboard.press("Control+k");
    const cmdDlg = page.getByRole("dialog", { name: "命令面板" });
    await expect(cmdDlg).toBeVisible();
    const combo = cmdDlg.getByRole("combobox");
    await expect(combo).toBeFocused();

    // Single Esc closes ONLY Command Palette, leaving Drawer open
    await page.keyboard.press("Escape");
    await expect(cmdDlg).toHaveCount(0);
    await expect(dsDrawer).toBeVisible();

    // Second Esc closes Drawer
    await page.keyboard.press("Escape");
    await expect(dsDrawer).toHaveCount(0);

    // Re-open via Ctrl+K, filter "血缘", press Enter -> navigates to /lineage
    await page.keyboard.press("Control+k");
    await expect(cmdDlg).toBeVisible();
    await combo.fill("血缘");
    await expect(cmdDlg.getByRole("option").first()).toContainText(/血缘/);
    await page.keyboard.press("Enter");
    await expect(cmdDlg).toHaveCount(0);
    await expect(page).toHaveURL(/\/lineage/);

    await assertCleanRuntime(page, errors);
  });

  test("B6: Workbench cold-start onboarding checklist renders CTAs, dismisses cleanly, and hides when populated", async ({
    page,
  }) => {
    const errors = trackPageErrors(page);
    await setThemeAndLang(page, "light", "zh-CN");
    await stubShellDefaults(page);

    await fastLogin(page);

    const onboarding = page.locator("section.av-onboarding");
    await expect(onboarding).toBeVisible();
    await expect(onboarding.locator(".av-onboarding__step")).toHaveCount(4);
    await expect(onboarding.getByRole("link", { name: "前往接入" })).toHaveAttribute(
      "href",
      "/settings/datasources",
    );
    await expect(onboarding.getByRole("link", { name: "导入血缘" })).toHaveAttribute(
      "href",
      "/lineage/import",
    );

    // Dismiss persists in localStorage
    await onboarding.getByRole("button", { name: "收起引导" }).click();
    await expect(onboarding).toHaveCount(0);
    expect(await page.evaluate(() => localStorage.getItem("avelis.onboarding.dismissed"))).toBe(
      "1",
    );

    // Reset dismiss & verify populated workbench hides cold-start guide
    await page.evaluate(() => localStorage.removeItem("avelis.onboarding.dismissed"));
    const populated = emptyWorkbench();
    populated.datasources = [
      {
        id: "ds-1",
        name: "Prod",
        kind: "starrocks",
        status: "ok",
        interval_minutes: 60,
        last_run_at: "2026-10-01T00:00:00Z",
      },
    ];
    populated.my_metrics = [
      { id: "m-1", name: "GMV", version_no: 1, status: "published", grain: ["day"], asks_week: 5 },
    ];
    await page.route("**/api/workbench", (route) => jsonOk(route, populated));
    await page.reload();
    await expect(page.getByRole("heading", { name: "工作台" })).toBeVisible();
    await expect(page.locator("section.av-onboarding")).toHaveCount(0);

    await assertCleanRuntime(page, errors);
  });

  test("B7: Scheduled tasks CRUD (create with model/skills, per-row loading, edit, run history, delete)", async ({
    page,
  }) => {
    const errors = trackPageErrors(page);
    await setThemeAndLang(page, "light", "zh-CN");
    await stubShellDefaults(page);

    const tasks: Array<Record<string, unknown>> = [
      {
        id: "t-2",
        title: "存量留存周报",
        question: "每周留存变化",
        status: "active",
        schedule_cron: "0 9 * * 1",
        next_run_at: "2026-10-12T09:00:00Z",
        window: {},
        confirmation_mode: "auto",
        skills: [],
        model_choice: null,
        last_run_id: null,
        paused_reason: null,
      },
    ];

    await page.route((url) => url.pathname.startsWith("/api/skills"), (route) =>
      jsonOk(route, [
        { id: "sk-1", name: "rev-skill", description: "Revenue diagnostics", status: "listed", enabled: true },
      ]),
    );

    let releaseRunNow!: () => void;
    const runNowGate = new Promise<void>((res) => {
      releaseRunNow = res;
    });

    await page.route("**/api/analysis/tasks/*/run", async (route) => {
      await runNowGate;
      return jsonOk(route, { id: "run-now-1", status: "queued" });
    });
    await page.route("**/api/analysis/tasks/*/runs", (route) =>
      jsonOk(route, [
        {
          id: "run-hist-1",
          status: "succeeded",
          question: "每日GMV巡检",
          trigger: "schedule",
          created_at: "2026-10-09T09:00:00Z",
          finished_at: "2026-10-09T09:01:00Z",
          error: null,
        },
      ]),
    );
    await page.route("**/api/analysis/tasks/*", async (route) => {
      const url = route.request().url();
      const id = url.split("/api/analysis/tasks/")[1]?.split("?")[0];
      const method = route.request().method();
      if (method === "PATCH") {
        const body = JSON.parse(route.request().postData() ?? "{}") as Record<string, unknown>;
        const idx = tasks.findIndex((t) => t.id === id);
        if (idx >= 0) tasks[idx] = { ...tasks[idx], ...body };
        return jsonOk(route, tasks[idx]);
      }
      if (method === "DELETE") {
        const idx = tasks.findIndex((t) => t.id === id);
        if (idx >= 0) tasks.splice(idx, 1);
        return route.fulfill({ status: 204, body: "" });
      }
      return route.continue();
    });
    await page.route("**/api/analysis/tasks", async (route) => {
      const method = route.request().method();
      if (method === "GET") return jsonOk(route, [...tasks]);
      if (method === "POST") {
        const body = JSON.parse(route.request().postData() ?? "{}") as Record<string, unknown>;
        const created = {
          id: "t-1",
          title: body.title || String(body.question ?? "").slice(0, 60),
          question: body.question,
          status: "active",
          schedule_cron: body.schedule_cron ?? null,
          next_run_at: "2026-10-11T09:00:00Z",
          window: body.window ?? {},
          confirmation_mode: body.confirmation_mode ?? "auto",
          skills: body.skills ?? [],
          model_choice: body.model
            ? { ...(body.model as Record<string, unknown>), reasoning_effort: body.reasoning_effort ?? null }
            : null,
          last_run_id: null,
          paused_reason: null,
        };
        tasks.unshift(created);
        return jsonOk(route, created, 201);
      }
      return route.continue();
    });

    await fastLogin(page);
    await page.goto("/analysis");

    // 1. Pick skill, fill question, save as scheduled task with title
    await page.locator("#aq").fill("每日GMV巡检");
    await page.getByRole("button", { name: "引用技能" }).click();
    await page.getByRole("option", { name: /rev-skill/i }).click();
    await page.getByRole("button", { name: "定时" }).click();

    const createDlg = page.getByRole("dialog", { name: "保存为定时任务" });
    await expect(createDlg).toBeVisible();
    await createDlg.getByLabel("任务名称").fill("华东巡检");
    await createDlg.getByRole("button", { name: "保存" }).click();

    await expect(page).toHaveURL(/\/analysis\/tasks/);
    const rows = page.locator(".av-task");
    await expect(rows).toHaveCount(2);
    await expect(rows.first()).toContainText("华东巡检");
    await expect(rows.first()).toContainText("gpt-5");
    await expect(rows.first()).toContainText("rev-skill");

    // 2. Per-row loading on "立即运行"
    await rows.first().getByRole("button", { name: "立即运行" }).click();
    await expect(rows.first().getByRole("button", { name: "运行中…" })).toBeDisabled();
    await expect(rows.nth(1).getByRole("button", { name: "立即运行" })).toBeEnabled();
    releaseRunNow();
    await expect(rows.first().getByRole("button", { name: "立即运行" })).toBeEnabled();

    // 3. Edit task
    await rows.first().getByRole("button", { name: "编辑" }).click();
    const editDlg = page.getByRole("dialog", { name: "编辑定时任务" });
    await expect(editDlg).toBeVisible();
    await editDlg.getByLabel("任务名称").fill("华东巡检V2");
    await editDlg.getByRole("button", { name: "保存" }).click();
    await expect(rows.first()).toContainText("华东巡检V2");

    // 4. Run History Drawer
    await rows.first().getByRole("button", { name: "运行历史" }).click();
    const histDlg = page.getByRole("dialog", { name: /运行历史/ });
    await expect(histDlg).toBeVisible();
    await expect(histDlg.locator(".av-taskrun")).toHaveCount(1);
    await histDlg.getByRole("button", { name: "关闭" }).click();

    // 5. Delete + danger ConfirmDialog
    await rows.first().getByRole("button", { name: "删除" }).click();
    const confirmDlg = page.getByRole("alertdialog");
    await expect(confirmDlg).toBeVisible();
    await expect(confirmDlg.getByRole("button", { name: "取消" })).toBeFocused();
    await confirmDlg.getByRole("button", { name: "删除" }).click();
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText("存量留存周报");

    await assertCleanRuntime(page, errors);
  });

  test("B8: Datasources CRUD (test connection success/failure, edit, delete + confirm)", async ({
    page,
  }) => {
    const errors = trackPageErrors(page);
    await setThemeAndLang(page, "light", "zh-CN");
    await stubShellDefaults(page);

    const sources: Array<Record<string, unknown>> = [
      {
        id: "ds-1",
        name: "Prod StarRocks",
        host: "10.0.0.1",
        port: 9030,
        collect_interval_minutes: 60,
        options: {},
        engine: {
          id: "starrocks",
          label: "StarRocks",
          capabilities: ["collect", "query"],
          options: [],
        },
        server_identity: { cluster: "sr-prod" },
        aliases: [],
        credentials: [],
        last_run: null,
      },
    ];

    let testShouldSucceed = true;
    await page.route("**/api/datasources/ds-1/test", (route) => {
      if (testShouldSucceed) {
        return jsonOk(route, { ok: true, latency_ms: 12, server_identity: { version: "3.2" } });
      }
      return jsonOk(route, { ok: false, latency_ms: 8000, error: "Connection refused" });
    });
    await page.route("**/api/datasources/*", async (route) => {
      const url = route.request().url();
      if (url.endsWith("/test") || url.includes("/runs")) return route.continue();
      const method = route.request().method();
      if (method === "PATCH") {
        const body = JSON.parse(route.request().postData() ?? "{}") as Record<string, unknown>;
        sources[0] = { ...sources[0], ...body };
        return jsonOk(route, sources[0]);
      }
      if (method === "DELETE") {
        sources.splice(0, sources.length);
        return route.fulfill({ status: 204, body: "" });
      }
      return route.continue();
    });
    await page.route("**/api/datasources", (route) => {
      if (route.request().method() === "GET") return jsonOk(route, [...sources]);
      return route.continue();
    });

    await fastLogin(page);
    await page.goto("/settings/datasources/ds-1");

    // 1. Test connection success then failure
    await page.getByRole("button", { name: "测试连接" }).click();
    await expect(page.locator('.av-banner--ok[role="status"]')).toContainText("12 ms");

    testShouldSucceed = false;
    await page.getByRole("button", { name: "测试连接" }).click();
    await expect(page.locator('.av-banner--danger[role="alert"]')).toContainText(
      "Connection refused",
    );

    // 2. Edit datasource
    await page.getByRole("button", { name: "编辑" }).click();
    const editDrawer = page.getByRole("dialog", { name: "编辑数据源" });
    await expect(editDrawer).toBeVisible();
    await editDrawer.getByLabel("名称").fill("Prod StarRocks V2");
    await editDrawer.getByRole("button", { name: "保存" }).click();
    await expect(editDrawer).toHaveCount(0);
    await expect(page.locator(".av-datasources")).toContainText("Prod StarRocks V2");

    // 3. Delete datasource with danger ConfirmDialog
    await page.getByRole("button", { name: "删除" }).click();
    const confirmDlg = page.getByRole("alertdialog");
    await expect(confirmDlg).toBeVisible();
    await expect(confirmDlg.getByRole("button", { name: "取消" })).toBeFocused();
    await confirmDlg.getByRole("button", { name: "删除" }).click();
    await expect(page).toHaveURL(/\/settings\/datasources$/);

    await assertCleanRuntime(page, errors);
  });

  test("B9: ChangesPage renders ALL definition_changed diffs rather than stopping at first", async ({
    page,
  }) => {
    const errors = trackPageErrors(page);
    await setThemeAndLang(page, "light", "zh-CN");
    await stubShellDefaults(page);

    await page.route("**/api/datasources", (route) =>
      jsonOk(route, [
        {
          id: "ds-1",
          name: "Prod SR",
          host: "10.0.0.1",
          port: 9030,
          collect_interval_minutes: 60,
          options: {},
          engine: { id: "starrocks", label: "StarRocks", capabilities: ["collect", "query"], options: [] },
          server_identity: {},
          aliases: [],
          credentials: [],
          last_run: null,
        },
      ]),
    );
    await page.route("**/api/change-sets", (route) =>
      jsonOk(route, [
        {
          id: "cs-multi",
          datasource_id: "ds-1",
          run_id: "cr-1",
          status: "open",
          created_at: "2026-10-10T00:00:00Z",
          effective_window: { after: "2026-10-09T23:00:00Z", before: "2026-10-10T00:00:00Z" },
          summary: { "event:definition_changed": 2, impacts: 0 },
          impacts: [],
          events: [{ kind: "definition_changed" }, { kind: "definition_changed" }],
        },
      ]),
    );
    await page.route("**/api/change-sets/cs-multi", (route) =>
      jsonOk(route, {
        id: "cs-multi",
        datasource_id: "ds-1",
        run_id: "cr-1",
        status: "open",
        created_at: "2026-10-10T00:00:00Z",
        effective_window: { after: "2026-10-09T23:00:00Z", before: "2026-10-10T00:00:00Z" },
        summary: { "event:definition_changed": 2, impacts: 0 },
        impacts: [],
        events: [
          {
            id: "ev-1",
            kind: "definition_changed",
            asset_id: "a-1",
            asset: "dw.view_a",
            before: { canonical_sql: "SELECT a FROM t1" },
            after: { canonical_sql: "SELECT a, b FROM t1" },
          },
          {
            id: "ev-2",
            kind: "definition_changed",
            asset_id: "a-2",
            asset: "dw.view_b",
            before: { canonical_sql: "SELECT x FROM t2" },
            after: { canonical_sql: "SELECT x, y FROM t2" },
          },
        ],
      }),
    );

    await fastLogin(page);
    await page.goto("/changes/cs-multi");

    const panels = page.locator("section.av-panel", { hasText: "定义对比" });
    await expect(panels).toHaveCount(2);
    await expect(panels.nth(0)).toContainText("view_a");
    await expect(panels.nth(1)).toContainText("view_b");

    await assertCleanRuntime(page, errors);
  });

  test("B10: ChangeRecords syncs filters to URL searchParams and resets cleanly", async ({
    page,
  }) => {
    const errors = trackPageErrors(page);
    await setThemeAndLang(page, "light", "zh-CN");
    await stubShellDefaults(page);

    const queries: string[] = [];
    await page.route("**/api/change-events*", (route) => {
      queries.push(new URL(route.request().url()).search);
      return jsonOk(route, { items: [], total: 0, actors: [] });
    });

    await fastLogin(page);
    await page.goto("/changes?view=records");

    await page.getByLabel("变更类型").click();
    await page.getByRole("option", { name: "定义变化" }).click();
    await page.getByRole("textbox", { name: "字段" }).fill("amt");

    await expect(page).toHaveURL(/view=records/);
    await expect(page).toHaveURL(/kind=definition_changed/);
    await expect(page).toHaveURL(/column=amt/);

    // Reload restores filters from URL
    queries.length = 0;
    await page.reload();
    await expect(page.getByRole("textbox", { name: "字段" })).toHaveValue("amt");
    await expect(page.getByLabel("变更类型")).toContainText("定义变化");
    expect(queries.some((q) => q.includes("kind=definition_changed") && q.includes("column=amt"))).toBe(
      true,
    );

    // Reset Filters preserves view=records and clears filter params
    await page.getByRole("button", { name: "重置筛选" }).click();
    await expect(page).toHaveURL(/\/changes\?view=records$/);
    await expect(page.getByRole("textbox", { name: "字段" })).toHaveValue("");

    await assertCleanRuntime(page, errors);
  });

  test("B11: numbers.ts never leaks simplified 万/亿 into en/zh-TW and presentation splits localized SQL headers", async ({
    browser,
  }) => {
    for (const locale of ["en", "zh-TW"] as const) {
      const ctx = await browser.newContext();
      const page = await ctx.newPage();
      const errors = trackPageErrors(page);
      await setThemeAndLang(page, "light", locale);
      await stubShellDefaults(page);

      const sql =
        locale === "en"
          ? "-- gmv V1 · Current 2026-09-01 ~ 2026-09-30\nSELECT 1\n-- gmv V1 · Previous period 2026-08-01 ~ 2026-08-31\nSELECT 2"
          : "-- gmv V1 · 本期 2026-09-01 ~ 2026-09-30\nSELECT 1\n-- gmv V1 · 上一期 2026-08-01 ~ 2026-08-31\nSELECT 2";

      await page.route("**/api/analysis/runs/run-i18n", (route) =>
        jsonOk(route, {
          id: "run-i18n",
          status: "succeeded",
          question: "GMV?",
          report: { kind: "lookup" },
          datasets: [],
          steps: [],
          created_at: "2026-10-10T00:00:00Z",
          answer: {
            id: "ans-i18n",
            status: "answered",
            question: "GMV?",
            message: "GMV result",
            sql,
            result: {
              columns: ["value"],
              rows: [[123456789]],
              units: { value: { kind: "level", unit: "元" } },
            },
            evidence: {},
            freshness: { tables: {} },
            steps: [],
          },
        }),
      );

      await fastLogin(page);
      await page.goto("/analysis/runs/run-i18n");

      const valText = await page.locator(".av-lookup__value > span").first().innerText();
      expect(valText).not.toContain("万");
      expect(valText).not.toContain("亿");
      if (locale === "en") {
        expect(valText).toMatch(/100,000,000|10,000|[MBK]/);
      } else {
        expect(valText).toContain("億");
      }

      await page.locator("details.av-doc__more > summary").click();
      await page.getByRole("tab", { name: "SQL" }).click();
      const sections = page.locator(".av-sql-section");
      await expect(sections).toHaveCount(2);

      await assertCleanRuntime(page, errors);
      await ctx.close();
    }
  });
});
