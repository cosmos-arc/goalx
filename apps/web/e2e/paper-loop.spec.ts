/**
 * 纸面用户闭环 e2e（票 36 验收）：隔离真实 API/DB 的完整业务流程。
 *
 * 浏览器操作 → API 状态 → SQLite 落库三方断言一致；纸面流程全程真实
 * 资金变化为 0；服务器在提交时重新校验停售/过期（不止禁用按钮）。
 */
import { execSync } from "node:child_process";
import path from "node:path";
import { expect, test } from "@playwright/test";

const repoRoot = path.resolve(import.meta.dirname, "../..");

const DB_ENV = {
	...process.env,
	GOALX_DB_PATH: path.join(import.meta.dirname, "../test-results/loop-e2e.db"),
};

/** 在隔离库上执行只读 SQL，返回 JSON 行。 */
function dbRows(sql: string): Array<Record<string, unknown>> {
	const script = `import json,sqlite3;conn=sqlite3.connect("${DB_ENV["GOALX_DB_PATH"]}");conn.row_factory=sqlite3.Row;print(json.dumps([dict(r) for r in conn.execute(${JSON.stringify(sql)})]))`;
	const out = execSync(`uv run --no-sync python -c ${JSON.stringify(script)}`, {
		cwd: repoRoot,
		env: DB_ENV,
		encoding: "utf8",
	});
	return JSON.parse(out) as Array<Record<string, unknown>>;
}

test.describe.configure({ mode: "serial" });

test("空库状态诚实显示，无需手查 Fixture ID", async ({ page }) => {
	await page.goto("/fixtures");
	// 票 13：统一空状态组件——空库是无数据态（非后端不可用）
	await expect(page.getByTestId("empty-state")).toHaveAttribute("data-variant", "no-data");

	await page.goto("/bets");
	await expect(page.getByText("无未锁定建议。")).toBeVisible();
	await expect(page.getByText("无已锁定注——建议锁定后在这里等待开奖。")).toBeVisible();

	await page.goto("/bankroll");
	await expect(page.getByTestId("bankroll-balance")).toHaveText("尚未入金");
	await expect(page.getByTestId("cost-missing")).toBeVisible();
});

test("seed-demo 灌入隔离演示数据（动态时间，不写主库）", async () => {
	execSync("uv run --no-sync python -m goalx_backend.cli seed-demo", {
		cwd: repoRoot,
		env: DB_ENV,
		encoding: "utf8",
		stdio: "pipe",
	});
	const fixtures = dbRows("SELECT COUNT(*) AS n FROM match_codes WHERE kind='jingcai'");
	expect(Number(fixtures[0]?.["n"])).toBe(3);
});

test("场次页展示资格判定与拒绝原因", async ({ page }) => {
	await page.goto("/fixtures");
	await expect(page.getByTestId("fixtures-row")).toHaveCount(3);
	const validVerdict = page.getByTestId("had-quote-valid").first();
	await expect(validVerdict).toHaveText(/可投/);
	await expect(validVerdict).toHaveText(/单固/);
	await expect(page.getByTestId("had-quote-rejected")).toHaveText(/已停售/);
	// 票 14：可投卡片置顶（001 单固在售 + 002 仅串关在售），003 停售弱化
	await expect(page.getByTestId("fixtures-card-1")).toBeVisible();
	await expect(page.getByTestId("fixtures-card-2")).toBeVisible();
	await expect(page.getByTestId("fixtures-not-eligible-count")).toHaveText(/1 场不可投/);
});

test("玩法页推荐流排序与组合头部（票 wb-03）：无正 EV 诚实空态，不建注", async ({ page }) => {
	await page.goto("/markets/had");
	await expect(page.getByRole("heading", { name: "GoalX · 胜平负" })).toBeVisible();

	// demo 种子三场 EV 全为负 → 推荐流照常渲染（沉底按开赛时间：001/002/003）
	const feed = page.getByTestId(/^market-card-/);
	await expect(feed).toHaveCount(3);
	await expect(feed.first()).toContainText("周六001");
	// 停售场（003）：按钮禁用 + 拒绝徽章
	await expect(page.getByTestId("market-pick-3-h")).toBeDisabled();

	// 组合头部：EV≤0 不入选 → 诚实占位（EV 是诊断量非机会信号），不出现带入按钮
	const combo = page.getByTestId("market-combo");
	await expect(combo).toBeVisible();
	await expect(combo.getByTestId("market-combo-empty")).toContainText("无正 EV 机会");
	await expect(combo.getByTestId("market-combo-apply")).toHaveCount(0);
	// 约束说明常驻（口径可对账）
	await expect(combo.getByTestId("market-combo-notes")).toContainText("共识口径");

	// 就地选注可用（不提交——保持后续步骤的注数断言不变）：003 停售不可选，002 可选
	await page.getByTestId("market-pick-2-a").click();
	await expect(page.getByTestId("basket-count")).toHaveText("1/2");
	await page.getByTestId("basket-open").click();
	await expect(page.getByTestId("basket-leg")).toHaveCount(1);
	await page.getByRole("button", { name: "继续浏览" }).click();
	await expect(page.getByTestId("basket-stake")).toHaveCount(0);
});

test("场次行点进研究页：逐书赔率与共识可见，可返回", async ({ page }) => {
	await page.goto("/fixtures");
	await page.getByTestId("fixtures-link-1").click();

	await expect(page.getByRole("heading", { name: "GoalX · 场次研究" })).toBeVisible();
	// demo 种子 fixture 1 带三家欧赔书 → 逐书行 + 去水共识；无 Forecast → 模型区诚实占位
	await expect(page.getByTestId("research-book-row")).toHaveCount(3);
	await expect(page.getByTestId("research-consensus")).toContainText("家三向均价");
	await expect(page.getByTestId("research-model-missing")).toContainText("暂无模型预测");
	// 资格徽章随研究页头部内嵌；一级导航"场次"保持激活
	await expect(page.getByTestId("had-quote-valid").first()).toContainText("可投");
	await expect(page.getByRole("navigation", { name: "主导航" }).getByRole("link", { name: "场次" })).toHaveAttribute(
		"aria-current",
		"page",
	);

	await page.getByRole("navigation", { name: "返回" }).getByRole("link").click();
	await expect(page.getByRole("heading", { name: "GoalX · 场次" })).toBeVisible();
});

test("had 单固纸面闭环：建议→锁定→(资金变化 0)", async ({ page }) => {
	await page.goto("/fixtures");
	await page.getByTestId("pick-1-h").click();
	// 票 14：金额/模式在选注篮抽屉里（底部常驻条 → 右侧 Drawer）
	await page.getByTestId("basket-open").click();
	// 票 wb-06：选注篮建议仓位（只读）——demo 种子主胜 EV 为负 → EV≤0 诚实建议 ¥0
	// （flat ¥2/未入金 的正路径在下方进球步覆盖：ttg s2 为正 EV）
	const advice = page.getByTestId("basket-stake-advice");
	await expect(advice).toContainText("¥0.00");
	await expect(advice).toContainText("建议不投");
	await page.getByTestId("basket-stake").fill("100");
	await page.getByTestId("basket-strategy").fill("manual-v1");
	await page.getByTestId("basket-submit").click();
	await expect(page.getByTestId("fixtures-message")).toContainText("已建建议");

	await page.goto("/bets");
	// 票 16 三段分组：未锁定建议组头计数（组头 = 标题 + 计数徽章，textContent 空格分隔）
	await expect(page.getByTestId("section-suggestions")).toContainText("未锁定建议 1");
	await page.getByTestId("lock-1").click();
	await expect(page.getByTestId("bets-message")).toContainText("已锁定纸面票");

	// 重复提交被服务器拒绝（可操作的错误提示，可重试其他操作）
	const again = await page.request.post("/api/v1/bet-slips", { data: { bet_ids: [1] } });
	expect(again.status()).toBe(400);
	expect(await again.text()).toContain("不能再次回录");

	await expect(page.getByTestId("section-locked")).toContainText("已锁定 1");

	// 真实资金变化 0：bankroll 无事件（浏览器 + API + DB 三方一致）
	await page.goto("/bankroll");
	await expect(page.getByTestId("bankroll-empty")).toBeVisible();
	const apiBank = await page.request.get("/api/v1/bankroll");
	expect((await apiBank.json())["events"]).toHaveLength(0);
	expect(dbRows("SELECT COUNT(*) AS n FROM bankroll_events")[0]?.["n"]).toBe(0);
});

test("2串1 共同可购买；无效腿由服务器拒绝（不止禁用按钮）", async ({ page }) => {
	await page.goto("/fixtures");
	await page.getByTestId("pick-1-h").click();
	await page.getByTestId("pick-2-a").click();
	await page.getByTestId("basket-open").click();
	await page.getByTestId("basket-stake").fill("2");
	await page.getByTestId("basket-submit").click();
	await expect(page.getByTestId("fixtures-message")).toContainText("已建建议");

	// 锁定 2串1（两腿同一 as_of 判定，共同可购买）
	await page.goto("/bets");
	await page.getByTestId("lock-2").click();
	await expect(page.getByTestId("bets-message")).toContainText("已锁定纸面票");

	// 停售场次：按钮禁用 + 行弱化（票 14 规则前置——无效组合到不了提交）
	await page.goto("/fixtures");
	await expect(page.getByTestId("pick-3-h")).toBeDisabled();
	await expect(page.getByTestId("fixtures-row").nth(2)).toHaveClass(/bg-muted\/50/);

	// 服务器侧：直接以 API 提交含停售腿的串关 → 400 + 原因（前端禁用不替代服务器判定）
	const today = await page.request.get("/api/v1/fixtures/today");
	const rows = (await today.json()) as Array<{
		fixture_id: number;
		match_code: string;
	}>;
	const stopped = rows.find((row) => row.match_code === "周六003");
	const ok = rows.find((row) => row.match_code === "周六001");
	expect(stopped && ok).toBeTruthy();
	const rejected = await page.request.post("/api/v1/bets", {
		data: {
			mode: "paper",
			stake: 2,
			legs: [
				{ fixture_id: ok?.fixture_id, market_code: "had", selection_code: "h", locked_odds: 6.5 },
				{ fixture_id: stopped?.fixture_id, market_code: "had", selection_code: "h", locked_odds: 2.0 },
			],
		},
	});
	expect(rejected.status()).toBe(400);
	expect(await rejected.text()).toContain("sale_stopped");
});

test("真实回录：实际条款结算、建议快照保留、账务按实际金额", async ({ page }) => {
	await page.goto("/fixtures");
	// live 单关同样须单固：选 fixture 1 客胜（该场最终 0:1 客胜）
	await page.getByTestId("pick-1-a").click();
	await page.getByTestId("basket-open").click();
	await page.getByTestId("basket-mode").selectOption("live");
	await page.getByTestId("basket-stake").fill("10");
	await page.getByTestId("basket-submit").click();
	await expect(page.getByTestId("fixtures-message")).toContainText("已建建议");

	await page.goto("/bets");
	// 票 16：回录走右侧 Drawer（不再内联在表格列），含快照对照与按实际条款记账提示
	await page
		.locator('[data-testid="section-suggestions"] [data-testid="bet-row"]', {
			hasText: "真金",
		})
		.getByRole("button", { name: "回录实际条款" })
		.click();
	const drawer = page.getByTestId("actual-drawer");
	await drawer.getByLabel("实际金额").fill("12");
	await drawer.getByLabel(/实际赔率/).fill("1.2");
	await expect(page.getByTestId("actual-diff-note")).toContainText("按实际条款记账");
	await drawer.getByRole("button", { name: "提交" }).click();
	await expect(page.getByTestId("bets-message")).toContainText("已回录真实购买票");

	// live 注金事件按实际金额 -12
	const bank = await (await page.request.get("/api/v1/bankroll")).json();
	expect(bank["balance"]).toBe(-12);
});

test("开奖→结算→复盘：胜负面目、盈亏与缺 closing 诚实展示", async ({ page }) => {
	await page.goto("/bets");
	await page.getByTestId("draw-fixture").selectOption({ label: "周六001 阿森纳 vs 切尔西" });
	await page.getByTestId("draw-home").fill("3");
	await page.getByTestId("draw-away").fill("1");
	await page.getByRole("button", { name: "导入" }).click();
	await expect(page.getByTestId("bets-message")).toContainText("已导入 1 条开奖结果");

	await page.getByTestId("draw-fixture").selectOption({ label: "周六002 利物浦 vs 曼城" });
	await page.getByTestId("draw-home").fill("0");
	await page.getByTestId("draw-away").fill("2");
	await page.getByRole("button", { name: "导入" }).click();
	await expect(page.getByTestId("bets-message")).toContainText("已导入 1 条开奖结果");

	await page.getByRole("button", { name: "结算批跑" }).click();
	await expect(page.getByTestId("bets-message")).toContainText("结算完成");

	// 复盘（票 16 分组：结算后的注全在已结算段，纸面/真金以行内徽章区分）：纸面单关
	// ¥100@6.5 命中 +550；串关命中 +26.6；live 客胜腿此时未中 -12
	const settledSection = page.getByTestId("section-settled");
	await expect(settledSection.getByText("+550.00")).toBeVisible();
	await expect(settledSection.getByText("+26.60")).toBeVisible();
	await expect(settledSection.locator('[data-testid="bet-row"]', { hasText: "真金" }).first()).toContainText("-12.00");
	await expect(settledSection.getByText("缺 closing").first()).toBeVisible();
	await expect(settledSection.getByText("live 单独分组")).toBeVisible();
	// live 建议快照保留：¥10@1.30 → 实际 ¥12@1.20
	await expect(settledSection.getByText("¥10.00→¥12.00")).toBeVisible();
	await expect(settledSection.getByText("1.30→1.20")).toBeVisible();

	const bank = await (await page.request.get("/api/v1/bankroll")).json();
	expect(bank["balance"]).toBe(-12); // live 腿未中: 只有实际注金流出
});

test("更正路径：预览影响→带原因导入→原子重算，纸面资金仍为 0 变化", async ({ page }) => {
	await page.goto("/bets");
	await page.getByTestId("draw-fixture").selectOption({ label: "周六001 阿森纳 vs 切尔西" });
	await expect(page.getByTestId("draw-correction-reason")).toBeVisible();
	await page.getByTestId("draw-home").fill("0");
	await page.getByTestId("draw-away").fill("1");
	await page.getByTestId("draw-correction-reason").fill("official corrected result");
	await page.getByTestId("draw-preview").click();
	const preview = page.getByTestId("draw-preview-result");
	await expect(preview).toBeVisible();
	await expect(preview).toContainText("更正");
	await expect(page.getByTestId("preview-affected-row").first()).toBeVisible();

	await page.getByRole("button", { name: "导入" }).click();
	await expect(page.getByTestId("bets-message")).toContainText("已导入 1 条开奖结果");

	// 原子重算：纸面单关/串关由胜转负（仍在已结算段，票 16 分组）
	const settledSection = page.getByTestId("section-settled");
	await expect(settledSection.getByText("+550.00")).toHaveCount(0);
	await expect(settledSection.getByText("负").first()).toBeVisible();
	// live 客胜腿经更正反败为胜: 实际条款 12×1.2=14.4, 冲正兑付后余额 +2.4
	await expect(settledSection.locator('[data-testid="bet-row"]', { hasText: "真金" }).first()).toContainText("+2.40");
	const bank = await (await page.request.get("/api/v1/bankroll")).json();
	expect(Number(bank["balance"])).toBeCloseTo(2.4, 2);

	// 幂等：同一更正重复导入不新增修订
	const before = dbRows("SELECT COUNT(*) AS n FROM draw_result_revisions")[0]?.["n"];
	await page.request.post("/api/v1/draw-results", {
		data: {
			source: "manual",
			results: [
				{
					fixture_id: 1,
					home_goals: 0,
					away_goals: 1,
					correction_reason: "official corrected result (replay)",
				},
			],
		},
	});
	const after = dbRows("SELECT COUNT(*) AS n FROM draw_result_revisions")[0]?.["n"];
	expect(after).toBe(before);
	expect(dbRows("SELECT COUNT(*) AS n FROM bankroll_events WHERE kind='bet_stake'")[0]?.["n"]).toBe(1);
});

test("进球玩法页（票 wb-05）：模型 EV 组合 → ttg 独立单关落地（paper 不动真金）", async ({ page }) => {
	await page.goto("/markets/goals");
	await expect(page.getByRole("heading", { name: "GoalX · 进球" })).toBeVisible();

	// demo 002 带模型：ttg s2 @4.50 → 模型 EV≈+10.3% → 组合非空（模型×竞彩价口径）
	const combo = page.getByTestId("goals-combo");
	await expect(combo.getByTestId("goals-combo-pick-2")).toContainText("总进球 2");
	await expect(combo.getByTestId("goals-combo-notes")).toContainText("不组串");
	// 001 无模型（概率/EV 空缺）、003 停售（禁用）：推荐流诚实呈现
	await expect(page.getByTestId("goals-model-note-1")).toContainText("无模型覆盖");
	await expect(page.getByTestId("goals-pick-3-ttg-2")).toBeDisabled();

	// 一键带入 → 每注独立单关提交；bankroll ¥2.40 → flat 档按最低注 ¥2 建议
	await combo.getByTestId("goals-combo-apply").click();
	await expect(page.getByTestId("goals-basket-count")).toHaveText("1/3");
	await expect(page.getByTestId("goals-basket-stake")).toHaveValue("2");
	// 票 wb-06：进球篮建议仓位——bankroll ¥2.40 过小，flat 档按最低注 ¥2 建议并说明越限
	const goalsAdvice = page.getByTestId("goals-basket-stake-advice");
	await expect(goalsAdvice).toContainText("¥2.00");
	await expect(goalsAdvice).toContainText("超出 5% 上限");
	await page.getByTestId("goals-basket-submit").click();
	await expect(page.getByTestId("goals-market-message")).toContainText("已建 1 条单关建议");

	const goalsBets = dbRows(
		"SELECT COUNT(*) AS n FROM bets b JOIN bet_legs l ON l.bet_id = b.id WHERE l.market_code = 'ttg'",
	);
	expect(Number(goalsBets[0]?.["n"])).toBe(1);
});

test("浏览器、API 与 DB 最终状态一致", async ({ page }) => {
	const apiBets = (
		(await (await page.request.get("/api/v1/bets")).json()) as Array<{
			id: number;
		}>
	).map((bet) => bet.id);
	const dbBets = dbRows("SELECT id FROM bets").map((row) => Number(row["id"]));
	expect(new Set(apiBets)).toEqual(new Set(dbBets));

	await page.goto("/bets");
	await expect(page.getByTestId("bet-row")).toHaveCount(apiBets.length);

	// paper 流程全程 0 真金流水：bankroll 只有 live 的一笔 stake 与一笔 payout
	const kinds = dbRows("SELECT kind, COUNT(*) AS n FROM bankroll_events GROUP BY kind ORDER BY kind");
	expect(kinds).toEqual([
		{ kind: "bet_payout", n: 1 },
		{ kind: "bet_stake", n: 1 },
	]);
	expect(dbRows("SELECT COUNT(*) AS n FROM settlements")[0]?.["n"]).toBeGreaterThanOrEqual(3);
});
