import type { TodayFixture } from "../api/goalx";
import { SELECTIONS, type Selection } from "./ui";

/**
 * 胜平负组合引擎 v1（票 wb-03，纯函数、前端侧）。
 *
 * 输入 = 场次窗口（行内 EV/竞彩价/资格判定/单固）+ bankroll + 档位参数；
 * 输出 = 推荐组合（腿列表、每腿建议注额、组合预期收益、约束说明）。
 *
 * 规则 v1（map 定稿）：
 * - 排序：EV × 置信，取 top-N（置信 = books/基准 截断 1——样本厚度越薄越降权）；
 * - 约束：单注 1–5% bankroll、同场不重复、串关 ≤2 腿、EV≤0 不入选；
 * - 组合 = 一组独立单关注单（单关须单固；非单固正 EV 场留在推荐流人工研判）；
 * - 预期收益 = Σ EV×注额（共识口径——EV 是诊断量非机会信号，卡片逐条标注）。
 *
 * 仓位档位：纸面期一律 flat（本引擎唯一档位）；真金 ¼Kelly 与仓位细化归票 06，
 * 到时扩展 StakeProfile 即可，不动排序/约束逻辑。
 */

/** 档位参数（票 wb-03：flat 单档；票 06 在此结构上扩展）。 */
export type StakeProfile = {
	/** flat 档目标单注比例（2%）。 */
	flatFraction: number;
	/** 单注占 bankroll 硬区间（map 定稿 1–5%）。 */
	minFraction: number;
	maxFraction: number;
	/** 绝对下限：竞彩最小注 ¥2（比例区间无法表达的最小面额）。 */
	minStakeCny: number;
};

export type ComboEngineConfig = {
	/** top-N 上限（v1 = 2，与串关 ≤2 腿约束一致）。 */
	maxPicks: number;
	/** 置信满分基准：置信 = min(books/该值, 1)。 */
	confidenceBooks: number;
	stake: StakeProfile;
};

/** 玩法页默认档（纯常量：改参数不改逻辑）。 */
export const HAD_COMBO_CONFIG: ComboEngineConfig = {
	maxPicks: 2,
	confidenceBooks: 5,
	stake: { flatFraction: 0.02, minFraction: 0.01, maxFraction: 0.05, minStakeCny: 2 },
};

export type ComboCandidate = {
	/** 场次窗口行（行内 EV/竞彩价/资格判定/单固齐备）。 */
	fixture: TodayFixture;
	/** 调用方用 isPickable 判定后传入（可投 = 证据链完整且新鲜 + 在售 + 未开赛）。 */
	pickable: boolean;
};

export type ComboPick = {
	fixture: TodayFixture;
	selection: Selection;
	/** 竞彩价（建建议时的 locked_odds）。 */
	odds: number;
	/** 共识口径 EV（诊断量，非机会信号）。 */
	ev: number;
	books: number;
	confidence: number;
	/** 排序键 = EV × 置信。 */
	score: number;
	/** flat 档建议注额（¥）。 */
	stake: number;
	/** EV × 注额（共识口径）。 */
	expectedProfit: number;
};

export type ComboResult = {
	picks: ComboPick[];
	/** 推荐流排序：最优向 score 降序在前；无正 EV 场沉底按开赛时间升序。 */
	feedOrder: TodayFixture[];
	totalStake: number;
	/** Σ EV×注额（共识口径，卡片标注）。 */
	expectedProfit: number;
	/** 应用了的约束与口径说明（卡片逐条展示）。 */
	notes: string[];
	/** bankroll 边界说明（未入金/过小）；null = 比例正常。 */
	bankrollNote: string | null;
};

/** 置信 = 样本厚度（books 线性放大，截断 1；无 book 无共识 → 0 不入组合）。 */
function confidenceFor(books: number, confidenceBooks: number): number {
	if (confidenceBooks <= 0) {
		return 1;
	}
	return Math.min(Math.max(books, 0) / confidenceBooks, 1);
}

/** 开赛时间排序键：不可解析的时间戳沉底（不炸排序）。 */
function kickoffMs(fixture: TodayFixture): number {
	const at = new Date(fixture.kickoff_utc).getTime();
	return Number.isNaN(at) ? Number.MAX_SAFE_INTEGER : at;
}

/**
 * flat 档单注额：目标比例 → 1–5% 硬区间截断 → ¥2 最低注兜底。
 * bankroll 未入金/过小时诚实降级并给说明（不静默给 0 或越限比例）。
 */
function flatStake(bankroll: number, profile: StakeProfile): { stake: number; note: string | null } {
	if (!(bankroll > 0)) {
		return {
			stake: profile.minStakeCny,
			note: "资金池未入金——flat 档暂按竞彩最低注 ¥2 建议，入金后按 bankroll 1–5% 校准",
		};
	}
	const upper = bankroll * profile.maxFraction;
	const clamped = Math.min(Math.max(bankroll * profile.flatFraction, bankroll * profile.minFraction), upper);
	const stake = Math.round(Math.max(clamped, profile.minStakeCny) * 100) / 100;
	if (stake > upper) {
		return {
			stake,
			note: `bankroll ¥${bankroll.toFixed(2)} 较小：最低注 ¥${profile.minStakeCny} 已超出 5% 上限（¥${Math.round(upper * 100) / 100}）——建议先入金再按比例投注`,
		};
	}
	return { stake, note: null };
}

/** 一场的最优向得分（>0 才有资格排前；无正 EV / 无 EV 场得 0）。 */
function bestScore(fixture: TodayFixture, confidenceBooks: number): number {
	const confidence = confidenceFor(fixture.books, confidenceBooks);
	let best = 0;
	for (const sel of SELECTIONS) {
		const ev = fixture.ev?.[sel];
		if (ev !== null && ev !== undefined && ev > 0) {
			best = Math.max(best, ev * confidence);
		}
	}
	return best;
}

/**
 * 推荐流排序（独立于 bankroll/档位）：最优向 score 降序在前，无正 EV 场沉底
 * 按开赛时间升序——资金池未就绪时页面也能先行渲染推荐流。
 */
export function rankFixturesForFeed(
	fixtures: TodayFixture[],
	confidenceBooks: number = HAD_COMBO_CONFIG.confidenceBooks,
): TodayFixture[] {
	return fixtures
		.slice()
		.sort((a, b) => bestScore(b, confidenceBooks) - bestScore(a, confidenceBooks) || kickoffMs(a) - kickoffMs(b));
}

/**
 * 构建胜平负组合 v1。
 *
 * @example
 * buildHadCombo({ candidates: [{ fixture, pickable: true }], bankroll: 1000 })
 * // → { picks: [...], feedOrder: [...], expectedProfit, notes, bankrollNote }
 */
export function buildHadCombo(input: {
	candidates: ComboCandidate[];
	bankroll: number;
	config?: ComboEngineConfig | undefined;
}): ComboResult {
	const config = input.config ?? HAD_COMBO_CONFIG;
	const { candidates, bankroll } = input;

	// 1) 展开向级候选：可投 + 单固 + EV>0 + 竞彩价在 → score = EV × 置信。
	const expanded: Array<{
		fixture: TodayFixture;
		selection: Selection;
		odds: number;
		ev: number;
		books: number;
		confidence: number;
		score: number;
	}> = [];
	for (const { fixture, pickable } of candidates) {
		if (!pickable || fixture.had_quote?.single_eligible !== true) {
			continue;
		}
		const confidence = confidenceFor(fixture.books, config.confidenceBooks);
		if (confidence <= 0) {
			continue;
		}
		for (const selection of SELECTIONS) {
			const ev = fixture.ev?.[selection];
			const odds = fixture.jc_odds?.[selection];
			if (ev === null || ev === undefined || ev <= 0) {
				continue;
			}
			if (odds === null || odds === undefined || !(odds > 1)) {
				continue;
			}
			expanded.push({ fixture, selection, odds, ev, books: fixture.books, confidence, score: ev * confidence });
		}
	}
	expanded.sort((a, b) => b.score - a.score || kickoffMs(a.fixture) - kickoffMs(b.fixture));

	// 2) top-N 贪心：同场不重复（一场只取最优一腿，竞彩禁同场串关）。
	const stakeInfo = flatStake(bankroll, config.stake);
	const picks: ComboPick[] = [];
	const usedFixtures = new Set<number>();
	for (const cand of expanded) {
		if (picks.length >= config.maxPicks) {
			break;
		}
		if (usedFixtures.has(cand.fixture.fixture_id)) {
			continue;
		}
		usedFixtures.add(cand.fixture.fixture_id);
		picks.push({ ...cand, stake: stakeInfo.stake, expectedProfit: cand.ev * stakeInfo.stake });
	}

	// 3) 推荐流：全部场次按最优向 score 降序；无正 EV 场沉底按开赛时间。
	const feedOrder = rankFixturesForFeed(
		candidates.map((candidate) => candidate.fixture),
		config.confidenceBooks,
	);

	const notes = [
		`排序：EV × 置信（置信 = books/${config.confidenceBooks} 截断 1），取前 ${config.maxPicks} 条`,
		`单注注额：flat 档 = bankroll ${(config.stake.flatFraction * 100).toFixed(0)}%（区间 ${(config.stake.minFraction * 100).toFixed(0)}–${(config.stake.maxFraction * 100).toFixed(0)}%，竞彩最低 ¥${config.stake.minStakeCny}）——纸面期一律 flat，仓位细化随票 06`,
		"同场不重复；EV≤0 不入选；单关须单固（非单固场的正 EV 见下方推荐流自行研判）",
		`串关 ≤${config.maxPicks} 腿（竞彩仅 2串1）`,
		"预期收益 = EV × 注额（共识口径）——EV 是诊断量非机会信号，见词典",
	];

	return {
		picks,
		feedOrder,
		totalStake: Math.round(picks.length * stakeInfo.stake * 100) / 100,
		expectedProfit: Math.round(picks.reduce((sum, pick) => sum + pick.expectedProfit, 0) * 100) / 100,
		notes,
		bankrollNote: stakeInfo.note,
	};
}
