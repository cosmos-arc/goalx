import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { expect, test } from "vitest";
import { researchFixture } from "../mocks/handlers";
import { server } from "../mocks/server";
import { router } from "../router";

/**
 * 票 wb-02 研究页测试：逐书赔率明细（偏差高亮/缺向占位/书名剥前缀）、
 * 去水共识 + 模型概率/模型 EV（红涨绿跌）、资格判定、基本面与 AI 留位
 * （not-available）、选注篮就地建注、404/后端不可用/加载三态、返回场次。
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

function mockCreatedBet(id: number, captured: { body: unknown }) {
	server.use(
		http.post("*/api/v1/bets", async ({ request }) => {
			captured.body = await request.json();
			return HttpResponse.json(
				{
					id,
					slip_id: null,
					mode: "paper",
					market_kind: "fixed",
					purchased: false,
					stake: 100,
					actual_stake: null,
					strategy_version: null,
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
							locked_odds: 1.92,
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
}

test("renders the research view: books table with deviation highlight, consensus and model", async () => {
	await renderAt("/fixtures/1");

	expect(await screen.findByText("GoalX · 场次研究")).toBeInTheDocument();
	const page = await screen.findByTestId("research-page");
	expect(page).toBeInTheDocument();
	// 头部：对阵 + 编号/联赛 + 开赛信息 + 资格徽章（复用场次页编码）
	const header = screen.getByTestId("research-header");
	expect(header).toHaveTextContent("阿森纳");
	expect(within(header).getByText(/周六001 · 英超/)).toBeInTheDocument();
	expect(within(header).getByTestId("had-quote-valid")).toHaveTextContent("可投");
	expect(header).toHaveTextContent(/开赛 \d{2}:\d{2}（北京时间）/);

	// 逐书明细：竞彩参考行置顶 + 四本书行；书名剥 odds_api: 前缀；缺向占位 —
	const books = screen.getByTestId("research-books");
	expect(within(books).getByText("竞彩（投注价）")).toBeInTheDocument();
	const bookRows = within(books).getAllByTestId("research-book-row");
	expect(bookRows).toHaveLength(4);
	expect(within(books).getByText("pinnacle")).toBeInTheDocument();
	expect(within(books).queryByText("odds_api:pinnacle")).not.toBeInTheDocument();
	expect(within(bookRows[3] as HTMLElement).getByText("—")).toBeInTheDocument();

	// 偏差高亮：pinnacle 主胜 1.60（隐含 62.5%）vs 共识 53% → 琥珀 + 方向 ↑
	const pinnacleRow = bookRows.find((row) => row.textContent?.includes("pinnacle"));
	expect(pinnacleRow).toBeDefined();
	const flagged = within(pinnacleRow as HTMLElement).getByTestId("book-odds-h");
	expect(flagged).toHaveTextContent("1.60");
	expect(within(flagged).getByText("↑")).toBeInTheDocument();
	// 未达阈值的格不标（avg 主胜 1.90 → 52.6% vs 53%）
	const avgRow = bookRows.find((row) => row.textContent?.includes("avg")) as HTMLElement;
	expect(within(avgRow).getByTestId("book-odds-h")).not.toHaveTextContent("↑");

	// 共识块：books 数 + 三向概率 + 判读说明；分母 3 <4 → 琥珀低置信（票 39，接词条）
	const consensus = screen.getByTestId("research-consensus");
	expect(consensus).toHaveTextContent("主胜 53.0%");
	expect(consensus).toHaveTextContent("3 家三向均价 → Shin 去水");
	expect(within(consensus).getByTestId("consensus-low-confidence")).toHaveTextContent("共识低置信");

	// 模型块：概率 + 模型 EV 红涨绿跌 + 版本信息
	const model = screen.getByTestId("research-model");
	expect(model).toHaveTextContent("主胜 58.0%");
	expect(within(model).getByText("主胜 +11.4%")).toHaveClass("text-profit");
	expect(within(model).getByText("客胜 -33.4%")).toHaveClass("text-loss");
	expect(model).toHaveTextContent("dc-demo");

	// 证据链（票 14 V2）：三轨对照 + JS 徽章 + 情报时间线 + 复核/追问/盲评入口
	// （evidence 是独立请求，findBy 等 query 落定）
	const chain = await screen.findByTestId("research-chain");
	expect(chain).toHaveTextContent("证据链");
	expect(within(chain).getByTestId("chain-track-ml")).toHaveTextContent("ML 量化");
	expect(within(chain).getByTestId("chain-track-llm")).toHaveTextContent("复核");
	expect(within(chain).getByTestId("chain-track-fused")).toHaveTextContent("48.0%");
	expect(within(chain).getByTestId("chain-js")).toHaveTextContent("JS(ML,LLM)=0.072 → 已入复核");
	expect(within(chain).getAllByTestId("chain-intel")).toHaveLength(2);
	expect(within(chain).getByTestId("chain-rationale")).toHaveTextContent("analyst 复核");
	expect(within(chain).getByTestId("chain-verdict")).toHaveTextContent("未裁决");
	expect(within(chain).getByTestId("chain-ask-pending")).toBeDisabled();
	expect(within(chain).getByTestId("chain-blind-entry")).toHaveAttribute("href", "/review");

	// 返回场次链接常显
	expect(screen.getByRole("navigation", { name: "返回" })).toHaveTextContent("返回场次");
});

test("evidence chain degrades honestly when the evidence endpoint is missing (票 14)", async () => {
	server.use(
		http.get("*/api/v1/fixtures/:id/evidence", () => HttpResponse.json({ detail: "not found" }, { status: 404 })),
	);
	await renderAt("/fixtures/1");

	const chain = await screen.findByTestId("research-chain");
	const empty = await within(chain).findByTestId("empty-state");
	expect(empty).toHaveAttribute("data-variant", "not-available");
	expect(empty).toHaveTextContent("证据链暂不可用");
});

test("evidence chain renders sparse data honestly: missing tracks, no intels, decided verdict", async () => {
	server.use(
		http.get("*/api/v1/fixtures/:id/evidence", () =>
			HttpResponse.json({
				fixture_id: 1,
				generated_at: new Date().toISOString(),
				caliber: "证据链 = 已存证工件渲染。",
				tracks: {
					ml: {
						track: "ml",
						h: 0.53,
						d: 0.24,
						a: 0.23,
						issued_at: new Date().toISOString(),
						model_version: "dc-demo",
						rationale: null,
						analyst: false,
					},
					llm: null,
					fused: null,
				},
				divergence: { js: null, routed: false },
				intels: [],
				reviews: [
					{
						route: "post_settle",
						status: "done",
						verdict: "key_contribution",
						js_value: null,
						created_at: new Date().toISOString(),
						decided_at: new Date().toISOString(),
					},
				],
			}),
		),
	);
	await renderAt("/fixtures/1");

	const chain = await screen.findByTestId("research-chain");
	// LLM/Fused 缺轨 → "无产出"（不虚构）；ML 照常渲染
	await waitFor(() => expect(within(chain).getByTestId("chain-track-llm")).toHaveTextContent("无产出"));
	expect(within(chain).getByTestId("chain-track-fused")).toHaveTextContent("无产出");
	expect(within(chain).getByTestId("chain-track-ml")).toHaveTextContent("53.0%");
	// 无分歧读数 + 复核结论（已裁决） + 无情报诚实空态
	expect(within(chain).getByTestId("chain-js")).toHaveTextContent("无分歧读数");
	expect(within(chain).getByTestId("chain-verdict")).toHaveTextContent("情报有关键贡献");
	expect(within(chain).getByTestId("chain-intels-empty")).toHaveTextContent("不装懂");
});

test("picks into the basket and creates a suggestion in place", async () => {
	const user = userEvent.setup();
	const captured: { body: unknown } = { body: null };
	mockCreatedBet(88, captured);
	await renderAt("/fixtures/1");

	await user.click(await screen.findByTestId("pick-1-h"));
	expect(screen.getByTestId("basket-count")).toHaveTextContent("1/2");
	expect(screen.getByTestId("basket-summary")).toHaveTextContent("周六001 主胜");
	await user.click(screen.getByTestId("basket-open"));
	await user.clear(screen.getByTestId("basket-stake"));
	await user.type(screen.getByTestId("basket-stake"), "100");
	await user.click(screen.getByTestId("basket-submit"));

	expect(await screen.findByTestId("fixtures-message")).toHaveTextContent("已建建议 #88（单关）");
	expect(captured.body).toMatchObject({
		mode: "paper",
		stake: 100,
		legs: [{ fixture_id: 1, market_code: "had", selection_code: "h", locked_odds: 1.92 }],
	});
	expect(screen.getByTestId("basket-count")).toHaveTextContent("0/2");
});

test("hides model block honestly when there is no forecast", async () => {
	server.use(
		http.get("*/api/v1/fixtures/:id/research", () =>
			HttpResponse.json({ ...researchFixture, model: null, books: [], consensus: null }),
		),
	);
	await renderAt("/fixtures/1");

	expect(await screen.findByTestId("research-model-missing")).toBeInTheDocument();
	expect(screen.getByTestId("research-model-missing")).toHaveTextContent("暂无模型预测");
	// 无逐书报价 → 明细区诚实占位（不渲染空表）
	expect(screen.getByText(/暂无欧赔逐书报价/)).toBeInTheDocument();
	expect(screen.queryByTestId("research-book-row")).not.toBeInTheDocument();
	expect(screen.getByTestId("research-consensus")).toHaveTextContent("共识不可得");
});

test("disables picking and shows the reason when the fixture is not pickable", async () => {
	server.use(
		http.get("*/api/v1/fixtures/:id/research", () =>
			HttpResponse.json({
				...researchFixture,
				had_quote: {
					as_of: new Date().toISOString(),
					status: "rejected",
					reasons: ["sale_stopped"],
					sale_state: "stopped",
					single_eligible: true,
					jc_source_updated_at: null,
					eu_books: 4,
				},
			}),
		),
	);
	await renderAt("/fixtures/1");

	expect(await screen.findByTestId("pick-1-h")).toBeDisabled();
	expect(screen.getByText(/停售\/已开赛\/证据未知时不可选注/)).toBeInTheDocument();
	expect(screen.getByTestId("had-quote-rejected")).toHaveTextContent("已停售");
});

test("shows the 404 state with a way back for unknown fixtures", async () => {
	await renderAt("/fixtures/999");

	const state = await screen.findByTestId("empty-state");
	expect(state).toHaveAttribute("data-variant", "no-data");
	expect(state).toHaveTextContent("找不到这场研究视图");
	expect(within(state).getByRole("button", { name: "返回场次" })).toBeInTheDocument();
});

test("shows the backend-unavailable state with retry", async () => {
	const user = userEvent.setup();
	server.use(http.get("*/api/v1/fixtures/:id/research", () => HttpResponse.json({ detail: "down" }, { status: 503 })));
	await renderAt("/fixtures/1");

	const state = await screen.findByTestId("empty-state");
	expect(state).toHaveAttribute("data-variant", "backend-unavailable");
	expect(state).toHaveTextContent("task server");

	server.use(http.get("*/api/v1/fixtures/:id/research", () => HttpResponse.json(researchFixture)));
	await user.click(within(state).getByRole("button", { name: "重试" }));
	expect(await screen.findByTestId("research-page")).toBeInTheDocument();
});

test("shows the loading skeleton before data arrives", async () => {
	server.use(http.get("*/api/v1/fixtures/:id/research", () => new Promise<Response>(() => {})));
	await renderAt("/fixtures/1");

	expect(await screen.findByTestId("research-loading")).toBeInTheDocument();
	expect(screen.queryByTestId("empty-state")).not.toBeInTheDocument();
});

test("fixtures list links into the research page", async () => {
	const user = userEvent.setup();
	await renderAt("/fixtures");

	await user.click(await screen.findByTestId("fixtures-link-1"));
	await waitFor(() => expect(router.state.location.pathname).toBe("/fixtures/1"));
	expect(await screen.findByTestId("research-page")).toBeInTheDocument();
	// 一级导航"场次"在研究页保持激活（前缀归属）
	const primary = within(screen.getByRole("navigation", { name: "主导航" }));
	expect(primary.getByRole("link", { name: "场次" })).toHaveAttribute("aria-current", "page");
});
