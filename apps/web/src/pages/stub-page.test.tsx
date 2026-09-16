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

test("history placeholder renders the not-available skeleton", async () => {
	await renderAt("/history");
	expect(await screen.findByRole("heading", { name: "GoalX · 历史" })).toBeInTheDocument();
	expect(screen.getByTestId("empty-state")).toHaveTextContent("随票 17");
});

test("glossary placeholder renders the not-available skeleton", async () => {
	await renderAt("/glossary");
	expect(await screen.findByRole("heading", { name: "GoalX · 词典" })).toBeInTheDocument();
	expect(screen.getByTestId("empty-state")).toHaveTextContent("随票 18");
});

test("overview placeholder at / guides to today", async () => {
	await renderAt("/");

	expect(await screen.findByRole("heading", { name: "GoalX · 总览" })).toBeInTheDocument();
	const state = screen.getByTestId("empty-state");
	expect(state).toHaveAttribute("data-variant", "not-available");
	expect(state).toHaveTextContent("随票 15");
	expect(screen.getByRole("link", { name: "先去今日看盘" })).toHaveAttribute("href", "/today");
});
