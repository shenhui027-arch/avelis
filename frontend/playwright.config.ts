import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 8_000 },
  workers: 1,
  fullyParallel: false,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:18100",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "uv run --frozen --project ../backend python e2e/server_harness.py --port 18100",
    url: "http://127.0.0.1:18100/api/health",
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
