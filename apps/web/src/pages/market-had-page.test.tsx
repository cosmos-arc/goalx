import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { beforeEach, expect, test } from "vitest";
import { bankrollFixture, poolListTodayFixture, todayFixture } from "../mocks/handlers";
import { server } from "../mocks/server";
import { router } from "../router";

/**
 * 票 wb-03 胜平负玩法页测试：组合头部卡（引擎产出/注数/预期收益口径/约束说明）、
 * 一键带入选注篮（就地打开 + flat 档注额预填）、推荐流排序（EV×置信、
 * 沉底按开赛时间、非可投卡禁用 + 资格徽章、跨日标签）、组合空窗诚实占位、
 * 三态空状态。引擎约束与档位边界的穷举见 lib/combo-engine.test.ts。
 */

// 票 02：票型记忆走 localStorage——逐测清场，默认回到"首次访问 = 单关合计"
beforeEach(() => {
	localStorage.clear();
});

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

	// 推荐流：唯一正 EV 场置顶；其余按开赛时间（fixture 3 已完赛复盘行
	// → 1.5h → 26h——裁决③后 mock fixture 3 是过去场，可带 ou 复盘 flag）
	const feed = screen.getByTestId("market-feed");
	const cards = within(feed).getAllByTestId(/^market-card-/);
	expect(cards.map((card) => card.getAttribute("data-testid"))).toEqual([
		"market-card-1",
		"market-card-3",
		"market-card-2",
		"market-card-4",
	]);
	// 非可投场（fixture 3 停售且已开赛）：按钮禁用 + 资格徽章说明原因；跨日场（4）带"明天"标签
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

test("two positive-EV picks: slip form explicit (default singles), preview == submitted caliber", async () => {
	const user = userEvent.setup();
	// fixture 4 也给正向 EV（books 5 → 置信 1 → score 0.06 > fixture 1 的 0.053×0.4）
	server.use(
		http.get("*/api/v1/fixtures/today", () =>
			HttpResponse.json(
				todayFixture.map((row) => (row.fixture_id === 4 ? { ...row, ev: { h: 0.06, d: -0.109, a: -0.076 } } : row)),
			),
		),
	);
	const bodies: Array<Record<string, unknown>> = [];
	server.use(
		http.post("*/api/v1/bets", async ({ request }) => {
			bodies.push((await request.json()) as Record<string, unknown>);
			return HttpResponse.json({ id: 90 + bodies.length, legs: [] }, { status: 201 });
		}),
	);
	await renderAt("/markets/had");

	const combo = await screen.findByTestId("market-combo");
	expect(await within(combo).findByTestId("market-combo-pick-4")).toHaveTextContent("主胜");
	expect(within(combo).getByTestId("market-combo-pick-1")).toBeInTheDocument();
	expect(within(combo).getByTestId("market-combo-count")).toHaveTextContent("2");

	// 票 02：两口径预览并列（slipFormPlan 单源）；默认 = 单关合计（引擎本义，人裁决）
	const forms = within(combo).getByTestId("market-combo-forms");
	expect(within(forms).getByTestId("market-combo-form-singles")).toBeChecked();
	expect(within(forms).getByTestId("market-combo-form-plan-singles")).toHaveTextContent(
		`两笔单关：2 注 · 总注 ¥${(F1_STAKE * 2).toFixed(2)} · 预期 +¥11.31`,
	);
	// 2串1 口径：联合 EV = 1.053×1.06−1 ≈ +11.6%，预期 +¥11.63（路线 C）
	expect(within(forms).getByTestId("market-combo-form-plan-parlay")).toHaveTextContent("联合 EV 11.6%");
	expect(within(forms).getByTestId("market-combo-form-plan-parlay")).toHaveTextContent("全部命中才赢");
	expect(within(combo).getByTestId("market-combo-total")).toHaveTextContent("两笔单关");

	// 带入（默认单关）→ 篮内同一票型状态（卡/篮两选择器同源），票级预览同数
	await user.click(within(combo).getByTestId("market-combo-apply"));
	expect(screen.getByTestId("market-message")).toHaveTextContent("已带入 2 条推荐");
	expect(screen.getByTestId("market-message")).toHaveTextContent("按两笔单关提交");
	expect(screen.getByTestId("basket-count")).toHaveTextContent("2/2");
	const basketForms = screen.getByTestId("basket-forms");
	expect(within(basketForms).getByTestId("basket-form-singles")).toBeChecked();
	expect(within(basketForms).getByTestId("basket-form-plan-singles")).toHaveTextContent(
		`总注 ¥${(F1_STAKE * 2).toFixed(2)}`,
	);

	// 提交 = 两笔独立单关（各 F1_STAKE）；预览数 == 落库口径（票级单源）
	await user.click(screen.getByTestId("basket-submit"));
	expect(await screen.findByTestId("market-message")).toHaveTextContent("已建建议 #91、#92（2 笔单关）");
	expect(bodies).toHaveLength(2);
	for (const body of bodies) {
		expect(body).toMatchObject({ mode: "paper", stake: F1_STAKE, legs: [expect.anything()] });
	}
	expect(screen.getByTestId("basket-count")).toHaveTextContent("0/2");
});

test("slip form explicitly switched to parlay: one 2串1 bet at the parlay caliber", async () => {
	const user = userEvent.setup();
	server.use(
		http.get("*/api/v1/fixtures/today", () =>
			HttpResponse.json(
				todayFixture.map((row) => (row.fixture_id === 4 ? { ...row, ev: { h: 0.06, d: -0.109, a: -0.076 } } : row)),
			),
		),
	);
	const captured: { body: unknown } = { body: null };
	mockCreatedBet(93, captured);
	await renderAt("/markets/had");

	const combo = await screen.findByTestId("market-combo");
	await within(combo).findByTestId("market-combo-pick-4");
	// 显式切换 2串1（人裁决：2串1 为显式动作）；头部合计随之换口径
	await user.click(within(combo).getByTestId("market-combo-form-parlay"));
	expect(within(combo).getByTestId("market-combo-total")).toHaveTextContent(`总注额 ¥${F1_STAKE.toFixed(2)}`);
	expect(within(combo).getByTestId("market-combo-total")).toHaveTextContent("联合 EV 口径");

	await user.click(within(combo).getByTestId("market-combo-apply"));
	expect(screen.getByTestId("market-message")).toHaveTextContent("按一注 2串1提交");
	expect(screen.getByTestId("market-message")).toHaveTextContent("联合 EV 11.6%");
	expect(screen.getByTestId("basket-form-parlay")).toBeChecked();

	await user.click(screen.getByTestId("basket-submit"));
	expect(await screen.findByTestId("market-message")).toHaveTextContent("已建建议 #93（2串1）");
	expect(captured.body).toMatchObject({ mode: "paper", stake: F1_STAKE, legs: [expect.anything(), expect.anything()] });
});

test("singles submit partial failure: created bets stay booked, failed leg stays in basket", async () => {
	const user = userEvent.setup();
	server.use(
		http.get("*/api/v1/fixtures/today", () =>
			HttpResponse.json(
				todayFixture.map((row) => (row.fixture_id === 4 ? { ...row, ev: { h: 0.06, d: -0.109, a: -0.076 } } : row)),
			),
		),
	);
	// 第一笔 201、第二笔 400（部分失败路径）
	let calls = 0;
	server.use(
		http.post("*/api/v1/bets", async () => {
			calls += 1;
			if (calls === 1) {
				return HttpResponse.json({ id: 95, legs: [] }, { status: 201 });
			}
			return HttpResponse.json({ detail: "fixture 4 不可投: sale_stopped" }, { status: 400 });
		}),
	);
	await renderAt("/markets/had");
	const combo = await screen.findByTestId("market-combo");
	await within(combo).findByTestId("market-combo-pick-4");
	await user.click(within(combo).getByTestId("market-combo-apply"));
	await user.click(screen.getByTestId("basket-submit"));

	// 第一笔成账可见、第二笔失败留篮（1/2）可重试——不静默丢也不重复建
	expect(await screen.findByTestId("market-message")).toHaveTextContent("第 2 笔失败");
	expect(screen.getByTestId("market-message")).toHaveTextContent("已建 #95");
	expect(screen.getByTestId("basket-count")).toHaveTextContent("1/2");
});

test("singles submit mid-loop price drift clears basket (票 04 裁决① 同流)", async () => {
	const user = userEvent.setup();
	server.use(
		http.get("*/api/v1/fixtures/today", () =>
			HttpResponse.json(
				todayFixture.map((row) => (row.fixture_id === 4 ? { ...row, ev: { h: 0.06, d: -0.109, a: -0.076 } } : row)),
			),
		),
	);
	let calls = 0;
	server.use(
		http.post("*/api/v1/bets", async () => {
			calls += 1;
			if (calls === 1) {
				return HttpResponse.json({ id: 96, legs: [] }, { status: 201 });
			}
			return HttpResponse.json(
				{
					detail: {
						message: "报价已变: fixture 4 h: 3.1（快照 #147）",
						current_quotes: [
							{
								fixture_id: 4,
								selection_code: "h",
								server_odds: 3.1,
								snapshot_id: 147,
								observed_at: "2026-11-03T10:00:00+00:00",
							},
						],
					},
				},
				{ status: 409 },
			);
		}),
	);
	await renderAt("/markets/had");
	const combo = await screen.findByTestId("market-combo");
	await within(combo).findByTestId("market-combo-pick-4");
	await user.click(within(combo).getByTestId("market-combo-apply"));
	await user.click(screen.getByTestId("basket-submit"));

	// 价变 = 清篮按新价重选（不残留旧价腿）；已建成的第一笔在消息与账面可见
	expect(await screen.findByTestId("market-message")).toHaveTextContent("第 2 笔报价已变");
	expect(screen.getByTestId("market-message")).toHaveTextContent("已建 #96");
	expect(screen.getByTestId("market-message")).toHaveTextContent("请按新价重新选注");
	expect(screen.getByTestId("basket-count")).toHaveTextContent("0/2");
});

test("slip form choice persists across visits (记住上次选择)", async () => {
	const user = userEvent.setup();
	server.use(
		http.get("*/api/v1/fixtures/today", () =>
			HttpResponse.json(
				todayFixture.map((row) => (row.fixture_id === 4 ? { ...row, ev: { h: 0.06, d: -0.109, a: -0.076 } } : row)),
			),
		),
	);
	const firstView = await renderAt("/markets/had");
	const combo = await screen.findByTestId("market-combo");
	await within(combo).findByTestId("market-combo-pick-4");
	expect(within(combo).getByTestId("market-combo-form-singles")).toBeChecked();
	await user.click(within(combo).getByTestId("market-combo-form-parlay"));
	expect(localStorage.getItem("goalx:slip-form")).toBe("parlay");

	// 二次访问：默认 = 上次选择（parlay），非首试引擎本义值
	firstView.unmount();
	await renderAt("/markets/had");
	const comboAgain = await screen.findByTestId("market-combo");
	await within(comboAgain).findByTestId("market-combo-pick-4");
	expect(within(comboAgain).getByTestId("market-combo-form-parlay")).toBeChecked();
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

	// 票 08①：腿满时点击已选场的另一方向=换选替换而非上限拦截；
	// 真第三场（未选过）仍被上限拦下
	await user.click(screen.getByTestId("market-pick-1-d"));
	expect(screen.getByTestId("market-message")).toHaveTextContent("同场只能选一腿");
	expect(screen.getByTestId("basket-summary")).toHaveTextContent("周六001 平");
	expect(screen.getByTestId("basket-summary")).toHaveTextContent("周日004 客胜");
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
	// 票 02：双腿默认票型 = 记忆值（清场后 singles）→ 建议仓位按单关注额口径
	expect(screen.getByTestId("basket-stake-advice")).toHaveTextContent("单关注额口径");
	// 显式切 2串1 → 串关联合口径（票 wb-06：整注一个 Kelly，不分腿）
	await user.click(screen.getByTestId("basket-form-parlay"));
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

// --- 票 38：poolList 口径单固进入组合引擎（误记时被"非单固"错误排除）---

test("poolList-derived single fixture enters the combo picks instead of being excluded", async () => {
	server.use(http.get("*/api/v1/fixtures/today", () => HttpResponse.json([poolListTodayFixture])));
	await renderAt("/markets/had");

	// 修正前误记形状（is_single=false + single_eligible=false）会被引擎排除；
	// 修正后（池级 single=1 → single_eligible=true）唯一正 EV 场进入单关推荐
	const combo = await screen.findByTestId("market-combo");
	expect(within(combo).getByTestId("market-combo-count")).toHaveTextContent("1");
	expect(within(combo).getByTestId("market-combo-pick-6")).toHaveTextContent("周四006");
	expect(within(combo).getByTestId("market-combo-pick-6")).toHaveTextContent("主胜");

	// 推荐流卡片：不显示"仅串关"，选注钮可用
	const card = screen.getByTestId("market-card-6");
	expect(within(card).queryByText("仅串关")).not.toBeInTheDocument();
	expect(screen.getByTestId("market-pick-6-h")).toBeEnabled();
});

test("stale line read-only field renders per fixture (票 48)", async () => {
	await renderAt("/markets/had");
	const field = await screen.findByTestId("stale-line-1");
	expect(field).toHaveTextContent("上次调盘 8 分钟 前");
	expect(field).toHaveTextContent("sharp 参考 客胜 +8.1%（主锚）");
	// 只读信号：无交互元素、不参与流程
	expect(field.querySelector("button")).toBeNull();
});
