import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { expect, test } from "vitest";
import { server } from "../mocks/server";
import { parlayAdviceInput, StakeAdviceNote } from "./stake-advice";

/**
 * 票 wb-06 建议仓位组件测试：展示后端建议（注额+档位理由）与三类诚实降级
 * （资金池读取失败 / 缺 EV 数据 / 建议服务不可用）。规则本身的穷举见后端
 * betting/staking 单测——前端只测输入组装（串关联合口径）与展示。
 */

function renderNote(props: Parameters<typeof StakeAdviceNote>[0]) {
	const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
	return render(
		<QueryClientProvider client={queryClient}>
			<StakeAdviceNote {...props} />
		</QueryClientProvider>,
	);
}

test("renders paper flat advice with tier reason from the backend", async () => {
	renderNote({
		mode: "paper",
		bankroll: 5_004.2,
		ev: 0.053,
		odds: 1.92,
		testid: "advice",
	});

	await screen.findByText("¥100.08");
	expect(screen.getByTestId("advice-reason")).toHaveTextContent("纸面期一律 flat（红线）");
	expect(screen.getByTestId("advice-reason")).toHaveTextContent("skill 过线前不启用");
	// 档位行：模式口径 + 词典锚点
	expect(screen.getByTestId("advice")).toHaveTextContent("纸面 flat");
});

test("renders live quarter-kelly advice including cap wording", async () => {
	renderNote({
		mode: "live",
		bankroll: 10_000,
		ev: 0.3,
		odds: 1.5,
		testid: "advice",
	});

	await screen.findByText("¥500.00");
	expect(screen.getByTestId("advice-reason")).toHaveTextContent("¼ fractional Kelly");
	expect(screen.getByTestId("advice-reason")).toHaveTextContent("截断");
	expect(screen.getByTestId("advice")).toHaveTextContent("真金 ¼Kelly");
});

test("renders zero advice with explicit wording when EV is non-positive", async () => {
	renderNote({
		mode: "live",
		bankroll: 10_000,
		ev: -0.02,
		odds: 2,
		testid: "advice",
	});

	await screen.findByText("¥0.00");
	expect(screen.getByTestId("advice-reason")).toHaveTextContent("建议不投");
});

test("degrades honestly when bankroll failed, EV missing, or advice API is down", async () => {
	// 资金池读取失败：不给数字，给降级说明
	renderNote({ mode: "paper", bankroll: null, ev: 0.05, odds: 2, testid: "no-bank" });
	expect(screen.getByTestId("no-bank-reason")).toHaveTextContent("资金池读取失败");
	expect(screen.getByText("—")).toBeInTheDocument();

	// 缺 EV 数据（如无欧赔共识的场）：无 EV 不伪造建议
	renderNote({ mode: "paper", bankroll: 100, ev: null, odds: 2, testid: "no-ev" });
	expect(screen.getByTestId("no-ev-reason")).toHaveTextContent("缺 EV/赔率数据");

	// 建议端点不可用（旧后端/服务掉线）：诚实占位可重试
	server.use(http.post("*/api/v1/stake-advice", () => HttpResponse.error()));
	renderNote({ mode: "paper", bankroll: 100, ev: 0.05, odds: 2, testid: "down" });
	await waitFor(() => expect(screen.getByTestId("down-reason")).toHaveTextContent("建议服务不可用"));
});

test("shows the caliber note verbatim (parlay whole-bet caliber etc.)", async () => {
	renderNote({
		mode: "paper",
		bankroll: 100,
		ev: 0.05,
		odds: 2,
		note: "串关注额=单关口径：联合 EV/联合赔率整注计算",
		testid: "with-note",
	});

	await screen.findByTestId("with-note");
	expect(screen.getByTestId("with-note")).toHaveTextContent("口径：串关注额=单关口径");
});

test("parlayAdviceInput computes joint odds and joint EV (independence assumed)", () => {
	expect(
		parlayAdviceInput([
			{ ev: 0.1, odds: 2 },
			{ ev: 0.05, odds: 3 },
		]),
	).toEqual({
		ev: 1.1 * 1.05 - 1,
		odds: 6,
	});
	// 任一腿缺 EV → 联合 EV 置 null（无数据不伪造），联合赔率照常
	expect(
		parlayAdviceInput([
			{ ev: null, odds: 2 },
			{ ev: 0.05, odds: 3 },
		]),
	).toEqual({
		ev: null,
		odds: 6,
	});
});
