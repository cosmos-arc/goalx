import type { ReactNode } from "react";

/**
 * 页内固定浮层消息（role=status）：四页（场次/胜平负/进球/研究）原逐字
 * 复制的 toast div 抽取（review-20260928 票 08）；testid 经 prop 注入，
 * e2e 锚定不变。
 */
export function StatusToast({ testid, children }: { testid: string; children: ReactNode }) {
	return (
		<div
			role="status"
			data-testid={testid}
			className="fixed inset-x-0 bottom-16 z-50 mx-auto w-fit max-w-[min(92vw,42rem)] rounded-md bg-muted px-3 py-1.5 text-sm text-foreground shadow-sm"
		>
			{children}
		</div>
	);
}
