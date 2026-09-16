import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { expect, test } from "vitest";
import { betsFixture, todayFixture } from "../mocks/handlers";
import { server } from "../mocks/server";
import { router } from "../router";

/**
 * 票 16 投注生命周期测试：三段分组（未锁定建议/已锁定/已结算）+ 组头计数与
 * 状态徽章（胜=profit 红、负=loss 绿、底纹文字前景色）、真实回录右侧 Drawer
 * （建议快照 vs 实际条款对照 + 金额≠建议提示）、赛果同步面板降级态与待出列表、
 * 人工兜底表单（更正必填原因/影响预览）、锁定开赛 <5 分钟警示（不阻止）。
 */

async function renderBets() {
	await router.navigate({ to: "/bets" });
	const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
	return render(
		<QueryClientProvider client={queryClient}>
			<RouterProvider router={router} />
		</QueryClientProvider>,
	);
}

test("groups suggestions, locked and settled with semantic status badges and recency order", async () => {
	renderBets();

	expect(await screen.findByTestId("section-suggestions")).toHaveTextContent("未锁定建议");
	expect(screen.getByTestId("section-locked")).toHaveTextContent("已锁定");
	expect(screen.getByTestId("section-settled")).toHaveTextContent("已结算");

	// 未锁定建议：纸面 #1 + 真金 #3，按 created_at 倒序（#3 11:00 > #1 10:00）
	const suggestionRows = within(screen.getByTestId("section-suggestions")).getAllByTestId("bet-row");
	expect(suggestionRows).toHaveLength(2);
	expect(suggestionRows[0]).toHaveTextContent("¥50.00");
	expect(suggestionRows[1]).toHaveTextContent("¥100.00");

	// 已锁定段：默认 fixture 无已锁定注 → 诚实空态
	expect(screen.getByTestId("section-locked")).toHaveTextContent("无已锁定注——建议锁定后在这里等待开奖。");

	// 已结算段：纸面胜（profit 红）+ 真金胜，行内模式徽章区分（票 06：行内徽章不再单独分组）
	const settledRows = within(screen.getByTestId("section-settled")).getAllByTestId("bet-row");
	expect(settledRows).toHaveLength(2);
	expect(settledRows[0]).toHaveTextContent("+26.60");
	expect(settledRows[1]).toHaveTextContent("+10.80");
	const paperSettled = settledRows[0];
	if (!paperSettled) {
		throw new Error("expected a settled paper row");
	}
	expect(within(paperSettled).getByTestId("mode-paper")).toHaveTextContent("纸面");
	expect(within(paperSettled).getByText("胜")).toHaveClass("bg-profit/10", "text-foreground");
	expect(within(paperSettled).getByText("+26.60")).toHaveClass("text-profit");
	expect(within(paperSettled).getByText("缺 closing")).toBeInTheDocument();
	const liveSettled = settledRows[1];
	if (!liveSettled) {
		throw new Error("expected a settled live row");
	}
	expect(within(liveSettled).getByTestId("mode-live")).toHaveTextContent("真金");
	// 真实回录显示建议→实际条款差异（快照保留）
	expect(within(liveSettled).getByText("¥10.00→¥12.00")).toBeInTheDocument();
	expect(within(liveSettled).getByText(/2\.00→1\.90/)).toBeInTheDocument();
	expect(within(liveSettled).getByText("live 单独分组")).toBeInTheDocument();
});

test("locked-open bets wait in the locked section with pending hint", async () => {
	const lockedOpen = {
		...betsFixture[1],
		id: 21,
		status: "open",
		payout: null,
		profit: null,
		settled_at: null,
	};
	server.use(http.get("*/api/v1/bets", () => HttpResponse.json([lockedOpen])));
	renderBets();

	const section = await screen.findByTestId("section-locked");
	expect(section).toHaveTextContent("已锁定 1");
	expect(section).toHaveTextContent("1 注待开奖/结算");
	const row = within(section).getByTestId("bet-row");
	expect(within(row).getByText("未结")).toHaveClass("bg-muted", "text-muted-foreground");
	// 未结注盈亏列中性占位
	expect(row).toHaveTextContent("—");
	expect(screen.getByTestId("section-suggestions")).toHaveTextContent("无未锁定建议。");
	expect(screen.getByTestId("section-settled")).toHaveTextContent("尚无已结算注——开奖导入并结算后在这里复盘。");
});

test("locks a paper suggestion as a slip without touching bankroll", async () => {
	const user = userEvent.setup();
	renderBets();

	await user.click(await screen.findByTestId("lock-1"));
	expect(await screen.findByTestId("bets-message")).toHaveTextContent("已锁定纸面票 #1");
	expect(screen.getByTestId("bets-message")).toHaveTextContent("不产生真金流水");
});

test("warns when locking within 5 minutes of kickoff, then continues on confirm", async () => {
	const user = userEvent.setup();
	let lockPosts = 0;
	server.use(
		http.get("*/api/v1/fixtures/today", () =>
			HttpResponse.json([{ ...todayFixture[0], kickoff_utc: new Date(Date.now() + 3 * 60_000).toISOString() }]),
		),
		http.post("*/api/v1/bet-slips", async () => {
			lockPosts += 1;
			return HttpResponse.json({
				id: 1,
				mode: "paper",
				placed_at: null,
				note: null,
				created_at: "2026-09-12T19:00:00+00:00",
				bet_count: 1,
				stake_total: 100,
				profit_total: 0,
			});
		}),
	);
	renderBets();

	// 第一次点击：只警示不锁定（规则前置不阻止，警示后可继续）
	await user.click(await screen.findByTestId("lock-1"));
	expect(await screen.findByTestId("bets-message")).toHaveTextContent("3 分钟后开赛（不足 5 分钟）");
	expect(lockPosts).toBe(0);

	// 再次点击：确认继续锁定
	await user.click(screen.getByTestId("lock-1"));
	expect(await screen.findByTestId("bets-message")).toHaveTextContent("已锁定纸面票 #1");
	expect(lockPosts).toBe(1);
});

test("warns on already-kicked-off fixtures before locking", async () => {
	const user = userEvent.setup();
	server.use(
		http.get("*/api/v1/fixtures/today", () =>
			HttpResponse.json([{ ...todayFixture[0], kickoff_utc: new Date(Date.now() - 60 * 60_000).toISOString() }]),
		),
	);
	renderBets();

	await user.click(await screen.findByTestId("lock-1"));
	expect(await screen.findByTestId("bets-message")).toHaveTextContent("含已开赛场次");
	expect(screen.getByTestId("bets-message")).toHaveTextContent("锁定晚于开赛");
});

test("records a live purchase in the drawer with suggestion-vs-actual comparison", async () => {
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
	const drawer = await screen.findByTestId("actual-drawer");
	// 对照区：建议快照 vs 实际条款（默认实际=建议金额、赔率按锁定价）
	expect(within(drawer).getByTestId("actual-compare")).toHaveTextContent("金额 ¥50.00 → ¥50.00");
	expect(within(drawer).getByTestId("actual-compare")).toHaveTextContent("#2 赔率 1.90 → 按锁定价");
	// 金额≠建议 → 按实际条款记账提示
	expect(screen.queryByTestId("actual-diff-note")).not.toBeInTheDocument();
	await user.clear(screen.getByLabelText("实际金额"));
	await user.type(screen.getByLabelText("实际金额"), "60");
	expect(await screen.findByTestId("actual-diff-note")).toHaveTextContent("按实际条款记账");
	expect(within(drawer).getByTestId("actual-compare")).toHaveTextContent("金额 ¥50.00 → ¥60.00");

	// 逐腿赔率（placeholder=锁定赔率）+ 回录时点
	const legOdds = screen.getByLabelText("#2 实际赔率");
	expect(legOdds).toHaveAttribute("placeholder", "1.90");
	await user.type(legOdds, "1.85");
	expect(within(drawer).getByTestId("actual-compare")).toHaveTextContent("#2 赔率 1.90 → 1.85");
	await user.type(screen.getByLabelText("回录时点"), "2026-09-12T18:30");
	await user.click(screen.getByTestId("actual-submit"));

	expect(await screen.findByTestId("bets-message")).toHaveTextContent("已回录真实购买票 #9");
	expect(captured).toMatchObject({
		bet_ids: [3],
		actuals: { "3": { stake: 60, leg_odds: [{ fixture_id: 2, odds: 1.85 }] } },
	});
	const payload = captured as { placed_at: string | null };
	expect(payload.placed_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00/);
	// 成功后抽屉关闭
	await waitFor(() => expect(screen.queryByTestId("actual-drawer")).not.toBeInTheDocument());
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

test("sync panel shows the degraded state and lists kickoff-passed fixtures without results", async () => {
	const user = userEvent.setup();
	server.use(
		http.get("*/api/v1/fixtures/today", () =>
			HttpResponse.json([
				{
					...todayFixture[0],
					fixture_id: 9,
					match_code: "周五001",
					home_team: "C 队",
					away_team: "D 队",
					kickoff_utc: new Date(Date.now() - 60 * 60_000).toISOString(),
				},
			]),
		),
		http.get("*/api/v1/bets", () =>
			HttpResponse.json([
				{
					...betsFixture[0],
					id: 30,
					legs: [
						{
							fixture_id: 9,
							market_code: "had",
							selection_code: "h",
							locked_odds: 2.05,
							actual_odds: null,
							goal_line: null,
						},
					],
				},
			]),
		),
	);
	renderBets();

	// 同步优先裁决：后端源未接入 → 降级态（人工兜底通道保留）
	expect(await screen.findByTestId("sync-status")).toHaveTextContent("自动同步待后端源接入——当前人工兜底。");
	const row = await screen.findByTestId("draw-pending-row");
	expect(row).toHaveTextContent("周五001");
	expect(row).toHaveTextContent("C 队 vs D 队");
	expect(row).toHaveTextContent("开赛");
	expect(row).toHaveTextContent("挂未结注");

	// 待出场次一键带入人工录入表单
	await user.click(screen.getByRole("button", { name: "录入 周五001" }));
	expect(screen.getByTestId("draw-fixture")).toHaveValue("9");
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
	await user.click(screen.getByTestId("actual-submit"));

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

test("lists slip-level status with stake and profit totals", async () => {
	renderBets();

	const row = await screen.findByTestId("slip-row");
	const section = screen.getByTestId("section-slips");
	expect(section).toHaveTextContent("票级状态");
	expect(row).toHaveTextContent("纸面");
	expect(row).toHaveTextContent("¥100.00");
});

test("shows empty states honestly when there are no bets", async () => {
	server.use(
		http.get("*/api/v1/bets", () => HttpResponse.json([])),
		http.get("*/api/v1/bet-slips", () => HttpResponse.json([])),
	);
	renderBets();

	expect(await screen.findByText("无未锁定建议。")).toBeInTheDocument();
	expect(screen.getByText("无已锁定注——建议锁定后在这里等待开奖。")).toBeInTheDocument();
	expect(screen.getByText("尚无已结算注——开奖导入并结算后在这里复盘。")).toBeInTheDocument();
	expect(screen.getByText("无回录票。")).toBeInTheDocument();
});
