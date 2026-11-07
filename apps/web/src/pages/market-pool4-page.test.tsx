import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { expect, test } from "vitest";
import type { PoolMatch } from "../api/goalx";
import { goals4PeriodDetailFixture } from "../mocks/handlers";
import { server } from "../mocks/server";
import { router } from "../router";
import { matchHitProb } from "./market-pool4-page";

/**
 * 票 10（report-audit-20261103）四场进球页测试：goals4 期次贯通、
 * 8 档选择面、同场联合=矩阵格聚合展示（连乘反例钉死）、复式计数、
 * 纸面提交闭环、生成器不放行（页面无入口）。
 * 默认 mock：期次 26226（在售 4 场，档位/联合已知格值）。
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

/** 逐场选 h1 + a1（联合格值 0.2，四场票级 = 0.2^4）。先等数据到位。 */
async function pickDefaultPairs(user: ReturnType<typeof userEvent.setup>) {
	await screen.findByTestId("pool4-band-1-h1");
	for (const seq of [1, 2, 3, 4]) {
		await user.click(screen.getByTestId(`pool4-band-${seq}-h1`));
		await user.click(screen.getByTestId(`pool4-band-${seq}-a1`));
	}
}

test("goals4 period loads with joint caliber and tab active", async () => {
	await renderAt("/markets/pool4");

	expect(await screen.findByRole("heading", { name: "GoalX · 四场进球" })).toBeVisible();
	const select = await screen.findByTestId("pool4-period-select");
	expect(select).toHaveValue("26226");
	// 口径行 = 后端 caliber 原样（联合=格聚合非连乘披露）
	const caliber = screen.getByTestId("pool4-caliber");
	expect(caliber).toHaveTextContent("比分矩阵格聚合");
	expect(caliber).toHaveTextContent("非边际连乘");
	// 四入口 tab 且当前页点亮
	const tabs = screen.getByTestId("market-tabs");
	expect(within(tabs).getByRole("link", { name: "四场进球" })).toHaveAttribute("aria-current", "page");
	expect(within(tabs).getByRole("link", { name: "14场任9" })).not.toHaveAttribute("aria-current");
	// 4 场 8 档面 + 档位概率展示（pct 一位小数口径）
	expect((await screen.findAllByTestId("pool4-match")).length).toBe(4);
	expect(screen.getByTestId("pool4-band-1-h2")).toHaveTextContent("2球");
	expect(screen.getByTestId("pool4-band-1-h2")).toHaveTextContent("40.0%");
});

test("selected pair shows matrix-aggregated joint, not marginal product", async () => {
	await renderAt("/markets/pool4");
	const user = userEvent.setup();

	await user.click(await screen.findByTestId("pool4-band-1-h1"));
	await user.click(screen.getByTestId("pool4-band-1-a1"));
	// 联合格值 h1a1=0.2（连乘口径会得 0.3×0.3=9%——格聚合钉死 20%）
	const pairLine = await screen.findByTestId("pool4-pair-joint");
	expect(pairLine).toHaveTextContent("h1/a1 = 20.0%");
	expect(pairLine).toHaveTextContent("矩阵格聚合");
});

test("complete four matches unlock submit with ticket joint probability", async () => {
	await renderAt("/markets/pool4");
	const user = userEvent.setup();

	await pickDefaultPairs(user);
	// 票级联合 = 0.2^4 = 0.16%（跨场独立假设披露）
	expect((await screen.findAllByTestId("pool4-pair-joint")).length).toBe(4);
	const count = screen.getByTestId("pool4-pick-count");
	expect(count).toHaveTextContent("4/4");
	await waitForText(count, "1 注");
	expect(count).toHaveTextContent("0.2%");
	expect(screen.getByTestId("pool4-submit-button")).toBeEnabled();
});

test("multi-band pick counts combinations and blocks single-pair advice", async () => {
	await renderAt("/markets/pool4");
	const user = userEvent.setup();

	await pickDefaultPairs(user);
	await screen.findAllByTestId("pool4-pair-joint");
	// 第 1 场主档复式（h0+h1）→ 组合数 2（mock 与后端同口径 |H|×|A| 积）
	await user.click(screen.getByTestId("pool4-band-1-h0"));
	const count = screen.getByTestId("pool4-pick-count");
	await waitForText(count, "2 注");
	expect(screen.getByTestId("pool4-submit-button")).toBeEnabled();
	// 复式场仍显示联合命中概率（按配对求和），不出单一票级建议
	expect(count).toHaveTextContent("跨场独立假设");
});

test("submit creates paper goals4 slip with backend-counted combinations", async () => {
	await renderAt("/markets/pool4");
	const user = userEvent.setup();

	await pickDefaultPairs(user);
	await screen.findAllByTestId("pool4-pair-joint");
	await waitFor(() => expect(screen.getByTestId("pool4-submit-button")).toBeEnabled());
	await user.click(screen.getByTestId("pool4-submit-button"));
	const result = await screen.findByTestId("pool4-submit-result");
	expect(result).toHaveTextContent("已建纸面池票 #901（1 注 × ¥2，合计 ¥2.00）");
});

test("incomplete picks keep submit disabled with honest hint", async () => {
	await renderAt("/markets/pool4");
	const user = userEvent.setup();

	const button = await screen.findByTestId("pool4-submit-button");
	expect(button).toBeDisabled();
	// 只选主档不成配对：计数 0、按钮仍禁用
	await user.click(await screen.findByTestId("pool4-band-1-h1"));
	expect(screen.getByTestId("pool4-pick-count")).toHaveTextContent("0 注");
	expect(button).toBeDisabled();
	expect(button).toHaveTextContent("需 4 场各选主/客档");
});

test("submit gate holds until the preview for changed picks completes (票 07)", async () => {
	await renderAt("/markets/pool4");
	const user = userEvent.setup();

	await pickDefaultPairs(user);
	const submit = screen.getByTestId("pool4-submit-button");
	await waitFor(() => {
		expect(submit).toBeEnabled();
		expect(submit).toHaveTextContent("1 注 × ¥2");
	});

	// 第 1 场主档复式（h0+h1）→ 新签名预览挂起：旧占位 1 注不可提交
	let release: (() => void) | undefined;
	const gate = new Promise<void>((resolve) => {
		release = resolve;
	});
	server.use(
		http.post("*/api/v1/pool-slips/preview", async ({ request }) => {
			const body = (await request.json()) as { picks?: unknown[] };
			const multi = (body.picks?.length ?? 0) > 8;
			if (multi) {
				await gate;
			}
			return HttpResponse.json({
				market_code: "goals4",
				combination_count: multi ? 2 : 1,
				stake_per_combination: 2,
				total_stake: multi ? 4 : 2,
				combinations_sha256: "preview-goals4-fixture",
			});
		}),
	);
	await user.click(screen.getByTestId("pool4-band-1-h0"));
	await waitFor(() => {
		expect(submit).toBeDisabled();
		expect(submit).toHaveTextContent("预览更新中");
	});
	expect(screen.getByTestId("pool4-preview-status")).toHaveTextContent("未完成前不可提交");

	release?.();
	await waitFor(() => {
		expect(submit).toBeEnabled();
		expect(submit).toHaveTextContent("2 注 × ¥2");
	});
});

/** 等到元素文本包含目标串（轮询避 flake；文本由查询回填异步更新）。 */
async function waitForText(element: HTMLElement, text: string) {
	await screen.findByText(
		(_, el) => el === element && element.textContent?.includes(text) === true,
		{},
		{ timeout: 3000 },
	);
}

test("goals4 ticket advice applies the return rate once (票 08 anchor +58.69%)", async () => {
	// 全档份额 0.4 → 配对份额 0.16，h1/a1 联合 0.2 → 每场 p/s=1.25：
	// 票级 EV = 0.65×1.25⁴−1 = +58.69%（报告二判别样例；串关连乘 R⁴ 会
	// 算成 −56.42% 判反）。捕获 stake-advice 请求体断言页面送出的口径。
	const bodies: Array<{ ev?: number; odds?: number }> = [];
	server.use(
		http.post("*/api/v1/stake-advice", async ({ request }) => {
			bodies.push((await request.json()) as { ev?: number; odds?: number });
			return HttpResponse.json({
				stake: 2,
				tier: "flat",
				fraction: null,
				full_kelly_fraction: null,
				capped: false,
				reason: "纸面 flat（测试桩）",
			});
		}),
		http.get("*/api/v1/pool/periods/:periodNo", ({ params, request }) => {
			if (params["periodNo"] !== "26226" || new URL(request.url).searchParams.get("market_code") !== "goals4") {
				return HttpResponse.json({ detail: "period not found" }, { status: 404 });
			}
			return HttpResponse.json({
				...goals4PeriodDetailFixture,
				matches: goals4PeriodDetailFixture.matches.map((match) => ({
					...match,
					selections: match.selections.map((sel) => ({
						...sel,
						share: 0.4,
						implied_odds: 1.625,
						ev: null,
					})),
				})),
			});
		}),
	);
	await renderAt("/markets/pool4");
	const user = userEvent.setup();

	await pickDefaultPairs(user); // 每场 h1+a1：联合 0.2、配对份额 0.16
	await waitFor(() => {
		if (bodies.length === 0) {
			throw new Error("stake-advice not called yet");
		}
	});
	const last = bodies[bodies.length - 1] as { ev?: number; odds?: number };
	expect(last.ev).toBeCloseTo(0.65 * 1.25 ** 4 - 1, 4); // +58.69%
	expect(last.odds).toBeCloseTo(0.65 / 0.16 ** 4, 1);
});

test("票 05 E9：任一配对缺联合 = null——null 在前不被后到数值对复活", () => {
	// 稀疏联合矩阵：h0×a0 缺（pairsOf 排序后首个配对 null）、h0×a1=0.3
	const match = {
		goals4_joint: { h0: { a1: 0.3 } },
	} as unknown as PoolMatch;
	// 旧实现 reduce (acc ?? 0) + joint：null 累加器被 0.3 复活 → 假概率
	expect(matchHitProb(match, ["h0", "a0", "a1"])).toBeNull();
	// 全配对齐 = 数值和（对照：复活 bug 修复不误伤正常路径）
	const full = { goals4_joint: { h0: { a0: 0.1, a1: 0.3 } } } as unknown as PoolMatch;
	expect(matchHitProb(full, ["h0", "a0", "a1"])).toBeCloseTo(0.4);
});
