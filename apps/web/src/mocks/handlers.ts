import { HttpResponse, http } from "msw";

export const statusFixture = {
	app_name: "goalx-backend",
	app_version: "0.1.0",
	environment: "testing",
} as const;

export const todayFixture = [
	{
		fixture_id: 1,
		match_code: "周六001",
		competition: "英超",
		tier: "tier1",
		home_team: "阿森纳",
		away_team: "切尔西",
		kickoff_utc: "2026-09-13T02:00:00+00:00",
		is_single: true,
		joined: true,
		jc_odds: { h: 6.5, d: 5.0, a: 1.3 },
		jc_updated_at: "2026-09-12T12:00:00+00:00",
		books: 2,
		eu_prob: { h: 0.16, d: 0.2, a: 0.64 },
		ev: { h: 0.04, d: 0.0, a: -0.17 },
		flags: ["few_books", "custom_flag"],
	},
	{
		fixture_id: 3,
		match_code: "周六003",
		competition: "德乙",
		tier: "tier2",
		home_team: "A 队",
		away_team: "B 队",
		kickoff_utc: "2026-09-13T20:00:00+00:00",
		is_single: false,
		joined: true,
		jc_odds: { h: null, d: null, a: null },
		jc_updated_at: null,
		books: 4,
		eu_prob: null,
		ev: null,
		flags: [],
	},
	{
		fixture_id: 2,
		match_code: "周六002",
		competition: "荷甲",
		tier: "tier2",
		home_team: "福图纳锡塔德",
		away_team: "阿贾克斯",
		kickoff_utc: "2026-09-12T18:00:00+00:00",
		is_single: false,
		joined: false,
		jc_odds: { h: 6.6, d: 5.1, a: 1.28 },
		jc_updated_at: "2026-09-12T14:29:36+00:00",
		books: 0,
		eu_prob: null,
		ev: null,
		flags: ["not_joined"],
	},
] as const;

export const betsFixture = [
	{
		id: 1,
		slip_id: null,
		mode: "paper",
		market_kind: "fixed",
		purchased: false,
		stake: 100,
		placed_at: null,
		created_at: "2026-09-12T10:00:00+00:00",
		status: "open",
		payout: null,
		profit: null,
		settled_at: null,
		legs: [{ fixture_id: 1, market_code: "had", selection_code: "h", locked_odds: 6.5, goal_line: null }],
	},
	{
		id: 2,
		slip_id: 1,
		mode: "paper",
		market_kind: "fixed",
		purchased: true,
		stake: 2,
		placed_at: "2026-09-12T19:00:00+00:00",
		created_at: "2026-09-12T10:00:00+00:00",
		status: "won",
		payout: 28.6,
		profit: 26.6,
		settled_at: "2026-09-13T12:00:00+00:00",
		legs: [
			{ fixture_id: 1, market_code: "had", selection_code: "h", locked_odds: 6.5, goal_line: null },
			{ fixture_id: 2, market_code: "had", selection_code: "a", locked_odds: 2.2, goal_line: null },
		],
	},
	{
		id: 3,
		slip_id: null,
		mode: "live",
		market_kind: "fixed",
		purchased: false,
		stake: 50,
		placed_at: null,
		created_at: "2026-09-12T11:00:00+00:00",
		status: "partial",
		payout: null,
		profit: null,
		settled_at: null,
		legs: [{ fixture_id: 2, market_code: "hhad", selection_code: "a", locked_odds: 1.9, goal_line: -0.5 }],
	},
] as const;

export const bankrollFixture = {
	balance: 5004.2,
	events: [
		{
			id: 2,
			occurred_at: "2026-09-13T12:00:00+00:00",
			kind: "bet_payout",
			amount_cny: 4.2,
			balance_after: 5004.2,
			bet_id: 7,
			note: "settlement",
		},
		{
			id: 1,
			occurred_at: "2026-09-01T00:00:00+00:00",
			kind: "deposit",
			amount_cny: 5000,
			balance_after: 5000,
			bet_id: null,
			note: "初始资金",
		},
	],
} as const;

export const handlers = [
	http.get("*/api/v1/status", () => HttpResponse.json(statusFixture)),
	http.get("*/api/v1/fixtures/today", () => HttpResponse.json(todayFixture)),
	http.get("*/api/v1/bets", () => HttpResponse.json(betsFixture)),
	http.post("*/api/v1/bet-slips", () =>
		HttpResponse.json(
			{
				id: 1,
				mode: "paper",
				placed_at: "2026-09-12T19:00:00+00:00",
				note: null,
				created_at: "2026-09-12T19:00:00+00:00",
				bet_count: 1,
				stake_total: 100,
				profit_total: 0,
			},
			{ status: 201 },
		),
	),
	http.post("*/api/v1/settlements/run", () =>
		HttpResponse.json({ settled: 1, still_open: 0, won: 1, lost: 0, void: 0 }),
	),
	http.post("*/api/v1/draw-results", () => HttpResponse.json({ imported: 1 }, { status: 201 })),
	http.get("*/api/v1/bankroll", () => HttpResponse.json(bankrollFixture)),
];

export const oddsFixture = [
	{
		id: 1,
		fixture_id: 1,
		market_code: "had",
		selection_code: "h",
		source: "sporttery",
		odds: 6.6,
		captured_at: "2026-09-12T14:29:36+00:00",
	},
] as const;

export const slipsFixture = [
	{
		id: 1,
		mode: "paper",
		placed_at: "2026-09-12T19:00:00+00:00",
		note: null,
		created_at: "2026-09-12T19:00:00+00:00",
		bet_count: 1,
		stake_total: 100,
		profit_total: 0,
	},
] as const;

export const drawResultsFixture = [
	{
		fixture_id: 1,
		home_goals: 3,
		away_goals: 1,
		half_home_goals: null,
		half_away_goals: null,
		void: false,
		void_reason: null,
		source: "manual",
		published_at: null,
	},
] as const;

handlers.push(
	http.get("*/api/v1/fixtures/1/odds", () => HttpResponse.json(oddsFixture)),
	http.get("*/api/v1/bet-slips", () => HttpResponse.json(slipsFixture)),
	http.get("*/api/v1/draw-results", () => HttpResponse.json(drawResultsFixture)),
	http.post("*/api/v1/bets", () => HttpResponse.json(betsFixture[1], { status: 201 })),
);
