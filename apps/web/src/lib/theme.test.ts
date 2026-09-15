import { afterEach, describe, expect, it, vi } from "vitest";
import { applyTheme, persistTheme, resolveTheme, THEME_STORAGE_KEY } from "./theme";

// localStorage mock：隔离真实存储，并允许按用例编排返回值
const store = new Map<string, string>();
vi.stubGlobal("localStorage", {
	getItem: (key: string) => store.get(key) ?? null,
	setItem: (key: string, value: string) => void store.set(key, value),
	removeItem: (key: string) => void store.delete(key),
	clear: () => store.clear(),
});

function stubPrefersDark(matches: boolean) {
	vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches }));
}

afterEach(() => {
	store.clear();
	document.documentElement.classList.remove("dark");
});

describe("resolveTheme", () => {
	it("持久化值优先于系统偏好", () => {
		store.set(THEME_STORAGE_KEY, "light");
		stubPrefersDark(true);
		expect(resolveTheme()).toBe("light");
		store.set(THEME_STORAGE_KEY, "dark");
		stubPrefersDark(false);
		expect(resolveTheme()).toBe("dark");
	});

	it("未持久化时跟随 prefers-color-scheme", () => {
		stubPrefersDark(true);
		expect(resolveTheme()).toBe("dark");
		stubPrefersDark(false);
		expect(resolveTheme()).toBe("light");
	});

	it("非法持久化值与缺失 matchMedia 都回退浅色", () => {
		store.set(THEME_STORAGE_KEY, "blue");
		expect(resolveTheme()).toBe("light");
		store.clear();
		// jsdom 无 matchMedia：模拟真实缺失（不 stub 即 undefined）
		expect(resolveTheme()).toBe("light");
	});
});

describe("applyTheme / persistTheme", () => {
	it("applyTheme 切换 <html> 的 .dark class", () => {
		applyTheme("dark");
		expect(document.documentElement.classList.contains("dark")).toBe(true);
		applyTheme("light");
		expect(document.documentElement.classList.contains("dark")).toBe(false);
	});

	it("persistTheme 写入 localStorage（供重载保留）", () => {
		persistTheme("dark");
		expect(store.get(THEME_STORAGE_KEY)).toBe("dark");
	});
});
