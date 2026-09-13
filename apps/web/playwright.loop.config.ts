import path from "node:path";
import { defineConfig } from "@playwright/test";

/**
 * 纸面用户闭环 e2e（票 36 验收）：隔离真实 API + SQLite。
 *
 * 与 smoke 配置分离——smoke 假定后端不可用的降级路径；本套件启动
 * granian（临时 GOALX_DB_PATH）+ vite（代理指向它），先验证空库诚实
 * 显示，再由测试内 seed-demo 灌入动态时间演示数据跑完整业务闭环。
 */
const repoRoot = path.resolve(import.meta.dirname, "../..");
const dbPath = path.join(import.meta.dirname, "test-results", "loop-e2e.db");
const backendPort = 8931;
const webPort = 5199;

export default defineConfig({
	testDir: "./e2e",
	testMatch: "paper-loop.spec.ts",
	timeout: 45_000,
	workers: 1,
	fullyParallel: false,
	retries: 0,
	reporter: [["list"]],
	use: {
		baseURL: `http://127.0.0.1:${webPort}`,
		trace: "on-first-retry",
	},
	globalSetup: "./e2e/loop-global-setup",
	webServer: [
		{
			command: `uv run --no-sync python -m goalx_backend.server goalx_backend.main:app --interface asgi --host 127.0.0.1 --port ${backendPort}`,
			url: `http://127.0.0.1:${backendPort}/healthz`,
			reuseExistingServer: false,
			cwd: repoRoot,
			env: { ...process.env, GOALX_DB_PATH: dbPath },
			timeout: 60_000,
		},
		{
			command: `bun run dev --host 127.0.0.1 --port ${webPort}`,
			url: `http://127.0.0.1:${webPort}`,
			reuseExistingServer: false,
			cwd: import.meta.dirname,
			env: { ...process.env, VITE_DEV_API_TARGET: `http://127.0.0.1:${backendPort}` },
			timeout: 60_000,
		},
	],
});
