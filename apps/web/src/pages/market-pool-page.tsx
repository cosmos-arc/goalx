import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import {
	createPoolSlip,
	fetchBankroll,
	fetchPoolPeriodDetail,
	fetchPoolPeriods,
	fetchPoolSyncStatus,
	runPoolSync,
} from "../api/goalx";
import { AppShell } from "../components/app-shell";
import { EmptyState } from "../components/empty-state";
import { EvidenceCardSection } from "../components/evidence-card";
import { GlossaryTerm } from "../components/glossary-term";
import { MarketTabs } from "../components/market-tabs";
import { PoolGenerator, PoolTargetPlanner } from "../components/pool-generators";
import { bestSelection, type PoolPickEntry, PoolSlotList, selectionOf } from "../components/pool-slots";
import { parlayAdviceInput, StakeAdviceNote } from "../components/stake-advice";
import { errorText, TABULAR_NUMS } from "../lib/ui";

/**
 * 票 43：14场任9 页点亮（wb-07 骨架毕业）；票 review-20260928/07 按区块拆分
 * （槽位列表 → components/pool-slots，生成器/反推 → components/pool-generators，
 * 各自 mutation/消息状态随组件走；页面只留取数、期次选择、picks 状态与提交）。
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
	const submitMutation = useMutation({
		mutationFn: createPoolSlip,
		onSuccess: (slip) => {
			setSubmitResult(`已建纸面池票 #${slip.id}`);
			void queryClient.invalidateQueries({ queryKey: ["slips"] });
		},
		onError: (error) => setSubmitResult(`提交失败：${errorText(error)}`),
	});

	const detail = detailQuery.data;
	const matches = detail?.matches ?? [];
	// 已选（一场一选）：match + 所选向，索引访问经 flatMap 收窄（无 undefined）
	const pickedEntries: PoolPickEntry[] = matches.flatMap((m) => {
		const code = picks[m.match_seq];
		return code === undefined ? [] : [{ match: m, code }];
	});
	const comboCount = combinations(pickedEntries.length, PICK9);
	/** 三档输入：所选组合联合口径（parlayAdviceInput 正典——估计派彩赔率 Π × 联合 EV = Π(1+ev)−1）。 */
	const adviceInput = useMemo(
		() =>
			parlayAdviceInput(
				pickedEntries.map(({ match, code }) => {
					const sel = selectionOf(match, code);
					return {
						ev: sel?.ev ?? null,
						odds: sel?.implied_odds ?? Number.NaN, // 缺份额的向毒化联合赔率 → 下方整体降级
					};
				}),
			),
		[pickedEntries],
	);
	const hasValidInput = pickedEntries.length > 0 && Number.isFinite(adviceInput.odds);
	const bankroll = bankrollQuery.data?.balance ?? (bankrollQuery.isError ? null : 0);

	function pick(matchSeq: number, selection: string) {
		setSubmitResult(null);
		setPicks((current) => {
			const next = { ...current };
			if (next[matchSeq] === selection) {
				delete next[matchSeq]; // 再点同向 = 取消
			} else {
				next[matchSeq] = selection; // 一场一选（任9 口径）
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
	/** 工具区采用票面 → 页面 picks（采用消息由各工具组件本地呈现）。 */
	function adoptPicks(next: Record<number, string>) {
		setPicks(next);
		setSubmitResult(null);
	}

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

				{periods.length > 0 ? (
					<>
						{/* 期次选择 + 预选 */}
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

						{/* 销量区块：AI 代采待命（官方销量四源直接 GET 不可得，票 43 兜底层） */}
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

						<PoolSlotList
							matches={matches}
							picks={picks}
							emptySlots={emptySlots}
							onPick={(match, selection) => pick(match.match_seq, selection)}
						/>

						{/* 额度建议：奖池红线 = 只纸面 flat（真金档位禁用不呈现） */}
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

						<PoolGenerator
							activePeriod={activePeriod}
							pickedEntries={pickedEntries}
							matchCount={matches.length}
							onAdopt={adoptPicks}
						/>
						<PoolTargetPlanner activePeriod={activePeriod} onAdopt={adoptPicks} />

						{/* AI 证据总结（票 14 V1 证据卡转正：存量工件渲染 + 诚实降级） */}
						<section
							aria-labelledby="pool-evidence-heading"
							className="mb-8 rounded-lg border border-border bg-card p-4"
							data-testid="pool-evidence"
						>
							<h2 id="pool-evidence-heading" className="mb-2 text-sm font-medium">
								AI 证据总结{" "}
								<span className="font-normal text-muted-foreground">
									为何这样研判——情报/概率证据（advisory，不改注策）
								</span>
							</h2>
							{activePeriod ? <EvidenceCardSection periodNo={activePeriod} /> : null}
						</section>
					</>
				) : null}

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
