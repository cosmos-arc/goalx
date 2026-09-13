import { useQuery } from "@tanstack/react-query";
import { fetchBankroll, fetchCostSummary } from "../api/goalx";
import { AppShell } from "../components/app-shell";

const KIND_LABELS: Record<string, string> = {
	deposit: "入金",
	withdraw: "出金",
	bet_stake: "投注",
	bet_payout: "兑付",
	cost: "成本",
};

const CATEGORY_LABELS: Record<string, string> = {
	odds_api_credit: "The Odds API credits",
	llm_api: "LLM 调用",
	data_subscription: "数据订阅",
};

export function BankrollPage() {
	const bankroll = useQuery({ queryKey: ["bankroll"], queryFn: () => fetchBankroll() });
	const costs = useQuery({ queryKey: ["costs"], queryFn: () => fetchCostSummary() });

	return (
		<AppShell title="资金">
			<p className="mb-4 text-sm text-neutral-500">
				Bankroll 只受真金（live）模式影响；纸面收益是模拟收益，不进入本页。纸面与真实流水相互隔离。
			</p>
			{bankroll.isPending ? <p data-testid="bankroll-loading">加载资金状态…</p> : null}
			{bankroll.isError ? (
				<p data-testid="bankroll-error" className="text-sm text-neutral-500">
					后端不可用 — 用 <code>task server</code> 启动 API。
				</p>
			) : null}
			{bankroll.data ? (
				<>
					<p data-testid="bankroll-balance" className="mb-4 text-2xl font-semibold tabular-nums">
						{bankroll.data.balance === null ? "尚未入金" : `¥${bankroll.data.balance.toFixed(2)}`}
					</p>
					{bankroll.data.events.length === 0 ? (
						<p data-testid="bankroll-empty" className="mb-6 text-sm text-neutral-500">
							无真金流水（纸面锁定不产生资金变动）。
						</p>
					) : (
						<table className="mb-8 w-full text-sm">
							<thead>
								<tr className="border-b border-neutral-200 text-left text-xs text-neutral-500">
									<th className="px-2 py-2">时间</th>
									<th className="px-2 py-2">类型</th>
									<th className="px-2 py-2">金额</th>
									<th className="px-2 py-2">余额</th>
								</tr>
							</thead>
							<tbody className="divide-y divide-neutral-100">
								{bankroll.data.events.map((event) => (
									<tr key={event.id} data-testid="bankroll-event">
										<td className="px-2 py-1.5 text-neutral-500">{event.occurred_at.slice(0, 16)}</td>
										<td className="px-2 py-1.5">{KIND_LABELS[event.kind] ?? event.kind}</td>
										<td className="px-2 py-1.5 tabular-nums">
											{event.amount_cny >= 0 ? "+" : ""}
											{event.amount_cny.toFixed(2)}
										</td>
										<td className="px-2 py-1.5 tabular-nums">{event.balance_after.toFixed(2)}</td>
									</tr>
								))}
							</tbody>
						</table>
					)}
				</>
			) : null}

			<section className="rounded-lg border border-neutral-200 p-4" data-testid="cost-summary">
				<h2 className="mb-2 text-sm font-semibold">期间成本摘要（金额与 credits 分列）</h2>
				<p className="mb-3 text-xs text-neutral-500">仅统计已记账成本；未记录的成本标缺失，不视为总成本已覆盖。</p>
				{costs.isError ? (
					<p data-testid="cost-error" className="text-sm text-neutral-500">
						成本摘要不可用。
					</p>
				) : costs.data ? (
					<div className="flex flex-wrap gap-6 text-sm">
						<p data-testid="cost-total" className="tabular-nums">
							已记账金额：<span className="font-medium">¥{costs.data.total_cny.toFixed(2)}</span>
						</p>
						<p data-testid="cost-credits" className="tabular-nums">
							API credits：<span className="font-medium">{costs.data.credits_used}</span>
						</p>
					</div>
				) : null}
				{costs.data && costs.data.items.length > 0 ? (
					<table className="mt-3 w-full text-sm">
						<thead>
							<tr className="border-b border-neutral-200 text-left text-xs text-neutral-500">
								<th className="px-2 py-2">类别</th>
								<th className="px-2 py-2">units/credits</th>
								<th className="px-2 py-2">金额(¥)</th>
								<th className="px-2 py-2">笔数</th>
							</tr>
						</thead>
						<tbody className="divide-y divide-neutral-100">
							{costs.data.items.map((item) => (
								<tr key={item.category} data-testid="cost-item">
									<td className="px-2 py-1.5">{CATEGORY_LABELS[item.category] ?? item.category}</td>
									<td className="px-2 py-1.5 tabular-nums">{item.units}</td>
									<td className="px-2 py-1.5 tabular-nums">{item.amount_cny.toFixed(2)}</td>
									<td className="px-2 py-1.5 tabular-nums">{item.entries}</td>
								</tr>
							))}
						</tbody>
					</table>
				) : (
					<p data-testid="cost-missing" className="mt-3 text-sm text-neutral-500">
						暂无已记账成本（未知成本不在此冒充已覆盖）。
					</p>
				)}
			</section>
		</AppShell>
	);
}
