import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { expect, test } from "vitest";
import { todayFixture } from "../mocks/handlers";
import { server } from "../mocks/server";
import { router } from "../router";
import { combinations } from "./market-pool-page";

/**
 * 票 wb-07 14场任9 骨架页测试：诚实原则（骨架横幅/待数据槽位/占位口径标注）、
 * 期次选择（演示=业务日）、推荐/搏冷标记与预选、三档额度建议（复用票 wb-06
 * 端点的档位映射）、留位 not-available 与提交占位（disabled）。
 * 默认 mock：今天 3 场 + 明天 1 场（fixture 4）；bankroll 5004.2。
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

test("skeleton banner and caliber line make the page honestly a skeleton", async () => {
	await renderAt("/markets/pool");

	expect(await screen.findByRole("heading", { name: "GoalX · 14场任9" })).toBeVisible();
	// 横幅级骨架说明（页头）+ 占位口径行（概率=欧共识、标记=纯前端）
	const banner = screen.getByTestId("pool-skeleton-banner");
	expect(banner).toHaveTextContent("骨架页");
	expect(banner).toHaveTextContent("goalx-quant");
	expect(screen.getByTestId("pool-caliber")).toHaveTextContent("欧共识");
	expect(screen.getByTestId("pool-caliber")).toHaveTextContent("非生成器产出");
	// MarketTabs 三入口在页内且当前页点亮
	const tabs = screen.getByTestId("market-tabs");
	expect(within(tabs).getByRole("link", { name: "14场任9" })).toHaveAttribute("aria-current", "page");
});

test("period select aggregates demo business dates and empty slots stay dashed", async () => {
	await renderAt("/markets/pool");

	// 期次（演示）= 业务日聚合：默认今天；槽位 = 数据 3 场 + 第 4–14 场共 11 个虚线待数据
	expect(await screen.findByTestId("pool-slot-1")).toBeVisible();
	expect(screen.getByTestId("pool-period-note")).toHaveTextContent("演示");
	const slots = screen.getByTestId("pool-slots");
	expect(within(slots).getAllByTestId(/^pool-slot-empty-/)).toHaveLength(11);
	expect(within(slots).getByTestId("pool-slot-empty-14")).toHaveTextContent("第 14 场");

	// 切到"明天"（fixture 4）：1 场数据 + 13 空槽
	await userEvent.selectOptions(screen.getByTestId("pool-period-select"), todayFixture[3].business_date);
	expect(within(screen.getByTestId("pool-slots")).getAllByTestId(/^pool-slot-empty-/)).toHaveLength(13);
	expect(screen.getByTestId("pool-pick-count")).toHaveTextContent("已选 0/14");
});

test("picks are one-per-match toggles with recommended/cold markers visible", async () => {
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");

	// fixture 1 欧共识：h .529 最优（推荐标记）、a .223 最冷（搏冷标记）——纯前端标注
	const pickGroup = screen.getByTestId("pool-pick-1");
	expect(within(pickGroup).getByText(/推荐/)).toBeInTheDocument();
	expect(within(pickGroup).getByText(/搏冷/)).toBeInTheDocument();

	// 一场一选：点主胜 → 选中；再点主胜 = 取消
	await userEvent.click(screen.getByTestId("pool-pick-1-h"));
	expect(screen.getByTestId("pool-pick-1-h")).toHaveAttribute("aria-pressed", "true");
	expect(screen.getByTestId("pool-pick-count")).toHaveTextContent("已选 1/14");
	await userEvent.click(screen.getByTestId("pool-pick-1-h"));
	expect(screen.getByTestId("pool-pick-count")).toHaveTextContent("已选 0/14");

	// 换选同场另一向 = 替换（一场一选，任9 口径）
	await userEvent.click(screen.getByTestId("pool-pick-1-d"));
	expect(screen.getByTestId("pool-pick-1-d")).toHaveAttribute("aria-pressed", "true");
	await userEvent.click(screen.getByTestId("pool-pick-1-a"));
	expect(screen.getByTestId("pool-pick-1-a")).toHaveAttribute("aria-pressed", "true");
	expect(screen.getByTestId("pool-pick-1-d")).toHaveAttribute("aria-pressed", "false");
});

test("prefill picks the recommended marker of the first 9 data slots", async () => {
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");

	// 今天 3 场中 fixture 3 无欧共识概率 → 不预选；预选 1/2 两场（共识最高向）；
	// 组合数 C(2,9) = 0（<9 场不成注，诚实显示 0 注）
	await userEvent.click(screen.getByTestId("pool-prefill-recommended"));
	expect(screen.getByTestId("pool-pick-count")).toHaveTextContent("已选 2/14");
	expect(screen.getByTestId("pool-pick-count")).toHaveTextContent("0 注");
	expect(screen.getByTestId("pool-pick-1-h")).toHaveAttribute("aria-pressed", "true"); // 共识最高向
	expect(screen.getByTestId("pool-pick-2-a")).toHaveAttribute("aria-pressed", "true"); // .39 最高
});

test("switching the demo period resets picks; single-probability rows mark recommend only", async () => {
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");

	// 选一场后切期次：选择清零（期次切换重置，不跨期残留）
	await userEvent.click(screen.getByTestId("pool-pick-1-h"));
	expect(screen.getByTestId("pool-pick-count")).toHaveTextContent("已选 1/14");
	await userEvent.selectOptions(screen.getByTestId("pool-period-select"), todayFixture[3].business_date);
	expect(screen.getByTestId("pool-pick-count")).toHaveTextContent("已选 0/14");

	// fixture 4 三向概率俱全：推荐/搏冷标记成对出现（同一向既是最高又是最低不可能——三值互异）
	const pickGroup = screen.getByTestId("pool-pick-4");
	expect(within(pickGroup).getAllByText(/推荐/)).toHaveLength(1);
	expect(within(pickGroup).getAllByText(/搏冷/)).toHaveLength(1);
});

test("three tiers reuse the stake-advice endpoint with the agreed mapping", async () => {
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");
	await userEvent.click(screen.getByTestId("pool-prefill-recommended"));

	// 映射行页内标注（保守=flat / 标准=¼Kelly 2% / 激进=¼Kelly 5%）
	const mapping = screen.getByTestId("pool-tier-mapping");
	expect(mapping).toHaveTextContent("保守 = flat");
	expect(mapping).toHaveTextContent("标准 = ¼Kelly");
	expect(mapping).toHaveTextContent("5%");

	// 三档卡常驻（mock 下联合 EV 为负 → 三档诚实 ¥0；档位名+口径照常渲染）
	const tiers = screen.getByTestId("pool-tiers");
	for (const key of ["conservative", "standard", "aggressive"]) {
		expect(within(tiers).getByTestId(`pool-tier-${key}`)).toBeVisible();
	}
	const conservative = within(tiers).getByTestId("pool-tier-conservative");
	expect(conservative).toHaveTextContent("保守档（flat）");
	await within(conservative).findByText("¥0.00");
	expect(within(conservative).getByTestId("pool-tier-conservative-reason")).toHaveTextContent("建议不投");
});

test("tiers show flat amounts when the joint placeholder EV is positive", async () => {
	// 覆盖正 EV 路径：把窗口内三场的 EV 全部 mock 为正（联合口径为正 → 保守 flat ¥100.08）
	server.use(
		http.get("*/api/v1/fixtures/today", () =>
			HttpResponse.json(
				todayFixture.map((row) => ({
					...row,
					ev: row.ev === null ? null : { h: 0.05, d: 0.03, a: 0.04 },
				})),
			),
		),
	);
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");
	await userEvent.click(screen.getByTestId("pool-prefill-recommended"));

	// 保守档（flat，纸面红线）：bankroll 5004.2 × 2% = ¥100.08
	const tiers = screen.getByTestId("pool-tiers");
	expect(await within(tiers).findByTestId("pool-tier-conservative")).toHaveTextContent("¥100.08");
	// 标准/激进档 = live ¼Kelly（cap 2%/5%）：理由含 ¼ fractional Kelly（两档各一处）
	expect(await within(tiers).findAllByText(/¼ fractional Kelly/)).toHaveLength(2);
});

test("nine or more picks make real combination counts (prefill caps at 9)", async () => {
	// 10 场有数据的期次：预选封顶 9 场（break 分支）→ C(9,9) = 1 注起算
	server.use(
		http.get("*/api/v1/fixtures/today", () =>
			HttpResponse.json(
				Array.from({ length: 10 }, (_, i) => ({
					...todayFixture[0],
					fixture_id: i + 1,
					match_code: `周六00${String(i + 1).padStart(2, "0")}`,
				})),
			),
		),
	);
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");

	await userEvent.click(screen.getByTestId("pool-prefill-recommended"));
	expect(screen.getByTestId("pool-pick-count")).toHaveTextContent("已选 9/14");
	expect(screen.getByTestId("pool-pick-count")).toHaveTextContent("1 注");
	// 手动补选第 10 场 → C(10,9) = 10 注（复式组合数实算）
	await userEvent.click(screen.getByTestId("pool-pick-10-h"));
	expect(screen.getByTestId("pool-pick-count")).toHaveTextContent("10 注");
	// 组合数函数口径自检：C(14,9) = 2002
	expect(combinations(14, 9)).toBe(2002);
	expect(combinations(8, 9)).toBe(0);
});

test("coming-soon blocks and disabled submit say what arrives and why", async () => {
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");

	const coming = screen.getByTestId("pool-coming-soon");
	const blocks = within(coming).getAllByTestId("empty-state");
	expect(blocks).toHaveLength(3);
	expect(blocks[0]).toHaveTextContent("AI 证据总结");
	expect(blocks[0]).toHaveAttribute("data-variant", "not-available");
	expect(blocks[1]).toHaveTextContent("目标金额反推");
	expect(blocks[2]).toHaveTextContent("搏冷模式生成器");

	const submit = screen.getByTestId("pool-submit");
	expect(submit).toBeDisabled();
	expect(submit).toHaveTextContent("占位");
});

test("backend-unavailable degrades honestly while the skeleton stays visible", async () => {
	server.use(http.get("*/api/v1/fixtures/today", () => HttpResponse.error()));
	await renderAt("/markets/pool");

	// 降级空态 + 结构骨架照常（14 个虚线槽位 + 三档区引导；留位三块也是 empty-state）
	const degraded = await waitFor(() => {
		const found = screen
			.getAllByTestId("empty-state")
			.filter((node) => node.getAttribute("data-variant") === "backend-unavailable");
		expect(found).toHaveLength(1);
		return found[0] as HTMLElement;
	});
	// 重试动作接 refetch（点击不炸，仍停在骨架结构）
	await userEvent.click(within(degraded).getByRole("button", { name: "重试" }));
	expect(screen.getByTestId("pool-skeleton-banner")).toBeVisible();
	expect(screen.getByTestId("pool-slots").querySelectorAll('[data-testid^="pool-slot-empty-"]')).toHaveLength(14);
	expect(screen.getByTestId("pool-tiers-empty")).toBeVisible();
});
