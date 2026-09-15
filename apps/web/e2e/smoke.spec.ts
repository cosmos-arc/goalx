import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

test("today page renders with navigation and stays accessible", async ({ page }) => {
	await page.goto("/");

	await expect(page.getByRole("heading", { name: "GoalX · 今日" })).toBeVisible();
	await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();

	const results = await new AxeBuilder({ page }).analyze();
	expect(results.violations).toEqual([]);
});

test("six-page skeleton navigates", async ({ page }) => {
	await page.goto("/");
	for (const label of ["复核", "投注", "资金", "验证", "设置"]) {
		await page.getByRole("link", { name: label }).click();
		await expect(page.getByRole("heading", { name: new RegExp(label) })).toBeVisible();
	}
});

test("placeholder pages announce their milestone", async ({ page }) => {
	await page.goto("/review");
	await expect(page.getByTestId("placeholder")).toContainText("M3");
	await page.goto("/settings");
	await expect(page.getByTestId("placeholder")).toContainText("M4");
});

test("validation page degrades gracefully without backend", async ({ page }) => {
	await page.goto("/validation");
	await expect(page.getByRole("heading", { name: "验证" })).toBeVisible();
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
	await expect(page.getByRole("heading", { name: "GoalX · 今日" })).toBeVisible();
});
