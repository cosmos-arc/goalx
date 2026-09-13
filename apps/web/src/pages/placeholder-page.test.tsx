import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { router } from "../router";

test("announces the milestone that will fill the page", async () => {
	await router.navigate({ to: "/review" });
	const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
	render(
		<QueryClientProvider client={queryClient}>
			<RouterProvider router={router} />
		</QueryClientProvider>,
	);
	expect(await screen.findByRole("heading", { name: "GoalX · 复核" })).toBeInTheDocument();
	expect(screen.getByTestId("placeholder")).toHaveTextContent("M3");
});
