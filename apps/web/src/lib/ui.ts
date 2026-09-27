/** 页面共享的展示工具（票 36）。 */

/**
 * 数字场景工具类（票 12，票 02 裁决）：等宽数字。盈亏/赔率列的颜色只是辅助，
 * 等宽数字 + 正负号才是可读性主承载（红绿色弱可读）。
 */
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

/** API 错误 → 可读文本（detail 字符串/对象都处理，不显示 [object Object]）。 */
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

import type { Bet } from "../api/goalx";

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

/** ISO 串 → MM-DD HH:mm；空值 → "—"。 */
export function shortTime(iso: string | null): string {
	return iso ? iso.slice(5, 16).replace("T", " ") : "—";
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

/** 注的金额：实际金额差异时 ¥建议→¥实际。 */
export function stakeText(bet: Bet): string {
	if (bet.actual_stake === null || bet.actual_stake === undefined) {
		return `¥${bet.stake.toFixed(2)}`;
	}
	return `¥${bet.stake.toFixed(2)}→¥${bet.actual_stake.toFixed(2)}`;
}
