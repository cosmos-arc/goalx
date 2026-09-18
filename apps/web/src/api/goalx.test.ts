import { HttpResponse, http } from "msw";
import { expect, test } from "vitest";
import { server } from "../mocks/server";
import {
	createBet,
	fetchBankroll,
	fetchBets,
	fetchCostSummary,
	fetchDrawResults,
	fetchFixtureOdds,
	fetchGoalsMarket,
	fetchSlips,
	fetchTodayFixtures,
	importDrawResults,
	previewDrawResults,
	recordPurchase,
	runSettlement,
} from "./goalx";

test("fetchers round-trip against msw handlers", async () => {
	const today = await fetchTodayFixtures("2026-09-12");
	expect(today).toHaveLength(3);
	expect(today[0]?.jc_odds.h).toBe(1.92);
	expect(today[2]?.had_quote?.status).toBe("rejected");

	// 票 wb-05：进球玩法读模型（ttg/crs 网格 + 模型概率/EV）
	const goals = await fetchGoalsMarket();
	expect(goals).toHaveLength(3);
	expect(goals[1]?.model_version).toBe("dc-demo");
	expect(goals[1]?.ttg?.selections).toHaveLength(8);
	expect(goals[1]?.crs?.selections).toHaveLength(31);
	const s2 = goals[1]?.ttg?.selections?.find((sel) => sel.code === "2");
	expect(s2).toMatchObject({ odds: 4.5, probability: 0.245, ev: 0.1025 });
	expect(goals[0]?.model_version).toBeNull(); // 无模型场概率/EV 置空
	expect(goals[0]?.ttg?.selections?.[0]?.probability).toBeNull();
	expect(goals[2]?.ttg?.sale_state).toBe("stopped");

	const odds = await fetchFixtureOdds(1);
	expect(odds[0]?.source).toBe("sporttery");

	const bets = await fetchBets({ mode: "paper", only_open: false });
	expect(bets).toHaveLength(4);

	const allBets = await fetchBets();
	expect(allBets).toHaveLength(4);
	expect(allBets[3]?.actual_stake).toBe(12);
	expect(allBets[3]?.review?.forward).toBe("live_separate");

	const created = await createBet({
		mode: "paper",
		stake: 2,
		legs: [{ fixture_id: 1, market_code: "had", selection_code: "h", locked_odds: 6.5 }],
	});
	expect(created.status).toBe("won");

	const slips = await fetchSlips();
	expect(slips[0]?.bet_count).toBe(1);

	const slip = await recordPurchase({ bet_ids: [1] });
	expect(slip.stake_total).toBe(100);

	const results = await fetchDrawResults();
	expect(results[0]?.home_goals).toBe(3);

	const filtered = await fetchDrawResults(1);
	expect(filtered).toHaveLength(1);

	const preview = await previewDrawResults({
		source: "manual",
		results: [{ fixture_id: 1, home_goals: 0, away_goals: 1, void: false }],
	});
	expect(preview.results[0]?.is_correction).toBe(true);
	expect(preview.affected_bets[0]?.delta_payout).toBe(-28.6);

	const imported = await importDrawResults({
		source: "manual",
		results: [{ fixture_id: 1, home_goals: 2, away_goals: 0, void: false }],
	});
	expect(imported.imported).toBe(1);

	const stats = await runSettlement();
	expect(stats.settled).toBe(1);

	const bankroll = await fetchBankroll();
	expect(bankroll.balance).toBe(5004.2);

	const costs = await fetchCostSummary();
	expect(costs.credits_used).toBe(38);
	expect(costs.total_cny).toBe(12.5);
});

test("fetchers surface HTTP errors", async () => {
	server.use(http.get("*/api/v1/fixtures/today", () => HttpResponse.json({ detail: "boom" }, { status: 500 })));
	await expect(fetchTodayFixtures()).rejects.toMatchObject({ detail: "boom" });
});
