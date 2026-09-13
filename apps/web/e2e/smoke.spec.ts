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
	await page.goto("/validation");
	await expect(page.getByTestId("placeholder")).toContainText("M2");
});
