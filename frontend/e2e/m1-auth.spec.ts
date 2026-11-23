import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const SCREENSHOT_DIR =
  process.env.E2E_SCREENSHOT_DIR?.trim() || path.join(os.tmpdir(), "avelis-e2e-screenshots");

fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });

function shotPath(name: string): string {
  return path.join(SCREENSHOT_DIR, name);
}

function trackPageErrors(page: Page) {
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

async function assertCleanRuntime(page: Page, errors: string[]) {
  const csp = await page.evaluate(
    () => (window as unknown as { __cspViolations?: string[] }).__cspViolations ?? [],
  );
  expect(csp, "Expected zero CSP violations").toEqual([]);
  expect(errors, "Expected zero console/page errors").toEqual([]);
}

async function setTheme(page: Page, theme: "light" | "dark", lang = "zh-CN") {
  await page.addInitScript(
    ({ th, lg }) => {
      localStorage.setItem("avelis.theme", th);
      localStorage.setItem("avelis.lang", lg);
    },
    { th: theme, lg: lang },
  );
}

async function loginViaUi(page: Page, username: string, password: string, remember = false) {
  await page.goto("/login");
  await page.locator('input[autocomplete="username"]').fill(username);
  await page.locator('input[autocomplete="current-password"]').fill(password);
  if (remember) {
    await page.locator(".av-login__remember input[type='checkbox']").check();
  }
  await page.locator("button.av-login__submit").click();
}

test.describe("M1B Commercial Auth, User Ops & High-Res Visual Capture", () => {
  test("1. Login desktop light/dark, interactive stage, password toggle & mobile", async ({ browser }) => {
    // Light desktop (1440x900, step 0)
    const ctxLight = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await ctxLight.newPage();
    const errors = trackPageErrors(page);
    await setTheme(page, "light");

    const themeScriptResp = await page.request.get("/theme-init.js");
    expect(themeScriptResp.status()).toBe(200);

    await page.goto("/login");
    await page.waitForLoadState("networkidle");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    await expect(page.getByRole("tab", { name: /血缘溯源/ })).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("button.av-login__submit")).toBeEnabled();

    // Verify password show/hide toggle
    const pwInput = page.locator('input[autocomplete="current-password"]');
    await expect(pwInput).toHaveAttribute("type", "password");
    await page.locator(".av-login__reveal").click();
    await expect(pwInput).toHaveAttribute("type", "text");
    await page.locator(".av-login__reveal").click();
    await expect(pwInput).toHaveAttribute("type", "password");

    await page.waitForTimeout(450);
    await page.screenshot({ path: shotPath("login-desktop-light.png"), fullPage: false });

    // Empty submit shows inline validation without any network/console errors
    await page.locator("button.av-login__submit").click();
    await expect(page.getByText("请输入账号")).toBeVisible();
    await expect(page.getByText("请输入密码")).toBeVisible();
    await expect(page.locator('input[autocomplete="username"]')).toHaveAttribute("aria-invalid", "true");
    await expect(pwInput).toHaveAttribute("aria-invalid", "true");
    await expect(page).toHaveURL(/\/login/);

    // Keyboard & click stage step 1
    await page.getByRole("tab", { name: /血缘溯源/ }).focus();
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("tab", { name: /口径校验/ })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByText("SUM(metric_amt)")).toBeVisible();
    await assertCleanRuntime(page, errors);
    await ctxLight.close();

    // Dark desktop (1440x900, step 1)
    const ctxDark = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const darkPage = await ctxDark.newPage();
    const darkErrors = trackPageErrors(darkPage);
    await setTheme(darkPage, "dark");
    await darkPage.goto("/login");
    await darkPage.waitForLoadState("networkidle");
    await expect(darkPage.locator("html")).toHaveAttribute("data-theme", "dark");
    await expect(darkPage.locator("button.av-login__submit")).toBeEnabled();
    await darkPage.getByRole("tab", { name: /口径校验/ }).click();
    await darkPage.waitForTimeout(450);
    await darkPage.screenshot({ path: shotPath("login-desktop-dark.png"), fullPage: false });
    await assertCleanRuntime(darkPage, darkErrors);
    await ctxDark.close();

    // Mobile light (390x844)
    const ctxMobLight = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const mobLight = await ctxMobLight.newPage();
    const mobLightErr = trackPageErrors(mobLight);
    await setTheme(mobLight, "light");
    await mobLight.goto("/login");
    await mobLight.waitForLoadState("networkidle");
    await mobLight.waitForTimeout(450);
    await expect(mobLight.locator(".av-login__stage-card")).toBeHidden();
    await expect(mobLight.locator('.av-login__rail [role="tab"]').first()).toBeHidden();
    await expect(mobLight.locator("button.av-login__submit")).toBeEnabled();
    const hasOverflowLight = await mobLight.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    expect(hasOverflowLight).toBe(false);
    await mobLight.screenshot({ path: shotPath("login-mobile-light.png"), fullPage: false });
    await assertCleanRuntime(mobLight, mobLightErr);
    await ctxMobLight.close();

    // Mobile dark (390x844)
    const ctxMobDark = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const mobDark = await ctxMobDark.newPage();
    const mobDarkErr = trackPageErrors(mobDark);
    await setTheme(mobDark, "dark");
    await mobDark.goto("/login");
    await mobDark.waitForLoadState("networkidle");
    await mobDark.waitForTimeout(450);
    await expect(mobDark.locator(".av-login__stage-card")).toBeHidden();
    await expect(mobDark.locator('.av-login__rail [role="tab"]').first()).toBeHidden();
    await expect(mobDark.locator("button.av-login__submit")).toBeEnabled();
    const hasOverflowDark = await mobDark.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    expect(hasOverflowDark).toBe(false);
    await mobDark.screenshot({ path: shotPath("login-mobile-dark.png"), fullPage: false });
    await assertCleanRuntime(mobDark, mobDarkErr);
    await ctxMobDark.close();
  });

  test("2. OIDC single-slot + white-label branding + step 2 & lockout countdown", async ({ browser }) => {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await ctx.newPage();
    const errors = trackPageErrors(page);
    await setTheme(page, "light");

    // Configure OIDC + white-label via real admin session, then log out and inspect /login
    await loginViaUi(page, "admin", "Admin-e2e-password-2026!");
    await expect(page).toHaveURL("/");

    await page.goto("/settings/auth");
    await page.waitForLoadState("networkidle");
    await page.getByRole("switch", { name: "启用 OIDC 单点登录（PKCE）" }).click();
    await page.getByLabel("按钮显示名称").fill("Okta SSO");
    await page.getByLabel("Issuer URL").fill("https://sso.example.invalid");
    await page.getByLabel("Client ID").fill("avelis-web");
    await page.getByLabel("品牌名称").fill("Northstar Analytics");
    await page.getByLabel("登录页副标题").fill("使用企业统一身份或本地凭证登录");
    await page.getByLabel("服务条款链接").fill("https://example.invalid/terms");
    await page.getByLabel("隐私政策链接").fill("https://example.invalid/privacy");
    await page.getByRole("button", { name: "保存" }).click();
    await expect(page.getByText("已保存认证与品牌配置")).toBeVisible();

    // Logout cleanly
    await page.locator(".av-sb__me").click();
    await page.getByRole("menuitem", { name: "退出登录" }).click();
    await expect(page).toHaveURL(/\/login/);
    await page.waitForLoadState("networkidle");

    await expect(page.locator(".av-login__sso")).toContainText("使用 Okta SSO 登录");
    await expect(page.locator(".av-login__logo b")).toHaveText("Northstar Analytics");
    await page.getByRole("tab", { name: /归因结论/ }).click();
    await page.waitForTimeout(450);
    await page.screenshot({ path: shotPath("login-desktop-oidc-branding.png"), fullPage: false });

    await assertCleanRuntime(page, errors);

    // Trigger 429 lockout on an isolated probe username (keeps admin/analyst unlocked)
    await page.locator('input[autocomplete="username"]').fill("lock_probe_user");
    for (let i = 0; i < 4; i++) {
      await page.locator('input[autocomplete="current-password"]').fill(`Wrong-Pass-${i}`);
      await page.locator("button.av-login__submit").click();
      await page.waitForTimeout(180);
    }
    await expect(page.locator(".av-banner--warn")).toContainText(/\d{2}:\d{2}/);
    await expect(page.locator("button.av-login__submit")).toBeDisabled();
    await page.screenshot({ path: shotPath("login-desktop-lockout.png"), fullPage: false });

    // Restore default auth config so subsequent tests see clean default branding
    await loginViaUi(page, "admin", "Admin-e2e-password-2026!");
    await expect(page).toHaveURL("/");
    await page.goto("/settings/auth");
    await page.waitForLoadState("networkidle");
    await page.getByRole("switch", { name: "启用 OIDC 单点登录（PKCE）" }).click();
    await page.getByLabel("品牌名称").fill("Avelis");
    await page.getByLabel("登录页副标题").fill("");
    await page.getByLabel("服务条款链接").fill("");
    await page.getByLabel("隐私政策链接").fill("");
    await page.getByRole("button", { name: "保存" }).click();
    await expect(page.getByText("已保存认证与品牌配置")).toBeVisible();

    const unexpectedErrors = errors.filter((e) => !/status of (401|429)/.test(e));
    await assertCleanRuntime(page, unexpectedErrors);
    await ctx.close();
  });

  test("3. Forced must_change_password, rules bar, toggles, light/dark/mobile PNGs & cookie reload", async ({ browser }) => {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await ctx.newPage();
    const errors = trackPageErrors(page);
    await setTheme(page, "light");

    await loginViaUi(page, "forced_user", "Forced-e2e-password-2026!", true);
    await expect(page).toHaveURL(/\/account\/password/);
    await page.waitForLoadState("networkidle");

    // Verify / redirects to /account/password and backend blocks /oauth/consent with 403 must_change_password
    await page.goto("/");
    await expect(page).toHaveURL(/\/account\/password/);
    const consentResp = await page.request.get("/oauth/consent?ticket=dummy");
    expect(consentResp.status()).toBe(403);
    const consentJson = await consentResp.json();
    expect(consentJson.detail?.code).toBe("must_change_password");

    // Fill inputs to showcase live password rules bar & toggles
    const pwInputs = page.locator(".av-login__input input");
    await pwInputs.nth(0).fill("Forced-e2e-password-2026!");
    await pwInputs.nth(1).fill("NewStrong#Pass99");
    await pwInputs.nth(2).fill("NewStrong#Pass99");
    await expect(page.locator(".av-pw-rules__list li.is-met")).toHaveCount(4);

    // Toggle reveal on new password field
    await page.locator(".av-login__reveal").nth(1).click();
    await expect(pwInputs.nth(1)).toHaveAttribute("type", "text");
    await page.waitForTimeout(350);
    await page.screenshot({ path: shotPath("changepassword-desktop-light.png"), fullPage: false });

    // Capture mobile light ChangePassword
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(250);
    const hasMobOverflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    expect(hasMobOverflow).toBe(false);
    await page.screenshot({ path: shotPath("changepassword-mobile-light.png"), fullPage: false });

    // Capture dark desktop ChangePassword via theme quick toggle
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.locator(".av-login__theme-btn").click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    await page.waitForTimeout(250);
    await page.screenshot({ path: shotPath("changepassword-desktop-dark.png"), fullPage: false });

    // Submit password change -> lands on /
    await page.locator("button.av-login__submit").click();
    await expect(page).toHaveURL("/");

    // Prove HttpOnly cookie session survives clearing sessionStorage + reload
    await page.evaluate(() => sessionStorage.clear());
    await page.reload();
    await expect(page).toHaveURL("/");
    await expect(page.locator(".av-sb__me")).toContainText("Forced Change User");

    await assertCleanRuntime(page, errors);
    await ctx.close();
  });

  test("4. Admin User Ops (search, kick, reset-password modal), Domain member remove, Security TOTP+PAT, Role tabs", async ({ browser }) => {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await ctx.newPage();
    const errors = trackPageErrors(page);
    await setTheme(page, "light");

    await loginViaUi(page, "admin", "Admin-e2e-password-2026!");
    await expect(page).toHaveURL("/");

    // 4a. UsersPage: search + force kick + reset-password one-time modal
    await page.goto("/settings/users");
    await page.waitForLoadState("networkidle");
    await page.getByLabel("搜索用户").fill("kick_user");
    await expect(page.locator("tbody tr")).toHaveCount(1);
    await expect(page.locator("tbody tr").first()).toContainText("kick_user");

    // Force kick
    await page.getByRole("button", { name: "强制下线" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "强制下线" }).click();
    await expect(page.getByText("已强制 kick_user 下线")).toBeVisible();

    // Reset password -> one-time temp password modal open
    await page.getByRole("button", { name: "重置密码" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "生成临时口令" }).click();
    await expect(page.locator(".av-webhooks__secret")).toBeVisible();
    await page.waitForTimeout(300);
    await page.screenshot({ path: shotPath("users-page-desktop.png"), fullPage: false });
    await page.getByRole("alertdialog").getByRole("button", { name: "我已保存" }).click();

    // 4b. DomainDetail: members tab + remove member button & dialog
    await page.goto("/catalog/domains/e2e-domain-1");
    await page.waitForLoadState("networkidle");
    await page.getByRole("tab", { name: /成员/ }).click();
    await expect(page.getByRole("button", { name: "移除" }).first()).toBeVisible();
    await page.waitForTimeout(300);
    await page.screenshot({ path: shotPath("domain-detail-members-desktop.png"), fullPage: false });

    // Remove analyst_user cleanly
    const analystRow = page.locator("tbody tr", { hasText: "analyst_user" });
    await analystRow.getByRole("button", { name: "移除" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "移除" }).click();
    await expect(page.getByText("已移除成员 Analyst User")).toBeVisible();

    // 4c. Account Security: TOTP enroll + PAT create
    await page.goto("/account/security");
    await page.waitForLoadState("networkidle");
    await page.getByRole("button", { name: "开启双因素认证" }).click();
    await expect(page.getByText("密钥（Base32）")).toBeVisible();
    await expect(page.getByText("配置链接（otpauth）")).toBeVisible();
    const qrSvg = page.getByRole("img", { name: "双因素认证配置二维码" });
    await expect(qrSvg).toBeVisible();
    const qrBox = await qrSvg.boundingBox();
    expect(qrBox?.width ?? 0).toBeGreaterThanOrEqual(100);
    expect(qrBox?.height ?? 0).toBeGreaterThanOrEqual(100);
    await expect(qrSvg.locator("path")).toHaveAttribute("d", /.+/);

    await page.getByRole("button", { name: "生成令牌" }).click();
    await page.getByLabel("令牌名称").fill("ci-readonly-script");
    await page.getByRole("button", { name: "生成", exact: true }).click();
    await expect(page.getByText("已生成令牌 · ci-readonly-script")).toBeVisible();
    await page.getByRole("alertdialog").getByRole("button", { name: "我已保存" }).click();
    await expect(page.locator("tbody tr").first()).toContainText("ci-readonly-script");
    await page.waitForTimeout(300);
    await page.screenshot({ path: shotPath("account-security-totp-pat-desktop.png"), fullPage: false });

    await assertCleanRuntime(page, errors);
    await ctx.close();

    // 4d. Role-filtered SettingsTabs as analyst (hides models, users, webhooks, auth)
    const analystCtx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const analystPage = await analystCtx.newPage();
    const analystErrors = trackPageErrors(analystPage);
    await setTheme(analystPage, "light");
    await loginViaUi(analystPage, "analyst_user", "Analyst-e2e-password-2026!");
    await expect(analystPage).toHaveURL("/");
    await analystPage.goto("/settings/datasources");
    await analystPage.waitForLoadState("networkidle");

    const tabsNav = analystPage.getByRole("navigation", { name: "设置分区" });
    await expect(tabsNav.getByRole("link", { name: "数据源" })).toBeVisible();
    await expect(tabsNav.getByRole("link", { name: "业务日历" })).toBeVisible();
    await expect(tabsNav.getByRole("link", { name: "MCP 接入" })).toBeVisible();
    await expect(tabsNav.getByRole("link", { name: "模型配置" })).toHaveCount(0);
    await expect(tabsNav.getByRole("link", { name: "用户" })).toHaveCount(0);
    await expect(tabsNav.getByRole("link", { name: "通知订阅" })).toHaveCount(0);
    await expect(tabsNav.getByRole("link", { name: "认证与品牌" })).toHaveCount(0);

    await analystPage.waitForTimeout(300);
    await analystPage.screenshot({ path: shotPath("settings-tabs-role-filtered-desktop.png"), fullPage: false });

    await assertCleanRuntime(analystPage, analystErrors);
    await analystCtx.close();
  });
});
