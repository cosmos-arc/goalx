import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { beforeEach, expect, test, vi } from "vitest";
import { validationProgressFixture } from "../mocks/handlers";
import { server } from "../mocks/server";
import { router } from "../router";
import {
	bestForwardSkill,
	clvMetricRows,
	forwardMetricRows,
	validationVerdict,
	verdictText,
	yieldCurveOption,
} from "./validation-page";

/**
 * 票 19 验证页测试（票 09 定稿口径）：首屏状态结论推导（三条件 x/3，整赛季不计入）/
 * 三条件卡达成·进行中两态（服务端判定为准）/前瞻 yield 主图（双线 + 0 基准 markline，
 * 点数不足不出图）/弱类型指标映射（已知 key 带标签行 + 未知 key 折叠）/回测 vs 前瞻
 * 对比（回测标注不算通过线）/三态空状态与骨架。echarts/core 按票 11 约定在测试文件内 mock。
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

// ---- 纯函数：状态结论推导 ----

test("validationVerdict：整赛季条件独立不计入三条件，unmet 按服务端顺序列出", () => {
	const verdict = validationVerdict(validationProgressFixture.conditions);
	expect(verdict.achieved).toBe(0);
	expect(verdict.total).toBe(3);
	expect(verdict.unmet).toEqual([
		"纸面 CLV beat ≥60% 且 ≥200 唯一注",
		"前瞻对市场 skill ≥ 0 (RPS, ≥30 场)",
		"复核无系统性错误",
	]);
	expect(verdict.fullSeason?.key).toBe("full_season");
	expect(verdictText(verdict)).toBe(
		"纸面转真金三条件 0/3——还差：纸面 CLV beat ≥60% 且 ≥200 唯一注；前瞻对市场 skill ≥ 0 (RPS, ≥30 场)；复核无系统性错误",
	);
});

test("validationVerdict：部分/全部达成与空条件的结论措辞", () => {
	const base = [
		{ key: "clv_beat", label: "CLV", achieved: true, current: "62%", target: "60%" },
		{ key: "market_skill", label: "skill", achieved: false, current: "-1%", target: "0" },
		{ key: "review_errors", label: "复核", achieved: false, current: "未评估", target: "无" },
	] as const;
	const partial = validationVerdict([...base]);
	expect(verdictText(partial)).toBe("纸面转真金三条件 1/3——还差：skill；复核");

	const allAchieved = validationVerdict(base.map((condition) => ({ ...condition, achieved: true })));
	expect(verdictText(allAchieved)).toBe("纸面转真金三条件 3/3——前瞻口径全部达成；整赛季窗口覆盖仍需独立验收。");

	const noSeason = validationVerdict([base[1]]);
	expect(noSeason.fullSeason).toBeNull();
	expect(verdictText(noSeason)).toBe("验证条件 0/1——还差：skill");

	expect(verdictText(validationVerdict([]))).toBe("服务端暂未返回验证条件，无法下结论。");
});

// ---- 纯函数：弱类型指标映射 ----

test("clvMetricRows：已知 key 映射为带标签行，未知 key 进折叠区", () => {
	const { rows, unknownEntries } = clvMetricRows(validationProgressFixture.clv);
	const byId = new Map(rows.map((row) => [row.id, row]));

	const singlePaper = byId.get("singles-paper");
	expect(singlePaper?.value).toBe("56.0%");
	expect(singlePaper?.term).toBe("clv");
	expect(singlePaper?.hint).toContain("n=9");
	const singleLive = byId.get("singles-live");
	expect(singleLive?.value).toBe("—");
	expect(byId.get("parlay2-paper")?.value).toBe("50.0%");
	expect(byId.get("denominator")?.value).toBe("11 注");
	expect(byId.get("denominator")?.hint).toContain("原始 12");
	expect(byId.get("independence")?.value).toContain("两腿独立连乘");
	expect(byId.get("minutes-buckets")).toBeUndefined(); // 空桶不出行
	expect(byId.get("regression")?.value).toContain("斜率 3.20");

	// 基准来源分层（票 40）：各级计数成一行，接词条，note 落 hint
	const basis = byId.get("close-basis");
	expect(basis?.term).toBe("clv-basis");
	expect(basis?.value).toContain("Pinnacle 主锚 5 注 · 6 腿 · 纸面单关 beat 75.0%(n=4)");
	expect(basis?.value).toContain("分层前共识(legacy) 5 注 · 6 腿");
	expect(basis?.value).toContain("串关跨基准(mixed) 1 注");
	expect(basis?.hint).toContain("pinnacle 主锚");

	// 分层字段缺失（旧后端/空报表）→ 不出行不进折叠区
	const legacyOnly = clvMetricRows({
		singles: {
			paper: { n_bets: 0, beat_rate: null, avg_clv: null },
			live: { n_bets: 0, beat_rate: null, avg_clv: null },
		},
	});
	expect(legacyOnly.rows.map((row) => row.id)).not.toContain("close-basis");

	// 默认 fixture 无未知 key；混入未知 key 后进 unknownEntries，已知 key 不进
	const mixed = clvMetricRows({ ...validationProgressFixture.clv, mystery_field: { a: 1 } });
	expect(mixed.unknownEntries).toEqual([["mystery_field", { a: 1 }]]);
	expect(unknownEntries).toEqual([]);
});

test("forwardMetricRows：覆盖四态与分组 skill 成行，bestForwardSkill 与服务端同构跳过样本不足组", () => {
	const { rows } = forwardMetricRows(validationProgressFixture.forward);
	const byId = new Map(rows.map((row) => [row.id, row]));
	expect(byId.get("rule")?.value).toBe("latest_forecast_before_kickoff_v1");
	expect(byId.get("rule")?.term).toBe("forward-inclusion");
	expect(byId.get("coverage")?.value).toContain("scored 5");
	expect(byId.get("coverage")?.value).toContain("无基准 1");
	expect(byId.get("group-dc-demo")?.value).toBe("skill +0.0120 · n=5");
	expect(byId.get("group-dc-demo")?.hint).toContain("样本不足");

	// 服务端判定同构：样本不足组不作为通过依据
	expect(bestForwardSkill(validationProgressFixture.forward)).toEqual({
		skill: null,
		version: null,
		allInsufficient: true,
	});
	const report = {
		groups: {
			weak: { n: 5, skill_rps: 0.5, insufficient_samples: true },
			strong: { n: 40, skill_rps: 0.02, insufficient_samples: false },
			negative: { n: 40, skill_rps: -0.01, insufficient_samples: false },
		},
	};
	expect(bestForwardSkill(report)).toEqual({ skill: 0.02, version: "strong", allInsufficient: false });
});

// ---- 纯函数：yield option ----

test("yieldCurveOption：累计/滚动双线 + 0 基准虚线 markline，滚动缺失传 null 不断点冒充", () => {
	const option = yieldCurveOption(
		[
			{ index: 1, cumulative_yield: 0.02, rolling_yield: 0.03 },
			{ index: 2, cumulative_yield: -0.01, rolling_yield: null },
		],
		"#2563eb",
		"#ea580c",
		"#6b7280",
	);
	const series = option.series as Array<{ name: string; data: Array<number | null>; markLine?: { data: unknown } }>;
	expect(series).toHaveLength(2);
	expect(series[0]?.name).toBe("累计 yield");
	expect(series[0]?.data).toEqual([0.02, -0.01]);
	expect(series[0]?.markLine?.data).toEqual([{ yAxis: 0 }]);
	expect(series[1]?.name).toBe("滚动 100 注");
	expect(series[1]?.data).toEqual([0.03, null]); // 缺失不冒充累计值
	expect((option.xAxis as { data: string[] }).data).toEqual(["#1", "#2"]);
	expect(option.legend).toBeDefined();
});

// ---- 渲染：首屏 ----

test("首屏：状态结论 + 三条件卡（整赛季独立卡）+ yield 主图", async () => {
	await renderAt("/validation");

	const verdict = await screen.findByTestId("validation-verdict");
	expect(verdict).toHaveTextContent("纸面转真金三条件 0/3——还差：");
	// 三条件卡只有 3 张（full_season 独立显示），状态徽章以服务端判定为准
	const cards = screen.getAllByTestId("condition-row");
	expect(cards).toHaveLength(3);
	expect(screen.getAllByTestId("condition-status").map((node) => node.textContent)).toEqual([
		"进行中",
		"进行中",
		"进行中",
	]);
	const seasonCard = screen.getByTestId("full-season-card");
	expect(seasonCard).toHaveTextContent("整赛季纸面样本覆盖");
	expect(seasonCard).toHaveTextContent("独立项 · 不计入三条件");
	// 数据 ≥2 点 → 图表挂载
	expect(screen.getByTestId("yield-curve")).toBeInTheDocument();
	// 首屏刻意不放回测 skill（下钻次级区才有），不放成本摘要
	expect(screen.getByTestId("metric-回测 skill")).toBeInTheDocument();
	expect(screen.queryByTestId("cost-summary")).not.toBeInTheDocument();
});

test("三条件卡：达成态以服务端字段为准呈现", async () => {
	server.use(
		http.get("*/api/v1/validation/progress", () =>
			HttpResponse.json({
				...validationProgressFixture,
				conditions: [
					{
						key: "clv_beat",
						label: "纸面 CLV beat ≥60% 且 ≥200 唯一注",
						achieved: true,
						current: "beat=62.0% @ 210 唯一注",
						target: "≥60% @ ≥200 唯一注",
					},
					validationProgressFixture.conditions[1],
					validationProgressFixture.conditions[2],
					validationProgressFixture.conditions[3],
				],
			}),
		),
	);
	await renderAt("/validation");

	const verdict = await screen.findByTestId("validation-verdict");
	expect(verdict).toHaveTextContent("纸面转真金三条件 1/3——还差：");
	const statuses = screen.getAllByTestId("condition-status").map((node) => node.textContent);
	expect(statuses).toEqual(["达成", "进行中", "进行中"]);
});

// ---- 渲染：yield 主图 ----

test("yield 主图：≥2 点挂图，option 双线带 0 基准，滚动缺失传 null", async () => {
	server.use(
		http.get("*/api/v1/validation/progress", () =>
			HttpResponse.json({
				...validationProgressFixture,
				yield_curve: [
					{ index: 1, cumulative_yield: 0.02, rolling_yield: null },
					{ index: 2, cumulative_yield: -0.01, rolling_yield: null },
				],
			}),
		),
	);
	await renderAt("/validation");

	expect(await screen.findByTestId("yield-curve")).toBeInTheDocument();
	await waitFor(() => {
		expect(chartMock.setOption).toHaveBeenCalled();
	});
	const option = chartMock.setOption.mock.calls.at(-1)?.[0] as {
		series: Array<{ data: Array<number | null>; markLine?: { data: unknown } }>;
	};
	expect(option.series[0]?.data).toEqual([0.02, -0.01]);
	expect(option.series[0]?.markLine?.data).toEqual([{ yAxis: 0 }]);
	expect(option.series[1]?.data).toEqual([null, null]); // 缺失不冒充累计值
});

test("yield 主图：不足 2 点出空态，不挂图表容器", async () => {
	server.use(
		http.get("*/api/v1/validation/progress", () =>
			HttpResponse.json({ ...validationProgressFixture, yield_curve: [{ index: 1, cumulative_yield: 0.02 }] }),
		),
	);
	await renderAt("/validation");

	expect(await screen.findByTestId("yield-empty")).toHaveTextContent("已结算注不足 2，曲线待积累。");
	expect(screen.queryByTestId("yield-curve")).not.toBeInTheDocument();
	expect(initMock).not.toHaveBeenCalled();
});

// ---- 渲染：弱类型映射与下钻层 ----

test("下钻层：弱类型指标映射行带词典 tooltip，未知 key 折叠进其他指标", async () => {
	server.use(
		http.get("*/api/v1/validation/progress", () =>
			HttpResponse.json({
				...validationProgressFixture,
				clv: { ...validationProgressFixture.clv, mystery_field: { a: 1 } },
			}),
		),
	);
	await renderAt("/validation");

	const drilldown = await screen.findByTestId("validation-drilldown");
	const clvRows = within(drilldown).getByTestId("clv-rows");
	expect(within(clvRows).getByText("单关 · 纸面 beat rate")).toBeInTheDocument();
	expect(clvRows).toHaveTextContent("56.0%");
	// 票 18 词典 tooltip 体系接入：CLV/skill/前瞻纳入指标名带 tooltip 触发器
	expect(within(clvRows).getAllByTestId("glossary-term-clv").length).toBeGreaterThan(0);
	const forwardRows = within(drilldown).getByTestId("forward-rows");
	expect(within(forwardRows).getAllByTestId("glossary-term-forward-inclusion").length).toBeGreaterThan(0);
	expect(within(forwardRows).getAllByTestId("glossary-term-skill").length).toBeGreaterThan(0);

	// 未知 key 折叠呈现原始 JSON；已知 key 不进折叠区
	const unknowns = within(drilldown).getAllByTestId("unknown-metrics");
	expect(unknowns.some((node) => node.textContent.includes("mystery_field"))).toBe(true);
	expect(unknowns.every((node) => !node.textContent.includes("singles"))).toBe(true);

	// 样本约束：唯一注分母 + 未购买在途
	const constraints = within(drilldown).getByTestId("sample-constraints");
	expect(constraints).toHaveTextContent("另有 2 条未购买在途建议");
	expect(within(constraints).getByTestId("sample-paper")).toHaveTextContent("11");
});

// ---- 渲染：回测 vs 前瞻对比 ----

test("回测 vs 前瞻对比：回测 skill 标注不算通过线，前瞻口径取达标分组最好值", async () => {
	server.use(
		http.get("*/api/v1/validation/progress", () =>
			HttpResponse.json({
				...validationProgressFixture,
				forward: {
					...validationProgressFixture.forward,
					groups: { "dc-demo-v2": { n: 40, skill_rps: 0.02, insufficient_samples: false } },
				},
			}),
		),
	);
	await renderAt("/validation");

	const backtest = await screen.findByTestId("metric-回测 skill");
	expect(backtest).toHaveTextContent("+0.0010");
	expect(screen.getByTestId("validation-compare-heading")).toHaveTextContent("回测口径不算通过线");
	// 前瞻（通过线口径）取样本足分组的最好 skill
	expect(screen.getByTestId("metric-前瞻 skill（通过线口径）")).toHaveTextContent("+0.0200");
});

test("回测缺失时诚实回退：无 run 显示 —，全样本不足显示 样本不足", async () => {
	server.use(
		http.get("*/api/v1/backtest/runs", () => HttpResponse.json([])),
		http.get("*/api/v1/validation/progress", () =>
			HttpResponse.json({ ...validationProgressFixture, latest_run: null }),
		),
	);
	await renderAt("/validation");

	expect(await screen.findByTestId("metric-回测 skill")).toHaveTextContent("—");
	expect(screen.getByTestId("metric-前瞻 skill（通过线口径）")).toHaveTextContent("样本不足");
	expect(screen.queryByTestId("backtest-run-row")).not.toBeInTheDocument();
});

// ---- 空态 / 降级 / 骨架 ----

test("空态：无回测、无曲线时诚实显示，结论与条件照常推导", async () => {
	server.use(
		http.get("*/api/v1/backtest/runs", () => HttpResponse.json([])),
		http.get("*/api/v1/validation/progress", () =>
			HttpResponse.json({
				conditions: [
					{
						key: "clv_beat",
						label: "纸面 CLV beat ≥60% 且 ≥200 唯一注",
						achieved: false,
						current: "@ 0 唯一注",
						target: "≥60% @ ≥200 唯一注",
					},
					{
						key: "market_skill",
						label: "前瞻对市场 skill ≥ 0 (RPS, ≥30 场)",
						achieved: false,
						current: "无前瞻样本",
						target: "≥ 0",
					},
					{
						key: "review_errors",
						label: "复核无系统性错误",
						achieved: false,
						current: "无复核记录(未评估)",
						target: "无系统性错误",
					},
					{
						key: "full_season",
						label: "整赛季纸面样本覆盖",
						achieved: false,
						current: "0 唯一注 / 无样本(覆盖未验收)",
						target: "一个完整销售赛季的窗口覆盖",
					},
				],
				paper: { bets: 0, unique_bets: 0, legs: 0, fixtures: 0, staked: 0, profit: 0 },
				live: { bets: 0, unique_bets: 0, legs: 0, fixtures: 0, staked: 0, profit: 0 },
				unpurchased_open: 0,
				yield_curve: [],
				yield_curve_mode: "paper",
				clv: { n_records: 2, beat_rate_overall: null },
				forward: {
					rule: "latest_forecast_before_kickoff_v1",
					coverage: { settled_fixtures: 0, no_forecast: 2, post_kickoff_only: 0, no_market_baseline: 0, scored: 0 },
					groups: {},
				},
				latest_run: null,
			}),
		),
	);
	await renderAt("/validation");

	expect(await screen.findByTestId("yield-empty")).toBeInTheDocument();
	expect(screen.queryByTestId("yield-curve")).not.toBeInTheDocument(); // 图表容器不挂载，空态替代
	expect(screen.getAllByTestId("condition-status").map((node) => node.textContent)).toEqual([
		"进行中",
		"进行中",
		"进行中",
	]);
	// 遗留/未知 clv 字段折叠呈现，不裸字典直出
	const unknowns = screen.getAllByTestId("unknown-metrics");
	expect(unknowns.some((node) => node.textContent.includes("n_records"))).toBe(true);
});

test("后端不可用降级为空状态 + 重试恢复", async () => {
	const user = userEvent.setup();
	server.use(
		http.get("*/api/v1/validation/progress", () => HttpResponse.json({ detail: "no" }, { status: 503 })),
		http.get("*/api/v1/backtest/runs", () => HttpResponse.json({ detail: "no" }, { status: 503 })),
	);
	await renderAt("/validation");

	const error = await screen.findByTestId("validation-error");
	expect(error).toHaveTextContent("task server");
	expect(screen.queryByTestId("validation-verdict")).not.toBeInTheDocument();

	server.resetHandlers();
	await user.click(within(error).getByRole("button", { name: "重试" }));
	await waitFor(() => expect(screen.getByTestId("validation-verdict")).toHaveTextContent("0/3"));
});

test("加载中先出骨架屏", async () => {
	server.use(
		http.get("*/api/v1/validation/progress", () => new Promise<Response>(() => {})),
		http.get("*/api/v1/backtest/runs", () => new Promise<Response>(() => {})),
	);
	await renderAt("/validation");

	expect(await screen.findByTestId("validation-loading")).toBeInTheDocument();
	expect(screen.queryByTestId("validation-verdict")).not.toBeInTheDocument();
	expect(screen.queryByTestId("condition-row")).not.toBeInTheDocument();
});
