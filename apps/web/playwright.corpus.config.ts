import path from "node:path";
import { defineConfig } from "@playwright/test";

/**
 * corpus 主链 e2e（boundary-hardening 票 06）：四场制式语料夹具上跑
 * 今日页 + 纸面锁定全链，断言读的是 corpus 面非 legacy 降级。
 *
 * 与 loop 配置分离（seed-demo 演示数据）；backend 进程注入
 * GOALX_CORPUS_ROOT 指向夹具语料树——corpus reader 不再缺席降级。
 */
const repoRoot = path.resolve(import.meta.dirname, "../..");
const dbPath = path.join(import.meta.dirname, "test-results", "corpus-e2e.db");
const corpusRoot = path.join(import.meta.dirname, "test-results", "corpus-e2e-tree");
const backendPort = Number(process.env["E2E_CORPUS_BACKEND_PORT"] ?? 8932);
const webPort = Number(process.env["E2E_CORPUS_WEB_PORT"] ?? 5200);

export default defineConfig({
	testDir: "./e2e",
	testMatch: "corpus-loop.spec.ts",
	timeout: 45_000,
	workers: 1,
	fullyParallel: false,
	retries: 0,
	reporter: [["list"]],
	use: {
		baseURL: `http://127.0.0.1:${webPort}`,
		trace: "on-first-retry",
	},
	globalSetup: "./e2e/corpus-global-setup",
	webServer: [
		{
			command: `uv run --no-sync python -m goalx_backend.server goalx_backend.main:app --interface asgi --host 127.0.0.1 --port ${backendPort}`,
			url: `http://127.0.0.1:${backendPort}/healthz`,
			reuseExistingServer: false,
			cwd: repoRoot,
			env: { ...process.env, GOALX_DB_PATH: dbPath, GOALX_CORPUS_ROOT: corpusRoot },
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
