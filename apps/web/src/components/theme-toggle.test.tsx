import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { applyTheme, THEME_STORAGE_KEY } from "../lib/theme";
import { ThemeToggle } from "./theme-toggle";

// localStorage mock：断言持久化写入，不触真实存储
const localStorageMock = {
	getItem: vi.fn<(key: string) => string | null>(),
	setItem: vi.fn<(key: string, value: string) => void>(),
	removeItem: vi.fn<(key: string) => void>(),
	clear: vi.fn(),
};
vi.stubGlobal("localStorage", localStorageMock);

beforeEach(() => {
	localStorageMock.getItem.mockReturnValue(null);
	document.documentElement.classList.remove("dark");
});

afterEach(() => {
	document.documentElement.classList.remove("dark");
	vi.clearAllMocks();
});

test("浅色初始：点按切到暗色并持久化", async () => {
	render(<ThemeToggle />);
	const button = screen.getByRole("button", { name: "切换到暗色主题" });
	expect(button).toHaveAttribute("aria-pressed", "false");

	await userEvent.click(button);

	expect(document.documentElement.classList.contains("dark")).toBe(true);
	expect(localStorageMock.setItem).toHaveBeenCalledWith(THEME_STORAGE_KEY, "dark");
	expect(screen.getByRole("button", { name: "切换到浅色主题" })).toHaveAttribute("aria-pressed", "true");
});

test("暗色初始（main.tsx 已应用 .dark）：点按切回浅色并持久化", async () => {
	applyTheme("dark");
	render(<ThemeToggle />);

	await userEvent.click(screen.getByRole("button", { name: "切换到浅色主题" }));

	expect(document.documentElement.classList.contains("dark")).toBe(false);
	expect(localStorageMock.setItem).toHaveBeenCalledWith(THEME_STORAGE_KEY, "light");
});
