/** 页面共享的展示工具（票 36）。 */

/**
 * 数字场景工具类（票 12，票 02 裁决）：等宽数字。盈亏/赔率列的颜色只是辅助，
 * 等宽数字 + 正负号才是可读性主承载（红绿色弱可读）。
 */
import type { Bet } from "../api/goalx";

export const TABULAR_NUMS = "tabular-nums";

export const SELECTION_LABELS: Record<string, string> = {
	h: "主胜",
	d: "平",
	a: "客胜",
};

/** had 三向选择（票 wb-03 起从 had-quote-ui 下沉到 lib：组合引擎等纯函数也要用）。 */
export type Selection = "h" | "d" | "a";

export const SELECTIONS: Selection[] = ["h", "d", "a"];

// ---- 业务日工具（票 wb-01 定稿口径，票 wb-03 起跨页共享：场次页/玩法页） ----

/** 北京时区业务日（YYYY-MM-DD）——与后端 beijing_business_date 同口径（全后端唯一出处）。 */
export function beijingBusinessDate(now: number): string {
	return new Date(now + 8 * 3_600_000).toISOString().slice(0, 10);
}

export function addDays(isoDate: string, days: number): string {
	const date = new Date(`${isoDate}T00:00:00Z`);
	date.setUTCDate(date.getUTCDate() + days);
	return date.toISOString().slice(0, 10);
}

/** 业务日的人话标签：今天/明天/后天，更远落 MM月DD。 */
export function dayLabel(isoDate: string, now: number): string {
	const today = beijingBusinessDate(now);
	if (isoDate === today) {
		return "今天";
	}
	if (isoDate === addDays(today, 1)) {
		return "明天";
	}
	if (isoDate === addDays(today, 2)) {
		return "后天";
	}
	return isoDate.slice(5).replace("-", "月");
}

/** 路由词典型（IA 命名）：总览/场次/玩法(胜平负·进球·14场任9)/投注/历史/验证/资金/词典/复核/设置。 */
export type AppRoute =
	| "/"
	| "/fixtures"
	| "/markets"
	| "/markets/had"
	| "/markets/goals"
	| "/markets/pool"
	| "/bets"
	| "/history"
	| "/validation"
	| "/bankroll"
	| "/glossary"
	| "/review"
	| "/settings";

/** 价变 409 的逐腿报价载荷；非该形态（含畸形字段——消费者直接 .toFixed）返回 null。 */
export function currentQuotes(error: unknown): CurrentQuote[] | null {
	if (typeof error !== "object" || error === null || !("detail" in error)) {
		return null;
	}
	const detail = (error as { detail: unknown }).detail;
	if (typeof detail !== "object" || detail === null || !("current_quotes" in detail)) {
		return null;
	}
	const quotes = (detail as { current_quotes: unknown }).current_quotes;
	if (!Array.isArray(quotes) || quotes.length === 0) {
		return null;
	}
	// 逐字段校验（对齐 intentConflict）：一条畸形即整体按非价变处理，不让坏行崩确认流
	const wellFormed = quotes.every(
		(quote) =>
			typeof quote === "object" &&
			quote !== null &&
			typeof (quote as Record<string, unknown>)["selection_code"] === "string" &&
			typeof (quote as Record<string, unknown>)["server_odds"] === "number",
	);
	return wellFormed ? (quotes as CurrentQuote[]) : null;
}

import type { CurrentQuote } from "../api/goalx";

/** 建注 409 的原注摘要（票 09 意图冲突载荷）；非该形态返回 null。 */
export type ExistingBetSummary = { id: number; mode: "paper" | "live"; stake: number };

export function intentConflict(error: unknown): ExistingBetSummary | null {
	if (typeof error !== "object" || error === null || !("detail" in error)) {
		return null;
	}
	const detail = (error as { detail: unknown }).detail;
	if (typeof detail !== "object" || detail === null || !("existing_bet" in detail)) {
		return null;
	}
	const existing = (detail as { existing_bet: unknown }).existing_bet;
	if (typeof existing !== "object" || existing === null) {
		return null;
	}
	const { id, mode, stake } = existing as Record<string, unknown>;
	if (typeof id !== "number" || (mode !== "paper" && mode !== "live") || typeof stake !== "number") {
		return null;
	}
	return { id, mode, stake };
}

/** 服务器返回的注模式 → 中文（成功提示展示实际落库口径，票 09）。 */
export function betModeLabel(mode: string): string {
	return mode === "live" ? "真金" : "纸面";
}

/** 服务器返回的注条款标签（模式+金额）——成功提示/冲突摘要同款（票 09）。 */
export function betTermsLabel(bet: { mode: string; stake: number }): string {
	return `${betModeLabel(bet.mode)} ¥${bet.stake}`;
}

/** 意图冲突恢复消息（票 09）：had/进球三处提交路径共享同一措辞骨架。 */
export function conflictRestoreMessage(prefix: string, conflict: ExistingBetSummary): string {
	return (
		`${prefix}原提交其实已成功：#${conflict.id}（${betTermsLabel(conflict)}）——已恢复原结果；` +
		"本次新条款未建立，确认后再点提交将按新意图另建"
	);
}

export function errorText(error: unknown): string {
	if (typeof error === "object" && error !== null && "detail" in error) {
		const detail = (error as { detail: unknown }).detail;
		if (typeof detail === "string") {
			return detail;
		}
		return JSON.stringify(detail);
	}
	return String(error);
}

// ---- 展示口径助手（lean-audit 票 03 收敛：此前散落 9 文件的逐字/同语义副本） ----

/** 盈亏数字永远只按正负红绿（票 02 钱层编码，正负号为主承载）。 */
export function pnlClass(value: number | null): string {
	if (value === null || value === 0) {
		return "text-muted-foreground";
	}
	return value > 0 ? "text-profit" : "text-loss";
}

/** 带符号人民币：0 不带符号（投注/历史页口径）。 */
export function signedCny(value: number): string {
	return `${value > 0 ? "+" : value < 0 ? "-" : ""}¥${Math.abs(value).toFixed(2)}`;
}

/** 带符号人民币：0 也带 +（总览页口径——日盈亏零值显 +¥0.00）。 */
export function signedCnyAlways(value: number): string {
	return `${value >= 0 ? "+" : "-"}¥${Math.abs(value).toFixed(2)}`;
}

/** ISO 串 → 北京时区 "MM-DD HH:mm"（原实现切片 ISO=UTC 显示，报告二
 * §八 UI 小件收口：显式 Asia/Shanghai，浏览器/CI 时区无关）；空值 → "—"。 */
export function shortTime(iso: string | null): string {
	return bjTime(iso, "MM-DD HH:mm");
}

/** UTC 时刻 → 北京时区 HH:MM（非法 → "—"）；had-quote-ui 与 bets 页同口径
 * （review-20260928 票 08 收敛；报告二 §八：显式 Asia/Shanghai 不随浏览器漂移）。 */
export function localTime(utc: string): string {
	return bjTime(utc, "HH:mm");
}

const _BJ_CACHE = new Map<string, Intl.DateTimeFormat>();
/** ISO 时刻 → 北京时区格式化（HH:mm / MM-DD HH:mm 两种形状；非法/空 → "—"）。 */
function bjTime(iso: string | null, shape: "HH:mm" | "MM-DD HH:mm"): string {
	if (!iso) {
		return "—";
	}
	const date = new Date(iso);
	if (Number.isNaN(date.getTime())) {
		return "—";
	}
	let fmt = _BJ_CACHE.get(shape);
	if (fmt === undefined) {
		fmt = new Intl.DateTimeFormat(
			"en-CA",
			shape === "HH:mm"
				? { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }
				: {
						timeZone: "Asia/Shanghai",
						month: "2-digit",
						day: "2-digit",
						hour: "2-digit",
						minute: "2-digit",
						hourCycle: "h23",
					},
		);
		_BJ_CACHE.set(shape, fmt);
	}
	return shape === "HH:mm" ? fmt.format(date) : fmt.format(date).replace(", ", " ");
}

/** 跨日标签（推荐流跨 3 日窗口）：业务日非今天时给"明天/后天"提示，今天 → undefined。 */
export function dayNoteOf(businessDate: string | null | undefined, now: number): string | undefined {
	const today = beijingBusinessDate(now);
	const day = businessDate ?? today;
	return day === today ? undefined : dayLabel(day, now);
}

/** 概率小数 → 百分比（digits 位小数）；空值 → "—"。 */
export function pct(value: number | null | undefined, digits = 0): string {
	return value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)}%`;
}

/** canvas 取不到 CSS 变量，option 构建时解析语义 token（解析失败退回近似色）。 */
export function cssVar(name: string, fallback: string): string {
	const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
	return raw === "" ? fallback : raw;
}

/** 注的腿描述：#场次 玩法@锁定价（实际价差异时 锁定价→实际价），多腿 × 连接。 */
export function legText(bet: Bet): string {
	return bet.legs
		.map((leg) => {
			const odds =
				leg.actual_odds !== null && leg.actual_odds !== undefined
					? `${leg.locked_odds.toFixed(2)}→${leg.actual_odds.toFixed(2)}`
					: leg.locked_odds.toFixed(2);
			return `#${leg.fixture_id} ${leg.market_code === "hhad" ? `让球${leg.goal_line ?? "?"} ` : ""}${
				SELECTION_LABELS[leg.selection_code] ?? leg.selection_code
			}@${odds}`;
		})
		.join(" × ");
}

/** 腿集合里可解析的最早开赛时刻（ms）；全缺/不可解析 → null（overview 与 bets 同口径，票 08 批二收敛）。 */
export function earliestKickoffMs(
	legs: ReadonlyArray<{ fixture_id: number }>,
	kickoffById: ReadonlyMap<number, string>,
): number | null {
	let earliest: number | null = null;
	for (const leg of legs) {
		const iso = kickoffById.get(leg.fixture_id);
		if (!iso) {
			continue;
		}
		const at = new Date(iso).getTime();
		if (!Number.isNaN(at) && (earliest === null || at < earliest)) {
			earliest = at;
		}
	}
	return earliest;
}

/** 未结（open）注全部腿的场次 id 集。 */
export function openBetFixtureIds(bets: readonly Bet[]): Set<number> {
	return new Set(bets.filter((bet) => bet.status === "open").flatMap((bet) => bet.legs.map((leg) => leg.fixture_id)));
}

/** 注的金额：实际金额差异时 ¥建议→¥实际。 */
export function stakeText(bet: Bet): string {
	if (bet.actual_stake === null || bet.actual_stake === undefined) {
		return `¥${bet.stake.toFixed(2)}`;
	}
	return `¥${bet.stake.toFixed(2)}→¥${bet.actual_stake.toFixed(2)}`;
}
