import { expect, test } from "@playwright/test";
import { expectNoSeriousAxeViolations } from "./axe-baseline";

/**
 * 票 13 IA 落地后的 smoke：导航/占位路由/主题切换 + 每页 axe 基调。
 * smoke 假定后端不可用也能跑（降级路径），除环境里恰有常驻后端时数据路径照常。
 */

const ALL_ROUTES = [
	{ path: "/", heading: "GoalX · 总览" },
	{ path: "/fixtures", heading: "GoalX · 场次" },
	// 票 wb-02：研究页逐页 axe（无数据/404 时空状态也过基调）
	{ path: "/fixtures/1", heading: "GoalX · 场次研究" },
	// 票 wb-03：玩法组三页逐页 axe（胜平负/进球真实页 + 14场任9 占位引导态）
	{ path: "/markets/had", heading: "GoalX · 胜平负" },
	{ path: "/markets/goals", heading: "GoalX · 进球" },
	{ path: "/markets/pool", heading: "GoalX · 14场任9" },
	{ path: "/bets", heading: "GoalX · 投注" },
	{ path: "/history", heading: "GoalX · 历史" },
	{ path: "/validation", heading: "GoalX · 验证" },
	{ path: "/bankroll", heading: "GoalX · 资金" },
	{ path: "/glossary", heading: "GoalX · 词典" },
	{ path: "/review", heading: "GoalX · 复核" },
	{ path: "/settings", heading: "GoalX · 设置" },
] as const;

test("overview triage is the new home and links into fixtures", async ({ page }) => {
	await page.goto("/");

	await expect(page.getByRole("heading", { name: "GoalX · 总览" })).toBeVisible();
	await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
	// 票 15：真实总览（待办清单卡 + 快照四卡）或后端不可用降级态，两者都算通过
	// （react-query 默认重试后才进 error 态，放宽超时；常驻后端下未入金引导的
	// EmptyState 也在页内，or() 会双命中 → .first() 放行严格模式）
	await expect(page.getByTestId("overview-todos").or(page.getByTestId("empty-state")).first()).toBeVisible({
		timeout: 20_000,
	});
	await page.getByRole("navigation", { name: "主导航" }).getByRole("link", { name: "场次" }).click();
	await expect(page.getByRole("heading", { name: "GoalX · 场次" })).toBeVisible();
});

test("two-level navigation matches the IA and marks the active page", async ({ page }) => {
	await page.goto("/fixtures");

	const primary = page.getByRole("navigation", { name: "主导航" });
	for (const label of ["总览", "场次", "玩法", "投注", "历史", "验证", "资金"]) {
		await expect(primary.getByRole("link", { name: label })).toBeVisible();
	}
	const secondary = page.getByRole("navigation", { name: "次级导航" });
	for (const label of ["词典", "复核", "设置"]) {
		await expect(secondary.getByRole("link", { name: label })).toBeVisible();
	}
	await expect(primary.getByRole("link", { name: "场次" })).toHaveAttribute("aria-current", "page");

	// 玩法组（票 wb-03）：入口默认落胜平负，三入口 Tab 在页内
	await primary.getByRole("link", { name: "玩法" }).click();
	await expect(page).toHaveURL(/\/markets\/had$/);
	await expect(page.getByRole("heading", { name: "GoalX · 胜平负" })).toBeVisible();
	await expect(primary.getByRole("link", { name: "玩法" })).toHaveAttribute("aria-current", "page");
	const tabs = page.getByTestId("market-tabs");
	await expect(tabs.getByRole("link", { name: "胜平负" })).toHaveAttribute("aria-current", "page");

	// 进球入口（票 wb-05 已上线真实页）：Tab 点亮 + 口径行常显
	await tabs.getByRole("link", { name: "进球" }).click();
	await expect(page.getByRole("heading", { name: "GoalX · 进球" })).toBeVisible();
	await expect(tabs.getByRole("link", { name: "进球" })).toHaveAttribute("aria-current", "page");
	await expect(page.getByTestId("goals-caliber")).toContainText("比分矩阵推导");
	await expect(primary.getByRole("link", { name: "玩法" })).toHaveAttribute("aria-current", "page");

	// 跨级切换后 active 态随路由移动
	await primary.getByRole("link", { name: "历史" }).click();
	await expect(page.getByRole("heading", { name: "GoalX · 历史" })).toBeVisible();
	await expect(primary.getByRole("link", { name: "历史" })).toHaveAttribute("aria-current", "page");
	await expect(primary.getByRole("link", { name: "玩法" })).not.toHaveAttribute("aria-current");
});

// 票 wb-01：/today 让位 /fixtures——旧路径重定向不破坏书签
test("legacy /today path redirects to the fixtures page", async ({ page }) => {
	await page.goto("/today");

	await expect(page).toHaveURL(/\/fixtures$/);
	await expect(page.getByRole("heading", { name: "GoalX · 场次" })).toBeVisible();
});

test("stub pages say what they are and when they arrive", async ({ page }) => {
	// 票 18：/glossary 已换为真实词典页——见下方词典 smoke 与 axe 循环
	await page.goto("/review");
	await expect(page.getByTestId("empty-state")).toContainText("M3");
	await page.goto("/settings");
	await expect(page.getByTestId("empty-state")).toContainText("M4");
});

// 票 18：词典页真实落地——词条常显、检索过滤、无结果 no-data 空状态
// （票 wb-05 起含进球矩阵词条，票 wb-06 起 Kelly/建议仓位两词条，共 15）
test("glossary page lists first-batch entries, searches, and empties honestly", async ({ page }) => {
	await page.goto("/glossary");

	await expect(page.getByRole("heading", { name: "GoalX · 词典" })).toBeVisible();
	const list = page.getByTestId("glossary-list");
	await expect(list.getByTestId("glossary-card-ev")).toBeVisible();
	expect(await list.locator("article").count()).toBe(15);

	const search = page.getByTestId("glossary-search");
	await search.fill("clv_prob");
	await expect(list.getByTestId("glossary-card-clv")).toBeVisible();
	await expect(list.locator("article")).toHaveCount(1);

	await search.fill("量子纠缠");
	const empty = page.getByTestId("empty-state");
	await expect(empty).toBeVisible();
	await empty.getByRole("button", { name: "清空检索" }).click();
	await expect(list.locator("article")).toHaveCount(15);
});

// 票 17：历史页真实落地——口径行常显，聚合区/空态/降级三选一都算通过
test("history page shows the caliber line and renders aggregation or degrades honestly", async ({ page }) => {
	await page.goto("/history");
	await expect(page.getByRole("heading", { name: "GoalX · 历史" })).toBeVisible();
	await expect(page.getByTestId("history-caliber")).toContainText("统计已锁定且已结算的注");
	// 数据路径（常驻后端含已结算注）与无已结算注/后端不可用的三态空状态都算通过
	// （react-query 默认重试后才进 error 态，放宽超时；常驻后端下空态也在页内，
	// or() 会双命中 → .first() 放行严格模式）
	await expect(page.getByTestId("history-metrics").or(page.getByTestId("empty-state")).first()).toBeVisible({
		timeout: 20_000,
	});
	// 模式大标签常显，默认纸面
	await expect(page.getByTestId("mode-paper")).toHaveAttribute("aria-pressed", "true");
	await expectNoSeriousAxeViolations(page);
});

test("fixtures page renders data or degrades honestly", async ({ page }) => {
	await page.goto("/fixtures");

	await expect(page.getByRole("heading", { name: "GoalX · 场次" })).toBeVisible();
	// 数据路径（常驻后端）或三态空状态（无数据/后端不可用）都算通过
	await expect(page.getByTestId("fixtures-row").first().or(page.getByTestId("empty-state"))).toBeVisible({
		timeout: 20_000,
	});
});

// 票 wb-02：研究页数据路径（逐书赔率/共识/模型）或 404/降级空状态都算通过
// （常驻后端为旧版本无研究端点 → 404 no-data 态；新版后端含 fixture 1 时照常渲染）
test("fixture research page renders books or degrades honestly", async ({ page }) => {
	await page.goto("/fixtures/1");

	await expect(page.getByRole("heading", { name: "GoalX · 场次研究" })).toBeVisible();
	await expect(page.getByTestId("research-page").or(page.getByTestId("empty-state")).first()).toBeVisible({
		timeout: 20_000,
	});
});

// 票 wb-03：玩法页数据路径（推荐流/组合卡）或空态（组合无正 EV 机会是诚实结果）
// 或后端不可用降级——三者都算通过；组合空窗时推荐流照常渲染
test("had market page renders the feed and combo or degrades honestly", async ({ page }) => {
	await page.goto("/markets/had");

	await expect(page.getByRole("heading", { name: "GoalX · 胜平负" })).toBeVisible();
	// 推荐流卡片 / 页面级空态（无数据/后端不可用）先到其一
	await expect(
		page
			.getByTestId(/^market-card-/)
			.first()
			.or(page.getByTestId("empty-state")),
	).toBeVisible({
		timeout: 20_000,
	});
	// 组合区在数据路径常驻：推荐腿 / 诚实占位（无正 EV / 计算中）；
	// 降级路径（页面级 empty-state 已命中）不再要求组合区——与 goals 页同款双路径
	await expect(page.getByTestId("market-combo").or(page.getByTestId("empty-state")).first()).toBeVisible({
		timeout: 20_000,
	});
	await expect(
		page
			.getByTestId(/^market-combo-pick-/)
			.first()
			.or(page.getByTestId("market-combo-empty"))
			.or(page.getByTestId("market-combo-pending"))
			.or(page.getByTestId("empty-state")),
	).toBeVisible({ timeout: 20_000 });
});

// 票 wb-07：14场任9 骨架页——横幅级骨架说明常显（无彩池数据时整页一眼是骨架），
// 期次选择/14 槽位（数据或虚线待数据）/三档映射/留位/提交占位结构完整可演示
test("pool market skeleton page is honestly a skeleton with full structure", async ({ page }) => {
	await page.goto("/markets/pool");

	await expect(page.getByRole("heading", { name: "GoalX · 14场任9" })).toBeVisible();
	await expect(page.getByTestId("pool-skeleton-banner")).toContainText("骨架页");
	await expect(page.getByTestId("pool-skeleton-banner")).toContainText("goalx-quant");
	// 口径行常显：占位概率口径 + 标记非生成器
	await expect(page.getByTestId("pool-caliber")).toContainText("欧共识");
	// 期次选择 + 14 个槽位（数据场或虚线待数据——结构骨架与后端可用性无关）
	await expect(page.getByTestId("pool-period-select")).toBeVisible();
	const slots = page.getByTestId("pool-slots");
	await expect(slots.locator('[data-testid^="pool-slot-"]')).toHaveCount(14);
	// 三档映射标注 + 留位 not-available 三块 + 提交占位 disabled
	await expect(page.getByTestId("pool-tier-mapping")).toContainText("保守 = flat");
	await expect(page.getByTestId("pool-coming-soon").getByTestId("empty-state")).toHaveCount(3);
	const submit = page.getByTestId("pool-submit");
	await expect(submit).toBeDisabled();
	await expect(submit).toContainText("占位");
});

// 票 wb-05：进球玩法页数据路径（推荐流/组合卡）或空态/降级都算通过
// （常驻后端为旧版本无 /markets/goals 时按后端不可用降级——契约先行的双路径）
test("goals market page renders the feed and combo or degrades honestly", async ({ page }) => {
	await page.goto("/markets/goals");

	await expect(page.getByRole("heading", { name: "GoalX · 进球" })).toBeVisible();
	// 口径行常显（矩阵推导 + 模型×竞彩价 + 单关为主）
	await expect(page.getByTestId("goals-caliber")).toContainText("比分矩阵推导");
	// 推荐流卡片 / 页面级空态（无数据/后端不可用/旧后端无该端点）先到其一
	await expect(
		page
			.getByTestId(/^goals-card-\d+$/)
			.first()
			.or(page.getByTestId("empty-state")),
	).toBeVisible({
		timeout: 20_000,
	});
	// 组合区在数据路径常驻：推荐腿 / 诚实占位（无正 EV/无模型/计算中）三选一；
	// 降级路径（empty-state 已命中）不再要求组合区
	await expect(
		page
			.getByTestId(/^goals-combo-pick-\d+$/)
			.first()
			.or(page.getByTestId("goals-combo-empty"))
			.or(page.getByTestId("goals-combo-pending"))
			.or(page.getByTestId("empty-state")),
	).toBeVisible({ timeout: 20_000 });
});

test("validation page renders verdict or degrades gracefully without backend", async ({ page }) => {
	await page.goto("/validation");
	await expect(page.getByRole("heading", { name: "GoalX · 验证" })).toBeVisible();
	// 票 19：首屏换为状态结论行（三条件 x/3）——双路径（票 13 基调）：常驻后端时结论
	// 照常渲染；后端不可达时 react-query 默认重试 3 次(指数退避)后才进入 error 态
	// （放宽超时等重试走完）。18 号的 metric-回测 skill 契约保留（对比次级区，数据路径可见）。
	await expect(page.getByTestId("validation-verdict").or(page.getByTestId("validation-error")).first()).toBeVisible({
		timeout: 20_000,
	});
	await expect(page.getByTestId("metric-回测 skill").or(page.getByTestId("validation-error")).first()).toBeVisible();
});

// 票 20：资金页重设计——口径行常显，余额大数字（含未入金引导）/降级双路径都算通过
test("bankroll page shows the caliber line and renders balance or degrades honestly", async ({ page }) => {
	await page.goto("/bankroll");
	await expect(page.getByRole("heading", { name: "GoalX · 资金" })).toBeVisible();
	await expect(page.getByTestId("bankroll-caliber")).toContainText("只受真金");
	// 常驻后端：余额大数字（未入金也在）或降级提示；后端不可达：error 态
	await expect(page.getByTestId("bankroll-balance").or(page.getByTestId("bankroll-error")).first()).toBeVisible({
		timeout: 20_000,
	});
	await expectNoSeriousAxeViolations(page);
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

// 票 13 验收基调：逐页跑 axe，零严重违例（票 wb-02 起含研究页，票 wb-03 起含玩法组三页）
for (const route of ALL_ROUTES) {
	test(`axe baseline: ${route.path} has no serious violations`, async ({ page }) => {
		await page.goto(route.path);
		await expect(page.getByRole("heading", { name: route.heading })).toBeVisible();
		await expectNoSeriousAxeViolations(page);
	});
}
