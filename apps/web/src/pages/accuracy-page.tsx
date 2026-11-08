import { useQuery } from "@tanstack/react-query";
import { type AccuracyReport, fetchAccuracyReport } from "../api/goalx";
import { AppShell } from "../components/app-shell";
import { EmptyState } from "../components/empty-state";
import { StatusToast } from "../components/status-toast";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { accuracyKey } from "../lib/query-keys";
import { TABULAR_NUMS } from "../lib/ui";

/**
 * auto-paper 票 02：前瞻准确率页 `/accuracy`（能力一·报告面）。
 *
 * 口径（prereg acc-v1，全部来自 /api/v1/accuracy/report 只读聚合）：
 * - 覆盖率首行与 forecast_coverage 同源（覆盖 n / 在售 n + skip 三因）；
 * - 期次(月)×玩法×联赛拆解，总体行（全部联赛）在最前；
 * - had/hhad=RPS，ttg/crs/hafu=Brier+log-loss；差值=模型−基准（负=好），
 *   双基准=JC 官方 SP 归一化 + 欧赔共识（票 01 快照同窗）；
 * - 判读规则冻结文字随报告展示：n≥1800 且 |差|>2SE 才可定论，未达标只报不判。
 */

/** prereg 冻结定论门槛（判读规则文字同源；改动=acc-v2 另起）。 */
const VERDICT_MIN_N = 1800;

const MARKET_LABELS: Record<string, string> = {
	had: "胜平负（RPS）",
	hhad: "让球胜平负（RPS，仅整数线）",
	ttg: "总进球（Brier/log-loss）",
	crs: "比分（Brier/log-loss）",
	hafu: "半全场（Brier/log-loss）",
};

const pct4 = (value: number | null | undefined): string => (value == null ? "—" : value.toFixed(4));

const diffText = (diff: number | null, se: number | null): string =>
	diff == null ? "—" : `${diff >= 0 ? "+" : ""}${diff.toFixed(4)}${se == null ? "" : ` ±${(2 * se).toFixed(4)}`}`;

export function AccuracyPage() {
	const reportQuery = useQuery({ queryKey: accuracyKey, queryFn: fetchAccuracyReport });

	return (
		<AppShell title="准确率">
			{reportQuery.isPending ? (
				<div data-testid="accuracy-loading" className="h-24 max-w-2xl animate-pulse rounded bg-muted" />
			) : null}
			{reportQuery.isError ? (
				<EmptyState
					variant="backend-unavailable"
					message="连不上后端，准确率报告读取失败。"
					hint={<>用 task server 启动 API 后重试。</>}
					action={{ label: "重试", onClick: () => void reportQuery.refetch() }}
				/>
			) : null}
			{reportQuery.data ? <ReportBody report={reportQuery.data} /> : null}
		</AppShell>
	);
}

function ReportBody({ report }: { report: AccuracyReport }) {
	return (
		<div className="space-y-6" data-testid="accuracy">
			<StatusToast testid="accuracy-reading-rule">{report.reading_rule}</StatusToast>
			{report.months.length === 0 ? (
				<EmptyState
					variant="no-data"
					message="尚无评分行——赛果落定后夜班结果链自动评分。"
					hint={<>首个月块在换栈后第一轮结果同步出现。</>}
				/>
			) : null}
			{report.months.map((block) => (
				<MonthSection key={block.coverage.month} block={block} />
			))}
		</div>
	);
}

function MonthSection({ block }: { block: AccuracyReport["months"][number] }) {
	const coverage = block.coverage;
	return (
		<section aria-labelledby={`accuracy-${coverage.month}`} data-testid={`accuracy-month-${coverage.month}`}>
			<h2 id={`accuracy-${coverage.month}`} className="mb-2 text-sm font-medium">
				{coverage.month} 期次{" "}
				<span className="text-xs font-normal text-muted-foreground">
					覆盖 {coverage.covered}/{coverage.total}（未覆盖：非FD联赛 {coverage.not_fd_league} · 无模型{" "}
					{coverage.no_model} · 无映射 {coverage.no_mapping} · 未知 {coverage.unknown}）
				</span>
			</h2>
			<Table>
				<TableHeader>
					<TableRow>
						<TableHead>玩法</TableHead>
						<TableHead>联赛</TableHead>
						<TableHead>n</TableHead>
						<TableHead>排除</TableHead>
						<TableHead>模型</TableHead>
						<TableHead>log-loss</TableHead>
						<TableHead>JC 基准</TableHead>
						<TableHead>欧赔基准</TableHead>
						<TableHead>差值 模型−JC</TableHead>
						<TableHead>差值 模型−欧</TableHead>
						<TableHead>定论门槛</TableHead>
						<TableHead>校准（箱:命中/置信）</TableHead>
					</TableRow>
				</TableHeader>
				<TableBody>
					{block.rows.map((row) => (
						<TableRow
							key={`${row.month}-${row.market}-${row.competition ?? "总体"}`}
							className={row.competition == null ? "font-medium" : undefined}
						>
							<TableCell>{MARKET_LABELS[row.market] ?? row.market}</TableCell>
							<TableCell>{row.competition ?? "总体"}</TableCell>
							<TableCell className={TABULAR_NUMS}>{String(row.n)}</TableCell>
							<TableCell className={TABULAR_NUMS}>{row.skipped === 0 ? "—" : String(row.skipped)}</TableCell>
							<TableCell className={TABULAR_NUMS}>{pct4(row.model_metric)}</TableCell>
							<TableCell className={TABULAR_NUMS}>{pct4(row.model_logloss)}</TableCell>
							<TableCell className={TABULAR_NUMS}>{pct4(row.jc_baseline)}</TableCell>
							<TableCell className={TABULAR_NUMS}>{pct4(row.eu_baseline)}</TableCell>
							<TableCell className={TABULAR_NUMS}>{diffText(row.diff_jc, row.diff_jc_se)}</TableCell>
							<TableCell className={TABULAR_NUMS}>{diffText(row.diff_eu, row.diff_eu_se)}</TableCell>
							<TableCell>
								{row.verdict_ready ? (
									<span className="rounded bg-warning/10 px-1">达标</span>
								) : (
									<span className="text-xs text-muted-foreground">
										{row.cum_n_baseline}/{VERDICT_MIN_N}
									</span>
								)}
							</TableCell>
							<TableCell className={`${TABULAR_NUMS} text-xs text-muted-foreground`}>
								{row.calibration.length === 0
									? "—"
									: row.calibration
											.map(
												(bin) =>
													`${bin.bin_index}:${(bin.hit_rate * 100).toFixed(0)}%/${(bin.mean_conf * 100).toFixed(0)}%`,
											)
											.join(" ")}
							</TableCell>
						</TableRow>
					))}
				</TableBody>
			</Table>
		</section>
	);
}
