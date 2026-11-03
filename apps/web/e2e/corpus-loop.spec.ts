/**
 * corpus 主链 e2e（boundary-hardening 票 06 验收）：
 *
 * 在「源T 页→bronze→silver 物化→corpus.duckdb→sid 映射→欧共识→报价
 * 资格→纸面锁定」的真主链夹具上（四场制式：主链/改期/缺源/两代源），
 * 断言今日页与纸面锁定读的是 corpus 面而非 legacy 降级路径。
 *
 * 主链正例 S1 在运行面库中**没有任何 legacy 欧赔行**——它的欧共识只能
 * 来自 corpus 物化；这是「非降级」的证明结构（unit 侧已钉死 books 计数）。
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";

type FixtureMeta = {
	code: string;
	fixture_id: number;
	sid: string | null;
	kickoff_utc: string;
	scenario: "mainline" | "rescheduled" | "no_source" | "legacy_eu";
};

const manifest = JSON.parse(
	readFileSync(path.join(import.meta.dirname, "../test-results/e2e-corpus-manifest.json"), "utf8"),
) as { fixtures: FixtureMeta[]; jc_had: Record<string, number> };

const byScenario = (scenario: FixtureMeta["scenario"]) =>
	manifest.fixtures.find((f) => f.scenario === scenario) as FixtureMeta;

test.describe.configure({ mode: "serial" });

test("今日页按 sid 链接分叉：corpus 三书/改期单书/缺源空面/legacy 双书", async ({ request }) => {
	const res = await request.get("/api/v1/fixtures/today?days=3");
	expect(res.status()).toBe(200);
	const rows = (await res.json()) as Array<{
		match_code: string;
		books: number;
		eu_prob: { h: number } | null;
		jc_odds: { h: number | null; d: number | null; a: number | null };
	}>;
	const books = new Map(rows.map((r) => [r.match_code, r]));

	const mainline = books.get(byScenario("mainline").code);
	expect(mainline?.books).toBe(3); // corpus 物化三书（sharp/lottery/晚到）
	expect(mainline?.eu_prob?.h).toBeGreaterThan(0);

	const moved = books.get(byScenario("rescheduled").code);
	expect(moved?.books).toBe(1); // 改期后 sid 链接仍挂共识
	expect(moved?.eu_prob?.h).toBeGreaterThan(0);

	expect(books.get(byScenario("no_source").code)?.books).toBe(0); // 缺源=诚实空面
	expect(books.get(byScenario("legacy_eu").code)?.books).toBe(2); // legacy 读径共存

	// JC 价绑定面：四场都有 sporttery 三向（今日页 jc_odds）
	for (const f of manifest.fixtures) {
		const jc = books.get(f.code)?.jc_odds;
		expect(jc?.["h"]).toBeCloseTo(manifest.jc_had["h"] ?? 2.05, 2);
	}
});

test("场次页与研究页：corpus 行渲染逐书三行与共识", async ({ page }) => {
	await page.goto("/fixtures");
	// 场次页默认单业务日:今日三场(S1/S3/S4);改期场 S2 在明日,直连研究页覆盖
	await expect(page.getByTestId("fixtures-row")).toHaveCount(3);

	const mainline = byScenario("mainline");
	await page.getByTestId(`fixtures-link-${mainline.fixture_id}`).click();
	await expect(page.getByRole("heading", { name: "GoalX · 场次研究" })).toBeVisible();
	// corpus 三书逐书行（运行面库无任何 legacy 行——只能来自 corpus）
	await expect(page.getByTestId("research-book-row")).toHaveCount(3);
	await expect(page.getByTestId("research-consensus")).toContainText("家三向均价");
});

test("纸面锁定主链：价变 409 确认流 → 按服务器报价落库", async ({ request }) => {
	const mainline = byScenario("mainline");
	const jcH = manifest.jc_had["h"] ?? 2.05;

	// 先以过期价提交 → 409 返回服务器当前价（票 04 裁决①）
	const stale = await request.post("/api/v1/bets", {
		data: {
			mode: "paper",
			stake: 2,
			legs: [
				{
					fixture_id: mainline.fixture_id,
					market_code: "had",
					selection_code: "h",
					locked_odds: 1.5,
				},
			],
		},
	});
	expect(stale.status()).toBe(409);
	const conflict = ((await stale.json()) as { detail: unknown }).detail as {
		current_quotes: Array<{ server_odds: number; fixture_id: number }>;
	};
	const serverOdds = conflict.current_quotes.find((q) => q.fixture_id === mainline.fixture_id)?.server_odds;
	expect(serverOdds).toBeCloseTo(jcH, 2);

	// 按服务器报价重提 → 建议成立（票 04:价绑服务器报价+system_paper 分类）
	const created = await request.post("/api/v1/bets", {
		data: {
			mode: "paper",
			stake: 2,
			legs: [
				{
					fixture_id: mainline.fixture_id,
					market_code: "had",
					selection_code: "h",
					locked_odds: serverOdds ?? jcH,
				},
			],
		},
	});
	expect(created.status()).toBe(201);
	const bet = (await created.json()) as { id: number; price_source: string };

	// 锁定（纸面票回录）→ 落库 locked_odds=服务器报价 + 快照身份
	const locked = await request.post("/api/v1/bet-slips", { data: { bet_ids: [bet.id] } });
	expect(locked.status()).toBe(201);
	const bets = (await (await request.get("/api/v1/bets")).json()) as Array<{
		id: number;
		price_source: string;
		legs: Array<{ locked_odds: number; snapshot_id: number | null }>;
	}>;
	const stored = bets.find((b) => b.id === bet.id);
	expect(stored?.price_source).toBe("system_paper");
	expect(stored?.legs[0]?.snapshot_id).not.toBeNull();
	expect(stored?.legs[0]?.locked_odds).toBeCloseTo(jcH, 2);
});

test("改期与两代源研究页：单书 corpus / legacy 双书", async ({ page }) => {
	// 改期场在明日业务日(今日列表不列),直连研究页——sid 链接仍挂 corpus 单书
	const moved = byScenario("rescheduled");
	await page.goto(`/fixtures/${moved.fixture_id}`);
	await expect(page.getByTestId("research-book-row")).toHaveCount(1);

	// 两代源:无 sid 链接 → legacy odds_snapshots 读径,双书逐行
	const legacy = byScenario("legacy_eu");
	await page.goto(`/fixtures/${legacy.fixture_id}`);
	await expect(page.getByTestId("research-book-row")).toHaveCount(2);
});
