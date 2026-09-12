import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

test("home page renders and stays accessible", async ({ page }) => {
	await page.goto("/");

	await expect(page.getByRole("heading", { name: "GoalX" })).toBeVisible();

	const results = await new AxeBuilder({ page }).analyze();
	expect(results.violations).toEqual([]);
});
