/** 浅/暗双主题（票 12）：解析、应用与持久化。token 定义见 styles/globals.css。 */

export type Theme = "light" | "dark";

export const THEME_STORAGE_KEY = "goalx-theme";

/** 解析当前主题：localStorage 持久化值优先，否则跟随 prefers-color-scheme（默认浅色）。 */
export function resolveTheme(): Theme {
	let stored: string | null = null;
	try {
		stored = window.localStorage.getItem(THEME_STORAGE_KEY);
	} catch {
		// localStorage 不可用（隐私模式等）：退回系统偏好
	}
	if (stored === "dark" || stored === "light") {
		return stored;
	}
	if (typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches) {
		return "dark";
	}
	return "light";
}

/** 应用主题到 <html>：.dark class 驱动 globals.css 的 @custom-variant dark。 */
export function applyTheme(theme: Theme): void {
	document.documentElement.classList.toggle("dark", theme === "dark");
}

/** 持久化选择；写入失败时静默降级为会话内生效。 */
export function persistTheme(theme: Theme): void {
	try {
		window.localStorage.setItem(THEME_STORAGE_KEY, theme);
	} catch {
		// 同 resolveTheme：不可用不阻断切换
	}
}
