import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";
import { GLOSSARY } from "../lib/glossary";
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

/** 票 18：词典页——检索框 + 词条卡列表（三要素齐全），无结果走 EmptyState no-data。 */
test("lists all first-batch entries with the three required elements", async () => {
	await renderAt("/glossary");

	expect(await screen.findByRole("heading", { name: "GoalX · 词典" })).toBeInTheDocument();
	const list = screen.getByTestId("glossary-list");
	expect(within(list).getAllByRole("article")).toHaveLength(GLOSSARY.length);

	// 三要素齐全：EV 卡 = 定义 + 判读方向徽章 + 实例 + 口径警示（"诊断量"警示不得删减）
	const evCard = screen.getByTestId("glossary-card-ev");
	expect(evCard).toHaveTextContent("欧共识概率 × 竞彩价 − 1");
	expect(within(evCard).getByText(/判读方向 · /)).toBeInTheDocument();
	expect(evCard).toHaveTextContent("例：");
	expect(evCard).toHaveTextContent("诊断量，不是机会信号");
	// 高亮警示词条逐条在卡上
	expect(screen.getByTestId("glossary-card-eligibility")).toHaveTextContent("可投 ≠ 必成交");
	expect(screen.getByTestId("glossary-card-skill")).toHaveTextContent("三条件只认前瞻");
});

test("search filters by term/alias/definition and empties into no-data with a reset action", async () => {
	const user = userEvent.setup();
	await renderAt("/glossary");

	const search = await screen.findByTestId("glossary-search");

	// 别名命中（大小写不敏感）：clv_prob → CLV 卡
	await user.type(search, "clv_prob");
	expect(screen.getByTestId("glossary-list")).toHaveTextContent("CLV");
	expect(screen.queryByTestId("glossary-card-ev")).not.toBeInTheDocument();

	// term 命中：单固
	await user.clear(search);
	await user.type(search, "单固");
	expect(screen.getByTestId("glossary-card-single")).toBeInTheDocument();

	// 无命中 → EmptyState no-data + 清空检索动作
	await user.clear(search);
	await user.type(search, "量子纠缠");
	const empty = screen.getByTestId("empty-state");
	expect(empty).toHaveAttribute("data-variant", "no-data");
	await user.click(within(empty).getByRole("button", { name: "清空检索" }));
	expect(screen.getAllByRole("article")).toHaveLength(GLOSSARY.length);
});
