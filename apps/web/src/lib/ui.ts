/** 页面共享的展示工具（票 36）。 */

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
