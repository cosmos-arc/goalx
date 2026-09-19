import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import {
	buildTargetPlan,
	type ColdVariants,
	createPoolSlip,
	fetchBankroll,
	fetchPoolPeriodDetail,
	fetchPoolPeriods,
	fetchPoolSyncStatus,
	generateColdVariants,
	type PoolMatch,
	type PoolSelection,
	runPoolSync,
} from "../api/goalx";
import { AppShell } from "../components/app-shell";
import { EmptyState } from "../components/empty-state";
import { GlossaryTerm } from "../components/glossary-term";
import { MarketTabs } from "../components/market-tabs";
import { type AdviceLeg, StakeAdviceNote } from "../components/stake-advice";
import { SELECTION_LABELS, SELECTIONS, TABULAR_NUMS } from "../lib/ui";

/**
 * 票 43：14场任9 页点亮（wb-07 骨架毕业）。
 *
 * 真实期次/对阵/分布来自彩池同步（源B，票 43 实证落地）；三向概率优先模型
 * 口径（映射场次有赛前 Forecast）、退化期次页欧指去水；估计派彩赔率 =
 * 返奖率 65% ÷ 份额（抽水折算）；EV = 概率 × 估计赔率 − 1（未建模 price
 * impact 与分彩风险，词典词条同口径）。
 *
 * 官方销量四源直接 GET 不可得 → "AI 代采待命"占位（代理经代采入口回填，
 * source=agent）；期次/对阵可得而销量缺失时只降级该区块（不整页骨架）。
 *
 * 红线：奖池型初期只纸面低敞口——提交只建 paper 池票；真金档位禁用不呈现。
 */

/** 14 场槽位总数（任9 规则：14 场任选 9 场）。 */
const POOL_SLOTS = 14;
/** 任9 的固定选取场次数。 */
const PICK9 = 9;
/** 前端 h/d/a → 官方池码（提交 pool-slips 用池码）。 */
const HAD_TO_POOL_CODE = { h: "3", d: "1", a: "0" } as const;
/** 每注金额（任9 一注 ¥2，规则价）。 */
const STAKE_PER_COMBINATION = 2;

/** C(n, k)：复式组合数（每场单选 → 组合数 = C(已选场数, 9)；导出供单测）。 */
export function combinations(n: number, k: number): number {
	if (n < k || k < 0) {
		return 0;
	}
	let result = 1;
	for (let i = 1; i <= k; i += 1) {
		result = (result * (n - k + i)) / i;
	}
	return Math.round(result);
}

/** 一场推荐向（概率最高）；无概率场返回 null。 */
function bestSelection(match: PoolMatch): string | null {
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

/** 冷门阈值（票 pool-v2/01）：公众份额低于此值才算冷门。 */
const COLD_SHARE_MAX = 0.25;

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

function selectionOf(match: PoolMatch, code: string): PoolSelection | undefined {
	return match.selections.find((sel) => sel.code === code);
}

/** 池页三向短标（官方口径：胜/平/负，非 had 页的主胜/客胜）。 */
const POOL_LABELS: Record<string, string> = { h: "胜", d: "平", a: "负" };

/** 反推风险档标签（票 pool-v2/03）。 */
const RISK_LABELS: Record<string, string> = { steady: "稳", balanced: "中", bold: "搏" };

/** 生成器票行（票 pool-v2/02）：替换明细 + 命中概率/估计派彩/EV + 采用。 */
function TicketRow({
	label,
	ticket,
	testid,
	onAdopt,
}: {
	label: string;
	ticket: ColdVariants["base"];
	testid: string;
	onAdopt?: () => void;
}) {
	return (
		<div
			className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-md border border-border p-3 text-sm"
			data-testid={testid}
		>
			<span className="font-medium">{label}</span>
			{ticket.swaps.length > 0 ? (
				<span className="text-xs text-muted-foreground">
					{ticket.swaps
						.map(
							(swap) =>
								`第${swap.match_seq}场 ${POOL_LABELS[swap.from_code]}→${POOL_LABELS[swap.to_code]}（EV+${(swap.ev_gain * 100).toFixed(0)}%）`,
						)
						.join("；")}
				</span>
			) : null}
			{ticket.hit_prob !== null && ticket.hit_prob !== undefined ? (
				<span className={TABULAR_NUMS} title="各场概率连乘">
					命中 {(ticket.hit_prob * 100).toFixed(1)}%
				</span>
			) : (
				<span className="text-muted-foreground">命中率缺数据</span>
			)}
			{ticket.est_odds !== null && ticket.est_odds !== undefined ? (
				<span className={TABULAR_NUMS} title="返奖率 65% ÷ 各场份额连乘">
					@{ticket.est_odds.toFixed(0)}
				</span>
			) : null}
			{ticket.ev !== null && ticket.ev !== undefined ? (
				<span className={`${TABULAR_NUMS} ${ticket.ev > 0 ? "text-info" : "text-muted-foreground"}`}>
					EV{ticket.ev >= 0 ? "+" : ""}
					{(ticket.ev * 100).toFixed(0)}%
				</span>
			) : null}
			{onAdopt ? (
				<button
					type="button"
					data-testid={`${testid}-adopt`}
					className="ml-auto rounded-md border border-border px-2 py-1 text-xs"
					onClick={onAdopt}
				>
					采用
				</button>
			) : null}
		</div>
	);
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

export function MarketPoolPage() {
	const periodsQuery = useQuery({ queryKey: ["pool-periods"], queryFn: () => fetchPoolPeriods() });
	const syncQuery = useQuery({ queryKey: ["pool-sync-status"], queryFn: fetchPoolSyncStatus });
	const bankrollQuery = useQuery({ queryKey: ["bankroll"], queryFn: fetchBankroll });
	const queryClient = useQueryClient();

	const periods = periodsQuery.data ?? [];
	const defaultPeriod = periods.find((p) => p.status === "on_sale") ?? periods[0];
	const [period, setPeriod] = useState<string | null>(null);
	const activePeriod =
		period !== null && periods.some((p) => p.period_no === period) ? period : defaultPeriod?.period_no;
	const detailQuery = useQuery({
		queryKey: ["pool-period-detail", activePeriod],
		queryFn: () => fetchPoolPeriodDetail(activePeriod ?? ""),
		enabled: activePeriod !== undefined,
	});

	const syncMutation = useMutation({
		mutationFn: runPoolSync,
		onSuccess: () => {
			void queryClient.invalidateQueries({ queryKey: ["pool-periods"] });
			void queryClient.invalidateQueries({ queryKey: ["pool-period-detail"] });
			void queryClient.invalidateQueries({ queryKey: ["pool-sync-status"] });
		},
	});

	/** 已选场序 → 三向选择（一场一选，任9 口径）。 */
	const [picks, setPicks] = useState<Record<number, string>>({});
	const [submitResult, setSubmitResult] = useState<string | null>(null);
	const [coldness, setColdness] = useState(2);
	const [generatorMessage, setGeneratorMessage] = useState<string | null>(null);
	const [targetAmount, setTargetAmount] = useState(10000);
	const [targetRisk, setTargetRisk] = useState<"steady" | "balanced" | "bold">("balanced");
	const [targetMessage, setTargetMessage] = useState<string | null>(null);
	const generatorMutation = useMutation({
		mutationFn: generateColdVariants,
		onSuccess: () => setGeneratorMessage(null),
	});
	const targetMutation = useMutation({ mutationFn: buildTargetPlan });
	const submitMutation = useMutation({
		mutationFn: createPoolSlip,
		onSuccess: (slip) => {
			setSubmitResult(`已建纸面池票 #${slip.id}`);
			void queryClient.invalidateQueries({ queryKey: ["slips"] });
		},
		onError: (error) => {
			setSubmitResult(`提交失败：${error instanceof Error ? error.message : "未知错误"}`);
		},
	});

	const detail = detailQuery.data;
	const matches = detail?.matches ?? [];
	const variants = generatorMutation.data;
	const plan = targetMutation.data;
	// 已选（一场一选）：match + 所选向，索引访问经 flatMap 收窄（无 undefined）
	const pickedEntries = matches.flatMap((m) => {
		const code = picks[m.match_seq];
		return code === undefined ? [] : [{ match: m, code }];
	});
	const comboCount = combinations(pickedEntries.length, PICK9);
	/** 三档输入：所选组合联合口径（估计派彩赔率 Π × 联合 EV = Π(1+ev)−1）。 */
	const adviceInput = useMemo(() => {
		const legs: AdviceLeg[] = pickedEntries.map(({ match, code }) => {
			const sel = selectionOf(match, code);
			return {
				ev: sel?.ev ?? null,
				odds: sel?.implied_odds ?? Number.NaN, // 缺份额的向毒化联合赔率 → 下方整体降级
			};
		});
		const odds = legs.reduce((acc, leg) => acc * leg.odds, 1);
		let ev: number | null = 1;
		for (const leg of legs) {
			if (leg.ev === null || leg.ev === undefined) {
				ev = null;
				break;
			}
			ev *= 1 + leg.ev;
		}
		return { ev: ev === null ? null : ev - 1, odds };
	}, [pickedEntries]);
	const hasValidInput = pickedEntries.length > 0 && Number.isFinite(adviceInput.odds);
	const bankroll = bankrollQuery.data?.balance ?? (bankrollQuery.isError ? null : 0);

	function pick(match: PoolMatch, selection: string) {
		setSubmitResult(null);
		setPicks((current) => {
			const next = { ...current };
			if (next[match.match_seq] === selection) {
				delete next[match.match_seq]; // 再点同向 = 取消
			} else {
				next[match.match_seq] = selection; // 一场一选（任9 口径）
			}
			return next;
		});
	}

	/** 按推荐标记预选：概率最高向填满前 9 个有数据的槽位。 */
	function prefillRecommended() {
		const next: Record<number, string> = {};
		for (const match of matches) {
			if (Object.keys(next).length >= PICK9) {
				break;
			}
			const best = bestSelection(match);
			if (best !== null) {
				next[match.match_seq] = best;
			}
		}
		setPicks(next);
	}

	function submitPaperSlip() {
		if (!detail) {
			return;
		}
		submitMutation.mutate({
			mode: "paper",
			pool_period_id: null,
			stake_per_combination: STAKE_PER_COMBINATION,
			picks: pickedEntries.map(({ match, code }) => ({
				match_seq: match.match_seq,
				selection_code: HAD_TO_POOL_CODE[code as "h" | "d" | "a"],
				...(match.fixture_id === null || match.fixture_id === undefined ? {} : { fixture_id: match.fixture_id }),
			})),
		});
	}

	const lastRun = syncQuery.data?.last_run;
	const emptySlots = Math.max(0, POOL_SLOTS - matches.length);

	return (
		<AppShell title="14场任9">
			<div className="pb-24">
				<MarketTabs />

				<header className="mb-5">
					<p className="text-sm text-muted-foreground" data-testid="pool-caliber">
						{detail ? (
							detail.caliber
						) : (
							<>
								14 场任选 9 场；<GlossaryTerm id="pool-ev">彩池 EV</GlossaryTerm> 为彩池口径（估计派彩赔率 = 返奖率 65%
								÷ 份额）。推荐 = 概率最高向；搏冷 = 价值判定（冷选项份额低于 25% 且 EV 为正、优于推荐向，悬浮看理由）。
							</>
						)}
					</p>
				</header>

				{/* 同步状态行（源B；触发幂等） */}
				<section className="mb-5 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-muted-foreground">
					<span data-testid="pool-sync-info">
						{lastRun
							? `上次同步 ${lastRun.observed_at.slice(0, 16).replace("T", " ")}（${lastRun.source}，${lastRun.matches} 场/${lastRun.share_rows} 份额行）`
							: "尚未同步过彩池数据"}
					</span>
					<button
						type="button"
						data-testid="pool-sync-run"
						className="rounded-md border border-border px-3 py-1.5 transition-colors hover:bg-muted disabled:opacity-50"
						disabled={syncMutation.isPending}
						onClick={() => syncMutation.mutate()}
					>
						{syncMutation.isPending ? "同步中…" : "立即同步"}
					</button>
					{syncMutation.isError ? (
						<span className="text-destructive" data-testid="pool-sync-error">
							同步失败（源不可达），稍后重试
						</span>
					) : null}
				</section>

				{/* 后端不可用降级（结构骨架照常渲染，槽位全部"待数据"） */}
				{periodsQuery.isError ? (
					<div className="mb-5">
						<EmptyState
							variant="backend-unavailable"
							message="连不上后端，彩池期次数据加载失败——结构骨架照常展示。"
							hint={<>用 task server 启动 API；期次数据来自彩池同步（源B）。</>}
							action={{ label: "重试", onClick: () => void periodsQuery.refetch() }}
						/>
					</div>
				) : null}

				{/* 无期次（已连后端但从未同步）：诚实空态 */}
				{!periodsQuery.isError && periodsQuery.isSuccess && periods.length === 0 ? (
					<div className="mb-5">
						<EmptyState
							variant="no-data"
							message="尚无彩池期次数据"
							hint={<>点上方"立即同步"拉取最新期次（或等定时同步）；历史期次会随同步累积。</>}
						/>
					</div>
				) : null}

				{/* 期次选择 + 预选 */}
				{periods.length > 0 ? (
					<section aria-labelledby="pool-period-heading" className="mb-5">
						<div className="flex flex-wrap items-end justify-between gap-3">
							<h2 id="pool-period-heading" className="text-sm font-medium">
								期次{" "}
								<span className="font-normal text-muted-foreground" data-testid="pool-period-note">
									（真实期次，来自彩池同步；14 场任选 9）
								</span>
							</h2>
							<div className="flex items-center gap-2">
								<label className="flex items-center gap-2 text-xs">
									<span className="text-muted-foreground">期次</span>
									<select
										data-testid="pool-period-select"
										className="rounded-md border border-input bg-background px-2 py-1.5 text-sm"
										value={activePeriod ?? ""}
										onChange={(event) => {
											setPeriod(event.target.value);
											setPicks({});
											setSubmitResult(null);
										}}
									>
										{periods.map((p) => (
											<option key={p.period_no} value={p.period_no}>
												{p.period_no}
												{p.status === "finished" ? "（已开赛）" : ""} · {p.match_count} 场
											</option>
										))}
									</select>
								</label>
								<button
									type="button"
									data-testid="pool-prefill-recommended"
									className="rounded-md border border-border px-3 py-1.5 text-sm transition-colors hover:bg-muted"
									onClick={prefillRecommended}
								>
									按推荐标记预选
								</button>
							</div>
						</div>
						<p className="mt-2 text-xs" data-testid="pool-pick-count">
							已选{" "}
							<span className={TABULAR_NUMS}>
								{pickedEntries.length}/{POOL_SLOTS}
							</span>{" "}
							场（任9 需 ≥{PICK9} 场；当前组合数 <span className={TABULAR_NUMS}>{comboCount}</span> 注）
						</p>
					</section>
				) : null}

				{/* 销量区块：AI 代采待命（官方销量四源直接 GET 不可得，票 43 兜底层） */}
				{periods.length > 0 ? (
					<section aria-label="彩池销量" className="mb-5" data-testid="pool-sales">
						{detail?.state ? (
							<p className="rounded-md border border-border bg-card p-3 text-sm">
								官方销量（AI 代采，公布时点 {detail.state.published_at?.slice(0, 10) ?? "未知"}）：
								<span className={TABULAR_NUMS}> ¥{(detail.state.sales_amount ?? 0).toLocaleString("zh-CN")}</span>
								{detail.state.rollover_in ? (
									<span className="ml-2 text-muted-foreground">
										滚存转入 ¥{detail.state.rollover_in.toLocaleString("zh-CN")}
									</span>
								) : null}
							</p>
						) : (
							<p
								className="rounded-md border border-dashed border-border p-3 text-xs text-muted-foreground"
								data-testid="pool-sales-agent-pending"
							>
								官方销量/滚存暂无自动源（四源实证均不可直接拉取）——AI 代采待命：代理读官方公布页结构化回填后此处点亮。
							</p>
						)}
					</section>
				) : null}

				{/* 14 场槽位列表 */}
				{periods.length > 0 ? (
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
														selected
															? "border-primary bg-primary text-primary-foreground"
															: "border-border hover:bg-muted"
													}`}
													onClick={() => pick(match, sel)}
												>
													{SELECTION_LABELS[sel]}
													{prob !== null ? (
														<span
															className="ml-1 opacity-80"
															title={data?.prob_source === "model" ? "模型概率" : "欧指去水"}
														>
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
													{recommended === sel ? (
														<span className="ml-1 rounded bg-info/10 px-1 text-info">推荐</span>
													) : null}
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
				) : null}

				{/* 额度建议：奖池红线 = 只纸面 flat（真金档位禁用不呈现） */}
				{periods.length > 0 ? (
					<section aria-labelledby="pool-tiers-heading" className="mb-8">
						<h2 id="pool-tiers-heading" className="mb-2 text-sm font-medium">
							额度建议 <span className="font-normal text-muted-foreground">纸面 flat（奖池型红线）</span>
						</h2>
						<p className="mb-3 text-xs text-muted-foreground" data-testid="pool-tier-mapping">
							奖池型初期只纸面低敞口（调研红线）：仅 flat 档（纸面）；标准/激进（live ¼Kelly）在
							奖池真金开放经用户单独裁决前禁用不呈现。输入 = 所选组合联合口径（估计派彩赔率 Π、 联合 EV =
							Π(1+EV)−1）；EV≤0 时建议 ¥0。
						</p>
						{pickedEntries.length === 0 ? (
							<p
								className="rounded-md border border-dashed border-border p-4 text-sm text-muted-foreground"
								data-testid="pool-tiers-empty"
							>
								先在上方 14 场列表选择（或"按推荐标记预选"）——额度建议随所选组合计算。
							</p>
						) : (
							<div className="grid gap-2 sm:grid-cols-3" data-testid="pool-tiers">
								<StakeAdviceNote
									mode="paper"
									bankroll={bankroll}
									ev={hasValidInput ? adviceInput.ev : null}
									odds={hasValidInput ? adviceInput.odds : 2}
									label="保守档（flat，纸面）"
									note={`组合 ${comboCount} 注 × ¥${STAKE_PER_COMBINATION}/注（每注同额，任9 复式；估计派彩口径）`}
									testid="pool-tier-conservative"
								/>
								<p
									className="rounded-md border border-dashed border-border p-3 text-xs text-muted-foreground"
									data-testid="pool-tier-live-disabled"
								>
									标准/激进档（live ¼Kelly）——奖池型真金未开放（须用户单独裁决），禁用。
								</p>
							</div>
						)}
					</section>
				) : null}

				{/* 搏冷生成器（票 pool-v2/02）：基础票按 EV 增益贪心替换 1..N 处冷门 */}
				{periods.length > 0 ? (
					<section
						aria-labelledby="pool-generator-heading"
						className="mb-8 rounded-lg border border-border bg-card p-4"
						data-testid="pool-generator"
					>
						<h2 id="pool-generator-heading" className="text-sm font-medium">
							搏冷生成器
						</h2>
						<p className="mt-1 text-xs text-muted-foreground">
							基础票 = 当前全选（未选满时用各场最高概率），按「冷选项 EV − 基础向 EV」降序贪心替换；
							变体牺牲命中率抬估计派彩，采用前看 EV 与命中概率两栏。
						</p>
						<div className="mt-3 flex flex-wrap items-center gap-2">
							<label className="text-xs text-muted-foreground" htmlFor="pool-coldness">
								冷度（替换处数）
							</label>
							<select
								id="pool-coldness"
								data-testid="pool-coldness"
								value={coldness}
								onChange={(event) => setColdness(Number(event.target.value))}
								className="rounded-md border border-border bg-background px-2 py-1 text-sm"
							>
								<option value={1}>1 处</option>
								<option value={2}>2 处</option>
								<option value={3}>3 处</option>
							</select>
							<button
								type="button"
								data-testid="pool-generate"
								disabled={!activePeriod || generatorMutation.isPending}
								className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-40"
								onClick={() =>
									generatorMutation.mutate({
										period_no: activePeriod ?? "",
										market_code: "ttt14",
										coldness,
										...(pickedEntries.length === matches.length && matches.length > 0
											? {
													base_picks: Object.fromEntries(
														pickedEntries.map(({ match, code }) => [String(match.match_seq), code]),
													),
												}
											: {}),
									})
								}
							>
								{generatorMutation.isPending ? "生成中…" : "生成变体"}
							</button>
							{generatorMutation.isError ? (
								<span className="text-xs text-destructive" data-testid="pool-generator-error">
									生成失败：{generatorMutation.error instanceof Error ? generatorMutation.error.message : "未知错误"}
								</span>
							) : null}
						</div>
						{variants ? (
							<div className="mt-3 space-y-2" data-testid="pool-variants">
								<p className="text-xs text-muted-foreground" data-testid="pool-generator-caliber">
									{variants.caliber}
								</p>
								<TicketRow
									label={`基础票（${Object.keys(variants.base.picks).length} 场）`}
									ticket={variants.base}
									testid="pool-variant-base"
								/>
								{variants.variants.map((variant) => (
									<TicketRow
										key={variant.swaps.map((s) => `${s.match_seq}${s.to_code}`).join("-")}
										label={`变体 ${variant.swaps.length}（${variant.swaps.length} 处冷门）`}
										ticket={variant}
										testid={`pool-variant-${variant.swaps.length}`}
										onAdopt={() => {
											setPicks(
												Object.fromEntries(Object.entries(variant.picks).map(([seq, code]) => [Number(seq), code])),
											);
											setGeneratorMessage(`已采用变体 ${variant.swaps.length} 作为当前选择`);
										}}
									/>
								))}
								{variants.variants.length === 0 ? (
									<p className="text-xs text-muted-foreground" data-testid="pool-variants-empty">
										本期无正期望冷门可替换（冷选项 EV 均为负或不如基础向）——跟大众是更优解。
									</p>
								) : null}
								{generatorMessage ? (
									<p className="text-xs text-info" data-testid="pool-generator-message">
										{generatorMessage}
									</p>
								) : null}
							</div>
						) : null}
					</section>
				) : null}

				{/* 目标金额反推（票 pool-v2/03）：输入目标 → 推荐票面 + 建议注数 */}
				{periods.length > 0 ? (
					<section
						aria-labelledby="pool-target-heading"
						className="mb-8 rounded-lg border border-border bg-card p-4"
						data-testid="pool-target"
					>
						<h2 id="pool-target-heading" className="text-sm font-medium">
							目标金额反推
						</h2>
						<p className="mt-1 text-xs text-muted-foreground">
							输入目标奖金，反推推荐票面与建议注数。估计派彩随最终池变——输出是"若命中估计得 X"，不承诺达成。
						</p>
						<div className="mt-3 flex flex-wrap items-center gap-2">
							<label className="text-xs text-muted-foreground" htmlFor="pool-target-amount">
								目标（¥）
							</label>
							<input
								id="pool-target-amount"
								data-testid="pool-target-amount"
								type="number"
								min={1}
								value={targetAmount}
								onChange={(event) => setTargetAmount(Number(event.target.value))}
								className="w-32 rounded-md border border-border bg-background px-2 py-1 text-sm"
							/>
							<select
								aria-label="风险档"
								data-testid="pool-target-risk"
								value={targetRisk}
								onChange={(event) => setTargetRisk(event.target.value as "steady" | "balanced" | "bold")}
								className="rounded-md border border-border bg-background px-2 py-1 text-sm"
							>
								<option value="steady">稳（14 场全稳）</option>
								<option value="balanced">中（任9 + 至多 1 冷）</option>
								<option value="bold">搏（任9 + 至多 3 冷）</option>
							</select>
							<button
								type="button"
								data-testid="pool-target-generate"
								disabled={
									!activePeriod || targetMutation.isPending || !Number.isFinite(targetAmount) || targetAmount <= 0
								}
								className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-40"
								onClick={() =>
									targetMutation.mutate({
										period_no: activePeriod ?? "",
										market_code: "ttt14",
										target_amount: targetAmount,
										risk: targetRisk,
									})
								}
							>
								{targetMutation.isPending ? "反推中…" : "反推票面"}
							</button>
						</div>
						{plan ? (
							<div className="mt-3 space-y-2" data-testid="pool-target-result">
								<p className="text-xs text-muted-foreground">{plan.note}</p>
								<TicketRow
									label={`${RISK_LABELS[plan.risk]}档票面（${Object.keys(plan.ticket.picks).length} 场）`}
									ticket={plan.ticket}
									testid="pool-target-ticket"
									onAdopt={() => {
										setPicks(
											Object.fromEntries(Object.entries(plan.ticket.picks).map(([seq, code]) => [Number(seq), code])),
										);
										setTargetMessage("已采用反推票面作为当前选择");
									}}
								/>
								<p className="text-sm" data-testid="pool-target-units">
									单注估计派彩{" "}
									{plan.est_payout_per_unit === null || plan.est_payout_per_unit === undefined
										? "缺数据"
										: `¥${plan.est_payout_per_unit.toLocaleString("zh-CN")}`}
									，建议 <span className={TABULAR_NUMS}>{plan.suggested_units}</span> 注（¥2/注）达目标 ¥
									{targetAmount.toLocaleString("zh-CN")}
									{plan.target_reached ? "（单注即达标）" : ""}
								</p>
								{targetMessage ? (
									<p className="text-xs text-info" data-testid="pool-target-message">
										{targetMessage}
									</p>
								) : null}
							</div>
						) : null}
					</section>
				) : null}

				{/* 留位：AI 证据总结（not-available，随 M3） */}
				<section aria-label="随 M3 上线" className="mb-8 grid gap-3" data-testid="pool-coming-soon">
					<EmptyState variant="not-available" message="AI 证据总结（为何这样研判）" hint="随 M3 LLM 线上线。" />
				</section>

				{/* 提交：纸面池票（pool-slips 接线，票 43）；真金不呈现 */}
				<section aria-labelledby="pool-submit-heading" className="rounded-lg border border-border bg-card p-4">
					<h2 id="pool-submit-heading" className="text-sm font-medium">
						提交（纸面）
					</h2>
					<p className="mt-1 text-xs text-muted-foreground">
						提交建纸面池票（pool-slips 端点，复式组合物化落库，任9 一注 ¥{STAKE_PER_COMBINATION}）——
						奖池型真金开放须用户单独裁决（当前只纸面，红线）。
					</p>
					<button
						type="button"
						data-testid="pool-submit"
						disabled={pickedEntries.length < PICK9 || submitMutation.isPending || !detail}
						className="mt-3 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-40"
						onClick={submitPaperSlip}
					>
						{submitMutation.isPending
							? "提交中…"
							: pickedEntries.length < PICK9
								? `提交（纸面，任9 需 ≥${PICK9} 场）`
								: `提交纸面池票（${comboCount} 注 × ¥${STAKE_PER_COMBINATION}）`}
					</button>
					{submitResult ? (
						<p className="mt-2 text-xs" data-testid="pool-submit-result">
							{submitResult}
						</p>
					) : null}
				</section>
			</div>
		</AppShell>
	);
}
