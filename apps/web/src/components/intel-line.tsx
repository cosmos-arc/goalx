import type { IntelItem } from "../api/goalx";
import { shortTime } from "../lib/ui";

/**
 * 一条情报行：kind + 文本 + 来源/时点徽章（review-20260928 票 08 批二：
 * evidence-chain.IntelRow 与 evidence-card.IntelLine 的逐字双副本合一，
 * testid 经 prop 注入保持 e2e 锚定）。
 */
export function IntelLine({ intel, testid }: { intel: IntelItem; testid: string }) {
	return (
		<li className="border-l-2 border-info/40 pl-2.5 text-xs" data-testid={testid}>
			<span className="mr-1.5 rounded border border-border px-1 text-muted-foreground">{intel.kind}</span>
			{intel.text}
			<span className="ml-2 inline-flex gap-1 align-baseline">
				<span className="rounded bg-info/10 px-1 text-info">{intel.source}</span>
				<span className="rounded border border-border px-1 text-muted-foreground">
					采集 {shortTime(intel.collected_at)}
				</span>
			</span>
		</li>
	);
}
