import { useMemo, useState } from "react";
import { AppShell } from "../components/app-shell";
import { EmptyState } from "../components/empty-state";
import { Badge } from "../components/ui/badge";
import { filterGlossary, type GlossaryEntry } from "../lib/glossary";

/**
 * 票 18：词典页落地（票 08 定稿形态 = 唯一事实源）。检索框（按 term/aliases/
 * definition 过滤）+ 词条卡列表；词条卡三要素齐全才上线：term + definition +
 * 判读方向徽章 + 数字实例 + 口径警示（警示逐条来自 glossary.md，不得删减）。
 * 无检索结果走 EmptyState no-data（票 13 三态统一，一句人话 + 清检索动作）。
 * 内容源 = src/lib/glossary.ts（票 08 定稿 TS 常量模块，口径以 docs/plans 词典为准）。
 */

/** 词条卡：term + 判读方向徽章 + 定义 + 别名 + 实例 + 口径警示（三要素齐全）。 */
function GlossaryCard({ entry }: { entry: GlossaryEntry }) {
	return (
		<article
			className="flex h-full flex-col gap-2 rounded-lg border border-border bg-card p-4"
			data-testid={`glossary-card-${entry.id}`}
		>
			<div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-1">
				<h3 className="text-sm font-medium">{entry.term}</h3>
				{/* 判读方向徽章（票 02/18：指标呈现必附判读方向）：长文本允许换行，不做定高药丸 */}
				<Badge variant="outline" className="h-auto items-start whitespace-normal border-transparent bg-muted py-1">
					判读方向 · {entry.direction}
				</Badge>
			</div>
			<p className="text-sm leading-relaxed">{entry.definition}</p>
			{entry.aliases.length > 0 ? (
				<p className="text-xs text-muted-foreground">别名：{entry.aliases.join(" / ")}</p>
			) : null}
			<p className="text-xs leading-relaxed text-muted-foreground">
				<span className="font-medium text-foreground">例：</span>
				{entry.example}
			</p>
			{/* 口径警示：琥珀 wash + 前景色文字（票 14/15 教训：低透明底上文字用前景色过 AA） */}
			<p className="mt-auto rounded-md bg-warning/10 px-2.5 py-1.5 text-xs leading-relaxed text-foreground">
				<span className="font-medium">注意</span> · {entry.caution}
			</p>
		</article>
	);
}

export function GlossaryPage() {
	const [query, setQuery] = useState("");
	const results = useMemo(() => filterGlossary(query), [query]);

	return (
		<AppShell title="词典">
			<div className="pb-4">
				<header className="mb-5">
					<p className="text-sm text-muted-foreground">
						指标口径与判读方向（首批 10 条）。各页里带虚线下划线的指标名，悬停或聚焦即可就地看定义。
					</p>
				</header>

				<div className="mb-4">
					{/* 票 08：检索框（term/aliases/definition）；label 显式关联满足可读名 */}
					<label htmlFor="glossary-search" className="flex flex-col gap-1 text-xs text-muted-foreground">
						检索词条
						<input
							id="glossary-search"
							type="search"
							data-testid="glossary-search"
							placeholder="如：EV、CLV、单固、前瞻…"
							className="max-w-md rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
							value={query}
							onChange={(event) => setQuery(event.target.value)}
						/>
					</label>
				</div>

				{results.length === 0 ? (
					<EmptyState
						variant="no-data"
						message={`没有匹配"${query.trim()}"的词条。`}
						hint="换个更短的关键词（指标名、别名或定义里的词），或清空检索看全部词条。"
						action={{ label: "清空检索", onClick: () => setQuery("") }}
					/>
				) : (
					<ul className="grid gap-3 md:grid-cols-2" data-testid="glossary-list">
						{results.map((entry) => (
							<li key={entry.id} className="list-none">
								<GlossaryCard entry={entry} />
							</li>
						))}
					</ul>
				)}
			</div>
		</AppShell>
	);
}
