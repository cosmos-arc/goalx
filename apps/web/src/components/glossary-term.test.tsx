import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";
import { GlossaryTerm } from "./glossary-term";

/** 票 18：GlossaryTerm 键盘可达——聚焦触发器即出 Popover（定义+判读方向），aria-describedby 关联。 */
test("renders dashed-underline trigger and opens definition on keyboard focus", async () => {
	const user = userEvent.setup();
	render(<GlossaryTerm id="forward-inclusion">"前瞻纳入"</GlossaryTerm>);

	const trigger = screen.getByTestId("glossary-term-forward-inclusion");
	expect(trigger.tagName).toBe("BUTTON");
	expect(trigger).toHaveTextContent("前瞻纳入");
	expect(trigger).toHaveClass("decoration-dashed");

	// 关闭态不渲染气泡内容
	expect(screen.queryByText(/前瞻验证只纳入/)).not.toBeInTheDocument();

	// 键盘聚焦 → 气泡打开，含定义 + 判读方向
	await user.tab();
	expect(screen.getByText(/前瞻验证只纳入/)).toBeInTheDocument();
	expect(screen.getByText(/判读：纳入 = 开球前发出/)).toBeInTheDocument();

	// aria-describedby 关联气泡（读屏可达）
	const describedBy = trigger.getAttribute("aria-describedby");
	expect(describedBy).toBeTruthy();
	if (describedBy) {
		expect(document.getElementById(describedBy)).not.toBeNull();
	}

	// 失焦关闭
	await user.tab();
	expect(screen.queryByText(/前瞻验证只纳入/)).not.toBeInTheDocument();
});

test("defaults the trigger label to the entry term", () => {
	render(<GlossaryTerm id="clv" />);
	expect(screen.getByTestId("glossary-term-clv")).toHaveTextContent("CLV");
});

test("unknown ids fail fast instead of rendering a dead tooltip", () => {
	expect(() => render(<GlossaryTerm id="nope" />)).toThrow(/nope/);
});
