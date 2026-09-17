import { describe, expect, test } from "vitest";
import type { GoalsFixture, GoalsSelection, TodayFixture } from "../api/goalx";
import {
	buildGoalsCombo,
	buildHadCombo,
	GOALS_COMBO_CONFIG,
	HAD_COMBO_CONFIG,
	isGoalsPickable,
	rankGoalsFixturesForFeed,
} from "./combo-engine";

/**
 * 票 wb-03 组合引擎 v1 纯函数单测：排序/约束（单注 1–5%、同场不重复、
 * 串关 ≤2 腿、EV≤0 不入选、单关须单固）/边界（空输入、未入金、bankroll 过小、
 * books=0）/口径（预期收益 = EV×注额、约束说明逐条给出）。
 */

function fixture(overrides: Partial<TodayFixture> & Pick<TodayFixture, "fixture_id" | "match_code">): TodayFixture {
	return {
		business_date: "2026-09-17",
		competition: "英超",
		tier: "tier1",
		home_team: `主${overrides.fixture_id}`,
		away_team: `客${overrides.fixture_id}`,
		kickoff_utc: new Date(Date.now() + 3 * 3_600_000).toISOString(),
		is_single: true,
		joined: true,
		jc_odds: { h: 2.0, d: 3.4, a: 3.2 },
		jc_updated_at: new Date().toISOString(),
		books: 5,
		eu_prob: { h: 0.55, d: 0.25, a: 0.2 },
		ev: { h: 0.1, d: -0.15, a: -0.36 },
		flags: [],
		had_quote: {
			as_of: new Date().toISOString(),
			status: "valid",
			reasons: [],
			sale_state: "on_sale",
			single_eligible: true,
			jc_source_updated_at: new Date().toISOString(),
			eu_books: 5,
		},
		...overrides,
	} as TodayFixture;
}

describe("buildHadCombo 排序与 top-N", () => {
	test("按 EV×置信排序取前 N；置信 = books/5 截断 1", () => {
		const thin = fixture({
			fixture_id: 1,
			match_code: "周六001",
			books: 2,
			ev: { h: 0.1, d: null, a: null }, // score = 0.1 × 0.4 = 0.04
		});
		const thick = fixture({
			fixture_id: 2,
			match_code: "周六002",
			books: 5,
			ev: { h: 0.05, d: null, a: null }, // score = 0.05 × 1 = 0.05
		});
		const result = buildHadCombo({
			candidates: [
				{ fixture: thin, pickable: true },
				{ fixture: thick, pickable: true },
			],
			bankroll: 10_000,
		});
		// 厚样本低 EV 反超薄样本高 EV（样本厚度降权）
		expect(result.picks.map((pick) => pick.fixture.fixture_id)).toEqual([2, 1]);
		expect(result.picks[0]?.score).toBeCloseTo(0.05, 10);
		expect(result.picks[1]?.score).toBeCloseTo(0.04, 10);
		expect(result.picks[1]?.confidence).toBeCloseTo(0.4, 10);
	});

	test("置信随 books 超基准截断为 1（10 books 不放大）", () => {
		const many = fixture({ fixture_id: 1, match_code: "周六001", books: 10, ev: { h: 0.1, d: null, a: null } });
		const result = buildHadCombo({ candidates: [{ fixture: many, pickable: true }], bankroll: 1000 });
		expect(result.picks[0]?.confidence).toBe(1);
	});

	test("同场多向只取最优一腿（同场不重复）", () => {
		const both = fixture({
			fixture_id: 1,
			match_code: "周六001",
			books: 5,
			jc_odds: { h: 2.0, d: 4.4, a: 3.2 },
			ev: { h: 0.1, d: 0.06, a: -0.1 }, // 两向为正
		});
		const other = fixture({ fixture_id: 2, match_code: "周六002", ev: { h: 0.03, d: null, a: null } });
		const result = buildHadCombo({
			candidates: [
				{ fixture: both, pickable: true },
				{ fixture: other, pickable: true },
			],
			bankroll: 1000,
		});
		expect(result.picks).toHaveLength(2);
		expect(result.picks[0]?.selection).toBe("h"); // 该场最优向
		expect(result.picks[1]?.fixture.fixture_id).toBe(2);
	});

	test("maxPicks 封顶（串关 ≤2 腿约束）；同分按开赛时间早者先", () => {
		const early = fixture({
			fixture_id: 1,
			match_code: "周六001",
			kickoff_utc: new Date(Date.now() + 1 * 3_600_000).toISOString(),
		});
		const mid = fixture({
			fixture_id: 2,
			match_code: "周六002",
			kickoff_utc: new Date(Date.now() + 2 * 3_600_000).toISOString(),
		});
		const late = fixture({
			fixture_id: 3,
			match_code: "周六003",
			kickoff_utc: new Date(Date.now() + 5 * 3_600_000).toISOString(),
		});
		const result = buildHadCombo({
			candidates: [
				{ fixture: late, pickable: true },
				{ fixture: early, pickable: true },
				{ fixture: mid, pickable: true },
			],
			bankroll: 1000,
		});
		expect(result.picks).toHaveLength(HAD_COMBO_CONFIG.maxPicks);
		expect(result.picks.map((pick) => pick.fixture.fixture_id)).toEqual([1, 2]);
	});
});

describe("buildHadCombo 约束", () => {
	test("EV≤0 / EV null 不入选", () => {
		const allNegative = fixture({ fixture_id: 1, match_code: "周六001", ev: { h: -0.01, d: -0.2, a: 0 } });
		const noEv = fixture({ fixture_id: 2, match_code: "周六002", ev: null });
		const result = buildHadCombo({
			candidates: [
				{ fixture: allNegative, pickable: true },
				{ fixture: noEv, pickable: true },
			],
			bankroll: 1000,
		});
		expect(result.picks).toHaveLength(0);
		expect(result.expectedProfit).toBe(0);
		expect(result.totalStake).toBe(0);
	});

	test("非可投场（停售/已开赛）不入组合", () => {
		const stopped = fixture({ fixture_id: 1, match_code: "周六001" });
		const result = buildHadCombo({ candidates: [{ fixture: stopped, pickable: false }], bankroll: 1000 });
		expect(result.picks).toHaveLength(0);
	});

	test("非单固场不入组合（单关须单固）——仍进推荐流", () => {
		const parlayOnly = fixture({
			fixture_id: 1,
			match_code: "周六001",
			had_quote: { ...fixture({ fixture_id: 1, match_code: "周六001" }).had_quote!, single_eligible: false },
		});
		const result = buildHadCombo({ candidates: [{ fixture: parlayOnly, pickable: true }], bankroll: 1000 });
		expect(result.picks).toHaveLength(0);
		expect(result.feedOrder).toHaveLength(1);
	});

	test("books=0（无共识样本）不入组合", () => {
		const noBooks = fixture({ fixture_id: 1, match_code: "周六001", books: 0, ev: { h: 0.1, d: null, a: null } });
		const result = buildHadCombo({ candidates: [{ fixture: noBooks, pickable: true }], bankroll: 1000 });
		expect(result.picks).toHaveLength(0);
	});

	test("竞彩价缺失或异常（≤1）的向不入选", () => {
		const broken = fixture({
			fixture_id: 1,
			match_code: "周六001",
			jc_odds: { h: null, d: 1.0, a: 3.0 },
			ev: { h: 0.2, d: 0.2, a: -0.1 },
		});
		const result = buildHadCombo({ candidates: [{ fixture: broken, pickable: true }], bankroll: 1000 });
		expect(result.picks).toHaveLength(0);
	});
});

describe("buildHadCombo 档位（flat）与边界", () => {
	test("flat 2% 在 1–5% 区间内：bankroll 10000 → 单注 200", () => {
		const f = fixture({ fixture_id: 1, match_code: "周六001" });
		const result = buildHadCombo({ candidates: [{ fixture: f, pickable: true }], bankroll: 10_000 });
		expect(result.picks[0]?.stake).toBe(200);
		expect(result.picks[0]?.expectedProfit).toBeCloseTo(0.1 * 200, 6);
		expect(result.bankrollNote).toBeNull();
	});

	test("小 bankroll：flat 2% 被 1% 下限托起（0.5% 目标 → 1%）", () => {
		const config = {
			...HAD_COMBO_CONFIG,
			stake: { ...HAD_COMBO_CONFIG.stake, flatFraction: 0.005 },
		};
		const f = fixture({ fixture_id: 1, match_code: "周六001" });
		const result = buildHadCombo({ candidates: [{ fixture: f, pickable: true }], bankroll: 1000, config });
		expect(result.picks[0]?.stake).toBe(10); // max(5, 10) = 10（1% 下限）
	});

	test("flat 超上限被 5% 截断", () => {
		const config = { ...HAD_COMBO_CONFIG, stake: { ...HAD_COMBO_CONFIG.stake, flatFraction: 0.5 } };
		const f = fixture({ fixture_id: 1, match_code: "周六001" });
		const result = buildHadCombo({ candidates: [{ fixture: f, pickable: true }], bankroll: 1000, config });
		expect(result.picks[0]?.stake).toBe(50);
	});

	test("未入金（bankroll 0）：按最低注 ¥2 建议并诚实说明", () => {
		const f = fixture({ fixture_id: 1, match_code: "周六001" });
		const result = buildHadCombo({ candidates: [{ fixture: f, pickable: true }], bankroll: 0 });
		expect(result.picks[0]?.stake).toBe(2);
		expect(result.bankrollNote).toContain("未入金");
		expect(result.bankrollNote).toContain("¥2");
	});

	test("bankroll 过小：最低注超出 5% 上限时说明冲突", () => {
		const f = fixture({ fixture_id: 1, match_code: "周六001" });
		const result = buildHadCombo({ candidates: [{ fixture: f, pickable: true }], bankroll: 10 });
		expect(result.picks[0]?.stake).toBe(2); // 5% 上限 = 0.5 < 2 → 最低注兜底
		expect(result.bankrollNote).toContain("超出 5% 上限");
	});

	test("空输入：picks/feedOrder/收益全空，约束说明仍给出", () => {
		const result = buildHadCombo({ candidates: [], bankroll: 1000 });
		expect(result.picks).toEqual([]);
		expect(result.feedOrder).toEqual([]);
		expect(result.expectedProfit).toBe(0);
		expect(result.notes.length).toBeGreaterThanOrEqual(5);
	});
});

describe("buildHadCombo 口径输出", () => {
	test("组合预期收益 = Σ EV×注额（共识口径），约束说明含口径标注", () => {
		const a = fixture({ fixture_id: 1, match_code: "周六001", ev: { h: 0.1, d: null, a: null } });
		const b = fixture({ fixture_id: 2, match_code: "周六002", ev: { h: 0.04, d: null, a: null } });
		const result = buildHadCombo({
			candidates: [
				{ fixture: a, pickable: true },
				{ fixture: b, pickable: true },
			],
			bankroll: 1000,
		});
		// 两条单关各 ¥20：总注额 40，预期收益 = 20×0.1 + 20×0.04 = 2.8
		expect(result.picks).toHaveLength(2);
		expect(result.totalStake).toBe(40);
		expect(result.expectedProfit).toBeCloseTo(2.8, 6);
		expect(result.notes.some((note) => note.includes("共识口径"))).toBe(true);
		expect(result.notes.some((note) => note.includes("EV≤0 不入选"))).toBe(true);
		expect(result.notes.some((note) => note.includes("flat"))).toBe(true);
	});

	test("推荐流：正 EV 场按 score 降序在前，无正 EV 场按开赛时间沉底", () => {
		const strong = fixture({ fixture_id: 1, match_code: "周六001", ev: { h: 0.2, d: null, a: null } });
		const weak = fixture({
			fixture_id: 2,
			match_code: "周六002",
			books: 5,
			kickoff_utc: new Date(Date.now() + 4 * 3_600_000).toISOString(),
			ev: { h: 0.02, d: null, a: null },
		});
		const negativeSoon = fixture({
			fixture_id: 3,
			match_code: "周六003",
			kickoff_utc: new Date(Date.now() + 1 * 3_600_000).toISOString(),
			ev: { h: -0.05, d: null, a: null },
		});
		const negativeLater = fixture({
			fixture_id: 4,
			match_code: "周六004",
			kickoff_utc: new Date(Date.now() + 6 * 3_600_000).toISOString(),
			ev: null,
		});
		const result = buildHadCombo({
			candidates: [
				{ fixture: negativeLater, pickable: false },
				{ fixture: weak, pickable: true },
				{ fixture: negativeSoon, pickable: true },
				{ fixture: strong, pickable: true },
			],
			bankroll: 1000,
		});
		expect(result.feedOrder.map((f) => f.fixture_id)).toEqual([1, 2, 3, 4]);
	});
});

// ---- 票 wb-05：进球类组合引擎（模型×竞彩价 EV、单关为主不组串）----

function goalsSel(code: string, odds: number | null, ev: number | null): GoalsSelection {
	return { code, odds, probability: ev === null ? null : 0.2, ev };
}

function goalsRow(
	overrides: Partial<GoalsFixture> & Pick<GoalsFixture, "fixture_id" | "match_code">,
	ttg: GoalsSelection[] = [
		goalsSel("0", 11.0, -0.26),
		goalsSel("1", 4.1, -0.25),
		goalsSel("2", 4.5, 0.1025),
		goalsSel("3", 3.4, -0.25),
	],
	sale = "on_sale",
	single = true,
): GoalsFixture {
	const block = { selections: ttg, single_eligible: single, sale_state: sale, updated_at: new Date().toISOString() };
	return {
		business_date: "2026-09-17",
		competition: "英超",
		tier: "tier1",
		home_team: `主${overrides.fixture_id}`,
		away_team: `客${overrides.fixture_id}`,
		kickoff_utc: new Date(Date.now() + 3 * 3_600_000).toISOString(),
		ttg: block,
		crs: { ...block, selections: [goalsSel("1:1", 6.0, -0.27), goalsSel("0:0", 11.0, -0.26)] },
		model_version: "dc-demo",
		issued_at: new Date().toISOString(),
		...overrides,
	} as GoalsFixture;
}

describe("buildGoalsCombo 排序与约束（单关为主）", () => {
	test("按模型 EV 降序取 top-N 独立单关；同场只取最优一注", () => {
		const a = goalsRow({ fixture_id: 1, match_code: "周六001" }); // ttg s2 +10.25%
		const b = goalsRow({ fixture_id: 2, match_code: "周六002" }, [goalsSel("0", 30.0, 0.3), goalsSel("1", 4.1, -0.25)]); // ttg s0 +30%
		const c = goalsRow({ fixture_id: 3, match_code: "周六003" }, [goalsSel("0", 20.0, 0.2)]);
		const d = goalsRow({ fixture_id: 4, match_code: "周六004" }, [goalsSel("0", 15.0, 0.15)]);
		const result = buildGoalsCombo({ rows: [a, b, c, d], bankroll: 1000, now: Date.now() });
		expect(result.picks.map((pick) => pick.fixture.fixture_id)).toEqual([2, 3, 4]); // top-3
		expect(result.picks[0]).toMatchObject({ market: "ttg", selection: "0", score: 0.3 });
		expect(result.picks.every((pick) => pick.score === pick.ev)).toBe(true); // 无置信加权
	});

	test("同场 ttg 与 crs 都有正 EV 时只取该场最优一注（同场不重复）", () => {
		const row = goalsRow({ fixture_id: 1, match_code: "周六001" });
		const withCrs = {
			...row,
			crs: { ...row.crs, selections: [goalsSel("1:1", 6.0, 0.05)] },
		} as GoalsFixture;
		const result = buildGoalsCombo({ rows: [withCrs], bankroll: 1000, now: Date.now() });
		expect(result.picks).toHaveLength(1);
		expect(result.picks[0]).toMatchObject({ market: "ttg", selection: "2" }); // EV 0.1025 > 0.05
	});

	test("EV≤0 / 无模型（ev null）不入选，但场次留在推荐流", () => {
		const allNegative = goalsRow({ fixture_id: 1, match_code: "周六001" }, [
			goalsSel("0", 11.0, -0.26),
			goalsSel("2", 4.5, -0.1),
		]);
		const noModel = goalsRow({ fixture_id: 2, match_code: "周六002" }, [goalsSel("2", 4.5, null)]);
		const result = buildGoalsCombo({
			rows: [allNegative, noModel],
			bankroll: 1000,
			now: Date.now(),
		});
		expect(result.picks).toHaveLength(0);
		expect(result.feedOrder).toHaveLength(2);
	});

	test("停售 / 非单固 / 已开赛的场不入组合（isGoalsPickable 同构 had 口径）", () => {
		const now = Date.now();
		const stopped = goalsRow({ fixture_id: 1, match_code: "周六001" }, undefined, "stopped", true);
		const parlayOnly = goalsRow({ fixture_id: 2, match_code: "周六002" }, undefined, "on_sale", false);
		const kickedOff = goalsRow({
			fixture_id: 3,
			match_code: "周六003",
			kickoff_utc: new Date(now - 6_000_000).toISOString(),
		});
		expect(isGoalsPickable(stopped, "ttg", now)).toBe(false);
		expect(isGoalsPickable(parlayOnly, "crs", now)).toBe(false);
		expect(isGoalsPickable(kickedOff, "ttg", now)).toBe(false);
		const result = buildGoalsCombo({
			rows: [stopped, parlayOnly, kickedOff],
			bankroll: 1000,
			now,
		});
		expect(result.picks).toHaveLength(0);
	});

	test("flat 注额与 had 同档：bankroll 10000 → 单注 200，预期收益 = EV×注额", () => {
		const row = goalsRow({ fixture_id: 1, match_code: "周六001" });
		const result = buildGoalsCombo({ rows: [row], bankroll: 10_000, now: Date.now() });
		expect(result.picks[0]?.stake).toBe(200);
		expect(result.picks[0]?.expectedProfit).toBeCloseTo(0.1025 * 200, 6);
		expect(result.totalStake).toBe(200);
		expect(result.bankrollNote).toBeNull();
	});

	test("未入金：最低注 ¥2 + 诚实说明（复用 had 档位边界）", () => {
		const row = goalsRow({ fixture_id: 1, match_code: "周六001" });
		const result = buildGoalsCombo({ rows: [row], bankroll: 0, now: Date.now() });
		expect(result.picks[0]?.stake).toBe(GOALS_COMBO_CONFIG.stake.minStakeCny);
		expect(result.bankrollNote).toContain("未入金");
	});

	test("约束说明给进球类口径：不组串 / 模型×竞彩价 / 置信缺位不加权", () => {
		const result = buildGoalsCombo({ rows: [], bankroll: 1000, now: Date.now() });
		expect(result.picks).toEqual([]);
		expect(result.notes.some((note) => note.includes("不组串"))).toBe(true);
		expect(result.notes.some((note) => note.includes("模型×竞彩价"))).toBe(true);
		expect(result.notes.some((note) => note.includes("不加权"))).toBe(true);
		expect(result.notes.some((note) => note.includes("flat"))).toBe(true);
	});

	test("推荐流：有模型场按最优 EV 降序在前，无模型/无正 EV 场按开赛时间沉底", () => {
		const strong = goalsRow({ fixture_id: 1, match_code: "周六001" });
		const weak = goalsRow(
			{ fixture_id: 2, match_code: "周六002", kickoff_utc: new Date(Date.now() + 4 * 3_600_000).toISOString() },
			[goalsSel("0", 12.0, 0.02)],
		);
		const noModelSoon = goalsRow(
			{
				fixture_id: 3,
				match_code: "周六003",
				kickoff_utc: new Date(Date.now() + 1 * 3_600_000).toISOString(),
				model_version: null,
				issued_at: null,
			},
			[goalsSel("2", 4.5, null)],
		);
		const noModelLater = goalsRow(
			{
				fixture_id: 4,
				match_code: "周六004",
				kickoff_utc: new Date(Date.now() + 6 * 3_600_000).toISOString(),
				model_version: null,
				issued_at: null,
			},
			[goalsSel("2", 4.5, null)],
		);
		expect(rankGoalsFixturesForFeed([noModelLater, weak, noModelSoon, strong]).map((f) => f.fixture_id)).toEqual([
			1, 2, 3, 4,
		]);
	});
});
