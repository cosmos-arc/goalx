import { HttpResponse, http } from "msw";
import { expect, test } from "vitest";
import { server } from "../mocks/server";
import {
	createBet,
	fetchBankroll,
	fetchBets,
	fetchDrawResults,
	fetchFixtureOdds,
	fetchSlips,
	fetchTodayFixtures,
	importDrawResults,
	recordPurchase,
	runSettlement,
} from "./goalx";

test("fetchers round-trip against msw handlers", async () => {
	const today = await fetchTodayFixtures("2026-09-12");
	expect(today).toHaveLength(3);
	expect(today[0]?.jc_odds.h).toBe(6.5);

	const odds = await fetchFixtureOdds(1);
	expect(odds[0]?.source).toBe("sporttery");

	const bets = await fetchBets({ mode: "paper", only_open: false });
	expect(bets).toHaveLength(3);

	const allBets = await fetchBets();
	expect(allBets).toHaveLength(3);

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

	const imported = await importDrawResults({
		source: "manual",
		results: [{ fixture_id: 1, home_goals: 2, away_goals: 0, void: false }],
	});
	expect(imported.imported).toBe(1);

	const stats = await runSettlement();
	expect(stats.settled).toBe(1);

	const bankroll = await fetchBankroll();
	expect(bankroll.balance).toBe(5004.2);
});

test("fetchers surface HTTP errors", async () => {
	server.use(http.get("*/api/v1/fixtures/today", () => HttpResponse.json({ detail: "boom" }, { status: 500 })));
	await expect(fetchTodayFixtures()).rejects.toMatchObject({ detail: "boom" });
});
