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

test.describe("M2A Accessibility, Overlay Stack, Responsive & Reduced Motion", () => {
  test("A1 & A2: Overlay Esc closes strictly top-most layer + danger ConfirmDialog focuses Cancel with aria-describedby", async ({
    page,
  }) => {
    const errors = trackPageErrors(page);
    await setThemeAndLang(page, "light", "zh-CN");
    await stubShellDefaults(page);

    const skillDetail = {
      id: "sk-1",
      name: "rev-skill",
      description: "Revenue diagnostics",
      visibility: "public",
      source: "upload",
      status: "listed",
      can_manage: true,
      enabled_users: 3,
      active_version: 1,
      owner: { id: "u-1", display_name: "Admin" },
      domain_id: "e2e-domain-1",
      versions: [
        {
          id: "sv-1",
          version: 1,
          status: "approved",
          note: "Initial",
          review_note: null,
          has_scripts: false,
          uploaded_by: { id: "u-1", display_name: "Admin" },
          uploaded_at: "2026-10-01T00:00:00Z",
          reviewed_by: { id: "u-1", display_name: "Admin" },
          reviewed_at: "2026-10-01T01:00:00Z",
          frontmatter: { name: "rev-skill", description: "Revenue diagnostics" },
          files: [{ path: "SKILL.md", size: 64, kind: "doc" }],
        },
      ],
      shown_version: {
        id: "sv-1",
        version: 1,
        status: "approved",
        note: "Initial",
        review_note: null,
        has_scripts: false,
        uploaded_by: { id: "u-1", display_name: "Admin" },
        uploaded_at: "2026-10-01T00:00:00Z",
        reviewed_by: { id: "u-1", display_name: "Admin" },
        reviewed_at: "2026-10-01T01:00:00Z",
        frontmatter: { name: "rev-skill", description: "Revenue diagnostics" },
        files: [{ path: "SKILL.md", size: 64, kind: "doc" }],
      },
      installed: false,
      listed: true,
    };

    await page.route((url) => url.pathname.startsWith("/api/skills"), (route) => {
      const url = route.request().url();
      if (url.includes("/api/skills/sk-1/versions/")) {
        return jsonOk(route, { path: "SKILL.md", content: "# Rev Skill\nDiagnostics", truncated: false });
      }
      if (url.includes("/api/skills/sk-1")) {
        return jsonOk(route, skillDetail);
      }
      return jsonOk(route, [
        {
          id: "sk-1",
          name: "rev-skill",
          description: "Revenue diagnostics",
          visibility: "public",
          source: "upload",
          status: "listed",
          version: 1,
          latest_version: 1,
          latest_status: "approved",
          latest_note: "Initial",
          review_note: null,
          owner: { id: "u-1", display_name: "Admin" },
          latest_uploaded_by: { id: "u-1", display_name: "Admin" },
          enabled_users: 3,
          updated_at: "2026-10-01T00:00:00Z",
        },
      ]);
    });

    await fastLogin(page);
    await page.goto("/skills/mine");

    // Click skill row button to open SkillDrawer (Dialog)
    await page.getByRole("button", { name: /Rev Skill/i }).click();
    const drawer = page.getByRole("dialog", { name: "技能详情" });
    await expect(drawer).toBeVisible();

    // Open danger ConfirmDialog from Drawer
    await drawer.getByRole("button", { name: "下架" }).click();
    const alertDlg = page.getByRole("alertdialog");
    await expect(alertDlg).toBeVisible();

    // A2: danger={true} defaults focus to Cancel & exposes aria-describedby
    const cancelBtn = alertDlg.getByRole("button", { name: "取消" });
    await expect(cancelBtn).toBeFocused();
    const descId = await alertDlg.getAttribute("aria-describedby");
    expect(descId).toBeTruthy();
    await expect(alertDlg.locator(`#${descId}`)).toContainText(/下架|停用/);

    // A1: First Esc closes ONLY ConfirmDialog, leaving SkillDrawer open
    await page.keyboard.press("Escape");
    await expect(alertDlg).toHaveCount(0);
    await expect(drawer).toBeVisible();

    // Second Esc closes SkillDrawer
    await page.keyboard.press("Escape");
    await expect(drawer).toHaveCount(0);

    await assertCleanRuntime(page, errors);
  });

  test("A3: DateInput Tab stays inside dialog, exposes ARIA grid (7 rows / 42 cells), and Year/Month quick-pick updates grid", async ({
    page,
  }) => {
    const errors = trackPageErrors(page);
    await setThemeAndLang(page, "light", "zh-CN");
    await stubShellDefaults(page);
    await page.route("**/api/change-events*", (route) =>
      jsonOk(route, { items: [], total: 0, actors: [] }),
    );

    await fastLogin(page);
    await page.goto("/changes?view=records");

    const trigger = page.getByRole("button", { name: "起始日期（业务时区）" });
    await trigger.click();

    const cal = page.getByRole("dialog", { name: "起始日期（业务时区）" });
    await expect(cal).toBeVisible();

    const grid = cal.getByRole("grid");
    await expect(grid).toBeVisible();
    expect(await cal.getByRole("row").count()).toBe(7);
    expect(await cal.getByRole("gridcell").count()).toBe(42);

    // Tab traversal must stay inside .av-datepick and wrap cleanly
    const initialInPicker = await page.evaluate(
      () => !!document.activeElement?.closest(".av-datepick"),
    );
    expect(initialInPicker).toBe(true);
    for (let i = 0; i < 8; i++) {
      await page.keyboard.press("Tab");
      const inPicker = await page.evaluate(
        () => !!document.activeElement?.closest(".av-datepick"),
      );
      expect(inPicker).toBe(true);
    }

    // Year / Month quick-pick
    await cal.getByRole("combobox", { name: "年份" }).click();
    await page.getByRole("option", { name: "2025" }).click();
    await cal.getByRole("combobox", { name: "月份" }).click();
    await page.getByRole("option", { name: "1月", exact: true }).click();

    await expect(grid).toHaveAttribute("aria-label", "2025年1月");
    await cal.getByRole("button", { name: "2025-01-15" }).click();
    await expect(cal).toHaveCount(0);
    await expect(trigger).toContainText("2025-01-15");

    await assertCleanRuntime(page, errors);
  });

  test("A4: Menu keyboard nav (ArrowDown/Up/Home/End, focus-on-open, Esc restore) + Skip Link", async ({
    page,
  }) => {
    const errors = trackPageErrors(page);
    await setThemeAndLang(page, "light", "zh-CN");
    await stubShellDefaults(page);

    await fastLogin(page);

    // Skip Link: first Tab from body focuses .av-skip, Enter moves focus to #av-main
    await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
    await page.keyboard.press("Tab");
    const skip = page.locator("a.av-skip");
    await expect(skip).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.locator("#av-main")).toBeFocused();

    // UserMenu keyboard navigation
    const meBtn = page.locator("button.av-sb__me");
    await meBtn.focus();
    await page.keyboard.press("ArrowDown");

    const menu = page.getByRole("menu");
    await expect(menu).toBeVisible();
    const items = menu.locator('[role="menuitem"], [role="menuitemradio"]');
    const count = await items.count();
    expect(count).toBeGreaterThanOrEqual(3);
    await expect(items.first()).toBeFocused();

    await page.keyboard.press("End");
    await expect(items.last()).toBeFocused();

    await page.keyboard.press("Home");
    await expect(items.first()).toBeFocused();

    await page.keyboard.press("ArrowDown");
    await expect(items.nth(1)).toBeFocused();

    await page.keyboard.press("ArrowUp");
    await expect(items.first()).toBeFocused();

    await page.keyboard.press("Escape");
    await expect(menu).toHaveCount(0);
    await expect(meBtn).toBeFocused();

    await assertCleanRuntime(page, errors);
  });

  test("A5: Narrow viewports (375px & 768px) allow .av-fill vertical scrolling without clipping or horizontal overflow", async ({
    browser,
  }) => {
    for (const viewport of [
      { width: 375, height: 812 },
      { width: 768, height: 1024 },
    ]) {
      const ctx = await browser.newContext({ viewport });
      const page = await ctx.newPage();
      const errors = trackPageErrors(page);
      await setThemeAndLang(page, "light", "zh-CN");
      await stubShellDefaults(page);

      const tallWorkbench = emptyWorkbench();
      tallWorkbench.todos = Array.from({ length: 10 }, (_, i) => ({
        kind: "review",
        status: "warn",
        title: `待复核变更 #${i + 1}`,
        meta: "口径影响检查",
        ref: `CS-${i + 1}`,
        at: "2026-10-01T10:00:00Z",
        link: "/changes",
      }));
      tallWorkbench.my_metrics = Array.from({ length: 8 }, (_, i) => ({
        id: `m-${i + 1}`,
        name: `核心指标 ${i + 1}`,
        version_no: 1,
        status: "published",
        grain: ["day"],
        asks_week: i + 3,
      }));
      tallWorkbench.datasources = Array.from({ length: 6 }, (_, i) => ({
        id: `ds-${i + 1}`,
        name: `StarRocks Cluster ${i + 1}`,
        kind: "starrocks",
        status: "ok",
        interval_minutes: 60,
        last_run_at: "2026-10-01T09:00:00Z",
      }));
      await page.route("**/api/workbench", (route) => jsonOk(route, tallWorkbench));

      await fastLogin(page);

      const fill = page.locator(".av-fill");
      await expect(fill).toBeVisible();

      const metrics = await page.evaluate(() => {
        const el = document.querySelector(".av-fill") as HTMLElement;
        const style = getComputedStyle(el);
        const before = el.scrollTop;
        el.scrollTop = el.scrollHeight;
        const after = el.scrollTop;
        return {
          overflowY: style.overflowY,
          scrollHeight: el.scrollHeight,
          clientHeight: el.clientHeight,
          innerHeight: window.innerHeight,
          scrollWidth: document.documentElement.scrollWidth,
          innerWidth: window.innerWidth,
          before,
          after,
        };
      });

      expect(["auto", "scroll"]).toContain(metrics.overflowY);
      expect(metrics.clientHeight).toBeLessThanOrEqual(metrics.innerHeight + 1);
      expect(metrics.scrollHeight).toBeGreaterThan(metrics.clientHeight);
      expect(metrics.after).toBeGreaterThan(metrics.before);
      expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.innerWidth);

      const lastSource = page.locator(".av-workbench__source").last();
      await lastSource.scrollIntoViewIfNeeded();
      await expect(lastSource).toBeVisible();

      await assertCleanRuntime(page, errors);
      await ctx.close();
    }
  });

  test("A6: prefers-reduced-motion: reduce zeroes transitions and disables skeleton shimmer animation", async ({
    browser,
  }) => {
    const ctx = await browser.newContext({ reducedMotion: "reduce" });
    const page = await ctx.newPage();
    const errors = trackPageErrors(page);
    await setThemeAndLang(page, "light", "zh-CN");
    await stubShellDefaults(page);

    // Delay workbench slightly so <Skeleton /> is captured while loading
    let releaseWb!: () => void;
    const wbGate = new Promise<void>((res) => {
      releaseWb = res;
    });
    await page.route("**/api/workbench", async (route) => {
      await wbGate;
      await jsonOk(route, emptyWorkbench());
    });

    await fastLogin(page);

    const skel = page.locator(".av-skeleton > span").first();
    await expect(skel).toBeVisible();
    const skelAnim = await skel.evaluate((el) => getComputedStyle(el, "::after").animationName);
    expect(skelAnim).toBe("none");

    releaseWb();
    const primaryBtn = page.getByRole("link", { name: "新建指标" }).first();
    await expect(primaryBtn).toBeVisible();
    const dur = await page.evaluate(() => {
      const btn = document.querySelector(".av-btn--primary")!;
      const nav = document.querySelector(".av-sb__nv")!;
      return {
        btnTransition: getComputedStyle(btn).transitionDuration,
        navTransition: getComputedStyle(nav).transitionDuration,
        reducedMatches: window.matchMedia("(prefers-reduced-motion: reduce)").matches,
      };
    });
    expect(dur.reducedMatches).toBe(true);
    expect(dur.btnTransition).toBe("0s");
    expect(dur.navTransition).toBe("0s");

    await assertCleanRuntime(page, errors);
    await ctx.close();
  });
});
