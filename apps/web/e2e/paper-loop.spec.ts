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
	await page.goto("/");
	await expect(page.getByTestId("today-empty")).toBeVisible();

	await page.goto("/bets");
	await expect(page.getByText("无未锁定建议。")).toBeVisible();
	await expect(page.getByText("无已锁定纸面注。")).toBeVisible();

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

test("今日页展示资格判定与拒绝原因", async ({ page }) => {
	await page.goto("/");
	await expect(page.getByTestId("today-row")).toHaveCount(3);
	const validVerdict = page.getByTestId("had-quote-valid").first();
	await expect(validVerdict).toHaveText(/可投/);
	await expect(validVerdict).toHaveText(/单固/);
	await expect(page.getByTestId("had-quote-rejected")).toHaveText(/已停售/);
});

test("had 单固纸面闭环：建议→锁定→(资金变化 0)", async ({ page }) => {
	await page.goto("/");
	await page.getByTestId("pick-1-h").click();
	await page.getByTestId("basket-stake").fill("100");
	await page.getByTestId("basket-strategy").fill("manual-v1");
	await page.getByRole("button", { name: "建立建议" }).click();
	await expect(page.getByTestId("today-message")).toContainText("已建建议");

	await page.goto("/bets");
	await expect(page.getByTestId("section-suggestions")).toContainText("未锁定建议（1）");
	await page.getByTestId("lock-1").click();
	await expect(page.getByTestId("bets-message")).toContainText("已锁定纸面票");

	// 重复提交被服务器拒绝（可操作的错误提示，可重试其他操作）
	const again = await page.request.post("/api/v1/bet-slips", { data: { bet_ids: [1] } });
	expect(again.status()).toBe(400);
	expect(await again.text()).toContain("不能再次回录");

	await expect(page.getByTestId("section-locked-paper")).toContainText("已锁定纸面（1）");

	// 真实资金变化 0：bankroll 无事件（浏览器 + API + DB 三方一致）
	await page.goto("/bankroll");
	await expect(page.getByTestId("bankroll-empty")).toBeVisible();
	const apiBank = await page.request.get("/api/v1/bankroll");
	expect((await apiBank.json())["events"]).toHaveLength(0);
	expect(dbRows("SELECT COUNT(*) AS n FROM bankroll_events")[0]?.["n"]).toBe(0);
});

test("2串1 共同可购买；无效腿由服务器拒绝（不止禁用按钮）", async ({ page }) => {
	await page.goto("/");
	await page.getByTestId("pick-1-h").click();
	await page.getByTestId("pick-2-a").click();
	await page.getByTestId("basket-stake").fill("2");
	await page.getByRole("button", { name: "建立建议" }).click();
	await expect(page.getByTestId("today-message")).toContainText("已建建议");

	// 锁定 2串1（两腿同一 as_of 判定，共同可购买）
	await page.goto("/bets");
	await page.getByTestId("lock-2").click();
	await expect(page.getByTestId("bets-message")).toContainText("已锁定纸面票");

	// 停售场次作第二腿：客户端提示拒绝原因
	await page.goto("/");
	await page.getByTestId("pick-1-h").click();
	await page.getByTestId("pick-3-h").click();
	await expect(page.getByTestId("today-message")).toContainText("不可作第二腿");
	await expect(page.getByTestId("today-message")).toContainText("已停售");

	// 服务器侧：直接以 API 提交含停售腿的串关 → 400 + 原因
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
	await page.goto("/");
	// live 单关同样须单固：选 fixture 1 客胜（该场最终 0:1 客胜）
	await page.getByTestId("pick-1-a").click();
	await page.getByTestId("basket-mode").selectOption("live");
	await page.getByTestId("basket-stake").fill("10");
	await page.getByRole("button", { name: "建立建议" }).click();
	await expect(page.getByTestId("today-message")).toContainText("已建建议");

	await page.goto("/bets");
	const liveRow = page.locator('[data-testid="section-suggestions"] [data-testid="bet-row"]', {
		hasText: "真金",
	});
	await liveRow.getByRole("button", { name: "回录实际条款" }).click();
	await liveRow.getByLabel("实际金额").fill("12");
	await liveRow.getByLabel(/实际赔率/).fill("1.2");
	await liveRow.getByRole("button", { name: "提交" }).click();
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

	// 复盘：纸面单关 ¥100@6.5 命中 +550；串关命中 +26.6；live 客胜腿此时未中 -12
	await expect(page.getByTestId("section-locked-paper").getByText("+550.00")).toBeVisible();
	await expect(page.getByTestId("section-locked-paper").getByText("+26.60")).toBeVisible();
	await expect(page.getByTestId("section-live").getByText("-12.00")).toBeVisible();
	await expect(page.getByTestId("section-locked-paper").getByText("缺 closing").first()).toBeVisible();
	await expect(page.getByTestId("section-live").getByText("live 单独分组")).toBeVisible();
	// live 建议快照保留：¥10@1.30 → 实际 ¥12@1.20
	await expect(page.getByTestId("section-live").getByText("¥10.00→¥12.00")).toBeVisible();
	await expect(page.getByTestId("section-live").getByText("1.30→1.20")).toBeVisible();

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

	// 原子重算：纸面单关/串关由胜转负
	await expect(page.getByTestId("section-locked-paper").getByText("+550.00")).toHaveCount(0);
	await expect(page.getByTestId("section-locked-paper").getByText("负").first()).toBeVisible();
	// live 客胜腿经更正反败为胜: 实际条款 12×1.2=14.4, 冲正兑付后余额 +2.4
	await expect(page.getByTestId("section-live").getByText("+2.40")).toBeVisible();
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
