import { useQuery } from "@tanstack/react-query";
import { fetchBacktestRuns, fetchValidationProgress } from "../api/goalx";
import { AppShell } from "../components/app-shell";

function asNumber(value: unknown): number | null {
	return typeof value === "number" ? value : null;
}

/** 从 unknown 的嵌套结构里按路径取值(clv/forward 为弱类型字典,票 34)。 */
function pick(source: unknown, ...keys: string[]): unknown {
	let cursor: unknown = source;
	for (const key of keys) {
		if (typeof cursor !== "object" || cursor === null) return undefined;
		cursor = (cursor as Record<string, unknown>)[key];
	}
	return cursor;
}

function formatPct(value: number | null | undefined, digits = 1): string {
	if (value === null || value === undefined) return "—";
	return `${(value * 100).toFixed(digits)}%`;
}

function formatSigned(value: number | null | undefined, digits = 4): string {
	if (value === null || value === undefined) return "—";
	return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}`;
}

function MetricCard({ label, value, hint }: { label: string; value: string; hint?: string }) {
	return (
		<div className="rounded-lg border border-neutral-200 bg-white p-4">
			<p className="text-xs text-neutral-500">{label}</p>
			<p data-testid={`metric-${label}`} className="mt-1 text-xl font-semibold tabular-nums">
				{value}
			</p>
			{hint ? <p className="mt-1 text-xs text-neutral-400">{hint}</p> : null}
		</div>
	);
}

function ConditionBar({
	achieved,
	label,
	current,
	target,
}: {
	achieved: boolean;
	label: string;
	current: string;
	target: string;
}) {
	return (
		<div className="rounded-lg border border-neutral-200 bg-white p-4" data-testid="condition-row">
			<div className="flex items-center justify-between">
				<p className="text-sm font-medium">{label}</p>
				<span
					data-testid="condition-status"
					className={`rounded-full px-2 py-0.5 text-xs ${
						achieved ? "bg-emerald-100 text-emerald-700" : "bg-neutral-100 text-neutral-500"
					}`}
				>
					{achieved ? "达成" : "进行中"}
				</span>
			</div>
			<div className="mt-2 h-1.5 w-full rounded-full bg-neutral-100">
				<div className={`h-1.5 rounded-full ${achieved ? "w-full bg-emerald-500" : "w-1/3 bg-amber-400"}`} />
			</div>
			<p className="mt-2 text-xs text-neutral-500">
				当前 {current} · 目标 {target}
			</p>
		</div>
	);
}

/** 滚动/累计 yield 双口径曲线（轻量内联 SVG，无图表依赖）。 */
function YieldCurve({
	points,
}: {
	points: { index: number; cumulative_yield: number; rolling_yield?: number | null }[];
}) {
	if (points.length < 2) {
		return (
			<p data-testid="yield-empty" className="text-sm text-neutral-500">
				已结算注数不足 2，曲线待积累。
			</p>
		);
	}
	const width = 640;
	const height = 160;
	const padding = 8;
	const values = points.flatMap((p) => [p.cumulative_yield, p.rolling_yield ?? p.cumulative_yield]);
	const min = Math.min(...values, 0);
	const max = Math.max(...values, 0);
	const span = max - min || 1;
	const toPath = (key: "cumulative_yield" | "rolling_yield") =>
		points
			.map((point, i) => {
				const value = point[key] ?? point.cumulative_yield;
				const x = padding + (i / (points.length - 1)) * (width - padding * 2);
				const y = height - padding - ((value - min) / span) * (height - padding * 2);
				return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
			})
			.join(" ");
	const zeroY = height - padding - ((0 - min) / span) * (height - padding * 2);
	return (
		<svg
			data-testid="yield-curve"
			viewBox={`0 0 ${width} ${height}`}
			className="w-full"
			role="img"
			aria-label="yield 曲线"
		>
			<line x1={padding} x2={width - padding} y1={zeroY} y2={zeroY} stroke="#d4d4d4" strokeDasharray="4 3" />
			<path d={toPath("cumulative_yield")} fill="none" stroke="#171717" strokeWidth="1.8" />
			<path d={toPath("rolling_yield")} fill="none" stroke="#f59e0b" strokeWidth="1.8" />
		</svg>
	);
}

export function ValidationPage() {
	const progress = useQuery({ queryKey: ["validation-progress"], queryFn: () => fetchValidationProgress() });
	const runs = useQuery({ queryKey: ["backtest-runs"], queryFn: () => fetchBacktestRuns() });

	const latest = progress.data?.latest_run ?? runs.data?.[0] ?? null;
	const metrics = latest?.overall_metrics ?? null;
	const clv = progress.data?.clv;
	// 票 34:单关/2串1、paper/live 分开看,不混成一个数
	const clvPaperSingles = asNumber(pick(clv, "singles", "paper", "beat_rate"));
	const clvPaperParlay = asNumber(pick(clv, "parlay2", "paper", "beat_rate"));
	const clvUniqueBets = asNumber(pick(clv, "denominator", "unique_bets")) ?? 0;
	const forwardScored = asNumber(pick(progress.data?.forward, "coverage", "scored")) ?? 0;

	return (
		<AppShell title="验证">
			<p className="mb-4 text-sm text-neutral-500">
				纸面期验证：对市场 skill、CLV beat rate 与滚动 yield（滚动 100 注 + 累计双口径）。回测引擎口径见 ADR 0007。
			</p>
			{progress.isError || runs.isError ? (
				<p data-testid="validation-error" className="mb-4 text-sm text-neutral-500">
					后端不可用 — 用 <code>task server</code> 启动 API。
				</p>
			) : null}
			{progress.isPending ? <p data-testid="validation-loading">加载验证进度…</p> : null}
			{progress.data ? (
				<section className="mb-6 grid gap-3 md:grid-cols-3">
					{progress.data.conditions.map((condition) => (
						<ConditionBar
							key={condition.key}
							achieved={condition.achieved}
							label={condition.label}
							current={condition.current}
							target={condition.target}
						/>
					))}
				</section>
			) : null}
			{metrics ? (
				<section className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
					<MetricCard
						label="回测 RPS 模型"
						value={asNumber(metrics["rps_model"])?.toFixed(4) ?? "—"}
						hint={`市场 ${asNumber(metrics["rps_market"])?.toFixed(4) ?? "—"} (n=${asNumber(metrics["n"]) ?? "—"})`}
					/>
					<MetricCard
						label="回测 skill"
						value={formatSigned(asNumber(metrics["skill_rps"]))}
						hint={`DM p=${asNumber(metrics["dm_p"])?.toFixed(3) ?? "—"}(探索性)`}
					/>
					<MetricCard label="前瞻样本" value={`${forwardScored} 场`} hint="赛前冻结 Forecast × 同期市场基准(票 34)" />
					<MetricCard
						label="CLV beat(纸面)"
						value={formatPct(clvPaperSingles)}
						hint={`单关 ${formatPct(clvPaperSingles)} · 2串1 ${formatPct(clvPaperParlay)} · 唯一 ${clvUniqueBets} 注`}
					/>
				</section>
			) : null}
			{progress.data ? (
				<section className="rounded-lg border border-neutral-200 bg-white p-4">
					<h2 className="mb-2 text-sm font-semibold">
						滚动 yield（琥珀 = 滚动 100 注，黑 = 累计；唯一纸面注，mode={progress.data.yield_curve_mode}）
					</h2>
					<YieldCurve points={progress.data.yield_curve} />
				</section>
			) : null}
			{runs.data && runs.data.length > 0 ? (
				<section className="mt-6">
					<h2 className="mb-2 text-sm font-semibold">回测 run</h2>
					<table className="w-full text-sm">
						<thead>
							<tr className="border-b border-neutral-200 text-left text-xs text-neutral-500">
								<th className="px-2 py-2">ID</th>
								<th className="px-2 py-2">标签</th>
								<th className="px-2 py-2">状态</th>
								<th className="px-2 py-2">场数</th>
								<th className="px-2 py-2">注数</th>
								<th className="px-2 py-2">ROI</th>
							</tr>
						</thead>
						<tbody className="divide-y divide-neutral-100">
							{runs.data.map((run) => (
								<tr key={run.id} data-testid="backtest-run-row">
									<td className="px-2 py-1.5 tabular-nums">{run.id}</td>
									<td className="px-2 py-1.5">{run.label}</td>
									<td className="px-2 py-1.5">{run.status}</td>
									<td className="px-2 py-1.5 tabular-nums">{asNumber(run.summary?.["predictions"]) ?? "—"}</td>
									<td className="px-2 py-1.5 tabular-nums">{asNumber(run.summary?.["bets"]) ?? "—"}</td>
									<td className="px-2 py-1.5 tabular-nums">{formatPct(asNumber(run.summary?.["roi"]))}</td>
								</tr>
							))}
						</tbody>
					</table>
				</section>
			) : null}
		</AppShell>
	);
}
