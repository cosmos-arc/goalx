import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
	testDir: "./e2e",
	// paper-loop 由 playwright.loop.config.ts 单独跑（需要隔离后端/DB）
	testIgnore: /paper-loop/,
	timeout: 30_000,
	fullyParallel: true,
	retries: process.env["CI"] ? 1 : 0,
	reporter: process.env["CI"] ? "github" : "list",
	use: {
		baseURL: "http://127.0.0.1:5173",
		trace: "on-first-retry",
	},
	projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
	webServer: {
		command: "bun run dev --host 127.0.0.1 --port 5173",
		url: "http://127.0.0.1:5173",
		reuseExistingServer: !process.env["CI"],
		timeout: 120_000,
	},
});
