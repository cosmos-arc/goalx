import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { router } from "../router";

async function renderAt(path: string) {
	await router.navigate({ to: path });
	const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
	render(
		<QueryClientProvider client={queryClient}>
			<RouterProvider router={router} />
		</QueryClientProvider>,
	);
}

test("reserved pages say what they are and when they arrive, not dead ends", async () => {
	await renderAt("/review");
	expect(await screen.findByRole("heading", { name: "GoalX · 复核" })).toBeInTheDocument();
	const state = screen.getByTestId("empty-state");
	expect(state).toHaveAttribute("data-variant", "not-available");
	expect(state).toHaveTextContent("赛后复盘");
	expect(state).toHaveTextContent("M3");
});

test("settings placeholder names its scope and milestone", async () => {
	await renderAt("/settings");
	expect(await screen.findByRole("heading", { name: "GoalX · 设置" })).toBeInTheDocument();
	const state = screen.getByTestId("empty-state");
	expect(state).toHaveTextContent("参数与数据源健康");
	expect(state).toHaveTextContent("M4");
});

test("glossary placeholder renders the not-available skeleton", async () => {
	await renderAt("/glossary");
	expect(await screen.findByRole("heading", { name: "GoalX · 词典" })).toBeInTheDocument();
	expect(screen.getByTestId("empty-state")).toHaveTextContent("随票 18");
});

// 票 15：`/` 已换为真实总览页，占位断言随之下线——见 overview-page.test.tsx。
// 票 17：`/history` 已换为真实历史页，占位断言随之下线——见 history-page.test.tsx。
