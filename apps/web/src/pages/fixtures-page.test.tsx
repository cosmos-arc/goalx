import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { expect, test } from "vitest";
import { todayFixture } from "../mocks/handlers";
import { server } from "../mocks/server";
import { router } from "../router";

/**
 * 票 14 今日页重设计测试（票 wb-01 迁移为场次页）：信息分层（可投卡片置顶 + 全量 compact 表）、
 * 语义编码（EV 红绿/琥珀徽章/资格蓝红灰/新鲜度）、选注篮规则前置
 * （同场替换/2串1 上限/非单固提示/停售禁用）、三态空状态 + 加载骨架。
 * 服务器校验仍为唯一权威——前端提示只验证"存在"，不验证业务判定。
 */

/** 北京时区业务日——与页面/后端同口径（mock 行 business_date 用）。 */
function todayBd(): string {
	return new Date(Date.now() + 8 * 3_600_000).toISOString().slice(0, 10);
}

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
					stake: 50,
					actual_stake: null,
					strategy_version: "manual-v1",
					placed_at: null,
					locked_at: null,
					created_at: "2026-09-12T10:00:00+00:00",
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

test("renders eligible cards on top, the full compact table, and the semantic encodings", async () => {
	await renderAt("/fixtures");

	expect(await screen.findByText("GoalX · 场次")).toBeInTheDocument();
	const rows = await screen.findAllByTestId("fixtures-row");
	expect(rows).toHaveLength(3);
	const [row1, row2, row3] = rows;
	if (!row1 || !row2 || !row3) {
		throw new Error("expected three fixtures rows");
	}

	// 信息分层：可投卡片置顶（1、2 可投；3 停售不可投），对照计数在标题行
	expect(screen.getByTestId("fixtures-card-1")).toBeInTheDocument();
	expect(screen.getByTestId("fixtures-card-2")).toBeInTheDocument();
	expect(screen.getByTestId("fixtures-not-eligible-count")).toHaveTextContent("1 场不可投");

	// 卡片编码：EV 永远只按正负红绿；偏差/样本少走独立琥珀徽章；最强标注；不重复"可投"徽章
	const card = screen.getByTestId("fixtures-card-1");
	expect(within(card).getByText("主胜 +5.3%")).toHaveClass("text-profit");
	expect(within(card).getByText("客胜 -17.5%")).toHaveClass("text-loss");
	expect(within(card).getByTestId("flag-ev_deviation")).toHaveTextContent("EV 偏差≥5%");
	expect(within(card).getByTestId("flag-few_books")).toHaveTextContent("样本少");
	expect(within(card).getByText("最强 主胜")).toBeInTheDocument();
	expect(within(card).queryByText("可投")).not.toBeInTheDocument();
	expect(within(card).getByText(/小时后$/)).toBeInTheDocument(); // 倒计时（kickoff +2h）
	// 仅串关标记：非单固可投场
	expect(within(screen.getByTestId("fixtures-card-2")).getByText("仅串关")).toBeInTheDocument();

	// 页头全局数据健康 + 配色图例
	expect(screen.getByTestId("fixtures-data-health")).toHaveTextContent(/竞彩报价 \d+分钟前/);
	expect(screen.getByTestId("fixtures-data-health")).toHaveTextContent("欧赔 已接入 3/3 场");
	expect(screen.getByText(/配色：红 = 正向 EV/)).toBeInTheDocument();

	// 表格资格列：可投（蓝+单固，两场 valid）/拒绝（红+原因）
	const validBadges = screen.getAllByTestId("had-quote-valid");
	expect(validBadges).toHaveLength(2);
	expect(validBadges[0]).toHaveTextContent("可投");
	expect(validBadges[0]).toHaveTextContent("单固");
	expect(screen.getByTestId("had-quote-rejected")).toHaveTextContent("拒绝");
	expect(screen.getByTestId("had-quote-rejected")).toHaveTextContent("已停售");

	// 表格 EV 列按正负红绿；无 EV 场占位
	const evCells = screen.getAllByTestId("ev-cell");
	const firstEvCell = evCells.at(0);
	if (!firstEvCell) {
		throw new Error("expected an ev-cell");
	}
	expect(firstEvCell).toHaveTextContent("+5.3%");
	expect(within(firstEvCell).getByText("-12.0%")).toHaveClass("text-loss");
	expect(evCells.at(2)).toHaveTextContent("—");

	// 新鲜度折进时间格第二行：>30 分钟琥珀；缺失省略
	expect(row2).toHaveTextContent(/彩 45分钟前/);
	expect(within(row2).getByText(/彩 45分钟前/)).toHaveClass("text-warning");
	expect(row3).not.toHaveTextContent(/彩 /);

	// 拒绝场：行 wash 弱化 + 选注按钮禁用（停售规则前置）
	expect(row3).toHaveClass("bg-muted/50");
	expect(screen.getByTestId("pick-3-h")).toBeDisabled();
	// 欧共识与 books
	expect(rows[0]).toHaveTextContent("53/25/22");
	expect(within(row3).getByText("—", { selector: "td:last-child" })).toBeInTheDocument();
});

test("encodes unknown evidence and missing verdict honestly", async () => {
	server.use(
		http.get("*/api/v1/fixtures/today", () =>
			HttpResponse.json([
				{
					fixture_id: 9,
					match_code: "周日009",
					business_date: todayBd(),
					competition: "英超",
					tier: "tier1",
					home_team: "X 队",
					away_team: "Y 队",
					kickoff_utc: new Date(Date.now() + 4 * 3_600_000).toISOString(),
					is_single: true,
					joined: false,
					jc_odds: { h: 2.0, d: 3.0, a: 3.0 },
					jc_updated_at: null,
					books: 0,
					eu_prob: null,
					ev: null,
					flags: [],
					had_quote: {
						as_of: new Date().toISOString(),
						status: "unknown",
						reasons: ["eu_no_quote"],
						sale_state: "on_sale",
						single_eligible: false,
						jc_source_updated_at: null,
						eu_books: 0,
					},
				},
				{
					fixture_id: 10,
					match_code: "周日010",
					business_date: todayBd(),
					competition: "西甲",
					tier: "tier2",
					home_team: "Z 队",
					away_team: "W 队",
					kickoff_utc: new Date(Date.now() + 6 * 3_600_000).toISOString(),
					is_single: false,
					joined: false,
					jc_odds: { h: 2.0, d: 3.0, a: 3.0 },
					jc_updated_at: null,
					books: 0,
					eu_prob: null,
					ev: null,
					flags: [],
				},
			]),
		),
	);
	await renderAt("/fixtures");

	expect(await screen.findAllByTestId("fixtures-row")).toHaveLength(2);
	// 证据未知 = 灰框 + 原因；行弱化
	expect(screen.getByTestId("had-quote-unknown")).toHaveTextContent("证据未知");
	expect(screen.getByTestId("had-quote-unknown")).toHaveTextContent("无欧赔");
	// 无判定占位；健康行：无任何竞彩报价时间 → 占位，欧赔覆盖诚实计 0/2
	expect(screen.getByText("无判定")).toBeInTheDocument();
	expect(screen.getByTestId("fixtures-data-health")).toHaveTextContent("竞彩报价 —");
	expect(screen.getByTestId("fixtures-data-health")).toHaveTextContent("欧赔 已接入 0/2 场");
});

test("quick pick adds, replaces on same fixture, deselects, and caps at two legs", async () => {
	const user = userEvent.setup();
	await renderAt("/fixtures");

	// 快捷选注：卡片钮（pick-card-*，区块即快捷入口）
	await user.click(await screen.findByTestId("pick-card-1-h"));
	expect(screen.getByTestId("basket-count")).toHaveTextContent("1/2");
	expect(screen.getByTestId("basket-summary")).toHaveTextContent("周六001 主胜");

	// 同场换选 = 直接替换 + 行内提示（不报错）
	await user.click(screen.getByTestId("pick-card-1-d"));
	expect(screen.getByTestId("fixtures-message")).toHaveTextContent("同场只能选一腿");
	expect(screen.getByTestId("fixtures-message")).toHaveTextContent("已替换原选择");
	expect(screen.getByTestId("basket-count")).toHaveTextContent("1/2");
	expect(screen.getByTestId("basket-summary")).toHaveTextContent("周六001 平");

	// 再点同选项 = 取消
	await user.click(screen.getByTestId("pick-card-1-d"));
	expect(screen.getByTestId("basket-count")).toHaveTextContent("0/2");

	// 两腿后展示组合赔率（表格钮同样可选）；第三选被上限提示拦下（到不了提交）
	await user.click(await screen.findByTestId("pick-1-h"));
	await user.click(screen.getByTestId("pick-2-a"));
	expect(screen.getByTestId("basket-count")).toHaveTextContent("2/2");
	expect(screen.getByTestId("basket-summary")).toHaveTextContent("组合赔率 4.22"); // 1.92 × 2.2
	await user.click(screen.getByTestId("pick-2-h"));
	expect(screen.getByTestId("fixtures-message")).toHaveTextContent("已达 2串1 上限");
	expect(screen.getByTestId("basket-count")).toHaveTextContent("2/2");
});

test("non-single first leg warns but is allowed; the message clears on a single pick", async () => {
	const user = userEvent.setup();
	await renderAt("/fixtures");

	// 周六002 非单固：提示"只能作串关腿"但不阻止选择（服务器最终裁决）
	await user.click(await screen.findByTestId("pick-2-a"));
	expect(screen.getByTestId("fixtures-message")).toHaveTextContent("非单固");
	expect(screen.getByTestId("fixtures-message")).toHaveTextContent("串关第二腿");
	expect(screen.getByTestId("basket-count")).toHaveTextContent("1/2");

	// 随后选单固场 → 提示清除
	await user.click(screen.getByTestId("pick-1-h"));
	await waitFor(() => expect(screen.queryByTestId("fixtures-message")).not.toBeInTheDocument());
	expect(screen.getByTestId("basket-count")).toHaveTextContent("2/2");
});

test("basket drawer manages legs and creates the suggestion through the real API shape", async () => {
	const user = userEvent.setup();
	const captured: { body: unknown } = { body: null };
	mockCreatedBet(77, captured);
	await renderAt("/fixtures");

	// 2串1 提交：腿、模式、金额、策略版本一起进请求体
	await user.click(await screen.findByTestId("pick-1-h"));
	await user.click(screen.getByTestId("pick-2-a"));
	await user.click(screen.getByTestId("basket-open"));
	const drawerLegs = await screen.findAllByTestId("basket-leg");
	expect(drawerLegs).toHaveLength(2);
	await user.clear(screen.getByTestId("basket-stake"));
	await user.type(screen.getByTestId("basket-stake"), "50");
	await user.type(screen.getByTestId("basket-strategy"), "manual-v1");
	await user.selectOptions(screen.getByTestId("basket-mode"), "live");
	await user.click(screen.getByTestId("basket-submit"));

	// 建议提交走现有 API；成功后清篮、关抽屉、消息常驻底部
	expect(await screen.findByTestId("fixtures-message")).toHaveTextContent("已建建议 #77（2串1）");
	expect(captured.body).toMatchObject({
		mode: "live",
		stake: 50,
		strategy_version: "manual-v1",
		legs: [
			{ fixture_id: 1, market_code: "had", selection_code: "h", locked_odds: 1.92 },
			{ fixture_id: 2, market_code: "had", selection_code: "a", locked_odds: 2.2 },
		],
	});
	expect(screen.getByTestId("basket-count")).toHaveTextContent("0/2");
	await waitFor(() => expect(screen.queryByTestId("basket-stake")).not.toBeInTheDocument());

	// 篮内移除 + 规则说明回退
	await user.click(screen.getByTestId("pick-1-h"));
	await user.click(screen.getByTestId("basket-open"));
	await user.click(screen.getByRole("button", { name: "移除 周六001" }));
	expect(screen.getByText("未选择。点页面里任意赔率加入。")).toBeInTheDocument();
	await user.click(screen.getByText("继续浏览"));
	await waitFor(() => expect(screen.queryByTestId("basket-stake")).not.toBeInTheDocument());

	// 单关 + 空策略版本（→ null）+ 纸面模式（回选）
	await user.click(screen.getByTestId("pick-1-h"));
	await user.click(screen.getByTestId("basket-open"));
	await user.clear(screen.getByTestId("basket-strategy"));
	await user.selectOptions(screen.getByTestId("basket-mode"), "paper");
	await user.click(screen.getByTestId("basket-submit"));
	expect(await screen.findByTestId("fixtures-message")).toHaveTextContent("已建建议 #77（单关）");
	expect(captured.body).toMatchObject({ mode: "paper", stake: 50, strategy_version: null });

	// 服务器拒绝 = 唯一权威：400 detail 透出为失败消息
	await user.click(screen.getByTestId("pick-1-h"));
	await user.click(screen.getByTestId("basket-open"));
	server.use(
		http.post("*/api/v1/bets", () => HttpResponse.json({ detail: "fixture 1 不可投: sale_stopped" }, { status: 400 })),
	);
	await user.click(screen.getByTestId("basket-submit"));
	expect(await screen.findByTestId("fixtures-message")).toHaveTextContent("建注失败");
	expect(screen.getByTestId("fixtures-message")).toHaveTextContent("sale_stopped");
});

test("renders edge encodings: partial triples, invalid timestamps, passed kickoff, neutral near-zero EV", async () => {
	server.use(
		http.get("*/api/v1/fixtures/today", () =>
			HttpResponse.json([
				{
					fixture_id: 11,
					match_code: "周六011",
					business_date: todayBd(),
					competition: "德甲",
					tier: "tier2",
					home_team: "E 队",
					away_team: "F 队",
					kickoff_utc: new Date(Date.now() + 2 * 3_600_000).toISOString(),
					is_single: true,
					joined: true,
					jc_odds: { h: null, d: 3.0, a: 3.2 },
					jc_updated_at: new Date(Date.now() - 45 * 60_000).toISOString(),
					books: 5,
					eu_prob: { h: 0.5, d: null, a: 0.2 },
					ev: { h: 0.001, d: null, a: -0.1 },
					flags: ["custom_flag"],
					had_quote: {
						as_of: new Date().toISOString(),
						status: "valid",
						reasons: [],
						sale_state: "on_sale",
						single_eligible: true,
						jc_source_updated_at: null,
						eu_books: 5,
					},
				},
				{
					fixture_id: 12,
					match_code: "周六012",
					business_date: todayBd(),
					competition: "西甲",
					tier: "tier2",
					home_team: "G 队",
					away_team: "H 队",
					kickoff_utc: "not-a-date",
					is_single: false,
					joined: false,
					jc_odds: { h: 2.0, d: 3.0, a: 3.0 },
					jc_updated_at: "garbage",
					books: 0,
					eu_prob: null,
					ev: null,
					flags: [],
					had_quote: { as_of: new Date().toISOString(), status: "unknown" },
				},
				{
					fixture_id: 13,
					match_code: "周六013",
					business_date: todayBd(),
					competition: "英超",
					tier: "tier1",
					home_team: "I 队",
					away_team: "J 队",
					kickoff_utc: new Date(Date.now() - 30 * 60_000).toISOString(),
					is_single: true,
					joined: true,
					jc_odds: { h: 2.0, d: 3.0, a: 3.0 },
					jc_updated_at: new Date(Date.now() - 90 * 60_000).toISOString(),
					books: 6,
					eu_prob: { h: 0.4, d: 0.3, a: 0.3 },
					ev: { h: -0.02, d: -0.03, a: -0.02 },
					flags: [],
					had_quote: {
						as_of: new Date().toISOString(),
						status: "rejected",
						reasons: [],
						sale_state: "stopped",
						single_eligible: true,
						jc_source_updated_at: null,
						eu_books: 6,
					},
				},
			]),
		),
	);
	await renderAt("/fixtures");

	const rows = await screen.findAllByTestId("fixtures-row");
	expect(rows).toHaveLength(3);
	const [rowA, rowB, rowC] = rows;
	if (!rowA || !rowB || !rowC) {
		throw new Error("expected three rows");
	}

	// 卡片：近零 EV 中性色；部分缺失的三元组各向占位；空赔率向按钮禁用；未知 flag 不渲染
	const card = screen.getByTestId("fixtures-card-11");
	expect(within(card).getByText("主胜 +0.1%")).toHaveClass("text-muted-foreground");
	expect(within(card).getByText("客胜 -10.0%")).toHaveClass("text-loss");
	expect(within(card).getByText("平 —")).toBeInTheDocument();
	expect(within(card).getByTestId("pick-card-11-h")).toHaveTextContent("—");
	expect(within(card).getByTestId("pick-card-11-h")).toBeDisabled();
	expect(within(card).queryByText("custom_flag")).not.toBeInTheDocument();
	expect(within(card).queryByText("T1")).not.toBeInTheDocument(); // tier2 → 无 T1 徽章
	expect(within(card).getByText("最强 主胜")).toBeInTheDocument();

	// 页头健康行：全部 jc 时间缺失 → 占位；仅有过期报价 → 琥珀
	expect(screen.getByTestId("fixtures-data-health")).toHaveTextContent("竞彩报价 45分钟前");
	expect(within(screen.getByTestId("fixtures-data-health")).getByText(/竞彩报价 45分钟前/)).toHaveClass("text-warning");

	// 坏时间戳：本地时间/新鲜度/倒计时诚实显示占位；unknown 无原因 → 只有灰框徽章
	expect(rowB).toHaveTextContent("—");
	expect(rowB).not.toHaveTextContent(/彩 /);
	const unknownBadge = within(rowB).getByTestId("had-quote-unknown");
	expect(unknownBadge).toHaveTextContent("证据未知");
	expect(unknownBadge).not.toHaveTextContent("无欧赔");

	// 已开赛：时间格琥珀"已开赛"；拒绝但无原因 → 只有红徽章
	expect(within(rowC).getByText("已开赛")).toHaveClass("text-warning");
	const rejectedBadge = within(rowC).getByTestId("had-quote-rejected");
	expect(rejectedBadge).toHaveTextContent("拒绝");
	expect(rejectedBadge).not.toHaveTextContent("已停售");
	// 欧共识三元组含缺失向 → ?? 0 兜底显示
	expect(rowA).toHaveTextContent("50/0/20");
});

test("shows the loading skeleton before data arrives", async () => {
	server.use(http.get("*/api/v1/fixtures/today", () => new Promise<Response>(() => {})));
	await renderAt("/fixtures");

	expect(await screen.findByTestId("fixtures-loading")).toBeInTheDocument();
	expect(screen.queryByTestId("empty-state")).not.toBeInTheDocument();
	expect(screen.queryByTestId("fixtures-eligible")).not.toBeInTheDocument();
});

test("shows the unified no-data state when nothing is on sale, and refresh recovers", async () => {
	const user = userEvent.setup();
	server.use(http.get("*/api/v1/fixtures/today", () => HttpResponse.json([])));

	await renderAt("/fixtures");

	const state = await screen.findByTestId("empty-state");
	expect(state).toHaveAttribute("data-variant", "no-data");
	expect(state).toHaveTextContent("3 天内无在售场次。");

	// 唯一动作：刷新后数据恢复
	server.use(http.get("*/api/v1/fixtures/today", () => HttpResponse.json(todayFixture)));
	await user.click(within(state).getByRole("button", { name: "刷新" }));
	expect(await screen.findAllByTestId("fixtures-row")).toHaveLength(3);
});

test("shows the backend-unavailable state with startup guidance and retry", async () => {
	const user = userEvent.setup();
	server.use(http.get("*/api/v1/fixtures/today", () => HttpResponse.json(todayFixture, { status: 503 })));

	await renderAt("/fixtures");

	const state = await screen.findByTestId("empty-state");
	expect(state).toHaveAttribute("data-variant", "backend-unavailable");
	// 启动指引保留：task server + ingest（票 03 空状态定稿）
	expect(state).toHaveTextContent("task server");
	expect(state).toHaveTextContent("task ingest-jingcai");

	// 唯一动作：重试成功后回到正常表
	server.use(http.get("*/api/v1/fixtures/today", () => HttpResponse.json(todayFixture)));
	await user.click(within(state).getByRole("button", { name: "重试" }));
	expect(await screen.findAllByTestId("fixtures-row")).toHaveLength(3);
});

test("day tabs default to today and carry per-day counts (ticket wb-01)", async () => {
	await renderAt("/fixtures");

	await screen.findAllByTestId("fixtures-row");
	const tabs = screen.getByTestId("fixtures-day-tabs");
	// 今天 3 场（fixture 1-3）、明天 1 场（fixture 4）、后天 0——计数随行内 business_date 推导
	expect(within(tabs).getByRole("button", { name: /^今天/ })).toHaveTextContent("3");
	expect(within(tabs).getByRole("button", { name: /^明天/ })).toHaveTextContent("1");
	expect(within(tabs).getByRole("button", { name: /^后天/ })).toHaveTextContent("0");
	expect(within(tabs).getByRole("button", { name: /^今天/ })).toHaveAttribute("aria-pressed", "true");
	expect(screen.getAllByTestId("fixtures-row")).toHaveLength(3);
});

test("switching to tomorrow shows only that day; an empty window day keeps an honest note", async () => {
	const user = userEvent.setup();
	await renderAt("/fixtures");

	await screen.findAllByTestId("fixtures-row");
	await user.click(within(screen.getByTestId("fixtures-day-tabs")).getByRole("button", { name: /^明天/ }));
	expect(screen.getAllByTestId("fixtures-row")).toHaveLength(1);
	expect(screen.getByTestId("fixtures-card-4")).toBeInTheDocument();
	expect(screen.getAllByText("周日004").length).toBeGreaterThan(0);

	await user.click(within(screen.getByTestId("fixtures-day-tabs")).getByRole("button", { name: /^后天/ }));
	expect(screen.queryByTestId("fixtures-row")).not.toBeInTheDocument();
	expect(screen.getByTestId("fixtures-day-empty")).toHaveTextContent("后天暂无在售场次");
});

test("selection basket spans the 3-day window", async () => {
	const user = userEvent.setup();
	await renderAt("/fixtures");

	// 今天选一腿 + 切到明天再选一腿 = 合法 2串1（不同场次，均未开赛）
	await user.click(await screen.findByTestId("pick-1-h"));
	await user.click(within(screen.getByTestId("fixtures-day-tabs")).getByRole("button", { name: /^明天/ }));
	await user.click(await screen.findByTestId("pick-4-h"));
	expect(screen.getByTestId("basket-count")).toHaveTextContent("2/2");
	expect(screen.getByTestId("basket-summary")).toHaveTextContent("周六001 主胜 × 周日004 主胜");
});
