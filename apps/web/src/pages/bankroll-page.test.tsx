import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { expect, test } from "vitest";
import { server } from "../mocks/server";
import { router } from "../router";

async function renderBankroll() {
	await router.navigate({ to: "/bankroll" });
	const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
	return render(
		<QueryClientProvider client={queryClient}>
			<RouterProvider router={router} />
		</QueryClientProvider>,
	);
}

test("shows balance and recent events", async () => {
	renderBankroll();

	expect(await screen.findByTestId("bankroll-balance")).toHaveTextContent("¥5004.20");
	const events = screen.getAllByTestId("bankroll-event");
	expect(events).toHaveLength(2);
	expect(screen.getByText("兑付")).toBeInTheDocument();
});

test("shows an unseeded state before any deposit", async () => {
	server.use(http.get("*/api/v1/bankroll", () => HttpResponse.json({ balance: null, events: [] })));

	await renderBankroll();

	expect(await screen.findByTestId("bankroll-balance")).toHaveTextContent("尚未入金");
});

test("shows a degraded hint when the backend is unreachable", async () => {
	server.use(http.get("*/api/v1/bankroll", () => HttpResponse.json({ detail: "no" }, { status: 503 })));

	await renderBankroll();

	expect(await screen.findByTestId("bankroll-error")).toBeInTheDocument();
});
