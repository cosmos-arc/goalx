import { defineConfig, devices } from "@playwright/test";

// 端口可用 E2E_WEB_PORT 覆盖：默认 5173 与历史行为一致；主工作区 dev server
// 常驻 5173 时（worktree 跑门禁的场景）换端口跑，避免 reuse 复用别人的代码。
const webPort = Number(process.env["E2E_WEB_PORT"] ?? 5173);

export default defineConfig({
	testDir: "./e2e",
	// paper-loop/corpus-loop/guide-shots 各由专属 config 单独跑（隔离后端/DB；
	// guide-shots 是手册截图采集器——混进默认套件会用降级态页覆盖已入册 PNG）
	testIgnore: /paper-loop|corpus-loop|guide-shots/,
	timeout: 30_000,
	fullyParallel: true,
	retries: process.env["CI"] ? 1 : 0,
	reporter: process.env["CI"] ? "github" : "list",
	use: {
		baseURL: `http://127.0.0.1:${webPort}`,
		trace: "on-first-retry",
	},
	projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
	webServer: {
		command: `bun run dev --host 127.0.0.1 --port ${webPort}`,
		url: `http://127.0.0.1:${webPort}`,
		reuseExistingServer: !process.env["CI"],
		timeout: 120_000,
	},
});
