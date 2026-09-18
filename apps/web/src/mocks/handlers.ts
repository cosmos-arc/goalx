import { HttpResponse, http } from "msw";

export const statusFixture = {
	app_name: "goalx-backend",
	app_version: "0.1.0",
	environment: "testing",
} as const;

function hoursFromNow(hours: number): string {
	return new Date(Date.now() + hours * 3_600_000).toISOString();
}

function minutesAgoIso(minutes: number): string {
	return new Date(Date.now() - minutes * 60_000).toISOString();
}

function daysAgoIso(days: number): string {
	return new Date(Date.now() - days * 86_400_000).toISOString();
}

/** 北京时区业务日（与后端 beijing_business_date 同口径）。 */
function beijingBusinessDate(now: number): string {
	return new Date(now + 8 * 3_600_000).toISOString().slice(0, 10);
}

/**
 * 票 14 今日页 mock：时间相对 now 动态生成（原型 today-proto-data.ts 的场景思路），
 * 三场覆盖 可投+单固+偏差/样本少、可投+仅串关+过期报价、停售拒绝。
 * 票 wb-01：行内带 business_date；加一场"明天"场次（fixture 4，可投）驱动日期 Tab。
 * 票 39：books 分母三例——fixture 1 = 2（few_books+low_confidence 并打，验证合并
 * 显示位）、fixture 2 = 3（仅 low_confidence）、fixture 4 = 5（干净，不命中护栏）。
 */
export const todayFixture = [
	{
		fixture_id: 1,
		match_code: "周六001",
		competition: "英超",
		tier: "tier1",
		home_team: "阿森纳",
		away_team: "切尔西",
		business_date: beijingBusinessDate(Date.now()),
		kickoff_utc: hoursFromNow(2.5),
		is_single: true,
		joined: true,
		jc_odds: { h: 1.92, d: 3.55, a: 3.7 },
		jc_updated_at: minutesAgoIso(8),
		books: 2,
		eu_prob: { h: 0.529, d: 0.248, a: 0.223 },
		ev: { h: 0.053, d: -0.12, a: -0.175 },
		flags: ["ev_deviation", "few_books", "low_confidence", "custom_flag"],
		had_quote: {
			as_of: minutesAgoIso(1),
			status: "valid",
			reasons: [],
			sale_state: "on_sale",
			single_eligible: true,
			jc_source_updated_at: minutesAgoIso(8),
			eu_books: 2,
		},
	},
	{
		fixture_id: 2,
		match_code: "周六002",
		competition: "英超",
		tier: "tier1",
		home_team: "利物浦",
		away_team: "曼城",
		business_date: beijingBusinessDate(Date.now()),
		kickoff_utc: hoursFromNow(1.5),
		is_single: false,
		joined: true,
		jc_odds: { h: 3.0, d: 3.4, a: 2.2 },
		jc_updated_at: minutesAgoIso(45),
		books: 3,
		eu_prob: { h: 0.32, d: 0.29, a: 0.39 },
		ev: { h: -0.01, d: -0.135, a: -0.171 },
		flags: ["low_confidence"],
		had_quote: {
			as_of: minutesAgoIso(1),
			status: "valid",
			reasons: [],
			sale_state: "on_sale",
			single_eligible: false,
			jc_source_updated_at: minutesAgoIso(45),
			eu_books: 3,
		},
	},
	{
		fixture_id: 3,
		match_code: "周六003",
		competition: "德乙",
		tier: "tier2",
		home_team: "A 队",
		away_team: "B 队",
		business_date: beijingBusinessDate(Date.now()),
		kickoff_utc: hoursFromNow(5),
		is_single: true,
		joined: true,
		jc_odds: { h: 2.1, d: 3.25, a: 3.15 },
		jc_updated_at: null,
		books: 0,
		eu_prob: null,
		ev: null,
		flags: [],
		had_quote: {
			as_of: minutesAgoIso(1),
			status: "rejected",
			reasons: ["sale_stopped"],
			sale_state: "stopped",
			single_eligible: true,
			jc_source_updated_at: null,
			eu_books: 0,
		},
	},
	{
		fixture_id: 4,
		match_code: "周日004",
		competition: "西甲",
		tier: "tier2",
		home_team: "C 队",
		away_team: "D 队",
		business_date: beijingBusinessDate(Date.now() + 86_400_000),
		kickoff_utc: hoursFromNow(26),
		is_single: true,
		joined: true,
		jc_odds: { h: 2.4, d: 3.3, a: 2.8 },
		jc_updated_at: minutesAgoIso(20),
		books: 5,
		eu_prob: { h: 0.4, d: 0.27, a: 0.33 },
		ev: { h: -0.04, d: -0.109, a: -0.076 },
		flags: [],
		had_quote: {
			as_of: minutesAgoIso(1),
			status: "valid",
			reasons: [],
			sale_state: "on_sale",
			single_eligible: true,
			jc_source_updated_at: minutesAgoIso(20),
			eu_books: 5,
		},
	},
] as const;

/**
 * 票 38 poolList 口径样例：比赛级 bettingSingle=0（is_single=false）但
 * poolList HAD 池 single=1 → 修正后 had_quote.single_eligible=true。
 * 复刻主库误记场次（2026-09-17 周四006 皇家社会 vs 伯恩茅斯）的修正后
 * 形状——徽章不应显示"仅串关"、组合引擎应纳入单关推荐。默认 handler 不
 * 返回它（不扰动既有计数断言），消费方用 server.use 覆盖注入。
 */
export const poolListTodayFixture = {
	fixture_id: 6,
	match_code: "周四006",
	competition: "西甲",
	tier: "tier1",
	home_team: "皇家社会",
	away_team: "伯恩茅斯",
	business_date: beijingBusinessDate(Date.now()),
	kickoff_utc: hoursFromNow(3.5),
	is_single: false, // 比赛级关单关（bettingSingle=0）
	joined: true,
	jc_odds: { h: 2.05, d: 3.4, a: 3.6 },
	jc_updated_at: minutesAgoIso(12),
	books: 6,
	eu_prob: { h: 0.51, d: 0.25, a: 0.24 },
	ev: { h: 0.046, d: -0.15, a: -0.136 },
	flags: [],
	had_quote: {
		as_of: minutesAgoIso(1),
		status: "valid",
		reasons: [],
		sale_state: "on_sale",
		single_eligible: true, // poolList 池级单固（票 38 修正口径）
		jc_source_updated_at: minutesAgoIso(12),
		eu_books: 6,
	},
} as const;

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

/**
 * 票 20：事件时间相对 now 动态生成（今日页同思路）——余额迷你曲线的"近 30 天"
 * 窗口过滤不因测试日期推移而漂移；金额与余额断言不受影响。
 */
export const bankrollFixture = {
	balance: 5004.2,
	events: [
		{
			id: 2,
			occurred_at: daysAgoIso(2),
			kind: "bet_payout",
			amount_cny: 4.2,
			balance_after: 5004.2,
			bet_id: 7,
			note: "settlement",
		},
		{
			id: 1,
			occurred_at: daysAgoIso(14),
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
	// 票 wb-01：days>1 返回多日窗口（含"明天"的 fixture 4）；无 days = 单日（3 场），
	// 与真实 API 同口径——单日消费方（总览/投注/历史）不看到跨日数据
	http.get("*/api/v1/fixtures/today", ({ request }) => {
		const days = Number(new URL(request.url).searchParams.get("days") ?? 1);
		const today = beijingBusinessDate(Date.now());
		const rows = days > 1 ? todayFixture : todayFixture.filter((row) => row.business_date === today);
		return HttpResponse.json(rows);
	}),
	// 票 wb-06：建议仓位端点 mock——与后端 betting/staking.py 同规则的最小镜像
	// （EV≤0→¥0；paper flat 2% 截断 1–5% 下限 ¥2；live ¼Kelly 截断 1–cap%；
	//   特殊场景用 server.use 覆盖）。bankroll 恒用请求值（mock 无账本）。
	http.post("*/api/v1/stake-advice", async ({ request }) => {
		const body = (await request.json()) as {
			mode?: string;
			bankroll?: number;
			ev?: number;
			odds?: number;
			cap_fraction?: number;
		};
		const mode = body.mode === "live" ? "live" : "paper";
		const bankroll = typeof body.bankroll === "number" ? body.bankroll : 0;
		const ev = typeof body.ev === "number" ? body.ev : 0;
		const odds = typeof body.odds === "number" ? body.odds : 2;
		const cap = typeof body.cap_fraction === "number" ? body.cap_fraction : 0.05;
		if (ev <= 0) {
			return HttpResponse.json({
				stake: 0,
				tier: "ev_non_positive",
				fraction: null,
				full_kelly_fraction: null,
				capped: false,
				reason: `EV ${(ev * 100).toFixed(1)}% ≤ 0——建议不投（¥0）。EV 是诊断量非机会信号：无正期望时不给注额，防止“看系统有推荐再自己加码”。`,
			});
		}
		if (mode === "paper") {
			const stake =
				bankroll <= 0
					? 2
					: Math.round(Math.max(Math.min(Math.max(bankroll * 0.02, bankroll * 0.01), bankroll * 0.05), 2) * 100) / 100;
			return HttpResponse.json({
				stake,
				tier: "flat",
				fraction: bankroll > 0 ? Math.round((stake / bankroll) * 10000) / 10000 : null,
				full_kelly_fraction: null,
				capped: false,
				reason:
					bankroll <= 0
						? "纸面期一律 flat（红线）：资金池未入金——暂按竞彩最低注 ¥2 建议，入金后按 bankroll 1–5% 校准"
						: `纸面期一律 flat（红线）：bankroll × 2.0% 截断到 1–5% 区间 → ¥${stake.toFixed(2)}——比例策略（Kelly）在前瞻 skill 过线前不启用，保证验证指标无偏`,
			});
		}
		if (bankroll <= 0) {
			return HttpResponse.json({
				stake: 0,
				tier: "unfunded",
				fraction: null,
				full_kelly_fraction: null,
				capped: false,
				reason:
					"真金建议 = ¼ fractional Kelly（f* = EV/(odds−1) 取 1/4，单注 1–5% 截断），但资金池未入金——暂无注额建议（入金后按比例校准）",
			});
		}
		const full = ev / (odds - 1);
		const quarter = full * 0.25;
		const fraction = Math.min(Math.max(quarter, 0.01), cap);
		const stake = Math.round(Math.max(bankroll * fraction, 2) * 100) / 100;
		return HttpResponse.json({
			stake,
			tier: "quarter_kelly",
			fraction: Math.round((stake / bankroll) * 10000) / 10000,
			full_kelly_fraction: Math.round(full * 10000) / 10000,
			capped: quarter > cap,
			reason: `真金 ¼ fractional Kelly：f* = EV/(odds−1) = ${(full * 100).toFixed(1)}%，取 1/4 = ${(quarter * 100).toFixed(1)}%${
				quarter > cap ? `，已按单注上限 ${(cap * 100).toFixed(0)}% 截断` : ""
			} → ¥${stake.toFixed(2)}；单注硬区间 1%–${(cap * 100).toFixed(0)}%（竞彩最低 ¥2）`,
		});
	}),
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
	// 票 20：入金端点 mock（无状态：按当前 fixture 余额 + 载荷金额回 201；
	// 需要"提交后刷新"闭环的测试在用例内用 server.use 覆盖为有状态版本）
	http.post("*/api/v1/bankroll/deposits", async ({ request }) => {
		const body = (await request.json()) as { amount_cny?: number; note?: string | null };
		const amount = typeof body.amount_cny === "number" ? body.amount_cny : 0;
		if (!(amount > 0)) {
			return HttpResponse.json({ detail: "入金金额必须大于 0" }, { status: 400 });
		}
		const balanceAfter = bankrollFixture.balance + amount;
		return HttpResponse.json(
			{
				event: {
					id: 99,
					occurred_at: new Date().toISOString(),
					kind: "deposit",
					amount_cny: amount,
					balance_after: balanceAfter,
					bet_id: null,
					note: body.note ?? null,
				},
				balance: balanceAfter,
			},
			{ status: 201 },
		);
	}),
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

/** 票 42：赛果同步状态 mock——上次同步带待人工清单（not_finished/stored_differs）。 */
export const drawSyncFixture = {
	last_run: {
		source: "500.com",
		observed_at: "2026-09-18T05:30:00+00:00",
		business_dates: ["2026-09-16"],
		pages: 1,
		fetched: 16,
		imported: 12,
		unchanged: 3,
		unmatched: 0,
		pending_manual: [
			{ business_date: "2026-09-16", code: "周三014", reason: "not_finished" },
			{ business_date: "2026-09-16", code: "周三009", reason: "stored_differs" },
		],
	},
	pending_results: 2,
} as const;

/**
 * 票 wb-02 研究页 mock：fixture 1 的逐书赔率（两家全三向 + 一家缺一向）、
 * 去水共识、模型概率/EV；pinnacle 主胜 1.60（隐含 62.5%）相对共识 53% 偏高
 * ≥5pp → 琥珀高亮的可断言场景。时间相对 now。
 * 票 39：完整三向 book 实为 3 家（avg/bet365/pinnacle，late_missing 缺 d）→
 * 共识分母 3 <4，low_confidence 为 true（研究页低置信标注的可断言场景）。
 */
export const researchFixture = {
	fixture_id: 1,
	match_code: "周六001",
	business_date: beijingBusinessDate(Date.now()),
	competition: "英超",
	tier: "tier1",
	home_team: "阿森纳",
	away_team: "切尔西",
	kickoff_utc: hoursFromNow(2.5),
	is_single: true,
	joined: true,
	jc_odds: { h: 1.92, d: 3.55, a: 3.7 },
	jc_updated_at: minutesAgoIso(8),
	books: [
		{
			book: "odds_api:avg",
			odds: { h: 1.9, d: 3.6, a: 3.8 },
			captured_at: minutesAgoIso(4),
		},
		{
			book: "odds_api:bet365",
			odds: { h: 1.88, d: 3.7, a: 3.9 },
			captured_at: minutesAgoIso(4),
		},
		{
			book: "odds_api:pinnacle",
			odds: { h: 1.6, d: 3.5, a: 4.2 },
			captured_at: minutesAgoIso(4),
		},
		{
			book: "odds_api:late_missing",
			odds: { h: 1.95, d: null, a: 3.75 },
			captured_at: minutesAgoIso(4),
		},
	],
	consensus: { books: 3, low_confidence: true, probability: { h: 0.53, d: 0.25, a: 0.22 } },
	model: {
		model_version: "dc-demo",
		issued_at: minutesAgoIso(30),
		probability: { h: 0.58, d: 0.24, a: 0.18 },
		ev: { h: 0.1136, d: -0.146, a: -0.334 },
	},
	had_quote: {
		as_of: minutesAgoIso(1),
		status: "valid",
		reasons: [],
		sale_state: "on_sale",
		single_eligible: true,
		jc_source_updated_at: minutesAgoIso(8),
		eu_books: 3,
	},
} as const;

/**
 * 票 wb-05 进球玩法页 mock：与后端 demo 种子同思路——fixture 2 带模型
 * （λ 1.4/1.3 矩阵口径，ttg s2 价 4.50 → 模型 EV≈+10.3% 驱动组合非空），
 * fixture 1 无模型（概率/EV 空缺的诚实多数态），fixture 3 停售（禁用路径）。
 * ttg/crs 概率由 λ 的独立 Poisson 乘积推导（与矩阵同口径的 mock 近似）。
 */
const GOALS_TTG_PROBS = [0.0672, 0.1815, 0.245, 0.2205, 0.1488, 0.0804, 0.0362, 0.0206];
const GOALS_TTG_ODDS = [11.0, 4.1, 4.5, 3.4, 5.0, 9.0, 20.0, 35.0];
const CRS_CODES: string[] = [
	"0:0",
	"0:1",
	"0:2",
	"0:3",
	"0:4",
	"0:5",
	"1:0",
	"1:1",
	"1:2",
	"1:3",
	"1:4",
	"1:5",
	"2:0",
	"2:1",
	"2:2",
	"2:3",
	"2:4",
	"2:5",
	"3:0",
	"3:1",
	"3:2",
	"3:3",
	"4:0",
	"4:1",
	"4:2",
	"5:0",
	"5:1",
	"5:2",
	"h_other",
	"d_other",
	"a_other",
];

function pois(k: number, lam: number): number {
	let f = 1;
	for (let i = 2; i <= k; i += 1) f *= i;
	return (Math.exp(-lam) * lam ** k) / f;
}

function crsProb(code: string): number {
	if (code === "h_other" || code === "d_other" || code === "a_other") return 0.006;
	const [h, a] = code.split(":").map(Number) as [number, number];
	return pois(h, 1.4) * pois(a, 1.3);
}

function round4(value: number): number {
	return Math.round(value * 10000) / 10000;
}

function goalsTtgBlock(model: boolean, odds: number[], sale: "on_sale" | "stopped", single: boolean | null) {
	return {
		selections: GOALS_TTG_PROBS.map((probability, i) => ({
			code: String(i),
			odds: odds[i] ?? null,
			probability: model ? round4(probability) : null,
			ev: model && odds[i] ? round4(probability * odds[i] - 1) : null,
		})),
		single_eligible: single,
		sale_state: sale,
		updated_at: minutesAgoIso(8),
	};
}

function goalsCrsBlock(model: boolean, sale: "on_sale" | "stopped", single: boolean | null) {
	return {
		selections: CRS_CODES.map((code) => {
			const odds = code.endsWith("_other") ? 90 : Math.round((0.75 / crsProb(code)) * 100) / 100;
			const probability = model ? round4(crsProb(code)) : null;
			return {
				code,
				odds,
				probability,
				ev: model ? round4(crsProb(code) * odds - 1) : null,
			};
		}),
		single_eligible: single,
		sale_state: sale,
		updated_at: minutesAgoIso(8),
	};
}

export const goalsMarketFixture = [
	{
		fixture_id: 1,
		match_code: "周六001",
		business_date: beijingBusinessDate(Date.now()),
		competition: "英超",
		tier: "tier1",
		home_team: "阿森纳",
		away_team: "切尔西",
		kickoff_utc: hoursFromNow(2.5),
		ttg: goalsTtgBlock(false, [10.5, 3.9, 3.05, 3.5, 5.2, 9.8, 17.5, 34.0], "on_sale", true),
		crs: goalsCrsBlock(false, "on_sale", true),
		model_version: null,
		issued_at: null,
	},
	{
		fixture_id: 2,
		match_code: "周六002",
		business_date: beijingBusinessDate(Date.now()),
		competition: "英超",
		tier: "tier1",
		home_team: "利物浦",
		away_team: "曼城",
		kickoff_utc: hoursFromNow(3),
		ttg: goalsTtgBlock(true, GOALS_TTG_ODDS, "on_sale", true),
		crs: goalsCrsBlock(true, "on_sale", true),
		model_version: "dc-demo",
		issued_at: minutesAgoIso(30),
	},
	{
		fixture_id: 3,
		match_code: "周六003",
		business_date: beijingBusinessDate(Date.now()),
		competition: "德乙",
		tier: "tier2",
		home_team: "A 队",
		away_team: "B 队",
		kickoff_utc: hoursFromNow(5),
		ttg: goalsTtgBlock(false, [10.0, 3.7, 3.0, 3.5, 5.3, 10.0, 18.0, 33.0], "stopped", null),
		crs: goalsCrsBlock(false, "stopped", null),
		model_version: null,
		issued_at: null,
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
		close_basis_note:
			"pinnacle 主锚 → betfair_ex 辅(back 价扣佣金, 默认 2%) → consensus fallback；legacy = 分层前共识口径行(历史不重算)；mixed = 串关跨基准",
		by_close_basis: {
			pinnacle: {
				legs: 6,
				bets: 5,
				groups: { single: { paper: { n_bets: 4, beat_rate: 0.75, avg_clv: 0.012 } } },
			},
			consensus: {
				legs: 2,
				bets: 1,
				groups: { single: { paper: { n_bets: 1, beat_rate: 0, avg_clv: -0.004 } } },
			},
			legacy: {
				legs: 6,
				bets: 5,
				groups: {
					single: { paper: { n_bets: 4, beat_rate: 0.5, avg_clv: 0.008 } },
					parlay2: { paper: { n_bets: 1, beat_rate: 0, avg_clv: -0.01 } },
				},
			},
			mixed: { bets: 1, note: "串关两腿基准不同，腿级见 clv_records.close_basis" },
		},
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
	// 票 wb-05：进球玩法读模型（与今日页同口径——单日默认、days>1 放行全窗）
	http.get("*/api/v1/markets/goals", () => HttpResponse.json(goalsMarketFixture)),
	// 票 wb-02：研究页读模型（fixture 1 有逐书/共识/模型；其他 id 404）
	http.get("*/api/v1/fixtures/:id/research", ({ params }) => {
		if (Number(params["id"]) !== 1) {
			return HttpResponse.json({ detail: "fixture not found" }, { status: 404 });
		}
		return HttpResponse.json(researchFixture);
	}),
	http.get("*/api/v1/bet-slips", () => HttpResponse.json(slipsFixture)),
	http.get("*/api/v1/draw-results", () => HttpResponse.json(drawResultsFixture)),
	http.get("*/api/v1/draw-sync/status", () => HttpResponse.json(drawSyncFixture)),
	http.post("*/api/v1/draw-sync/run", () => HttpResponse.json(drawSyncFixture)),
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
