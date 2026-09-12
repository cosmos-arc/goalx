import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { expect, test } from "vitest";
import { statusFixture } from "../mocks/handlers";
import { server } from "../mocks/server";
import { HomePage } from "./home-page";

function renderHome() {
	const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
	return render(
		<QueryClientProvider client={queryClient}>
			<HomePage />
		</QueryClientProvider>,
	);
}

test("renders the heading and live service status", async () => {
	renderHome();

	expect(await screen.findByRole("heading", { name: "GoalX" })).toBeInTheDocument();
	const status = await screen.findByTestId("status-ok");
	expect(status).toHaveTextContent("goalx-backend v0.1.0");
	expect(status).toHaveTextContent("testing");
});

test("shows a degraded hint when the backend is unreachable", async () => {
	server.use(http.get("*/api/v1/status", () => HttpResponse.json(statusFixture, { status: 503 })));

	renderHome();

	expect(await screen.findByTestId("status-error")).toBeInTheDocument();
	expect(screen.queryByTestId("status-ok")).not.toBeInTheDocument();
});
