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

	// 场 1 三向链路：概率 + 份额 + 估计赔率@ + EV；推荐标记（无冷门正 EV → 无搏冷）
	const pickGroup = screen.getByTestId("pool-pick-1");
	expect(within(pickGroup).getByText(/推荐/)).toBeInTheDocument();
	expect(within(pickGroup).queryByText(/搏冷/)).toBeNull();
	const home = screen.getByTestId("pool-pick-1-h");
	expect(home).toHaveTextContent("45%"); // 概率
	expect(home).toHaveTextContent("份50%"); // 公众份额
	expect(home).toHaveTextContent("@1.30"); // 估计派彩赔率 = 0.65/0.5
	expect(home).toHaveTextContent("EV-42%"); // 0.45×1.3−1

	// 场 2 价值判定搏冷：客胜份额 18%<25% 且 EV 0.34×3.61−1≈+23% 优于推荐位主胜
	const coldSlot = screen.getByTestId("pool-pick-2-a");
	expect(within(coldSlot).getByText(/搏冷/)).toHaveAttribute("title", expect.stringContaining("冷门正期望"));
	expect(within(coldSlot).getByText(/搏冷/)).toHaveAttribute("title", expect.stringContaining("EV+23%"));
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
	// 场 2 概率最高 = 主 42%（票 pool-v2/01 fixture：搏冷样本的推荐位与冷门位分离）
	expect(screen.getByTestId("pool-pick-2-h")).toHaveAttribute("aria-pressed", "true");
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

test("evidence cards render states with honest degradation (票 14 V1)", async () => {
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");

	const section = screen.getByTestId("pool-evidence");
	expect(within(section).getByTestId("pool-evidence-caliber")).toHaveTextContent("不装懂");
	// 场 1：analyst 已复核 → 融合线概率 + 2 条情报 + JS 徽章
	const card1 = within(section).getByTestId("evidence-card-1");
	expect(card1).toHaveAttribute("data-state", "analyst_done");
	expect(card1).toHaveTextContent("胜 48% / 平 25% / 负 27%");
	await userEvent.click(within(card1).getByRole("button"));
	const detail1 = within(section).getByTestId("evidence-detail-1");
	expect(within(detail1).getAllByTestId("evidence-intel")).toHaveLength(2);
	expect(within(detail1).getByText(/JS 0.072/)).toHaveTextContent("已入复核");
	expect(within(detail1).getByTestId("evidence-state-1")).toHaveTextContent("analyst 已复核");
	// 场 6：无情报无产出 → 诚实降级（不出概率，仅官方份额）
	const card6 = within(section).getByTestId("evidence-card-6");
	expect(card6).toHaveAttribute("data-state", "no_intel");
	expect(card6).toHaveTextContent("概率 —（仅官方份额）");
	await userEvent.click(within(card6).getByRole("button"));
	expect(within(section).getByTestId("evidence-degraded-6")).toHaveTextContent("不装懂");
	expect(within(section).getByTestId("evidence-state-6")).toHaveTextContent("无情报无产出");
});

test("evidence cards cover scout state and no_forecast degradation (票 14 V1)", async () => {
	server.use(
		http.get("*/api/v1/pool/periods/:periodNo/evidence-summary", () =>
			HttpResponse.json({
				period_no: "26999",
				market_code: "ttt14",
				generated_at: new Date().toISOString(),
				caliber: "证据卡 = 已存证工件渲染。",
				matches: [
					{
						match_seq: 2,
						fixture_id: 2,
						home_team: "利物浦",
						away_team: "曼城",
						league: "英超",
						kickoff_utc: new Date().toISOString(),
						forecast: {
							track: "llm",
							h: 0.36,
							d: 0.28,
							a: 0.36,
							issued_at: new Date().toISOString(),
							model_version: "glm:glm-5.3-flash",
							rationale: "双强对位平局权重上调",
							analyst: false,
						},
						intel_count: 1,
						intels: [
							{
								kind: "h2h",
								text: "近6次交锋主 3 胜",
								source: "fdhist:E0",
								collected_at: new Date().toISOString(),
							},
						],
						divergence: { js: 0.014, routed: false },
						state: "scout_done",
					},
					{
						match_seq: 3,
						fixture_id: 3,
						home_team: "维拉",
						away_team: "热刺",
						league: "英超",
						kickoff_utc: new Date().toISOString(),
						forecast: null,
						intel_count: 2,
						intels: [],
						divergence: { js: null, routed: false },
						state: "no_forecast",
					},
				],
			}),
		),
	);
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");

	const section = screen.getByTestId("pool-evidence");
	// scout 状态：LLM 轨概率 + 未路由 JS + 研判依据
	const card2 = within(section).getByTestId("evidence-card-2");
	expect(card2).toHaveAttribute("data-state", "scout_done");
	await userEvent.click(within(card2).getByRole("button"));
	expect(within(section).getByTestId("evidence-state-2")).toHaveTextContent("scout 已出概率");
	expect(within(section).getByText(/JS 0.014/)).toHaveTextContent("未路由");
	expect(within(section).getByTestId("evidence-detail-2")).toHaveTextContent("研判依据：双强对位");
	// no_forecast：有情报无产出 → 宁缺毋假降级文案
	const card3 = within(section).getByTestId("evidence-card-3");
	await userEvent.click(within(card3).getByRole("button"));
	expect(within(section).getByTestId("evidence-degraded-3")).toHaveTextContent("宁缺毋假");
});

test("evidence card section degrades honestly when the endpoint is missing (票 14 V1)", async () => {
	server.use(
		http.get("*/api/v1/pool/periods/:periodNo/evidence-summary", () =>
			HttpResponse.json({ detail: "not found" }, { status: 404 }),
		),
	);
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");

	const section = screen.getByTestId("pool-evidence");
	const empty = await within(section).findByTestId("empty-state");
	expect(empty).toHaveAttribute("data-variant", "not-available");
	expect(empty).toHaveTextContent("证据卡暂不可用");
});

test("target reverse builds a plan and adopting applies picks", async () => {
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");

	// 默认中档 ¥10000 → 任9 票面 + 建议注数 + 不承诺口径
	await userEvent.click(screen.getByTestId("pool-target-generate"));
	const result = await screen.findByTestId("pool-target-result");
	expect(result).toBeVisible();
	expect(screen.getByTestId("pool-target-ticket")).toHaveTextContent("中档票面（9 场）");
	const units = screen.getByTestId("pool-target-units");
	expect(units).toHaveTextContent(/建议 \d+ 注/);
	expect(units).toHaveTextContent("¥2/注");

	// 搏档：任9 + 场 2 冷替换（fixture 冷门样本）
	await userEvent.selectOptions(screen.getByTestId("pool-target-risk"), "bold");
	await userEvent.click(screen.getByTestId("pool-target-generate"));
	await waitFor(() => {
		expect(screen.getByTestId("pool-target-ticket")).toHaveTextContent("搏档票面（9 场）");
	});
	expect(screen.getByTestId("pool-target-ticket")).toHaveTextContent("第2场 胜→负");

	// 采用 → 9 场选择回填
	await userEvent.click(screen.getByTestId("pool-target-ticket-adopt"));
	expect(await screen.findByTestId("pool-target-message")).toHaveTextContent("已采用反推票面");
	expect(screen.getByTestId("pool-pick-count")).toHaveTextContent("已选 9/14");
});

test("panels degrade honestly on missing data and endpoint errors", async () => {
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");

	// 缺数据票面：估值字段全 null + 无冷门变体 → 票行降级文案 + 空变体说明
	server.use(
		http.post("*/api/v1/pool/cold-variants", () =>
			HttpResponse.json({
				period_no: "26999",
				base: { picks: { "1": "h" }, swaps: [], hit_prob: null, est_odds: null, ev: null },
				variants: [],
				caliber: "缺份额/概率的票面估值不可得（诚实降级）。",
			}),
		),
		http.post("*/api/v1/pool/target-plan", () =>
			HttpResponse.json({
				period_no: "26999",
				risk: "balanced",
				ticket: { picks: { "1": "h" }, swaps: [], hit_prob: null, est_odds: null, ev: null },
				est_payout_per_unit: null,
				suggested_units: 0,
				target_reached: false,
				note: "所选场次有缺份额/概率数据，估计派彩不可得——注数建议为 0（诚实降级）。",
				caliber: "不承诺目标达成。",
			}),
		),
	);
	await userEvent.click(screen.getByTestId("pool-generate"));
	expect(await screen.findByTestId("pool-variants")).toBeVisible();
	expect(screen.getByTestId("pool-variant-base")).toHaveTextContent("命中率缺数据");
	expect(screen.getByTestId("pool-variants-empty")).toHaveTextContent("无正期望冷门");
	await userEvent.click(screen.getByTestId("pool-target-generate"));
	expect(await screen.findByTestId("pool-target-result")).toBeVisible();
	expect(screen.getByTestId("pool-target-ticket")).toHaveTextContent("命中率缺数据");
	expect(screen.getByTestId("pool-target-units")).toHaveTextContent("缺数据");
	expect(screen.getByTestId("pool-target-units")).toHaveTextContent("0 注");

	// 端点错误：生成失败信息如实呈现（不崩、可重试）
	server.use(http.post("*/api/v1/pool/cold-variants", () => HttpResponse.error()));
	await userEvent.click(screen.getByTestId("pool-generate"));
	await waitFor(() => {
		expect(screen.getByTestId("pool-generator-error")).toBeInTheDocument();
	});
});

test("cold generator produces greedy variants and adopting applies picks", async () => {
	await renderAt("/markets/pool");
	await screen.findByTestId("pool-slot-1");

	// 默认冷度 2；fixture 场 2/5 为冷门样本（客胜 EV+23% 优于主胜推荐位）
	await userEvent.click(screen.getByTestId("pool-generate"));
	const variants = await screen.findByTestId("pool-variants");
	expect(screen.getByTestId("pool-variant-base")).toHaveTextContent("基础票（14 场）");
	const variant1 = within(screen.getByTestId("pool-variant-1"));
	expect(variant1.getByText(/1 处冷门/)).toBeVisible();
	expect(screen.getByTestId("pool-variant-1")).toHaveTextContent("第2场 胜→负");
	expect(screen.getByTestId("pool-variant-1")).toHaveTextContent("EV+");
	expect(screen.getByTestId("pool-variant-2")).toHaveTextContent("第2场");
	expect(screen.getByTestId("pool-variant-2")).toHaveTextContent("第5场");
	expect(variants).toBeVisible();

	// 采用变体 1 → 场 2 选择变为负（冷选项），其余保持基础票
	await userEvent.click(screen.getByTestId("pool-variant-1-adopt"));
	expect(await screen.findByTestId("pool-generator-message")).toHaveTextContent("已采用变体 1");
	expect(screen.getByTestId("pool-pick-2-a")).toHaveAttribute("aria-pressed", "true");
	expect(screen.getByTestId("pool-pick-2-h")).toHaveAttribute("aria-pressed", "false");
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
	// 重试动作接 refetch（点击不炸）；无期次时槽位区与证据卡区块都不渲染
	await userEvent.click(within(degraded).getByRole("button", { name: "重试" }));
	expect(screen.queryByTestId("pool-slots")).toBeNull();
	expect(screen.queryByTestId("pool-evidence")).toBeNull();
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
