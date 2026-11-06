import { defineConfig, devices } from "@playwright/test";

/**
 * 使用手册截图（v1-release 票 11）：对 serve 栈真实数据页拍摄 docs/user-guide/。
 *
 * 与 smoke 配置分离——本套件不是断言测试,是"UI 变更后一条命令重拍"的采集器
 * (task web-guide-shots)。两条防呆(correctness F1/F2):
 * - 专用端口 + 不复用既有 server(主工作区 5173 常驻——复用会拍到别的树的 UI
 *   静默覆盖入册 PNG;同 loop/corpus 专用端口惯例);
 * - globalSetup 探活后端(/healthz):后端不在即 fail-loud,降级态拍摄须显式
 *   GUIDE_ALLOW_DEGRADED=1(标题门是静态壳,降级页会"绿着"覆盖好图)。
 */
const webPort = Number(process.env["E2E_WEB_PORT"] ?? 5210);

export default defineConfig({
	testDir: "./e2e",
	testMatch: "guide-shots.spec.ts",
	globalSetup: "./e2e/guide-global-setup",
	timeout: 60_000,
	workers: 1,
	fullyParallel: false,
	retries: 0,
	reporter: [["list"]],
	use: {
		baseURL: `http://127.0.0.1:${webPort}`,
		trace: "off",
		...devices["Desktop Chrome"],
		viewport: { width: 1440, height: 900 },
		// 1x 即可入册:2x 全页 PNG 超 pre-commit 1MB 大文件闸(markets-pool 1018KB 实证)
		deviceScaleFactor: 1,
	},
	webServer: {
		command: `bun run dev --host 127.0.0.1 --port ${webPort}`,
		url: `http://127.0.0.1:${webPort}`,
		reuseExistingServer: false,
		timeout: 120_000,
	},
});
