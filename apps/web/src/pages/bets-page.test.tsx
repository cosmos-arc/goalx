import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { expect, test } from "vitest";
import { server } from "../mocks/server";
import { router } from "../router";

async function renderBets() {
	await router.navigate({ to: "/bets" });
	const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
	return render(
		<QueryClientProvider client={queryClient}>
			<RouterProvider router={router} />
		</QueryClientProvider>,
	);
}

test("sections split suggestions, locked paper and live recordings", async () => {
	renderBets();

	// 未锁定建议：paper + live 建议（含策略版本注金），已锁定纸面与真实回录分区
	expect(await screen.findAllByTestId("bet-row")).toHaveLength(4);
	expect(screen.getByTestId("section-suggestions")).toHaveTextContent("未锁定建议（2）");
	expect(screen.getByTestId("section-locked-paper")).toHaveTextContent("已锁定纸面（1）");
	expect(screen.getByTestId("section-live")).toHaveTextContent("真实回录（1");
	// 已结算串注盈利与复盘资格
	expect(screen.getByText("+26.60")).toBeInTheDocument();
	expect(screen.getByText("缺 closing")).toBeInTheDocument();
	// 真实回录显示建议→实际条款差异
	expect(screen.getByText("¥10.00→¥12.00")).toBeInTheDocument();
	expect(screen.getByText(/2\.00→1\.90/)).toBeInTheDocument();
	expect(screen.getByText("live 单独分组")).toBeInTheDocument();
});

test("locks a paper suggestion as a slip without touching bankroll", async () => {
	const user = userEvent.setup();
	renderBets();

	await user.click(await screen.findByTestId("lock-1"));
	expect(await screen.findByTestId("bets-message")).toHaveTextContent("已锁定纸面票 #1");
	expect(screen.getByTestId("bets-message")).toHaveTextContent("不产生真金流水");
});

test("records a live purchase with actual terms and suggestion diff", async () => {
	const user = userEvent.setup();
	let captured: unknown = null;
	server.use(
		http.post("*/api/v1/bet-slips", async ({ request }) => {
			captured = await request.json();
			return HttpResponse.json(
				{
					id: 9,
					mode: "live",
					placed_at: null,
					note: null,
					created_at: "2026-09-12T19:00:00+00:00",
					bet_count: 1,
					stake_total: 60,
					profit_total: 0,
				},
				{ status: 201 },
			);
		}),
	);
	renderBets();

	await user.click(await screen.findByTestId("open-actual-3"));
	await user.clear(screen.getByLabelText("实际金额"));
	await user.type(screen.getByLabelText("实际金额"), "60");
	await user.type(screen.getByLabelText("fixture 2 实际赔率"), "1.85");
	await user.click(screen.getByRole("button", { name: "提交" }));

	expect(await screen.findByTestId("bets-message")).toHaveTextContent("已回录真实购买票 #9");
	expect(captured).toMatchObject({
		bet_ids: [3],
		actuals: { "3": { stake: 60, leg_odds: [{ fixture_id: 2, odds: 1.85 }] } },
	});
});

test("imports a draw result and shows the settlement summary", async () => {
	const user = userEvent.setup();
	renderBets();

	await screen.findByText("周六003 A 队 vs B 队");
	await user.selectOptions(screen.getByTestId("draw-fixture"), "3");
	await user.type(screen.getByTestId("draw-home"), "2");
	await user.type(screen.getByTestId("draw-away"), "1");
	await user.click(screen.getByRole("button", { name: "导入" }));

	expect(await screen.findByTestId("bets-message")).toHaveTextContent("已导入 1 条开奖结果");

	await user.click(screen.getByRole("button", { name: "结算批跑" }));
	expect(await screen.findByTestId("bets-message")).toHaveTextContent("结算完成：1 注落定");
});

test("previews a correction with affected bets before importing", async () => {
	const user = userEvent.setup();
	renderBets();

	// fixture 1 已有结果 → 更正原因必填 + 预览面板
	await screen.findByText("周六001 阿森纳 vs 切尔西");
	await user.selectOptions(screen.getByTestId("draw-fixture"), "1");
	expect(screen.getByTestId("draw-correction-reason")).toBeRequired();
	await user.type(screen.getByTestId("draw-home"), "0");
	await user.type(screen.getByTestId("draw-away"), "1");
	await user.type(screen.getByTestId("draw-correction-reason"), "official fix");
	await user.click(screen.getByTestId("draw-preview"));

	const panel = await screen.findByTestId("draw-preview-result");
	expect(panel).toHaveTextContent("更正");
	expect(await screen.findByTestId("preview-affected-row")).toHaveTextContent("-28.60");
});

test("void result entry carries the void reason", async () => {
	const user = userEvent.setup();
	let captured: unknown = null;
	server.use(
		http.post("*/api/v1/draw-results", async ({ request }) => {
			captured = await request.json();
			return HttpResponse.json({ imported: 1 }, { status: 201 });
		}),
	);
	renderBets();

	await screen.findByText("周六003 A 队 vs B 队");
	await user.selectOptions(screen.getByTestId("draw-fixture"), "3");
	await user.type(screen.getByTestId("draw-home"), "0");
	await user.type(screen.getByTestId("draw-away"), "0");
	await user.click(screen.getByTestId("draw-void"));
	await user.type(screen.getByTestId("draw-void-reason"), "腰斩");
	await user.click(screen.getByRole("button", { name: "导入" }));

	expect(await screen.findByTestId("bets-message")).toHaveTextContent("已导入 1 条开奖结果");
	expect(captured).toMatchObject({
		results: [{ fixture_id: 3, void: true, void_reason: "腰斩" }],
	});
});

test("surfaces lock and import errors", async () => {
	const user = userEvent.setup();
	server.use(
		http.post("*/api/v1/bet-slips", () =>
			HttpResponse.json({ detail: "fixture 3 不可投: sale_stopped" }, { status: 400 }),
		),
	);
	renderBets();

	await user.click(await screen.findByTestId("lock-1"));
	expect(await screen.findByTestId("bets-message")).toHaveTextContent("锁定失败");
	expect(screen.getByTestId("bets-message")).toHaveTextContent("sale_stopped");

	// detail 为对象时 JSON 透出，不显示 [object Object]
	server.use(
		http.post("*/api/v1/bet-slips", () => HttpResponse.json({ detail: { code: "bad_leg" } }, { status: 400 })),
	);
	await user.click(screen.getByTestId("lock-1"));
	expect(await screen.findByTestId("bets-message")).toHaveTextContent("bad_leg");

	server.use(http.post("*/api/v1/draw-results", () => HttpResponse.json({ detail: "404" }, { status: 404 })));
	await user.selectOptions(screen.getByTestId("draw-fixture"), "2");
	await user.type(screen.getByTestId("draw-home"), "1");
	await user.type(screen.getByTestId("draw-away"), "1");
	await user.click(screen.getByRole("button", { name: "导入" }));
	expect(await screen.findByTestId("bets-message")).toHaveTextContent("导入失败");
});

test("surfaces preview errors", async () => {
	const user = userEvent.setup();
	server.use(
		http.post("*/api/v1/draw-results/preview", () => HttpResponse.json({ detail: { code: "stale" } }, { status: 400 })),
	);
	renderBets();

	await screen.findByText("周六003 A 队 vs B 队");
	await user.selectOptions(screen.getByTestId("draw-fixture"), "3");
	await user.type(screen.getByTestId("draw-home"), "1");
	await user.type(screen.getByTestId("draw-away"), "1");
	await user.click(screen.getByTestId("draw-preview"));

	expect(await screen.findByTestId("bets-message")).toHaveTextContent("预览失败");
	expect(screen.getByTestId("bets-message")).toHaveTextContent("stale");
});

test("surfaces live recording errors with object details", async () => {
	const user = userEvent.setup();
	server.use(
		http.post("*/api/v1/bet-slips", () => HttpResponse.json({ detail: { code: "double_record" } }, { status: 400 })),
	);
	renderBets();

	await user.click(await screen.findByTestId("open-actual-3"));
	await user.click(screen.getByRole("button", { name: "提交" }));

	expect(await screen.findByTestId("bets-message")).toHaveTextContent("回录失败");
	expect(screen.getByTestId("bets-message")).toHaveTextContent("double_record");
});

test("surfaces settlement errors", async () => {
	const user = userEvent.setup();
	server.use(http.post("*/api/v1/settlements/run", () => HttpResponse.json({ detail: "x" }, { status: 500 })));
	renderBets();

	await user.click(await screen.findByRole("button", { name: "结算批跑" }));
	expect(await screen.findByTestId("bets-message")).toHaveTextContent("结算失败");
});

test("shows empty states honestly when there are no bets", async () => {
	server.use(http.get("*/api/v1/bets", () => HttpResponse.json([])));
	renderBets();

	expect(await screen.findByText("无未锁定建议。")).toBeInTheDocument();
	expect(screen.getByText("无已锁定纸面注。")).toBeInTheDocument();
	expect(screen.getByText("无真实回录。")).toBeInTheDocument();
});
