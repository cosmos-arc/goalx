import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { expect, test } from "vitest";
import { poolPeriodDetailFixture } from "../mocks/handlers";
import { server } from "../mocks/server";
import { router } from "../router";
import { combinations } from "./market-pool-page";

/**
 * 票 43 14场任9 页点亮测试：真实期次/对阵/分布（源B 同步）、三向
 * 概率-份额-估计赔率-EV 链路、AI 代采待命占位、纸面池票提交接线、
 * 红线（真金档禁用）、留位三块与降级路径。
 * 默认 mock：期次 26999（在售 14 场全量分布）+ 26998（已开赛）；bankroll 5004.2。
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

test("caliber line shows pool-parimutuel wording and sync info row", async () => {
	await renderAt("/markets/pool");

	expect(await screen.findByRole("heading", { name: "GoalX · 14场任9" })).toBeVisible();
	// 口径行 = 后端 caliber 原样（估计派彩赔率/份额/未建模说明；等 detail 到位）
	expect(await screen.findByText(/未建模 price impact 与分彩风险/)).toBeVisible();
	const caliber = screen.getByTestId("pool-caliber");
	expect(caliber).toHaveTextContent("返奖率 65%");
	// 同步状态行：上次同步时点/来源 + 触发按钮
	expect(screen.getByTestId("pool-sync-info")).toHaveTextContent("上次同步");
	expect(screen.getByTestId("pool-sync-info")).toHaveTextContent("okooo.com");
	expect(screen.getByTestId("pool-sync-run")).toBeEnabled();
	// MarketTabs 三入口在页内且当前页点亮
	const tabs = screen.getByTestId("market-tabs");
	expect(within(tabs).getByRole("link", { name: "14场任9" })).toHaveAttribute("aria-current", "page");
});

test("real periods render 14 matches with probability/share/odds/ev chain", async () => {
	await renderAt("/markets/pool");

	// 期次下拉含真实期次（在售 + 已开赛标记）；默认选在售期
	const select = await screen.findByTestId("pool-period-select");
	expect(select).toHaveValue("26999");
	const option98 = within(select).getByRole("option", { name: /26998/ });
	expect(option98).toHaveTextContent("已开赛");

	// 14 场真实槽位（无虚线空槽——首期完整数据 → 骨架横幅已移除）
	await screen.findByTestId("pool-slot-1");
	const slots = screen.getByTestId("pool-slots");
	expect(within(slots).getAllByTestId(/^pool-slot-\d+$/)).toHaveLength(14);
	expect(slots.querySelectorAll('[data-testid^="pool-slot-empty-"]')).toHaveLength(0);
	expect(screen.queryByTestId("pool-skeleton-banner")).toBeNull();

	// 场 1 三向链路：概率 + 份额 + 估计赔率@ + EV；推荐/搏冷标记
	const pickGroup = screen.getByTestId("pool-pick-1");
	expect(within(pickGroup).getByText(/推荐/)).toBeInTheDocument();
	expect(within(pickGroup).getByText(/搏冷/)).toBeInTheDocument();
	const home = screen.getByTestId("pool-pick-1-h");
	expect(home).toHaveTextContent("45%"); // 概率
	expect(home).toHaveTextContent("份50%"); // 公众份额
	expect(home).toHaveTextContent("@1.30"); // 估计派彩赔率 = 0.65/0.5
	expect(home).toHaveTextContent("EV-42%"); // 0.45×1.3−1
});

test("picks are one-per-match toggles and prefill fills best selections", async () => {
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");

	// 一场一选：点主胜 → 选中；再点主胜 = 取消
	await userEvent.click(screen.getByTestId("pool-pick-1-h"));
	expect(screen.getByTestId("pool-pick-1-h")).toHaveAttribute("aria-pressed", "true");
	expect(screen.getByTestId("pool-pick-count")).toHaveTextContent("已选 1/14");
	await userEvent.click(screen.getByTestId("pool-pick-1-h"));
	expect(screen.getByTestId("pool-pick-count")).toHaveTextContent("已选 0/14");

	// 预选：概率最高向填前 9 场（全部场有概率）→ C(9,9) = 1 注
	await userEvent.click(screen.getByTestId("pool-prefill-recommended"));
	expect(screen.getByTestId("pool-pick-count")).toHaveTextContent("已选 9/14");
	expect(screen.getByTestId("pool-pick-count")).toHaveTextContent("1 注");
	expect(screen.getByTestId("pool-pick-1-h")).toHaveAttribute("aria-pressed", "true");
	// 场 2 概率最高 = 负 36%（> 主 32%）——概率而非份额驱动推荐
	expect(screen.getByTestId("pool-pick-2-a")).toHaveAttribute("aria-pressed", "true");
	// 手动补选第 10 场 → C(10,9) = 10 注
	await userEvent.click(screen.getByTestId("pool-pick-10-h"));
	expect(screen.getByTestId("pool-pick-count")).toHaveTextContent("10 注");
	// 组合数函数口径自检：C(14,9) = 2002
	expect(combinations(14, 9)).toBe(2002);
	expect(combinations(8, 9)).toBe(0);
});

test("switching the period resets picks", async () => {
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");

	await userEvent.click(screen.getByTestId("pool-pick-1-h"));
	expect(screen.getByTestId("pool-pick-count")).toHaveTextContent("已选 1/14");
	await userEvent.selectOptions(screen.getByTestId("pool-period-select"), "26998");
	expect(screen.getByTestId("pool-pick-count")).toHaveTextContent("已选 0/14");
});

test("sales block shows AI-agent-pending placeholder without agent data", async () => {
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");

	// 无代采销量 → 兜底层占位（不整页骨架；对阵照常点亮）
	const pending = screen.getByTestId("pool-sales-agent-pending");
	expect(pending).toHaveTextContent("AI 代采待命");
	expect(pending).toHaveTextContent("官方销量");
});

test("tier mapping is paper-flat only with live tiers disabled", async () => {
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");
	await userEvent.click(screen.getByTestId("pool-prefill-recommended"));

	// 红线：只呈现 flat 纸面档；标准/激进 live 档禁用占位
	const mapping = screen.getByTestId("pool-tier-mapping");
	expect(mapping).toHaveTextContent("只纸面低敞口");
	expect(mapping).toHaveTextContent("flat");
	const tiers = screen.getByTestId("pool-tiers");
	const conservative = within(tiers).getByTestId("pool-tier-conservative");
	expect(conservative).toHaveTextContent("保守档（flat，纸面）");
	expect(within(tiers).getByTestId("pool-tier-live-disabled")).toHaveTextContent("真金未开放");
	// mock 组合联合 EV 为负（任9 抽水后常态）→ 保守档诚实 ¥0
	await within(conservative).findByText("¥0.00");
	expect(within(conservative).getByTestId("pool-tier-conservative-reason")).toHaveTextContent("建议不投");
});

test("submit wires pool-slips as paper-only and reports the created slip", async () => {
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");

	// 不足 9 场：disabled + 说明
	const submit = screen.getByTestId("pool-submit");
	expect(submit).toBeDisabled();
	expect(submit).toHaveTextContent("任9 需 ≥9 场");

	await userEvent.click(screen.getByTestId("pool-prefill-recommended"));
	expect(submit).toBeEnabled();
	expect(submit).toHaveTextContent("1 注 × ¥2");

	let captured: { mode?: string; picks?: Array<{ match_seq: number; selection_code: string }> } = {};
	server.use(
		http.post("*/api/v1/pool-slips", async ({ request }) => {
			captured = (await request.json()) as typeof captured;
			return HttpResponse.json(
				{
					id: 902,
					mode: "paper",
					placed_at: null,
					note: null,
					created_at: "t",
					bet_count: 0,
					stake_total: 0,
					profit_total: 0,
				},
				{ status: 201 },
			);
		}),
	);
	await userEvent.click(submit);
	expect(await screen.findByTestId("pool-submit-result")).toHaveTextContent("已建纸面池票 #902");
	// 提交载荷：mode=paper + 官方池码（h→3/d→1/a→0）
	expect(captured.mode).toBe("paper");
	expect(captured.picks?.[0]).toEqual({ match_seq: 1, selection_code: "3" });
});

test("coming-soon blocks stay as reserved placeholders", async () => {
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");

	const coming = screen.getByTestId("pool-coming-soon");
	const blocks = within(coming).getAllByTestId("empty-state");
	expect(blocks).toHaveLength(3);
	expect(blocks[0]).toHaveTextContent("AI 证据总结");
	expect(blocks[0]).toHaveAttribute("data-variant", "not-available");
	expect(blocks[1]).toHaveTextContent("目标金额反推");
	expect(blocks[2]).toHaveTextContent("搏冷模式生成器");
});

test("backend-unavailable degrades honestly with dashed slots", async () => {
	server.use(http.get("*/api/v1/pool/periods", () => HttpResponse.error()));
	await renderAt("/markets/pool");

	const degraded = await waitFor(() => {
		const found = screen
			.getAllByTestId("empty-state")
			.filter((node) => node.getAttribute("data-variant") === "backend-unavailable");
		expect(found).toHaveLength(1);
		return found[0] as HTMLElement;
	});
	// 重试动作接 refetch（点击不炸）；无期次时槽位区不渲染、留位三块照常
	await userEvent.click(within(degraded).getByRole("button", { name: "重试" }));
	expect(screen.queryByTestId("pool-slots")).toBeNull();
	expect(within(screen.getByTestId("pool-coming-soon")).getAllByTestId("empty-state")).toHaveLength(3);
});

test("empty period list degrades to an honest no-data state", async () => {
	server.use(http.get("*/api/v1/pool/periods", () => HttpResponse.json([])));
	await renderAt("/markets/pool");

	const empty = await waitFor(() => {
		const found = screen
			.getAllByTestId("empty-state")
			.filter((node) => node.getAttribute("data-variant") === "no-data");
		expect(found).toHaveLength(1);
		return found[0] as HTMLElement;
	});
	expect(empty).toHaveTextContent("尚无彩池期次数据");
	expect(screen.queryByTestId("pool-slots")).toBeNull();
});

test("agent-imported sales light up the sales block", async () => {
	server.use(
		http.get("*/api/v1/pool/periods/:periodNo", () =>
			HttpResponse.json({
				...poolPeriodDetailFixture,
				state: {
					sales_amount: 12345678.5,
					rollover_in: 654321.0,
					published_at: "2026-09-21T12:00:00Z",
					source: "agent",
				},
			}),
		),
	);
	await renderAt("/markets/pool");

	expect(await screen.findByText(/12,345,678\.5/)).toBeVisible();
	const sales = screen.getByTestId("pool-sales");
	expect(sales).toHaveTextContent("官方销量");
	expect(sales).toHaveTextContent("AI 代采");
	expect(sales).toHaveTextContent("12,345,678.5");
	expect(sales).toHaveTextContent("滚存转入");
	expect(screen.queryByTestId("pool-sales-agent-pending")).toBeNull();
});

test("partial period keeps dashed slots with an honest note", async () => {
	server.use(
		http.get("*/api/v1/pool/periods/:periodNo", () =>
			HttpResponse.json({
				...poolPeriodDetailFixture,
				matches: poolPeriodDetailFixture.matches.slice(0, 12),
			}),
		),
	);
	await renderAt("/markets/pool");

	await screen.findByTestId("pool-slot-1");
	const slots = screen.getByTestId("pool-slots");
	expect(within(slots).getAllByTestId(/^pool-slot-\d+$/)).toHaveLength(12);
	const empty = within(slots).getAllByTestId(/^pool-slot-empty-/);
	expect(empty).toHaveLength(2);
	expect(empty[0]).toHaveTextContent("源未发布或期次不完整");
});

test("sync run failure shows an error hint without breaking the page", async () => {
	server.use(http.post("*/api/v1/pool-sync/run", () => HttpResponse.json({ detail: "x" }, { status: 502 })));
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");

	await userEvent.click(screen.getByTestId("pool-sync-run"));
	expect(await screen.findByTestId("pool-sync-error")).toHaveTextContent("同步失败");
	expect(screen.getByTestId("pool-slot-1")).toBeVisible();
});

test("a pick without distribution data degrades the tier input honestly", async () => {
	// 场 1 无份额（预售期无人气）：预选含该场 → 联合赔率毒化 → 缺数据不给比例建议
	server.use(
		http.get("*/api/v1/pool/periods/:periodNo", () =>
			HttpResponse.json({
				...poolPeriodDetailFixture,
				matches: poolPeriodDetailFixture.matches.map((match) =>
					match.match_seq === 1
						? {
								...match,
								selections: match.selections.map((sel) => ({
									...sel,
									share: null,
									implied_odds: null,
									ev: null,
								})),
							}
						: match,
				),
			}),
		),
	);
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");

	// 场 1 概率仍在（推荐可标）但无份额/赔率/EV
	const home = screen.getByTestId("pool-pick-1-h");
	expect(home).toHaveTextContent("45%");
	expect(home).not.toHaveTextContent("@");

	await userEvent.click(screen.getByTestId("pool-prefill-recommended"));
	const conservative = screen.getByTestId("pool-tier-conservative");
	await waitFor(() => {
		expect(within(conservative).getByTestId("pool-tier-conservative-reason")).toHaveTextContent("缺 EV/赔率数据");
	});
});

test("submit failure reports the error honestly without crashing", async () => {
	server.use(http.post("*/api/v1/pool-slips", () => HttpResponse.json({ detail: "boom" }, { status: 500 })));
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");
	await userEvent.click(screen.getByTestId("pool-prefill-recommended"));

	await userEvent.click(screen.getByTestId("pool-submit"));
	expect(await screen.findByTestId("pool-submit-result")).toHaveTextContent("提交失败");
});
