import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { expect, test } from "vitest";
import { bankrollFixture, goalsMarketFixture } from "../mocks/handlers";
import { server } from "../mocks/server";
import { router } from "../router";

/**
 * 票 wb-05 进球玩法页测试：口径行（矩阵推导/模型×竞彩价/单关为主三处词典标注）、
 * 组合头部卡（引擎产出/合计/约束说明）、一键带入（每注独立单关提交）、
 * 推荐流（玩法切换/停售禁用/选注规则）、三态空状态与组合空窗诚实占位。
 * 引擎约束与档位边界的穷举见 lib/combo-engine.test.ts。
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

/** 捕获建注请求体序列（进球类每腿独立一注）。 */
function mockCreatedBets(captured: { bodies: unknown[] }) {
	server.use(
		http.post("*/api/v1/bets", async ({ request }) => {
			captured.bodies.push(await request.json());
			return HttpResponse.json(
				{
					id: 90 + captured.bodies.length,
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
							fixture_id: 2,
							market_code: "ttg",
							selection_code: "2",
							locked_odds: 4.5,
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

/** 默认 mock 下唯一正 EV：fixture 2 总进球 2 @4.50 → 模型 EV≈+10.3%。 */
const F2_STAKE = 100.08; // bankroll 5004.2 × 2%

test("renders caliber line, combo card from the engine, and the feed in engine order", async () => {
	await renderAt("/markets/goals");

	expect(await screen.findByText("GoalX · 进球")).toBeInTheDocument();

	// 口径行：三处词典标注（矩阵推导 / 模型×竞彩价 / 单关为主）
	const caliber = screen.getByTestId("goals-caliber");
	expect(caliber).toHaveTextContent("比分矩阵推导");
	expect(caliber).toHaveTextContent("模型概率 × 竞彩价");
	expect(caliber).toHaveTextContent("单关为主");
	// 票 04(b) 研究入口身份三件：研究样本/结算归属/EV 依赖
	const note = screen.getByTestId("goals-research-note");
	expect(note).toHaveTextContent("研究样本");
	expect(note).toHaveTextContent("manual_research");
	expect(note).toHaveTextContent("不计入正式纸面验证分母");
	expect(note).toHaveTextContent("自动结算");
	expect(note).toHaveTextContent("模型概率 × 竞彩价");

	// 玩法页顶部三入口 Tab：当前页 aria-current（进球点亮）
	const tabs = screen.getByTestId("market-tabs");
	expect(within(tabs).getByRole("link", { name: "进球" })).toHaveAttribute("aria-current", "page");
	expect(within(tabs).getByRole("link", { name: "胜平负" })).not.toHaveAttribute("aria-current");
	expect(within(tabs).getByRole("link", { name: "14场任9" })).not.toHaveAttribute("aria-current");

	// 组合卡：1 注、模型概率与 EV、模型×竞彩价口径、flat 注额
	const combo = await screen.findByTestId("goals-combo");
	expect(within(combo).getByTestId("goals-combo-count")).toHaveTextContent("1");
	const pick = within(combo).getByTestId("goals-combo-pick-2");
	expect(pick).toHaveTextContent("周六002");
	expect(pick).toHaveTextContent("总进球 2");
	expect(pick).toHaveTextContent("@4.50");
	expect(pick).toHaveTextContent("24.5%");
	expect(pick).toHaveTextContent("¥100.08");
	expect(within(combo).getByTestId("goals-combo-total")).toHaveTextContent(`总注额 ¥${F2_STAKE.toFixed(2)}`);
	expect(within(combo).getByTestId("goals-combo-total")).toHaveTextContent("模型×竞彩价口径");
	const notes = within(combo).getByTestId("goals-combo-notes");
	expect(notes).toHaveTextContent("不组串");
	expect(notes).toHaveTextContent("不加权");
	expect(notes).toHaveTextContent("flat");

	// 推荐流：唯一正 EV 场置顶；无模型场按开赛时间沉底（2.5h → 3h → 5h）
	const feed = screen.getByTestId("goals-feed");
	const cards = within(feed).getAllByTestId(/^goals-card-\d+$/);
	expect(cards.map((card) => card.getAttribute("data-testid"))).toEqual([
		"goals-card-2",
		"goals-card-1",
		"goals-card-3",
	]);
	// 无模型场诚实标注（fixture 1）；停售场（3）按钮禁用 + 已停售
	expect(screen.getByTestId("goals-model-note-1")).toHaveTextContent("无模型覆盖");
	expect(screen.getByTestId("goals-pick-3-ttg-2")).toBeDisabled();
	expect(screen.getByTestId("goals-status-3")).toHaveTextContent("已停售");
	expect(screen.getByTestId("goals-pick-1-ttg-2")).toBeEnabled();
});

test("market toggle switches the feed between ttg and crs grids", async () => {
	const user = userEvent.setup();
	await renderAt("/markets/goals");

	await screen.findByTestId("goals-card-1");
	// 默认 ttg：八档网格（7+ 归并尾部）
	expect(screen.getAllByTestId(/^goals-pick-1-ttg-/)).toHaveLength(8);
	expect(screen.getByTestId("goals-pick-1-ttg-7")).toHaveTextContent("7+");

	await user.click(screen.getByTestId("goals-market-tab-crs"));
	expect(screen.getAllByTestId(/^goals-pick-1-crs-/)).toHaveLength(31);
	expect(screen.getByTestId("goals-pick-1-crs-h_other")).toHaveTextContent("胜其他");
	// ttg 网格不再渲染
	expect(screen.queryAllByTestId(/^goals-pick-1-ttg-/)).toHaveLength(0);
});

test("apply combo loads legs into the in-page basket; each leg submits as an independent single", async () => {
	const user = userEvent.setup();
	const captured: { bodies: unknown[] } = { bodies: [] };
	mockCreatedBets(captured);
	await renderAt("/markets/goals");

	await screen.findByTestId("goals-combo-pick-2");
	await user.click(screen.getByTestId("goals-combo-apply"));

	expect(await screen.findByTestId("goals-basket-stake")).toHaveValue(F2_STAKE);
	expect(screen.getByTestId("goals-market-message")).toHaveTextContent("已带入 1 条推荐");
	expect(screen.getByTestId("goals-market-message")).toHaveTextContent("独立单关");
	const drawerLegs = await screen.findAllByTestId("goals-basket-leg");
	expect(drawerLegs).toHaveLength(1);
	expect(drawerLegs[0]).toHaveTextContent("总进球 2");

	await user.click(screen.getByTestId("goals-basket-submit"));
	expect(await screen.findByTestId("goals-market-message")).toHaveTextContent("已建 1 条单关建议（纸面 ¥100.08）");
	expect(captured.bodies).toHaveLength(1);
	expect(captured.bodies[0]).toMatchObject({
		mode: "paper",
		stake: F2_STAKE,
		legs: [{ fixture_id: 2, market_code: "ttg", selection_code: "2", locked_odds: 4.5 }],
	});
	expect(screen.getByTestId("goals-basket-count")).toHaveTextContent("0/3");
});

test("feed picks follow the single-bet basket rules (toggle off, cap, same-fixture coexistence)", async () => {
	const user = userEvent.setup();
	await renderAt("/markets/goals");

	await screen.findByTestId("goals-card-2");
	await user.click(screen.getByTestId("goals-pick-2-ttg-2"));
	expect(screen.getByTestId("goals-basket-count")).toHaveTextContent("1/3");
	expect(screen.getByTestId("goals-basket-summary")).toHaveTextContent("周六002 总进球 2");

	// 独立单关：同场另一选项是另一注（不替换）
	await user.click(screen.getByTestId("goals-pick-2-ttg-3"));
	expect(screen.getByTestId("goals-basket-count")).toHaveTextContent("2/3");
	expect(screen.getByTestId("goals-basket-summary")).toHaveTextContent("总进球 3");

	// 再点同选项 = 取消
	await user.click(screen.getByTestId("goals-pick-2-ttg-3"));
	expect(screen.getByTestId("goals-basket-count")).toHaveTextContent("1/3");

	// 上限 3 注拦下第四选
	await user.click(screen.getByTestId("goals-pick-2-ttg-3"));
	await user.click(screen.getByTestId("goals-pick-1-ttg-0"));
	await user.click(screen.getByTestId("goals-pick-1-ttg-1"));
	expect(screen.getByTestId("goals-basket-count")).toHaveTextContent("3/3");
	await user.click(screen.getByTestId("goals-pick-1-ttg-2"));
	expect(screen.getByTestId("goals-market-message")).toHaveTextContent("已达 3 注上限");
});

test("basket drawer submits multiple singles and surfaces server rejection", async () => {
	const user = userEvent.setup();
	const captured: { bodies: unknown[] } = { bodies: [] };
	mockCreatedBets(captured);
	await renderAt("/markets/goals");

	await screen.findByTestId("goals-card-2");
	await user.click(screen.getByTestId("goals-pick-2-ttg-2"));
	await user.click(screen.getByTestId("goals-basket-open"));
	await user.clear(screen.getByTestId("goals-basket-stake"));
	await user.type(screen.getByTestId("goals-basket-stake"), "20");
	await user.type(screen.getByTestId("goals-basket-strategy"), "goals-v1");
	await user.selectOptions(screen.getByTestId("goals-basket-mode"), "live");
	await user.click(screen.getByTestId("goals-basket-submit"));

	expect(await screen.findByTestId("goals-market-message")).toHaveTextContent("已建 1 条单关建议（纸面 ¥100.08）");
	expect(captured.bodies[0]).toMatchObject({ mode: "live", stake: 20, strategy_version: "goals-v1" });

	// 服务器拒绝 = 唯一权威：400 detail 透出（票 06 逐腿语义：失败腿留篮可重试）
	await user.click(screen.getByTestId("goals-pick-2-ttg-2"));
	await user.click(screen.getByTestId("goals-basket-open"));
	server.use(
		http.post("*/api/v1/bets", () => HttpResponse.json({ detail: "fixture 2 不可投: sale_stopped" }, { status: 400 })),
	);
	await user.click(screen.getByTestId("goals-basket-submit"));
	const rejection = await screen.findByTestId("goals-market-message");
	expect(rejection).toHaveTextContent("第 1 笔（周六002 2）失败：fixture 2 不可投: sale_stopped");
	expect(rejection).toHaveTextContent("成功 0 · 失败 1 · 未提交 0，失败/未提交腿留篮可重试");
	// 失败腿留篮（草稿不丢）
	expect(screen.getByTestId("goals-basket-count")).toHaveTextContent("1/3");
	await user.click(screen.getByText("继续浏览"));
	await waitFor(() => expect(screen.queryByTestId("goals-basket-stake")).not.toBeInTheDocument());
});

test("票 09：意图冲突恢复原结果，该腿换新键留篮可另建", async () => {
	const user = userEvent.setup();
	await renderAt("/markets/goals");
	await screen.findByTestId("goals-card-2");
	await user.click(screen.getByTestId("goals-pick-2-ttg-2"));
	await user.click(screen.getByTestId("goals-basket-open"));

	const keys: string[] = [];
	let conflictOnce = true;
	server.use(
		http.post("*/api/v1/bets", async ({ request }) => {
			const body = (await request.json()) as { client_request_id?: string };
			if (body.client_request_id !== undefined) {
				keys.push(body.client_request_id);
			}
			if (conflictOnce) {
				conflictOnce = false;
				return HttpResponse.json(
					{
						detail: {
							message: "client_request_id 已绑定另一提交意图",
							existing_bet: { id: 80, mode: "paper", stake: 2 },
						},
					},
					{ status: 409 },
				);
			}
			return HttpResponse.json(
				{
					id: 81,
					slip_id: null,
					mode: "live",
					market_kind: "fixed",
					purchased: false,
					stake: 100.08,
					actual_stake: null,
					strategy_version: null,
					placed_at: null,
					locked_at: null,
					created_at: "2026-11-04T10:00:00+00:00",
					status: "open",
					payout: null,
					profit: null,
					settled_at: null,
					legs: [
						{
							fixture_id: 2,
							market_code: "ttg",
							selection_code: "2",
							locked_odds: 4.5,
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
	await user.click(screen.getByTestId("goals-basket-submit"));
	const message = await screen.findByTestId("goals-market-message");
	expect(message).toHaveTextContent("第 1 笔（周六002 2）原提交其实已成功：#80（纸面 ¥2）——已恢复原结果");
	expect(message).toHaveTextContent("本次新条款未建立");
	expect(screen.getByTestId("goals-basket-count")).toHaveTextContent("1/3"); // 腿留篮（换新键）

	// 再点提交：换新键 → 按新意图另建（消息展示服务器口径）
	await user.click(screen.getByTestId("goals-basket-submit"));
	expect(await screen.findByTestId("goals-market-message")).toHaveTextContent("已建 1 条单关建议（真金 ¥100.08）");
	expect(screen.getByTestId("goals-basket-count")).toHaveTextContent("0/3");
	expect(keys).toHaveLength(2);
	expect(keys[1]).not.toBe(keys[0]); // 冲突确认后才换键
});

test("shows the honest empty combo when no positive EV exists", async () => {
	server.use(
		http.get("*/api/v1/markets/goals", () =>
			HttpResponse.json(
				goalsMarketFixture.map((row) =>
					row.fixture_id === 2
						? {
								...row,
								ttg: {
									...row.ttg,
									selections: row.ttg.selections.map((sel) => (sel.code === "2" ? { ...sel, ev: -0.05 } : sel)),
								},
							}
						: row,
				),
			),
		),
	);
	await renderAt("/markets/goals");

	const combo = await screen.findByTestId("goals-combo");
	expect(within(combo).getByTestId("goals-combo-empty")).toHaveTextContent("无正 EV 选项");
	expect(within(combo).queryByTestId("goals-combo-apply")).not.toBeInTheDocument();
	expect(within(screen.getByTestId("goals-feed")).getAllByTestId(/^goals-card-\d+$/)).toHaveLength(3);
});

test("shows the loading skeleton, no-data and backend-unavailable states honestly", async () => {
	const user = userEvent.setup();
	server.use(http.get("*/api/v1/markets/goals", () => new Promise<Response>(() => {})));
	const loading = await renderAt("/markets/goals");
	expect(await screen.findByTestId("goals-loading")).toBeInTheDocument();
	expect(screen.queryByTestId("empty-state")).not.toBeInTheDocument();
	loading.unmount();

	server.use(http.get("*/api/v1/markets/goals", () => HttpResponse.json([])));
	const emptyView = await renderAt("/markets/goals");
	const empty = await screen.findByTestId("empty-state");
	expect(empty).toHaveAttribute("data-variant", "no-data");
	emptyView.unmount();

	server.use(http.get("*/api/v1/markets/goals", () => HttpResponse.json(goalsMarketFixture, { status: 503 })));
	await renderAt("/markets/goals");
	const unavailable = await screen.findByTestId("empty-state");
	expect(unavailable).toHaveAttribute("data-variant", "backend-unavailable");
	expect(unavailable).toHaveTextContent("task server");

	server.use(http.get("*/api/v1/markets/goals", () => HttpResponse.json(goalsMarketFixture)));
	await user.click(within(unavailable).getByRole("button", { name: "重试" }));
	expect(await screen.findAllByTestId(/^goals-card-\d+$/)).toHaveLength(3);
});

test("bankroll 未入金：组合按最低注 ¥2 建议并说明", async () => {
	server.use(http.get("*/api/v1/bankroll", () => HttpResponse.json({ ...bankrollFixture, balance: 0, events: [] })));
	await renderAt("/markets/goals");

	const combo = await screen.findByTestId("goals-combo");
	expect(await within(combo).findByTestId("goals-combo-pick-2")).toHaveTextContent("¥2.00");
	expect(within(combo).getByTestId("goals-combo-bankroll-warning")).toHaveTextContent("未入金");
});

test("bankroll 读取失败：组合注额建议诚实缺席，推荐流照常", async () => {
	server.use(http.get("*/api/v1/bankroll", () => HttpResponse.json({ detail: "down" }, { status: 503 })));
	await renderAt("/markets/goals");

	const combo = await screen.findByTestId("goals-combo");
	expect(within(combo).getByTestId("goals-combo-pending")).toHaveTextContent("资金池读取失败");
	expect(within(combo).queryByTestId("goals-combo-apply")).not.toBeInTheDocument();
	expect(within(screen.getByTestId("goals-feed")).getAllByTestId(/^goals-card-\d+$/)).toHaveLength(3);
});

// ---- 票 04：goals 提交飞行中的全局冻结（跨重挂载）----

test("提交飞行中重挂载后编辑被门拦下；settle 后清篮（票 04 验收）", async () => {
	const user = userEvent.setup();
	// 服务器挂起：提交进入飞行，onMutate 已置全局 goalsPending
	let release = () => {};
	const gate = new Promise<void>((resolve) => {
		release = resolve;
	});
	server.use(
		http.post(
			"*/api/v1/bets",
			async () => {
				await gate;
				return HttpResponse.json({ id: 31, mode: "paper", stake: 100, legs: [], status: "open" }, { status: 201 });
			},
			{ once: true },
		),
	);
	await renderAt("/markets/goals");
	await screen.findByTestId("goals-card-2");
	await user.click(screen.getByTestId("goals-pick-2-ttg-2"));
	await user.click(screen.getByTestId("goals-basket-open"));
	await user.click(screen.getByTestId("goals-basket-submit"));

	// 重挂载（新页实例 isPending=false）：再选另一档被 store 冻结门拦下
	cleanup();
	await renderAt("/markets/goals");
	await screen.findByTestId("goals-card-2");
	await user.click(screen.getByTestId("goals-pick-2-ttg-2"));
	expect(screen.getByTestId("goals-basket-count")).toHaveTextContent("1/3"); // 仍只有飞行前那腿

	// 票 01 验收：提交钮接全局门——重开篮后禁用+"提交中"，无跨页重提入口
	await user.click(screen.getByTestId("goals-basket-open"));
	expect(screen.getByTestId("goals-basket-submit")).toBeDisabled();
	expect(screen.getByTestId("goals-basket-submit")).toHaveTextContent("提交中");

	// 服务器返回 201 → 提交自写清篮不受门，settle 解冻
	release();
	await waitFor(() => expect(screen.queryByTestId("goals-basket-count")).toHaveTextContent("0/3"));
	await user.click(screen.getByTestId("goals-pick-2-ttg-2"));
	expect(screen.getByTestId("goals-basket-count")).toHaveTextContent("1/3"); // 解冻后可编辑
});

test("票 08：goals 价变 409 向 had 对齐——清篮+刷新逐场价+提示重选（不再旧价重试死循环）", async () => {
	const user = userEvent.setup();
	mockCreatedBets({ bodies: [] });
	await renderAt("/markets/goals");

	await screen.findByTestId("goals-card-2");
	await user.click(screen.getByTestId("goals-pick-2-ttg-2"));
	await user.click(screen.getByTestId("goals-basket-open"));
	server.use(
		http.post(
			"*/api/v1/bets",
			() =>
				HttpResponse.json(
					{
						detail: {
							message: "报价已变: fixture 2 ttg 2: 4.1（快照 #147）",
							current_quotes: [
								{
									fixture_id: 2,
									selection_code: "2",
									server_odds: 4.1,
									snapshot_id: 147,
									observed_at: "2026-11-07T10:00:00+00:00",
								},
							],
						},
					},
					{ status: 409 },
				),
			{ once: true },
		),
	);
	await user.click(screen.getByTestId("goals-basket-submit"));

	const message = await screen.findByTestId("goals-market-message");
	expect(message).toHaveTextContent("报价已变（2 → 4.10）——已刷新列表");
	expect(message).toHaveTextContent("请按新价重新选注");
	// 篮被清空（旧价腿不留守——重试不再携旧 request_id/旧价）
	expect(screen.getByTestId("goals-basket-count")).toHaveTextContent("0/3");
});
