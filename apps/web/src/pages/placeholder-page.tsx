import { AppShell } from "../components/app-shell";

export function PlaceholderPage({ title, milestone }: { title: string; milestone: string }) {
	return (
		<AppShell title={title}>
			<p
				data-testid="placeholder"
				className="rounded-lg border border-dashed border-neutral-300 p-8 text-sm text-neutral-500"
			>
				{milestone} 落地此页。M1（数据地基）只实现今日页与投注/资金基础视图。
			</p>
		</AppShell>
	);
}
