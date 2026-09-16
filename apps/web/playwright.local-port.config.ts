import { defineConfig, devices } from "@playwright/test";

/**
 * 本地多 worktree 并行跑 smoke e2e 的端口覆写（票 15 起使用）：
 * 主工作区 dev server 占 5173、e2e:loop 占 5199——本配置用 5195 起 vite，
 * 避免串台。API 代理仍走 VITE_DEV_API_TARGET 默认 127.0.0.1:8000。
 */
const webPort = 5195;

export default defineConfig({
	testDir: "./e2e",
	// paper-loop 由 playwright.loop.config.ts 单独跑（需要隔离后端/DB）
	testIgnore: /paper-loop/,
	timeout: 30_000,
	fullyParallel: true,
	retries: process.env["CI"] ? 1 : 0,
	reporter: "list",
	use: {
		baseURL: `http://127.0.0.1:${webPort}`,
		trace: "on-first-retry",
	},
	projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
	webServer: {
		command: `bun run dev --host 127.0.0.1 --port ${webPort}`,
		url: `http://127.0.0.1:${webPort}`,
		reuseExistingServer: false,
		timeout: 120_000,
	},
});
