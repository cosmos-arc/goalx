import { describe, expect, test } from "vitest";
import type { TodayFixture } from "../api/goalx";
import { buildHadCombo, HAD_COMBO_CONFIG } from "./combo-engine";

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
