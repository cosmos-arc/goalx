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
			exclude: ["src/api/generated/**", "src/test/**", "src/mocks/**", "src/main.tsx", "src/router.tsx"],
			thresholds: {
				lines: 85,
				branches: 85,
				functions: 85,
				statements: 85,
			},
		},
	},
});
