import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";
import {
	fetchBankroll,
	fetchBets,
	fetchDrawResults,
	fetchTodayFixtures,
	fetchValidationProgress,
	type TodayFixture,
} from "../api/goalx";
import { AppShell } from "../components/app-shell";
import { EmptyState } from "../components/empty-state";
import { Badge } from "../components/ui/badge";
import { TABULAR_NUMS } from "../lib/ui";

/**
 * 票 15：总览 Dashboard（票 05 定稿 = 唯一事实源）。总览 = 分诊（票 03）：
 * 待办清单卡（四规则）+ 首屏快照四卡（真金/纸面分区隔离）+ 页底验证进度细线。
 * 本页不做任何写操作；每张卡附判读方向，不裸放数字；红涨绿跌配正负号（票 02）。
 * 数据源 = 现有端点前端拼装（票 10 定稿，零 contract 变更），react-query 缓存键与
 * 今日/投注/资金/验证各页对齐，跨页返回不重拉。
 *
 * 判定边界口径（与票 05 规则一一对应）：
 * - 规则①②的开赛时点只能对今日列表内的场次推导；历史挂注场次无 kickoff 数据，不下判不误报。
 * - 规则③ = 未结注的全部腿已有开奖（与后端结算批跑同口径，不区分是否已锁定）。
 * - 规则④无 eu_updated_at（票 10 顺带项挂起），与票 14 同口径用 joined 覆盖近似；
 *   只在"整份快照缺"（含当日 0 场）且过了 10 点才触发，部分覆盖是常态不算异常，
 *   覆盖明细留 today 页健康行。
 * - 今日真金盈亏 = 当日 bankroll 投注流水净额（兑付 − 注金）；入金/出金/成本不计入盈亏。
 */

type TodoItem = {
	key: string;
	testid: string;
	text: string;
	/** 倒计时/已开赛片段；tone = 琥珀紧迫（<2h 或已开赛），否则中性。 */
	urgent?: { text: string; tone: boolean } | undefined;
	/** 就地指引（可含命令），不做跳转。 */
	hint?: ReactNode | undefined;
	to?: "/today" | "/bets";
	actionLabel?: string;
};

function pnlClass(value: number | null): string {
	if (value === null || value === 0) {
		return "text-muted-foreground";
	}
	return value > 0 ? "text-profit" : "text-loss";
}

function signedCny(value: number): string {
	return `${value >= 0 ? "+" : "-"}¥${Math.abs(value).toFixed(2)}`;
}

function signedPct(value: number): string {
	return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(1)}%`;
}

function startOfLocalDay(ms: number): number {
	const date = new Date(ms);
	return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

function isSameLocalDay(iso: string, now: number): boolean {
	const at = new Date(iso).getTime();
	return !Number.isNaN(at) && startOfLocalDay(at) === startOfLocalDay(now);
}

/** 可投 = 判定 valid + 在售 + 未开赛（与今日页 isPickable 同口径）。 */
function isPickable(fixture: TodayFixture, now: number): boolean {
	const quote = fixture.had_quote;
	if (quote?.status !== "valid" || quote.sale_state !== "on_sale") {
		return false;
	}
	const kickoff = new Date(fixture.kickoff_utc).getTime();
	return !Number.isNaN(kickoff) && kickoff > now;
}

/** 待办行：状态点 + 一句话 + （琥珀紧迫/就地指引）+ 动作链接。 */
function TodoRow({ item }: { item: TodoItem }) {
	return (
		<li className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-sm" data-testid={item.testid}>
			<span aria-hidden className="h-2 w-2 shrink-0 self-center rounded-full bg-muted-foreground/40" />
			<span>{item.text}</span>
			{item.urgent ? (
				<span className={`text-xs font-medium ${item.urgent.tone ? "text-warning" : "text-muted-foreground"}`}>
					{item.urgent.text}
				</span>
			) : null}
			{item.hint ? <span className="w-full text-xs text-muted-foreground">{item.hint}</span> : null}
			{item.to && item.actionLabel ? (
				<Link to={item.to} className="ml-auto text-xs text-primary underline-offset-2 hover:underline">
					{item.actionLabel} →
				</Link>
			) : null}
		</li>
	);
}

export function OverviewPage() {
	const todayQuery = useQuery({ queryKey: ["today"], queryFn: () => fetchTodayFixtures() });
	const betsQuery = useQuery({ queryKey: ["bets"], queryFn: () => fetchBets() });
	const bankrollQuery = useQuery({ queryKey: ["bankroll"], queryFn: () => fetchBankroll() });
	const drawQuery = useQuery({ queryKey: ["draw-results"], queryFn: () => fetchDrawResults() });
	const progressQuery = useQuery({ queryKey: ["validation-progress"], queryFn: () => fetchValidationProgress() });
	const now = Date.now();

	const fixtures = todayQuery.data ?? [];
	const bets = betsQuery.data ?? [];
	const results = drawQuery.data ?? [];

	const coreQueries = [todayQuery, betsQuery, bankrollQuery];
	const corePending = coreQueries.some((query) => query.isPending);
	const coreFailed = coreQueries.every((query) => query.isError);

	function retryAll() {
		void todayQuery.refetch();
		void betsQuery.refetch();
		void bankrollQuery.refetch();
		void drawQuery.refetch();
		void progressQuery.refetch();
	}

	// ---- 待办四规则（票 05 预裁决，按紧迫排序） ----
	const resultIds = new Set(results.map((row) => row.fixture_id));
	const todos: TodoItem[] = [];

	// ① 未锁定建议：最早开赛 <2h 琥珀紧迫倒计时 → /today
	const suggestions = bets.filter((bet) => !bet.purchased);
	const kickoffById = new Map(fixtures.map((fixture) => [fixture.fixture_id, fixture.kickoff_utc]));
	let earliest: number | null = null;
	for (const bet of suggestions) {
		for (const leg of bet.legs) {
			const iso = kickoffById.get(leg.fixture_id);
			if (!iso) {
				continue;
			}
			const at = new Date(iso).getTime();
			if (!Number.isNaN(at) && (earliest === null || at < earliest)) {
				earliest = at;
			}
		}
	}
	if (suggestions.length > 0) {
		const minutes = earliest === null ? null : Math.round((earliest - now) / 60_000);
		todos.push({
			key: "unlocked",
			testid: "todo-unlocked",
			text: `未锁定建议 ${suggestions.length} 条`,
			urgent:
				minutes === null
					? undefined
					: minutes <= 0
						? { text: "最早场次已开赛", tone: true }
						: minutes < 120
							? { text: `最早开赛 ${minutes} 分钟后`, tone: true }
							: { text: `最早开赛 ${Math.floor(minutes / 60)} 小时后`, tone: false },
			to: "/today",
			actionLabel: "去今日",
		});
	}

	// ② 待录赛果：已过开赛 + 挂着未结注 + 无开奖的今日场次 → /bets
	const openBetFixtureIds = new Set(
		bets.filter((bet) => bet.status === "open").flatMap((bet) => bet.legs.map((leg) => leg.fixture_id)),
	);
	const pendingResultCount = fixtures.filter((fixture) => {
		const kickoff = new Date(fixture.kickoff_utc).getTime();
		return (
			!Number.isNaN(kickoff) &&
			kickoff <= now &&
			openBetFixtureIds.has(fixture.fixture_id) &&
			!resultIds.has(fixture.fixture_id)
		);
	}).length;
	if (pendingResultCount > 0) {
		todos.push({
			key: "pending-results",
			testid: "todo-pending-results",
			text: `待录赛果 ${pendingResultCount} 场（已开赛、挂未结注、无开奖）`,
			to: "/bets",
			actionLabel: "去投注页录入",
		});
	}

	// ③ 可结算：全部腿赛果已齐的未结注（与结算批跑同口径）→ /bets
	const settleableCount = bets.filter(
		(bet) => bet.status === "open" && bet.legs.length > 0 && bet.legs.every((leg) => resultIds.has(leg.fixture_id)),
	).length;
	if (settleableCount > 0) {
		todos.push({
			key: "settleable",
			testid: "todo-settleable",
			text: `可结算 ${settleableCount} 注`,
			to: "/bets",
			actionLabel: "批跑可落定，去投注页",
		});
	}

	// ④ 数据异常：10 点后当日竞彩/欧赔整份快照缺（eu 用 joined 覆盖近似，票 14 同口径）→ 就地指引。
	// 仅在今日数据加载成功时判定——请求失败 ≠ 快照缺（失败走 notes 降级，不做真空异常）。
	const afterTen = new Date(now).getHours() >= 10;
	const jcMissing = fixtures.every((fixture) => !fixture.jc_updated_at);
	const euMissing = fixtures.every((fixture) => !fixture.joined);
	if (todayQuery.isSuccess && afterTen && (jcMissing || euMissing)) {
		const missing =
			fixtures.length === 0
				? "今日无场次数据——竞彩快照可能未拉取"
				: [jcMissing ? "当日竞彩快照缺" : null, euMissing ? `欧赔快照缺（0/${fixtures.length} 接入）` : null]
						.filter((part) => part !== null)
						.join("、");
		todos.push({
			key: "data-anomaly",
			testid: "todo-data-anomaly",
			text: `数据异常：${missing}`,
			hint: (
				<>
					跑 <code>task ingest-jingcai</code> 拉取当日竞彩快照；欧赔 join 随 ingest 补齐。
				</>
			),
		});
	}

	// 局部失败的诚实降级：核心三路通了但开奖/今日挂了，受影响规则不硬判
	const notes: string[] = [];
	if (!coreFailed) {
		if (drawQuery.isError) {
			notes.push("开奖结果加载失败——待录赛果与可结算判定暂缺。");
		}
		if (todayQuery.isError) {
			notes.push("今日场次加载失败——紧迫倒计时与数据异常判定暂缺。");
		}
	}

	// ---- 快照四卡 ----
	const balance = bankrollQuery.data ? bankrollQuery.data.balance : null;
	const todayLivePnlEvents = (bankrollQuery.data?.events ?? []).filter(
		(event) => (event.kind === "bet_stake" || event.kind === "bet_payout") && isSameLocalDay(event.occurred_at, now),
	);
	const todayLivePnl =
		todayLivePnlEvents.length > 0 ? todayLivePnlEvents.reduce((acc, event) => acc + event.amount_cny, 0) : null;

	const liveSettled = bets.filter((bet) => bet.mode === "live" && bet.status !== "open" && bet.profit !== null);
	const liveProfit = liveSettled.reduce((acc, bet) => acc + (bet.profit ?? 0), 0);
	const liveStake = liveSettled.reduce((acc, bet) => acc + (bet.actual_stake ?? bet.stake), 0);
	const liveRoi = liveStake > 0 ? liveProfit / liveStake : null;
	const liveOpenCount = bets.filter((bet) => bet.mode === "live" && bet.status === "open").length;

	const paperBets = bets.filter((bet) => bet.mode === "paper");
	const paperSettledToday = paperBets.filter(
		(bet) => bet.profit !== null && bet.settled_at !== null && isSameLocalDay(bet.settled_at, now),
	);
	const todayPaperPnl =
		paperSettledToday.length > 0 ? paperSettledToday.reduce((acc, bet) => acc + (bet.profit ?? 0), 0) : null;

	const pickableCount = fixtures.filter((fixture) => isPickable(fixture, now)).length;

	// ---- 验证进度细线（x/3：三条件，整赛季条件独立显示不计入） ----
	const coreConditions = (progressQuery.data?.conditions ?? []).filter((condition) => condition.key !== "full_season");
	const achievedCount = coreConditions.filter((condition) => condition.achieved).length;

	return (
		<AppShell title="总览">
			<div className="pb-4">
				<header className="mb-6">
					<p className="text-sm text-muted-foreground">
						总览是分诊台：先看待办与资金口径，再下钻到对应页面操作——本页不做任何写操作。
					</p>
				</header>

				{coreFailed ? (
					<EmptyState
						variant="backend-unavailable"
						message="总览数据加载失败。"
						hint={
							<>
								用 <code>task server</code> 启动 API；首次使用先跑 <code>task ingest-jingcai</code>。
							</>
						}
						action={{ label: "重试", onClick: retryAll }}
					/>
				) : null}

				{corePending ? (
					<div className="space-y-4" data-testid="overview-loading">
						<span className="sr-only">加载总览…</span>
						<div className="h-16 animate-pulse rounded-lg bg-muted" />
						<div className="h-40 animate-pulse rounded-lg bg-muted" />
					</div>
				) : null}

				{!corePending && !coreFailed ? (
					<>
						<section aria-labelledby="overview-todos-heading" className="mb-8" data-testid="overview-todos">
							<h2 id="overview-todos-heading" className="mb-3 text-sm font-medium">
								今日待办
							</h2>
							<div className="rounded-lg border border-border bg-card p-4">
								{todos.length === 0 ? (
									<p className="text-sm text-muted-foreground" data-testid="overview-no-todos">
										今日无事。
									</p>
								) : (
									<ul className="space-y-2.5">
										{todos.map((item) => (
											<TodoRow key={item.key} item={item} />
										))}
									</ul>
								)}
								{notes.map((note) => (
									<p key={note} className="mt-2 text-xs text-muted-foreground">
										{note}
									</p>
								))}
							</div>
						</section>

						<section aria-labelledby="overview-snapshot-heading" className="mb-8" data-testid="overview-snapshot">
							<h2 id="overview-snapshot-heading" className="mb-3 text-sm font-medium">
								资金与收益快照
							</h2>
							<div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
								{/* 真金区（票 05 红线：纸面不与真金混列，另起独立虚线区） */}
								<div className="rounded-lg border border-border p-4 md:col-span-2" data-testid="overview-live-zone">
									<p className="mb-3 flex items-center gap-2 text-xs text-muted-foreground">
										{/* 徽章文字用前景色：profit/warning 实色字压 10% 底纹 <AA（票 14 flag 同教训） */}
										<Badge className="bg-profit/10 text-foreground">真金</Badge>真金流水专区 · 仅真金口径
									</p>
									{bankrollQuery.isError ? (
										<p className="text-sm text-muted-foreground">真金数据加载失败——重试后恢复。</p>
									) : balance === null ? (
										<EmptyState
											variant="not-available"
											message="尚未入金——真金余额与真金盈亏将在这里出现。"
											hint="页内入金记账随资金页（票 20）上线。"
											action={{ label: "去资金页", to: "/bankroll" }}
										/>
									) : (
										<div className="grid gap-6 sm:grid-cols-2">
											<div data-testid="card-balance">
												<p className="text-xs text-muted-foreground">真金余额</p>
												<p className={`mt-1 text-2xl font-semibold ${TABULAR_NUMS}`}>¥{balance.toFixed(2)}</p>
												<p className="mt-1 text-xs text-muted-foreground">
													今日真金盈亏{" "}
													<span
														data-testid="live-pnl-today"
														className={`${TABULAR_NUMS} font-medium ${pnlClass(todayLivePnl)}`}
													>
														{todayLivePnl === null ? "—" : signedCny(todayLivePnl)}
													</span>
													{todayLivePnl === null ? "（今日无真金投注流水）" : ""}
												</p>
												<p className="mt-2 text-xs text-muted-foreground">
													判读：红 = 盈 · 绿 = 亏（正负号为准）；纸面不计入。
												</p>
											</div>
											<div data-testid="card-roi">
												<p className="text-xs text-muted-foreground">真金累计 ROI</p>
												<p className={`mt-1 text-2xl font-semibold ${TABULAR_NUMS} ${pnlClass(liveRoi)}`}>
													{liveRoi === null ? "—" : signedPct(liveRoi)}
												</p>
												<p className="mt-1 text-xs text-muted-foreground">
													已结算 {liveSettled.length} 注 · 未结 {liveOpenCount} 注不计。
												</p>
												<p className="mt-2 text-xs text-muted-foreground">判读：≥0 = 长期为正；红 = 正 · 绿 = 负。</p>
											</div>
										</div>
									)}
								</div>

								<Link
									to="/today"
									className="rounded-lg border border-border p-4 transition-colors hover:bg-muted"
									data-testid="card-pickable"
								>
									<p className="text-xs text-muted-foreground">今日可投场次</p>
									<p className={`mt-1 text-2xl font-semibold ${TABULAR_NUMS}`}>
										{todayQuery.isError ? "—" : pickableCount}
									</p>
									<p className="mt-1 text-xs text-muted-foreground">
										可投 = 在售且证据完整新鲜；多 = 选择余地，不等于该投。
									</p>
									<p className="mt-2 text-xs text-primary">去今日看盘 →</p>
								</Link>

								{/* 纸面区（票 05 红线：带"纸面"徽章，与真金不同区不并排） */}
								<div className="rounded-lg border border-dashed border-border p-4" data-testid="overview-paper-zone">
									<p className="mb-3 flex items-center gap-2 text-xs text-muted-foreground">
										<Badge className="bg-warning/10 text-foreground">纸面</Badge>模拟盘 · 不进真金资金池
									</p>
									{betsQuery.isError ? (
										<p className="text-sm text-muted-foreground">纸面数据加载失败——重试后恢复。</p>
									) : paperBets.length === 0 ? (
										<p className="text-sm text-muted-foreground" data-testid="paper-guide">
											还没有纸面记录——
											<Link to="/today" className="text-primary underline-offset-2 hover:underline">
												从今日页建第一笔建议
											</Link>
											，模拟收益会在这里累积。
										</p>
									) : (
										<div data-testid="card-paper-pnl">
											<p className="text-xs text-muted-foreground">今日纸面盈亏</p>
											<p className={`mt-1 text-2xl font-semibold ${TABULAR_NUMS} ${pnlClass(todayPaperPnl)}`}>
												{todayPaperPnl === null ? "—" : signedCny(todayPaperPnl)}
											</p>
											<p className="mt-1 text-xs text-muted-foreground">
												{todayPaperPnl === null
													? "今日无已结算纸面注。"
													: `今日已结算 ${paperSettledToday.length} 注。`}
											</p>
											<p className="mt-2 text-xs text-muted-foreground">
												判读：红 = 盈 · 绿 = 亏；模拟口径，与真金分开看。
											</p>
										</div>
									)}
								</div>
							</div>
						</section>

						{progressQuery.data ? (
							<Link
								to="/validation"
								className="block rounded-lg px-1 py-2 transition-colors hover:bg-muted"
								data-testid="overview-validation"
							>
								<span className="flex items-baseline justify-between text-xs text-muted-foreground">
									<span>纸面转真金验证进度（三条件）</span>
									<span className={TABULAR_NUMS}>
										{achievedCount}/{coreConditions.length}
									</span>
								</span>
								<span className="mt-1.5 block h-0.5 w-full rounded-full bg-muted">
									<span
										className="block h-0.5 rounded-full bg-info"
										style={{
											width: coreConditions.length === 0 ? "0%" : `${(achievedCount / coreConditions.length) * 100}%`,
										}}
									/>
								</span>
							</Link>
						) : null}
					</>
				) : null}
			</div>
		</AppShell>
	);
}
