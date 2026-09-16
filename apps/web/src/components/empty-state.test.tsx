import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { EmptyState } from "./empty-state";

test("renders message, hint and the variant marker", () => {
	render(<EmptyState variant="no-data" message="当日无在售场次。" hint="场次通常在开售前更新。" />);

	const state = screen.getByTestId("empty-state");
	expect(state).toHaveAttribute("data-variant", "no-data");
	expect(state).toHaveTextContent("当日无在售场次。");
	expect(state).toHaveTextContent("场次通常在开售前更新。");
});

test("renders a single action button wired to the retry callback", async () => {
	const user = userEvent.setup();
	const onRetry = vi.fn();
	render(
		<EmptyState variant="backend-unavailable" message="连不上后端。" action={{ label: "重试", onClick: onRetry }} />,
	);

	await user.click(screen.getByRole("button", { name: "重试" }));
	expect(onRetry).toHaveBeenCalledOnce();
});

test("omits the action entirely for not-available stubs without one", () => {
	render(<EmptyState variant="not-available" message="复核页随 M3 上线。" />);

	expect(screen.getByTestId("empty-state")).toHaveTextContent("复核页随 M3 上线。");
	expect(screen.queryByRole("button")).not.toBeInTheDocument();
	expect(screen.queryByRole("link")).not.toBeInTheDocument();
});
