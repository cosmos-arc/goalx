import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import {
	type BetsDayBucket,
	type BetsModeSummary,
	fetchBets,
	fetchBetsSummary,
	fetchTodayFixtures,
} from "../api/goalx";
import { AppShell } from "../components/app-shell";
import type { EChartsOption } from "../components/charts/echarts";
import { useECharts } from "../components/charts/use-echarts";
import { EmptyState } from "../components/empty-state";
import { GlossaryTerm } from "../components/glossary-term";
import { StatusBadge } from "../components/status-badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { betsKey, betsSummaryKey, todayFixturesKey } from "../lib/query-keys";
import {
	beijingBusinessDate,
	cssVar,
	legText,
	pnlClass,
	shortTime,
	signedCny,
	stakeText,
	TABULAR_NUMS,
} from "../lib/ui";
import { useNow } from "../lib/use-now";

/**
 * 票 17：历史复盘页落地（票 07 定稿 = 唯一事实源）。分析优先：五指标卡 + 盈亏累计
 * 曲线 + "查看明细"同页展开，流水是下钻层不是主界面。
 * iterate 票 10：五指标与累计曲线改 `GET /bets/summary` 服务端全量聚合（永不随
 * 列表有界 100 截断）；`GET /bets` 有界列表只供明细表与二级筛选。样本口径 =
 * 已锁定（purchased）且已结算（settled_at 非空）；纸面/真金大标签常显、分别
 * 统计不混算。
 *
 * 口径红线：
 * - 页头固定一行按 07 Answer 逐字："统计已锁定且已结算的注；前瞻验证口径（含排除
 *   规则）见验证页。"两页数字不同时各自以口径标注为准；术语"前瞻纳入"由 17 号的
 *   链接占位升级为 18 号的 GlossaryTerm Popover（悬停/聚焦就地看口径）。
 * - 指标卡口径=模式+时间范围（服务端全量）；二层筛选（Competition/策略版本/
 *   结果状态）只作用于明细表——服务端无注级 competition 维度，且聚合面不得
 *   被任何有界列表静默截断（票 10 不变量）。
 * - 平均 EV（票 41）：建注时随 BetView 落注级快照（共识/模型双口径概率与
 *   EV），均值共识口径优先、两口径不混算，样本数（有快照注 / 筛选注数）注明，
 *   无快照注保持"—"兜底；平均 CLV 仍缺注级 API 字段（clv_records 已有链路，
 *   BetView 增量待后续票），按缺失显示"—"并注明口径与判读方向（CLV 正 = 买在
 *   好价）；不造假数据，聚合口径见验证页。
 * - Competition 归因取自今日列表的 competition 字段（v1 无历史 fixture 详情端点），
 *   不在今日列表的场次无法归因、仅"全部"下可见；筛选选项随数据推导，不写死。
 * - 聚合行 → 注明细的下钻本页闭环；明细 → Fixture 的最后一跳留待场次详情页
 *   （map "Not yet specified"，依赖后端赔率历史数据可用性）。
 */

type Mode = "paper" | "live";

const MODES: Array<{ key: Mode; label: string }> = [
	{ key: "paper", label: "纸面" },
	{ key: "live", label: "真金" },
];

const RANGES = [
	{ key: "7d", label: "近 7 天", days: 7 },
	{ key: "30d", label: "近 30 天", days: 30 },
	{ key: "90d", label: "近 90 天", days: 90 },
	{ key: "all", label: "全部", days: null },
] as const;

type RangeKey = (typeof RANGES)[number]["key"];

const STATUS_OPTIONS = [
	{ value: "won", label: "胜" },
	{ value: "lost", label: "负" },
	{ value: "partial", label: "部分" },
	{ value: "void", label: "退款" },
] as const;

/** EV 口径标签：均值展示与样本数注明共用（共识优先，模型为回退口径）。 */
export type EvCaliber = "consensus" | "model";

export const EV_CALIBER_LABELS: Record<EvCaliber, string> = {
	consensus: "共识口径",
	model: "模型口径",
};

/** summary 日桶按时间范围求和后的指标面（票 10）。 */
export type RangeMetrics = {
	settledN: number;
	wonN: number;
	decidedN: number;
	pnl: number;
	ev: { value: number; caliber: EvCaliber; count: number } | null;
};

/**
 * 模式聚合面 × 时间范围 → 指标：日桶按北京业务日键（日粒度，≥startDay
 * 整日纳入——correctness：指标与明细表共用同一日粒度边界，不混 ms 精度）
 * 过滤求和；EV 共识口径优先、两口径不混算（票 41 口径），无快照注不入分母。
 */
export function summarizeRange(face: BetsModeSummary, startDay: string | null): RangeMetrics {
	const days = startDay === null ? face.days : face.days.filter((day) => day.date >= startDay);
	const sum = (pick: (day: BetsDayBucket) => number): number => days.reduce((acc, day) => acc + pick(day), 0);
	const nConsensus = sum((day) => day.n_ev_consensus);
	const nModel = sum((day) => day.n_ev_model);
	const ev =
		nConsensus > 0
			? { value: sum((day) => day.sum_ev_consensus) / nConsensus, caliber: "consensus" as const, count: nConsensus }
			: nModel > 0
				? { value: sum((day) => day.sum_ev_model) / nModel, caliber: "model" as const, count: nModel }
				: null;
	return {
		settledN: sum((day) => day.settled_n),
		wonN: sum((day) => day.won_n),
		decidedN: sum((day) => day.decided_n),
		pnl: Math.round(sum((day) => day.pnl) * 100) / 100,
		ev,
	};
}

/** 日桶 → 累计曲线点（升序累计，label=月-日；与旧逐注曲线同判读）。 */
export function buildDailyCumulativePoints(
	days: readonly BetsDayBucket[],
	startDay: string | null,
): Array<{ label: string; value: number }> {
	const scoped = startDay === null ? days : days.filter((day) => day.date >= startDay);
	let cumulative = 0;
	return scoped.map((day) => {
		cumulative += day.pnl;
		return { label: day.date.slice(5), value: Math.round(cumulative * 100) / 100 };
	});
}

/** 数据 → option 的纯映射（use-echarts 测试约定）：折线 + 0 基准虚线 markline。 */
export function cumulativePnlOption(
	points: Array<{ label: string; value: number }>,
	lineColor: string,
	axisColor: string,
): EChartsOption {
	return {
		grid: { left: 8, right: 16, top: 20, bottom: 8, containLabel: true },
		tooltip: { trigger: "axis" },
		xAxis: {
			type: "category",
			data: points.map((point) => point.label),
			axisLine: { lineStyle: { color: axisColor } },
			axisTick: { show: false },
			axisLabel: { color: axisColor, fontSize: 11 },
		},
		yAxis: {
			type: "value",
			axisLabel: { color: axisColor, fontSize: 11 },
			splitLine: { lineStyle: { color: axisColor, opacity: 0.3 } },
		},
		series: [
			{
				type: "line",
				data: points.map((point) => point.value),
				symbol: "circle",
				symbolSize: 5,
				lineStyle: { color: lineColor, width: 2 },
				itemStyle: { color: lineColor },
				markLine: {
					silent: true,
					symbol: "none",
					label: { show: false },
					lineStyle: { type: "dashed", color: axisColor },
					data: [{ yAxis: 0 }],
				},
			},
		],
	};
}

/**
 * 累计曲线独立子组件：useECharts 的 init effect 只在挂载时跑一次（deps=[]），
 * 容器必须随组件一起挂载（数据到位、点数 ≥2 才挂），否则 init 拿不到容器。
 */
function CumulativeChart({ points }: { points: Array<{ label: string; value: number }> }) {
	// 票 05 E17：option 引用稳定——useECharts 依赖 [option]，内联构造会
	// 每渲染 setOption(notMerge) 重置交互态（30s tick 掐 tooltip）
	const fg = cssVar("--chart-1", "#2563eb");
	const muted = cssVar("--muted-foreground", "#6b7280");
	const option = useMemo(() => cumulativePnlOption(points, fg, muted), [points, fg, muted]);
	const ref = useECharts(option);
	return (
		<div ref={ref} data-testid="history-chart" role="img" aria-label="盈亏累计曲线（0 基准）" className="h-64 w-full" />
	);
}

export function HistoryPage() {
	const betsQuery = useQuery({ queryKey: betsKey, queryFn: () => fetchBets() });
	const summaryQuery = useQuery({ queryKey: betsSummaryKey, queryFn: fetchBetsSummary });
	const todayQuery = useQuery({ queryKey: todayFixturesKey(1), queryFn: () => fetchTodayFixtures(undefined, 1) });
	const [mode, setMode] = useState<Mode>("paper");
	const [rangeKey, setRangeKey] = useState<RangeKey>("all");
	const [competition, setCompetition] = useState("");
	const [strategy, setStrategy] = useState("");
	const [status, setStatus] = useState("");
	const [detailOpen, setDetailOpen] = useState(false);
	const now = useNow(); // 共享滴答时钟：倒计时/年龄不冻结在一次性渲染

	const allBets = betsQuery.data ?? [];
	// 明细/筛选面：有界列表内已锁定且已结算的注（票 07 定稿口径；票 10 起
	// 列表=最新 100 渲染事实，只供明细表与筛选选项，不供指标卡）
	const modeSettled = useMemo(
		() => allBets.filter((bet) => bet.mode === mode && bet.purchased && bet.settled_at !== null),
		[allBets, mode],
	);

	// Competition 归因：唯一来源是今日列表的 competition 字段（v1 无历史 fixture 端点）
	const competitionByFixture = useMemo(
		() => new Map((todayQuery.data ?? []).map((fixture) => [fixture.fixture_id, fixture.competition])),
		[todayQuery.data],
	);
	const competitionOptions = Array.from(
		new Set(
			modeSettled
				.flatMap((bet) => bet.legs.map((leg) => competitionByFixture.get(leg.fixture_id)))
				.filter((value): value is string => value !== undefined),
		),
	).sort();
	const strategyValues = Array.from(new Set(modeSettled.map((bet) => bet.strategy_version)));
	const hasUnversioned = strategyValues.includes(null);

	const range = RANGES.find((item) => item.key === rangeKey) ?? RANGES[3];
	// 指标卡/曲线口径=模式+时间范围的服务端全量日桶（票 10：永不随列表有界
	// 截断）。correctness F1：时间边界统一日粒度（北京业务日 >= startDay 整日
	// 纳入），明细表与指标卡共用同一口径——不再一个 ms 精度一个日粒度
	const startDay = range.days === null ? null : beijingBusinessDate(now - range.days * 86_400_000);
	const face = summaryQuery.data?.[mode];
	const metrics = useMemo(() => (face ? summarizeRange(face, startDay) : null), [face, startDay]);
	// 票 05 E17 correctness F2：points 身份稳定——30s useNow 滴答重渲染不得
	// 给 points 换新身份（否则图表 setOption 重置交互态）
	const points = useMemo(() => (face ? buildDailyCumulativePoints(face.days, startDay) : []), [face, startDay]);

	const filtered = useMemo(
		() =>
			modeSettled.filter((bet) => {
				const settledMs = new Date(bet.settled_at ?? "").getTime();
				if (startDay !== null && (Number.isNaN(settledMs) || beijingBusinessDate(settledMs) < startDay)) {
					return false;
				}
				if (competition !== "" && !bet.legs.some((leg) => competitionByFixture.get(leg.fixture_id) === competition)) {
					return false;
				}
				if (strategy !== "") {
					const version = bet.strategy_version;
					if (strategy === "none" ? version !== null : version !== strategy) {
						return false;
					}
				}
				if (status !== "" && bet.status !== status) {
					return false;
				}
				return true;
			}),
		[modeSettled, startDay, competition, competitionByFixture, strategy, status],
	);

	// 明细行按结算时点倒序（最近结算在前，与票 16 已结算段同序）
	const filteredDesc = [...filtered].sort(
		(a, b) => new Date(b.settled_at ?? "").getTime() - new Date(a.settled_at ?? "").getTime(),
	);

	// ---- 五指标（模式+时间范围全量口径，服务端聚合；07 定稿：各带判读方向） ----
	const totalPnl = metrics?.pnl ?? 0;
	const hasPnl = (metrics?.settledN ?? 0) > 0;
	const hitRate = metrics && metrics.decidedN > 0 ? metrics.wonN / metrics.decidedN : null;
	const ev = metrics?.ev ?? null;

	function clearFilters() {
		setRangeKey("all");
		setCompetition("");
		setStrategy("");
		setStatus("");
	}

	return (
		<AppShell title="历史">
			<div className="pb-4">
				<header className="mb-5">
					{/* 口径行（票 07 Answer 定稿文案，逐字）：与验证页各按口径标注，两页数字可对账 */}
					<p data-testid="history-caliber" className="text-sm text-muted-foreground">
						统计已锁定且已结算的注；前瞻验证口径（含排除规则）见
						<Link to="/validation" className="text-primary underline-offset-2 hover:underline">
							验证页
						</Link>
						。术语
						{/* 票 18：链接占位升级为词典 tooltip（悬停/聚焦看"前瞻纳入"口径） */}
						<GlossaryTerm id="forward-inclusion">"前瞻纳入"</GlossaryTerm>
						见词典。
					</p>
				</header>

				{/* 常驻筛选：模式（大标签常显，默认纸面，不混算）+ 时间范围——不依赖查询结果，降级态也可见 */}
				<div className="mb-4 flex flex-wrap items-center gap-x-6 gap-y-3">
					<fieldset className="flex gap-2">
						<legend className="sr-only">模式</legend>
						{MODES.map((item) => (
							<button
								key={item.key}
								type="button"
								data-testid={`mode-${item.key}`}
								aria-pressed={mode === item.key}
								className={`rounded-lg border px-4 py-1.5 text-base font-medium transition-colors ${
									mode === item.key
										? "border-primary bg-primary text-primary-foreground"
										: "border-border text-muted-foreground hover:bg-muted hover:text-foreground"
								}`}
								onClick={() => setMode(item.key)}
							>
								{item.label}
							</button>
						))}
					</fieldset>
					<fieldset className="flex flex-wrap gap-1">
						<legend className="sr-only">时间范围</legend>
						{RANGES.map((item) => (
							<button
								key={item.key}
								type="button"
								data-testid={`range-${item.key}`}
								aria-pressed={rangeKey === item.key}
								className={`rounded-md px-2.5 py-1 text-sm transition-colors ${
									rangeKey === item.key
										? "bg-primary text-primary-foreground"
										: "text-muted-foreground hover:bg-muted hover:text-foreground"
								}`}
								onClick={() => setRangeKey(item.key)}
							>
								{item.label}
							</button>
						))}
					</fieldset>
				</div>

				{summaryQuery.isPending || betsQuery.isPending ? (
					<div className="space-y-4" data-testid="history-loading">
						<span className="sr-only">加载历史数据…</span>
						<div className="h-10 animate-pulse rounded-lg bg-muted" />
						<div className="h-40 animate-pulse rounded-lg bg-muted" />
					</div>
				) : null}

				{summaryQuery.isError ? (
					<EmptyState
						variant="backend-unavailable"
						message="历史聚合数据加载失败。"
						hint={
							<>
								用 <code>task server</code> 启动 API。
							</>
						}
						action={{ label: "重试", onClick: () => void summaryQuery.refetch() }}
					/>
				) : null}

				{face && metrics ? (
					<>
						{/* 二层折叠筛选：选项随当前模式样本推导，不写死 */}
						<details data-testid="filters-more" className="mb-6 rounded-lg border border-border px-4 py-2.5">
							<summary className="cursor-pointer text-sm text-muted-foreground hover:text-foreground">
								更多筛选（Competition / 策略版本 / 结果状态）
							</summary>
							<div className="mt-3 flex flex-wrap items-end gap-4 pb-1 text-sm">
								<label className="flex flex-col gap-1">
									<span className="text-xs text-muted-foreground">Competition</span>
									<select
										data-testid="filter-competition"
										className="rounded-md border border-input bg-background px-2 py-1.5 text-sm"
										value={competition}
										onChange={(event) => setCompetition(event.target.value)}
									>
										<option value="">全部</option>
										{competitionOptions.map((value) => (
											<option key={value} value={value}>
												{value}
											</option>
										))}
									</select>
								</label>
								<label className="flex flex-col gap-1">
									<span className="text-xs text-muted-foreground">策略版本</span>
									<select
										data-testid="filter-strategy"
										className="rounded-md border border-input bg-background px-2 py-1.5 text-sm"
										value={strategy}
										onChange={(event) => setStrategy(event.target.value)}
									>
										<option value="">全部</option>
										{strategyValues
											.filter((value) => value !== null)
											.sort()
											.map((value) => (
												<option key={value} value={value}>
													{value}
												</option>
											))}
										{hasUnversioned ? <option value="none">未标注</option> : null}
									</select>
								</label>
								<label className="flex flex-col gap-1">
									<span className="text-xs text-muted-foreground">结果状态</span>
									<select
										data-testid="filter-status"
										className="rounded-md border border-input bg-background px-2 py-1.5 text-sm"
										value={status}
										onChange={(event) => setStatus(event.target.value)}
									>
										<option value="">全部</option>
										{STATUS_OPTIONS.map((item) => (
											<option key={item.value} value={item.value}>
												{item.label}
											</option>
										))}
									</select>
								</label>
							</div>
						</details>

						{face.settled_n === 0 ? (
							<EmptyState
								variant="no-data"
								message="该模式无已结算注"
								hint="切另一模式查看，或到投注页锁定建议后等结算。"
							/>
						) : metrics.settledN === 0 ? (
							/* correctness F3：时间范围空≠模式空——零值卡片不是空态，给出范围出口 */
							<EmptyState
								variant="no-data"
								message="该时间范围内无已结算注"
								hint="指标卡与曲线随时间范围联动；放宽范围或看全部。"
								action={{ label: "看全部", onClick: () => setRangeKey("all") }}
							/>
						) : (
							<>
								<section aria-labelledby="history-metrics-heading" className="mb-8" data-testid="history-metrics">
									<h2 id="history-metrics-heading" className="mb-3 text-sm font-medium">
										聚合指标
										<span className="ml-2 text-xs font-normal text-muted-foreground">
											模式+时间范围全量口径 · 二级筛选只作用于明细
										</span>
									</h2>
									<div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
										<div className="rounded-lg border border-border p-4" data-testid="metric-pnl">
											<p className="text-xs text-muted-foreground">总盈亏</p>
											<p
												className={`mt-1 text-2xl font-semibold ${TABULAR_NUMS} ${pnlClass(hasPnl ? totalPnl : null)}`}
											>
												{hasPnl ? signedCny(totalPnl) : "—"}
											</p>
											<p className="mt-2 text-xs text-muted-foreground">判读：红 = 盈 · 绿 = 亏（正负号为准）。</p>
										</div>
										<div className="rounded-lg border border-border p-4" data-testid="metric-count">
											<p className="text-xs text-muted-foreground">注数</p>
											<p className={`mt-1 text-2xl font-semibold ${TABULAR_NUMS}`}>{metrics.settledN}</p>
											<p className="mt-2 text-xs text-muted-foreground">当前模式与时间范围下已结算的注（全量）。</p>
										</div>
										<div className="rounded-lg border border-border p-4" data-testid="metric-hitrate">
											<p className="text-xs text-muted-foreground">命中率</p>
											<p className={`mt-1 text-2xl font-semibold ${TABULAR_NUMS}`}>
												{hitRate === null ? "—" : `${(hitRate * 100).toFixed(1)}%`}
											</p>
											<p className="mt-2 text-xs text-muted-foreground">
												判读：越高越准；胜 {metrics.wonN} / 已分胜负 {metrics.decidedN}（退款不计）。
											</p>
										</div>
										<div className="rounded-lg border border-border p-4" data-testid="metric-ev">
											<p className="text-xs text-muted-foreground">平均 EV</p>
											<p
												className={`mt-1 text-2xl font-semibold ${TABULAR_NUMS} ${
													ev ? pnlClass(ev.value) : "text-muted-foreground"
												}`}
											>
												{ev ? `${ev.value >= 0 ? "+" : ""}${(ev.value * 100).toFixed(1)}%` : "—"}
											</p>
											<p className="mt-2 text-xs text-muted-foreground">
												{ev ? (
													<>
														判读：正 = 有正期望。
														<GlossaryTerm id="bet-ev-snapshot">{EV_CALIBER_LABELS[ev.caliber]}</GlossaryTerm>
														快照均值 {ev.count}/{metrics.settledN} 注（无快照注不计）。
													</>
												) : (
													"判读：正 = 有正期望。暂无带 EV 快照的已结算注（快照随新注落库，存量注无快照不计；机会级 EV 见今日页）。"
												)}
											</p>
										</div>
										<div className="rounded-lg border border-border p-4" data-testid="metric-clv">
											<p className="text-xs text-muted-foreground">平均 CLV</p>
											<p className={`mt-1 text-2xl font-semibold ${TABULAR_NUMS} text-muted-foreground`}>—</p>
											<p className="mt-2 text-xs text-muted-foreground">
												判读：正 = 买在好价（closing 优于锁定）。注级 CLV 未随 v1 API
												提供，聚合口径见验证页；收盘基准已三级分层（
												<GlossaryTerm id="clv-basis">基准分层</GlossaryTerm>
												），来源随记录标注。
											</p>
										</div>
									</div>
								</section>

								<section aria-labelledby="history-chart-heading" className="mb-8">
									<h2 id="history-chart-heading" className="mb-3 text-sm font-medium">
										盈亏累计曲线
										<span className="ml-2 text-xs font-normal text-muted-foreground">
											按结算日（北京业务日）累计 · 虚线为 0 基准
										</span>
									</h2>
									<div className="rounded-lg border border-border bg-card p-4">
										{points.length < 2 ? (
											<p data-testid="history-chart-empty" className="text-sm text-muted-foreground">
												有结算的业务日不足 2 天（曲线按日桶），待数据积累。
											</p>
										) : (
											<CumulativeChart points={points} />
										)}
									</div>
								</section>

								<section aria-labelledby="history-detail-heading" data-testid="history-detail-section">
									<div className="mb-3 flex flex-wrap items-center justify-between gap-2">
										<h2 id="history-detail-heading" className="text-sm font-medium">
											注明细 <span className={`${TABULAR_NUMS} text-muted-foreground`}>{filteredDesc.length}</span>
											{betsQuery.isError ? (
												<span className="ml-2 text-xs font-normal text-warning">明细加载失败——指标不受影响。</span>
											) : null}
										</h2>
										<button
											type="button"
											data-testid="detail-toggle"
											aria-expanded={detailOpen}
											aria-controls="history-detail"
											className="rounded-md border border-border px-3 py-1.5 text-sm transition-colors hover:bg-muted"
											onClick={() => setDetailOpen(!detailOpen)}
										>
											{detailOpen ? "收起明细" : "查看明细"}
										</button>
									</div>
									{detailOpen ? (
										<div data-testid="history-detail" id="history-detail">
											{filteredDesc.length === 0 ? (
												<p
													data-testid="history-detail-empty"
													className="rounded-lg border border-border p-4 text-sm text-muted-foreground"
												>
													当前筛选下（最新 100 注内）无已结算注——放宽时间范围或
													<button
														type="button"
														className="ml-1 text-primary underline-offset-2 hover:underline"
														onClick={clearFilters}
													>
														清二级筛选
													</button>
													。
												</p>
											) : (
												<div className="overflow-x-auto rounded-lg border border-border">
													<Table data-density="compact">
														<TableHeader>
															<TableRow>
																<TableHead>内容</TableHead>
																<TableHead>注金</TableHead>
																<TableHead>状态</TableHead>
																<TableHead>盈亏</TableHead>
																<TableHead>结算时点</TableHead>
															</TableRow>
														</TableHeader>
														<TableBody>
															{filteredDesc.map((bet) => (
																<TableRow key={bet.id} data-testid="history-detail-row">
																	<TableCell className="whitespace-nowrap">{legText(bet)}</TableCell>
																	<TableCell className={`${TABULAR_NUMS} whitespace-nowrap`}>
																		{stakeText(bet)}
																	</TableCell>
																	<TableCell>
																		<StatusBadge status={bet.status} />
																	</TableCell>
																	<TableCell className={`${TABULAR_NUMS} whitespace-nowrap ${pnlClass(bet.profit)}`}>
																		{bet.profit === null
																			? "—"
																			: `${bet.profit >= 0 ? "+" : ""}${bet.profit.toFixed(2)}`}
																	</TableCell>
																	<TableCell className={`${TABULAR_NUMS} whitespace-nowrap text-muted-foreground`}>
																		{shortTime(bet.settled_at)}
																	</TableCell>
																</TableRow>
															))}
														</TableBody>
													</Table>
												</div>
											)}
										</div>
									) : null}
								</section>
							</>
						)}
					</>
				) : null}
			</div>
		</AppShell>
	);
}
