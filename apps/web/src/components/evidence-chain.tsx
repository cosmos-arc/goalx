import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { type FixtureEvidence, fetchFixtureEvidence, type IntelItem, type TrackTriple } from "../api/goalx";
import { TABULAR_NUMS } from "../lib/ui";
import { EmptyState } from "./empty-state";

/**
 * 票 14 V2 证据链区块：挂 fixture-research 页底部（方案 A，票 06 定案）。
 *
 * 三轨概率对照（ML/LLM/Fused）+ JS 徽章 + 情报时间线（来源/时点徽章）+
 * 复核结论 + 追问入口（票 15 接线）+ 盲评入口（/review 双周三选一）。
 * 全部为存量工件渲染：无数据的轨道/情报诚实显示缺失，不虚构。
 */

const TRACK_LABELS: Record<string, string> = { ml: "ML 量化", llm: "LLM 情报", fused: "Fused 融合" };

const VERDICT_LABELS: Record<string, string> = {
	key_contribution: "情报有关键贡献",
	irrelevant: "情报无关",
	misleading: "情报误导",
};

function pct(value: number | null | undefined): string {
	return value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;
}

function shortTime(iso: string): string {
	return iso.slice(5, 16).replace("T", " ");
}

function IntelRow({ intel }: { intel: IntelItem }) {
	return (
		<li className="border-l-2 border-info/40 pl-2.5 text-xs" data-testid="chain-intel">
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

function TrackRow({ track }: { track: TrackTriple }) {
	return (
		<tr data-testid={`chain-track-${track.track}`}>
			<TableCell>
				{TRACK_LABELS[track.track] ?? track.track}
				{track.analyst ? <span className="ml-1 rounded bg-info/10 px-1 text-xs text-info">复核</span> : null}
			</TableCell>
			<TableCell className={TABULAR_NUMS}>{pct(track.h)}</TableCell>
			<TableCell className={TABULAR_NUMS}>{pct(track.d)}</TableCell>
			<TableCell className={TABULAR_NUMS}>{pct(track.a)}</TableCell>
			<TableCell className="text-xs text-muted-foreground">{shortTime(track.issued_at)}</TableCell>
		</tr>
	);
}

function TableCell({ children, className }: { children: ReactNode; className?: string }) {
	return <td className={`border-t border-border px-2 py-1.5 ${className ?? ""}`}>{children}</td>;
}

export function EvidenceChainSection({ fixtureId }: { fixtureId: number }) {
	const query = useQuery({
		queryKey: ["fixture-evidence", fixtureId],
		queryFn: () => fetchFixtureEvidence(fixtureId),
		retry: false,
	});
	// 区块骨架常驻（降级态也带标题——e2e 双路径断言同一 testid）
	return (
		<section aria-labelledby="research-chain-heading" data-testid="research-chain">
			<h3 id="research-chain-heading" className="mb-2 text-sm font-medium">
				证据链
				<span className="ml-2 font-normal text-muted-foreground">三轨对照 · 情报时间线 · 复核结论（存量工件渲染）</span>
			</h3>
			{query.isPending ? (
				<p className="text-xs text-muted-foreground">证据链加载中…</p>
			) : query.isError ? (
				<EmptyState
					variant="not-available"
					message="证据链暂不可用（后端不可达或版本较旧无证据端点）。"
					hint="三轨概率对照与情报时间线随 M3 证据端点部署后在此展示。"
				/>
			) : (
				<ChainBody data={query.data} />
			)}
		</section>
	);
}

function ChainBody({ data }: { data: FixtureEvidence }) {
	const { tracks, divergence, intels, reviews, caliber } = data;
	const llmTrack = tracks["llm"];
	const decided = reviews.find((r) => r.verdict !== null);
	return (
		<>
			<p className="mb-3 text-xs text-muted-foreground" data-testid="chain-caliber">
				{caliber}
			</p>

			<div className="overflow-x-auto rounded-lg border border-border" data-testid="chain-tracks">
				<table className="w-full text-sm">
					<thead>
						<tr className="text-left text-xs text-muted-foreground">
							<th className="px-2 py-1.5 font-medium">轨道</th>
							<th className="px-2 py-1.5 font-medium">主胜</th>
							<th className="px-2 py-1.5 font-medium">平</th>
							<th className="px-2 py-1.5 font-medium">客胜</th>
							<th className="px-2 py-1.5 font-medium">发出时点</th>
						</tr>
					</thead>
					<tbody>
						{(["ml", "llm", "fused"] as const).map((track) =>
							tracks[track] ? (
								<TrackRow key={track} track={tracks[track] as TrackTriple} />
							) : (
								<tr key={track} data-testid={`chain-track-${track}`}>
									<TableCell>{TRACK_LABELS[track]}</TableCell>
									<TableCell className="text-muted-foreground">—</TableCell>
									<TableCell className="text-muted-foreground">—</TableCell>
									<TableCell className="text-muted-foreground">—</TableCell>
									<TableCell className="text-xs text-muted-foreground">无产出</TableCell>
								</tr>
							),
						)}
					</tbody>
				</table>
			</div>

			<p className="mt-2 flex flex-wrap items-center gap-2 text-xs">
				<span
					className="rounded border px-1.5 py-0.5 text-indigo-600"
					data-testid="chain-js"
					title="ML×LLM Jensen-Shannon 散度（base 2）"
				>
					{divergence.js !== null && divergence.js !== undefined
						? `JS(ML,LLM)=${divergence.js.toFixed(3)}${divergence.routed ? " → 已入复核" : " · 未路由"}`
						: "无分歧读数（双轨未齐）"}
				</span>
				{decided ? (
					<span className="rounded bg-success/10 px-1.5 py-0.5 text-success" data-testid="chain-verdict">
						复核结论：{VERDICT_LABELS[decided.verdict ?? ""] ?? decided.verdict}
					</span>
				) : (
					<span className="text-muted-foreground" data-testid="chain-verdict">
						复核结论：未裁决
					</span>
				)}
			</p>

			{llmTrack?.rationale ? (
				<p className="mt-2 text-xs text-muted-foreground" data-testid="chain-rationale">
					研判依据（{llmTrack.analyst ? "analyst 复核" : "scout"}）：{llmTrack.rationale}
				</p>
			) : null}

			<div className="mt-3">
				<h4 className="mb-1.5 text-xs font-medium text-muted-foreground">情报时间线（全部已存证）</h4>
				{intels.length === 0 ? (
					<p
						className="rounded-md border border-dashed border-border p-2 text-xs text-muted-foreground"
						data-testid="chain-intels-empty"
					>
						本场无已存证情报——不出概率与证据，仅官方份额，不装懂。
					</p>
				) : (
					<ul className="space-y-1.5" data-testid="chain-intels">
						{intels.map((intel) => (
							<IntelRow key={`${intel.kind}-${intel.collected_at}-${intel.text}`} intel={intel} />
						))}
					</ul>
				)}
			</div>

			<p className="mt-3 flex flex-wrap items-center gap-3 text-xs">
				<button
					type="button"
					disabled
					className="rounded-md border border-info px-2 py-1 text-info disabled:opacity-40"
					data-testid="chain-ask-pending"
					title="随票 15（AG-UI 追问 analyst）上线"
				>
					追问 analyst（即将上线）
				</button>
				<Link to="/review" className="text-info underline-offset-2 hover:underline" data-testid="chain-blind-entry">
					盲评入口 → 双周匿名二选一（复核页）
				</Link>
			</p>
		</>
	);
}
