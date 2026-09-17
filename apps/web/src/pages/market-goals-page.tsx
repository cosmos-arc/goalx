import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import type { GoalsFixture, GoalsSelection } from "../api/goalx";
import { createBet, fetchBankroll, fetchGoalsMarket } from "../api/goalx";
import { AppShell } from "../components/app-shell";
import { EmptyState } from "../components/empty-state";
import { GlossaryTerm } from "../components/glossary-term";
import { type GoalsLeg, GoalsMarketCard, goalsLabel } from "../components/goals-ui";
import { evClass } from "../components/had-quote-ui";
import { MarketTabs } from "../components/market-tabs";
import {
	Drawer,
	DrawerClose,
	DrawerContent,
	DrawerDescription,
	DrawerFooter,
	DrawerHeader,
	DrawerTitle,
} from "../components/ui/drawer";
import { buildGoalsCombo, GOALS_COMBO_CONFIG, type GoalsMarket, rankGoalsFixturesForFeed } from "../lib/combo-engine";
import { beijingBusinessDate, dayLabel, errorText, TABULAR_NUMS } from "../lib/ui";

/**
 * 票 wb-05：进球玩法页 `/markets/goals`——玩法轴第二页（ttg/crs）。
 * - 口径（页头词典标注）：概率由比分矩阵推导（单市场不独立建模）；EV =
 *   模型概率 × 竞彩价 − 1（模型×竞彩价，与胜平负页的共识 EV 不同源）；
 *   进球类以单关为主（不组串、单注 1–5% flat）。
 * - 组合头部：`buildGoalsCombo` 纯函数（EV 排序 top-N 独立单关；置信信号
 *   缺位不加权并如实标注）；推荐流：3 日窗口按最优选项 EV 降序，无模型
 *   场沉底；一键带入/就地选注都进本页选注篮（每注独立单关提交）。
 */

/** 场次窗口与场次/胜平负页同宽。 */
const GOALS_WINDOW_DAYS = 3;

/** 选注篮上限：与引擎 top-N 一致（单关为主，一篮最多 3 注独立单关）。 */
const MAX_GOALS_LEGS = GOALS_COMBO_CONFIG.maxPicks;

export function MarketGoalsPage() {
	const goalsQuery = useQuery({
		queryKey: ["goals-market", GOALS_WINDOW_DAYS],
		queryFn: () => fetchGoalsMarket(undefined, GOALS_WINDOW_DAYS),
	});
	const bankrollQuery = useQuery({ queryKey: ["bankroll"], queryFn: fetchBankroll });
	const queryClient = useQueryClient();
	const [market, setMarket] = useState<GoalsMarket>("ttg");
	const [legs, setLegs] = useState<GoalsLeg[]>([]);
	const [basketOpen, setBasketOpen] = useState(false);
	const [stake, setStake] = useState("100");
	const [strategyVersion, setStrategyVersion] = useState("");
	const [mode, setMode] = useState<"paper" | "live">("paper");
	const [message, setMessage] = useState<string | null>(null);
	const now = Date.now();

	const rows = goalsQuery.data ?? [];
	const feedOrder = goalsQuery.data !== undefined ? rankGoalsFixturesForFeed(rows) : [];
	const combo =
		goalsQuery.data !== undefined && bankrollQuery.isSuccess
			? buildGoalsCombo({ rows, bankroll: bankrollQuery.data.balance ?? 0, now })
			: null;

	/** 跨日标签（推荐流跨 3 日窗口）。 */
	function dayNoteOf(businessDate: string): string | undefined {
		const day = businessDate ?? beijingBusinessDate(now);
		return day === beijingBusinessDate(now) ? undefined : dayLabel(day, now);
	}

	/** 选注规则：同选项再点=移除；上限=行内提示；独立单关允许同场多注。 */
	function pick(fixture: GoalsFixture, pickedMarket: GoalsMarket, selection: GoalsSelection) {
		if (selection.odds === null || selection.odds === undefined) {
			return;
		}
		const existing = legs.find(
			(leg) => leg.fixture_id === fixture.fixture_id && leg.market === pickedMarket && leg.selection === selection.code,
		);
		if (existing) {
			setLegs(legs.filter((leg) => leg !== existing));
			setMessage(null);
			return;
		}
		if (legs.length >= MAX_GOALS_LEGS) {
			setMessage(`已达 ${MAX_GOALS_LEGS} 注上限（进球类单关为主）——先在选注篮移除一注`);
			return;
		}
		setLegs([
			...legs,
			{
				fixture_id: fixture.fixture_id,
				match_code: fixture.match_code,
				home_team: fixture.home_team,
				away_team: fixture.away_team,
				market: pickedMarket,
				selection: selection.code,
				label: goalsLabel(pickedMarket, selection.code),
				odds: selection.odds,
			},
		]);
		setMessage(null);
	}

	function removeLeg(index: number) {
		setLegs(legs.filter((_, i) => i !== index));
		setMessage(null);
	}

	/** 一键带入：组合腿就地进本页选注篮（每注独立单关，注额预填）。 */
	function applyCombo() {
		if (combo === null || combo.picks.length === 0) {
			return;
		}
		setLegs(
			combo.picks.map((pickEntry) => ({
				fixture_id: pickEntry.fixture.fixture_id,
				match_code: pickEntry.fixture.match_code,
				home_team: pickEntry.fixture.home_team,
				away_team: pickEntry.fixture.away_team,
				market: pickEntry.market,
				selection: pickEntry.selection,
				label: goalsLabel(pickEntry.market, pickEntry.selection),
				odds: pickEntry.odds,
			})),
		);
		setStake(String(combo.picks[0]?.stake ?? GOALS_COMBO_CONFIG.stake.minStakeCny));
		setMessage(
			`已带入 ${combo.picks.length} 条推荐——每条按独立单关提交（建议注额 ¥${combo.picks[0]?.stake.toFixed(2)}/注）`,
		);
		setBasketOpen(true);
	}

	const createSuggestion = useMutation({
		mutationFn: async () => {
			// 进球类以单关为主：每腿独立一注（服务器侧 non-had 腿按既有行为不校验）
			const created = [];
			for (const leg of legs) {
				created.push(
					await createBet({
						mode,
						stake: Number(stake),
						strategy_version: strategyVersion.trim() === "" ? null : strategyVersion.trim(),
						legs: [
							{
								fixture_id: leg.fixture_id,
								market_code: leg.market,
								selection_code: leg.selection,
								locked_odds: leg.odds,
							},
						],
					}),
				);
			}
			return created;
		},
		onSuccess: (created) => {
			setMessage(`已建 ${created.length} 条单关建议 — 去投注页锁定`);
			setLegs([]);
			setBasketOpen(false);
			void queryClient.invalidateQueries({ queryKey: ["bets"] });
		},
		onError: (error) => setMessage(`建注失败：${errorText(error)}`),
	});

	return (
		<AppShell title="进球">
			<div className="pb-24">
				<MarketTabs />
				<header className="mb-5">
					<div className="flex flex-wrap items-baseline justify-between gap-3">
						<p className="text-sm text-muted-foreground" data-testid="goals-caliber">
							总进球/比分，今天起 3 天窗口。<GlossaryTerm id="score-matrix">概率由比分矩阵推导</GlossaryTerm>；
							<GlossaryTerm id="model-prob">EV = 模型概率 × 竞彩价 − 1</GlossaryTerm>（模型×竞彩价，与胜平负页的共识 EV
							不同源）；<GlossaryTerm id="single">单关为主</GlossaryTerm>，不组串。
						</p>
						<p className="text-xs text-muted-foreground" data-testid="goals-bankroll-note">
							{bankrollQuery.isError ? (
								<span>资金池读取失败——按竞彩最低注建议，重试可去资金页</span>
							) : (
								<span className={TABULAR_NUMS}>
									flat 档单注 ¥
									{combo?.picks[0] !== undefined
										? combo.picks[0].stake.toFixed(2)
										: GOALS_COMBO_CONFIG.stake.minStakeCny.toFixed(2)}
									（bankroll ¥{(bankrollQuery.data?.balance ?? 0).toFixed(2)} ·{" "}
									{(GOALS_COMBO_CONFIG.stake.flatFraction * 100).toFixed(0)}%）
								</span>
							)}
						</p>
					</div>
					<p className="mt-1 text-xs text-muted-foreground">
						配色：红 = 正向 EV · 绿 = 负向 EV · 概率空缺 = 无模型覆盖（仅五大在售场有 Forecast）
					</p>
				</header>

				{message ? (
					<div
						role="status"
						data-testid="goals-market-message"
						className="fixed inset-x-0 bottom-16 z-50 mx-auto w-fit max-w-[min(92vw,42rem)] rounded-md bg-muted px-3 py-1.5 text-sm text-foreground shadow-sm"
					>
						{message}
					</div>
				) : null}

				{goalsQuery.isPending ? (
					<div data-testid="goals-loading" className="space-y-4">
						<span className="sr-only">加载进球玩法推荐…</span>
						<div className="h-5 w-64 animate-pulse rounded bg-muted" />
						<div className="h-32 animate-pulse rounded-lg bg-muted" />
						<div className="grid gap-3 sm:grid-cols-2">
							<div className="h-36 animate-pulse rounded-lg bg-muted" />
							<div className="h-36 animate-pulse rounded-lg bg-muted" />
						</div>
					</div>
				) : null}

				{goalsQuery.isError ? (
					<EmptyState
						variant="backend-unavailable"
						message="连不上后端，进球玩法加载失败。"
						hint={
							<>
								用 <code>task server</code> 启动 API；进球数据随 <code>task ingest-jingcai</code> 的 ttg/crs
								玩法一同拉取。
							</>
						}
						action={{ label: "重试", onClick: () => void goalsQuery.refetch() }}
					/>
				) : null}

				{goalsQuery.data && goalsQuery.data.length === 0 ? (
					<EmptyState
						variant="no-data"
						message="3 天内无在售场次。"
						hint={
							<>
								玩法推荐随竞彩开售更新；刚搭好环境先跑 <code>task ingest-jingcai</code>。
							</>
						}
						action={{ label: "刷新", onClick: () => void goalsQuery.refetch() }}
					/>
				) : null}

				{goalsQuery.data && goalsQuery.data.length > 0 ? (
					<>
						{/* 组合头部卡（票 wb-05）：引擎产出 + 约束说明 + 一键带入 */}
						<section
							aria-labelledby="goals-combo-heading"
							data-testid="goals-combo"
							className="mb-8 rounded-lg border border-border bg-card p-4"
						>
							<div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
								<h2 id="goals-combo-heading" className="text-sm font-medium">
									组合推荐 v1{" "}
									<span className="font-normal text-muted-foreground">
										进球 · 单关注单{" "}
										<span className={TABULAR_NUMS} data-testid="goals-combo-count">
											{combo?.picks.length ?? 0}
										</span>
									</span>
								</h2>
								{combo && combo.picks.length > 0 ? (
									<p className={`${TABULAR_NUMS} text-sm`} data-testid="goals-combo-total">
										总注额 ¥{combo.totalStake.toFixed(2)} · 预期收益{" "}
										<span className={evClass(combo.expectedProfit)}>+¥{combo.expectedProfit.toFixed(2)}</span>
										<span className="text-xs font-normal text-muted-foreground">
											（<GlossaryTerm id="model-prob">EV×注额</GlossaryTerm> 模型×竞彩价口径）
										</span>
									</p>
								) : null}
							</div>

							{combo === null ? (
								<p className="text-sm text-muted-foreground" data-testid="goals-combo-pending">
									{bankrollQuery.isError
										? "资金池读取失败——注额建议暂缺，去资金页重试后再看组合"
										: "组合计算中（等待场次与资金池数据）…"}
								</p>
							) : combo.picks.length === 0 ? (
								<p
									data-testid="goals-combo-empty"
									className="rounded-md border border-dashed border-border p-4 text-sm text-muted-foreground"
								>
									当前窗口无正 EV 选项——EV≤0 不入选，或场次无模型覆盖（仅五大在售场有 Forecast）；下方推荐流供逐场研判。
								</p>
							) : (
								<>
									<ul className="mb-3 space-y-2">
										{combo.picks.map((pickEntry) => (
											<li
												key={`${pickEntry.fixture.fixture_id}-${pickEntry.market}-${pickEntry.selection}`}
												data-testid={`goals-combo-pick-${pickEntry.fixture.fixture_id}`}
												className="flex flex-wrap items-baseline justify-between gap-2 rounded-md border border-border p-3 text-sm"
											>
												<span>
													<span className="text-xs text-muted-foreground">{pickEntry.fixture.match_code}</span>{" "}
													{pickEntry.fixture.home_team} vs {pickEntry.fixture.away_team} ·{" "}
													<span className="font-medium">
														{pickEntry.market === "ttg" ? "总进球" : "比分"}{" "}
														{goalsLabel(pickEntry.market, pickEntry.selection)}
													</span>
													<span className={`${TABULAR_NUMS} ml-1.5 text-muted-foreground`}>
														@{pickEntry.odds.toFixed(2)}
													</span>
													<span className={`${TABULAR_NUMS} ml-2 text-muted-foreground`}>
														模型概率 {(pickEntry.probability * 100).toFixed(1)}%
													</span>
													<span className={`${TABULAR_NUMS} ml-2 ${evClass(pickEntry.ev)}`}>
														EV +{(pickEntry.ev * 100).toFixed(1)}%
													</span>
												</span>
												<span className={`${TABULAR_NUMS} text-xs text-muted-foreground`}>
													建议注额 ¥{pickEntry.stake.toFixed(2)} · 预期{" "}
													<span className={evClass(pickEntry.expectedProfit)}>
														+¥{pickEntry.expectedProfit.toFixed(2)}
													</span>
												</span>
											</li>
										))}
									</ul>
									{combo.bankrollNote ? (
										<p
											data-testid="goals-combo-bankroll-warning"
											className="mb-3 rounded bg-warning/10 px-2 py-1 text-xs text-foreground"
										>
											{combo.bankrollNote}
										</p>
									) : null}
									<button
										type="button"
										data-testid="goals-combo-apply"
										className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
										onClick={applyCombo}
									>
										一键带入选注篮
									</button>
								</>
							)}

							{combo ? (
								<ul className="mt-3 space-y-0.5 text-xs text-muted-foreground" data-testid="goals-combo-notes">
									{combo.notes.map((note) => (
										<li key={note}>· {note}</li>
									))}
								</ul>
							) : null}
						</section>

						{/* 推荐流：玩法切换 + 全部场次按引擎排序 */}
						<section aria-labelledby="goals-feed-heading">
							<div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
								<h2 id="goals-feed-heading" className="text-sm font-medium">
									推荐流 <span className={`${TABULAR_NUMS} text-muted-foreground`}>{feedOrder.length}</span>{" "}
									<span className="text-xs font-normal text-muted-foreground">
										按模型 EV 排序；无模型场按开赛时间沉底
									</span>
								</h2>
								<div
									role="tablist"
									aria-label="玩法切换"
									className="flex gap-1 text-xs"
									data-testid="goals-market-toggle"
								>
									{(["ttg", "crs"] as const).map((code) => (
										<button
											key={code}
											type="button"
											role="tab"
											aria-selected={market === code}
											data-testid={`goals-market-tab-${code}`}
											className={`rounded-md border px-2.5 py-1 transition-colors ${
												market === code
													? "border-primary bg-primary font-medium text-primary-foreground"
													: "border-border text-muted-foreground hover:bg-muted hover:text-foreground"
											}`}
											onClick={() => setMarket(code)}
										>
											{code === "ttg" ? "总进球" : "比分"}
										</button>
									))}
								</div>
							</div>
							<div className="grid gap-3 sm:grid-cols-2" data-testid="goals-feed">
								{feedOrder.map((fixture) => (
									<GoalsMarketCard
										key={fixture.fixture_id}
										fixture={fixture}
										market={market}
										now={now}
										legs={legs}
										onPick={(f, m, sel) => pick(f, m, sel)}
										dayNote={dayNoteOf(fixture.business_date)}
									/>
								))}
							</div>
						</section>
					</>
				) : null}
			</div>

			{/* 底部常驻选注条 + 右侧 Drawer（每注独立单关，复用场次页模式） */}
			<div className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-background/95 backdrop-blur">
				<div className="mx-auto flex max-w-6xl items-center gap-3 px-6 py-3">
					<span className="text-sm">
						选注篮{" "}
						<span className={`${TABULAR_NUMS} font-medium`} data-testid="goals-basket-count">
							{legs.length}/{MAX_GOALS_LEGS}
						</span>
						<span className="ml-2 hidden text-xs text-muted-foreground sm:inline">独立单关</span>
					</span>
					{legs.length > 0 ? (
						<span
							className={`${TABULAR_NUMS} hidden text-xs text-muted-foreground sm:inline`}
							data-testid="goals-basket-summary"
						>
							{legs
								.map((leg) => `${leg.match_code} ${leg.market === "ttg" ? "总进球" : "比分"} ${leg.label}`)
								.join("；")}
						</span>
					) : (
						<span className="hidden text-xs text-muted-foreground sm:inline">
							点推荐流赔率或"一键带入"加入；单关须单固
						</span>
					)}
					<button
						type="button"
						data-testid="goals-basket-open"
						className="ml-auto rounded-md border border-border bg-background px-3 py-1.5 text-sm font-medium transition-colors hover:bg-muted disabled:pointer-events-none disabled:opacity-50"
						disabled={legs.length === 0}
						onClick={() => setBasketOpen(true)}
					>
						展开选注篮
					</button>
				</div>
			</div>

			<Drawer open={basketOpen} onOpenChange={setBasketOpen} swipeDirection="right">
				<DrawerContent className="mx-0 w-full sm:max-w-md">
					<DrawerHeader>
						<DrawerTitle>选注篮（独立单关）</DrawerTitle>
						<DrawerDescription>
							每条按一注独立单关提交（进球类不组串）；单关须单固（提交时以服务器为准）
						</DrawerDescription>
					</DrawerHeader>
					<div className="flex-1 overflow-y-auto p-4">
						{legs.length === 0 ? (
							<p className="text-sm text-muted-foreground">未选择。点推荐流赔率或组合卡"一键带入"加入。</p>
						) : (
							<ul className="space-y-2">
								{legs.map((leg, index) => (
									<li
										key={`${leg.fixture_id}-${leg.market}-${leg.selection}`}
										className="flex items-center justify-between rounded-md border border-border p-3 text-sm"
										data-testid="goals-basket-leg"
									>
										<span>
											<span className="text-xs text-muted-foreground">{leg.match_code}</span>
											<br />
											{leg.home_team} vs {leg.away_team}
											<br />
											<span className="font-medium">
												{leg.market === "ttg" ? "总进球" : "比分"} {leg.label}
											</span>
											<span className={`${TABULAR_NUMS} ml-2 text-muted-foreground`}>@{leg.odds.toFixed(2)}</span>
										</span>
										<button
											type="button"
											className="rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
											aria-label={`移除 ${leg.match_code} ${leg.label}`}
											onClick={() => removeLeg(index)}
										>
											移除
										</button>
									</li>
								))}
							</ul>
						)}
					</div>
					<DrawerFooter>
						<div className="flex flex-wrap items-end gap-3">
							<label className="flex flex-col gap-1 text-xs">
								<span className="text-muted-foreground">模式</span>
								<select
									data-testid="goals-basket-mode"
									className="rounded-md border border-input bg-background px-2 py-1.5 text-sm"
									value={mode}
									onChange={(event) => setMode(event.target.value === "live" ? "live" : "paper")}
								>
									<option value="paper">纸面</option>
									<option value="live">真金</option>
								</select>
							</label>
							<label className="flex flex-col gap-1 text-xs">
								<span className="text-muted-foreground">每注金额(¥)</span>
								<input
									required
									type="number"
									min={2}
									step="0.01"
									data-testid="goals-basket-stake"
									className="w-24 rounded-md border border-input bg-background px-2 py-1.5 text-sm"
									value={stake}
									onChange={(event) => setStake(event.target.value)}
								/>
							</label>
							<label className="flex flex-col gap-1 text-xs">
								<span className="text-muted-foreground">策略版本(可选)</span>
								<input
									data-testid="goals-basket-strategy"
									placeholder="手动"
									className="w-32 rounded-md border border-input bg-background px-2 py-1.5 text-sm"
									value={strategyVersion}
									onChange={(event) => setStrategyVersion(event.target.value)}
								/>
							</label>
						</div>
						<button
							type="button"
							data-testid="goals-basket-submit"
							className="w-full rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-40"
							disabled={createSuggestion.isPending || legs.length === 0}
							onClick={() => createSuggestion.mutate()}
						>
							建立 {legs.length > 0 ? `${legs.length} 注` : ""}单关建议
						</button>
						<div className="flex items-center justify-between text-xs">
							<DrawerClose className="rounded-md px-1 py-0.5 text-muted-foreground underline-offset-2 hover:text-foreground hover:underline">
								继续浏览
							</DrawerClose>
							<Link to="/bets" className="text-primary underline-offset-2 hover:underline">
								去投注页锁定 →
							</Link>
						</div>
					</DrawerFooter>
				</DrawerContent>
			</Drawer>
		</AppShell>
	);
}
