import { expect, test } from "@playwright/test";

import {
  assertCleanRuntime,
  fastLogin,
  jsonOk,
  setThemeAndLang,
  stubShellDefaults,
  trackPageErrors,
} from "./m2-helpers";

test.describe("M3B SSE Streaming & Multi-Model Governance", () => {
  test("M3B-1: Analysis SSE live streaming and automatic 2000ms polling fallback", async ({
    page,
  }) => {
    const errors = trackPageErrors(page);
    await setThemeAndLang(page, "light", "zh-CN");
    await stubShellDefaults(page);

    const sseRunBase = {
      id: "run-sse-1",
      conversation_id: "conv-sse-1",
      turn: 1,
      question: "核心指标为什么上升？",
      skills: [],
      status: "running",
      trigger: "manual",
      title: null,
      verified: null,
      report: null,
      datasets: [],
      steps: [],
      live: null,
      messages: [],
      budget: { used: { model_requests: 1, tool_calls: 0, queries: 0, seconds: 1 }, limits: { model_requests: 20, tool_calls: 30, queries: 30, seconds: 300 } },
      model_adapter: "openai · gpt-5",
      created_at: "2026-11-23T10:00:00Z",
      updated_at: "2026-11-23T10:00:01Z",
    };

    const sseRunDone = {
      ...sseRunBase,
      status: "succeeded",
      verified: true,
      title: "核心指标上升归因",
      steps: [
        { seq: 1, kind: "model", tool: "model", status: "ok", duration_ms: 420, args: {}, output: { thinking: "已完成多维拆解核对。", text: "整理结论" } },
      ],
      report: {
        title: "核心指标上升归因",
        summary: "整体较基线显著上升。",
        claims: [{ type: "fact", text: "整体指标上升显著。", refs: [] }],
        causes: [],
      },
      verification: { ok: true, status: "all_passed", claims: [], fields: [], sources: {} },
    };

    let sseRunPolls = 0;
    await page.route("**/api/analysis/runs/run-sse-1", (route) => {
      sseRunPolls += 1;
      return jsonOk(route, sseRunBase);
    });
    await page.route("**/api/analysis/conversations/conv-sse-1", (route) =>
      jsonOk(route, { id: "conv-sse-1", title: "核心指标分析", latest_run_id: "run-sse-1", runs: [{ id: "run-sse-1", turn: 1 }] }),
    );
    await page.route("**/api/analysis/runs/run-sse-1/stream*", async (route) => {
      const body = [
        ": keepalive\n\n",
        `event: status\ndata: ${JSON.stringify({ run_id: "run-sse-1", status: "running", phase: "model", after_seq: 0 })}\n\n`,
        `event: thinking\ndata: ${JSON.stringify({ delta: "正在核对核心指标口径…", content: "正在核对核心指标口径…", phase: "model", after_seq: 0 })}\n\n`,
        `event: step\ndata: ${JSON.stringify({ step: sseRunDone.steps[0] })}\n\n`,
        `event: done\ndata: ${JSON.stringify(sseRunDone)}\n\n`,
      ].join("");
      await route.fulfill({
        status: 200,
        headers: { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache" },
        body,
      });
    });

    await fastLogin(page);
    await page.goto("/analysis/runs/run-sse-1");

    await expect(page.getByRole("heading", { name: "核心指标上升归因" })).toBeVisible();
    await expect(page.getByText("整体较基线显著上升。")).toBeVisible();
    // Initial GET (or deduplicated initial fetch) only; no repeated 2000ms polling
    expect(sseRunPolls).toBeLessThanOrEqual(2);

    // Part B: Polling fallback when SSE fails
    let fbPolls = 0;
    const fbRunning = { ...sseRunBase, id: "run-fb-1", conversation_id: "conv-fb-1", question: "回退轮询测试问题" };
    const fbDone = { ...sseRunDone, id: "run-fb-1", conversation_id: "conv-fb-1", question: "回退轮询测试问题", title: "轮询回退完成报告" };
    fbDone.report = { ...sseRunDone.report, title: "轮询回退完成报告", summary: "通过 2 秒轮询回退完成。" };

    await page.route("**/api/analysis/runs/run-fb-1", (route) => {
      fbPolls += 1;
      return jsonOk(route, fbPolls >= 2 ? fbDone : fbRunning);
    });
    await page.route("**/api/analysis/conversations/conv-fb-1", (route) =>
      jsonOk(route, { id: "conv-fb-1", title: "回退测试", latest_run_id: "run-fb-1", runs: [{ id: "run-fb-1", turn: 1 }] }),
    );
    await page.route("**/api/analysis/runs/run-fb-1/stream*", (route) =>
      route.fulfill({ status: 502, contentType: "application/json", body: JSON.stringify({ detail: "stream unavailable" }) }),
    );

    await page.goto("/analysis/runs/run-fb-1");
    await expect(page.getByRole("heading", { name: "轮询回退完成报告" })).toBeVisible({ timeout: 8000 });
    await expect(page.getByText("通过 2 秒轮询回退完成。")).toBeVisible();
    expect(fbPolls).toBeGreaterThanOrEqual(2);

    const unexpected = errors.filter((e) => !/status of 502/.test(e));
    await assertCleanRuntime(page, unexpected);
  });

  test("M3B-2: Ask SSE live streaming, JSON fallback, and HTTP 429 quota error display", async ({
    page,
  }) => {
    const errors = trackPageErrors(page);
    await setThemeAndLang(page, "light", "zh-CN");
    await stubShellDefaults(page);

    const makeAnswer = (id: string, question: string, msg: string) => ({
      id,
      question,
      asked_by: "u-admin",
      asked_at: "2026-11-23T10:00:00Z",
      status: "answered",
      message: msg,
      error_code: null,
      model_adapter: "openai",
      intent: { metric_codes: ["core_metric"], group_by: [] },
      plan: {},
      sql: "SELECT SUM(val) AS value FROM dw.fact_core",
      metric_version_ids: ["mv-1"],
      result: {
        columns: ["value"],
        rows: [[1250]],
        column_roles: { value: { role: "measure", label: "指标" } },
        units: { value: { kind: "level", unit: "个" } },
      },
      evidence: {
        metric_version: { metric_code: "core_metric", metric_name: "核心指标", version_no: 1, status: "published" },
      },
      filter_alignment: [],
      freshness: {},
      executed_at: "2026-11-23T10:00:02Z",
      duration_ms: 350,
      cache_hit: false,
      recheck_status: null,
      thread_id: "th-m3b",
      steps: [{ node: "understand", result: "intent", at: "2026-11-23T10:00:01Z" }],
      explanation: `${msg}。`,
      explanation_source: "model",
      feedback_rating: null,
      feedback_note: null,
      feedback_at: null,
    });

    const answers = [makeAnswer("ans-init", "初始指标是多少？", "核心指标合计 1,250")];
    await page.route("**/api/ask/threads", (route) =>
      jsonOk(route, [{ id: "th-m3b", title: "流式问数会话", status: "active", created_at: "2026-11-23T10:00:00Z", updated_at: "2026-11-23T10:00:00Z" }]),
    );
    await page.route("**/api/ask/threads/th-m3b", (route) =>
      jsonOk(route, { id: "th-m3b", title: "流式问数会话", status: "active", answers, compacted_summary: null, compacted_at: null }),
    );

    let streamMode: "sse" | "fallback404" | "quota429" = "sse";
    let fallbackJsonCalls = 0;

    await page.route("**/api/ask/stream", async (route) => {
      if (streamMode === "quota429") {
        return route.fulfill({
          status: 429,
          contentType: "application/json",
          body: JSON.stringify({
            detail: { code: "quota_exceeded", detail: "已达到模型用量或费用配额上限，请联系管理员调整" },
          }),
        });
      }
      if (streamMode === "fallback404") {
        return route.fulfill({
          status: 404,
          contentType: "application/json",
          body: JSON.stringify({ detail: "Not Found" }),
        });
      }
      const nextAns = makeAnswer("ans-sse", "流式查询问题", "流式回答完成：1,250");
      answers.push(nextAns);
      const sseBody = [
        `event: status\ndata: ${JSON.stringify({ phase: "understand", status: "running" })}\n\n`,
        `event: step\ndata: ${JSON.stringify({ step: { node: "understand", result: "intent" } })}\n\n`,
        `event: thinking\ndata: ${JSON.stringify({ delta: "检索核心指标口径", content: "检索核心指标口径", phase: "understand" })}\n\n`,
        `event: delta\ndata: ${JSON.stringify({ delta: "流式回答完成：1,250。", content: "流式回答完成：1,250。", phase: "explain" })}\n\n`,
        `event: done\ndata: ${JSON.stringify(nextAns)}\n\n`,
      ].join("");
      return route.fulfill({
        status: 200,
        headers: { "Content-Type": "text/event-stream; charset=utf-8" },
        body: sseBody,
      });
    });

    await page.route("**/api/ask", async (route) => {
      fallbackJsonCalls += 1;
      const nextAns = makeAnswer("ans-json", "回退普通接口问题", "JSON 回退回答完成：1,250");
      answers.push(nextAns);
      return jsonOk(route, nextAns);
    });

    await fastLogin(page);
    await page.goto("/ask/th-m3b");

    // 1. Live SSE ask
    const input = page.locator("#q");
    await input.fill("流式查询问题");
    await page.locator("form.av-composer button[type='submit']").click();
    await expect(page.getByText("流式回答完成：1,250", { exact: true })).toBeVisible();
    expect(fallbackJsonCalls).toBe(0);

    // 2. Fallback to POST /api/ask when stream returns 404
    streamMode = "fallback404";
    await input.fill("回退普通接口问题");
    await page.locator("form.av-composer button[type='submit']").click();
    await expect(page.getByText("JSON 回退回答完成：1,250", { exact: true })).toBeVisible();
    expect(fallbackJsonCalls).toBe(1);

    // 3. HTTP 429 quota error surfaces cleanly without falling back
    streamMode = "quota429";
    await input.fill("触发配额上限");
    await page.locator("form.av-composer button[type='submit']").click();
    await expect(page.locator(".av-ask-page__error")).toContainText("已达到模型用量或费用配额上限");
    expect(fallbackJsonCalls).toBe(1);

    const unexpected = errors.filter((e) => !/status of (404|429)/.test(e));
    await assertCleanRuntime(page, unexpected);
  });

  test("M3B-3: Model Settings 4 tabs — providers, failover & routing, pricing & quotas, usage ledger & chart (light & dark)", async ({
    page,
  }) => {
    const errors = trackPageErrors(page);
    await setThemeAndLang(page, "light", "zh-CN");
    await stubShellDefaults(page);

    const providersData = [
      {
        id: "prov-oai",
        code: "openai",
        name: "OpenAI",
        kind: "openai_compatible",
        preset: "openai",
        base_url: "https://api.openai.com/v1",
        endpoint: "https://api.openai.com/v1/chat/completions",
        api_key: "sk-...1234",
        has_api_key: true,
        enabled: true,
        models: [
          {
            id: "gpt-5",
            enabled: true,
            tool_calling: true,
            context_window: 400000,
            efforts: [
              { value: "low", label: "低", effective: "low", effective_label: "低" },
              { value: "high", label: "高", effective: "high", effective_label: "高" },
            ],
          },
          {
            id: "gpt-4o-mini",
            enabled: true,
            tool_calling: true,
            context_window: 128000,
            efforts: [],
          },
        ],
        last_check: { model_id: "gpt-5", ok: true, tool_calling: true, latency_ms: 180, error: null, checked_at: "2026-11-23T09:00:00Z" },
        updated_at: "2026-11-23T09:00:00Z",
      },
      {
        id: "prov-ant",
        code: "anthropic",
        name: "Anthropic",
        kind: "anthropic",
        preset: "anthropic",
        base_url: "https://api.anthropic.com",
        endpoint: "https://api.anthropic.com/v1/messages",
        api_key: "sk-...9999",
        has_api_key: true,
        enabled: true,
        models: [
          {
            id: "claude-sonnet-4",
            enabled: true,
            tool_calling: true,
            context_window: 200000,
            efforts: [
              { value: "off", label: "关闭", effective: "off", effective_label: "关闭" },
              { value: "medium", label: "中", effective: "medium", effective_label: "中" },
            ],
          },
        ],
        last_check: { model_id: "claude-sonnet-4", ok: true, tool_calling: true, latency_ms: 210, error: null, checked_at: "2026-11-23T09:00:00Z" },
        updated_at: "2026-11-23T09:00:00Z",
      },
    ];

    let savedSettingsPayload: Record<string, unknown> | null = null;
    const settingsState = {
      assignments: {
        ask: {
          label: "问数理解",
          needs_tools: true,
          hint: "理解问题、检索口径",
          provider_id: "prov-oai",
          model_id: "gpt-5",
          updated_at: "2026-11-23T09:00:00Z",
          reasoning_effort: "low",
          default_effort: "low",
          effective_effort: "low",
          backups: [],
        },
        explain: {
          label: "结果解释",
          needs_tools: false,
          hint: "解释查询结果",
          provider_id: "prov-oai",
          model_id: "gpt-4o-mini",
          updated_at: "2026-11-23T09:00:00Z",
          reasoning_effort: "low",
          default_effort: "low",
          effective_effort: "low",
          backups: [],
        },
        import: {
          label: "导入血缘推断",
          needs_tools: false,
          hint: "提出候选血缘",
          provider_id: null,
          model_id: null,
          updated_at: null,
          reasoning_effort: "low",
          default_effort: "low",
          effective_effort: null,
          backups: [],
        },
        analysis: {
          label: "分析",
          needs_tools: true,
          hint: "多步分析",
          provider_id: "prov-ant",
          model_id: "claude-sonnet-4",
          updated_at: "2026-11-23T09:00:00Z",
          reasoning_effort: "medium",
          default_effort: "low",
          effective_effort: "medium",
          backups: [],
        },
      },
      policy: { send_results: true, send_code: true },
      routing: { enabled: false, simple_target: null, complex_target: null },
      mode: "configured",
      note: null,
    };

    const pricingCatalog = {
      configured: [
        { id: "pr-1", provider_code: "openai", model_id: "gpt-5", input_per_1m: 1.25, output_per_1m: 10.0, cache_hit_per_1m: 0.125, currency: "USD", source: "configured", updated_at: "2026-11-23T09:00:00Z" },
      ],
      defaults: [
        { provider_code: "*", model_id: "gpt-4o-mini", input_per_1m: 0.15, output_per_1m: 0.6, cache_hit_per_1m: 0.075, currency: "USD", source: "builtin" },
      ],
    };

    let quotasList = [
      { id: "q-global", scope_type: "global", scope_id: "*", daily_token_limit: 500000, monthly_token_limit: 10000000, daily_cost_limit: 25, monthly_cost_limit: 500, rpm_limit: 60, enabled: true, updated_at: "2026-11-23T09:00:00Z" },
    ];

    const ledgerItems = [
      {
        id: "led-1",
        created_at: "2026-11-23T10:15:00Z",
        user_id: "u-admin",
        domain_id: "e2e-domain-1",
        purpose: "analysis",
        phase: "plan",
        provider_id: "prov-ant",
        provider_code: "anthropic",
        model_id: "claude-sonnet-4",
        adapter: "anthropic",
        thread_id: null,
        answer_id: null,
        run_id: "run-1",
        input_tokens: 1800,
        output_tokens: 650,
        cache_hit_tokens: 400,
        reasoning_tokens: 200,
        total_tokens: 2450,
        estimated_cost: 0.0152,
        currency: "USD",
        latency_ms: 820,
        status: "ok",
        failover_from: "openai:gpt-5",
        routing_tier: "complex",
        error_code: null,
      },
      {
        id: "led-2",
        created_at: "2026-11-23T09:45:00Z",
        user_id: "u-admin",
        domain_id: "e2e-domain-1",
        purpose: "ask",
        phase: "understand",
        provider_id: "prov-oai",
        provider_code: "openai",
        model_id: "gpt-4o-mini",
        adapter: "openai",
        thread_id: "th-1",
        answer_id: "ans-1",
        run_id: null,
        input_tokens: 600,
        output_tokens: 180,
        cache_hit_tokens: 0,
        reasoning_tokens: 0,
        total_tokens: 780,
        estimated_cost: 0.0002,
        currency: "USD",
        latency_ms: 210,
        status: "ok",
        failover_from: null,
        routing_tier: "simple",
        error_code: null,
      },
    ];

    await page.route("**/api/users*", (route) =>
      jsonOk(route, [{ id: "u-admin", username: "admin", display_name: "管理员", roles: ["admin"], domains: [], is_active: true, must_change_password: false }]),
    );
    await page.route("**/api/model-providers/presets", (route) =>
      jsonOk(route, {
        presets: {
          openai: { name: "OpenAI", kind: "openai_compatible", base_url: "https://api.openai.com/v1" },
          anthropic: { name: "Anthropic", kind: "anthropic", base_url: "https://api.anthropic.com" },
        },
        purposes: {},
      }),
    );
    await page.route("**/api/model-providers", (route) => jsonOk(route, providersData));
    await page.route("**/api/model-settings", async (route) => {
      if (route.request().method() === "PUT") {
        savedSettingsPayload = JSON.parse(route.request().postData() ?? "{}");
        return jsonOk(route, { ...settingsState, ...(savedSettingsPayload as object) });
      }
      return jsonOk(route, settingsState);
    });
    await page.route("**/api/model-pricing", async (route) => {
      if (route.request().method() === "PUT") {
        const body = JSON.parse(route.request().postData() ?? "{}") as { items: Array<Record<string, unknown>> };
        for (const item of body.items) {
          if (item.delete) continue;
          pricingCatalog.configured.push({
            id: `pr-${pricingCatalog.configured.length + 1}`,
            provider_code: String(item.provider_code ?? "*"),
            model_id: String(item.model_id),
            input_per_1m: Number(item.input_per_1m ?? 0),
            output_per_1m: Number(item.output_per_1m ?? 0),
            cache_hit_per_1m: item.cache_hit_per_1m != null ? Number(item.cache_hit_per_1m) : null,
            currency: String(item.currency ?? "USD"),
            source: "configured",
            updated_at: "2026-11-23T10:30:00Z",
          });
        }
        return jsonOk(route, pricingCatalog);
      }
      return jsonOk(route, pricingCatalog);
    });
    await page.route("**/api/llm-quotas*", async (route) => {
      if (route.request().method() === "PUT") {
        const body = JSON.parse(route.request().postData() ?? "{}") as Record<string, unknown>;
        const created = {
          id: `q-${quotasList.length + 1}`,
          scope_type: body.scope_type,
          scope_id: body.scope_id,
          daily_token_limit: body.daily_token_limit ?? null,
          monthly_token_limit: body.monthly_token_limit ?? null,
          daily_cost_limit: body.daily_cost_limit ?? null,
          monthly_cost_limit: body.monthly_cost_limit ?? null,
          rpm_limit: body.rpm_limit ?? null,
          enabled: body.enabled ?? true,
          updated_at: "2026-11-23T10:35:00Z",
        };
        quotasList = [...quotasList, created as typeof quotasList[number]];
        return jsonOk(route, created);
      }
      if (route.request().method() === "DELETE") {
        const id = route.request().url().split("/api/llm-quotas/")[1];
        quotasList = quotasList.filter((q) => q.id !== id);
        return route.fulfill({ status: 204 });
      }
      return jsonOk(route, quotasList);
    });
    await page.route("**/api/llm-usage/me", (route) =>
      jsonOk(route, {
        summary: {
          days: 30, total_calls: 2, failover_count: 1, error_count: 0,
          input_tokens: 2400, output_tokens: 830, cache_hit_tokens: 400, reasoning_tokens: 200,
          total_tokens: 3230, estimated_cost: 0.0154, currency: "USD",
          by_model: [], by_purpose: [], daily: [],
        },
        today_tokens: 3230,
        today_cost: 0.0154,
        month_tokens: 3230,
        month_cost: 0.0154,
        quotas: [
          { ...quotasList[0], used_daily_tokens: 3230, used_monthly_tokens: 3230, used_daily_cost: 0.0154, used_monthly_cost: 0.0154 },
        ],
      }),
    );
    await page.route("**/api/llm-usage/summary*", (route) =>
      jsonOk(route, {
        days: 30,
        total_calls: 2,
        failover_count: 1,
        error_count: 0,
        input_tokens: 2400,
        output_tokens: 830,
        cache_hit_tokens: 400,
        reasoning_tokens: 200,
        total_tokens: 3230,
        estimated_cost: 0.0154,
        currency: "USD",
        by_model: [
          { provider_code: "anthropic", model_id: "claude-sonnet-4", calls: 1, total_tokens: 2450, estimated_cost: 0.0152 },
          { provider_code: "openai", model_id: "gpt-4o-mini", calls: 1, total_tokens: 780, estimated_cost: 0.0002 },
        ],
        by_purpose: [
          { purpose: "analysis", calls: 1, total_tokens: 2450, estimated_cost: 0.0152 },
          { purpose: "ask", calls: 1, total_tokens: 780, estimated_cost: 0.0002 },
        ],
        daily: [
          { date: "2026-11-22", calls: 1, total_tokens: 780, estimated_cost: 0.0002, ask_tokens: 780, analysis_tokens: 0 },
          { date: "2026-11-23", calls: 1, total_tokens: 2450, estimated_cost: 0.0152, ask_tokens: 0, analysis_tokens: 2450 },
        ],
      }),
    );
    await page.route("**/api/llm-usage/ledger*", (route) => {
      const url = new URL(route.request().url());
      const purposeFilter = url.searchParams.get("purpose");
      const filtered = purposeFilter ? ledgerItems.filter((i) => i.purpose === purposeFilter) : ledgerItems;
      return jsonOk(route, { items: filtered, total: filtered.length, limit: 20, offset: 0 });
    });

    await fastLogin(page);
    await page.goto("/settings/models");

    // Tab 1: Providers & Capabilities
    await expect(page.getByRole("tab", { name: "提供方与模型" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("cell", { name: "gpt-5", exact: true })).toBeVisible();
    await expect(page.locator(".av-models__caps").first().getByText("流式")).toBeVisible();
    await expect(page.locator(".av-models__caps").first().getByText("思考")).toBeVisible();
    await expect(page.locator(".av-models__caps").first().getByText("支持工具")).toBeVisible();
    await expect(page.getByRole("button", { name: "$1.25 / 10" })).toBeVisible();

    // Tab 2: Failover & Complexity Routing
    await page.getByRole("tab", { name: "默认、容灾与路由" }).click();
    await expect(page).toHaveURL(/tab=routing/);

    // Add backup model for ask
    await page.getByRole("combobox", { name: "为问数理解添加备选模型" }).click();
    await page.getByRole("listbox", { name: "为问数理解添加备选模型" }).getByRole("option", { name: "Anthropic · claude-sonnet-4" }).click();
    await expect(page.getByText("#1")).toBeVisible();

    // Enable complexity routing & select simple_target
    await page.getByRole("switch", { name: "启用复杂度路由" }).click();
    await page.getByRole("combobox", { name: "简单查询路由目标" }).click();
    await page.getByRole("listbox", { name: "简单查询路由目标" }).getByRole("option", { name: "OpenAI · gpt-4o-mini" }).click();

    await page.getByRole("button", { name: "保存配置" }).click();
    await expect(page.getByText("已保存")).toBeVisible();
    expect(savedSettingsPayload).not.toBeNull();
    expect((savedSettingsPayload as { routing: { enabled: boolean; simple_target: { model_id: string } } }).routing.enabled).toBe(true);
    expect((savedSettingsPayload as { routing: { simple_target: { model_id: string } } }).routing.simple_target.model_id).toBe("gpt-4o-mini");

    // Tab 3: Pricing & Quotas
    await page.getByRole("tab", { name: "定价与配额" }).click();
    await expect(page).toHaveURL(/tab=quotas/);
    await expect(page.getByRole("meter", { name: "日 Token 用量" })).toBeVisible();

    await page.getByRole("button", { name: "添加定价" }).click();
    const pricingDialog = page.getByRole("dialog", { name: "配置模型定价" });
    await pricingDialog.getByRole("combobox", { name: "模型 ID" }).fill("deepseek-chat");
    await pricingDialog.getByRole("button", { name: "保存定价" }).click();
    await expect(page.getByText("定价已更新")).toBeVisible();

    await page.getByRole("button", { name: "新建配额规则" }).click();
    const quotaDialog = page.getByRole("dialog", { name: "配置配额与限流规则" });
    await quotaDialog.getByRole("combobox", { name: "作用域类型" }).click();
    await page.getByRole("listbox", { name: "作用域类型" }).getByRole("option", { name: "业务域" }).click();
    await quotaDialog.getByRole("button", { name: "保存规则" }).click();
    await expect(page.getByText("配额规则已保存")).toBeVisible();

    // Tab 4: Usage & Cost Ledger + Chart + Dark Theme check
    await page.getByRole("tab", { name: "用量与费用账本" }).click();
    await expect(page).toHaveURL(/tab=usage/);
    await expect(page.getByText("总消耗 Tokens")).toBeVisible();
    await expect(page.getByText("预估总费用")).toBeVisible();
    await expect(page.locator(".av-echart canvas")).toBeVisible();
    await expect(page.getByText("已降级")).toBeVisible();
    await expect(page.getByText("复杂路由")).toBeVisible();

    // Filter ledger by purpose
    await page.getByRole("combobox", { name: "场景" }).click();
    await page.getByRole("listbox", { name: "场景" }).getByRole("option", { name: "问数理解" }).click();
    await expect(page.locator("table").getByText("openai · gpt-4o-mini")).toBeVisible();
    await expect(page.locator("table").getByText("anthropic · claude-sonnet-4")).toBeHidden();
    await expect(page.getByRole("button", { name: "重置筛选" })).toBeVisible();
    await page.getByRole("button", { name: "重置筛选" }).click();
    await expect(page.locator("table").getByText("anthropic · claude-sonnet-4")).toBeVisible();

    // Verify dark theme renders cleanly without console/page/CSP errors
    await page.evaluate(() => {
      document.documentElement.setAttribute("data-theme", "dark");
    });
    await expect(page.locator(".av-echart canvas")).toBeVisible();

    await assertCleanRuntime(page, errors);
  });
});
