import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { beforeEach, expect, test, vi } from "vitest";
import type { Bet, TodayFixture } from "../api/goalx";
import { server } from "../mocks/server";
import { router } from "../router";
import { buildCumulativePoints } from "./history-page";

/**
 * 票 17 历史页测试：口径行（07 定稿文案逐字）/两层筛选联动（模式不混算 + 时间范围
 * + Competition/策略版本/结果状态）/五指标聚合计算与 EV·CLV 缺失口径/累计曲线
 * （≥2 点出图 + 0 基准 markline，不足 2 点不出图）/空态清筛选/明细同页展开/
 * 后端不可用降级。echarts/core 按票 11 约定在测试文件内 mock。
 */

const initMock = vi.fn();

vi.mock("echarts/core", () => ({
	init: (...args: unknown[]) => initMock(...args),
	use: vi.fn(),
}));

const chartMock = {
	setOption: vi.fn(),
	dispose: vi.fn(),
	resize: vi.fn(),
};

async function renderAt(path: string) {
	await router.navigate({ to: path });
	const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
	return render(
		<QueryClientProvider client={queryClient}>
			<RouterProvider router={router} />
		</QueryClientProvider>,
	);
}

beforeEach(() => {
	initMock.mockReset();
	initMock.mockReturnValue(chartMock);
	chartMock.setOption.mockClear();
});

function daysAgoIso(days: number): string {
	return new Date(Date.now() - days * 86_400_000).toISOString();
}

function makeTodayFixture(overrides: Partial<TodayFixture> & { fixture_id: number }): TodayFixture {
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
		jc_updated_at: daysAgoIso(0.01),
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
			jc_source_updated_at: daysAgoIso(0.01),
			eu_books: 8,
		},
		...overrides,
	};
}

function makeBet(overrides: Partial<Bet> & { id: number }): Bet {
	return {
		slip_id: 1,
		mode: "paper",
		market_kind: "fixed",
		purchased: true,
		stake: 10,
		actual_stake: null,
		strategy_version: null,
		placed_at: null,
		locked_at: null,
		created_at: "2026-09-10T10:00:00+00:00",
		status: "won",
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

/** 已结算纸面注的便捷工厂：默认胜 +10、1 天前结算、腿挂 fixture 1（英超）。 */
function settledPaper(overrides: Partial<Bet> & { id: number }): Bet {
	return makeBet({ settled_at: daysAgoIso(1), profit: 10, ...overrides });
}

function mockAll({ bets, fixtures }: { bets: Bet[]; fixtures?: TodayFixture[] }) {
	server.use(
		http.get("*/api/v1/bets", () => HttpResponse.json(bets)),
		http.get("*/api/v1/fixtures/today", () => HttpResponse.json(fixtures ?? [])),
	);
}

test("口径行按 07 定稿逐字常显，术语 tooltip 就位，模式默认纸面", async () => {
	await renderAt("/history");

	const caliber = await screen.findByTestId("history-caliber");
	expect(caliber).toHaveTextContent("统计已锁定且已结算的注；前瞻验证口径（含排除规则）见验证页。");
	expect(within(caliber).getByRole("link", { name: "验证页" })).toHaveAttribute("href", "/validation");
	// 票 18："前瞻纳入"由链接占位升级为词典 tooltip 触发器（键盘可聚焦，悬停/聚焦出 Popover）
	const term = within(caliber).getByTestId("glossary-term-forward-inclusion");
	expect(term).toHaveTextContent("前瞻纳入");
	expect(term).toHaveClass("decoration-dashed");
	// 默认 handlers（betsFixture）加载完成后筛选区才渲染
	expect(await screen.findByTestId("history-metrics")).toBeInTheDocument();
	// 模式大标签常显：默认纸面按下，真金未按下
	expect(screen.getByTestId("mode-paper")).toHaveAttribute("aria-pressed", "true");
	expect(screen.getByTestId("mode-live")).toHaveAttribute("aria-pressed", "false");
	// 时间范围默认全部；二层筛选默认折叠
	expect(screen.getByTestId("range-all")).toHaveAttribute("aria-pressed", "true");
	expect(screen.getByTestId("detail-toggle")).toHaveAttribute("aria-expanded", "false");
	expect(screen.queryByTestId("history-detail")).not.toBeInTheDocument();
});

test("五指标聚合随默认筛选联动：live 不入纸面口径，EV/CLV 缺失注明口径", async () => {
	mockAll({
		bets: [
			// 样本外：未购买建议、未结算锁定注 → 不计入
			makeBet({ id: 1, purchased: false, settled_at: null }),
			settledPaper({ id: 2, status: "won", profit: 26.6, stake: 2, settled_at: daysAgoIso(1) }),
			settledPaper({ id: 3, status: "lost", profit: -10, settled_at: daysAgoIso(2) }),
			settledPaper({ id: 4, status: "void", profit: 0, settled_at: daysAgoIso(2) }),
			// 真金已结算：默认纸面口径不混算
			settledPaper({ id: 5, mode: "live", status: "lost", profit: -12, actual_stake: 12, settled_at: daysAgoIso(1) }),
		],
	});
	await renderAt("/history");

	expect(await screen.findByTestId("history-metrics")).toBeInTheDocument();
	// 总盈亏 = +26.6 - 10 + 0 = +16.60（红涨）；live -12 不计
	const pnl = screen.getByTestId("metric-pnl");
	expect(pnl).toHaveTextContent("+¥16.60");
	expect(within(pnl).getByText("+¥16.60")).toHaveClass("text-profit");
	// 注数 = 3（含退款）；命中率 = 胜 1 / 已分胜负 2（退款不计）
	expect(screen.getByTestId("metric-count")).toHaveTextContent("3");
	expect(screen.getByTestId("metric-hitrate")).toHaveTextContent("50.0%");
	expect(screen.getByTestId("metric-hitrate")).toHaveTextContent("退款不计");
	// EV/CLV：BetView 无注级字段 → "—" + 口径注明（不造假数据）
	expect(screen.getByTestId("metric-ev")).toHaveTextContent("—");
	expect(screen.getByTestId("metric-ev")).toHaveTextContent("注级 EV 未随 v1 API 提供");
	expect(screen.getByTestId("metric-clv")).toHaveTextContent("—");
	expect(screen.getByTestId("metric-clv")).toHaveTextContent("正 = 买在好价");
	// 真金盈亏不出现在任何卡片
	expect(screen.queryByText("-¥12.00")).not.toBeInTheDocument();
});

test("累计曲线：≥2 点出图，option 按结算时点升序累计并带 0 基准 markline；不足 2 点不出图", async () => {
	const bets = [
		settledPaper({ id: 1, profit: 10, settled_at: "2026-09-12T10:00:00+00:00" }),
		settledPaper({ id: 2, profit: -4, settled_at: "2026-09-13T10:00:00+00:00" }),
		settledPaper({ id: 3, profit: 2.5, settled_at: "2026-09-11T10:00:00+00:00" }),
	];
	mockAll({ bets });
	await renderAt("/history");

	// 纯函数口径：乱序入参按 settled_at 升序（9-11, 9-12, 9-13）累计 → [2.5, 12.5, 8.5]
	expect(buildCumulativePoints(bets)).toEqual([
		{ label: "09-11 10:00", value: 2.5 },
		{ label: "09-12 10:00", value: 12.5 },
		{ label: "09-13 10:00", value: 8.5 },
	]);

	await screen.findByTestId("history-chart");
	await waitFor(() => {
		expect(chartMock.setOption).toHaveBeenCalled();
	});
	const option = chartMock.setOption.mock.calls.at(-1)?.[0] as {
		series: Array<{ data: number[]; markLine: { data: Array<{ yAxis: number }> } }>;
		xAxis: { data: string[] };
	};
	expect(option.series[0]?.data).toEqual([2.5, 12.5, 8.5]);
	expect(option.series[0]?.markLine.data).toEqual([{ yAxis: 0 }]);
	expect(option.xAxis.data).toEqual(["09-11 10:00", "09-12 10:00", "09-13 10:00"]);
	expect(screen.queryByTestId("history-chart-empty")).not.toBeInTheDocument();
});

test("累计曲线不足 2 点不出图，显示积累提示", async () => {
	mockAll({ bets: [settledPaper({ id: 1, profit: 10, settled_at: daysAgoIso(1) })] });
	await renderAt("/history");

	expect(await screen.findByTestId("history-chart-empty")).toHaveTextContent("已结算注不足 2 注");
	expect(screen.queryByTestId("history-chart")).not.toBeInTheDocument();
	expect(initMock).not.toHaveBeenCalled();
});

test("时间范围预设联动：近 7/30/90 天与全部依次过滤", async () => {
	const user = userEvent.setup();
	mockAll({
		bets: [
			settledPaper({ id: 1, profit: 5, settled_at: daysAgoIso(1) }),
			settledPaper({ id: 2, profit: 100, settled_at: daysAgoIso(40) }),
		],
	});
	await renderAt("/history");

	expect(await screen.findByTestId("metric-count")).toHaveTextContent("2");
	expect(screen.getByTestId("metric-pnl")).toHaveTextContent("+¥105.00");

	await user.click(screen.getByTestId("range-30d"));
	expect(screen.getByTestId("range-30d")).toHaveAttribute("aria-pressed", "true");
	expect(screen.getByTestId("metric-count")).toHaveTextContent("1");
	expect(screen.getByTestId("metric-pnl")).toHaveTextContent("+¥5.00");

	await user.click(screen.getByTestId("range-7d"));
	expect(screen.getByTestId("metric-count")).toHaveTextContent("1");

	await user.click(screen.getByTestId("range-90d"));
	expect(screen.getByTestId("metric-count")).toHaveTextContent("2");

	await user.click(screen.getByTestId("range-all"));
	expect(screen.getByTestId("metric-count")).toHaveTextContent("2");
});

test("模式切换不混算：真金口径只聚合真金已结算注", async () => {
	const user = userEvent.setup();
	mockAll({
		bets: [
			settledPaper({ id: 1, status: "won", profit: 26.6, settled_at: daysAgoIso(1) }),
			settledPaper({ id: 2, mode: "live", status: "lost", profit: -12, actual_stake: 12, settled_at: daysAgoIso(2) }),
		],
	});
	await renderAt("/history");

	expect(await screen.findByTestId("metric-pnl")).toHaveTextContent("+¥26.60");
	await user.click(screen.getByTestId("mode-live"));
	expect(screen.getByTestId("mode-live")).toHaveAttribute("aria-pressed", "true");
	expect(screen.getByTestId("metric-pnl")).toHaveTextContent("-¥12.00");
	expect(within(screen.getByTestId("metric-pnl")).getByText("-¥12.00")).toHaveClass("text-loss");
	expect(screen.getByTestId("metric-count")).toHaveTextContent("1");
});

test("二层筛选：Competition 归因今日列表、策略版本与结果状态组合过滤，清筛选一键还原", async () => {
	const user = userEvent.setup();
	mockAll({
		fixtures: [
			makeTodayFixture({ fixture_id: 1, competition: "英超" }),
			makeTodayFixture({ fixture_id: 2, competition: "德乙" }),
		],
		bets: [
			settledPaper({
				id: 1,
				status: "won",
				profit: 10,
				strategy_version: "dc-demo-v1",
				settled_at: daysAgoIso(1),
				legs: [
					{
						fixture_id: 1,
						market_code: "had",
						selection_code: "h",
						locked_odds: 2.0,
						actual_odds: null,
						goal_line: null,
					},
				],
			}),
			settledPaper({
				id: 2,
				status: "lost",
				profit: -10,
				strategy_version: null,
				settled_at: daysAgoIso(2),
				legs: [
					{
						fixture_id: 2,
						market_code: "had",
						selection_code: "a",
						locked_odds: 1.8,
						actual_odds: null,
						goal_line: null,
					},
				],
			}),
			settledPaper({
				id: 3,
				status: "won",
				profit: 8,
				strategy_version: null,
				settled_at: daysAgoIso(3),
				// 腿场次不在今日列表 → Competition 无法归因，仅"全部"可见
				legs: [
					{
						fixture_id: 99,
						market_code: "had",
						selection_code: "h",
						locked_odds: 2.5,
						actual_odds: null,
						goal_line: null,
					},
				],
			}),
		],
	});
	await renderAt("/history");

	expect(await screen.findByTestId("metric-count")).toHaveTextContent("3");
	await user.click(screen.getByTestId("filters-more").querySelector("summary") as HTMLElement);

	// Competition 过滤：英超 → 仅注 1
	await user.selectOptions(screen.getByTestId("filter-competition"), "英超");
	expect(screen.getByTestId("metric-count")).toHaveTextContent("1");
	await user.selectOptions(screen.getByTestId("filter-competition"), "");

	// 策略版本过滤：未标注 → 注 2、3；dc-demo-v1 → 仅注 1
	await user.selectOptions(screen.getByTestId("filter-strategy"), "none");
	expect(screen.getByTestId("metric-count")).toHaveTextContent("2");
	await user.selectOptions(screen.getByTestId("filter-strategy"), "dc-demo-v1");
	expect(screen.getByTestId("metric-count")).toHaveTextContent("1");
	await user.selectOptions(screen.getByTestId("filter-strategy"), "");

	// 结果状态过滤：负 → 仅注 2
	await user.selectOptions(screen.getByTestId("filter-status"), "lost");
	expect(screen.getByTestId("metric-count")).toHaveTextContent("1");

	// 组合到空 → no-data 空态 + 清筛选动作还原
	await user.selectOptions(screen.getByTestId("filter-status"), "void");
	const state = await screen.findByTestId("empty-state");
	expect(state).toHaveAttribute("data-variant", "no-data");
	expect(state).toHaveTextContent("该筛选下无已结算注");
	await user.click(within(state).getByRole("button", { name: "清筛选" }));
	expect(await screen.findByTestId("metric-count")).toHaveTextContent("3");
	expect(screen.getByTestId("range-all")).toHaveAttribute("aria-pressed", "true");
	expect(screen.getByTestId("filter-competition")).toHaveDisplayValue("全部");
	expect(screen.getByTestId("filter-strategy")).toHaveDisplayValue("全部");
	expect(screen.getByTestId("filter-status")).toHaveDisplayValue("全部");
});

test("查看明细同页展开：五列与投注页已结算行同构，收起即隐藏，不跳页", async () => {
	const user = userEvent.setup();
	mockAll({
		bets: [
			settledPaper({
				id: 7,
				status: "won",
				profit: 26.6,
				stake: 2,
				settled_at: "2026-09-13T12:00:00+00:00",
				legs: [
					{
						fixture_id: 1,
						market_code: "had",
						selection_code: "h",
						locked_odds: 6.5,
						actual_odds: null,
						goal_line: null,
					},
					{
						fixture_id: 2,
						market_code: "had",
						selection_code: "a",
						locked_odds: 2.2,
						actual_odds: null,
						goal_line: null,
					},
				],
			}),
			// 真实回录的注：注金列 = 建议→实际，盈亏列绿跌
			settledPaper({
				id: 8,
				status: "lost",
				profit: -12,
				stake: 10,
				actual_stake: 12,
				settled_at: "2026-09-13T14:00:00+00:00",
				legs: [
					{
						fixture_id: 1,
						market_code: "had",
						selection_code: "a",
						locked_odds: 1.3,
						actual_odds: 1.2,
						goal_line: null,
					},
				],
			}),
		],
	});
	await renderAt("/history");

	await user.click(await screen.findByTestId("detail-toggle"));
	const detail = screen.getByTestId("history-detail");
	expect(detail).toBeInTheDocument();
	expect(screen.getByTestId("detail-toggle")).toHaveAttribute("aria-expanded", "true");
	for (const heading of ["内容", "注金", "状态", "盈亏", "结算时点"]) {
		expect(within(detail).getByRole("columnheader", { name: heading })).toBeInTheDocument();
	}
	const rows = within(detail).getAllByTestId("history-detail-row");
	expect(rows).toHaveLength(2);
	// 倒序：最近结算（#8）在前
	const wonRow = rows[1] as HTMLElement;
	expect(wonRow).toHaveTextContent("#1 主胜@6.50 × #2 客胜@2.20");
	expect(wonRow).toHaveTextContent("¥2.00");
	expect(wonRow).toHaveTextContent("胜");
	expect(wonRow).toHaveTextContent("+26.60");
	expect(wonRow).toHaveTextContent("09-13 12:00");
	const lostRow = rows[0] as HTMLElement;
	expect(lostRow).toHaveTextContent("#1 客胜@1.30→1.20");
	expect(lostRow).toHaveTextContent("¥10.00→¥12.00");
	expect(lostRow).toHaveTextContent("负");
	expect(lostRow).toHaveTextContent("-12.00");
	expect(within(lostRow).getByText("-12.00")).toHaveClass("text-loss");
	expect(lostRow).toHaveTextContent("09-13 14:00");

	await user.click(screen.getByTestId("detail-toggle"));
	expect(screen.queryByTestId("history-detail")).not.toBeInTheDocument();
	expect(screen.getByTestId("detail-toggle")).toHaveAttribute("aria-expanded", "false");
});

test("后端不可用降级为三态空状态，重试后恢复", async () => {
	const user = userEvent.setup();
	server.use(http.get("*/api/v1/bets", () => HttpResponse.json({ detail: "down" }, { status: 503 })));
	await renderAt("/history");

	const state = await screen.findByTestId("empty-state");
	expect(state).toHaveAttribute("data-variant", "backend-unavailable");
	expect(state).toHaveTextContent("task server");

	server.resetHandlers();
	await user.click(within(state).getByRole("button", { name: "重试" }));
	await waitFor(() => expect(screen.getByTestId("history-metrics")).toBeInTheDocument());
});

test("加载中先出骨架屏", async () => {
	server.use(
		http.get("*/api/v1/bets", () => new Promise<Response>(() => {})),
		http.get("*/api/v1/fixtures/today", () => new Promise<Response>(() => {})),
	);
	await renderAt("/history");

	expect(await screen.findByTestId("history-loading")).toBeInTheDocument();
	expect(screen.queryByTestId("history-metrics")).not.toBeInTheDocument();
	expect(screen.queryByTestId("empty-state")).not.toBeInTheDocument();
});
