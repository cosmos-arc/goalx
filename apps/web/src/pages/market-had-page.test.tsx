import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { expect, test } from "vitest";
import { bankrollFixture, todayFixture } from "../mocks/handlers";
import { server } from "../mocks/server";
import { router } from "../router";

/**
 * 票 wb-03 胜平负玩法页测试：组合头部卡（引擎产出/注数/预期收益口径/约束说明）、
 * 一键带入选注篮（就地打开 + flat 档注额预填）、推荐流排序（EV×置信、
 * 沉底按开赛时间、非可投卡禁用 + 资格徽章、跨日标签）、组合空窗诚实占位、
 * 三态空状态。引擎约束与档位边界的穷举见 lib/combo-engine.test.ts。
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
					stake: 100.08,
					actual_stake: null,
					strategy_version: null,
					placed_at: null,
					locked_at: null,
					created_at: "2026-09-17T10:00:00+00:00",
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

/** 默认 mock 下唯一正 EV：fixture 1 主胜 +5.3%（books 2 → 置信 0.4）。 */
const F1_STAKE = 100.08; // bankroll 5004.2 × 2%，在 1–5% 区间内

test("renders the combo card from the engine and the feed in engine order", async () => {
	await renderAt("/markets/had");

	expect(await screen.findByText("GoalX · 胜平负")).toBeInTheDocument();

	// 组合卡：注数 1、flat 档注额、EV×注额 口径、约束说明
	const combo = await screen.findByTestId("market-combo");
	expect(within(combo).getByTestId("market-combo-count")).toHaveTextContent("1");
	expect(within(combo).getByTestId("market-combo-pick-1")).toHaveTextContent("周六001");
	expect(within(combo).getByTestId("market-combo-pick-1")).toHaveTextContent("主胜");
	expect(within(combo).getByTestId("market-combo-pick-1")).toHaveTextContent(`¥${F1_STAKE.toFixed(2)}`);
	expect(within(combo).getByTestId("market-combo-pick-1")).toHaveTextContent("+5.3%");
	expect(within(combo).getByTestId("market-combo-total")).toHaveTextContent(`总注额 ¥${F1_STAKE.toFixed(2)}`);
	expect(within(combo).getByTestId("market-combo-total")).toHaveTextContent("共识口径");
	const notes = within(combo).getByTestId("market-combo-notes");
	expect(notes).toHaveTextContent("EV≤0 不入选");
	expect(notes).toHaveTextContent("flat");
	expect(notes).toHaveTextContent("串关 ≤2 腿");

	// flat 档口径行：bankroll 真金余额 + 2% 建议
	expect(screen.getByTestId("market-bankroll-note")).toHaveTextContent(`flat 档单注 ¥${F1_STAKE.toFixed(2)}`);
	expect(screen.getByTestId("market-bankroll-note")).toHaveTextContent("5004.20");

	// 票 wb-06 组合卡建议仓位（只读）：后端口径 = 引擎 flat 数字 + 档位理由
	const comboAdvice = await within(combo).findByTestId("market-combo-stake-advice");
	expect(comboAdvice).toHaveTextContent(`¥${F1_STAKE.toFixed(2)}`);
	expect(within(comboAdvice).getByTestId("market-combo-stake-advice-reason")).toHaveTextContent(
		"纸面期一律 flat（红线）",
	);

	// 推荐流：唯一正 EV 场置顶；其余按开赛时间（1.5h → 5h → 26h）
	const feed = screen.getByTestId("market-feed");
	const cards = within(feed).getAllByTestId(/^market-card-/);
	expect(cards.map((card) => card.getAttribute("data-testid"))).toEqual([
		"market-card-1",
		"market-card-2",
		"market-card-3",
		"market-card-4",
	]);
	// 非可投场（fixture 3 停售）：按钮禁用 + 资格徽章说明原因；跨日场（4）带"明天"标签
	expect(screen.getByTestId("market-pick-3-h")).toBeDisabled();
	expect(within(screen.getByTestId("market-card-3")).getByTestId("had-quote-rejected")).toHaveTextContent("已停售");
	expect(within(screen.getByTestId("market-card-4")).getByText("明天")).toBeInTheDocument();
	expect(screen.getByTestId("market-pick-1-h")).toBeEnabled();
});

test("apply combo loads legs into the in-page basket with the flat stake prefilled", async () => {
	const user = userEvent.setup();
	const captured: { body: unknown } = { body: null };
	mockCreatedBet(88, captured);
	await renderAt("/markets/had");

	await screen.findByTestId("market-combo-pick-1");
	await user.click(screen.getByTestId("market-combo-apply"));

	// 就地选注篮：腿已带入、flat 档注额预填、口径提示（EV×注额 共识口径）
	expect(await screen.findByTestId("basket-stake")).toHaveValue(F1_STAKE);
	expect(screen.getByTestId("market-message")).toHaveTextContent("已带入 1 条推荐");

	// 票 wb-06 选注篮建议仓位（只读）：注额与档位理由随当前模式/篮内选择
	const basketAdvice = await screen.findByTestId("basket-stake-advice");
	expect(basketAdvice).toHaveTextContent(`¥${F1_STAKE.toFixed(2)}`);
	expect(screen.getByTestId("basket-stake-advice-reason")).toHaveTextContent("纸面期一律 flat（红线）");
	expect(basketAdvice).toHaveTextContent("单关口径");
	const drawerLegs = await screen.findAllByTestId("basket-leg");
	expect(drawerLegs).toHaveLength(1);
	expect(drawerLegs[0]).toHaveTextContent("周六001");
	expect(drawerLegs[0]).toHaveTextContent("主胜");

	// 提交建建议：单关、引擎注额、locked_odds = 竞彩价
	await user.click(screen.getByTestId("basket-submit"));
	expect(await screen.findByTestId("market-message")).toHaveTextContent("已建建议 #88（单关）");
	expect(captured.body).toMatchObject({
		mode: "paper",
		stake: F1_STAKE,
		legs: [{ fixture_id: 1, market_code: "had", selection_code: "h", locked_odds: 1.92 }],
	});
	expect(screen.getByTestId("basket-count")).toHaveTextContent("0/2");
});

test("two positive-EV picks fill the combo: totals aggregate and apply warns the 2串1 caliber", async () => {
	const user = userEvent.setup();
	// fixture 4 也给正向 EV（books 5 → 置信 1 → score 0.06 > fixture 1 的 0.053×0.4）
	server.use(
		http.get("*/api/v1/fixtures/today", () =>
			HttpResponse.json(
				todayFixture.map((row) => (row.fixture_id === 4 ? { ...row, ev: { h: 0.06, d: -0.109, a: -0.076 } } : row)),
			),
		),
	);
	await renderAt("/markets/had");

	const combo = await screen.findByTestId("market-combo");
	expect(await within(combo).findByTestId("market-combo-pick-4")).toHaveTextContent("主胜");
	expect(within(combo).getByTestId("market-combo-pick-1")).toBeInTheDocument();
	expect(within(combo).getByTestId("market-combo-count")).toHaveTextContent("2");
	// 合计 = 两条单关之和（共识口径线性合计）
	expect(within(combo).getByTestId("market-combo-total")).toHaveTextContent(`总注额 ¥${(F1_STAKE * 2).toFixed(2)}`);
	expect(within(combo).getByTestId("market-combo-total")).toHaveTextContent("预期收益");

	await user.click(within(combo).getByTestId("market-combo-apply"));
	expect(screen.getByTestId("market-message")).toHaveTextContent("已带入 2 条推荐");
	expect(screen.getByTestId("market-message")).toHaveTextContent("独立单关口径");
	expect(screen.getByTestId("basket-count")).toHaveTextContent("2/2");
	// 引擎保证同场不重复 + ≤2 腿：两条腿来自不同场次
	const drawerLegs = screen.getAllByTestId("basket-leg");
	expect(drawerLegs).toHaveLength(2);
});

test("feed picks follow the shared basket rules (same-fixture replace, deselect, cap)", async () => {
	const user = userEvent.setup();
	await renderAt("/markets/had");

	await screen.findByTestId("market-card-4");
	await user.click(screen.getByTestId("market-pick-4-h"));
	expect(screen.getByTestId("basket-count")).toHaveTextContent("1/2");
	expect(screen.getByTestId("basket-summary")).toHaveTextContent("周日004 主胜");

	// 同场换选 = 替换 + 提示；再点同选项 = 取消（与场次页同规则）
	await user.click(screen.getByTestId("market-pick-4-d"));
	expect(screen.getByTestId("market-message")).toHaveTextContent("同场只能选一腿");
	expect(screen.getByTestId("basket-summary")).toHaveTextContent("周日004 平");
	await user.click(screen.getByTestId("market-pick-4-d"));
	expect(screen.getByTestId("basket-count")).toHaveTextContent("0/2");

	// 补两腿后第三选被 2串1 上限拦下（到不了提交）
	await user.click(screen.getByTestId("market-pick-1-h"));
	await user.click(screen.getByTestId("market-pick-4-a"));
	expect(screen.getByTestId("basket-count")).toHaveTextContent("2/2");
	await user.click(screen.getByTestId("market-pick-2-a"));
	expect(screen.getByTestId("market-message")).toHaveTextContent("已达 2串1 上限");
});

test("basket drawer manages legs, non-single hint, and surfaces server rejection", async () => {
	const user = userEvent.setup();
	const captured: { body: unknown } = { body: null };
	mockCreatedBet(89, captured);
	await renderAt("/markets/had");

	// 非单固场作唯一腿：抽屉规则说明切换（服务器最终裁决）
	await screen.findByTestId("market-card-2");
	await user.click(screen.getByTestId("market-pick-2-a"));
	expect(screen.getByTestId("market-message")).toHaveTextContent("非单固");
	await user.click(screen.getByTestId("basket-open"));
	expect(screen.getByText(/当前腿非单固/)).toBeInTheDocument();

	// 篮内移除 → 空篮说明；补两腿后改模式/策略版本提交 2串1
	await user.click(screen.getByRole("button", { name: "移除 周六002" }));
	expect(screen.getByText(/未选择/)).toBeInTheDocument();
	await user.click(screen.getByTestId("market-pick-1-h"));
	await user.click(screen.getByTestId("market-pick-4-a"));
	await user.click(screen.getByTestId("basket-open"));
	await user.clear(screen.getByTestId("basket-stake"));
	await user.type(screen.getByTestId("basket-stake"), "50");
	await user.type(screen.getByTestId("basket-strategy"), "combo-v1");
	await user.selectOptions(screen.getByTestId("basket-mode"), "live");
	// 真金 + 串关：联合口径（整注一个 Kelly）——两腿联合 EV = 1.053×0.924−1 ≈ −2.7% ≤ 0，
	// 建议诚实 ¥0（EV≤0 不给注额）；单腿 ¼Kelly 的正路径见 stake-advice 组件测试
	expect(screen.getByTestId("basket-stake-advice")).toHaveTextContent("串关注额=单关口径");
	await waitFor(() => expect(screen.getByTestId("basket-stake-advice-reason")).toHaveTextContent("建议不投（¥0）"));
	await user.click(screen.getByTestId("basket-submit"));

	expect(await screen.findByTestId("market-message")).toHaveTextContent("已建建议 #89（2串1）");
	expect(captured.body).toMatchObject({
		mode: "live",
		stake: 50,
		strategy_version: "combo-v1",
		legs: [
			{ fixture_id: 1, market_code: "had", selection_code: "h", locked_odds: 1.92 },
			{ fixture_id: 4, market_code: "had", selection_code: "a", locked_odds: 2.8 },
		],
	});

	// 服务器拒绝 = 唯一权威：400 detail 透出为失败消息
	await user.click(screen.getByTestId("market-pick-1-h"));
	await user.click(screen.getByTestId("basket-open"));
	server.use(
		http.post("*/api/v1/bets", () => HttpResponse.json({ detail: "fixture 1 不可投: sale_stopped" }, { status: 400 })),
	);
	await user.click(screen.getByTestId("basket-submit"));
	expect(await screen.findByTestId("market-message")).toHaveTextContent("建注失败");
	expect(screen.getByTestId("market-message")).toHaveTextContent("sale_stopped");
	await user.click(screen.getByText("继续浏览"));
	await waitFor(() => expect(screen.queryByTestId("basket-stake")).not.toBeInTheDocument());
});

test("shows the honest empty combo when no positive EV exists (EV≤0 excluded)", async () => {
	server.use(
		http.get("*/api/v1/fixtures/today", () =>
			HttpResponse.json(
				todayFixture.map((row) =>
					row.fixture_id === 1 ? { ...row, ev: { h: -0.053, d: -0.12, a: -0.175 }, flags: [] } : row,
				),
			),
		),
	);
	await renderAt("/markets/had");

	const combo = await screen.findByTestId("market-combo");
	expect(within(combo).getByTestId("market-combo-empty")).toHaveTextContent("无正 EV 机会");
	expect(within(combo).queryByTestId("market-combo-apply")).not.toBeInTheDocument();
	// 推荐流照常渲染（全部场次沉底按开赛时间），不因组合空窗塌掉
	expect(within(screen.getByTestId("market-feed")).getAllByTestId(/^market-card-/)).toHaveLength(4);
});

test("shows the loading skeleton before data arrives", async () => {
	server.use(http.get("*/api/v1/fixtures/today", () => new Promise<Response>(() => {})));
	await renderAt("/markets/had");
	expect(await screen.findByTestId("market-loading")).toBeInTheDocument();
	expect(screen.queryByTestId("empty-state")).not.toBeInTheDocument();
});

test("shows the unified no-data state when nothing is on sale, and refresh recovers", async () => {
	const user = userEvent.setup();
	server.use(http.get("*/api/v1/fixtures/today", () => HttpResponse.json([])));
	await renderAt("/markets/had");
	const empty = await screen.findByTestId("empty-state");
	expect(empty).toHaveAttribute("data-variant", "no-data");
	expect(empty).toHaveTextContent("3 天内无在售场次");

	server.use(http.get("*/api/v1/fixtures/today", () => HttpResponse.json(todayFixture)));
	await user.click(within(empty).getByRole("button", { name: "刷新" }));
	expect(await screen.findAllByTestId(/^market-card-/)).toHaveLength(4);
});

test("shows the backend-unavailable state with startup guidance, and retry recovers", async () => {
	const user = userEvent.setup();
	server.use(http.get("*/api/v1/fixtures/today", () => HttpResponse.json(todayFixture, { status: 503 })));
	await renderAt("/markets/had");
	const unavailable = await screen.findByTestId("empty-state");
	expect(unavailable).toHaveAttribute("data-variant", "backend-unavailable");
	expect(unavailable).toHaveTextContent("task server");

	server.use(http.get("*/api/v1/fixtures/today", () => HttpResponse.json(todayFixture)));
	await user.click(within(unavailable).getByRole("button", { name: "重试" }));
	expect(await screen.findAllByTestId(/^market-card-/)).toHaveLength(4);
});

test("bankroll 未入金：组合按最低注 ¥2 建议并给出说明", async () => {
	server.use(http.get("*/api/v1/bankroll", () => HttpResponse.json({ ...bankrollFixture, balance: 0, events: [] })));
	await renderAt("/markets/had");

	const combo = await screen.findByTestId("market-combo");
	expect(await within(combo).findByTestId("market-combo-pick-1")).toHaveTextContent("¥2.00");
	expect(within(combo).getByTestId("market-combo-bankroll-warning")).toHaveTextContent("未入金");
});

test("bankroll 读取失败：组合注额建议诚实缺席（不按未入金冒充）", async () => {
	server.use(http.get("*/api/v1/bankroll", () => HttpResponse.json({ detail: "down" }, { status: 503 })));
	await renderAt("/markets/had");

	const combo = await screen.findByTestId("market-combo");
	expect(within(combo).getByTestId("market-combo-pending")).toHaveTextContent("资金池读取失败");
	expect(within(combo).queryByTestId("market-combo-apply")).not.toBeInTheDocument();
	// 推荐流不依赖 bankroll：照常渲染
	expect(within(screen.getByTestId("market-feed")).getAllByTestId(/^market-card-/)).toHaveLength(4);
});
