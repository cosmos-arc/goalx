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
