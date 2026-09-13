import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { type Bet, fetchBets, importDrawResults, recordPurchase, runSettlement } from "../api/goalx";
import { AppShell } from "../components/app-shell";

const STATUS_LABELS: Record<string, string> = {
	open: "未结",
	won: "胜",
	lost: "负",
	void: "退款",
	partial: "部分",
};

const SELECTION_LABELS: Record<string, string> = {
	h: "主胜",
	d: "平",
	a: "客胜",
};

function legText(bet: Bet): string {
	return bet.legs
		.map(
			(leg) =>
				`#${leg.fixture_id} ${leg.market_code === "hhad" ? `让球${leg.goal_line ?? "?"} ` : ""}${
					SELECTION_LABELS[leg.selection_code] ?? leg.selection_code
				}@${leg.locked_odds.toFixed(2)}`,
		)
		.join(" × ");
}

export function BetsPage() {
	const queryClient = useQueryClient();
	const [selected, setSelected] = useState<number[]>([]);
	const [drawForm, setDrawForm] = useState({ fixtureId: "", homeGoals: "", awayGoals: "" });
	const [message, setMessage] = useState<string | null>(null);

	const bets = useQuery({ queryKey: ["bets"], queryFn: () => fetchBets() });

	const purchase = useMutation({
		mutationFn: () => recordPurchase({ bet_ids: selected }),
		onSuccess: (slip) => {
			setMessage(`已回录票 #${slip.id}（${slip.bet_count} 注，合计 ¥${slip.stake_total}）`);
			setSelected([]);
			void queryClient.invalidateQueries({ queryKey: ["bets"] });
		},
		onError: (error) => setMessage(`回录失败：${String(error)}`),
	});

	const settle = useMutation({
		mutationFn: () => runSettlement(),
		onSuccess: (stats) => {
			setMessage(
				`结算完成：${stats.settled} 注落定（胜 ${stats.won} / 负 ${stats.lost} / 退款 ${stats.void}），${stats.still_open} 注待开`,
			);
			void queryClient.invalidateQueries({ queryKey: ["bets"] });
		},
		onError: () => setMessage("结算失败：后端不可用"),
	});

	const importResult = useMutation({
		mutationFn: () =>
			importDrawResults({
				source: "manual",
				results: [
					{
						fixture_id: Number(drawForm.fixtureId),
						home_goals: Number(drawForm.homeGoals),
						away_goals: Number(drawForm.awayGoals),
						void: false,
					},
				],
			}),
		onSuccess: (result) => {
			setMessage(`已导入 ${result.imported} 条开奖结果`);
			setDrawForm({ fixtureId: "", homeGoals: "", awayGoals: "" });
		},
		onError: (error) => setMessage(`导入失败：${String(error)}`),
	});

	function toggleSelection(betId: number, enabled: boolean) {
		setSelected((prev) => (enabled ? [...prev, betId] : prev.filter((id) => id !== betId)));
	}

	const suggestions = bets.data?.filter((bet) => !bet.purchased) ?? [];

	return (
		<AppShell title="投注">
			<p className="mb-4 text-sm text-neutral-500">
				注级建议（含未购标记）→ 勾选实际购买子集做票级回录 → 开奖导入 → 结算批跑 → 复盘列表。
			</p>

			{message ? (
				<p data-testid="bets-message" className="mb-4 rounded-md bg-neutral-100 px-3 py-2 text-sm">
					{message}
				</p>
			) : null}

			<section className="mb-6 flex flex-wrap items-end gap-3">
				<button
					type="button"
					className="rounded-md bg-neutral-900 px-3 py-1.5 text-sm text-white disabled:opacity-40"
					disabled={selected.length === 0 || purchase.isPending}
					onClick={() => purchase.mutate()}
				>
					回录选中的 {selected.length} 注为一张票
				</button>
				<button
					type="button"
					className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm disabled:opacity-40"
					disabled={settle.isPending}
					onClick={() => settle.mutate()}
				>
					结算批跑
				</button>
			</section>

			{bets.isPending ? <p data-testid="bets-loading">加载投注记录…</p> : null}
			{bets.isError ? (
				<p data-testid="bets-error" className="text-sm text-neutral-500">
					后端不可用 — 用 <code>task server</code> 启动 API。
				</p>
			) : null}

			{bets.data ? (
				<table className="w-full text-sm">
					<thead>
						<tr className="border-b border-neutral-200 text-left text-xs text-neutral-500">
							<th className="px-2 py-2">回录</th>
							<th className="px-2 py-2">#</th>
							<th className="px-2 py-2">模式</th>
							<th className="px-2 py-2">内容</th>
							<th className="px-2 py-2">注金</th>
							<th className="px-2 py-2">状态</th>
							<th className="px-2 py-2">盈亏</th>
						</tr>
					</thead>
					<tbody className="divide-y divide-neutral-100">
						{bets.data.map((bet) => (
							<tr key={bet.id} data-testid="bet-row">
								<td className="px-2 py-1.5">
									{bet.purchased ? (
										<span className="text-xs text-neutral-400">已购</span>
									) : (
										<input
											type="checkbox"
											aria-label={`回录注 ${bet.id}`}
											checked={selected.includes(bet.id)}
											onChange={(event) => toggleSelection(bet.id, event.target.checked)}
										/>
									)}
								</td>
								<td className="px-2 py-1.5">{bet.id}</td>
								<td className="px-2 py-1.5">{bet.mode === "live" ? "真金" : "纸面"}</td>
								<td className="px-2 py-1.5">{legText(bet)}</td>
								<td className="px-2 py-1.5 tabular-nums">¥{bet.stake.toFixed(2)}</td>
								<td className="px-2 py-1.5">{STATUS_LABELS[bet.status] ?? bet.status}</td>
								<td className="px-2 py-1.5 tabular-nums">
									{bet.profit === null ? "—" : `${bet.profit >= 0 ? "+" : ""}${bet.profit.toFixed(2)}`}
								</td>
							</tr>
						))}
					</tbody>
				</table>
			) : null}
			{suggestions.length === 0 && bets.data ? <p className="mt-3 text-sm text-neutral-500">无未购建议。</p> : null}

			<section className="mt-8 rounded-lg border border-neutral-200 p-4">
				<h2 className="mb-3 text-sm font-semibold">导入开奖结果（官方口径，唯一事实源）</h2>
				<form
					className="flex flex-wrap items-end gap-3 text-sm"
					onSubmit={(event) => {
						event.preventDefault();
						importResult.mutate();
					}}
				>
					<label className="flex flex-col gap-1">
						<span className="text-xs text-neutral-500">fixture id</span>
						<input
							required
							data-testid="draw-fixture"
							className="w-24 rounded border border-neutral-300 px-2 py-1"
							value={drawForm.fixtureId}
							onChange={(event) => setDrawForm({ ...drawForm, fixtureId: event.target.value })}
						/>
					</label>
					<label className="flex flex-col gap-1">
						<span className="text-xs text-neutral-500">主队进球</span>
						<input
							required
							type="number"
							min={0}
							data-testid="draw-home"
							className="w-20 rounded border border-neutral-300 px-2 py-1"
							value={drawForm.homeGoals}
							onChange={(event) => setDrawForm({ ...drawForm, homeGoals: event.target.value })}
						/>
					</label>
					<label className="flex flex-col gap-1">
						<span className="text-xs text-neutral-500">客队进球</span>
						<input
							required
							type="number"
							min={0}
							data-testid="draw-away"
							className="w-20 rounded border border-neutral-300 px-2 py-1"
							value={drawForm.awayGoals}
							onChange={(event) => setDrawForm({ ...drawForm, awayGoals: event.target.value })}
						/>
					</label>
					<button
						type="submit"
						className="rounded-md bg-neutral-900 px-3 py-1.5 text-white disabled:opacity-40"
						disabled={importResult.isPending}
					>
						导入
					</button>
				</form>
			</section>
		</AppShell>
	);
}
