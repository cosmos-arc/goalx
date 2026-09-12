import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { expect, test } from "vitest";
import { todayFixture } from "../mocks/handlers";
import { server } from "../mocks/server";
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

test("renders the fixture comparison table with flags", async () => {
	await renderAt("/");

	expect(await screen.findByText("GoalX · 今日")).toBeInTheDocument();
	const rows = await screen.findAllByTestId("today-row");
	expect(rows).toHaveLength(3);

	expect(screen.getByText("阿森纳 vs 切尔西")).toBeInTheDocument();
	expect(screen.getByText("周六002")).toBeInTheDocument();
	// 未 join 的荷甲场次带缺口标记
	expect(screen.getByTestId("flag-not_joined")).toHaveTextContent("未 join 欧赔");
	expect(screen.getByTestId("flag-few_books")).toHaveTextContent("样本少");
	// EV 列渲染（阿森纳主胜 +4.0%）
	expect(screen.getAllByTestId("ev-cell")[0]).toHaveTextContent("+4.0%");
	// 未开售玩法显示占位（第三行全空）
	expect(screen.getAllByTestId("ev-cell")[2]).toHaveTextContent("—");
});

test("shows an empty state when no fixtures are on sale", async () => {
	server.use(http.get("*/api/v1/fixtures/today", () => HttpResponse.json([])));

	await renderAt("/");

	expect(await screen.findByTestId("today-empty")).toBeInTheDocument();
});

test("shows a degraded hint when the backend is unreachable", async () => {
	server.use(http.get("*/api/v1/fixtures/today", () => HttpResponse.json(todayFixture, { status: 503 })));

	await renderAt("/");

	expect(await screen.findByTestId("today-error")).toBeInTheDocument();
});
