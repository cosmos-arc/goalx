import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { type Bet, fetchBets, fetchTodayFixtures } from "../api/goalx";
import { AppShell } from "../components/app-shell";
import type { EChartsOption } from "../components/charts/echarts";
import { useECharts } from "../components/charts/use-echarts";
import { EmptyState } from "../components/empty-state";
import { GlossaryTerm } from "../components/glossary-term";
import { Badge } from "../components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { SELECTION_LABELS, TABULAR_NUMS } from "../lib/ui";

/**
 * 票 17：历史复盘页落地（票 07 定稿 = 唯一事实源）。分析优先：五指标卡 + 盈亏累计
 * 曲线 + "查看明细"同页展开，流水是下钻层不是主界面。数据源 = 现有 `GET /bets`
 * 前端过滤聚合（票 10 定稿零新端点），样本口径 = 已锁定（purchased）且已结算
 * （settled_at 非空，与票 16 已结算段同判定）；纸面/真金大标签常显、分别统计不混算。
 *
 * 口径红线：
 * - 页头固定一行按 07 Answer 逐字："统计已锁定且已结算的注；前瞻验证口径（含排除
 *   规则）见验证页。"两页数字不同时各自以口径标注为准；术语"前瞻纳入"由 17 号的
 *   链接占位升级为 18 号的 GlossaryTerm Popover（悬停/聚焦就地看口径）。
 * - 平均 EV / 平均 CLV：BetView 无注级 EV/CLV 字段（v1 API），按缺失显示"—"并注明
 *   口径与判读方向（CLV 正 = 买在好价）；不造假数据，聚合口径见验证页。
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

const STATUS_META: Record<string, { label: string; className: string }> = {
	won: { label: "胜", className: "bg-profit/10 text-foreground" },
	lost: { label: "负", className: "bg-loss/10 text-foreground" },
	void: { label: "退款", className: "bg-muted text-muted-foreground" },
	partial: { label: "部分", className: "bg-warning/10 text-foreground" },
};

/** 盈亏数字永远只按正负红绿（票 02 钱层编码，正负号为主承载）。 */
function pnlClass(value: number | null): string {
	if (value === null || value === 0) {
		return "text-muted-foreground";
	}
	return value > 0 ? "text-profit" : "text-loss";
}

function signedCny(value: number): string {
	return `${value > 0 ? "+" : value < 0 ? "-" : ""}¥${Math.abs(value).toFixed(2)}`;
}

function shortTime(iso: string | null): string {
	return iso ? iso.slice(5, 16).replace("T", " ") : "—";
}

function legText(bet: Bet): string {
	return bet.legs
		.map((leg) => {
			const odds =
				leg.actual_odds !== null && leg.actual_odds !== undefined
					? `${leg.locked_odds.toFixed(2)}→${leg.actual_odds.toFixed(2)}`
					: leg.locked_odds.toFixed(2);
			return `#${leg.fixture_id} ${leg.market_code === "hhad" ? `让球${leg.goal_line ?? "?"} ` : ""}${
				SELECTION_LABELS[leg.selection_code] ?? leg.selection_code
			}@${odds}`;
		})
		.join(" × ");
}

function stakeText(bet: Bet): string {
	if (bet.actual_stake === null || bet.actual_stake === undefined) {
		return `¥${bet.stake.toFixed(2)}`;
	}
	return `¥${bet.stake.toFixed(2)}→¥${bet.actual_stake.toFixed(2)}`;
}

function StatusBadge({ status }: { status: string }) {
	const meta = STATUS_META[status];
	if (!meta) {
		return <span className="text-xs text-muted-foreground">{status}</span>;
	}
	return (
		<Badge variant="outline" className={`border-transparent ${meta.className}`}>
			{meta.label}
		</Badge>
	);
}

/** canvas 取不到 CSS 变量，option 构建时解析语义 token（解析失败退回近似色）。 */
function cssVar(name: string, fallback: string): string {
	const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
	return raw === "" ? fallback : raw;
}

/** 结算时点升序的累计盈亏点（每注一个点；profit 缺失按 0 接力，不做断点）。 */
export function buildCumulativePoints(bets: Bet[]): Array<{ label: string; value: number }> {
	const at = (bet: Bet): number => new Date(bet.settled_at ?? "").getTime();
	const ordered = [...bets].sort((a, b) => at(a) - at(b));
	let cumulative = 0;
	return ordered.map((bet) => {
		cumulative += bet.profit ?? 0;
		return { label: shortTime(bet.settled_at), value: Math.round(cumulative * 100) / 100 };
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
	const ref = useECharts(
		cumulativePnlOption(points, cssVar("--chart-1", "#2563eb"), cssVar("--muted-foreground", "#6b7280")),
	);
	return (
		<div ref={ref} data-testid="history-chart" role="img" aria-label="盈亏累计曲线（0 基准）" className="h-64 w-full" />
	);
}

export function HistoryPage() {
	const betsQuery = useQuery({ queryKey: ["bets"], queryFn: () => fetchBets() });
	const todayQuery = useQuery({ queryKey: ["today"], queryFn: () => fetchTodayFixtures() });
	const [mode, setMode] = useState<Mode>("paper");
	const [rangeKey, setRangeKey] = useState<RangeKey>("all");
	const [competition, setCompetition] = useState("");
	const [strategy, setStrategy] = useState("");
	const [status, setStatus] = useState("");
	const [detailOpen, setDetailOpen] = useState(false);
	const now = Date.now();

	const allBets = betsQuery.data ?? [];
	// 样本口径：已锁定且已结算（票 07 定稿；与票 16 已结算段同判定）
	const modeSettled = allBets.filter((bet) => bet.mode === mode && bet.purchased && bet.settled_at !== null);

	// Competition 归因：唯一来源是今日列表的 competition 字段（v1 无历史 fixture 端点）
	const competitionByFixture = new Map(
		(todayQuery.data ?? []).map((fixture) => [fixture.fixture_id, fixture.competition]),
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
	const startMs = range.days === null ? null : now - range.days * 86_400_000;

	const filtered = modeSettled.filter((bet) => {
		const settledMs = new Date(bet.settled_at ?? "").getTime();
		if (startMs !== null && (Number.isNaN(settledMs) || settledMs < startMs)) {
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
	});

	// 明细行按结算时点倒序（最近结算在前，与票 16 已结算段同序）
	const filteredDesc = [...filtered].sort(
		(a, b) => new Date(b.settled_at ?? "").getTime() - new Date(a.settled_at ?? "").getTime(),
	);

	// ---- 五指标（随筛选联动；07 定稿：各带判读方向） ----
	const profits = filtered.flatMap((bet) => (bet.profit === null ? [] : [bet.profit]));
	const totalPnl = profits.reduce((acc, value) => acc + value, 0);
	const wonCount = filtered.filter((bet) => bet.status === "won").length;
	const decidedCount = filtered.filter((bet) => bet.status !== "void").length;
	const hitRate = decidedCount > 0 ? wonCount / decidedCount : null;

	const points = buildCumulativePoints(filtered);

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

				{betsQuery.isPending ? (
					<div className="space-y-4" data-testid="history-loading">
						<span className="sr-only">加载历史数据…</span>
						<div className="h-10 animate-pulse rounded-lg bg-muted" />
						<div className="h-40 animate-pulse rounded-lg bg-muted" />
					</div>
				) : null}

				{betsQuery.isError ? (
					<EmptyState
						variant="backend-unavailable"
						message="历史数据加载失败。"
						hint={
							<>
								用 <code>task server</code> 启动 API。
							</>
						}
						action={{ label: "重试", onClick: () => void betsQuery.refetch() }}
					/>
				) : null}

				{betsQuery.data ? (
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

						{filtered.length === 0 ? (
							<EmptyState
								variant="no-data"
								message="该筛选下无已结算注"
								hint="放宽时间范围或清除二级筛选；纸面/真金分别查看。"
								action={{ label: "清筛选", onClick: clearFilters }}
							/>
						) : (
							<>
								<section aria-labelledby="history-metrics-heading" className="mb-8" data-testid="history-metrics">
									<h2 id="history-metrics-heading" className="mb-3 text-sm font-medium">
										聚合指标
										<span className="ml-2 text-xs font-normal text-muted-foreground">
											随筛选联动 · 纸面/真金分别统计不混算
										</span>
									</h2>
									<div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
										<div className="rounded-lg border border-border p-4" data-testid="metric-pnl">
											<p className="text-xs text-muted-foreground">总盈亏</p>
											<p
												className={`mt-1 text-2xl font-semibold ${TABULAR_NUMS} ${pnlClass(profits.length > 0 ? totalPnl : null)}`}
											>
												{profits.length > 0 ? signedCny(totalPnl) : "—"}
											</p>
											<p className="mt-2 text-xs text-muted-foreground">判读：红 = 盈 · 绿 = 亏（正负号为准）。</p>
										</div>
										<div className="rounded-lg border border-border p-4" data-testid="metric-count">
											<p className="text-xs text-muted-foreground">注数</p>
											<p className={`mt-1 text-2xl font-semibold ${TABULAR_NUMS}`}>{filtered.length}</p>
											<p className="mt-2 text-xs text-muted-foreground">当前筛选下已结算的注。</p>
										</div>
										<div className="rounded-lg border border-border p-4" data-testid="metric-hitrate">
											<p className="text-xs text-muted-foreground">命中率</p>
											<p className={`mt-1 text-2xl font-semibold ${TABULAR_NUMS}`}>
												{hitRate === null ? "—" : `${(hitRate * 100).toFixed(1)}%`}
											</p>
											<p className="mt-2 text-xs text-muted-foreground">
												判读：越高越准；胜 {wonCount} / 已分胜负 {decidedCount}（退款不计）。
											</p>
										</div>
										<div className="rounded-lg border border-border p-4" data-testid="metric-ev">
											<p className="text-xs text-muted-foreground">平均 EV</p>
											<p className={`mt-1 text-2xl font-semibold ${TABULAR_NUMS} text-muted-foreground`}>—</p>
											<p className="mt-2 text-xs text-muted-foreground">
												判读：正 = 有正期望。注级 EV 未随 v1 API 提供（机会级 EV 见今日页），暂不可算。
											</p>
										</div>
										<div className="rounded-lg border border-border p-4" data-testid="metric-clv">
											<p className="text-xs text-muted-foreground">平均 CLV</p>
											<p className={`mt-1 text-2xl font-semibold ${TABULAR_NUMS} text-muted-foreground`}>—</p>
											<p className="mt-2 text-xs text-muted-foreground">
												判读：正 = 买在好价（closing 优于锁定）。注级 CLV 未随 v1 API 提供，聚合口径见验证页。
											</p>
										</div>
									</div>
								</section>

								<section aria-labelledby="history-chart-heading" className="mb-8">
									<h2 id="history-chart-heading" className="mb-3 text-sm font-medium">
										盈亏累计曲线
										<span className="ml-2 text-xs font-normal text-muted-foreground">
											按结算时点累计 · 虚线为 0 基准
										</span>
									</h2>
									<div className="rounded-lg border border-border bg-card p-4">
										{points.length < 2 ? (
											<p data-testid="history-chart-empty" className="text-sm text-muted-foreground">
												已结算注不足 2 注，累计曲线待数据积累。
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
																<TableCell className={`${TABULAR_NUMS} whitespace-nowrap`}>{stakeText(bet)}</TableCell>
																<TableCell>
																	<StatusBadge status={bet.status} />
																</TableCell>
																<TableCell className={`${TABULAR_NUMS} whitespace-nowrap ${pnlClass(bet.profit)}`}>
																	{bet.profit === null ? "—" : `${bet.profit >= 0 ? "+" : ""}${bet.profit.toFixed(2)}`}
																</TableCell>
																<TableCell className={`${TABULAR_NUMS} whitespace-nowrap text-muted-foreground`}>
																	{shortTime(bet.settled_at)}
																</TableCell>
															</TableRow>
														))}
													</TableBody>
												</Table>
											</div>
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
