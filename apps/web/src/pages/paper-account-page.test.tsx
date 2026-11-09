import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { paperAccountFixture } from "../mocks/handlers";
import { router } from "../router";

/**
 * auto-paper 票 04：纸面账户页——两策略分列不混数、样本不足横幅
 * （独立暴露<200，票 13 分母）、ROI 双栏（等注/按场）、浮仓单列、
 * 归集计税注记；纯函数 evGapText 缺数据显 — 不冒充。
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

test("renders strategy split, insufficient-sample banner, floating and tax note", async () => {
	await renderAt("/paper-account");

	expect(await screen.findByTestId("paper-account")).toBeInTheDocument();
	// 样本不足横幅（独立暴露 3 场 < 200，票 13 分母语义）
	const banner = screen.getByTestId("paper-account-insufficient");
	expect(banner).toHaveTextContent("样本不足");
	expect(banner).toHaveTextContent("独立暴露 3 场");
	expect(banner).toHaveTextContent("不判盈亏");
	// 两策略分列不混数
	const auto = screen.getByTestId("paper-account-auto-v1");
	expect(auto).toHaveTextContent("auto-v1（系统组合）");
	expect(auto).toHaveTextContent("2（2/0）");
	const manual = screen.getByTestId("paper-account-manual");
	expect(manual).toHaveTextContent("人工纸面");
	// ROI 双栏并列（等注 149900.0% 与按场 149900.0% 同现于行）
	expect(manual).toHaveTextContent("149900.0%");
	// 浮仓单列（open ¥5 + 池 ¥4）
	expect(screen.getByText("¥9.00")).toBeInTheDocument();
	// 税后注记（同期同游戏归集计税）
	expect(screen.getByTestId("paper-account-tax-note")).toHaveTextContent("¥3001.20");
	// 曲线要点按结算日
	expect(screen.getByTestId("paper-account-curve-auto-v1")).toHaveTextContent("2026-11-09 ¥1002 → 2026-11-10 ¥1000");
});

test("evGapText: 缺 EV 快照显 — 不冒充", async () => {
	const { evGapText } = await import("./paper-account-page");
	expect(evGapText(paperAccountFixture.strategies[1]!)).toBe("—");
	expect(evGapText(paperAccountFixture.strategies[0]!)).toBe("-5.0%");
});
