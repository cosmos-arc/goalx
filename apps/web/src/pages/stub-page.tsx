import { AppShell } from "../components/app-shell";
import { EmptyState } from "../components/empty-state";
import type { AppRoute } from "../lib/ui";

type StubPageProps = {
	title: string;
	/** 一句话说明这里是什么。 */
	message: string;
	/** 何时上线（里程碑/票号）。 */
	hint?: string;
	/** 可选的站内引导动作（如总览占位引导去今日）。 */
	action?: { label: string; to: AppRoute };
};

/** 票 13：占位页统一形态——"功能未达"空状态，说明是什么 + 何时来，不装死页。 */
export function StubPage({ title, message, hint, action }: StubPageProps) {
	return (
		<AppShell title={title}>
			<EmptyState variant="not-available" message={message} hint={hint} action={action} />
		</AppShell>
	);
}
