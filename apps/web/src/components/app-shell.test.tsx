import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import { expect, test } from "vitest";
import { router } from "../router";

async function renderAt(path: string) {
	await router.navigate({ to: path });
	const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
	return render(
		<QueryClientProvider client={queryClient}>
			<RouterProvider router={router} />
		</QueryClientProvider>,
	);
}

test("renders the two-level flat IA: six primary items, three secondary, no group titles", async () => {
	await renderAt("/glossary");

	const primary = within(screen.getByRole("navigation", { name: "主导航" }));
	expect(primary.getAllByRole("link").map((link) => link.textContent)).toEqual([
		"总览",
		"今日",
		"投注",
		"历史",
		"验证",
		"资金",
	]);
	const secondary = within(screen.getByRole("navigation", { name: "次级导航" }));
	expect(secondary.getAllByRole("link").map((link) => link.textContent)).toEqual(["词典", "复核", "设置"]);

	// 不设组标题：除页面标题外不渲染任何 heading
	expect(screen.getByRole("heading", { name: "GoalX · 词典" })).toBeInTheDocument();
	expect(screen.getAllByRole("heading")).toHaveLength(1);
});

test("aria-current marks the active page in the primary nav", async () => {
	await renderAt("/today");

	const primary = within(screen.getByRole("navigation", { name: "主导航" }));
	expect(primary.getByRole("link", { name: "今日" })).toHaveAttribute("aria-current", "page");
	expect(primary.getByRole("link", { name: "总览" })).not.toHaveAttribute("aria-current");
	expect(primary.getByRole("link", { name: "投注" })).not.toHaveAttribute("aria-current");
});

test("aria-current marks the active page in the secondary nav without spilling over", async () => {
	await renderAt("/glossary");

	const secondary = within(screen.getByRole("navigation", { name: "次级导航" }));
	expect(secondary.getByRole("link", { name: "词典" })).toHaveAttribute("aria-current", "page");
	expect(secondary.getByRole("link", { name: "复核" })).not.toHaveAttribute("aria-current");
	for (const link of within(screen.getByRole("navigation", { name: "主导航" })).getAllByRole("link")) {
		expect(link).not.toHaveAttribute("aria-current");
	}
});

test("keeps the theme toggle beside the primary navigation", async () => {
	await renderAt("/history");

	// 测试环境无 dark class → 显示"切换到暗色主题"单按钮
	expect(screen.getByRole("button", { name: "切换到暗色主题" })).toBeInTheDocument();
	expect(screen.queryByRole("button", { name: "切换到浅色主题" })).not.toBeInTheDocument();
});
