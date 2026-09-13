import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { createBet, fetchTodayFixtures, type HadQuoteStatus, type TodayFixture } from "../api/goalx";
import { AppShell } from "../components/app-shell";

const FLAG_LABELS: Record<string, string> = {
	ev_deviation: "EV 偏差≥5%(非机会)",
	not_joined: "未 join 欧赔",
	few_books: "样本少",
};

const REASON_LABELS: Record<string, string> = {
	sale_stopped: "已停售",
	kickoff_passed: "已开赛",
	stale_source: "报价过期",
	jc_three_way_incomplete: "竞彩三向不全",
	jc_no_quote: "无竞彩报价",
	sale_status_unknown: "销售状态未知",
	sale_status_unknown_value: "销售状态未知",
	single_eligibility_unknown: "单固资格未知",
	jc_observed_at_unknown: "观测时点未知",
	jc_source_time_unknown: "源时间未知",
	eu_no_quote: "无欧赔",
	eu_no_valid_books: "无有效欧赔",
	eu_observed_at_unknown: "欧赔观测未知",
};

const SALE_LABELS: Record<string, string> = {
	on_sale: "在售",
	stopped: "停售",
	unknown: "未知",
};

const SELECTION_LABELS: Record<string, string> = { h: "主胜", d: "平", a: "客胜" };

type PickedLeg = {
	fixture_id: number;
	match_code: string;
	selection_code: string;
	odds: number;
};

function formatLocal(utc: string): string {
	const date = new Date(utc);
	if (Number.isNaN(date.getTime())) {
		return utc;
	}
	return `${String(date.getMonth() + 1).padStart(2, "0")}/${String(date.getDate()).padStart(2, "0")} ${String(
		date.getHours(),
	).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function oddsText(value: number | null | undefined): string {
	return value === null || value === undefined ? "—" : value.toFixed(2);
}

function probText(value: number | null | undefined): string {
	return value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;
}

function evText(value: number | null | undefined): string {
	return value === null || value === undefined ? "—" : `${value >= 0 ? "+" : ""}${(value * 100).toFixed(1)}%`;
}

function eligibilityCell(hadQuote: HadQuoteStatus | null | undefined, isSingle: boolean) {
	if (!hadQuote) {
		return <span className="text-xs text-neutral-400">无判定</span>;
	}
	const reasons = hadQuote.reasons ?? [];
	const verdict =
		hadQuote.status === "valid"
			? { label: "可投", tone: "bg-emerald-50 text-emerald-700" }
			: hadQuote.status === "rejected"
				? { label: "拒绝", tone: "bg-red-50 text-red-700" }
				: { label: "证据未知", tone: "bg-amber-50 text-amber-700" };
	return (
		<span className="flex flex-wrap items-center gap-1" data-testid={`had-quote-${hadQuote.status}`}>
			<span className={`rounded px-1.5 py-0.5 text-xs ${verdict.tone}`}>{verdict.label}</span>
			{hadQuote.sale_state ? (
				<span className="text-xs text-neutral-500">{SALE_LABELS[hadQuote.sale_state] ?? hadQuote.sale_state}</span>
			) : null}
			{hadQuote.single_eligible === true ? (
				<span className="rounded bg-neutral-100 px-1 text-xs text-neutral-600">单固</span>
			) : null}
			{reasons.length > 0 ? (
				<span className="text-xs text-red-600">
					{reasons.map((reason) => REASON_LABELS[reason] ?? reason).join("/")}
				</span>
			) : null}
			{hadQuote.status !== "rejected" && hadQuote.single_eligible === false && isSingle ? (
				<span className="text-xs text-neutral-500">仅串关</span>
			) : null}
		</span>
	);
}

export function TodayPage() {
	const today = useQuery({ queryKey: ["today"], queryFn: () => fetchTodayFixtures() });
	const queryClient = useQueryClient();
	const [picked, setPicked] = useState<PickedLeg[]>([]);
	const [stake, setStake] = useState("100");
	const [strategyVersion, setStrategyVersion] = useState("");
	const [mode, setMode] = useState<"paper" | "live">("paper");
	const [message, setMessage] = useState<string | null>(null);

	function togglePick(fixture: TodayFixture, selection: "h" | "d" | "a", odds: number) {
		setMessage(null);
		const already = picked.find((leg) => leg.fixture_id === fixture.fixture_id);
		if (already && already.selection_code === selection) {
			setPicked(picked.filter((leg) => leg.fixture_id !== fixture.fixture_id));
			return;
		}
		if (picked.length >= 2) {
			setMessage("首版仅支持单关与 2串1；先移除已选项");
			return;
		}
		if (already) {
			setMessage("一场比赛只能选一腿（竞彩禁止同场串关）");
			return;
		}
		const hadQuote = fixture.had_quote;
		if (picked.length === 1 && hadQuote?.status === "rejected") {
			setMessage(
				`${fixture.match_code} 不可作第二腿：${(hadQuote.reasons ?? []).map((r) => REASON_LABELS[r] ?? r).join("/")}`,
			);
			return;
		}
		if (picked.length === 0 && hadQuote && (hadQuote.single_eligible !== true || hadQuote.status === "rejected")) {
			setMessage(`${fixture.match_code} 非单固或不可投，只能作为串关腿（服务器会再校验）`);
		}
		setPicked([
			...picked,
			{
				fixture_id: fixture.fixture_id,
				match_code: fixture.match_code,
				selection_code: selection,
				odds,
			},
		]);
	}

	const createSuggestion = useMutation({
		mutationFn: () =>
			createBet({
				mode,
				stake: Number(stake),
				strategy_version: strategyVersion.trim() === "" ? null : strategyVersion.trim(),
				legs: picked.map((leg) => ({
					fixture_id: leg.fixture_id,
					market_code: "had",
					selection_code: leg.selection_code,
					locked_odds: leg.odds,
				})),
			}),
		onSuccess: (bet) => {
			setMessage(`已建建议 #${bet.id}（${picked.length === 1 ? "单关" : "2串1"}）— 去投注页锁定`);
			setPicked([]);
			void queryClient.invalidateQueries({ queryKey: ["bets"] });
		},
		onError: (error) => setMessage(`建注失败：${errorText(error)}`),
	});

	return (
		<AppShell title="今日">
			<p className="mb-4 text-sm text-neutral-500">
				竞彩场次对照：竞彩赔率 vs 欧洲共识（Shin 去晦）、EV 偏差标记（非机会）、had 资格判定（票 35
				共享证据）。点赔率加入选注（单关须单固，2串1 须不同场次且共同可购买；提交时服务器再校验）。
			</p>

			{message ? (
				<p data-testid="today-message" className="mb-4 rounded-md bg-neutral-100 px-3 py-2 text-sm">
					{message}
				</p>
			) : null}

			{today.isPending ? <p data-testid="today-loading">加载今日场次…</p> : null}
			{today.isError ? (
				<p data-testid="today-error" className="text-sm text-neutral-500">
					后端不可用或当日无数据 — 用 <code>task server</code> 启动 API，先跑一次
					<code>task ingest-jingcai</code>。
				</p>
			) : null}

			{today.data ? (
				today.data.length === 0 ? (
					<p data-testid="today-empty" className="text-sm text-neutral-500">
						当日无在售场次。
					</p>
				) : (
					<div className="overflow-x-auto">
						<table className="w-full text-sm">
							<thead>
								<tr className="border-b border-neutral-200 text-left text-xs text-neutral-500">
									<th className="px-2 py-2">时间(本地)</th>
									<th className="px-2 py-2">编号</th>
									<th className="px-2 py-2">联赛</th>
									<th className="px-2 py-2">主 vs 客</th>
									<th className="px-2 py-2">竞彩 H/D/A（点击选择）</th>
									<th className="px-2 py-2">欧共识 p</th>
									<th className="px-2 py-2">EV H/D/A</th>
									<th className="px-2 py-2">books</th>
									<th className="px-2 py-2">资格</th>
									<th className="px-2 py-2">标记</th>
								</tr>
							</thead>
							<tbody className="divide-y divide-neutral-100">
								{today.data.map((fixture) => {
									const evs = [fixture.ev?.h, fixture.ev?.d, fixture.ev?.a].filter(
										(value): value is number => value !== null && value !== undefined,
									);
									const maxAbsEv = evs.length > 0 ? Math.max(...evs.map(Math.abs)) : 0;
									const selected = (sel: "h" | "d" | "a") =>
										picked.some((leg) => leg.fixture_id === fixture.fixture_id && leg.selection_code === sel);
									return (
										<tr key={fixture.fixture_id} data-testid="today-row" className={fixture.joined ? "" : "opacity-60"}>
											<td className="px-2 py-1.5 text-neutral-500">{formatLocal(fixture.kickoff_utc)}</td>
											<td className="px-2 py-1.5">{fixture.match_code}</td>
											<td className="px-2 py-1.5">
												{fixture.competition}
												{fixture.tier === "tier1" ? (
													<span className="ml-1 rounded bg-amber-100 px-1 text-xs text-amber-800">T1</span>
												) : null}
											</td>
											<td className="px-2 py-1.5 font-medium">
												{fixture.home_team} vs {fixture.away_team}
											</td>
											<td className="px-2 py-1.5 tabular-nums">
												<span className="flex gap-1">
													{(["h", "d", "a"] as const).map((sel) => {
														const value = fixture.jc_odds[sel];
														const disabled = value === null || value === undefined;
														return (
															<button
																key={sel}
																type="button"
																data-testid={`pick-${fixture.fixture_id}-${sel}`}
																aria-label={`${fixture.match_code} ${SELECTION_LABELS[sel]} @${oddsText(value)}`}
																disabled={disabled}
																className={`rounded px-1.5 py-0.5 ${
																	selected(sel)
																		? "bg-neutral-900 text-white"
																		: disabled
																			? "text-neutral-300"
																			: "hover:bg-neutral-100"
																}`}
																onClick={() => value !== null && value !== undefined && togglePick(fixture, sel, value)}
															>
																{oddsText(value)}
															</button>
														);
													})}
												</span>
											</td>
											<td className="px-2 py-1.5 tabular-nums text-neutral-600">
												{fixture.eu_prob
													? `${probText(fixture.eu_prob.h)} / ${probText(fixture.eu_prob.d)} / ${probText(fixture.eu_prob.a)}`
													: "—"}
											</td>
											<td className="px-2 py-1.5 tabular-nums" data-testid="ev-cell">
												{fixture.ev ? (
													<span className={maxAbsEv >= 0.05 ? "font-semibold text-amber-700" : ""}>
														{evText(fixture.ev.h)} / {evText(fixture.ev.d)} / {evText(fixture.ev.a)}
													</span>
												) : (
													"—"
												)}
											</td>
											<td className="px-2 py-1.5 text-center tabular-nums">{fixture.books || "—"}</td>
											<td className="px-2 py-1.5">{eligibilityCell(fixture.had_quote, fixture.is_single)}</td>
											<td className="px-2 py-1.5">
												{(fixture.flags ?? []).length === 0 ? (
													"—"
												) : (
													<span className="flex flex-wrap gap-1">
														{(fixture.flags ?? []).map((flag) => (
															<span
																key={flag}
																data-testid={`flag-${flag}`}
																className="rounded bg-red-50 px-1.5 py-0.5 text-xs text-red-700"
															>
																{FLAG_LABELS[flag] ?? flag}
															</span>
														))}
													</span>
												)}
											</td>
										</tr>
									);
								})}
							</tbody>
						</table>
					</div>
				)
			) : null}

			<section className="mt-6 rounded-lg border border-neutral-200 p-4" data-testid="selection-basket">
				<h2 className="mb-3 text-sm font-semibold">选注（{picked.length}/2）</h2>
				{picked.length === 0 ? (
					<p className="text-sm text-neutral-500">未选择；点击上方赔率加入。无信号不建策略注，仅手工选择。</p>
				) : (
					<>
						<ul className="mb-3 space-y-1 text-sm">
							{picked.map((leg) => (
								<li key={leg.fixture_id} data-testid="picked-leg">
									{leg.match_code} #{leg.fixture_id} {SELECTION_LABELS[leg.selection_code]} @{leg.odds.toFixed(2)}
									<button
										type="button"
										className="ml-2 text-xs text-red-600"
										onClick={() => setPicked(picked.filter((item) => item.fixture_id !== leg.fixture_id))}
									>
										移除
									</button>
								</li>
							))}
						</ul>
						<form
							className="flex flex-wrap items-end gap-3 text-sm"
							onSubmit={(event) => {
								event.preventDefault();
								createSuggestion.mutate();
							}}
						>
							<label className="flex flex-col gap-1">
								<span className="text-xs text-neutral-500">模式</span>
								<select
									data-testid="basket-mode"
									className="rounded border border-neutral-300 px-2 py-1"
									value={mode}
									onChange={(event) => setMode(event.target.value === "live" ? "live" : "paper")}
								>
									<option value="paper">纸面 paper</option>
									<option value="live">真实 live</option>
								</select>
							</label>
							<label className="flex flex-col gap-1">
								<span className="text-xs text-neutral-500">金额(¥)</span>
								<input
									required
									type="number"
									min={1}
									step="0.01"
									data-testid="basket-stake"
									className="w-24 rounded border border-neutral-300 px-2 py-1"
									value={stake}
									onChange={(event) => setStake(event.target.value)}
								/>
							</label>
							<label className="flex flex-col gap-1">
								<span className="text-xs text-neutral-500">策略版本(可选)</span>
								<input
									data-testid="basket-strategy"
									placeholder="手动"
									className="w-32 rounded border border-neutral-300 px-2 py-1"
									value={strategyVersion}
									onChange={(event) => setStrategyVersion(event.target.value)}
								/>
							</label>
							<button
								type="submit"
								className="rounded-md bg-neutral-900 px-3 py-1.5 text-white disabled:opacity-40"
								disabled={createSuggestion.isPending || picked.length === 0}
							>
								建立建议
							</button>
							<Link to="/bets" className="text-sm text-neutral-600 underline">
								去投注页锁定 →
							</Link>
						</form>
					</>
				)}
			</section>
		</AppShell>
	);
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
