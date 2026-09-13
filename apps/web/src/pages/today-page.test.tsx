import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

test("renders the fixture comparison table with flags and eligibility", async () => {
	await renderAt("/");

	expect(await screen.findByText("GoalX · 今日")).toBeInTheDocument();
	const rows = await screen.findAllByTestId("today-row");
	expect(rows).toHaveLength(3);

	expect(screen.getByText("阿森纳 vs 切尔西")).toBeInTheDocument();
	expect(screen.getByText("周六002")).toBeInTheDocument();
	// 未 join 的荷甲场次带缺口标记
	expect(screen.getByTestId("flag-not_joined")).toHaveTextContent("未 join 欧赔");
	expect(screen.getByTestId("flag-few_books")).toHaveTextContent("样本少");
	// EV 列渲染（阿森纳主胜 +4.0%），偏差标记不冒充机会
	expect(screen.getAllByTestId("ev-cell")[0]).toHaveTextContent("+4.0%");
	// 资格列：可投/单固、停售拒绝、证据未知（票 35 判定）
	expect(screen.getByTestId("had-quote-valid")).toHaveTextContent("可投");
	expect(screen.getByTestId("had-quote-valid")).toHaveTextContent("单固");
	expect(screen.getByTestId("had-quote-rejected")).toHaveTextContent("已停售");
	expect(screen.getByTestId("had-quote-unknown")).toHaveTextContent("证据未知");
	// 未开售玩法显示占位（第三行全空）
	expect(screen.getAllByTestId("ev-cell")[2]).toHaveTextContent("—");
});

test("picks selections into the basket and creates a suggestion", async () => {
	const user = userEvent.setup();
	let captured: unknown = null;
	server.use(
		http.post("*/api/v1/bets", async ({ request }) => {
			captured = await request.json();
			return HttpResponse.json(
				{
					id: 77,
					slip_id: null,
					mode: "paper",
					market_kind: "fixed",
					purchased: false,
					stake: 100,
					actual_stake: null,
					strategy_version: "manual-v1",
					placed_at: null,
					locked_at: null,
					created_at: "2026-09-12T10:00:00+00:00",
					status: "open",
					payout: null,
					profit: null,
					settled_at: null,
					legs: [
						{
							fixture_id: 1,
							market_code: "had",
							selection_code: "h",
							locked_odds: 6.5,
							actual_odds: null,
							goal_line: null,
						},
					],
					review: null,
				},
				{ status: 201 },
			);
		}),
	);
	await renderAt("/");

	await user.click(await screen.findByTestId("pick-1-h"));
	expect(screen.getByTestId("selection-basket")).toHaveTextContent("1/2");
	await user.click(screen.getByText("移除")); // 篮内移除
	expect(screen.getByTestId("selection-basket")).toHaveTextContent("0/2");
	await user.click(screen.getByTestId("pick-1-h"));
	await user.selectOptions(screen.getByTestId("basket-mode"), "live"); // live 模式切换
	await user.click(screen.getByTestId("pick-2-a")); // 不同场次可作第二腿
	expect(screen.getByTestId("selection-basket")).toHaveTextContent("2/2");
	await user.clear(screen.getByTestId("basket-stake"));
	await user.type(screen.getByTestId("basket-stake"), "100");
	await user.type(screen.getByTestId("basket-strategy"), "manual-v1");
	await user.click(screen.getByRole("button", { name: "建立建议" }));

	expect(await screen.findByTestId("today-message")).toHaveTextContent("已建建议 #77（2串1）");
	expect(captured).toMatchObject({
		mode: "live", // 上一步切到 live
		stake: 100,
		strategy_version: "manual-v1",
		legs: [
			{ fixture_id: 1, selection_code: "h", locked_odds: 6.5 },
			{ fixture_id: 2, selection_code: "a" },
		],
	});
});

test("rejects a same-fixture second leg and shows server rejection reasons", async () => {
	const user = userEvent.setup();
	await renderAt("/");

	await user.click(await screen.findByTestId("pick-1-h"));
	await user.click(screen.getByTestId("pick-1-d")); // 同场第二选 → 拒绝
	expect(await screen.findByTestId("today-message")).toHaveTextContent("一场比赛只能选一腿");

	// 服务器资格拒绝原因透出（票 36：界面提示不替代服务器校验）
	server.use(
		http.post("*/api/v1/bets", () => HttpResponse.json({ detail: "fixture 1 不可投: sale_stopped" }, { status: 400 })),
	);
	await user.click(screen.getByRole("button", { name: "建立建议" }));
	expect(await screen.findByTestId("today-message")).toHaveTextContent("sale_stopped");

	// detail 为对象时 JSON 透出
	server.use(http.post("*/api/v1/bets", () => HttpResponse.json({ detail: { code: "not_single" } }, { status: 400 })));
	await user.click(screen.getByRole("button", { name: "建立建议" }));
	expect(await screen.findByTestId("today-message")).toHaveTextContent("not_single");
});

test("basket rules: non-single warning, rejected second leg, cap at two, deselect", async () => {
	const user = userEvent.setup();
	// 加一场「已停售但有报价」的场次，覆盖第二腿拒绝分支
	server.use(
		http.get("*/api/v1/fixtures/today", () =>
			HttpResponse.json([
				...todayFixture,
				{
					fixture_id: 4,
					match_code: "周六004",
					competition: "英超",
					tier: "tier1",
					home_team: "C 队",
					away_team: "D 队",
					kickoff_utc: "2026-09-13T22:00:00+00:00",
					is_single: false,
					joined: false,
					jc_odds: { h: 2.5, d: 3.1, a: 2.8 },
					jc_updated_at: null,
					books: 0,
					eu_prob: null,
					ev: null,
					flags: [],
					had_quote: {
						as_of: "2026-09-12T12:05:00+00:00",
						status: "rejected",
						reasons: ["sale_stopped"],
						sale_state: "stopped",
						single_eligible: null,
						jc_source_updated_at: null,
						eu_books: 0,
					},
				},
			]),
		),
	);
	await renderAt("/");

	// 非单固首 pick：提示但仍可选（服务器最终裁决）
	await user.click(await screen.findByTestId("pick-2-a"));
	expect(await screen.findByTestId("today-message")).toHaveTextContent("非单固或不可投");
	await user.click(screen.getByTestId("pick-2-d")); // 同场第二选先拒绝
	expect(screen.getByTestId("today-message")).toHaveTextContent("一场比赛只能选一腿");
	// 停售但有报价的场次不可作第二腿
	await user.click(await screen.findByTestId("pick-4-h"));
	expect(screen.getByTestId("today-message")).toHaveTextContent("不可作第二腿");
	expect(screen.getByTestId("today-message")).toHaveTextContent("已停售");
	// 凑满两腿后再点第三选 → 上限提示
	await user.click(screen.getByTestId("pick-1-h"));
	expect(screen.getByTestId("selection-basket")).toHaveTextContent("2/2");
	await user.click(screen.getByTestId("pick-1-d"));
	expect(screen.getByTestId("today-message")).toHaveTextContent("首版仅支持单关与 2串1");
	// 再点已选的同一选项 → 移除
	await user.click(screen.getByTestId("pick-1-h"));
	expect(screen.getByTestId("selection-basket")).toHaveTextContent("1/2");
	expect(screen.getByText("周六002")).toBeInTheDocument(); // fixture 2 仍在篮中
});

test("shows no-verdict placeholder when the backend omits had_quote", async () => {
	server.use(
		http.get("*/api/v1/fixtures/today", () =>
			HttpResponse.json([
				{
					fixture_id: 9,
					match_code: "周日009",
					competition: "英超",
					tier: "tier1",
					home_team: "X 队",
					away_team: "Y 队",
					kickoff_utc: "2026-09-14T02:00:00+00:00",
					is_single: false,
					joined: false,
					jc_odds: { h: 2.0, d: 3.0, a: 3.0 },
					jc_updated_at: null,
					books: 0,
					eu_prob: null,
					ev: null,
					flags: [],
				},
			]),
		),
	);
	await renderAt("/");

	expect(await screen.findByText("X 队 vs Y 队")).toBeInTheDocument();
	expect(screen.getByText("无判定")).toBeInTheDocument();
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
