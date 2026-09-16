import { expect, test } from "@playwright/test";
import { expectNoSeriousAxeViolations } from "./axe-baseline";

/**
 * 票 13 IA 落地后的 smoke：导航/占位路由/主题切换 + 每页 axe 基调。
 * smoke 假定后端不可用也能跑（降级路径），除环境里恰有常驻后端时数据路径照常。
 */

const ALL_ROUTES = [
	{ path: "/", heading: "GoalX · 总览" },
	{ path: "/today", heading: "GoalX · 今日" },
	{ path: "/bets", heading: "GoalX · 投注" },
	{ path: "/history", heading: "GoalX · 历史" },
	{ path: "/validation", heading: "GoalX · 验证" },
	{ path: "/bankroll", heading: "GoalX · 资金" },
	{ path: "/glossary", heading: "GoalX · 词典" },
	{ path: "/review", heading: "GoalX · 复核" },
	{ path: "/settings", heading: "GoalX · 设置" },
] as const;

test("overview placeholder is the new home and guides to today", async ({ page }) => {
	await page.goto("/");

	await expect(page.getByRole("heading", { name: "GoalX · 总览" })).toBeVisible();
	await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
	await page.getByRole("link", { name: "先去今日看盘" }).click();
	await expect(page.getByRole("heading", { name: "GoalX · 今日" })).toBeVisible();
});

test("two-level navigation matches the IA and marks the active page", async ({ page }) => {
	await page.goto("/today");

	const primary = page.getByRole("navigation", { name: "主导航" });
	for (const label of ["总览", "今日", "投注", "历史", "验证", "资金"]) {
		await expect(primary.getByRole("link", { name: label })).toBeVisible();
	}
	const secondary = page.getByRole("navigation", { name: "次级导航" });
	for (const label of ["词典", "复核", "设置"]) {
		await expect(secondary.getByRole("link", { name: label })).toBeVisible();
	}
	await expect(primary.getByRole("link", { name: "今日" })).toHaveAttribute("aria-current", "page");

	// 跨级切换后 active 态随路由移动
	await primary.getByRole("link", { name: "历史" }).click();
	await expect(page.getByRole("heading", { name: "GoalX · 历史" })).toBeVisible();
	await expect(primary.getByRole("link", { name: "历史" })).toHaveAttribute("aria-current", "page");
	await expect(primary.getByRole("link", { name: "今日" })).not.toHaveAttribute("aria-current");
});

test("stub pages say what they are and when they arrive", async ({ page }) => {
	await page.goto("/history");
	await expect(page.getByTestId("empty-state")).toContainText("随票 17");
	await page.goto("/glossary");
	await expect(page.getByTestId("empty-state")).toContainText("随票 18");
	await page.goto("/review");
	await expect(page.getByTestId("empty-state")).toContainText("M3");
	await page.goto("/settings");
	await expect(page.getByTestId("empty-state")).toContainText("M4");
});

test("today page renders data or degrades honestly", async ({ page }) => {
	await page.goto("/today");

	await expect(page.getByRole("heading", { name: "GoalX · 今日" })).toBeVisible();
	// 数据路径（常驻后端）或三态空状态（无数据/后端不可用）都算通过
	await expect(page.getByTestId("today-row").first().or(page.getByTestId("empty-state"))).toBeVisible();
});

test("validation page degrades gracefully without backend", async ({ page }) => {
	await page.goto("/validation");
	await expect(page.getByRole("heading", { name: "GoalX · 验证" })).toBeVisible();
	// 后端不可达时 react-query 默认重试 3 次(指数退避)后才进入 error 态
	await expect(page.getByTestId("validation-error")).toBeVisible({ timeout: 20_000 });
});

test("theme toggle switches to dark and persists across reload", async ({ page }) => {
	await page.goto("/");
	// Playwright 默认 colorScheme=light 且无持久化 → 初始浅色
	await expect(page.locator("html")).not.toHaveClass(/dark/);

	await page.getByRole("button", { name: "切换到暗色主题" }).click();
	await expect(page.locator("html")).toHaveClass(/dark/);
	await expect(page.getByRole("button", { name: "切换到浅色主题" })).toBeVisible();

	// 重载保留暗色（localStorage 持久化 + main.tsx 首帧前应用）
	await page.reload();
	await expect(page.locator("html")).toHaveClass(/dark/);
	await expect(page.getByRole("heading", { name: "GoalX · 总览" })).toBeVisible();
});

// 票 13 验收基调：九条路由逐页跑 axe，零严重违例（后续页面票沿用）
for (const route of ALL_ROUTES) {
	test(`axe baseline: ${route.path} has no serious violations`, async ({ page }) => {
		await page.goto(route.path);
		await expect(page.getByRole("heading", { name: route.heading })).toBeVisible();
		await expectNoSeriousAxeViolations(page);
	});
}
