import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
	type Bet,
	fetchBets,
	fetchDrawResults,
	fetchTodayFixtures,
	importDrawResults,
	previewDrawResults,
	recordPurchase,
	runSettlement,
} from "../api/goalx";
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

const FORWARD_LABELS: Record<string, string> = {
	included: "前瞻纳入",
	excluded_unlocked: "未锁定, 排除",
	excluded_post_kickoff: "锁定晚于开赛, 排除",
	live_separate: "live 单独分组",
	missing_closing: "缺 closing",
	unknown: "资格未知",
};

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

function reviewText(bet: Bet): string {
	return bet.review ? (FORWARD_LABELS[bet.review.forward] ?? bet.review.forward) : "—";
}

function profitText(bet: Bet): string {
	return bet.profit === null ? "—" : `${bet.profit >= 0 ? "+" : ""}${bet.profit.toFixed(2)}`;
}

function errorText(error: unknown): string {
	if (typeof error === "object" && error !== null && "detail" in error) {
		const detail = (error as { detail: unknown }).detail;
		if (typeof detail === "string") {
			return detail;
		}
		return JSON.stringify(detail);
	}
	return String(error);
}

function BetTable({ bets, children }: { bets: Bet[]; children?: (bet: Bet) => React.ReactNode }) {
	return (
		<table className="w-full text-sm">
			<thead>
				<tr className="border-b border-neutral-200 text-left text-xs text-neutral-500">
					<th className="px-2 py-2">#</th>
					<th className="px-2 py-2">模式</th>
					<th className="px-2 py-2">内容</th>
					<th className="px-2 py-2">注金(建议→实际)</th>
					<th className="px-2 py-2">状态</th>
					<th className="px-2 py-2">盈亏</th>
					<th className="px-2 py-2">锁定时点</th>
					<th className="px-2 py-2">复盘资格</th>
					{children ? <th className="px-2 py-2">操作</th> : null}
				</tr>
			</thead>
			<tbody className="divide-y divide-neutral-100">
				{bets.map((bet) => (
					<tr key={bet.id} data-testid="bet-row">
						<td className="px-2 py-1.5">{bet.id}</td>
						<td className="px-2 py-1.5">{bet.mode === "live" ? "真金" : "纸面"}</td>
						<td className="px-2 py-1.5">{legText(bet)}</td>
						<td className="px-2 py-1.5 tabular-nums">{stakeText(bet)}</td>
						<td className="px-2 py-1.5">{STATUS_LABELS[bet.status] ?? bet.status}</td>
						<td className="px-2 py-1.5 tabular-nums">{profitText(bet)}</td>
						<td className="px-2 py-1.5 text-neutral-500">
							{bet.locked_at ? bet.locked_at.slice(5, 16).replace("T", " ") : "—"}
						</td>
						<td className="px-2 py-1.5 text-xs text-neutral-600">{reviewText(bet)}</td>
						{children ? <td className="px-2 py-1.5">{children(bet)}</td> : null}
					</tr>
				))}
			</tbody>
		</table>
	);
}

type DrawForm = {
	fixtureId: string;
	homeGoals: string;
	awayGoals: string;
	isVoid: boolean;
	voidReason: string;
	correctionReason: string;
};

export function BetsPage() {
	const queryClient = useQueryClient();
	const [message, setMessage] = useState<string | null>(null);
	const [actualFor, setActualFor] = useState<number | null>(null);
	const [actualForm, setActualForm] = useState<{
		stake: string;
		oddsByFixture: Record<number, string>;
		placedAt: string;
	}>({
		stake: "",
		oddsByFixture: {},
		placedAt: "",
	});
	const [drawForm, setDrawForm] = useState<DrawForm>({
		fixtureId: "",
		homeGoals: "",
		awayGoals: "",
		isVoid: false,
		voidReason: "",
		correctionReason: "",
	});
	const [preview, setPreview] = useState<Awaited<ReturnType<typeof previewDrawResults>> | null>(null);

	const bets = useQuery({ queryKey: ["bets"], queryFn: () => fetchBets() });
	const drawResults = useQuery({ queryKey: ["draw-results"], queryFn: () => fetchDrawResults() });
	const today = useQuery({ queryKey: ["today"], queryFn: () => fetchTodayFixtures() });

	function refresh() {
		void queryClient.invalidateQueries({ queryKey: ["bets"] });
		void queryClient.invalidateQueries({ queryKey: ["draw-results"] });
		void queryClient.invalidateQueries({ queryKey: ["bankroll"] });
		void queryClient.invalidateQueries({ queryKey: ["validation-progress"] });
	}

	const lockPaper = useMutation({
		mutationFn: (betId: number) => recordPurchase({ bet_ids: [betId] }),
		onSuccess: (slip) => {
			setMessage(`已锁定纸面票 #${slip.id}（¥${slip.stake_total}，不产生真金流水）`);
			refresh();
		},
		onError: (error) => setMessage(`锁定失败：${errorText(error)}`),
	});

	const recordLive = useMutation({
		mutationFn: (betId: number) => {
			const bet = (bets.data ?? []).find((row) => row.id === betId);
			const legOdds = (bet?.legs ?? []).flatMap((leg) => {
				const raw = actualForm.oddsByFixture[leg.fixture_id]?.trim();
				return raw === undefined || raw === "" ? [] : [{ fixture_id: leg.fixture_id, odds: Number(raw) }];
			});
			return recordPurchase({
				bet_ids: [betId],
				placed_at: actualForm.placedAt.trim() === "" ? null : actualForm.placedAt.trim(),
				actuals: {
					[String(betId)]: { stake: Number(actualForm.stake), leg_odds: legOdds },
				},
			});
		},
		onSuccess: (slip) => {
			setMessage(`已回录真实购买票 #${slip.id}（按实际条款记账）`);
			setActualFor(null);
			refresh();
		},
		onError: (error) => setMessage(`回录失败：${errorText(error)}`),
	});

	const settle = useMutation({
		mutationFn: () => runSettlement(),
		onSuccess: (stats) => {
			setMessage(
				`结算完成：${stats.settled} 注落定（胜 ${stats.won} / 负 ${stats.lost} / 退款 ${stats.void}），${stats.still_open} 注待开`,
			);
			refresh();
		},
		onError: (error) => setMessage(`结算失败：${errorText(error)}`),
	});

	function drawPayload() {
		return {
			source: "manual",
			results: [
				{
					fixture_id: Number(drawForm.fixtureId),
					home_goals: Number(drawForm.homeGoals),
					away_goals: Number(drawForm.awayGoals),
					void: drawForm.isVoid,
					void_reason: drawForm.isVoid && drawForm.voidReason.trim() !== "" ? drawForm.voidReason.trim() : null,
					correction_reason: drawForm.correctionReason.trim() === "" ? null : drawForm.correctionReason.trim(),
				},
			],
		};
	}

	const doPreview = useMutation({
		mutationFn: () => previewDrawResults(drawPayload()),
		onSuccess: (data) => {
			setPreview(data);
			setMessage(null);
		},
		onError: (error) => setMessage(`预览失败：${errorText(error)}`),
	});

	const importResult = useMutation({
		mutationFn: () => importDrawResults(drawPayload()),
		onSuccess: (result) => {
			setMessage(`已导入 ${result.imported} 条开奖结果（更正已原子重算与冲正）`);
			setPreview(null);
			setDrawForm({
				fixtureId: "",
				homeGoals: "",
				awayGoals: "",
				isVoid: false,
				voidReason: "",
				correctionReason: "",
			});
			refresh();
		},
		onError: (error) => setMessage(`导入失败：${errorText(error)}`),
	});

	const suggestions = bets.data?.filter((bet) => !bet.purchased) ?? [];
	const lockedPaper = bets.data?.filter((bet) => bet.purchased && bet.mode === "paper") ?? [];
	const liveRecords = bets.data?.filter((bet) => bet.purchased && bet.mode === "live") ?? [];

	const fixtureOptions = (today.data ?? []).map((fixture) => ({
		value: fixture.fixture_id,
		label: `${fixture.match_code} ${fixture.home_team} vs ${fixture.away_team}`,
	}));
	const openBetFixtureIds: number[] = Array.from(
		new Set(
			(bets.data ?? []).filter((bet) => bet.status === "open").flatMap((bet) => bet.legs.map((leg) => leg.fixture_id)),
		),
	).filter((fixtureId) => !fixtureOptions.some((option) => option.value === fixtureId));
	const openBetFixtures = openBetFixtureIds.map((fixtureId) => ({
		value: fixtureId,
		label: `#${fixtureId}（未在今日列表）`,
	}));

	const existing = drawResults.data?.find((row) => row.fixture_id === Number(drawForm.fixtureId));

	function openActualForm(bet: Bet) {
		setActualFor(bet.id);
		setActualForm({ stake: String(bet.stake), oddsByFixture: {}, placedAt: "" });
	}

	return (
		<AppShell title="投注">
			<p className="mb-4 text-sm text-neutral-500">
				建议（未锁定）→ 纸面锁定 / 真实回录（实际条款，建议快照保留）→ 开奖导入（支持无效/更正与影响预览）→ 结算 →
				复盘。纸面收益是模拟收益，不进真金资金池。
			</p>

			{message ? (
				<p data-testid="bets-message" className="mb-4 rounded-md bg-neutral-100 px-3 py-2 text-sm">
					{message}
				</p>
			) : null}

			<section className="mb-6 flex flex-wrap items-end gap-3">
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

			<section className="mb-8" data-testid="section-suggestions">
				<h2 className="mb-2 text-sm font-semibold">未锁定建议（{suggestions.length}）</h2>
				{suggestions.length === 0 ? (
					<p className="text-sm text-neutral-500">无未锁定建议。</p>
				) : (
					<BetTable bets={suggestions}>
						{(bet) => (
							<span className="flex flex-wrap items-center gap-2">
								{bet.mode === "paper" ? (
									<button
										type="button"
										data-testid={`lock-${bet.id}`}
										className="rounded bg-neutral-900 px-2 py-1 text-xs text-white disabled:opacity-40"
										disabled={lockPaper.isPending}
										onClick={() => lockPaper.mutate(bet.id)}
									>
										锁定纸面
									</button>
								) : (
									<button
										type="button"
										data-testid={`open-actual-${bet.id}`}
										className="rounded border border-neutral-300 px-2 py-1 text-xs"
										onClick={() => openActualForm(bet)}
									>
										回录实际条款
									</button>
								)}
								{actualFor === bet.id ? (
									<span className="flex flex-wrap items-center gap-1 text-xs" data-testid={`actual-form-${bet.id}`}>
										<input
											aria-label="实际金额"
											type="number"
											min={1}
											step="0.01"
											className="w-20 rounded border border-neutral-300 px-1 py-0.5"
											value={actualForm.stake}
											onChange={(event) => setActualForm({ ...actualForm, stake: event.target.value })}
										/>
										{bet.legs.map((leg) => (
											<span key={leg.fixture_id} className="flex items-center gap-1">
												<span className="text-neutral-500">#{leg.fixture_id}</span>
												<input
													aria-label={`fixture ${leg.fixture_id} 实际赔率`}
													type="number"
													min={1.01}
													step="0.01"
													placeholder={leg.locked_odds.toFixed(2)}
													className="w-20 rounded border border-neutral-300 px-1 py-0.5"
													value={actualForm.oddsByFixture[leg.fixture_id] ?? ""}
													onChange={(event) =>
														setActualForm({
															...actualForm,
															oddsByFixture: {
																...actualForm.oddsByFixture,
																[leg.fixture_id]: event.target.value,
															},
														})
													}
												/>
											</span>
										))}
										<button
											type="button"
											className="rounded bg-neutral-900 px-2 py-0.5 text-white"
											onClick={() => recordLive.mutate(bet.id)}
										>
											提交
										</button>
									</span>
								) : null}
							</span>
						)}
					</BetTable>
				)}
			</section>

			<section className="mb-8" data-testid="section-locked-paper">
				<h2 className="mb-2 text-sm font-semibold">已锁定纸面（{lockedPaper.length}）</h2>
				{lockedPaper.length === 0 ? (
					<p className="text-sm text-neutral-500">无已锁定纸面注。</p>
				) : (
					<BetTable bets={lockedPaper} />
				)}
			</section>

			<section className="mb-8" data-testid="section-live">
				<h2 className="mb-2 text-sm font-semibold">真实回录（{liveRecords.length}，单独分组）</h2>
				{liveRecords.length === 0 ? (
					<p className="text-sm text-neutral-500">无真实回录。</p>
				) : (
					<BetTable bets={liveRecords} />
				)}
			</section>

			<section className="mt-8 rounded-lg border border-neutral-200 p-4">
				<h2 className="mb-3 text-sm font-semibold">按比赛录入开奖（官方口径，唯一事实源）</h2>
				<form
					className="flex flex-wrap items-end gap-3 text-sm"
					onSubmit={(event) => {
						event.preventDefault();
						importResult.mutate();
					}}
				>
					<label className="flex flex-col gap-1">
						<span className="text-xs text-neutral-500">场次</span>
						<select
							required
							data-testid="draw-fixture"
							className="w-56 rounded border border-neutral-300 px-2 py-1"
							value={drawForm.fixtureId}
							onChange={(event) => setDrawForm({ ...drawForm, fixtureId: event.target.value })}
						>
							<option value="">选择场次…</option>
							{[...fixtureOptions, ...openBetFixtures].map((option) => (
								<option key={option.value} value={option.value}>
									{option.label}
								</option>
							))}
						</select>
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
					<label className="flex items-center gap-1 pb-1">
						<input
							type="checkbox"
							data-testid="draw-void"
							checked={drawForm.isVoid}
							onChange={(event) => setDrawForm({ ...drawForm, isVoid: event.target.checked })}
						/>
						<span className="text-xs text-neutral-600">无效场次</span>
					</label>
					{drawForm.isVoid ? (
						<label className="flex flex-col gap-1">
							<span className="text-xs text-neutral-500">无效原因</span>
							<input
								data-testid="draw-void-reason"
								className="w-32 rounded border border-neutral-300 px-2 py-1"
								value={drawForm.voidReason}
								onChange={(event) => setDrawForm({ ...drawForm, voidReason: event.target.value })}
							/>
						</label>
					) : null}
					{existing ? (
						<label className="flex flex-col gap-1">
							<span className="text-xs text-neutral-500">
								更正原因（已有 {existing.home_goals}:{existing.away_goals}
								{existing.void ? " 无效" : ""}，必填）
							</span>
							<input
								required
								data-testid="draw-correction-reason"
								className="w-44 rounded border border-neutral-300 px-2 py-1"
								value={drawForm.correctionReason}
								onChange={(event) => setDrawForm({ ...drawForm, correctionReason: event.target.value })}
							/>
						</label>
					) : null}
					<button
						type="button"
						data-testid="draw-preview"
						className="rounded-md border border-neutral-300 px-3 py-1.5 disabled:opacity-40"
						disabled={doPreview.isPending || drawForm.fixtureId === ""}
						onClick={() => doPreview.mutate()}
					>
						影响预览
					</button>
					<button
						type="submit"
						data-testid="draw-import"
						className="rounded-md bg-neutral-900 px-3 py-1.5 text-white disabled:opacity-40"
						disabled={importResult.isPending}
					>
						导入
					</button>
				</form>

				{preview ? (
					<div
						className="mt-4 rounded border border-neutral-200 bg-neutral-50 p-3 text-sm"
						data-testid="draw-preview-result"
					>
						<p className="mb-2 font-medium">
							预览（只读，导入才执行重算与冲正）：
							{preview.results.map((change) => (
								<span key={change.fixture_id} className="ml-2 text-neutral-600">
									#{change.fixture_id}
									{change.is_correction
										? ` 更正 ${change.previous?.["home_goals"]}:${change.previous?.["away_goals"]} → ${change.replacement["home_goals"]}:${change.replacement["away_goals"]}`
										: " 首次导入"}
								</span>
							))}
						</p>
						{preview.affected_bets.length === 0 ? (
							<p className="text-neutral-500">无受影响注。</p>
						) : (
							<table className="w-full text-xs">
								<thead>
									<tr className="text-left text-neutral-500">
										<th className="px-2 py-1">注</th>
										<th className="px-2 py-1">当前</th>
										<th className="px-2 py-1">投影</th>
										<th className="px-2 py-1">兑付差</th>
									</tr>
								</thead>
								<tbody>
									{preview.affected_bets.map((bet) => (
										<tr key={bet.bet_id} data-testid="preview-affected-row">
											<td className="px-2 py-1">#{bet.bet_id}</td>
											<td className="px-2 py-1">
												{bet.status_current}
												{bet.payout_current === null ? "" : ` ¥${bet.payout_current.toFixed(2)}`}
											</td>
											<td className="px-2 py-1">
												{bet.status_projected}
												{bet.payout_projected === null ? "" : ` ¥${bet.payout_projected.toFixed(2)}`}
											</td>
											<td className="px-2 py-1 tabular-nums">
												{bet.delta_payout === null
													? "—"
													: `${bet.delta_payout >= 0 ? "+" : ""}${bet.delta_payout.toFixed(2)}`}
											</td>
										</tr>
									))}
								</tbody>
							</table>
						)}
					</div>
				) : null}
			</section>
		</AppShell>
	);
}
