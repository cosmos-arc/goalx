import { AppShell } from "../components/app-shell";
import { EmptyState } from "../components/empty-state";
import { MarketTabs } from "../components/market-tabs";

/**
 * 玩法占位页（票 wb-03）：进球与 14场任9 先上引导态——EmptyState
 * not-available 说明这里是什么、何时来，不装死页。数据/玩法随票 04 / 07 落地。
 */
export function MarketPlaceholderPage({ title, message, hint }: { title: string; message: string; hint: string }) {
	return (
		<AppShell title={title}>
			<MarketTabs />
			<EmptyState variant="not-available" message={message} hint={hint} />
		</AppShell>
	);
}
