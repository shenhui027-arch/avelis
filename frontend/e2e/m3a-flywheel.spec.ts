import { expect, test } from "@playwright/test";

import {
  assertCleanRuntime,
  fastLogin,
  jsonOk,
  setThemeAndLang,
  stubShellDefaults,
  trackPageErrors,
} from "./m2-helpers";

test.describe("M3A Ask Flywheel, Domain Experiences & VQR Flows", () => {
  test("M3A-1: Ask feedback, grounding warning, VQR/semantic-cache badges, compaction & save-as-VQR", async ({
    page,
  }) => {
    const errors = trackPageErrors(page);
    await setThemeAndLang(page, "light", "zh-CN");
    await stubShellDefaults(page);

    let feedbackBodies: Array<{ rating: string | null; note: string | null }> = [];
    let compacted = false;

    const makeAnswer = (id: string, question: string, opts: { semantic?: boolean; vqr?: boolean; ungrounded?: boolean } = {}) => ({
      id,
      question,
      asked_by: "u-admin",
      asked_at: "2026-11-23T10:00:00Z",
      status: "answered",
      message: "核心指标合计 1,250",
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
        ...(opts.vqr ? { vqr_refs: [{ id: "vqr-ref-1", title: "核心指标标准口径查询", metric_codes: ["core_metric"] }] } : {}),
        ...(opts.semantic ? { semantic_cache: { hit: true, similarity: 0.94 } } : {}),
        ...(opts.ungrounded
          ? { explanation_grounding: { ok: false, checked_count: 2, ungrounded: ["999"] } }
          : { explanation_grounding: { ok: true, checked_count: 1, ungrounded: [] } }),
      },
      filter_alignment: [],
      freshness: {},
      executed_at: "2026-11-23T10:00:02Z",
      duration_ms: 420,
      cache_hit: !!opts.semantic,
      recheck_status: null,
      thread_id: "th-m3a",
      steps: [{ node: "understand", result: "intent", at: "2026-11-23T10:00:01Z" }],
      explanation: opts.ungrounded ? "核心指标达到 1,250，较基线 999 显著提升。" : "核心指标达到 1,250。",
      explanation_source: "model",
      feedback_rating: null,
      feedback_note: null,
      feedback_at: null,
    });

    let answers = [
      makeAnswer("ans-1", "核心指标总计是多少？", { vqr: true, semantic: true, ungrounded: true }),
      makeAnswer("ans-2", "近七天趋势如何？"),
    ];

    await page.route("**/api/ask/threads", (route) =>
      jsonOk(route, [{ id: "th-m3a", title: "核心指标会话", status: "active", created_at: "2026-11-23T10:00:00Z", updated_at: "2026-11-23T10:01:00Z" }]),
    );
    await page.route("**/api/ask/threads/th-m3a", (route) =>
      jsonOk(route, {
        id: "th-m3a",
        title: "核心指标会话",
        status: "active",
        answers,
        compacted_summary: compacted ? "历史上下文摘要：已讨论核心指标总计与趋势。" : null,
        compacted_at: compacted ? "2026-11-23T10:05:00Z" : null,
      }),
    );
    await page.route("**/api/ask/threads/th-m3a/compact", async (route) => {
      compacted = true;
      return jsonOk(route, {
        thread_id: "th-m3a",
        compacted: true,
        before_messages: 8,
        after_messages: 4,
        before_tokens: 3200,
        after_tokens: 900,
        compacted_summary: "历史上下文摘要：已讨论核心指标总计与趋势。",
        compacted_at: "2026-11-23T10:05:00Z",
      });
    });
    await page.route("**/api/answers/*/feedback", async (route) => {
      const id = route.request().url().split("/api/answers/")[1].split("/")[0];
      const body = JSON.parse(route.request().postData() ?? "{}") as { rating: "up" | "down" | null; note?: string | null };
      feedbackBodies.push({ rating: body.rating, note: body.note ?? null });
      answers = answers.map((a) =>
        a.id === id ? { ...a, feedback_rating: body.rating, feedback_note: body.note ?? null, feedback_at: "2026-11-23T10:06:00Z" } : a,
      );
      const updated = answers.find((a) => a.id === id)!;
      return jsonOk(route, updated);
    });

    await fastLogin(page);
    await page.goto("/ask/th-m3a");

    // 1. VQR match badge, semantic cache badge, and numeric grounding warning
    const firstCard = page.locator("article.av-answer").first();
    await expect(firstCard.getByRole("link", { name: "标准查询" })).toBeVisible();
    await expect(firstCard.getByText("语义缓存")).toBeVisible();
    await expect(firstCard.getByText("解读数字待核对")).toBeVisible();
    await expect(firstCard.getByText("待核对数值：999")).toBeVisible();

    // 2. Thumbs up & Thumbs down with optional reason bar + Esc close
    await firstCard.getByRole("button", { name: "有帮助" }).click();
    await expect(firstCard.getByRole("button", { name: "有帮助" })).toHaveAttribute("aria-pressed", "true");
    expect(feedbackBodies[0]).toEqual({ rating: "up", note: null });

    await firstCard.getByRole("button", { name: "待改进" }).click();
    const reasonRegion = firstCard.getByRole("region", { name: "反馈说明" });
    await expect(reasonRegion).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(reasonRegion).toBeHidden();

    // Re-open thumbs down (currently pressed -> toggles off, then press again to submit reason)
    await firstCard.getByRole("button", { name: "待改进" }).click();
    await firstCard.getByRole("button", { name: "待改进" }).click();
    await firstCard.getByLabel("说明哪里不准确或缺少什么").fill("口径时间范围有偏差");
    await firstCard.getByRole("button", { name: "提交说明" }).click();
    await expect(firstCard.getByRole("region", { name: "反馈说明" })).toBeHidden();
    expect(feedbackBodies.at(-1)).toEqual({ rating: "down", note: "口径时间范围有偏差" });

    // 3. Thread compaction button + collapsible summary banner
    await page.getByRole("button", { name: "压缩上下文" }).click();
    await expect(page.getByText("已压缩早期上下文")).toBeVisible();
    await page.getByText("已压缩早期上下文").click();
    await expect(page.getByText("历史上下文摘要：已讨论核心指标总计与趋势。")).toBeVisible();

    // 4. Save-as-VQR shortcut pre-fills drawer
    await firstCard.getByRole("button", { name: "存为标准查询" }).click();
    const dialog = page.getByRole("dialog", { name: "新建标准查询" });
    await expect(dialog).toBeVisible();
    await expect(dialog.locator("textarea").first()).toHaveValue("核心指标总计是多少？");
    await expect(dialog.locator("textarea.av-mono")).toHaveValue(/SELECT SUM\(val\)/);
    await dialog.getByRole("button", { name: "取消" }).click();

    await assertCleanRuntime(page, errors);
  });

  test("M3A-2: Domain experience sharing & anti-self-approval in ExperiencesPage and DomainDetail", async ({
    page,
  }) => {
    const errors = trackPageErrors(page);
    await setThemeAndLang(page, "light", "zh-CN");
    await stubShellDefaults(page);

    let meId = "";
    await page.route("**/api/me", async (route) => {
      const resp = await route.fetch();
      const data = (await resp.json()) as { id: string };
      meId = data.id;
      return route.fulfill({ response: resp, json: data });
    });

    await page.route("**/api/domains", (route) =>
      jsonOk(route, [
        {
          id: "dom-1",
          name: "核心域",
          code: "core",
          description: "核心业务域",
          owner_id: "u-other",
          members: [{ user_id: "u-other", username: "other", display_name: "Other", role: "owner" }],
          example_questions: [],
        },
      ]),
    );

    const experiences: Array<Record<string, unknown>> = [
      {
        id: "exp-personal",
        owner_id: "__SELF__",
        scope: "personal",
        domain_id: null,
        review_status: "none",
        submitted_by: null,
        submitted_at: null,
        reviewed_by: null,
        reviewed_at: null,
        review_note: null,
        current_version: 1,
        status: "active",
        created_at: "2026-11-23T09:00:00Z",
        version_id: "ev-1",
        version: 1,
        kind: "rule",
        content: "统计活跃指标需排除测试账号",
        refs: [{ object_type: "metric_version", object_id: "mv-1", version: 1 }],
        source: { run_id: "run-1", checkpoint: "assumptions", user_text: "排除测试账号" },
        confirmed_by: "__SELF__",
        confirmed_at: "2026-11-23T09:00:00Z",
        superseded_by: null,
      },
      {
        id: "exp-other-pending",
        owner_id: "u-other",
        scope: "domain",
        domain_id: "dom-1",
        review_status: "pending",
        submitted_by: "u-other",
        submitted_at: "2026-11-23T09:30:00Z",
        reviewed_by: null,
        reviewed_at: null,
        review_note: null,
        current_version: 1,
        status: "active",
        created_at: "2026-11-23T09:30:00Z",
        version_id: "ev-2",
        version: 1,
        kind: "driver",
        content: "区域渠道波动主要受节假日排期影响",
        refs: [{ object_type: "metric_version", object_id: "mv-1", version: 1 }],
        source: { run_id: "run-2", checkpoint: "assumptions", user_text: "节假日排期" },
        confirmed_by: "u-other",
        confirmed_at: "2026-11-23T09:30:00Z",
        superseded_by: null,
      },
    ];

    const resolveOwners = () =>
      experiences.map((e) => ({
        ...e,
        owner_id: e.owner_id === "__SELF__" ? meId : e.owner_id,
        submitted_by: e.submitted_by === "__SELF__" ? meId : e.submitted_by,
        confirmed_by: e.confirmed_by === "__SELF__" ? meId : e.confirmed_by,
      }));

    await page.route("**/api/analysis/experiences*", async (route) => {
      const url = new URL(route.request().url());
      if (route.request().method() === "GET") {
        const scope = url.searchParams.get("scope") ?? "personal";
        const domainId = url.searchParams.get("domain_id");
        const reviewStatus = url.searchParams.get("review_status");
        const items = resolveOwners().filter((e) => {
          if (scope !== "all" && e.scope !== scope) return false;
          if (domainId && e.domain_id !== domainId) return false;
          if (reviewStatus && e.review_status !== reviewStatus) return false;
          return true;
        });
        return jsonOk(route, items);
      }
      return route.continue();
    });

    await page.route("**/api/analysis/experiences/*/submit", async (route) => {
      const id = route.request().url().split("/api/analysis/experiences/")[1].split("/")[0];
      const body = JSON.parse(route.request().postData() ?? "{}") as { domain_id: string };
      const target = experiences.find((e) => e.id === id)!;
      target.scope = "domain";
      target.domain_id = body.domain_id;
      target.review_status = "pending";
      target.submitted_by = "__SELF__";
      return jsonOk(route, resolveOwners().find((e) => e.id === id));
    });

    await page.route("**/api/analysis/experiences/*/approve", async (route) => {
      const id = route.request().url().split("/api/analysis/experiences/")[1].split("/")[0];
      const target = experiences.find((e) => e.id === id)!;
      target.review_status = "approved";
      return jsonOk(route, resolveOwners().find((e) => e.id === id));
    });

    await fastLogin(page);
    await page.goto("/analysis/experiences");

    // 1. Share personal experience to domain
    await page.getByRole("button", { name: "分享到业务域" }).click();
    const shareDrawer = page.getByRole("dialog", { name: "分享到业务域" });
    await expect(shareDrawer).toBeVisible();
    await shareDrawer.getByRole("button", { name: "提交审核" }).click();
    await expect(shareDrawer).toBeHidden();

    // 2. Switch to 待审核 scope -> verify self-submitted experience blocks self-approval
    await page.getByRole("button", { name: "待审核" }).click();
    const selfRow = page.locator(".av-experience", { hasText: "统计活跃指标需排除测试账号" });
    await expect(selfRow.getByText("不能审核自己提交的经验")).toBeVisible();
    await expect(selfRow.getByRole("button", { name: "批准" })).toBeDisabled();
    await expect(selfRow.getByRole("button", { name: "驳回" })).toBeDisabled();

    // 3. Open DomainDetail -> 共享经验 tab -> approve other user's submission
    await page.goto("/catalog/domains/dom-1");
    await page.getByRole("tab", { name: /共享经验/ }).click();
    const otherRow = page.locator(".av-experience", { hasText: "区域渠道波动主要受节假日排期影响" });
    await expect(otherRow.getByRole("button", { name: "批准" })).toBeEnabled();
    await otherRow.getByRole("button", { name: "批准" }).click();
    await expect(otherRow.getByText("已批准")).toBeVisible();

    await assertCleanRuntime(page, errors);
  });

  test("M3A-3: VQR management, create/submit & anti-self-approval review in /catalog/queries", async ({
    page,
  }) => {
    const errors = trackPageErrors(page);
    await setThemeAndLang(page, "light", "zh-CN");
    await stubShellDefaults(page);

    let meId = "";
    await page.route("**/api/me", async (route) => {
      const resp = await route.fetch();
      const data = (await resp.json()) as { id: string };
      meId = data.id;
      return route.fulfill({ response: resp, json: data });
    });

    const vqrs: Array<Record<string, unknown>> = [
      {
        id: "vqr-other",
        domain_id: "e2e-domain-1",
        title: "活跃用户按月统计",
        question: "每月活跃用户数是多少？",
        question_variants: [],
        sql_text: "SELECT month, COUNT(DISTINCT user_id) FROM dw.active_users GROUP BY month",
        dialect: "postgres",
        datasource_id: null,
        metric_codes: ["active_users"],
        tags: ["monthly"],
        notes: null,
        source_answer_id: null,
        source_run_id: null,
        status: "pending_review",
        created_by: "u-other",
        created_at: "2026-11-23T08:00:00Z",
        updated_at: "2026-11-23T08:00:00Z",
        submitted_by: "u-other",
        submitted_at: "2026-11-23T08:05:00Z",
        reviewed_by: null,
        reviewed_at: null,
        review_note: null,
        approved_at: null,
      },
    ];

    const resolveVqrs = () =>
      vqrs.map((v) => ({
        ...v,
        created_by: v.created_by === "__SELF__" ? meId : v.created_by,
        submitted_by: v.submitted_by === "__SELF__" ? meId : v.submitted_by,
      }));

    await page.route("**/api/verified-queries", async (route) => {
      if (route.request().method() === "POST") {
        const body = JSON.parse(route.request().postData() ?? "{}") as Record<string, unknown>;
        const created = {
          id: "vqr-created",
          domain_id: body.domain_id ?? "e2e-domain-1",
          title: body.title,
          question: body.question,
          question_variants: body.question_variants ?? [],
          sql_text: body.sql_text,
          dialect: body.dialect ?? "postgres",
          datasource_id: null,
          metric_codes: body.metric_codes ?? [],
          tags: body.tags ?? [],
          notes: body.notes ?? null,
          source_answer_id: null,
          source_run_id: null,
          status: "draft",
          created_by: "__SELF__",
          created_at: "2026-11-23T11:00:00Z",
          updated_at: "2026-11-23T11:00:00Z",
          submitted_by: null,
          submitted_at: null,
          reviewed_by: null,
          reviewed_at: null,
          review_note: null,
          approved_at: null,
        };
        vqrs.unshift(created);
        return jsonOk(route, resolveVqrs()[0], 201);
      }
      return jsonOk(route, resolveVqrs());
    });
    await page.route("**/api/verified-queries?*", (route) => jsonOk(route, resolveVqrs()));
    await page.route("**/api/verified-queries/*/submit", (route) => {
      const id = route.request().url().split("/api/verified-queries/")[1].split("/")[0];
      const item = vqrs.find((v) => v.id === id)!;
      item.status = "pending_review";
      item.submitted_by = "__SELF__";
      return jsonOk(route, resolveVqrs().find((v) => v.id === id));
    });
    await page.route("**/api/verified-queries/*/approve", (route) => {
      const id = route.request().url().split("/api/verified-queries/")[1].split("/")[0];
      const item = vqrs.find((v) => v.id === id)!;
      item.status = "approved";
      return jsonOk(route, resolveVqrs().find((v) => v.id === id));
    });

    await fastLogin(page);
    await page.goto("/catalog/queries");

    // 1. Create & submit VQR
    await page.getByRole("button", { name: "新建标准查询" }).click();
    const createDialog = page.getByRole("dialog", { name: "新建标准查询" });
    await createDialog.getByPlaceholder("简明概括该标准查询的用途").fill("核心留存查询");
    await createDialog.getByPlaceholder("提问人通常会如何问这个问题").fill("七日留存率是多少？");
    await createDialog.getByPlaceholder("SELECT ...").fill("SELECT dt, retention_7d FROM dw.retention_daily");
    await createDialog.getByRole("button", { name: "保存并提交审核" }).click();
    await expect(createDialog).toBeHidden();

    // 2. Self-submitted VQR enforces anti-self-approval in Review Drawer
    const selfCard = page.locator("article.av-vqr-card", { hasText: "核心留存查询" });
    await expect(selfCard).toBeVisible();
    await selfCard.getByRole("button", { name: "查看" }).click();
    const selfReviewDialog = page.getByRole("dialog", { name: "核心留存查询" });
    await expect(selfReviewDialog.getByText("不能审核自己创建或提交的标准查询")).toBeVisible();
    await expect(selfReviewDialog.getByRole("button", { name: "批准" })).toBeDisabled();
    await expect(selfReviewDialog.getByRole("button", { name: "驳回" })).toBeDisabled();
    await selfReviewDialog.getByRole("button", { name: "关闭" }).last().click();

    // 3. Approve another user's pending VQR
    const otherCard = page.locator("article.av-vqr-card", { hasText: "活跃用户按月统计" });
    await otherCard.getByRole("button", { name: "审核" }).click();
    const otherReviewDialog = page.getByRole("dialog", { name: "活跃用户按月统计" });
    await expect(otherReviewDialog.getByRole("button", { name: "批准" })).toBeEnabled();
    await otherReviewDialog.getByRole("button", { name: "批准" }).click();
    await expect(otherReviewDialog.getByText("已批准").first()).toBeVisible();

    await assertCleanRuntime(page, errors);
  });
});
