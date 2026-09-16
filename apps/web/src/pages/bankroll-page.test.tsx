import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { beforeEach, expect, test, vi } from "vitest";
import type { BankrollEvent, DepositInput } from "../api/goalx";
import { server } from "../mocks/server";
import { router } from "../router";
import { amountTone, balanceCurveOption, balancePoints } from "./bankroll-page";

/**
 * 票 20 资金页测试（票 09 定稿口径）：余额大数字 + 近 30 天迷你曲线（点数足才挂图、
 * 纯函数 option 映射）/ 流水 compact 表语义色（投注/兑付与盈亏一致，出入金中性）/
 * 空态入金引导 → 内联表单 → 成功刷新（MSW 有状态双 handler）/ 成本 ¥ 与 credits
 * 分列 + 未记录成本警示。echarts/core 按票 11 约定在测试文件内 mock。
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

async function renderBankroll() {
	await router.navigate({ to: "/bankroll" });
	const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
	return render(
		<QueryClientProvider client={queryClient}>
			<RouterProvider router={router} />
		</QueryClientProvider>,
	);
}

/** 可解析的相对时间 → ISO（窗口过滤测试用）。 */
function daysAgo(days: number): string {
	return new Date(Date.now() - days * 86_400_000).toISOString();
}

beforeEach(() => {
	initMock.mockReset();
	initMock.mockReturnValue(chartMock);
	chartMock.setOption.mockClear();
	chartMock.dispose.mockClear();
	chartMock.resize.mockClear();
});

// ---- 纯函数：余额曲线与语义色 ----

test("balancePoints：只取近 30 天且时间正序，窗口外/不可解析时间剔除", () => {
	const events = [
		{ id: 3, occurred_at: daysAgo(40), kind: "deposit", amount_cny: 1, balance_after: 1, bet_id: null, note: null },
		{
			id: 2,
			occurred_at: daysAgo(2),
			kind: "bet_payout",
			amount_cny: 4.2,
			balance_after: 5004.2,
			bet_id: 7,
			note: null,
		},
		{
			id: 1,
			occurred_at: daysAgo(14),
			kind: "deposit",
			amount_cny: 5000,
			balance_after: 5000,
			bet_id: null,
			note: null,
		},
		{ id: 4, occurred_at: "not-a-date", kind: "cost", amount_cny: 0, balance_after: 0, bet_id: null, note: null },
	] as const;
	const points = balancePoints(events as unknown as BankrollEvent[]);
	expect(points.map((point) => point.balance)).toEqual([5000, 5004.2]);
});

test("balanceCurveOption：单线 + 余额数据映射，x 轴 MM-DD", () => {
	const points = [
		{ date: "2026-09-01T00:00:00Z", balance: 5000 },
		{ date: "2026-09-13T12:00:00Z", balance: 5004.2 },
	];
	const option = balanceCurveOption(points, "#2563eb", "#6b7280");
	const series = option.series as Array<{ name: string; type: string; data: number[] }>;
	expect(series).toHaveLength(1);
	expect(series[0]?.name).toBe("余额");
	expect(series[0]?.data).toEqual([5000, 5004.2]);
	const xAxis = option.xAxis as { data: string[] };
	expect(xAxis.data).toEqual(["09-01", "09-13"]);
});

test("amountTone：投注/兑付按盈亏红绿，出入金/成本中性", () => {
	expect(amountTone("bet_payout", 4.2)).toBe("profit");
	expect(amountTone("bet_stake", -12)).toBe("loss");
	expect(amountTone("deposit", 5000)).toBe("neutral");
	expect(amountTone("withdraw", -100)).toBe("neutral");
	expect(amountTone("cost", -3)).toBe("neutral");
});

// ---- 余额 + 流水 + 语义色 + 曲线挂载 ----

test("shows balance, kind-colored events, and mounts the 30-day balance chart", async () => {
	renderBankroll();

	expect(await screen.findByTestId("bankroll-balance")).toHaveTextContent("¥5004.20");
	const events = screen.getAllByTestId("bankroll-event");
	expect(events).toHaveLength(2);
	expect(screen.getByText("兑付")).toBeInTheDocument();
	// 兑付 +4.20 → 盈亏红（profit）；入金 +5000 → 中性
	expect(screen.getByText("+4.20")).toHaveClass("text-profit");
	expect(screen.getByText("+5000.00")).not.toHaveClass("text-profit");
	// 曲线：两笔流水都在近 30 天 → 挂载（echarts 已 mock，断言容器渲染）
	expect(screen.getByTestId("balance-curve")).toHaveAttribute("aria-label", "近 30 天余额迷你曲线");
});

test("does not mount the chart when fewer than 2 events fall in the window", async () => {
	server.use(
		http.get("*/api/v1/bankroll", () =>
			HttpResponse.json({
				balance: 100,
				events: [
					{
						id: 1,
						occurred_at: daysAgo(40),
						kind: "deposit",
						amount_cny: 100,
						balance_after: 100,
						bet_id: null,
						note: null,
					},
				],
			}),
		),
	);

	await renderBankroll();

	expect(await screen.findByTestId("bankroll-balance")).toHaveTextContent("¥100.00");
	expect(screen.queryByTestId("balance-curve")).not.toBeInTheDocument();
	expect(screen.getByTestId("balance-curve-empty")).toHaveTextContent("近 30 天流水不足 2 笔");
});

// ---- 成本摘要（¥ 与 credits 分列 + 警示保留） ----

test("shows the period cost summary with amounts and credits separated", async () => {
	renderBankroll();

	expect(await screen.findByTestId("cost-total")).toHaveTextContent("¥12.50");
	expect(screen.getByTestId("cost-credits")).toHaveTextContent("38");
	const items = screen.getAllByTestId("cost-item");
	expect(items).toHaveLength(2);
	// 未记录成本不冒充已覆盖
	expect(screen.getByTestId("cost-summary")).toHaveTextContent("不视为总成本已覆盖");
});

// ---- 空态 → 入金引导 → 表单 → 成功刷新 ----

test("empty page guides the first deposit, submits the form, and refreshes balance", async () => {
	// 有状态 mock：POST 落一条流水，随后的 GET（invalidate 后）带回新余额
	type EventRow = BankrollEvent;
	let balance: number | null = null;
	let events: EventRow[] = [];
	server.use(
		http.get("*/api/v1/bankroll", () => HttpResponse.json({ balance, events })),
		http.post("*/api/v1/bankroll/deposits", async ({ request }) => {
			const body = (await request.json()) as DepositInput;
			const event: EventRow = {
				id: events.length + 1,
				occurred_at: new Date().toISOString(),
				kind: "deposit",
				amount_cny: body.amount_cny,
				balance_after: (balance ?? 0) + body.amount_cny,
				bet_id: null,
				note: body.note ?? null,
			};
			events = [event, ...events];
			balance = event.balance_after;
			return HttpResponse.json({ event, balance }, { status: 201 });
		}),
	);

	await renderBankroll();

	// 空态：余额未入金 + 入金引导（教流程，不装死页）
	expect(await screen.findByTestId("bankroll-balance")).toHaveTextContent("尚未入金");
	const guide = screen.getByTestId("bankroll-empty");
	await userEvent.click(within(guide).getByRole("button", { name: "记录第一笔入金" }));

	const form = screen.getByTestId("deposit-form");
	await userEvent.type(within(form).getByLabelText("金额（¥）"), "2000");
	await userEvent.type(within(form).getByLabelText("备注"), "首笔入金");
	await userEvent.click(within(form).getByRole("button", { name: "提交入金" }));

	// 成功后刷新：余额大数字、流水表出现，引导与表单收起
	expect(await screen.findByTestId("bankroll-balance")).toHaveTextContent("¥2000.00");
	expect(await screen.findByTestId("bankroll-event")).toBeInTheDocument();
	expect(screen.getByTestId("bankroll-message")).toHaveTextContent("已入金 ¥2000.00，最新余额 ¥2000.00");
	expect(screen.queryByTestId("bankroll-empty")).not.toBeInTheDocument();
	expect(screen.queryByTestId("deposit-form")).not.toBeInTheDocument();
	// 单笔流水不足 2 点：曲线诚实说"待积累"（曲线挂载形态由其余用例覆盖）
	expect(screen.getByTestId("balance-curve-empty")).toHaveTextContent("近 30 天流水不足 2 笔");
});

test("deposit form rejects non-positive amounts locally without a request", async () => {
	let posted = 0;
	server.use(
		http.post("*/api/v1/bankroll/deposits", () => {
			posted += 1;
			return HttpResponse.json({ detail: "nope" }, { status: 400 });
		}),
	);

	await renderBankroll();

	// 已有余额：从次级入口打开同一张表单
	await userEvent.click(await screen.findByTestId("deposit-open"));
	const form = screen.getByTestId("deposit-form");
	await userEvent.type(within(form).getByLabelText("金额（¥）"), "0");
	await userEvent.click(within(form).getByRole("button", { name: "提交入金" }));

	expect(screen.getByTestId("deposit-problem")).toHaveTextContent("金额必须大于 0");
	expect(posted).toBe(0);
});

test("surfaces server errors from the deposit endpoint", async () => {
	server.use(
		http.post("*/api/v1/bankroll/deposits", () => HttpResponse.json({ detail: "入金金额非法" }, { status: 400 })),
	);

	await renderBankroll();

	await userEvent.click(await screen.findByTestId("deposit-open"));
	const form = screen.getByTestId("deposit-form");
	await userEvent.type(within(form).getByLabelText("金额（¥）"), "100");
	await userEvent.click(within(form).getByRole("button", { name: "提交入金" }));

	expect(await screen.findByTestId("deposit-problem")).toHaveTextContent("入金失败：入金金额非法");
	// 表单保持打开，可修正后重试
	expect(screen.getByTestId("deposit-form")).toBeInTheDocument();
});

test("offers a secondary deposit entry once a balance exists", async () => {
	renderBankroll();

	expect(await screen.findByTestId("bankroll-balance")).toHaveTextContent("¥5004.20");
	await userEvent.click(screen.getByTestId("deposit-open"));
	expect(screen.getByTestId("deposit-form")).toBeInTheDocument();
	expect(screen.getByLabelText("发生时间（留空 = 现在）")).toBeInTheDocument();
	expect(screen.getByLabelText("备注")).toBeInTheDocument();
});

// ---- 空库与降级三态 ----

test("shows an unseeded state before any deposit", async () => {
	server.use(
		http.get("*/api/v1/bankroll", () => HttpResponse.json({ balance: null, events: [] })),
		http.get("*/api/v1/costs/summary", () =>
			HttpResponse.json({ since: null, total_cny: 0, credits_used: 0, items: [] }),
		),
	);

	await renderBankroll();

	expect(await screen.findByTestId("bankroll-balance")).toHaveTextContent("尚未入金");
	expect(screen.getByTestId("bankroll-empty")).toBeInTheDocument();
	expect(await screen.findByTestId("cost-missing")).toBeInTheDocument();
});

test("shows a degraded hint when the backend is unreachable", async () => {
	server.use(http.get("*/api/v1/bankroll", () => HttpResponse.json({ detail: "no" }, { status: 503 })));

	await renderBankroll();

	expect(await screen.findByTestId("bankroll-error")).toBeInTheDocument();
	expect(screen.getByTestId("empty-state")).toHaveAttribute("data-variant", "backend-unavailable");
});
