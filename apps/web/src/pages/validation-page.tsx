import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import {
	type BacktestRun,
	type ClvReport,
	type ForwardSkillReport,
	fetchBacktestRuns,
	fetchValidationProgress,
	type ValidationProgress,
} from "../api/goalx";
import { AppShell } from "../components/app-shell";
import type { EChartsOption } from "../components/charts/echarts";
import { useECharts } from "../components/charts/use-echarts";
import { EmptyState } from "../components/empty-state";
import { GlossaryTerm } from "../components/glossary-term";
import { Badge } from "../components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import type { GlossaryId } from "../lib/glossary";
import { backtestRunsKey, validationProgressKey } from "../lib/query-keys";
import { cssVar, TABULAR_NUMS } from "../lib/ui";

/**
 * 票 19：验证页重设计落地（票 09 定稿 = 唯一事实源）。
 *
 * 首屏 = 一行状态结论（三条件 x/3 + 还差什么）+ 三条件卡 + 前瞻 yield 主图
 * （滚动/累计双线，0 基准虚线 markline——"≥0 是唯一通过线"的可视化）。
 * 下钻层 = CLV 明细、样本约束、回测 vs 前瞻对比、条件口径说明，折叠呈现。
 *
 * 口径红线（票 09 不变量）：
 * - 验证三条件只认前瞻 skill（开球前 Forecast × 同期市场基准）；回测 skill 是
 *   诊断量，呈现时必须标注"口径上不算通过线"，不得出现在首屏、不得暗示可替代。
 * - 条件达成/进行中的判定以服务端（evaluation/validation）为准，前端只呈现，
 *   不在呈现层自创或弱化通过线。
 * - 成本摘要刻意不上本页（资金页的事，票 20）。
 *
 * 数据源（票 06 起）：clv/forward 为契约 typed models（后端改键 = CI 红灯，
 * 不再页上静默消失）；回测 run 的 summary/overall_metrics 仍是自由字典
 * （本票外），取值经 asNumber 容错。
 */

type ConditionProgress = ValidationProgress["conditions"][number];
type YieldPoint = ValidationProgress["yield_curve"][number];

// ---- 数值容错（仅回测 run 自由字典用；clv/forward 已是契约类型） ----

function asNumber(value: unknown): number | null {
	return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function formatPct(value: number | null | undefined, digits = 1): string {
	return value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)}%`;
}

function formatSigned(value: number | null | undefined, digits = 4): string {
	if (value === null || value === undefined) return "—";
	return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}`;
}

/** 概率域 CLV 转百分比带符号（+0.01 → +1.00%），与词典"持续 +0.5%~3%"同读法。 */
function formatSignedPct(value: number | null | undefined, digits = 2): string {
	if (value === null || value === undefined) return "—";
	return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(digits)}%`;
}

// ---- 状态结论（首屏第一行；整赛季条件独立显示，不计入三条件——与总览 x/3 同口径） ----

export type ValidationVerdict = {
	achieved: number;
	total: number;
	unmet: string[];
	fullSeason: ConditionProgress | null;
};

export function validationVerdict(conditions: readonly ConditionProgress[]): ValidationVerdict {
	const core = conditions.filter((condition) => condition.key !== "full_season");
	return {
		achieved: core.filter((condition) => condition.achieved).length,
		total: core.length,
		unmet: core.filter((condition) => !condition.achieved).map((condition) => condition.label),
		fullSeason: conditions.find((condition) => condition.key === "full_season") ?? null,
	};
}

export function verdictText(verdict: ValidationVerdict): string {
	if (verdict.total === 0) {
		return "服务端暂未返回验证条件，无法下结论。";
	}
	const base =
		verdict.total === 3
			? `纸面转真金三条件 ${verdict.achieved}/${verdict.total}`
			: `验证条件 ${verdict.achieved}/${verdict.total}`;
	return verdict.unmet.length === 0
		? `${base}——前瞻口径全部达成；整赛季窗口覆盖仍需独立验收。`
		: `${base}——还差：${verdict.unmet.join("；")}`;
}

// ---- 前瞻 yield 主图 option（纯函数：双线 + 0 基准虚线 markline） ----

/**
 * 数据 → option 的纯映射（use-echarts 测试约定）：累计（蓝）与滚动 100 注（橙）
 * 双线；0 基准虚线 = "skill≥0 唯一通过线"在收益口径上的同构判读。rolling 缺失
 * 的点传 null 由 ECharts 断线，不用累计值冒充。
 */
export function yieldCurveOption(
	points: YieldPoint[],
	cumulativeColor: string,
	rollingColor: string,
	axisColor: string,
): EChartsOption {
	return {
		grid: { left: 8, right: 16, top: 28, bottom: 8, containLabel: true },
		tooltip: { trigger: "axis" },
		legend: { top: 0, textStyle: { color: axisColor, fontSize: 11 } },
		xAxis: {
			type: "category",
			data: points.map((point) => `#${point.index}`),
			axisLine: { lineStyle: { color: axisColor } },
			axisTick: { show: false },
			axisLabel: { color: axisColor, fontSize: 11 },
		},
		yAxis: {
			type: "value",
			axisLabel: { color: axisColor, fontSize: 11, formatter: (value: number) => `${Math.round(value * 100)}%` },
			splitLine: { lineStyle: { color: axisColor, opacity: 0.3 } },
		},
		series: [
			{
				name: "累计 yield",
				type: "line",
				data: points.map((point) => point.cumulative_yield),
				symbol: "none",
				lineStyle: { color: cumulativeColor, width: 2 },
				itemStyle: { color: cumulativeColor },
				markLine: {
					silent: true,
					symbol: "none",
					label: { show: false },
					lineStyle: { type: "dashed", color: axisColor },
					data: [{ yAxis: 0 }],
				},
			},
			{
				name: "滚动 100 注",
				type: "line",
				data: points.map((point) => point.rolling_yield ?? null),
				symbol: "none",
				lineStyle: { color: rollingColor, width: 2 },
				itemStyle: { color: rollingColor },
			},
		],
	};
}

/**
 * 收益曲线独立子组件（票 17 的 CumulativeChart 同模式）：useECharts 的 init
 * effect 只在挂载时跑一次（deps=[]），容器必须随组件一起挂载——数据到位且
 * 点数 ≥2 才挂，否则 init 拿不到容器。
 */
function YieldChart({ points }: { points: YieldPoint[] }) {
	const ref = useECharts(
		yieldCurveOption(
			points,
			cssVar("--chart-1", "#2563eb"),
			cssVar("--chart-2", "#ea580c"),
			cssVar("--muted-foreground", "#6b7280"),
		),
	);
	return (
		<div
			ref={ref}
			data-testid="yield-curve"
			role="img"
			aria-label="前瞻收益曲线：累计与滚动 100 注双线，虚线为 0 基准通过线"
			className="h-64 w-full"
		/>
	);
}

// ---- 契约类型指标映射（票 06：键集在 schema 冻结，改键 = CI 红） ----

export type MetricRow = {
	id: string;
	label: string;
	value: string;
	hint?: string;
	term?: GlossaryId;
};

/** 基准来源分层的显示名（票 40：锚级顺序固定，未知级原样显示）。 */
const CLOSE_BASIS_LABELS: Record<string, string> = {
	pinnacle: "Pinnacle 主锚",
	betfair_ex: "Betfair 辅锚(扣佣)",
	consensus: "多书共识 fallback",
	legacy: "分层前共识(legacy)",
	mixed: "串关跨基准(mixed)",
};

/** clv 报表 → 指标行（契约类型直取；分桶/分层字典键为动态扩展位）。 */
export function clvMetricRows(clv: ClvReport): MetricRow[] {
	const rows: MetricRow[] = [];
	for (const [key, kind] of [
		["singles", "单关"],
		["parlay2", "2串1"],
	] as const) {
		for (const mode of ["paper", "live"] as const) {
			// 后端恒填 paper/live 两键；TS 索引签名层面表达不了"键恒在"，落零值兜底
			const stats = clv[key][mode] ?? { n_bets: 0 };
			rows.push({
				id: `${key}-${mode}`,
				label: `${kind} · ${mode === "paper" ? "纸面" : "真金"} beat rate`,
				term: "clv",
				value: formatPct(stats.beat_rate),
				hint: `n=${stats.n_bets} 注 · 平均 CLV ${formatSignedPct(stats.avg_clv)}（判读：≥60% 为门槛，正 = 买在好价）`,
			});
		}
	}

	const parts: string[] = [];
	for (const basis of ["pinnacle", "betfair_ex", "consensus", "legacy", "mixed"] as const) {
		const entry = clv.by_close_basis[basis];
		if (entry === undefined) {
			continue;
		}
		const paperSingle = entry.groups?.["single"]?.["paper"];
		const pieces = [
			`${entry.bets} 注`,
			entry.legs ? `${entry.legs} 腿` : null,
			paperSingle && paperSingle.n_bets > 0 && paperSingle.beat_rate !== null && paperSingle.beat_rate !== undefined
				? `纸面单关 beat ${formatPct(paperSingle.beat_rate)}(n=${paperSingle.n_bets})`
				: null,
		].filter((piece): piece is string => piece !== null);
		if (pieces.length > 0) {
			parts.push(`${CLOSE_BASIS_LABELS[basis] ?? basis} ${pieces.join(" · ")}`);
		}
	}
	if (parts.length > 0) {
		rows.push({
			id: "close-basis",
			label: "基准来源分层",
			term: "clv-basis",
			value: parts.join(" · "),
			hint: clv.close_basis_note,
		});
	}

	rows.push({
		id: "independence",
		label: "串关票级联合口径",
		value: clv.independence_assumed ? "两腿独立连乘（假设已声明）" : "未声明独立性假设",
		hint: "beat 按票级联合概率判定；腿级 CLV 仅诊断。",
	});

	const denom = clv.denominator;
	rows.push({
		id: "denominator",
		label: "样本分母（唯一注）",
		value: `${denom.unique_bets} 注`,
		hint: `原始 ${denom.raw_bets} · 重复去重 ${denom.deduped_duplicates} · 缺收盘 ${denom.no_close_bets} · 覆盖场次 ${denom.fixtures}`,
	});

	const bucketEntries = Object.entries(clv.by_minutes_bucket_single);
	if (bucketEntries.length > 0) {
		const text = bucketEntries
			.map(([bucket, stats]) => `${bucket} ${formatPct(stats.beat_rate, 0)}（n=${stats.n}）`)
			.join(" · ");
		rows.push({
			id: "minutes-buckets",
			label: "距开赛分桶 beat rate（单关）",
			value: text,
			hint: "下注越早通常 CLV 越高；分桶看买入时点质量。",
		});
	}

	// 渲染等价（迁移前口径）：slope 无值（样本 <3 的空/小数据形态）整行不出
	if (clv.regression.slope !== null && clv.regression.slope !== undefined) {
		rows.push({
			id: "regression",
			label: "单关 CLV → 盈亏回归",
			value: `斜率 ${clv.regression.slope.toFixed(2)} · R²=${clv.regression.r_squared?.toFixed(2) ?? "—"}`,
			hint: `n=${clv.regression.n}（仅单关；串关不作独立样本）`,
		});
	}

	return rows;
}

/** forward 报表 → 指标行（覆盖四态 + 分组 skill）。 */
export function forwardMetricRows(forward: ForwardSkillReport): MetricRow[] {
	const rows: MetricRow[] = [
		{
			id: "rule",
			label: "纳入规则（防时间泄漏）",
			term: "forward-inclusion",
			value: forward.rule,
			hint: "只计开球前发出的最新 Forecast；开球后补发一律排除。",
		},
		{
			id: "coverage",
			label: "前瞻覆盖（排除全计）",
			value: [
				`scored ${forward.coverage.scored}`,
				`无预测 ${forward.coverage.no_forecast}`,
				`仅赛后 ${forward.coverage.post_kickoff_only}`,
				`无基准 ${forward.coverage.no_market_baseline}`,
			].join(" · "),
			hint: `已结算 ${forward.coverage.settled_fixtures} 场；scored = 纳入计分，其余为排除分母（不静默丢弃）。`,
		},
	];
	for (const [version, metrics] of Object.entries(forward.groups)) {
		rows.push({
			id: `group-${version}`,
			label: `前瞻分组 ${version}`,
			term: "skill",
			value: `skill ${formatSigned(metrics.skill_rps)} · n=${metrics.n}`,
			hint: metrics.insufficient_samples
				? `样本不足（<30 场），不作通过依据 · RPS 模型 ${formatSigned(metrics.rps_model, 3)} vs 市场 ${formatSigned(metrics.rps_market, 3)}`
				: `RPS 模型 ${formatSigned(metrics.rps_model, 3)} vs 市场 ${formatSigned(metrics.rps_market, 3)}`,
		});
	}
	return rows;
}

/** 达标判定与服务器同构（validation.py）：样本足的分组里取最好 skill；全样本不足则只报不足。 */
export function bestForwardSkill(forward: ForwardSkillReport): {
	skill: number | null;
	version: string | null;
	allInsufficient: boolean;
} {
	let best: { skill: number; version: string } | null = null;
	let anyGroup = false;
	for (const [version, metrics] of Object.entries(forward.groups)) {
		if (metrics.insufficient_samples) {
			continue;
		}
		anyGroup = true;
		if (best === null || metrics.skill_rps > best.skill) {
			best = { skill: metrics.skill_rps, version };
		}
	}
	return { skill: best?.skill ?? null, version: best?.version ?? null, allInsufficient: !anyGroup };
}

// ---- 呈现组件 ----

/** 条件口径的判读说明（票 map 红线：指标呈现必须附判读方向；key 未知的条件不硬造）。 */
const CONDITION_HINTS: Record<string, ReactNode> = {
	clv_beat: (
		<>
			判读：beat rate ≥60% 且唯一注 ≥200 才算达成；分母是去重后的唯一注，腿数不凑。口径见{" "}
			<GlossaryTerm id="clv">CLV</GlossaryTerm> 词条。
		</>
	),
	market_skill: (
		<>
			判读：skill ≥0 是唯一通过线；只认前瞻口径（开球前 Forecast × 市场基准），回测 skill 不算此条。见{" "}
			<GlossaryTerm id="skill">skill</GlossaryTerm> 词条。
		</>
	),
	review_errors: <>判读：无复核记录 = 未评估（不做真空通过）；系统性错误的认定随复核线上线。</>,
	full_season: <>整赛季窗口覆盖独立验收，不计入三条件；缺真实整赛季证据前恒未完成。</>,
};

function ConditionCard({ condition }: { condition: ConditionProgress }) {
	return (
		<div
			className="rounded-lg border border-border bg-card p-4"
			data-testid="condition-row"
			data-condition-key={condition.key}
		>
			<div className="flex items-start justify-between gap-2">
				<p className="text-sm font-medium">{condition.label}</p>
				<span
					data-testid="condition-status"
					className={`shrink-0 rounded-full px-2 py-0.5 text-xs ${
						// 徽章文字用前景色：muted-foreground 压 10% 底纹 <AA 4.5:1（票 14/15 同教训）
						condition.achieved ? "bg-success/10 text-foreground" : "bg-muted text-foreground"
					}`}
				>
					{condition.achieved ? "达成" : "进行中"}
				</span>
			</div>
			<p className={`mt-2 text-xs ${TABULAR_NUMS}`}>当前 {condition.current}</p>
			<p className={`text-xs text-muted-foreground ${TABULAR_NUMS}`}>目标 {condition.target}</p>
			{CONDITION_HINTS[condition.key] ? (
				<p className="mt-2 text-xs text-muted-foreground">{CONDITION_HINTS[condition.key]}</p>
			) : null}
		</div>
	);
}

function MetricRowList({ rows, testid }: { rows: MetricRow[]; testid: string }) {
	if (rows.length === 0) {
		return (
			<p data-testid={testid} className="text-sm text-muted-foreground">
				本组暂无可呈现指标。
			</p>
		);
	}
	return (
		<ul className="space-y-2.5" data-testid={testid}>
			{rows.map((row) => (
				<li key={row.id} className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-0.5">
					<span className="text-sm">
						{row.term ? <GlossaryTerm id={row.term}>{row.label}</GlossaryTerm> : row.label}
					</span>
					<span className={`text-sm font-medium ${TABULAR_NUMS}`}>{row.value}</span>
					{row.hint ? <span className="w-full text-xs text-muted-foreground">{row.hint}</span> : null}
				</li>
			))}
		</ul>
	);
}

function CompareCard({
	label,
	value,
	hint,
	tone,
}: {
	label: string;
	value: string;
	hint: string;
	tone?: "neutral" | "positive" | "negative";
}) {
	const toneClass = tone === "positive" ? "text-profit" : tone === "negative" ? "text-loss" : "text-foreground";
	return (
		<div className="rounded-lg border border-border bg-card p-4">
			<p className="text-xs text-muted-foreground">{label}</p>
			<p data-testid={`metric-${label}`} className={`mt-1 text-xl font-semibold ${TABULAR_NUMS} ${toneClass}`}>
				{value}
			</p>
			<p className="mt-2 text-xs text-muted-foreground">{hint}</p>
		</div>
	);
}

function signedTone(value: number | null | undefined): "neutral" | "positive" | "negative" {
	if (value === null || value === undefined || value === 0) return "neutral";
	return value > 0 ? "positive" : "negative";
}

/** 回测 run 列表（下钻层；口径上不算通过线）。 */
function BacktestRunTable({ runs }: { runs: BacktestRun[] }) {
	return (
		<div className="overflow-x-auto rounded-lg border border-border">
			<Table data-density="compact">
				<TableHeader>
					<TableRow>
						<TableHead>ID</TableHead>
						<TableHead>标签</TableHead>
						<TableHead>状态</TableHead>
						<TableHead>场数</TableHead>
						<TableHead>注数</TableHead>
						<TableHead>ROI</TableHead>
					</TableRow>
				</TableHeader>
				<TableBody>
					{runs.map((run) => (
						<TableRow key={run.id} data-testid="backtest-run-row">
							<TableCell className={TABULAR_NUMS}>{run.id}</TableCell>
							<TableCell>{run.label}</TableCell>
							<TableCell>{run.status}</TableCell>
							<TableCell className={TABULAR_NUMS}>{asNumber(run.summary?.["predictions"]) ?? "—"}</TableCell>
							<TableCell className={TABULAR_NUMS}>{asNumber(run.summary?.["bets"]) ?? "—"}</TableCell>
							<TableCell className={TABULAR_NUMS}>{formatPct(asNumber(run.summary?.["roi"]))}</TableCell>
						</TableRow>
					))}
				</TableBody>
			</Table>
		</div>
	);
}

// ---- 页面 ----

export function ValidationPage() {
	const progress = useQuery({ queryKey: validationProgressKey, queryFn: () => fetchValidationProgress() });
	const runs = useQuery({ queryKey: backtestRunsKey, queryFn: () => fetchBacktestRuns() });

	const data = progress.data ?? null;
	const verdict = data ? validationVerdict(data.conditions) : null;
	const curve = data?.yield_curve ?? [];
	const latestRun = data?.latest_run ?? runs.data?.[0] ?? null;
	const metrics = latestRun?.overall_metrics ?? null;
	const backtestSkill = metrics ? asNumber(metrics["skill_rps"]) : null;
	const clvRows = data ? clvMetricRows(data.clv) : [];
	const forwardRows = data ? forwardMetricRows(data.forward) : [];
	const bestForward = data ? bestForwardSkill(data.forward) : null;
	const backtestRuns = runs.data ?? [];

	return (
		<AppShell title="验证">
			<div className="pb-4">
				<header className="mb-5 space-y-1">
					{/* 口径行：票 09 不变量逐字落呈——三条件只认前瞻 skill，判定以服务端为准 */}
					<p className="text-sm text-muted-foreground">
						纸面转真金前三条件验证：只认前瞻 skill，回测口径不算通过线；达成与否由服务端判定，本页只做呈现。
					</p>
					{verdict ? (
						<p data-testid="validation-verdict" className="text-base font-semibold">
							{verdictText(verdict)}
						</p>
					) : null}
				</header>

				{progress.isPending ? (
					<div className="space-y-4" data-testid="validation-loading">
						<span className="sr-only">加载验证进度…</span>
						<div className="h-8 w-2/3 animate-pulse rounded-lg bg-muted" />
						<div className="h-28 animate-pulse rounded-lg bg-muted" />
						<div className="h-64 animate-pulse rounded-lg bg-muted" />
					</div>
				) : null}

				{progress.isError ? (
					<div data-testid="validation-error">
						<EmptyState
							variant="backend-unavailable"
							message="验证进度加载失败。"
							hint={
								<>
									用 <code>task server</code> 启动 API。
								</>
							}
							action={{
								label: "重试",
								onClick: () => {
									void progress.refetch();
									void runs.refetch();
								},
							}}
						/>
					</div>
				) : null}

				{data && verdict ? (
					<>
						{/* 首屏：三条件卡（整赛季独立显示，不计入 x/3） */}
						<section aria-labelledby="validation-conditions-heading" className="mb-8">
							<h2 id="validation-conditions-heading" className="mb-3 text-sm font-medium">
								三条件进度
								<span className="ml-2 text-xs font-normal text-muted-foreground">
									达成/进行中以服务端判定为准 · 整赛季覆盖独立验收不计入
								</span>
							</h2>
							<div className="grid gap-3 md:grid-cols-3">
								{data.conditions
									.filter((condition) => condition.key !== "full_season")
									.map((condition) => (
										<ConditionCard key={condition.key} condition={condition} />
									))}
							</div>
							{verdict.fullSeason ? (
								<div
									className="mt-3 rounded-lg border border-dashed border-border bg-card p-4"
									data-testid="full-season-card"
								>
									<div className="flex items-start justify-between gap-2">
										<p className="text-sm font-medium">{verdict.fullSeason.label}</p>
										<Badge variant="outline" className="shrink-0 border-transparent bg-muted text-foreground">
											独立项 · 不计入三条件
										</Badge>
									</div>
									<p className={`mt-2 text-xs ${TABULAR_NUMS}`}>当前 {verdict.fullSeason.current}</p>
									<p className={`mt-2 text-xs text-muted-foreground ${TABULAR_NUMS}`}>
										目标 {verdict.fullSeason.target}
									</p>
									{CONDITION_HINTS["full_season"] ? (
										<p className="mt-2 text-xs text-muted-foreground">{CONDITION_HINTS["full_season"]}</p>
									) : null}
								</div>
							) : null}
						</section>

						{/* 首屏：前瞻 yield 主图（滚动/累计双线 + 0 基准虚线） */}
						<section aria-labelledby="validation-yield-heading" className="mb-8">
							<h2 id="validation-yield-heading" className="mb-3 text-sm font-medium">
								前瞻收益曲线（{data.yield_curve_mode === "live" ? "真金" : "纸面"}唯一注）
								<span className="ml-2 text-xs font-normal text-muted-foreground">
									滚动 100 注 / 累计双口径 · 虚线 0 基准 = 唯一通过线（≥0 才算跑赢）
								</span>
							</h2>
							<div className="rounded-lg border border-border bg-card p-4">
								{curve.length < 2 ? (
									<p data-testid="yield-empty" className="text-sm text-muted-foreground">
										已结算注不足 2，曲线待积累。
									</p>
								) : (
									<YieldChart points={curve} />
								)}
							</div>
						</section>

						{/* 次级区（刻意不上首屏）：回测 vs 前瞻对比——回测口径明确标注不算通过线 */}
						<section aria-labelledby="validation-compare-heading" className="mb-8">
							<h2
								id="validation-compare-heading"
								data-testid="validation-compare-heading"
								className="mb-3 text-sm font-medium"
							>
								回测 vs 前瞻对比
								<span className="ml-2 text-xs font-normal text-muted-foreground">
									回测是诊断量：三条件只认前瞻 skill，回测口径不算通过线
								</span>
							</h2>
							<div className="grid gap-3 md:grid-cols-2">
								<CompareCard
									label="回测 skill"
									value={formatSigned(backtestSkill)}
									tone={signedTone(backtestSkill)}
									hint={
										metrics
											? `walk-forward 口径 · 模型 RPS ${formatSigned(asNumber(metrics["rps_model"]), 3)} vs 市场 ${formatSigned(
													asNumber(metrics["rps_market"]),
													3,
												)}（n=${asNumber(metrics["n"]) ?? "—"}）· DM p=${formatSigned(asNumber(metrics["dm_p"]), 3)}（探索性）`
											: "暂无已完成的回测 run。"
									}
								/>
								<CompareCard
									label="前瞻 skill（通过线口径）"
									value={
										bestForward?.skill !== null && bestForward?.skill !== undefined
											? formatSigned(bestForward.skill)
											: bestForward?.allInsufficient
												? "样本不足"
												: "无前瞻样本"
									}
									tone={signedTone(bestForward?.skill)}
									hint={
										bestForward?.version
											? `最好分组 ${bestForward.version} · skill ≥0 是唯一通过线（样本 ≥30 场才算数）`
											: "开球前 Forecast × 市场基准，与回测分开看；≥0 才是唯一通过线。"
									}
								/>
							</div>
						</section>

						{/* 下钻层：CLV 明细 / 前瞻明细 / 样本约束 / 条件口径 / 回测 run 表 */}
						<details data-testid="validation-drilldown" className="rounded-lg border border-border px-4 py-3">
							<summary className="cursor-pointer text-sm font-medium">
								下钻：CLV 明细 · 前瞻评分 · 样本约束 · 回测 run
							</summary>
							<div className="mt-4 space-y-6 pb-2">
								<section aria-labelledby="validation-clv-heading">
									<h3 id="validation-clv-heading" className="mb-2 text-sm font-medium">
										CLV 明细（单关 / 2串1 × 纸面 / 真金 分开报告）
									</h3>
									<div className="rounded-lg border border-border p-4">
										<MetricRowList rows={clvRows} testid="clv-rows" />
									</div>
								</section>

								<section aria-labelledby="validation-forward-heading">
									<h3 id="validation-forward-heading" className="mb-2 text-sm font-medium">
										前瞻评分明细（纳入规则 · 覆盖四态 · 分组 skill）
									</h3>
									<div className="rounded-lg border border-border p-4">
										<MetricRowList rows={forwardRows} testid="forward-rows" />
									</div>
								</section>

								<section aria-labelledby="validation-sample-heading" data-testid="sample-constraints">
									<h3 id="validation-sample-heading" className="mb-2 text-sm font-medium">
										样本约束（唯一注为验证分母；纸面/真金分开）
									</h3>
									<div className="overflow-x-auto rounded-lg border border-border">
										<Table data-density="compact">
											<TableHeader>
												<TableRow>
													<TableHead>模式</TableHead>
													<TableHead>注</TableHead>
													<TableHead>唯一注</TableHead>
													<TableHead>腿</TableHead>
													<TableHead>场次</TableHead>
													<TableHead>注金</TableHead>
													<TableHead>盈亏</TableHead>
												</TableRow>
											</TableHeader>
											<TableBody>
												{(
													[
														["paper", data.paper],
														["live", data.live],
													] as const
												).map(([mode, counts]) => (
													<TableRow key={mode} data-testid={`sample-${mode}`}>
														<TableCell>{mode === "paper" ? "纸面" : "真金"}</TableCell>
														<TableCell className={TABULAR_NUMS}>{counts.bets}</TableCell>
														<TableCell className={TABULAR_NUMS}>{counts.unique_bets}</TableCell>
														<TableCell className={TABULAR_NUMS}>{counts.legs}</TableCell>
														<TableCell className={TABULAR_NUMS}>{counts.fixtures}</TableCell>
														<TableCell className={TABULAR_NUMS}>{counts.staked.toFixed(2)}</TableCell>
														<TableCell className={TABULAR_NUMS}>{counts.profit.toFixed(2)}</TableCell>
													</TableRow>
												))}
											</TableBody>
										</Table>
									</div>
									<p className="mt-2 text-xs text-muted-foreground">
										另有 {data.unpurchased_open} 条未购买在途建议（不计入分母）；200
										注门槛的唯一分母是唯一注，腿数不凑。
									</p>
								</section>

								<section aria-labelledby="validation-caliber-heading">
									<h3 id="validation-caliber-heading" className="mb-2 text-sm font-medium">
										条件口径说明
									</h3>
									<div className="rounded-lg border border-border p-4 text-xs text-muted-foreground">
										<p>
											三条件 = CLV beat（≥60% @ ≥200 唯一注）、前瞻对市场 skill（≥0，≥30
											场）、复核无系统性错误；整赛季窗口覆盖独立验收。
											判定与目标值由服务端（evaluation/validation）给出，前端不自行判达标。
										</p>
										<p className="mt-2">
											回测指标（RPS / skill / DM）仅供诊断——回测可过拟合，口径上不算通过线；前瞻纳入规则见词条
											<GlossaryTerm id="forward-inclusion">"前瞻纳入"</GlossaryTerm>
											。术语与数字实例见词典页。
										</p>
									</div>
								</section>

								<section aria-labelledby="validation-runs-heading">
									<h3 id="validation-runs-heading" className="mb-2 text-sm font-medium">
										回测 run（新 → 旧；诊断用，不算通过线）
									</h3>
									{runs.isError ? (
										<p data-testid="runs-error" className="text-sm text-muted-foreground">
											回测 run 列表加载失败。
										</p>
									) : backtestRuns.length === 0 ? (
										<p className="text-sm text-muted-foreground">暂无回测 run。</p>
									) : (
										<BacktestRunTable runs={backtestRuns} />
									)}
								</section>
							</div>
						</details>
					</>
				) : null}
			</div>
		</AppShell>
	);
}
