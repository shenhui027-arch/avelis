import fs from "node:fs";
import { expect, test, type APIRequestContext, type BrowserContext, type Page } from "@playwright/test";

async function loginViaUi(page: Page, username: string, password: string, remember = false) {
  await page.goto("/login");
  await page.locator('input[autocomplete="username"]').fill(username);
  await page.locator('input[autocomplete="current-password"]').fill(password);
  const checkbox = page.locator(".av-login__remember input[type='checkbox']");
  if (remember) {
    await checkbox.check();
  } else {
    await checkbox.uncheck();
  }
  await page.locator("button.av-login__submit").click();
}

async function newZhContext(
  browser: import("@playwright/test").Browser,
  options: Parameters<import("@playwright/test").Browser["newContext"]>[0] = {},
) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, ...options });
  await ctx.addInitScript(() => {
    localStorage.setItem("avelis.lang", "zh-CN");
  });
  return ctx;
}

async function getAuthCookies(context: BrowserContext) {
  const cookies = await context.cookies();
  return {
    access: cookies.find((c) => c.name === "avelis_access"),
    refresh: cookies.find((c) => c.name === "avelis_refresh"),
  };
}

async function adminLoginToken(request: APIRequestContext): Promise<string> {
  const resp = await request.post("/api/auth/token", {
    form: { username: "admin", password: "Admin-e2e-password-2026!" },
  });
  expect(resp.status()).toBe(200);
  const json = (await resp.json()) as { access_token: string };
  return json.access_token;
}

async function verifyCsvDownloadAndLineageUpload(
  page: Page,
  runId: string,
  filename: string,
  expectLocalToken: boolean,
) {
  const tok = await page.evaluate(() => sessionStorage.getItem("avelis.token"));
  if (expectLocalToken) {
    expect(tok).toBeTruthy();
  } else {
    expect(tok).toBeNull();
  }

  // 1. 验证分析数据集 CSV 下载
  await page.goto(`/analysis/runs/${runId}`);
  const dsToggle = page.locator(".av-dataset__hd").first();
  await expect(dsToggle).toBeVisible();
  if ((await dsToggle.getAttribute("aria-expanded")) !== "true") {
    await dsToggle.click();
  }
  const downloadPromise = page.waitForEvent("download");
  await page.locator(".av-dataset__ft button", { hasText: "CSV" }).first().click();
  const download = await downloadPromise;
  const dlPath = await download.path();
  expect(dlPath).toBeTruthy();
  const csvContent = fs.readFileSync(dlPath!, "utf-8");
  expect(csvContent).toContain("region,gmv");
  expect(csvContent).toContain("12345");

  // 2. 验证血缘文件上传 (/api/imports/upload)
  await page.goto("/lineage/import");
  await expect(page.getByText("上传文件")).toBeVisible();
  await page.getByText("上传文件", { exact: true }).click();
  await page.getByLabel("上传原文文件").setInputFiles({
    name: filename,
    mimeType: "text/plain",
    buffer: Buffer.from("INSERT INTO demo.target SELECT id FROM demo.src;"),
  });
  const uploadRespPromise = page.waitForResponse(
    (res) => res.url().endsWith("/api/imports/upload") && res.request().method() === "POST",
  );
  await page.getByRole("button", { name: "解析", exact: true }).click();
  const uploadResp = await uploadRespPromise;
  expect(uploadResp.status()).toBe(202);
  await expect(page).toHaveURL(/\/lineage\/import\/.+/);
}

test.describe("Auth Issues #149 & #150 Browser Verification", () => {
  test("#149: Browser session renewal uses Refresh Cookie without Bearer and preserves remember_me", async ({
    browser,
  }) => {
    // 1. 勾选「记住我」：访问令牌失效 / 清空后仍通过 Refresh Cookie 续期且保留持久化 Cookie
    const ctxRemember = await newZhContext(browser);
    const page = await ctxRemember.newPage();
    await loginViaUi(page, "steward_user", "Steward-e2e-password-2026!", true);
    await expect(page).toHaveURL("/");
    await page.waitForLoadState("networkidle");

    const initialCookies = await getAuthCookies(ctxRemember);
    expect(initialCookies.access?.expires ?? -1).toBeGreaterThan(0);
    expect(initialCookies.refresh?.expires ?? -1).toBeGreaterThan(0);

    const refreshHeaders: Array<{ auth: string | undefined; status: number }> = [];
    page.on("response", (res) => {
      if (new URL(res.url()).pathname === "/api/auth/refresh") {
        refreshHeaders.push({ auth: res.request().headers()["authorization"], status: res.status() });
      }
    });

    // 模拟标签页闲置后 access token 过期：sessionStorage 中仍残留失效 token，但 refresh cookie 仍有效
    await page.evaluate(() => {
      sessionStorage.setItem("avelis.token", "expired.jwt.token");
    });
    await ctxRemember.clearCookies({ name: "avelis_access" });

    await page.goto("/lineage");
    await expect(page.locator(".av-sb__me")).toContainText("Steward User");
    await expect(page).toHaveURL(/\/lineage/);
    await expect(page).not.toHaveURL(/\/login/);
    expect(refreshHeaders.length).toBeGreaterThanOrEqual(1);
    expect(refreshHeaders[0].status).toBe(200);
    expect(refreshHeaders[0].auth).toBeUndefined();

    const renewedCookies = await getAuthCookies(ctxRemember);
    expect(renewedCookies.access?.expires ?? -1).toBeGreaterThan(0);
    expect(renewedCookies.refresh?.expires ?? -1).toBeGreaterThan(0);

    // 2. 主动提前续期（sessionStorage 中的访问令牌仍未过期）：同样走 Refresh Cookie，保留「记住我」持久化期限
    const proactiveCountBefore = refreshHeaders.length;
    await page.evaluate(() => {
      const now = Date.now();
      sessionStorage.setItem("avelis.token.expiry", JSON.stringify({ issued: now - 60_000, expires: now + 30_000 }));
      window.dispatchEvent(new Event("pointerdown"));
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect.poll(() => refreshHeaders.length).toBeGreaterThan(proactiveCountBefore);
    const proactive = refreshHeaders[refreshHeaders.length - 1];
    expect(proactive.status).toBe(200);
    expect(proactive.auth).toBeUndefined();

    const proactiveCookies = await getAuthCookies(ctxRemember);
    expect(proactiveCookies.access?.expires ?? -1).toBeGreaterThan(0);
    expect(proactiveCookies.refresh?.expires ?? -1).toBeGreaterThan(0);
    await ctxRemember.close();

    // 3. 未勾选「记住我」：续期后两端 Cookie 仍保持会话级有效期（expires === -1）
    const ctxSession = await newZhContext(browser);
    const sessionPage = await ctxSession.newPage();
    await loginViaUi(sessionPage, "steward_user", "Steward-e2e-password-2026!", false);
    await expect(sessionPage).toHaveURL("/");
    await sessionPage.waitForLoadState("networkidle");

    const sessInit = await getAuthCookies(ctxSession);
    expect(sessInit.access?.expires).toBe(-1);
    expect(sessInit.refresh?.expires).toBe(-1);

    await sessionPage.evaluate(() => {
      sessionStorage.setItem("avelis.token", "expired.jwt.token");
    });
    await ctxSession.clearCookies({ name: "avelis_access" });
    await sessionPage.goto("/lineage");
    await expect(sessionPage.locator(".av-sb__me")).toContainText("Steward User");
    await expect(sessionPage).toHaveURL(/\/lineage/);

    const sessRenewed = await getAuthCookies(ctxSession);
    expect(sessRenewed.access?.expires).toBe(-1);
    expect(sessRenewed.refresh?.expires).toBe(-1);
    await ctxSession.close();
  });

  test("#150: Cookie-only OIDC & Forward Auth + local login can download analysis CSV & upload lineage", async ({
    browser,
    request,
  }) => {
    const infoResp = await request.get("/__e2e/info");
    expect(infoResp.status()).toBe(200);
    const { oidc_issuer: oidcIssuer } = (await infoResp.json()) as { oidc_issuer: string };

    const adminToken = await adminLoginToken(request);
    const enableResp = await request.put("/api/settings/auth", {
      headers: { Authorization: `Bearer ${adminToken}` },
      data: {
        local_login_enabled: true,
        forward_auth_enabled: true,
        forward_auth_header: "X-Forwarded-User",
        forward_auth_auto_create: true,
        clear_forward_auth_secret: true,
        oidc: {
          enabled: true,
          provider_name: "E2E Mock IdP",
          issuer_url: oidcIssuer,
          client_id: "avelis-e2e-client",
          scopes: "openid profile email",
          auto_create_user: true,
          default_role: "analyst",
        },
        branding: { brand_name: "Avelis" },
      },
    });
    expect(enableResp.status()).toBe(200);

    try {
      // A. OIDC 无本地 token 会话
      const ctxOidc = await newZhContext(browser);
      const oidcPage = await ctxOidc.newPage();
      await oidcPage.goto("/api/auth/oidc/login?next=/");
      await expect(oidcPage).toHaveURL("http://127.0.0.1:18100/");
      await expect(oidcPage.locator(".av-sb__me")).toContainText("OIDC Steward");
      await verifyCsvDownloadAndLineageUpload(oidcPage, "e2e-run-oidc", "oidc_lineage.sql", false);
      await ctxOidc.close();

      // B. Forward Auth 无本地 token 会话（直连受信任回环代理 + X-Forwarded-For 访客 IP，同步验证 #151）
      const ctxFw = await newZhContext(browser, {
        extraHTTPHeaders: {
          "X-Forwarded-User": "fw_steward",
          "X-Forwarded-For": "203.0.113.50",
        },
      });
      const fwPage = await ctxFw.newPage();
      await fwPage.goto("/api/auth/forward?next=/");
      await expect(fwPage).toHaveURL("http://127.0.0.1:18100/");
      await expect(fwPage.locator(".av-sb__me")).toContainText("fw_steward");
      await verifyCsvDownloadAndLineageUpload(fwPage, "e2e-run-fw", "fw_lineage.sql", false);
      await ctxFw.close();

      // C. 本地账号密码基线
      const ctxLocal = await newZhContext(browser);
      const localPage = await ctxLocal.newPage();
      await loginViaUi(localPage, "steward_user", "Steward-e2e-password-2026!", false);
      await expect(localPage).toHaveURL("/");
      await verifyCsvDownloadAndLineageUpload(localPage, "e2e-run-local", "local_lineage.sql", true);
      await ctxLocal.close();
    } finally {
      await request.put("/api/settings/auth", {
        headers: { Authorization: `Bearer ${adminToken}` },
        data: {
          local_login_enabled: true,
          forward_auth_enabled: false,
          forward_auth_header: "X-Forwarded-User",
          forward_auth_auto_create: true,
          clear_forward_auth_secret: true,
          oidc: {
            enabled: false,
            provider_name: "SSO",
            issuer_url: "",
            client_id: "",
            scopes: "openid profile email",
            auto_create_user: true,
            default_role: "analyst",
          },
          branding: { brand_name: "Avelis" },
        },
      });
    }
  });
});