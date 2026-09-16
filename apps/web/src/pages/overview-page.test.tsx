import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterEach, expect, test, vi } from "vitest";
import type { Bet, DrawResultView, TodayFixture } from "../api/goalx";
import { server } from "../mocks/server";
import { router } from "../router";

/**
 * 票 15 总览页测试：待办四规则（未锁定建议紧迫倒计时/待录赛果/可结算/数据异常）
 * 各一例 + 整卡"今日无事" + 快照四卡（真金/纸面分区隔离、判读方向）+ 未入金/无纸面
 * 引导态 + 后端不可用降级。数据全部由现有端点前端拼装（票 10 定稿）。
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

afterEach(() => {
	vi.useRealTimers();
});

function minutesAgoIso(minutes: number): string {
	return new Date(Date.now() - minutes * 60_000).toISOString();
}

function makeFixture(overrides: Partial<TodayFixture> & { fixture_id: number }): TodayFixture {
	return {
		match_code: "周日001",
		competition: "英超",
		tier: "tier2",
		home_team: "主队",
		away_team: "客队",
		kickoff_utc: new Date(Date.now() + 6 * 3_600_000).toISOString(),
		is_single: true,
		joined: true,
		jc_odds: { h: 2.0, d: 3.2, a: 3.4 },
		jc_updated_at: minutesAgoIso(10),
		books: 8,
		eu_prob: { h: 0.4, d: 0.3, a: 0.3 },
		ev: { h: 0.01, d: -0.02, a: -0.03 },
		flags: [],
		had_quote: {
			as_of: new Date().toISOString(),
			status: "valid",
			reasons: [],
			sale_state: "on_sale",
			single_eligible: true,
			jc_source_updated_at: minutesAgoIso(10),
			eu_books: 8,
		},
		...overrides,
	};
}

function makeBet(overrides: Partial<Bet> & { id: number }): Bet {
	return {
		slip_id: null,
		mode: "paper",
		market_kind: "fixed",
		purchased: false,
		stake: 10,
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
			{ fixture_id: 1, market_code: "had", selection_code: "h", locked_odds: 2.0, actual_odds: null, goal_line: null },
		],
		review: null,
		...overrides,
	};
}

function makeResult(fixtureId: number): DrawResultView {
	return {
		fixture_id: fixtureId,
		home_goals: 1,
		away_goals: 0,
		half_home_goals: null,
		half_away_goals: null,
		void: false,
		void_reason: null,
		source: "manual",
		published_at: null,
	};
}

function mockAll({
	fixtures,
	bets,
	bankroll,
	results,
}: {
	fixtures: TodayFixture[];
	bets: Bet[];
	bankroll?: { balance: number | null; events: unknown[] };
	results?: DrawResultView[];
}) {
	server.use(
		http.get("*/api/v1/fixtures/today", () => HttpResponse.json(fixtures)),
		http.get("*/api/v1/bets", () => HttpResponse.json(bets)),
		http.get("*/api/v1/bankroll", () => HttpResponse.json(bankroll ?? { balance: null, events: [] })),
		http.get("*/api/v1/draw-results", () => HttpResponse.json(results ?? [])),
	);
}

test("default snapshot: todo rules ①③ fire, four cards render, validation line shows 0/3", async () => {
	await renderAt("/");

	// 默认 mock：建议 #1（腿→fixture 1，开赛 +2.5h）与 #3（腿→fixture 2，开赛 +1.5h）
	const todos = await screen.findByTestId("overview-todos");
	const unlocked = within(todos).getByTestId("todo-unlocked");
	expect(unlocked).toHaveTextContent("未锁定建议 2 条");
	// 最早开赛 = fixture 2（+1.5h <2h）→ 琥珀紧迫倒计时；动作去今日
	expect(within(unlocked).getByText(/最早开赛 \d+ 分钟后/)).toHaveClass("text-warning");
	expect(within(unlocked).getByRole("link", { name: /去今日/ })).toHaveAttribute("href", "/today");

	// 规则③：建议 #1（未结）的腿 fixture 1 已有开奖 → 可结算 1 注；#3 腿 fixture 2 无开奖不算
	const settleable = within(todos).getByTestId("todo-settleable");
	expect(settleable).toHaveTextContent("可结算 1 注");
	expect(settleable).toHaveTextContent("批跑可落定");
	expect(within(settleable).getByRole("link", { name: /去投注页/ })).toHaveAttribute("href", "/bets");

	// 规则②：无已开赛挂注场次 → 不触发；规则④：竞彩/欧赔快照齐全 → 不触发
	expect(screen.queryByTestId("todo-pending-results")).not.toBeInTheDocument();
	expect(screen.queryByTestId("todo-data-anomaly")).not.toBeInTheDocument();

	// 真金区：余额 + 今日真金盈亏（默认流水非今日 → 占位）+ ROI（已结算 live #4：+10.8/12）
	const liveZone = screen.getByTestId("overview-live-zone");
	expect(within(liveZone).getByTestId("card-balance")).toHaveTextContent("¥5004.20");
	expect(within(liveZone).getByTestId("live-pnl-today")).toHaveTextContent("—");
	const roi = within(liveZone).getByTestId("card-roi");
	expect(roi).toHaveTextContent("+90.0%"); // 10.8 / 12
	expect(within(roi).getByText("+90.0%")).toHaveClass("text-profit");
	expect(roi).toHaveTextContent("未结 0 注不计");

	// 机会卡：fixture 1、2 可投（3 已停售）；验证进度三条件全未达成 → 0/3
	expect(screen.getByTestId("card-pickable")).toHaveTextContent("2");
	expect(screen.getByTestId("overview-validation")).toHaveTextContent("0/3");

	// 票 05 红线：纸面独立虚线区带徽章，真金区内无纸面卡
	const paperZone = screen.getByTestId("overview-paper-zone");
	expect(within(paperZone).getByTestId("card-paper-pnl")).toBeInTheDocument();
	expect(within(paperZone).getByText("纸面")).toBeInTheDocument();
	expect(within(liveZone).queryByText("纸面")).not.toBeInTheDocument();
	expect(within(liveZone).queryByTestId("card-paper-pnl")).not.toBeInTheDocument();
	// 默认无今日结算纸面注 → 占位 + 说明
	expect(within(paperZone).getByTestId("card-paper-pnl")).toHaveTextContent("今日无已结算纸面注");
});

test("rule ②: kicked-off fixture with an open bet and no result asks for result entry", async () => {
	mockAll({
		fixtures: [makeFixture({ fixture_id: 90, kickoff_utc: new Date(Date.now() - 3_600_000).toISOString() })],
		bets: [
			makeBet({
				id: 21,
				purchased: true,
				status: "open",
				legs: [
					{
						fixture_id: 90,
						market_code: "had",
						selection_code: "h",
						locked_odds: 2.0,
						actual_odds: null,
						goal_line: null,
					},
				],
			}),
		],
		results: [],
	});
	await renderAt("/");

	const pending = await screen.findByTestId("todo-pending-results");
	expect(pending).toHaveTextContent("待录赛果 1 场");
	expect(pending).toHaveTextContent("已开赛、挂未结注、无开奖");
	expect(within(pending).getByRole("link", { name: /去投注页录入/ })).toHaveAttribute("href", "/bets");
	// 无建议/可结算/数据异常
	expect(screen.queryByTestId("todo-unlocked")).not.toBeInTheDocument();
	expect(screen.queryByTestId("todo-settleable")).not.toBeInTheDocument();
	expect(screen.queryByTestId("todo-data-anomaly")).not.toBeInTheDocument();
});

test("rule ③: open bet with all leg results present is settleable; far kickoff is not urgent", async () => {
	mockAll({
		fixtures: [makeFixture({ fixture_id: 3 })],
		bets: [
			makeBet({
				id: 22,
				purchased: true,
				status: "open",
				legs: [
					{
						fixture_id: 91,
						market_code: "had",
						selection_code: "h",
						locked_odds: 2.0,
						actual_odds: null,
						goal_line: null,
					},
					{
						fixture_id: 92,
						market_code: "had",
						selection_code: "a",
						locked_odds: 1.8,
						actual_odds: null,
						goal_line: null,
					},
				],
			}),
			makeBet({
				id: 23,
				legs: [
					{
						fixture_id: 3,
						market_code: "had",
						selection_code: "h",
						locked_odds: 2.0,
						actual_odds: null,
						goal_line: null,
					},
				],
			}),
		],
		results: [makeResult(91), makeResult(92)],
	});
	await renderAt("/");

	const settleable = await screen.findByTestId("todo-settleable");
	expect(settleable).toHaveTextContent("可结算 1 注");
	// 规则①非紧迫分支：建议腿 fixture 3 开赛 +6h → 中性小时倒计时
	const unlocked = screen.getByTestId("todo-unlocked");
	expect(unlocked).toHaveTextContent("未锁定建议 1 条");
	expect(within(unlocked).getByText(/最早开赛 \d+ 小时后/)).toHaveClass("text-muted-foreground");
	// 腿 91/92 不在今日列表 → 规则②不下判
	expect(screen.queryByTestId("todo-pending-results")).not.toBeInTheDocument();
});

test("rule ④: after 10:00 with the whole snapshot missing, the item appears with ingest guidance", async () => {
	vi.useFakeTimers({ toFake: ["Date"] });
	vi.setSystemTime(new Date(2026, 8, 15, 11, 5, 0)); // 本地 11:05，已过 10 点
	server.use(
		http.get("*/api/v1/fixtures/today", () =>
			HttpResponse.json([
				makeFixture({ fixture_id: 91, jc_updated_at: null, joined: false, books: 0, eu_prob: null, ev: null }),
			]),
		),
	);
	await renderAt("/");

	const anomaly = await screen.findByTestId("todo-data-anomaly");
	expect(anomaly).toHaveTextContent("数据异常");
	expect(anomaly).toHaveTextContent("当日竞彩快照缺");
	expect(anomaly).toHaveTextContent("欧赔快照缺（0/1 接入）");
	expect(anomaly).toHaveTextContent("task ingest-jingcai");
	// 就地指引：无跳转动作
	expect(within(anomaly).queryByRole("link")).not.toBeInTheDocument();
});

test("no todos and no paper records: the card says 今日无事 and guides the first suggestion", async () => {
	mockAll({ fixtures: [makeFixture({ fixture_id: 1 })], bets: [] });
	await renderAt("/");

	expect(await screen.findByTestId("overview-no-todos")).toHaveTextContent("今日无事");
	expect(screen.queryByTestId("todo-unlocked")).not.toBeInTheDocument();
	// 票 05：无纸面记录 → 引导从今日页建第一笔建议
	const paperZone = screen.getByTestId("overview-paper-zone");
	const guide = within(paperZone).getByTestId("paper-guide");
	expect(guide).toHaveTextContent("还没有纸面记录");
	expect(within(guide).getByRole("link", { name: "从今日页建第一笔建议" })).toHaveAttribute("href", "/today");
});

test("no deposits yet: the live zone shows the deposit onboarding empty state", async () => {
	server.use(http.get("*/api/v1/bankroll", () => HttpResponse.json({ balance: null, events: [] })));
	await renderAt("/");

	const liveZone = await screen.findByTestId("overview-live-zone");
	const state = within(liveZone).getByTestId("empty-state");
	expect(state).toHaveAttribute("data-variant", "not-available");
	expect(state).toHaveTextContent("尚未入金");
	expect(state).toHaveTextContent("到资金页记录第一笔入金");
	expect(within(state).getByRole("link", { name: "去资金页" })).toHaveAttribute("href", "/bankroll");
	expect(within(liveZone).queryByTestId("card-balance")).not.toBeInTheDocument();
});

test("today's live flows color the pnl red/green; paper zone stays isolated with its badge", async () => {
	const todayIso = new Date().toISOString();
	mockAll({
		fixtures: [makeFixture({ fixture_id: 1 })],
		bets: [
			makeBet({
				id: 31,
				mode: "paper",
				purchased: true,
				status: "won",
				profit: 26.6,
				settled_at: todayIso,
			}),
			makeBet({
				id: 32,
				mode: "live",
				purchased: true,
				status: "lost",
				stake: 10,
				actual_stake: 12,
				profit: -12,
				settled_at: todayIso,
			}),
			makeBet({ id: 33, mode: "live", purchased: true, status: "open" }),
		],
		bankroll: {
			balance: 1160,
			events: [
				{
					id: 2,
					occurred_at: minutesAgoIso(30),
					kind: "bet_stake",
					amount_cny: -100,
					balance_after: 900,
					bet_id: 32,
					note: null,
				},
				{
					id: 3,
					occurred_at: minutesAgoIso(5),
					kind: "bet_payout",
					amount_cny: 0,
					balance_after: 900,
					bet_id: 32,
					note: "settlement",
				},
				{
					id: 1,
					occurred_at: "2026-09-01T00:00:00+00:00",
					kind: "deposit",
					amount_cny: 1000,
					balance_after: 1000,
					bet_id: null,
					note: "初始资金",
				},
			],
		},
	});
	await renderAt("/");

	// 今日真金盈亏 = 当日投注流水净额（-100 + 0 = -100；入金 1000 不计入）→ 绿跌
	const pnl = await screen.findByTestId("live-pnl-today");
	expect(pnl).toHaveTextContent("-¥100.00");
	expect(pnl).toHaveClass("text-loss");
	// ROI 仅已结算 live：-12/12 = -100%，未结 1 注不计
	const roi = screen.getByTestId("card-roi");
	expect(roi).toHaveTextContent("-100.0%");
	expect(within(roi).getByText("-100.0%")).toHaveClass("text-loss");
	expect(roi).toHaveTextContent("未结 1 注不计");
	// 今日纸面盈亏：+26.60 红涨；徽章在纸面区
	const paperZone = screen.getByTestId("overview-paper-zone");
	const paperCard = within(paperZone).getByTestId("card-paper-pnl");
	expect(paperCard).toHaveTextContent("+¥26.60");
	expect(within(paperCard).getByText("+¥26.60")).toHaveClass("text-profit");
	expect(within(paperZone).getByText("纸面")).toBeInTheDocument();
});

test("partial failures degrade honestly inside the todo card", async () => {
	server.use(
		http.get("*/api/v1/fixtures/today", () => HttpResponse.json({ detail: "down" }, { status: 503 })),
		http.get("*/api/v1/draw-results", () => HttpResponse.json([], { status: 503 })),
		http.get("*/api/v1/bets", () => HttpResponse.json([])),
	);
	await renderAt("/");

	expect(await screen.findByTestId("overview-no-todos")).toBeInTheDocument();
	expect(screen.getByText("开奖结果加载失败——待录赛果与可结算判定暂缺。")).toBeInTheDocument();
	expect(screen.getByText("今日场次加载失败——紧迫倒计时与数据异常判定暂缺。")).toBeInTheDocument();
});

test("shows the loading skeleton before core data arrives", async () => {
	server.use(
		http.get("*/api/v1/fixtures/today", () => new Promise<Response>(() => {})),
		http.get("*/api/v1/bets", () => new Promise<Response>(() => {})),
		http.get("*/api/v1/bankroll", () => new Promise<Response>(() => {})),
	);
	await renderAt("/");

	expect(await screen.findByTestId("overview-loading")).toBeInTheDocument();
	expect(screen.queryByTestId("overview-todos")).not.toBeInTheDocument();
	expect(screen.queryByTestId("overview-snapshot")).not.toBeInTheDocument();
});

test("shows the backend-unavailable state when every core endpoint fails, and retry recovers", async () => {
	const user = userEvent.setup();
	for (const path of ["/api/v1/fixtures/today", "/api/v1/bets", "/api/v1/bankroll"]) {
		server.use(http.get(`*${path}`, () => HttpResponse.json({ detail: "unavailable" }, { status: 503 })));
	}
	await renderAt("/");

	const state = await screen.findByTestId("empty-state");
	expect(state).toHaveAttribute("data-variant", "backend-unavailable");
	expect(state).toHaveTextContent("task server");

	// 恢复默认 handler 后重试 → 正常总览
	server.resetHandlers();
	await user.click(within(state).getByRole("button", { name: "重试" }));
	await waitFor(() => expect(screen.getByTestId("overview-todos")).toBeInTheDocument());
});
