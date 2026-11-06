import { join } from "node:path";
import { expect, test } from "@playwright/test";

/**
 * 使用手册截图采集器（v1-release 票 11）：对 serve 栈真实数据页拍摄,
 * 产物落仓库根 docs/user-guide/ 供 docs/USER-GUIDE.md 内嵌。
 *
 * - 重拍触发:普查表触发列"UI 变→重拍"——`task web-guide-shots` 一条命令。
 * - 书商名展示已解禁(2026-11-06 用户裁决:合法数据分析,研究页直名显示),
 *   页面含真实在售场次,USER-GUIDE 正文须注明拍摄日期。
 * - 研究页场次由 GUIDE_FIXTURE_ID 指定(默认今日 tier1 有链场次;换拍日重选)。
 */

const SHOT_DIR = join(import.meta.dirname, "..", "..", "..", "docs", "user-guide");
const FIXTURE_ID = process.env["GUIDE_FIXTURE_ID"] ?? "549";

/** 每页等待语义:标题出现(数据面 hydration 完成后)再截,避免半空页。
 * 2026-11-06 书商名解禁后 /validation 与 /glossary 恢复入册(票 13);
 * 词典页全高 PNG 超 1MB 入库闸 → JPEG(其余页面保持 PNG)。 */
type GuidePage = { name: string; path: string; heading: string; jpeg?: boolean };
const PAGES: readonly GuidePage[] = [
	{ name: "overview", path: "/", heading: "GoalX · 总览" },
	{ name: "fixtures", path: "/fixtures", heading: "GoalX · 场次" },
	{ name: "research", path: `/fixtures/${FIXTURE_ID}`, heading: "GoalX · 场次研究" },
	{ name: "markets-had", path: "/markets/had", heading: "GoalX · 胜平负" },
	{ name: "markets-goals", path: "/markets/goals", heading: "GoalX · 进球" },
	{ name: "markets-pool", path: "/markets/pool", heading: "GoalX · 14场任9" },
	{ name: "markets-pool4", path: "/markets/pool4", heading: "GoalX · 四场进球" },
	{ name: "bets", path: "/bets", heading: "GoalX · 投注" },
	{ name: "history", path: "/history", heading: "GoalX · 历史" },
	{ name: "bankroll", path: "/bankroll", heading: "GoalX · 资金" },
	{ name: "validation", path: "/validation", heading: "GoalX · 验证" },
	{ name: "glossary", path: "/glossary", heading: "GoalX · 词典", jpeg: true },
];

for (const { name, path, heading, jpeg } of PAGES) {
	test(`guide shot: ${name}`, async ({ page }) => {
		await page.goto(path, { waitUntil: "networkidle" });
		await expect(page.getByText(heading, { exact: false }).first()).toBeVisible();
		// 数据行再等一拍(networkidle 后 query 可能仍在途;标题已见,加 500ms 缓冲)
		await page.waitForTimeout(500);
		await page.screenshot({
			path: join(SHOT_DIR, `${name}.${jpeg ? "jpg" : "png"}`),
			fullPage: true,
			animations: "disabled",
			type: jpeg ? "jpeg" : "png",
			...(jpeg ? { quality: 80 } : {}),
		});
	});
}
