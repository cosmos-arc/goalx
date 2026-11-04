import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import {
	createPoolSlip,
	fetchBankroll,
	fetchPoolPeriodDetail,
	fetchPoolPeriods,
	type PoolMatch,
	previewPoolSlip,
} from "../api/goalx";
import { AppShell } from "../components/app-shell";
import { EmptyState } from "../components/empty-state";
import { GlossaryTerm } from "../components/glossary-term";
import { MarketTabs } from "../components/market-tabs";
import { parlayAdviceInput, StakeAdviceNote } from "../components/stake-advice";
import {
	bankrollKey,
	poolPeriodDetailKeyFor,
	poolPeriodsKeyFor,
	poolSlipPreviewKey,
	slipsKey,
} from "../lib/query-keys";
import { errorText, pct, TABULAR_NUMS } from "../lib/ui";

/**
 * 票 10（report-audit-20261103）：四场进球（goals4）开面。
 *
 * 每场主/客各选进球档（0/1/2/3+，双侧可复式）；**同场联合 = 比分矩阵格
 * 聚合**（后端 goals4_joint，主客档相关——边际连乘口径错误，报告路线 D
 * 红线）；联合的边缘和 == 档位边际（后端一致性测试钉死）。金额/EV 用
 * 联合口径：配对份额 = 主档份额 × 客档份额（独立近似，披露）；票级 =
 * Π(1+EV_pair)−1（跨场独立假设沿用池票现状并披露）。
 *
 * 红线不变：paper 池票 flat 一档；真金禁用；生成器端点对 goals4 422
 * （一场两档硬跑产废票），本页不放生成器入口。
 */

/** goals4 市场码（贯通 API 查询参数）。 */
const MARKET = "goals4";
/** 四场进球固定场次数（与后端 GOALS4_MATCHES 单源语义一致）。 */
const GOALS4_MATCHES = 4;
/** 每注金额（goals4 一注 ¥2，规则价）。 */
const STAKE_PER_COMBINATION = 2;
/** 返奖率（口径镜像后端 POOL_RETURN_RATE，配对份额估计派彩用）。 */
const POOL_RETURN_RATE = 0.65;
/** 档位短标（h0..h3 / a0..a3 → 展示）。 */
const BAND_LABELS = ["0球", "1球", "2球", "3+球"] as const;

/** picks 状态形状：match_seq → 已选码（主/客档混装，前缀区分侧）。 */
type Goals4Picks = Record<number, string[]>;

function sideOf(code: string): "h" | "a" {
	return code.startsWith("h") ? "h" : "a";
}

/** 一场当前已成形的配对（主档 × 客档笛卡尔积；单侧缺 = 空）。 */
function pairsOf(picks: string[]): Array<{ home: string; away: string }> {
	const homes = picks.filter((c) => sideOf(c) === "h").sort();
	const aways = picks.filter((c) => sideOf(c) === "a").sort();
	return homes.flatMap((home) => aways.map((away) => ({ away, home })));
}

function selectionOf(match: PoolMatch, code: string) {
	return match.selections.find((sel) => sel.code === code);
}

/** 一场联合命中概率（所选配对联合之和；缺联合/缺选 = null）。 */
function pairJoint(match: PoolMatch, home: string, away: string): number | null {
	if (!match.goals4_joint) {
		return null;
	}
	const row = match.goals4_joint[home];
	const p = row?.[away];
	return p === undefined ? null : p;
}

function matchHitProb(match: PoolMatch, picks: string[]): number | null {
	if (picks.length === 0) {
		return null;
	}
	return pairsOf(picks).reduce<number | null>((acc, { away, home }) => {
		const joint = pairJoint(match, home, away);
		if (joint === null) {
			return null;
		}
		return (acc ?? 0) + joint;
	}, 0);
}

export function MarketPool4Page() {
	const periodsQuery = useQuery({
		queryKey: poolPeriodsKeyFor(MARKET),
		queryFn: () => fetchPoolPeriods(MARKET),
	});
	const bankrollQuery = useQuery({ queryKey: bankrollKey, queryFn: fetchBankroll });
	const queryClient = useQueryClient();

	const periods = periodsQuery.data ?? [];
	const defaultPeriod = periods.find((p) => p.status === "on_sale") ?? periods[0];
	const [period, setPeriod] = useState<string | null>(null);
	const activePeriod =
		period !== null && periods.some((p) => p.period_no === period) ? period : defaultPeriod?.period_no;
	const detailQuery = useQuery({
		queryKey: poolPeriodDetailKeyFor(MARKET, activePeriod),
		queryFn: () => fetchPoolPeriodDetail(activePeriod ?? "", MARKET),
		enabled: activePeriod !== undefined,
	});

	const [picks, setPicks] = useState<Goals4Picks>({});
	const [submitResult, setSubmitResult] = useState<string | null>(null);
	// 期次被动漂移（非用户切换：refetch 后默认期变化）时清 picks——goals4
	// 各期 match_seq 同为 1..4，跨期残留会是可提交的错期票（correctness I5）
	const userPickedPeriod = useRef<string | null>(null);
	useEffect(() => {
		if (activePeriod !== userPickedPeriod.current) {
			userPickedPeriod.current = activePeriod ?? null;
			setPicks({});
			setSubmitResult(null);
		}
	}, [activePeriod]);
	const submitMutation = useMutation({
		mutationFn: createPoolSlip,
		onSuccess: (slip) => {
			setSubmitResult(
				`已建纸面池票 #${slip.id}（${slip.combination_count} 注 × ¥${STAKE_PER_COMBINATION}，合计 ¥${slip.pool_stake_total}）`,
			);
			void queryClient.invalidateQueries({ queryKey: slipsKey });
		},
		onError: (error) => setSubmitResult(`提交失败：${errorText(error)}`),
	});

	const detail = detailQuery.data;
	const matches = detail?.matches ?? [];

	function toggle(matchSeq: number, code: string) {
		setSubmitResult(null);
		setPicks((current) => {
			const sel = current[matchSeq] ?? [];
			const next = sel.includes(code) ? sel.filter((c) => c !== code) : [...sel, code];
			return { ...current, [matchSeq]: next };
		});
	}

	/** 按推荐预填：联合最优配对（无联合面退化为两侧边际各自最优）。 */
	function prefillRecommended() {
		const next: Goals4Picks = {};
		for (const match of matches) {
			if (Object.keys(next).length >= GOALS4_MATCHES) {
				break;
			}
			if (match.goals4_joint) {
				let best: { home: string; away: string; prob: number } | null = null;
				for (const [home, row] of Object.entries(match.goals4_joint)) {
					for (const [away, prob] of Object.entries(row)) {
						if (best === null || prob > best.prob) {
							best = { away, home, prob };
						}
					}
				}
				if (best) {
					next[match.match_seq] = [best.home, best.away];
					continue;
				}
			}
			const top = (side: "h" | "a") =>
				match.selections
					.filter((s) => sideOf(s.code) === side && s.prob !== null)
					.sort((a, b) => (b.prob ?? 0) - (a.prob ?? 0))[0];
			const home = top("h");
			const away = top("a");
			if (home && away) {
				next[match.match_seq] = [home.code, away.code];
			}
		}
		setPicks(next);
		setSubmitResult(null);
	}

	/** 提交/预览共用 picks 载荷（同输入同展开序，票 01/10 单源在后端）。 */
	const picksPayload = useMemo(
		() =>
			matches.flatMap((m) =>
				(picks[m.match_seq] ?? []).map((code) => ({
					match_seq: m.match_seq,
					selection_code: code,
					...(m.fixture_id === null || m.fixture_id === undefined ? {} : { fixture_id: m.fixture_id }),
				})),
			),
		[matches, picks],
	);
	const picksSignature = useMemo(
		() => JSON.stringify(picksPayload.map(({ match_seq, selection_code }) => [match_seq, selection_code])),
		[picksPayload],
	);
	const previewQuery = useQuery({
		queryKey: poolSlipPreviewKey(MARKET, activePeriod, picksSignature),
		queryFn: () =>
			previewPoolSlip({
				market_code: MARKET,
				stake_per_combination: STAKE_PER_COMBINATION,
				picks: picksPayload,
			}),
		enabled: picksPayload.length > 0,
		placeholderData: (previous) => previous,
	});
	const comboCount = previewQuery.data?.combination_count ?? 0;

	/** 每场配对面（联合概率 + 可得时的配对 EV），建议区展示。 */
	const pickedMatches = matches.filter((m) => (picks[m.match_seq] ?? []).length > 0);
	const complete =
		pickedMatches.length === GOALS4_MATCHES && pickedMatches.every((m) => pairsOf(picks[m.match_seq] ?? []).length > 0);

	/** 票级建议（联合口径）：每场恰一配对时才出（复式多配对无单一票级 EV）。 */
	const singlePairs = complete
		? pickedMatches.flatMap((m) => {
				const [pair, ...rest] = pairsOf(picks[m.match_seq] ?? []);
				return pair === undefined || rest.length > 0 ? [] : [{ match: m, pair }];
			})
		: [];
	const adviceInput = useMemo(() => {
		const legs = singlePairs.map(({ match, pair }) => {
			const joint = pairJoint(match, pair.home, pair.away);
			const homeSel = selectionOf(match, pair.home);
			const awaySel = selectionOf(match, pair.away);
			const shareH = homeSel?.share ?? null;
			const shareA = awaySel?.share ?? null;
			const odds =
				shareH !== null && shareA !== null && shareH > 0 && shareA > 0
					? POOL_RETURN_RATE / (shareH * shareA)
					: Number.NaN; // 缺份额毒化票级赔率 → 整体降级（同任9页先例）
			const ev = joint === null || !Number.isFinite(odds) ? null : joint * odds - 1;
			return { ev, odds };
		});
		return parlayAdviceInput(legs);
	}, [singlePairs]);
	const hasValidAdvice = singlePairs.length === GOALS4_MATCHES && Number.isFinite(adviceInput.odds);
	const bankroll = bankrollQuery.data?.balance ?? (bankrollQuery.isError ? null : 0);

	/** 票级联合命中概率（复式场按所选配对联合求和；任一场缺联合 = null）。 */
	const ticketJointProb = useMemo(() => {
		if (!complete) {
			return null;
		}
		let prob = 1;
		for (const match of pickedMatches) {
			const hit = matchHitProb(match, picks[match.match_seq] ?? []);
			if (hit === null) {
				return null;
			}
			prob *= hit;
		}
		return prob;
	}, [complete, pickedMatches, picks]);

	function submitPaperSlip() {
		if (!detail) {
			return;
		}
		submitMutation.mutate({
			mode: "paper",
			pool_period_id: detail.pool_period_id,
			market_code: MARKET,
			stake_per_combination: STAKE_PER_COMBINATION,
			picks: picksPayload,
		});
	}

	return (
		<AppShell title="四场进球">
			<div className="pb-24">
				<MarketTabs />

				<header className="mb-5">
					<p className="text-sm text-muted-foreground" data-testid="pool4-caliber">
						{detail ? (
							detail.caliber
						) : (
							<>
								四场进球：每场主/客各选一档（0/1/2/3+球）；同场联合 = 比分矩阵格聚合（非边际连乘）；
								<GlossaryTerm id="pool-ev">彩池 EV</GlossaryTerm> 为彩池口径，配对份额 = 主档 × 客档（独立近似）。
							</>
						)}
					</p>
				</header>

				{periodsQuery.isError ? (
					<div className="mb-5">
						<EmptyState
							variant="backend-unavailable"
							message="连不上后端，goals4 期次数据加载失败。"
							hint={<>用 task server 启动 API；期次数据来自彩池同步。</>}
							action={{ label: "重试", onClick: () => void periodsQuery.refetch() }}
						/>
					</div>
				) : null}

				{!periodsQuery.isError && periodsQuery.isSuccess && periods.length === 0 ? (
					<div className="mb-5">
						<EmptyState
							variant="no-data"
							message="尚无 goals4 期次数据"
							hint={<>goals4 期次随彩池同步累积（14场任9 页"立即同步"同源拉取）。</>}
						/>
					</div>
				) : null}

				{periods.length > 0 ? (
					<>
						<section aria-labelledby="pool4-period-heading" className="mb-5">
							<div className="flex flex-wrap items-end justify-between gap-3">
								<h2 id="pool4-period-heading" className="text-sm font-medium">
									期次{" "}
									<span className="font-normal text-muted-foreground" data-testid="pool4-period-note">
										（四场进球，4 场固定，每场主/客各一档）
									</span>
								</h2>
								<div className="flex items-center gap-2">
									<label className="flex items-center gap-2 text-xs">
										<span className="text-muted-foreground">期次</span>
										<select
											data-testid="pool4-period-select"
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
										data-testid="pool4-prefill-recommended"
										className="rounded-md border border-border px-3 py-1.5 text-sm transition-colors hover:bg-muted"
										onClick={prefillRecommended}
									>
										按推荐标记预选
									</button>
								</div>
							</div>
							<p className="mt-2 text-xs" data-testid="pool4-pick-count">
								已选{" "}
								<span className={TABULAR_NUMS}>
									{pickedMatches.length}/{GOALS4_MATCHES}
								</span>{" "}
								场（每场须主/客档各至少一选；当前组合数 <span className={TABULAR_NUMS}>{comboCount}</span> 注）
								{ticketJointProb !== null ? (
									<>
										；票级联合命中概率 <span className={TABULAR_NUMS}>{pct(ticketJointProb, 1)}</span>
										（跨场独立假设）
									</>
								) : null}
							</p>
						</section>

						{/* 逐场 8 档选择面：主档组 + 客档组，双侧可复式 */}
						<section aria-label="四场进球选择面" className="mb-8 space-y-3" data-testid="pool4-matches">
							{matches.map((match) => {
								const sel = picks[match.match_seq] ?? [];
								const jointReady = match.goals4_joint !== null && match.goals4_joint !== undefined;
								return (
									<div
										key={match.match_seq}
										className="rounded-lg border border-border bg-card p-3"
										data-testid="pool4-match"
									>
										<div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
											<p className="text-sm font-medium">
												{match.match_seq}. {match.home_team} vs {match.away_team}
												<span className="ml-2 font-normal text-xs text-muted-foreground">{match.league}</span>
											</p>
											{!jointReady ? (
												<span className="text-xs text-muted-foreground" data-testid="pool4-joint-missing">
													无联合面（未映射/无预测）——只展示档位，不出联合
												</span>
											) : null}
										</div>
										<div className="grid gap-2 sm:grid-cols-2">
											{(["h", "a"] as const).map((side) => (
												<div key={side} className="flex flex-wrap items-center gap-1.5">
													<span className="w-8 text-xs text-muted-foreground">{side === "h" ? "主队" : "客队"}</span>
													{[0, 1, 2, 3].map((band) => {
														const code = `${side}${band}`;
														const view = selectionOf(match, code);
														const active = sel.includes(code);
														return (
															<button
																key={code}
																type="button"
																data-testid={`pool4-band-${match.match_seq}-${code}`}
																aria-pressed={active}
																className={`rounded-md border px-2.5 py-1 text-sm transition-colors ${
																	active
																		? "border-primary bg-primary font-medium text-primary-foreground"
																		: "border-border text-foreground hover:bg-muted"
																}`}
																onClick={() => toggle(match.match_seq, code)}
															>
																{BAND_LABELS[band]}
																{view?.prob !== null && view?.prob !== undefined ? (
																	<span className={`ml-1 text-xs ${active ? "" : "text-muted-foreground"}`}>
																		{pct(view.prob, 1)}
																	</span>
																) : null}
															</button>
														);
													})}
												</div>
											))}
										</div>
										{jointReady && pairsOf(sel).length > 0 ? (
											<p className="mt-2 text-xs text-muted-foreground" data-testid="pool4-pair-joint">
												已选配对联合：
												{pairsOf(sel)
													.map(({ away, home }) => {
														const joint = pairJoint(match, home, away);
														return `${home}/${away} = ${joint === null ? "—" : pct(joint, 1)}`;
													})
													.join(" · ")}
												（矩阵格聚合，非边际连乘）
											</p>
										) : null}
									</div>
								);
							})}
						</section>

						{/* 额度建议：联合口径；paper flat（奖池红线） */}
						<section aria-labelledby="pool4-tiers-heading" className="mb-8">
							<h2 id="pool4-tiers-heading" className="mb-2 text-sm font-medium">
								额度建议 <span className="font-normal text-muted-foreground">纸面 flat（奖池型红线）</span>
							</h2>
							<p className="mb-3 text-xs text-muted-foreground" data-testid="pool4-tier-mapping">
								联合口径：每场配对 EV = 联合概率 × 配对估计赔率 − 1（配对份额 = 主档 × 客档，独立近似； 份额无源时不出
								EV）；票级 = Π(1+EV)−1（跨场独立假设沿用池票现状）。EV≤0 时建议 ¥0。
								复式（一场多配对）时不出单一票级建议，按组合数 × ¥{STAKE_PER_COMBINATION} 计金额。
							</p>
							{pickedMatches.length === 0 ? (
								<p
									className="rounded-md border border-dashed border-border p-4 text-sm text-muted-foreground"
									data-testid="pool4-tiers-empty"
								>
									先在上方 4 场列表选择主/客档（或"按推荐标记预选"）——建议随所选组合计算。
								</p>
							) : (
								<div className="grid gap-2 sm:grid-cols-3" data-testid="pool4-tiers">
									<StakeAdviceNote
										mode="paper"
										bankroll={bankroll}
										ev={hasValidAdvice ? adviceInput.ev : null}
										odds={hasValidAdvice ? adviceInput.odds : 2}
										label="保守档（flat，纸面）"
										note={`组合 ${comboCount} 注 × ¥${STAKE_PER_COMBINATION}/注（goals4；联合口径估计）`}
										testid="pool4-tier-conservative"
									/>
									<p
										className="rounded-md border border-dashed border-border p-3 text-xs text-muted-foreground"
										data-testid="pool4-tier-live-disabled"
									>
										标准/激进档（live ¼Kelly）——奖池型真金未开放（须用户单独裁决），禁用。
									</p>
								</div>
							)}
						</section>

						{/* 提交：纸面池票；真金不呈现；生成器不放行（422 护栏维持） */}
						<section
							aria-labelledby="pool4-submit-heading"
							className="rounded-lg border border-border bg-card p-4"
							data-testid="pool4-submit"
						>
							<h2 id="pool4-submit-heading" className="text-sm font-medium">
								提交（纸面）
							</h2>
							<p className="mt-1 text-xs text-muted-foreground">
								提交建纸面 goals4 池票（复式组合物化落库，一注 ¥{STAKE_PER_COMBINATION}）——
								奖池型真金开放须用户单独裁决（当前只纸面，红线）。生成器（搏冷/目标票）对 goals4
								维持不放行（一场两档语义）。
							</p>
							<button
								type="button"
								data-testid="pool4-submit-button"
								disabled={!complete || submitMutation.isPending || !detail}
								className="mt-3 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-40"
								onClick={submitPaperSlip}
							>
								{submitMutation.isPending
									? "提交中…"
									: complete
										? `提交纸面池票（${comboCount} 注 × ¥${STAKE_PER_COMBINATION}）`
										: `提交（纸面，需 ${GOALS4_MATCHES} 场各选主/客档）`}
							</button>
							{submitResult ? (
								<p className="mt-2 text-xs" data-testid="pool4-submit-result">
									{submitResult}
								</p>
							) : null}
						</section>
					</>
				) : null}
			</div>
		</AppShell>
	);
}
