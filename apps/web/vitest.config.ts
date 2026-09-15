import path from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
	resolve: {
		alias: {
			"@": path.resolve(import.meta.dirname, "./src"),
		},
	},
	test: {
		environment: "jsdom",
		setupFiles: ["./src/test/setup.ts"],
		include: ["src/**/*.test.{ts,tsx}"],
		coverage: {
			provider: "v8",
			reporter: ["text", "html", "json"],
			include: ["src/**"],
			// src/components/ui/** 是 shadcn registry 的 copy-in 组件（票 11 决策）：
			// 属 vendor 代码，单测它等于测上游；改动跟随上游 registry，
			// 真实行为由使用它的页面单测与 e2e/axe 覆盖。不豁免 Biome（是源码不是产物）。
			exclude: [
				"src/api/generated/**",
				"src/components/ui/**",
				"src/test/**",
				"src/mocks/**",
				"src/main.tsx",
				"src/router.tsx",
			],
			thresholds: {
				lines: 85,
				branches: 85,
				functions: 85,
				statements: 85,
			},
		},
	},
});
