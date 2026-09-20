import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { expect, test } from "vitest";
import { reviewQueueFixture } from "../mocks/handlers";
import { server } from "../mocks/server";
import { router } from "../router";

/**
 * 票 14 复核页测试：复核队列渲染 + 结论三分类提交 + 盲评二选一
 * （选后揭晓 + 幂等提示）+ 队列空态 + 后端不可用降级。
 */

async function renderAt(path: string) {
	await router.navigate({ to: path });
	const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
	return render(
		<QueryClientProvider client={queryClient}>
			<RouterProvider router={router} />
		</QueryClientProvider>,
	);
}

test("queue renders open items and submits a three-way verdict", async () => {
	const captured: { path: string; body: unknown } = { path: "", body: null };
	server.use(
		http.post("*/api/v1/review/items/:itemId/verdict", async ({ request }) => {
			captured.path = new URL(request.url).pathname;
			captured.body = await request.json();
			return HttpResponse.json({ recorded: true });
		}),
	);
	await renderAt("/review");

	expect(await screen.findByRole("heading", { name: "GoalX · 复核" })).toBeVisible();
	expect(screen.getByTestId("review-caliber")).toHaveTextContent("只进评测集");
	const queue = screen.getByTestId("review-queue");
	const row = await within(queue).findByTestId("review-queue-row");
	expect(row).toHaveTextContent("阿森纳 vs 切尔西");
	expect(row).toHaveTextContent("赛前分歧");
	expect(row).toHaveTextContent("JS 0.072");

	await userEvent.click(within(row).getByTestId("verdict-key_contribution"));
	await waitFor(() => expect(screen.getAllByText("已记录（只进评测集）").length).toBeGreaterThan(0));
	expect(captured.path).toBe("/api/v1/review/items/7/verdict");
	expect(captured.body).toEqual({ classification: "key_contribution" });
});

test("blind review picks a card, reveals tracks, and posts the choice", async () => {
	const captured: { body: unknown } = { body: null };
	server.use(
		http.post("*/api/v1/blind-reviews", async ({ request }) => {
			captured.body = await request.json();
			return HttpResponse.json({ recorded: true });
		}),
	);
	await renderAt("/review");

	const panel = await screen.findByTestId("review-blind");
	// 等场次选择器出现（队列加载完成）再断言盲评卡
	await within(panel).findByTestId("blind-fixture-select");
	const cardA = await within(panel).findByTestId("blind-card-甲");
	const cardB = await within(panel).findByTestId("blind-card-乙");
	// 揭晓前不显示轨道标签
	expect(within(cardA).queryByTestId("blind-reveal-甲")).toBeNull();
	await userEvent.click(within(cardB).getByTestId("blind-pick-乙"));

	// 提交载荷：choice 映射到乙卡对应轨道（按渲染顺序），周期为双周标签
	await waitFor(() => expect(captured.body).not.toBeNull());
	const body = captured.body as { cycle: string; fixture_id: number; choice: string };
	expect(body.cycle).toMatch(/^\d{4}-B\d+$/);
	expect(body.fixture_id).toBe(1);
	expect(["ml", "llm"]).toContain(body.choice);
	// 选后揭晓两卡的轨道
	expect(await within(panel).findByTestId("blind-reveal-甲")).toBeVisible();
	expect(within(panel).getByTestId("blind-reveal-乙")).toBeVisible();
	expect(within(panel).getByTestId("blind-result")).toHaveTextContent("已记录");
});

test("empty queue degrades honestly and blind panel explains", async () => {
	server.use(http.get("*/api/v1/review/queue", () => HttpResponse.json({ items: [] })));
	await renderAt("/review");

	expect(await screen.findByTestId("review-queue-empty")).toHaveTextContent("暂无待复核项");
	expect(screen.getByTestId("blind-no-fixture")).toBeVisible();
});

test("blind review switches fixture from the queue dropdown and reports submit failure", async () => {
	server.use(
		http.get("*/api/v1/review/queue", () =>
			HttpResponse.json({
				items: [
					{ ...reviewQueueFixture.items[0] },
					{
						id: 8,
						fixture_id: 2,
						home_team: "利物浦",
						away_team: "曼城",
						competition: "英超",
						kickoff_utc: new Date().toISOString(),
						route: "post_settle",
						js_value: null,
						status: "open",
						created_at: new Date().toISOString(),
					},
				],
			}),
		),
		http.post("*/api/v1/blind-reviews", () => HttpResponse.error()),
	);
	await renderAt("/review");

	const panel = await screen.findByTestId("review-blind");
	const select = await within(panel).findByTestId("blind-fixture-select");
	// 切换到场次 2（无证据数据）→ 诚实报错，盲评不可进行
	await userEvent.selectOptions(select, "2");
	expect(await within(panel).findByTestId("blind-evidence-error")).toHaveTextContent("盲评需要两轨研判");
});

test("backend-unavailable shows retry on the queue", async () => {
	server.use(http.get("*/api/v1/review/queue", () => HttpResponse.error()));
	await renderAt("/review");

	const degraded = await screen.findByTestId("empty-state");
	await waitFor(() => expect(degraded).toHaveAttribute("data-variant", "backend-unavailable"));
});
