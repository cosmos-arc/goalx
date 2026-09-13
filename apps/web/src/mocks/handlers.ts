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
		had_quote: {
			as_of: "2026-09-12T12:05:00+00:00",
			status: "valid",
			reasons: [],
			sale_state: "on_sale",
			single_eligible: true,
			jc_source_updated_at: "2026-09-12T12:00:00+00:00",
			eu_books: 2,
		},
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
		had_quote: {
			as_of: "2026-09-12T12:05:00+00:00",
			status: "rejected",
			reasons: ["sale_stopped"],
			sale_state: "stopped",
			single_eligible: null,
			jc_source_updated_at: null,
			eu_books: 0,
		},
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
		had_quote: {
			as_of: "2026-09-12T12:05:00+00:00",
			status: "unknown",
			reasons: ["eu_no_quote"],
			sale_state: "on_sale",
			single_eligible: false,
			jc_source_updated_at: "2026-09-12T14:29:36+00:00",
			eu_books: 0,
		},
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
		actual_stake: null,
		strategy_version: "manual-v1",
		placed_at: null,
		locked_at: null,
		created_at: "2026-09-12T10:00:00+00:00",
		status: "open",
		payout: null,
		profit: null,
		settled_at: null,
		legs: [
			{ fixture_id: 1, market_code: "had", selection_code: "h", locked_odds: 6.5, actual_odds: null, goal_line: null },
		],
		review: { locked_pre_kickoff: null, closing_present: false, forward: "excluded_unlocked" },
	},
	{
		id: 2,
		slip_id: 1,
		mode: "paper",
		market_kind: "fixed",
		purchased: true,
		stake: 2,
		actual_stake: null,
		strategy_version: null,
		placed_at: "2026-09-12T19:00:00+00:00",
		locked_at: "2026-09-12T19:00:00+00:00",
		created_at: "2026-09-12T10:00:00+00:00",
		status: "won",
		payout: 28.6,
		profit: 26.6,
		settled_at: "2026-09-13T12:00:00+00:00",
		legs: [
			{ fixture_id: 1, market_code: "had", selection_code: "h", locked_odds: 6.5, actual_odds: null, goal_line: null },
			{ fixture_id: 2, market_code: "had", selection_code: "a", locked_odds: 2.2, actual_odds: null, goal_line: null },
		],
		review: { locked_pre_kickoff: true, closing_present: false, forward: "missing_closing" },
	},
	{
		id: 3,
		slip_id: null,
		mode: "live",
		market_kind: "fixed",
		purchased: false,
		stake: 50,
		actual_stake: null,
		strategy_version: null,
		placed_at: null,
		locked_at: null,
		created_at: "2026-09-12T11:00:00+00:00",
		status: "partial",
		payout: null,
		profit: null,
		settled_at: null,
		legs: [
			{ fixture_id: 2, market_code: "hhad", selection_code: "a", locked_odds: 1.9, actual_odds: null, goal_line: -0.5 },
		],
		review: { locked_pre_kickoff: null, closing_present: null, forward: "excluded_unlocked" },
	},
	{
		id: 4,
		slip_id: 2,
		mode: "live",
		market_kind: "fixed",
		purchased: true,
		stake: 10,
		actual_stake: 12,
		strategy_version: null,
		placed_at: "2026-09-12T18:00:00+00:00",
		locked_at: "2026-09-12T18:00:00+00:00",
		created_at: "2026-09-12T11:30:00+00:00",
		status: "won",
		payout: 22.8,
		profit: 10.8,
		settled_at: "2026-09-13T12:00:00+00:00",
		legs: [
			{ fixture_id: 1, market_code: "had", selection_code: "h", locked_odds: 2, actual_odds: 1.9, goal_line: null },
		],
		review: { locked_pre_kickoff: true, closing_present: false, forward: "live_separate" },
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

export const backtestRunsFixture = [
	{
		id: 7,
		label: "m2-smoke",
		status: "done",
		created_at: "2026-09-13T10:00:00+00:00",
		finished_at: "2026-09-13T10:05:00+00:00",
		summary: { predictions: 4200, bets: 96, staked: 4500, profit: -120, roi: -0.027, skipped: {} },
		overall_metrics: {
			n: 4200,
			rps_model: 0.2031,
			rps_market: 0.2029,
			skill_rps: 0.001,
			dm_p: 0.42,
			brier_model: 0.58,
			brier_market: 0.578,
		},
	},
] as const;

export const validationProgressFixture = {
	conditions: [
		{
			key: "clv_beat",
			label: "纸面 CLV beat ≥60% 且 ≥200 唯一注",
			achieved: false,
			current: "beat=58.0% @ 11 唯一注",
			target: "≥60% @ ≥200 唯一注",
		},
		{
			key: "market_skill",
			label: "前瞻对市场 skill ≥ 0 (RPS, ≥30 场)",
			achieved: false,
			current: "dc-demo: n=5 skill=+0.0120(样本不足)",
			target: "≥ 0",
		},
		{
			key: "review_errors",
			label: "复核无系统性错误",
			achieved: false,
			current: "无复核记录(未评估)",
			target: "无系统性错误",
		},
		{
			key: "full_season",
			label: "整赛季纸面样本覆盖",
			achieved: false,
			current: "11 唯一注 / 2026-09-01~2026-09-12(覆盖未验收)",
			target: "一个完整销售赛季的窗口覆盖",
		},
	],
	paper: { bets: 12, unique_bets: 11, legs: 14, fixtures: 10, staked: 600, profit: -12 },
	live: { bets: 0, unique_bets: 0, legs: 0, fixtures: 0, staked: 0, profit: 0 },
	unpurchased_open: 2,
	yield_curve: Array.from({ length: 12 }, (_, i) => ({
		index: i + 1,
		cumulative_yield: -0.02 + 0.004 * i,
		rolling_yield: -0.03 + 0.005 * i,
	})),
	yield_curve_mode: "paper",
	clv: {
		singles: {
			paper: { n_bets: 9, beat_rate: 0.56, avg_clv: 0.01 },
			live: { n_bets: 0, beat_rate: null, avg_clv: null },
		},
		parlay2: {
			paper: { n_bets: 2, beat_rate: 0.5, avg_clv: 0.02 },
			live: { n_bets: 0, beat_rate: null, avg_clv: null },
		},
		independence_assumed: true,
		denominator: {
			settled_bets: 12,
			legs: 14,
			no_close_bets: 1,
			unsupported_bets: 0,
			raw_bets: 12,
			reconciled_bets: 11,
			unique_bets: 11,
			deduped_duplicates: 0,
			fixtures: 10,
		},
		by_minutes_bucket_single: {},
		regression: { n: 9, slope: 3.2, r_squared: 0.4, note: "singles only" },
	},
	forward: {
		rule: "latest_forecast_before_kickoff_v1",
		coverage: { settled_fixtures: 8, no_forecast: 2, post_kickoff_only: 0, no_market_baseline: 1, scored: 5 },
		groups: { "dc-demo": { n: 5, skill_rps: 0.012, insufficient_samples: true } },
	},
	latest_run: backtestRunsFixture[0],
} as const;

handlers.push(
	http.get("*/api/v1/fixtures/1/odds", () => HttpResponse.json(oddsFixture)),
	http.get("*/api/v1/bet-slips", () => HttpResponse.json(slipsFixture)),
	http.get("*/api/v1/draw-results", () => HttpResponse.json(drawResultsFixture)),
	http.post("*/api/v1/bets", () => HttpResponse.json(betsFixture[1], { status: 201 })),
	http.get("*/api/v1/backtest/runs", () => HttpResponse.json(backtestRunsFixture)),
	http.get("*/api/v1/validation/progress", () => HttpResponse.json(validationProgressFixture)),
	http.get("*/api/v1/costs/summary", () =>
		HttpResponse.json({
			since: null,
			total_cny: 12.5,
			credits_used: 38,
			items: [
				{ category: "odds_api_credit", units: 38, amount_cny: 0, entries: 19 },
				{ category: "llm_api", units: 25, amount_cny: 12.5, entries: 3 },
			],
		}),
	),
	http.post("*/api/v1/draw-results/preview", () =>
		HttpResponse.json({
			results: [
				{
					fixture_id: 1,
					is_correction: true,
					previous: { fixture_id: 1, home_goals: 3, away_goals: 1, void: 0 },
					replacement: { fixture_id: 1, home_goals: 0, away_goals: 1, void: false },
				},
			],
			affected_bets: [
				{
					bet_id: 2,
					mode: "paper",
					purchased: true,
					status_current: "won",
					payout_current: 28.6,
					status_projected: "lost",
					payout_projected: 0,
					delta_payout: -28.6,
				},
			],
		}),
	),
);
