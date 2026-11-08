import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { type ReactNode, useState } from "react";
import {
	type AttributionBucket,
	fetchBankroll,
	fetchBets,
	fetchBetsSummary,
	fetchDrawResults,
	fetchNoCandidateAttribution,
	fetchTodayFixtures,
	fetchValidationProgress,
} from "../api/goalx";
import { AppShell } from "../components/app-shell";
import { EmptyState } from "../components/empty-state";
import { GlossaryTerm } from "../components/glossary-term";
import { isPickable, localTime } from "../components/had-quote-ui";
import { Badge } from "../components/ui/badge";
import {
	attributionKey,
	bankrollKey,
	betsSummaryKey,
	drawResultsKey,
	openBetsKey,
	todayFixturesKey,
	validationProgressKey,
} from "../lib/query-keys";
import {
	beijingBusinessDate,
	earliestKickoffMs,
	openBetFixtureIds,
	pnlClass,
	signedCnyAlways,
	TABULAR_NUMS,
} from "../lib/ui";

import { useNow } from "../lib/use-now";

/**
 * 票 15：总览 Dashboard（票 05 定稿 = 唯一事实源）。总览 = 分诊（票 03）：
 * 待办清单卡（四规则）+ 首屏快照四卡（真金/纸面分区隔离）+ 页底验证进度细线。
 * 本页不做任何写操作；每张卡附判读方向，不裸放数字；红涨绿跌配正负号（票 02）。
 * 数据源 = 现有端点前端拼装（票 10 定稿，零 contract 变更），react-query 缓存键
 * 走 lib/query-keys 注册表与今日/投注/资金/验证各页同键——同端点单份缓存条目
 * （票 07；staleTime 全站默认 0，每次挂载重拍，赔率新鲜优先）。
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
	to?: "/fixtures" | "/bets";
	actionLabel?: string;
};

function signedPct(value: number): string {
	return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(1)}%`;
}

/** 北京业务日同日判定（今日盈亏分桶口径）——浏览器本地零点在 UTC 机器上会错 8h。 */
function isSameBeijingDay(iso: string, now: number): boolean {
	const at = new Date(iso).getTime();
	return !Number.isNaN(at) && beijingBusinessDate(at) === beijingBusinessDate(now);
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

/**
 * 票 07 六桶归因桶卡：计数 + 点击下钻展开桶内场次（样本含裁决原因）。
 * label/判定来自后端单一映射（与 quote_evidence 裁决原因一一对应）。
 */
function BucketCard({
	bucket,
	footnote = false,
	open,
	onToggle,
}: {
	bucket: AttributionBucket;
	footnote?: boolean;
	open: boolean;
	onToggle: () => void;
}) {
	const hasSamples = bucket.count > 0;
	const samples = bucket.samples ?? [];
	const reasonsSeen = bucket.reasons_seen ?? [];
	return (
		<div
			className={`rounded-lg border p-3 ${footnote ? "border-dashed" : "border-border"} ${hasSamples ? "" : "opacity-70"}`}
			data-testid={`attribution-bucket-${bucket.key}`}
		>
			<button
				type="button"
				className="flex w-full items-baseline justify-between gap-2 text-left disabled:cursor-default"
				disabled={!hasSamples}
				onClick={onToggle}
				aria-expanded={open}
				data-testid={`attribution-bucket-toggle-${bucket.key}`}
			>
				<span className="text-xs text-muted-foreground">
					{bucket.label}
					{footnote ? "（脚注，不入六桶）" : ""}
				</span>
				<span className={`${TABULAR_NUMS} text-xl font-semibold`}>{bucket.count}</span>
			</button>
			{bucket.count > 0 && reasonsSeen.length > 0 ? (
				<p className="mt-1 text-xs text-muted-foreground">裁决原因：{reasonsSeen.join("、")}</p>
			) : null}
			{open && hasSamples ? (
				<>
					<ul className="mt-2 space-y-1" data-testid={`attribution-bucket-samples-${bucket.key}`}>
						{samples.map((sample) => (
							<li key={sample.fixture_id} className="border-t border-border pt-1 text-xs">
								<span className="text-muted-foreground">{sample.match_code}</span> {sample.home_team} vs{" "}
								{sample.away_team}
								<span className="ml-1 text-muted-foreground">· 开球 {localTime(sample.kickoff_utc)}</span>
								{(sample.reasons ?? []).length > 0 ? (
									<span className="ml-1 text-muted-foreground">（{(sample.reasons ?? []).join("、")}）</span>
								) : null}
							</li>
						))}
					</ul>
					{bucket.count > samples.length ? (
						<p className="mt-1 text-xs text-muted-foreground" data-testid={`attribution-bucket-more-${bucket.key}`}>
							还有 {bucket.count - samples.length} 场未展示（样本封顶，非全量清单）
						</p>
					) : null}
				</>
			) : null}
		</div>
	);
}

export function OverviewPage() {
	const todayQuery = useQuery({ queryKey: todayFixturesKey(1), queryFn: () => fetchTodayFixtures(undefined, 1) });
	// 票 10：待办规则走 only_open 面（待办/结算判定集合，服务端不有界）；
	// 经济面（真金 ROI/未结数、今日纸面盈亏）走 /bets/summary 全量聚合
	const betsQuery = useQuery({ queryKey: openBetsKey, queryFn: () => fetchBets({ only_open: true }) });
	const summaryQuery = useQuery({ queryKey: betsSummaryKey, queryFn: fetchBetsSummary });
	const bankrollQuery = useQuery({ queryKey: bankrollKey, queryFn: () => fetchBankroll() });
	const drawQuery = useQuery({ queryKey: drawResultsKey, queryFn: () => fetchDrawResults() });
	const progressQuery = useQuery({ queryKey: validationProgressKey, queryFn: () => fetchValidationProgress() });
	// 票 07：无候选六桶归因（独立查询——缺席不拖垮核心三路，失败诚实降级）
	const attributionQuery = useQuery({
		queryKey: attributionKey(1),
		queryFn: () => fetchNoCandidateAttribution(undefined, 1),
	});
	const [openBucket, setOpenBucket] = useState<string | null>(null);
	const now = useNow(); // 共享滴答时钟：倒计时/年龄不冻结在一次性渲染

	const fixtures = todayQuery.data ?? [];
	const bets = betsQuery.data ?? [];
	const results = drawQuery.data ?? [];

	const coreQueries = [todayQuery, betsQuery, summaryQuery, bankrollQuery];
	const corePending = coreQueries.some((query) => query.isPending);
	const coreFailed = coreQueries.every((query) => query.isError);

	function retryAll() {
		void todayQuery.refetch();
		void betsQuery.refetch();
		void summaryQuery.refetch();
		void bankrollQuery.refetch();
		void drawQuery.refetch();
		void progressQuery.refetch();
	}

	// ---- 待办四规则（票 05 预裁决，按紧迫排序） ----
	const resultIds = new Set(results.map((row) => row.fixture_id));
	const todos: TodoItem[] = [];

	// ① 未锁定建议：最早开赛 <2h 琥珀紧迫倒计时 → /fixtures（票 wb-01 起路径）。
	// 票 10：口径=还挂着的建议（open 且未锁）——结算批扫过的死建议不再计数
	const suggestions = bets.filter((bet) => !bet.purchased && bet.status === "open");
	const kickoffById = new Map(fixtures.map((fixture) => [fixture.fixture_id, fixture.kickoff_utc]));
	const earliest = earliestKickoffMs(
		suggestions.flatMap((bet) => bet.legs),
		kickoffById,
	);
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
			to: "/fixtures",
			actionLabel: "去场次",
		});
	}

	// ② 待录赛果：已过开赛 + 挂着未结注 + 无开奖的今日场次 → /bets
	const openBetIds = openBetFixtureIds(bets);
	const pendingResultCount = fixtures.filter((fixture) => {
		const kickoff = new Date(fixture.kickoff_utc).getTime();
		return (
			!Number.isNaN(kickoff) &&
			kickoff <= now &&
			openBetIds.has(fixture.fixture_id) &&
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
	// 10 点门取北京时（后端 schedules.py 拍班同口径），浏览器本地时区不可信
	const afterTen = new Date(now + 8 * 3_600_000).getUTCHours() >= 10;
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

	// ---- 快照四卡（票 10：真金/纸面经济面走 /bets/summary 全量聚合） ----
	const balance = bankrollQuery.data ? bankrollQuery.data.balance : null;
	const todayLivePnlEvents = (bankrollQuery.data?.events ?? []).filter(
		(event) => (event.kind === "bet_stake" || event.kind === "bet_payout") && isSameBeijingDay(event.occurred_at, now),
	);
	const todayLivePnl =
		todayLivePnlEvents.length > 0 ? todayLivePnlEvents.reduce((acc, event) => acc + event.amount_cny, 0) : null;

	// 真金累计 = 已回录（purchased）且已结算的全量账；真金未锁建议不进资金面
	const liveFace = summaryQuery.data?.live;
	const liveRoi = liveFace && liveFace.stake_settled > 0 ? liveFace.total_pnl / liveFace.stake_settled : null;
	const liveSettledCount = liveFace?.settled_n ?? 0;
	const liveOpenCount = liveFace?.open_n ?? 0;

	const paperFace = summaryQuery.data?.paper;
	const paperTodayBucket = paperFace?.days.find((bucket) => bucket.date === beijingBusinessDate(now)) ?? null;
	const todayPaperPnl = paperTodayBucket && paperTodayBucket.settled_n > 0 ? paperTodayBucket.pnl : null;
	const paperSettledTodayCount = paperTodayBucket?.settled_n ?? 0;

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

						<section aria-labelledby="overview-attribution-heading" className="mb-8" data-testid="overview-attribution">
							<h2 id="overview-attribution-heading" className="mb-1 text-sm font-medium">
								今日为何没有候选 <span className="font-normal text-muted-foreground">六桶归因</span>
							</h2>
							<p className="mb-3 text-xs text-muted-foreground">
								每场只进一桶（首匹配）；桶判定与报价证据裁决原因一一对应——点击计数下钻场次。
								{attributionQuery.data ? (
									<>
										今日板覆盖 {attributionQuery.data.board_coverage[1]}/{attributionQuery.data.board_coverage[0]}{" "}
										已链（与晨检同源）。
									</>
								) : null}
							</p>
							{attributionQuery.data?.corpus_reader === "degraded" ? (
								<p
									className="mb-3 rounded bg-warning/10 px-2 py-1 text-xs text-foreground"
									data-testid="attribution-reader-degraded"
								>
									欧赔读面本请求缺席（被物化锁占或库缺席）——"国际参考断供"桶可能混入读面缺席
									而非真断供，对账以晨检为准。
								</p>
							) : null}
							{attributionQuery.data && attributionQuery.data.board_coverage[0] === 0 ? (
								<p
									className="mb-3 rounded bg-warning/10 px-2 py-1 text-xs text-foreground"
									data-testid="attribution-not-measured"
								>
									今日板覆盖 0 场——桶未统计（≠ 观察为零）：窗口内无可归因场次，计数零是缺分母非判读。
								</p>
							) : null}
							{attributionQuery.isError ? (
								<p
									className="rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground"
									data-testid="attribution-unavailable"
								>
									归因面暂不可用（端点较新或后端不可达）——六桶读数暂缺，不硬判。
								</p>
							) : attributionQuery.data ? (
								<>
									<div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
										{attributionQuery.data.buckets.map((bucket) => (
											<BucketCard
												key={bucket.key}
												bucket={bucket}
												open={openBucket === bucket.key}
												onToggle={() => setOpenBucket(openBucket === bucket.key ? null : bucket.key)}
											/>
										))}
									</div>
									{attributionQuery.data.freshness_shadow ? (
										<p className="mt-2 text-xs text-muted-foreground" data-testid="attribution-freshness-shadow">
											300s 双口径读数（票 08 并行测量，快照日 {attributionQuery.data.freshness_shadow_date ?? "—"}
											）：规则新鲜 {attributionQuery.data.freshness_shadow["rule_fresh"]}/
											{attributionQuery.data.freshness_shadow["n_comparable"]} · 观测新鲜{" "}
											{attributionQuery.data.freshness_shadow["observed_fresh"]}/
											{attributionQuery.data.freshness_shadow["n_comparable"]} · 分歧（规则旧 /观测新）
											{attributionQuery.data.freshness_shadow["divergent_rule_stale_observed_fresh"]}
											——仅记录不改规则，读数与票 09 检查点对齐（11-13 首读）。
										</p>
									) : null}
									<div className="mt-2 grid gap-2 sm:grid-cols-2">
										{/* 候选对照面：有候选时"没有候选"前提不成立——如实亮出 */}
										<BucketCard
											bucket={attributionQuery.data.positive_candidates}
											open={openBucket === "positive_candidates"}
											onToggle={() =>
												setOpenBucket(openBucket === "positive_candidates" ? null : "positive_candidates")
											}
										/>
										<BucketCard
											bucket={attributionQuery.data.window_closed}
											footnote
											open={openBucket === "window_closed"}
											onToggle={() => setOpenBucket(openBucket === "window_closed" ? null : "window_closed")}
										/>
									</div>
								</>
							) : (
								<div data-testid="attribution-loading" className="h-24 animate-pulse rounded-lg bg-muted" />
							)}
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
											hint="到资金页记录第一笔入金即可开始真金口径。"
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
														{todayLivePnl === null ? "—" : signedCnyAlways(todayLivePnl)}
													</span>
													{todayLivePnl === null ? "（今日无真金投注流水）" : ""}
												</p>
												<p className="mt-2 text-xs text-muted-foreground">
													判读：红 = 盈 · 绿 = 亏（正负号为准）；纸面不计入。
												</p>
											</div>
											<div data-testid="card-roi">
												<p className="text-xs text-muted-foreground">
													{/* 票 18：ROI 指标名接词典 tooltip（盈亏与 ROI 口径） */}
													真金累计 <GlossaryTerm id="pnl-roi">ROI</GlossaryTerm>
												</p>
												<p className={`mt-1 text-2xl font-semibold ${TABULAR_NUMS} ${pnlClass(liveRoi)}`}>
													{liveRoi === null ? "—" : signedPct(liveRoi)}
												</p>
												<p className="mt-1 text-xs text-muted-foreground">
													已结算 {liveSettledCount} 注 · 未结 {liveOpenCount} 注不计。
												</p>
												<p className="mt-2 text-xs text-muted-foreground">判读：≥0 = 长期为正；红 = 正 · 绿 = 负。</p>
											</div>
										</div>
									)}
								</div>

								<Link
									to="/fixtures"
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
									<p className="mt-2 text-xs text-primary">去场次看盘 →</p>
								</Link>

								{/* 纸面区（票 05 红线：带"纸面"徽章，与真金不同区不并排） */}
								<div className="rounded-lg border border-dashed border-border p-4" data-testid="overview-paper-zone">
									<p className="mb-3 flex items-center gap-2 text-xs text-muted-foreground">
										<Badge className="bg-warning/10 text-foreground">纸面</Badge>模拟盘 · 不进真金资金池
									</p>
									{summaryQuery.isError ? (
										<p className="text-sm text-muted-foreground">纸面数据加载失败——重试后恢复。</p>
									) : (paperFace?.bets_n ?? 0) === 0 ? (
										<p className="text-sm text-muted-foreground" data-testid="paper-guide">
											还没有纸面记录——
											<Link to="/fixtures" className="text-primary underline-offset-2 hover:underline">
												从场次页建第一笔建议
											</Link>
											，模拟收益会在这里累积。
										</p>
									) : (
										<div data-testid="card-paper-pnl">
											<p className="text-xs text-muted-foreground">今日纸面盈亏</p>
											<p className={`mt-1 text-2xl font-semibold ${TABULAR_NUMS} ${pnlClass(todayPaperPnl)}`}>
												{todayPaperPnl === null ? "—" : signedCnyAlways(todayPaperPnl)}
											</p>
											<p className="mt-1 text-xs text-muted-foreground">
												{todayPaperPnl === null ? "今日无已结算纸面注。" : `今日已结算 ${paperSettledTodayCount} 注。`}
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
