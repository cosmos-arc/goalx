import { useQuery } from "@tanstack/react-query";
import { fetchBankroll } from "../api/goalx";
import { AppShell } from "../components/app-shell";

const KIND_LABELS: Record<string, string> = {
	deposit: "入金",
	withdraw: "出金",
	bet_stake: "投注",
	bet_payout: "兑付",
	cost: "成本",
};

export function BankrollPage() {
	const bankroll = useQuery({ queryKey: ["bankroll"], queryFn: () => fetchBankroll() });

	return (
		<AppShell title="资金">
			<p className="mb-4 text-sm text-neutral-500">
				Bankroll 只受真金（live）模式影响；纸面记录不进入资金池。M1 展示余额与流水，Kelly 建议/熔断横幅在 M4 落地。
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
					<table className="w-full text-sm">
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
				</>
			) : null}
		</AppShell>
	);
}
