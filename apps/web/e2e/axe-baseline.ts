import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

/**
 * 票 13 确立的 e2e + axe 无障碍验收基调（后续页面票沿用）：
 * 每页零严重违例——不允许 impact 为 serious / critical 的 axe 违例；
 * minor / moderate 记录修复但不阻塞验收。
 */
export async function expectNoSeriousAxeViolations(page: Page): Promise<void> {
	const results = await new AxeBuilder({ page }).analyze();
	const serious = results.violations.filter(
		(violation) => violation.impact === "serious" || violation.impact === "critical",
	);
	expect(serious).toEqual([]);
}
