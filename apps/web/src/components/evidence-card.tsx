import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { type EvidenceMatch, fetchPoolEvidenceSummary, type IntelItem } from "../api/goalx";
import { TABULAR_NUMS } from "../lib/ui";
import { EmptyState } from "./empty-state";

/**
 * 票 14 V1 证据卡：彩池页"AI 证据总结"存量渲染。
 *
 * 形态照单（票 06 用户确认）：展开式卡片 + 每条情报来源/时点徽章 +
 * 卡底 scout/analyst/Fused 状态行；无情报场次诚实降级——明说不出
 * 概率，仅展示官方份额（不装懂）。数据 = evidence-summary 端点一
 * fetch 整期；加载失败（旧后端无端点/后端不可达）整块降级不装数据。
 */

/** 状态行文案（诚实口径：state 是 LLM 证据线的状态，不是装懂）。 */
const STATE_LABELS: Record<string, string> = {
	analyst_done: "analyst 已复核",
	scout_done: "scout 已出概率",
	no_forecast: "有情报暂无 LLM 概率产出（宁缺毋假）",
	no_intel: "无情报无产出",
};

function pct(value: number | null | undefined): string {
	return value === null || value === undefined ? "—" : `${(value * 100).toFixed(0)}%`;
}

function shortTime(iso: string): string {
	return iso.slice(5, 16).replace("T", " ");
}

/** 一条情报：kind + 文本 + 来源/时点徽章。 */
function IntelLine({ intel }: { intel: IntelItem }) {
	return (
		<li className="border-l-2 border-info/40 pl-2.5" data-testid="evidence-intel">
			<span className="mr-1.5 rounded border border-border px-1 text-xs text-muted-foreground">{intel.kind}</span>
			{intel.text}
			<span className="ml-2 inline-flex gap-1 align-baseline">
				<span className="rounded bg-info/10 px-1 text-xs text-info">{intel.source}</span>
				<span className="rounded border border-border px-1 text-xs text-muted-foreground">
					采集 {shortTime(intel.collected_at)}
				</span>
			</span>
		</li>
	);
}

/** 一场的展开式证据卡。 */
function MatchEvidenceCard({ match }: { match: EvidenceMatch }) {
	const [open, setOpen] = useState(false);
	const degraded = match.state === "no_intel" || match.state === "no_forecast";
	const forecast = match.forecast;
	const summary = degraded
		? "证据卡 · 诚实降级"
		: `证据卡 · ${match.intel_count} 条已存证情报${match.divergence?.routed ? " · 已入复核" : ""}`;
	return (
		<article
			className="rounded-md border border-border p-3 text-sm"
			data-testid={`evidence-card-${match.match_seq}`}
			data-state={match.state}
		>
			<button
				type="button"
				className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 text-left"
				aria-expanded={open}
				onClick={() => setOpen((v) => !v)}
			>
				<span className="font-medium">
					第 {match.match_seq} 场 · {match.home_team} vs {match.away_team}
				</span>
				<span className="text-xs text-muted-foreground">{match.league}</span>
				<span className="text-xs text-muted-foreground">{summary}</span>
				{forecast ? (
					<span className={`${TABULAR_NUMS} ml-auto text-xs`}>
						胜 {pct(forecast.h)} / 平 {pct(forecast.d)} / 负 {pct(forecast.a)}
						<span className="ml-1 rounded bg-info/10 px-1 text-info">{forecast.track.toUpperCase()}</span>
					</span>
				) : (
					<span className="ml-auto text-xs text-muted-foreground">概率 —（仅官方份额）</span>
				)}
			</button>
			{open ? (
				<div className="mt-2 space-y-2 border-t border-border pt-2" data-testid={`evidence-detail-${match.match_seq}`}>
					{degraded ? (
						<p
							className="rounded-md border border-dashed border-warning/40 bg-warning/5 p-2 text-xs"
							data-testid={`evidence-degraded-${match.match_seq}`}
						>
							本场无已存证情报（或情报未产出概率）——不出概率与证据，仅展示官方份额，不装懂。
							{match.state === "no_forecast" ? " scout 采集已完成但模型未产出，宁缺毋假。" : ""}
						</p>
					) : (
						<ul className="space-y-1.5 text-xs">
							{(match.intels ?? []).map((intel) => (
								<IntelLine key={`${intel.kind}-${intel.collected_at}-${intel.text}`} intel={intel} />
							))}
						</ul>
					)}
					{forecast?.rationale ? <p className="text-xs text-muted-foreground">研判依据：{forecast.rationale}</p> : null}
					<p className="text-xs text-muted-foreground" data-testid={`evidence-state-${match.match_seq}`}>
						状态：{STATE_LABELS[match.state] ?? match.state}
						{match.divergence?.js !== null && match.divergence?.js !== undefined ? (
							<>
								{" "}
								· JS {match.divergence.js.toFixed(3)}
								{match.divergence.routed ? "（>0.06 已入复核）" : "（<0.06 未路由）"}
							</>
						) : null}
						{forecast
							? ` · ${forecast.track === "fused" ? "Fused 三项融合" : "LLM 轨"} ${shortTime(forecast.issued_at)}`
							: ""}
					</p>
				</div>
			) : null}
		</article>
	);
}

/** 期次证据卡区块（activePeriod 变化随取；失败/无数据诚实降级）。 */
export function EvidenceCardSection({ periodNo }: { periodNo: string }) {
	const query = useQuery({
		queryKey: ["pool-evidence-summary", periodNo],
		queryFn: () => fetchPoolEvidenceSummary(periodNo),
		retry: false,
		enabled: periodNo !== "",
	});
	if (query.isPending) {
		return <p className="text-xs text-muted-foreground">证据卡加载中…</p>;
	}
	if (query.isError) {
		return (
			<EmptyState
				variant="not-available"
				message="证据卡暂不可用（后端不可达或版本较旧无证据端点）。"
				hint="情报/概率证据随 M3 采集与 scout 调度生成；端点部署后此处点亮。"
			/>
		);
	}
	const summary = query.data;
	return (
		<div className="space-y-2" data-testid="pool-evidence-cards">
			<p className="text-xs text-muted-foreground" data-testid="pool-evidence-caliber">
				{summary.caliber}
			</p>
			{summary.matches.map((match) => (
				<MatchEvidenceCard key={match.match_seq} match={match} />
			))}
		</div>
	);
}
