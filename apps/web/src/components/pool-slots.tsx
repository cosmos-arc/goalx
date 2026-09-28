import type { PoolMatch, PoolSelection } from "../api/goalx";
import { SELECTION_LABELS, SELECTIONS, TABULAR_NUMS } from "../lib/ui";

/**
 * 彩池页 14 场槽位列表（review-20260928 票 07 自 market-pool-page 拆出）：
 * 一场一行三向按钮（概率/份额/估计赔率/EV + 推荐/搏冷标记）+ 缺场空槽。
 * 判定助手（bestSelection/coldWorthySelection）与池页三向短标在此正典，
 * 页面预选与生成器区共用。
 */

/** 冷门阈值（票 pool-v2/01）：公众份额低于此值才算冷门。 */
const COLD_SHARE_MAX = 0.25;

/** 池页三向短标（官方口径：胜/平/负，非 had 页的主胜/客胜）。 */
export const POOL_LABELS: Record<string, string> = { h: "胜", d: "平", a: "负" };

/** 已选场条目（一场一选）：页面 picks 状态与槽位/生成器区的共享形状。 */
export type PoolPickEntry = { match: PoolMatch; code: string };

/** 一场推荐向（概率最高）；无概率场返回 null。 */
export function bestSelection(match: PoolMatch): string | null {
	let best: string | null = null;
	let bestProb = -1;
	for (const sel of match.selections) {
		if (sel.prob === null || sel.prob === undefined) {
			continue;
		}
		if (best === null || sel.prob > bestProb) {
			best = sel.code;
			bestProb = sel.prob;
		}
	}
	return best;
}

export function selectionOf(match: PoolMatch, code: string): PoolSelection | undefined {
	return match.selections.find((sel) => sel.code === code);
}

/**
 * 一场"值得搏冷"的选项（票 pool-v2/01 价值判定，替代旧"概率最低"标注）：
 * 冷选项（份额 <25% 且 EV>0）中 EV 最高者，且 EV 须高于本场推荐（最高概率）
 * 选项的 EV——牺牲命中换赔率只在正期望且优于稳妥选时值得。无则 null。
 */
function coldWorthySelection(match: PoolMatch): { code: string; ev: number } | null {
	let best: { code: string; ev: number } | null = null;
	for (const sel of match.selections) {
		const share = sel.share ?? null;
		const ev = sel.ev ?? null;
		if (share === null || share >= COLD_SHARE_MAX) continue;
		if (ev === null || ev <= 0) continue;
		if (best === null || ev > best.ev) best = { code: sel.code, ev };
	}
	if (!best) return null;
	const recommended = bestSelection(match);
	const recommendedEv = recommended ? (selectionOf(match, recommended)?.ev ?? null) : null;
	if (recommendedEv !== null && recommendedEv >= best.ev) return null;
	return best;
}

/** 开赛时间（北京时间口径展示）。 */
function formatTime(utc: string): string {
	return new Date(utc).toLocaleString("zh-CN", {
		month: "2-digit",
		day: "2-digit",
		hour: "2-digit",
		minute: "2-digit",
	});
}

export function PoolSlotList({
	matches,
	picks,
	emptySlots,
	onPick,
}: {
	matches: PoolMatch[];
	picks: Record<number, string>;
	emptySlots: number;
	onPick: (match: PoolMatch, selection: string) => void;
}) {
	return (
		<section aria-label="14 场列表" data-testid="pool-slots" className="mb-8 space-y-2">
			{matches.map((match) => {
				const recommended = bestSelection(match);
				const cold = coldWorthySelection(match);
				return (
					<article
						key={match.match_seq}
						data-testid={`pool-slot-${match.match_seq}`}
						className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-border bg-card p-3 text-sm"
					>
						<span className="min-w-32 text-xs text-muted-foreground">
							{String(match.match_seq).padStart(2, "0")} · {match.league}
							<br />
							{match.home_team} vs {match.away_team}
							<br />
							{formatTime(match.kickoff_utc)}
						</span>
						<span className="flex flex-wrap gap-1" data-testid={`pool-pick-${match.match_seq}`}>
							{SELECTIONS.map((sel) => {
								const data = selectionOf(match, sel);
								const prob = data?.prob ?? null;
								const share = data?.share ?? null;
								const odds = data?.implied_odds ?? null;
								const ev = data?.ev ?? null;
								const selected = picks[match.match_seq] === sel;
								return (
									<button
										key={sel}
										type="button"
										data-testid={`pool-pick-${match.match_seq}-${sel}`}
										aria-pressed={selected}
										className={`rounded-md border px-2 py-1 text-xs ${TABULAR_NUMS} transition-colors ${
											selected ? "border-primary bg-primary text-primary-foreground" : "border-border hover:bg-muted"
										}`}
										onClick={() => onPick(match, sel)}
									>
										{SELECTION_LABELS[sel]}
										{prob !== null ? (
											<span className="ml-1 opacity-80" title={data?.prob_source === "model" ? "模型概率" : "欧指去水"}>
												{(prob * 100).toFixed(0)}%
											</span>
										) : (
											<span className="ml-1 opacity-60">待数据</span>
										)}
										{share !== null ? (
											<span className="ml-1 opacity-70" title="公众份额（人气分布代理）">
												份{(share * 100).toFixed(0)}%
											</span>
										) : null}
										{odds !== null ? (
											<span className="ml-1 opacity-60" title="估计派彩赔率 = 返奖率 65% ÷ 份额">
												@{odds.toFixed(2)}
											</span>
										) : null}
										{ev !== null ? (
											<span
												className={`ml-1 ${ev > 0 ? "text-info font-medium" : "opacity-60"}`}
												title="彩池口径 EV = 概率 × 估计赔率 − 1（未建模分彩风险）"
											>
												EV{ev >= 0 ? "+" : ""}
												{(ev * 100).toFixed(0)}%
											</span>
										) : null}
										{recommended === sel ? <span className="ml-1 rounded bg-info/10 px-1 text-info">推荐</span> : null}
										{cold?.code === sel ? (
											<span
												className="ml-1 rounded bg-warning/10 px-1"
												title={`冷门正期望：概率 ${((data?.prob ?? 0) * 100).toFixed(0)}% vs 份额 ${((data?.share ?? 0) * 100).toFixed(0)}%，估计赔率 @${(data?.implied_odds ?? 0).toFixed(2)}，EV+${(cold.ev * 100).toFixed(0)}%（高于推荐向）`}
											>
												搏冷
											</span>
										) : null}
									</button>
								);
							})}
						</span>
					</article>
				);
			})}
			{Array.from({ length: emptySlots }, (_, index) => matches.length + index + 1).map((slotNo) => (
				<div
					key={`empty-${slotNo}`}
					data-testid={`pool-slot-empty-${slotNo}`}
					className="rounded-lg border border-dashed border-border p-3 text-xs text-muted-foreground"
				>
					第 {slotNo} 场：本期次页面暂缺该场对阵（源未发布或期次不完整）
				</div>
			))}
		</section>
	);
}
