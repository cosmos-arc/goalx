import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { expect, test } from "vitest";
import { server } from "../mocks/server";
import { router } from "../router";

async function renderValidation() {
	await router.navigate({ to: "/validation" });
	const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
	return render(
		<QueryClientProvider client={queryClient}>
			<RouterProvider router={router} />
		</QueryClientProvider>,
	);
}

test("shows metric cards, condition progress and run table", async () => {
	await renderValidation();

	expect(await screen.findByTestId("backtest-run-row")).toBeInTheDocument();
	expect(screen.getByTestId("metric-回测 RPS 模型")).toHaveTextContent("0.2031");
	expect(screen.getByTestId("metric-回测 skill")).toHaveTextContent("+0.0010");
	expect(screen.getByTestId("metric-前瞻样本")).toHaveTextContent("5 场");
	expect(screen.getByTestId("metric-CLV beat(纸面)")).toHaveTextContent("56.0%");

	const statuses = screen.getAllByTestId("condition-status").map((node) => node.textContent);
	expect(statuses).toEqual(["进行中", "进行中", "进行中", "进行中"]);
	expect(screen.getByTestId("yield-curve")).toBeInTheDocument();
	expect(screen.getByText("m2-smoke")).toBeInTheDocument();
});

test("shows empty states when no backtest and no settled bets", async () => {
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
				clv: {
					singles: {
						paper: { n_bets: 0, beat_rate: null, avg_clv: null },
						live: { n_bets: 0, beat_rate: null, avg_clv: null },
					},
					parlay2: {
						paper: { n_bets: 0, beat_rate: null, avg_clv: null },
						live: { n_bets: 0, beat_rate: null, avg_clv: null },
					},
					independence_assumed: true,
					denominator: {
						settled_bets: 0,
						legs: 0,
						no_close_bets: 0,
						unsupported_bets: 0,
						raw_bets: 0,
						reconciled_bets: 0,
						unique_bets: 0,
						deduped_duplicates: 0,
						fixtures: 0,
					},
					by_minutes_bucket_single: {},
					regression: { n: 0, slope: null, r_squared: null, note: "singles only" },
				},
				forward: {
					rule: "latest_forecast_before_kickoff_v1",
					coverage: { settled_fixtures: 0, no_forecast: 2, post_kickoff_only: 0, no_market_baseline: 0, scored: 0 },
					groups: {},
				},
				latest_run: null,
			}),
		),
	);

	await renderValidation();

	expect(await screen.findByTestId("yield-empty")).toBeInTheDocument();
	expect(screen.queryByTestId("backtest-run-row")).not.toBeInTheDocument();
	expect(screen.queryByTestId("metric-回测 skill")).not.toBeInTheDocument();
	const statuses = screen.getAllByTestId("condition-status").map((node) => node.textContent);
	expect(statuses).toEqual(["进行中", "进行中", "进行中", "进行中"]);
});

test("renders fallbacks for null rolling yield and missing summary fields", async () => {
	server.use(
		http.get("*/api/v1/backtest/runs", () =>
			HttpResponse.json([
				{
					id: 9,
					label: "bare",
					status: "done",
					created_at: "2026-09-13T00:00:00+00:00",
					finished_at: null,
					summary: null,
					overall_metrics: null,
				},
			]),
		),
		http.get("*/api/v1/validation/progress", () =>
			HttpResponse.json({
				conditions: [
					{
						key: "clv_beat",
						label: "CLV beat rate ≥60% 且 ≥200 注",
						achieved: false,
						current: "@ 2 注",
						target: "≥60% @ ≥200 注",
					},
				],
				settled_bets: 2,
				yield_curve: [
					{ index: 1, cumulative_yield: 0.1, rolling_yield: null },
					{ index: 2, cumulative_yield: -0.05, rolling_yield: null },
				],
				clv: {
					n_records: 2,
					beat_rate_overall: null,
					by_minutes_bucket: {},
					by_market_league: {},
					regression: { n: 2, slope: null, r_squared: null },
				},
				latest_run: null,
			}),
		),
	);

	await renderValidation();

	const row = await screen.findByTestId("backtest-run-row");
	expect(row).toHaveTextContent("bare");
	expect(row).toHaveTextContent("—"); // summary 缺字段回退
	expect(screen.getByTestId("yield-curve")).toBeInTheDocument();
	expect(screen.getByTestId("condition-status")).toHaveTextContent("进行中");
});

test("shows a degraded hint when the backend is unreachable", async () => {
	server.use(
		http.get("*/api/v1/validation/progress", () => HttpResponse.json({ detail: "no" }, { status: 503 })),
		http.get("*/api/v1/backtest/runs", () => HttpResponse.json({ detail: "no" }, { status: 503 })),
	);

	await renderValidation();

	expect(await screen.findByTestId("validation-error")).toBeInTheDocument();
});
