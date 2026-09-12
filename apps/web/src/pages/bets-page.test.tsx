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

test("lists suggestions with unpurchased checkboxes and settled profit", async () => {
	renderBets();

	expect(await screen.findAllByTestId("bet-row")).toHaveLength(3);
	// 未购建议有回录勾选框，已购票显示「已购」
	expect(screen.getByLabelText("回录注 1")).toBeInTheDocument();
	expect(screen.getByText("已购")).toBeInTheDocument();
	// 已结算的串注显示盈利
	expect(screen.getByText("+26.60")).toBeInTheDocument();
	expect(screen.getByText("胜")).toBeInTheDocument();
	// live 未结注：真金标签、盈亏占位、状态 fallback
	expect(screen.getByText("真金")).toBeInTheDocument();
	expect(screen.getAllByText("—").length).toBeGreaterThan(0);
	expect(screen.getByText("部分")).toBeInTheDocument();
});

test("records a slip from the checked suggestion subset", async () => {
	const user = userEvent.setup();
	renderBets();

	await user.click(await screen.findByLabelText("回录注 1"));
	await user.click(screen.getByRole("button", { name: /回录选中的 1 注/ }));

	expect(await screen.findByTestId("bets-message")).toHaveTextContent("已回录票 #1（1 注");
});

test("imports a draw result and shows the settlement summary", async () => {
	const user = userEvent.setup();
	renderBets();

	await user.type(await screen.findByTestId("draw-fixture"), "3");
	await user.type(screen.getByTestId("draw-home"), "2");
	await user.type(screen.getByTestId("draw-away"), "1");
	await user.click(screen.getByRole("button", { name: "导入" }));

	expect(await screen.findByTestId("bets-message")).toHaveTextContent("已导入 1 条开奖结果");

	await user.click(screen.getByRole("button", { name: "结算批跑" }));
	expect(await screen.findByTestId("bets-message")).toHaveTextContent("结算完成：1 注落定");
});

test("surfaces purchase and import errors", async () => {
	const user = userEvent.setup();
	server.use(http.post("*/api/v1/bet-slips", () => HttpResponse.json({ detail: "nope" }, { status: 400 })));
	renderBets();

	await user.click(await screen.findByLabelText("回录注 1"));
	await user.click(screen.getByRole("button", { name: /回录选中的 1 注/ }));
	expect(await screen.findByTestId("bets-message")).toHaveTextContent("回录失败");

	server.use(http.post("*/api/v1/draw-results", () => HttpResponse.json({ detail: "404" }, { status: 404 })));
	await user.type(await screen.getByTestId("draw-fixture"), "99");
	await user.type(screen.getByTestId("draw-home"), "1");
	await user.type(screen.getByTestId("draw-away"), "1");
	await user.click(screen.getByRole("button", { name: "导入" }));
	expect(await screen.findByTestId("bets-message")).toHaveTextContent("导入失败");
});

test("surfaces settlement errors", async () => {
	const user = userEvent.setup();
	server.use(http.post("*/api/v1/settlements/run", () => HttpResponse.json({ detail: "x" }, { status: 500 })));
	renderBets();

	await user.click(await screen.findByRole("button", { name: "结算批跑" }));
	expect(await screen.findByTestId("bets-message")).toHaveTextContent("后端不可用");
});

test("shows the no-suggestions state after everything is purchased", async () => {
	server.use(http.get("*/api/v1/bets", () => HttpResponse.json([])));
	renderBets();

	expect(await screen.findByText("无未购建议。")).toBeInTheDocument();
});
