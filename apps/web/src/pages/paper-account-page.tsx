import { useQuery } from "@tanstack/react-query";
import { fetchPaperAccount, type PaperAccount, type StrategyFace } from "../api/goalx";
import { AppShell } from "../components/app-shell";
import { EmptyState } from "../components/empty-state";
import { StatusToast } from "../components/status-toast";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { paperAccountKey } from "../lib/query-keys";
import { pnlClass, signedCny, TABULAR_NUMS } from "../lib/ui";

/**
 * auto-paper 票 04：纸面账户 P&L 页 `/paper-account`（能力二·账户面）。
 *
 * 口径（prereg B，全部来自 /api/v1/paper-account 只读聚合）：
 * - 账户=初始 ¥1000+Σ已结算 P&L；待结算浮仓单列（open 注+彩池未开奖），
 *   绝不计入已实现——「浮仓」块与「已实现」块物理分栏。
 * - auto-v1 与人工纸面分列（两行账户行+两条曲线要点），不混一个数字。
 * - EV 实现差（实现 ROI vs 决策 EV 均值）为主指标列；累计注数<200 时
 *   页首挂「样本不足，只报曲线不判盈亏」横幅（sample_sufficient=false）。
 * - 税后=逐注奖金计税口径（≤1 万免/超限全额 20%），与毛额并排展示。
 */

const STRATEGY_LABELS: Record<string, string> = {
	"auto-v1": "auto-v1（系统组合）",
	manual: "人工纸面",
};

/** 策略行的主指标格式化：缺数据（无结算/无 EV 快照）如实显 —，不冒充。 */
export function evGapText(face: StrategyFace): string {
	if (face.ev_realization_gap === null || face.ev_realization_gap === undefined) {
		return "—";
	}
	const pct = (face.ev_realization_gap * 100).toFixed(1);
	return `${Number(pct) >= 0 ? "+" : ""}${pct}%`;
}

export function PaperAccountPage() {
	const accountQuery = useQuery({ queryKey: paperAccountKey, queryFn: fetchPaperAccount });

	return (
		<AppShell title="纸面账户">
			{accountQuery.isPending ? (
				<div data-testid="paper-account-loading" className="h-24 max-w-2xl animate-pulse rounded bg-muted" />
			) : null}
			{accountQuery.isError ? (
				<EmptyState
					variant="backend-unavailable"
					message="连不上后端，纸面账户读取失败。"
					hint={<>用 task server 启动 API 后重试。</>}
					action={{ label: "重试", onClick: () => void accountQuery.refetch() }}
				/>
			) : null}
			{accountQuery.data ? <AccountBody face={accountQuery.data} /> : null}
		</AppShell>
	);
}

function AccountBody({ face }: { face: PaperAccount }) {
	return (
		<div className="space-y-6" data-testid="paper-account">
			{face.sample_sufficient ? null : (
				<StatusToast testid="paper-account-insufficient">
					样本不足（累计 {face.total_bets_n} 注 &lt; 200）——只报曲线，不判盈亏
				</StatusToast>
			)}
			<header className="grid gap-3 sm:grid-cols-4">
				<StatCard label="账户现值（已实现）" value={`¥${face.bankroll_now.toFixed(2)}`} />
				<StatCard
					label="已实现 P&L"
					value={signedCny(face.total_profit_settled)}
					className={pnlClass(face.total_profit_settled)}
				/>
				<StatCard
					label={`待结算浮仓（open ¥${face.floating_stake_fixed.toFixed(2)} + 池 ¥${face.floating_stake_pool.toFixed(2)}）`}
					value={`¥${(face.floating_stake_fixed + face.floating_stake_pool).toFixed(2)}`}
					className="text-info"
				/>
				<StatCard
					label="税后已实现（逐注奖金口径）"
					value={signedCny(face.profit_after_tax)}
					className={pnlClass(face.profit_after_tax)}
				/>
			</header>

			<section aria-labelledby="paper-account-strategies">
				<h2 id="paper-account-strategies" className="mb-2 text-sm font-medium">
					策略分列{" "}
					<span className="text-xs font-normal text-muted-foreground">
						初始 ¥{face.initial_bankroll.toFixed(0)} · 不混一个数字
					</span>
				</h2>
				<Table>
					<TableHeader>
						<TableRow>
							<TableHead>策略</TableHead>
							<TableHead>注数（已结算/open）</TableHead>
							<TableHead>已结算投注</TableHead>
							<TableHead>已实现 P&L</TableHead>
							<TableHead>实现 ROI</TableHead>
							<TableHead>决策 EV 均值</TableHead>
							<TableHead>EV 实现差（主指标）</TableHead>
							<TableHead>最大回撤</TableHead>
						</TableRow>
					</TableHeader>
					<TableBody>
						{face.strategies.map((row) => (
							<TableRow key={row.strategy} data-testid={`paper-account-${row.strategy}`}>
								<TableCell>{STRATEGY_LABELS[row.strategy] ?? row.strategy}</TableCell>
								<TableCell className={TABULAR_NUMS}>
									{row.bets_n}（{row.settled_n}/{row.open_n}）
								</TableCell>
								<TableCell className={TABULAR_NUMS}>¥{row.stake_settled.toFixed(2)}</TableCell>
								<TableCell className={`${TABULAR_NUMS} ${pnlClass(row.profit_settled)}`}>
									{signedCny(row.profit_settled)}
								</TableCell>
								<TableCell className={TABULAR_NUMS}>
									{row.realized_roi == null ? "—" : `${(row.realized_roi * 100).toFixed(1)}%`}
								</TableCell>
								<TableCell className={TABULAR_NUMS}>
									{row.mean_decision_ev == null ? "—" : `${(row.mean_decision_ev * 100).toFixed(1)}%`}
								</TableCell>
								<TableCell className={`${TABULAR_NUMS} font-medium`}>{evGapText(row)}</TableCell>
								<TableCell className={TABULAR_NUMS}>¥{row.max_drawdown.toFixed(2)}</TableCell>
							</TableRow>
						))}
					</TableBody>
				</Table>
			</section>

			<section aria-labelledby="paper-account-curve">
				<h2 id="paper-account-curve" className="mb-2 text-sm font-medium">
					累计曲线（按结算日）
				</h2>
				{face.strategies.every((row) => row.curve.length === 0) ? (
					<p className="text-sm text-muted-foreground" data-testid="paper-account-curve-empty">
						尚无已结算注——账户停在初始 ¥{face.initial_bankroll.toFixed(0)}。
					</p>
				) : (
					<ul className="space-y-3">
						{face.strategies.map((row) =>
							row.curve.length === 0 ? null : (
								<li key={row.strategy} data-testid={`paper-account-curve-${row.strategy}`}>
									<span className="text-xs text-muted-foreground">
										{STRATEGY_LABELS[row.strategy] ?? row.strategy}：
									</span>
									<span className={`${TABULAR_NUMS} ml-2 text-sm`}>
										{row.curve.map((point) => `${point.date} ¥${point.value.toFixed(0)}`).join(" → ")}
									</span>
								</li>
							),
						)}
					</ul>
				)}
			</section>

			<section aria-labelledby="paper-account-markets">
				<h2 id="paper-account-markets" className="mb-2 text-sm font-medium">
					分玩法盈亏（已结算）
				</h2>
				<Table>
					<TableHeader>
						<TableRow>
							<TableHead>玩法</TableHead>
							<TableHead>已结算注数</TableHead>
							<TableHead>P&L</TableHead>
						</TableRow>
					</TableHeader>
					<TableBody>
						{face.per_market.map((row) => (
							<TableRow key={row.market}>
								<TableCell>{row.market}</TableCell>
								<TableCell className={TABULAR_NUMS}>{String(row.bets_n)}</TableCell>
								<TableCell className={`${TABULAR_NUMS} ${pnlClass(row.profit)}`}>{signedCny(row.profit)}</TableCell>
							</TableRow>
						))}
					</TableBody>
				</Table>
				{face.tax_total > 0 ? (
					<p className="mt-2 text-xs text-muted-foreground" data-testid="paper-account-tax-note">
						税后口径：中奖注奖金超 ¥10,000 全额 20% 计税，本账已计税 ¥{face.tax_total.toFixed(2)}。
					</p>
				) : null}
			</section>
		</div>
	);
}

function StatCard({ label, value, className = "" }: { label: string; value: string; className?: string }) {
	return (
		<div className="rounded-lg border border-border bg-card p-3">
			<p className="text-xs text-muted-foreground">{label}</p>
			<p className={`mt-1 text-xl font-semibold ${TABULAR_NUMS} ${className}`}>{value}</p>
		</div>
	);
}
