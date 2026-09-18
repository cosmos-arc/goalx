import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { type BacktestRun, fetchBacktestRuns, fetchValidationProgress, type ValidationProgress } from "../api/goalx";
import { AppShell } from "../components/app-shell";
import type { EChartsOption } from "../components/charts/echarts";
import { useECharts } from "../components/charts/use-echarts";
import { EmptyState } from "../components/empty-state";
import { GlossaryTerm } from "../components/glossary-term";
import { Badge } from "../components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import type { GlossaryId } from "../lib/glossary";
import { TABULAR_NUMS } from "../lib/ui";

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
 * 数据源 = 现有端点（票 10 定稿零 contract 变更）：`GET /validation/progress`
 * 已内嵌 clv/forward 两份弱类型字典，其中 forward 与 `GET /validation/forward-skill`
 * 是同一份报告（forward_skill_report），不再重复请求。
 */

type ConditionProgress = ValidationProgress["conditions"][number];
type YieldPoint = ValidationProgress["yield_curve"][number];

// ---- 弱类型字典取值工具（票 34：clv/forward 为 unknown 字典，缺失一律回退） ----

function pick(source: unknown, ...keys: string[]): unknown {
	let cursor: unknown = source;
	for (const key of keys) {
		if (typeof cursor !== "object" || cursor === null) return undefined;
		cursor = (cursor as Record<string, unknown>)[key];
	}
	return cursor;
}

function asNumber(value: unknown): number | null {
	return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asString(value: unknown): string | null {
	return typeof value === "string" && value !== "" ? value : null;
}

function asBool(value: unknown): boolean | null {
	return typeof value === "boolean" ? value : null;
}

function formatPct(value: number | null, digits = 1): string {
	return value === null ? "—" : `${(value * 100).toFixed(digits)}%`;
}

function formatSigned(value: number | null, digits = 4): string {
	if (value === null) return "—";
	return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}`;
}

/** 概率域 CLV 转百分比带符号（+0.01 → +1.00%），与词典"持续 +0.5%~3%"同读法。 */
function formatSignedPct(value: number | null, digits = 2): string {
	if (value === null) return "—";
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

/** canvas 取不到 CSS 变量，option 构建时解析语义 token（与历史页同法，解析失败退回近似色）。 */
function cssVar(name: string, fallback: string): string {
	const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
	return raw === "" ? fallback : raw;
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

// ---- 弱类型指标映射（票 09 定稿：已知 key 映射为带标签指标行，未知 key 折叠） ----

export type MetricRow = {
	id: string;
	label: string;
	value: string;
	hint?: string;
	term?: GlossaryId;
};

export type MappedMetrics = {
	rows: MetricRow[];
	unknownEntries: Array<[string, unknown]>;
};

const CLV_KNOWN_KEYS = new Set([
	"singles",
	"parlay2",
	"independence_assumed",
	"close_basis_note",
	"by_close_basis",
	"denominator",
	"by_minutes_bucket_single",
	"regression",
]);
const FORWARD_KNOWN_KEYS = new Set(["rule", "track", "coverage", "groups"]);

function splitUnknown(dict: Record<string, unknown>, known: Set<string>): Array<[string, unknown]> {
	return Object.entries(dict).filter(([key]) => !known.has(key));
}

function groupRows(dict: Record<string, unknown>, key: "singles" | "parlay2", kind: string): MetricRow[] {
	const rows: MetricRow[] = [];
	const section = dict[key];
	if (typeof section !== "object" || section === null) {
		return rows;
	}
	for (const mode of ["paper", "live"] as const) {
		const stats = pick(section, mode);
		if (typeof stats !== "object" || stats === null) {
			continue;
		}
		const n = asNumber(pick(stats, "n_bets"));
		const beat = asNumber(pick(stats, "beat_rate"));
		const avg = asNumber(pick(stats, "avg_clv"));
		rows.push({
			id: `${key}-${mode}`,
			label: `${kind} · ${mode === "paper" ? "纸面" : "真金"} beat rate`,
			term: "clv",
			value: formatPct(beat),
			hint: `n=${n ?? "—"} 注 · 平均 CLV ${formatSignedPct(avg)}（判读：≥60% 为门槛，正 = 买在好价）`,
		});
	}
	return rows;
}

/** 基准来源分层的显示名（票 40：锚级顺序固定，未知级原样显示）。 */
const CLOSE_BASIS_LABELS: Record<string, string> = {
	pinnacle: "Pinnacle 主锚",
	betfair_ex: "Betfair 辅锚(扣佣)",
	consensus: "多书共识 fallback",
	legacy: "分层前共识(legacy)",
	mixed: "串关跨基准(mixed)",
};

/** by_close_basis 字典 → 一行分层摘要（窗口期新旧口径并行判读，票 40）。 */
function closeBasisRow(dict: Record<string, unknown>): MetricRow | null {
	const basisDict = dict["by_close_basis"];
	if (typeof basisDict !== "object" || basisDict === null) {
		return null;
	}
	const parts: string[] = [];
	for (const basis of ["pinnacle", "betfair_ex", "consensus", "legacy", "mixed"]) {
		const entry = pick(basisDict, basis);
		if (typeof entry !== "object" || entry === null) {
			continue;
		}
		const bets = asNumber(pick(entry, "bets"));
		const legs = asNumber(pick(entry, "legs"));
		const beat = asNumber(pick(entry, "groups", "single", "paper", "beat_rate"));
		const beatN = asNumber(pick(entry, "groups", "single", "paper", "n_bets"));
		const pieces = [
			bets !== null ? `${bets} 注` : null,
			legs !== null ? `${legs} 腿` : null,
			beat !== null && beatN ? `纸面单关 beat ${formatPct(beat)}(n=${beatN})` : null,
		].filter((piece): piece is string => piece !== null);
		if (pieces.length > 0) {
			parts.push(`${CLOSE_BASIS_LABELS[basis] ?? basis} ${pieces.join(" · ")}`);
		}
	}
	if (parts.length === 0) {
		return null;
	}
	return {
		id: "close-basis",
		label: "基准来源分层",
		term: "clv-basis",
		value: parts.join(" · "),
		hint: asString(dict["close_basis_note"]) ?? "pinnacle 主锚 → betfair_ex 辅 → consensus fallback",
	};
}

/** clv 字典 → 指标行；未映射 key 原样进 unknownEntries（其他指标折叠区）。 */
export function clvMetricRows(clv: unknown): MappedMetrics {
	if (typeof clv !== "object" || clv === null) {
		return { rows: [], unknownEntries: [] };
	}
	const dict = clv as Record<string, unknown>;
	const rows: MetricRow[] = [...groupRows(dict, "singles", "单关"), ...groupRows(dict, "parlay2", "2串1")];

	const basis = closeBasisRow(dict);
	if (basis !== null) {
		rows.push(basis);
	}

	const independence = asBool(dict["independence_assumed"]);
	if (independence !== null) {
		rows.push({
			id: "independence",
			label: "串关票级联合口径",
			value: independence ? "两腿独立连乘（假设已声明）" : "未声明独立性假设",
			hint: "beat 按票级联合概率判定；腿级 CLV 仅诊断。",
		});
	}

	const uniqueBets = asNumber(pick(dict, "denominator", "unique_bets"));
	if (uniqueBets !== null) {
		rows.push({
			id: "denominator",
			label: "样本分母（唯一注）",
			value: `${uniqueBets} 注`,
			hint: [
				`原始 ${asNumber(pick(dict, "denominator", "raw_bets")) ?? "—"}`,
				`重复去重 ${asNumber(pick(dict, "denominator", "deduped_duplicates")) ?? "—"}`,
				`缺收盘 ${asNumber(pick(dict, "denominator", "no_close_bets")) ?? "—"}`,
				`覆盖场次 ${asNumber(pick(dict, "denominator", "fixtures")) ?? "—"}`,
			].join(" · "),
		});
	}

	const buckets = dict["by_minutes_bucket_single"];
	if (typeof buckets === "object" && buckets !== null && Object.keys(buckets).length > 0) {
		const text = Object.entries(buckets as Record<string, unknown>)
			.map(
				([bucket, stats]) =>
					`${bucket} ${formatPct(asNumber(pick(stats, "beat_rate")), 0)}（n=${asNumber(pick(stats, "n")) ?? "—"}）`,
			)
			.join(" · ");
		rows.push({
			id: "minutes-buckets",
			label: "距开赛分桶 beat rate（单关）",
			value: text,
			hint: "下注越早通常 CLV 越高；分桶看买入时点质量。",
		});
	}

	const slope = asNumber(pick(dict, "regression", "slope"));
	if (slope !== null) {
		const rSquared = asNumber(pick(dict, "regression", "r_squared"));
		rows.push({
			id: "regression",
			label: "单关 CLV → 盈亏回归",
			value: `斜率 ${slope.toFixed(2)} · R²=${rSquared === null ? "—" : rSquared.toFixed(2)}`,
			hint: `n=${asNumber(pick(dict, "regression", "n")) ?? "—"}（仅单关；串关不作独立样本）`,
		});
	}

	return { rows, unknownEntries: splitUnknown(dict, CLV_KNOWN_KEYS) };
}

/** forward 字典 → 指标行（覆盖四态 + 分组 skill，排除全计不静默丢弃）。 */
export function forwardMetricRows(forward: unknown): MappedMetrics {
	if (typeof forward !== "object" || forward === null) {
		return { rows: [], unknownEntries: [] };
	}
	const dict = forward as Record<string, unknown>;
	const rows: MetricRow[] = [];

	const rule = asString(dict["rule"]);
	if (rule !== null) {
		rows.push({
			id: "rule",
			label: "纳入规则（防时间泄漏）",
			term: "forward-inclusion",
			value: rule,
			hint: "只计开球前发出的最新 Forecast；开球后补发一律排除。",
		});
	}

	const settled = asNumber(pick(dict, "coverage", "settled_fixtures"));
	if (settled !== null) {
		rows.push({
			id: "coverage",
			label: "前瞻覆盖（排除全计）",
			value: [
				`scored ${asNumber(pick(dict, "coverage", "scored")) ?? 0}`,
				`无预测 ${asNumber(pick(dict, "coverage", "no_forecast")) ?? 0}`,
				`仅赛后 ${asNumber(pick(dict, "coverage", "post_kickoff_only")) ?? 0}`,
				`无基准 ${asNumber(pick(dict, "coverage", "no_market_baseline")) ?? 0}`,
			].join(" · "),
			hint: `已结算 ${settled} 场；scored = 纳入计分，其余为排除分母（不静默丢弃）。`,
		});
	}

	const groups = dict["groups"];
	if (typeof groups === "object" && groups !== null) {
		for (const [version, metrics] of Object.entries(groups as Record<string, unknown>)) {
			if (typeof metrics !== "object" || metrics === null) {
				continue;
			}
			const insufficient = asBool(pick(metrics, "insufficient_samples")) === true;
			rows.push({
				id: `group-${version}`,
				label: `前瞻分组 ${version}`,
				term: "skill",
				value: `skill ${formatSigned(asNumber(pick(metrics, "skill_rps")))} · n=${asNumber(pick(metrics, "n")) ?? "—"}`,
				hint: insufficient
					? `样本不足（<30 场），不作通过依据 · RPS 模型 ${formatSigned(asNumber(pick(metrics, "rps_model")), 3)} vs 市场 ${formatSigned(asNumber(pick(metrics, "rps_market")), 3)}`
					: `RPS 模型 ${formatSigned(asNumber(pick(metrics, "rps_model")), 3)} vs 市场 ${formatSigned(asNumber(pick(metrics, "rps_market")), 3)}`,
			});
		}
	}

	return { rows, unknownEntries: splitUnknown(dict, FORWARD_KNOWN_KEYS) };
}

/** 达标判定与服务器同构（validation.py）：样本足的分组里取最好 skill；全样本不足则只报不足。 */
export function bestForwardSkill(forward: unknown): {
	skill: number | null;
	version: string | null;
	allInsufficient: boolean;
} {
	const groups = pick(forward, "groups");
	if (typeof groups !== "object" || groups === null) {
		return { skill: null, version: null, allInsufficient: false };
	}
	let best: { skill: number; version: string } | null = null;
	let anyGroup = false;
	for (const [version, metrics] of Object.entries(groups as Record<string, unknown>)) {
		if (asBool(pick(metrics, "insufficient_samples")) === true) {
			continue;
		}
		anyGroup = true;
		const skill = asNumber(pick(metrics, "skill_rps"));
		if (skill !== null && (best === null || skill > best.skill)) {
			best = { skill, version };
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

/** 未映射 key 的兜底（票 09：不裸字典直出——折叠呈现原始 JSON，不冒充已解读）。 */
function UnknownMetrics({ entries, label }: { entries: Array<[string, unknown]>; label: string }) {
	if (entries.length === 0) {
		return null;
	}
	return (
		<details data-testid="unknown-metrics" className="mt-3 rounded-md border border-dashed border-border px-3 py-2">
			<summary className="cursor-pointer text-xs text-muted-foreground">
				{label} · 其他指标（{entries.length} 项未映射字段）
			</summary>
			<pre className="mt-2 overflow-x-auto text-xs text-muted-foreground">
				{JSON.stringify(Object.fromEntries(entries), null, 2)}
			</pre>
		</details>
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

function signedTone(value: number | null): "neutral" | "positive" | "negative" {
	if (value === null || value === 0) return "neutral";
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
	const progress = useQuery({ queryKey: ["validation-progress"], queryFn: () => fetchValidationProgress() });
	const runs = useQuery({ queryKey: ["backtest-runs"], queryFn: () => fetchBacktestRuns() });

	const data = progress.data ?? null;
	const verdict = data ? validationVerdict(data.conditions) : null;
	const curve = data?.yield_curve ?? [];
	const latestRun = data?.latest_run ?? runs.data?.[0] ?? null;
	const metrics = latestRun?.overall_metrics ?? null;
	const backtestSkill = metrics ? asNumber(metrics["skill_rps"]) : null;
	const clv = data ? clvMetricRows(data.clv) : null;
	const forward = data ? forwardMetricRows(data.forward) : null;
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
									<p className={`text-xs text-muted-foreground ${TABULAR_NUMS}`}>目标 {verdict.fullSeason.target}</p>
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
									tone={signedTone(bestForward?.skill ?? null)}
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
										<MetricRowList rows={clv?.rows ?? []} testid="clv-rows" />
										<UnknownMetrics entries={clv?.unknownEntries ?? []} label="CLV" />
									</div>
								</section>

								<section aria-labelledby="validation-forward-heading">
									<h3 id="validation-forward-heading" className="mb-2 text-sm font-medium">
										前瞻评分明细（纳入规则 · 覆盖四态 · 分组 skill）
									</h3>
									<div className="rounded-lg border border-border p-4">
										<MetricRowList rows={forward?.rows ?? []} testid="forward-rows" />
										<UnknownMetrics entries={forward?.unknownEntries ?? []} label="前瞻" />
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
