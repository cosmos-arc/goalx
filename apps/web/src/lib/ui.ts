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
