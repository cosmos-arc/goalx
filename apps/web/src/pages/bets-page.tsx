import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
	type Bet,
	fetchBets,
	fetchDrawResults,
	fetchSlips,
	fetchTodayFixtures,
	importDrawResults,
	previewDrawResults,
	recordPurchase,
	runSettlement,
} from "../api/goalx";
import { AppShell } from "../components/app-shell";
import { EmptyState } from "../components/empty-state";
import { Badge } from "../components/ui/badge";
import {
	Drawer,
	DrawerClose,
	DrawerContent,
	DrawerDescription,
	DrawerFooter,
	DrawerHeader,
	DrawerTitle,
} from "../components/ui/drawer";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { errorText, SELECTION_LABELS, TABULAR_NUMS } from "../lib/ui";

/**
 * 票 16：投注生命周期落地（票 06 定稿 = 唯一事实源）。
 * 增强分组表三段（未锁定建议 / 已锁定[纸面+真金回录，行内徽章区分] / 已结算），
 * 组头计数+待办提示、行内状态徽章（胜=profit 红、负=loss 绿，与红涨绿跌一致；
 * 底纹徽章文字用前景色——票 15 对比度结论）、时间倒序。
 * 真实回录 = 右侧 Drawer（实际金额/逐腿赔率/时点 + 提交前"建议快照 vs 实际条款"对照，
 * 金额≠建议时提示按实际条款记账）。赛果区按 06 预裁决 = 同步优先：同步面板本票为
 * 降级态（自动同步端点随票 10 排期，后端源属 goalx-quant 地图），人工录入降级为
 * 兜底/更正通道（更正必填原因 + 影响预览，ADR-0001 冲正/重算语义不变）。
 * 规则前置补充：锁定时对应场次开赛 <5 分钟先警示，不阻止——警示后再次点击继续。
 * 零 contract 变更；服务器校验仍为唯一权威。
 */

const STATUS_META: Record<string, { label: string; className: string }> = {
	open: { label: "未结", className: "bg-muted text-muted-foreground" },
	won: { label: "胜", className: "bg-profit/10 text-foreground" },
	lost: { label: "负", className: "bg-loss/10 text-foreground" },
	void: { label: "退款", className: "bg-muted text-muted-foreground" },
	partial: { label: "部分", className: "bg-warning/10 text-foreground" },
};

const FORWARD_LABELS: Record<string, string> = {
	included: "前瞻纳入",
	excluded_unlocked: "未锁定, 排除",
	excluded_post_kickoff: "锁定晚于开赛, 排除",
	live_separate: "live 单独分组",
	missing_closing: "缺 closing",
	unknown: "资格未知",
};

/** 盈亏数字永远只按正负红绿（票 02 钱层编码，正负号为主承载）。 */
function pnlClass(value: number | null): string {
	if (value === null || value === 0) {
		return "text-muted-foreground";
	}
	return value > 0 ? "text-profit" : "text-loss";
}

function ModeBadge({ mode }: { mode: string }) {
	return mode === "live" ? (
		<Badge className="bg-profit/10 text-foreground" data-testid="mode-live">
			真金
		</Badge>
	) : (
		<Badge variant="outline" className="text-muted-foreground" data-testid="mode-paper">
			纸面
		</Badge>
	);
}

function StatusBadge({ status }: { status: string }) {
	const meta = STATUS_META[status];
	if (!meta) {
		return <span className="text-xs text-muted-foreground">{status}</span>;
	}
	return (
		<Badge variant="outline" className={`border-transparent ${meta.className}`}>
			{meta.label}
		</Badge>
	);
}

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

function shortTime(iso: string | null): string {
	return iso ? iso.slice(5, 16).replace("T", " ") : "—";
}

/** 各段的"时间"取该阶段事实时点，倒序即最近动作在前。 */
function sortByDesc(rows: Bet[], pick: (bet: Bet) => string | null): Bet[] {
	const at = (bet: Bet): number => {
		const parsed = new Date(pick(bet) ?? "").getTime();
		return Number.isNaN(parsed) ? 0 : parsed;
	};
	return [...rows].sort((a, b) => at(b) - at(a));
}

function BetTable({
	rows,
	timePick,
	actions,
	emptyText,
}: {
	rows: Bet[];
	timePick: (bet: Bet) => string | null;
	/** 操作列（仅未锁定建议段有）：渲染在行尾。 */
	actions?: (bet: Bet) => React.ReactNode;
	emptyText: string;
}) {
	if (rows.length === 0) {
		return <p className="text-sm text-muted-foreground">{emptyText}</p>;
	}
	return (
		<div className="overflow-x-auto rounded-lg border border-border">
			<Table data-density="compact">
				<TableHeader>
					<TableRow>
						<TableHead>#</TableHead>
						<TableHead>模式</TableHead>
						<TableHead>内容</TableHead>
						<TableHead>注金(建议→实际)</TableHead>
						<TableHead>状态</TableHead>
						<TableHead>盈亏</TableHead>
						<TableHead>时间</TableHead>
						<TableHead>复盘资格</TableHead>
						{actions ? <TableHead>操作</TableHead> : null}
					</TableRow>
				</TableHeader>
				<TableBody>
					{rows.map((bet) => (
						<TableRow key={bet.id} data-testid="bet-row">
							<TableCell className={TABULAR_NUMS}>{bet.id}</TableCell>
							<TableCell>
								<ModeBadge mode={bet.mode} />
							</TableCell>
							<TableCell className="whitespace-nowrap">{legText(bet)}</TableCell>
							<TableCell className={`${TABULAR_NUMS} whitespace-nowrap`}>{stakeText(bet)}</TableCell>
							<TableCell>
								<StatusBadge status={bet.status} />
							</TableCell>
							<TableCell className={`${TABULAR_NUMS} whitespace-nowrap ${pnlClass(bet.profit)}`}>
								{bet.profit === null ? "—" : `${bet.profit >= 0 ? "+" : ""}${bet.profit.toFixed(2)}`}
							</TableCell>
							<TableCell className={`${TABULAR_NUMS} whitespace-nowrap text-muted-foreground`}>
								{shortTime(timePick(bet))}
							</TableCell>
							<TableCell className="text-xs whitespace-nowrap text-muted-foreground">{reviewText(bet)}</TableCell>
							{actions ? <TableCell className="whitespace-nowrap">{actions(bet)}</TableCell> : null}
						</TableRow>
					))}
				</TableBody>
			</Table>
		</div>
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

const EMPTY_DRAW_FORM: DrawForm = {
	fixtureId: "",
	homeGoals: "",
	awayGoals: "",
	isVoid: false,
	voidReason: "",
	correctionReason: "",
};

function localClock(utc: string): string {
	const date = new Date(utc);
	if (Number.isNaN(date.getTime())) {
		return "—";
	}
	return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

export function BetsPage() {
	const queryClient = useQueryClient();
	const [message, setMessage] = useState<string | null>(null);
	// 已警示过的建议（票 06 规则前置：开赛 <5 分钟先警示，不阻止——再次点击继续）
	const [warnedBets, setWarnedBets] = useState<ReadonlySet<number>>(new Set());
	// 真实回录 Drawer（票 06 定稿：右侧抽屉，不用模态/行展开）
	const [actualFor, setActualFor] = useState<number | null>(null);
	const [actualForm, setActualForm] = useState<{
		stake: string;
		oddsByFixture: Record<number, string>;
		placedAt: string;
	}>({ stake: "", oddsByFixture: {}, placedAt: "" });
	const [drawForm, setDrawForm] = useState<DrawForm>(EMPTY_DRAW_FORM);
	const [preview, setPreview] = useState<Awaited<ReturnType<typeof previewDrawResults>> | null>(null);

	const bets = useQuery({ queryKey: ["bets"], queryFn: () => fetchBets() });
	const drawResults = useQuery({ queryKey: ["draw-results"], queryFn: () => fetchDrawResults() });
	const today = useQuery({ queryKey: ["today"], queryFn: () => fetchTodayFixtures() });
	const slips = useQuery({ queryKey: ["slips"], queryFn: () => fetchSlips() });
	const now = Date.now();

	const allBets = bets.data ?? [];
	const kickoffById = new Map((today.data ?? []).map((fixture) => [fixture.fixture_id, fixture.kickoff_utc]));

	function refresh() {
		void queryClient.invalidateQueries({ queryKey: ["bets"] });
		void queryClient.invalidateQueries({ queryKey: ["draw-results"] });
		void queryClient.invalidateQueries({ queryKey: ["bankroll"] });
		void queryClient.invalidateQueries({ queryKey: ["validation-progress"] });
		void queryClient.invalidateQueries({ queryKey: ["slips"] });
	}

	// ---- 生命周期三段（票 06 定稿：未锁定建议 / 已锁定 / 已结算），各段倒序 ----
	const suggestions = sortByDesc(
		allBets.filter((bet) => !bet.purchased),
		(bet) => bet.created_at,
	);
	const locked = sortByDesc(
		allBets.filter((bet) => bet.purchased && bet.settled_at === null),
		(bet) => bet.locked_at ?? bet.created_at,
	);
	const settled = sortByDesc(
		allBets.filter((bet) => bet.purchased && bet.settled_at !== null),
		(bet) => bet.settled_at ?? bet.locked_at ?? bet.created_at,
	);

	// ---- 规则前置（票 06 补充）：锁定时段最早开赛 <5 分钟（或已开赛）先警示 ----
	function kickoffWarning(bet: Bet): string | null {
		let earliest: number | null = null;
		for (const leg of bet.legs) {
			const iso = kickoffById.get(leg.fixture_id);
			if (!iso) {
				continue;
			}
			const at = new Date(iso).getTime();
			if (!Number.isNaN(at) && (earliest === null || at < earliest)) {
				earliest = at;
			}
		}
		if (earliest === null) {
			return null;
		}
		const minutes = Math.round((earliest - now) / 60_000);
		if (minutes >= 5) {
			return null;
		}
		return minutes <= 0
			? `建议 #${bet.id} 含已开赛场次——锁定后复盘将按"锁定晚于开赛"排除。再次点击确认锁定。`
			: `建议 #${bet.id} 含场次将在 ${minutes} 分钟后开赛（不足 5 分钟）——锁定后可能失去前瞻复盘资格。再次点击确认锁定。`;
	}

	const lockPaper = useMutation({
		mutationFn: (betId: number) => recordPurchase({ bet_ids: [betId] }),
		onSuccess: (slip) => {
			setMessage(`已锁定纸面票 #${slip.id}（¥${slip.stake_total}，不产生真金流水）`);
			refresh();
		},
		onError: (error) => setMessage(`锁定失败：${errorText(error)}`),
	});

	function handleLock(bet: Bet) {
		const warning = kickoffWarning(bet);
		if (warning && !warnedBets.has(bet.id)) {
			setWarnedBets(new Set(warnedBets).add(bet.id));
			setMessage(warning);
			return;
		}
		lockPaper.mutate(bet.id);
	}

	// ---- 真实回录 Drawer（票 06：实际金额/逐腿赔率/时点 + 快照对照） ----
	const actualBet = actualFor === null ? null : (allBets.find((bet) => bet.id === actualFor) ?? null);
	const actualStakeValue = Number(actualForm.stake);
	const actualStakeValid = actualForm.stake.trim() !== "" && !Number.isNaN(actualStakeValue) && actualStakeValue > 0;

	const recordLive = useMutation({
		mutationFn: (input: {
			betId: number;
			stake: number;
			legOdds: Array<{ fixture_id: number; odds: number }>;
			placedAt: string | null;
		}) =>
			recordPurchase({
				bet_ids: [input.betId],
				placed_at: input.placedAt,
				actuals: { [String(input.betId)]: { stake: input.stake, leg_odds: input.legOdds } },
			}),
		onSuccess: (slip) => {
			setMessage(`已回录真实购买票 #${slip.id}（按实际条款记账，建议快照保留）`);
			setActualFor(null);
			refresh();
		},
		onError: (error) => setMessage(`回录失败：${errorText(error)}`),
	});

	function openActualForm(bet: Bet) {
		setActualFor(bet.id);
		setActualForm({ stake: String(bet.stake), oddsByFixture: {}, placedAt: "" });
	}

	function submitActual() {
		if (!actualBet || !actualStakeValid) {
			return;
		}
		const legOdds = actualBet.legs.flatMap((leg) => {
			const raw = actualForm.oddsByFixture[leg.fixture_id]?.trim();
			return raw === undefined || raw === "" ? [] : [{ fixture_id: leg.fixture_id, odds: Number(raw) }];
		});
		const rawPlaced = actualForm.placedAt.trim();
		const parsed = rawPlaced === "" ? null : new Date(rawPlaced);
		recordLive.mutate({
			betId: actualBet.id,
			stake: actualStakeValue,
			legOdds,
			placedAt: parsed && !Number.isNaN(parsed.getTime()) ? parsed.toISOString() : null,
		});
	}

	// ---- 赛果同步面板（06 预裁决 = 同步优先；本票降级态 + 人工兜底通道） ----
	const resultIds = new Set((drawResults.data ?? []).map((row) => row.fixture_id));
	const openFixtureIds = new Set(
		allBets.filter((bet) => bet.status === "open").flatMap((bet) => bet.legs.map((leg) => leg.fixture_id)),
	);
	const pendingFixtures = (today.data ?? []).filter((fixture) => {
		const kickoff = new Date(fixture.kickoff_utc).getTime();
		return !Number.isNaN(kickoff) && kickoff <= now && !resultIds.has(fixture.fixture_id);
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
			setDrawForm(EMPTY_DRAW_FORM);
			refresh();
		},
		onError: (error) => setMessage(`导入失败：${errorText(error)}`),
	});

	const fixtureOptions = (today.data ?? []).map((fixture) => ({
		value: fixture.fixture_id,
		label: `${fixture.match_code} ${fixture.home_team} vs ${fixture.away_team}`,
	}));
	const openBetFixtureIds: number[] = Array.from(
		new Set(allBets.filter((bet) => bet.status === "open").flatMap((bet) => bet.legs.map((leg) => leg.fixture_id))),
	).filter((fixtureId) => !fixtureOptions.some((option) => option.value === fixtureId));
	const openBetFixtures = openBetFixtureIds.map((fixtureId) => ({
		value: fixtureId,
		label: `#${fixtureId}（未在今日列表）`,
	}));

	const existing = drawResults.data?.find((row) => row.fixture_id === Number(drawForm.fixtureId));
	const suggestionHint = suggestions.some((bet) => bet.mode === "live")
		? "真金建议回录实际条款后入账；纸面锁定不产生真金流水"
		: "纸面锁定不产生真金流水；锁定后进入复盘样本";

	return (
		<AppShell title="投注">
			<div className="pb-4">
				<header className="mb-5">
					<p className="text-sm text-muted-foreground">
						建议（未锁定）→ 锁定 / 真实回录 → 开奖导入 → 结算 → 复盘。纸面是模拟收益不进真金资金池；开奖是唯一事实源，
						更正保留原因并原子重算与冲正。
					</p>
				</header>

				{message ? (
					<p data-testid="bets-message" role="status" className="mb-4 rounded-md bg-muted px-3 py-2 text-sm">
						{message}
					</p>
				) : null}

				{bets.isPending ? <p data-testid="bets-loading">加载投注记录…</p> : null}
				{bets.isError ? (
					<p data-testid="bets-error" className="text-sm text-muted-foreground">
						后端不可用 — 用 <code>task server</code> 启动 API。
					</p>
				) : null}

				{bets.data ? (
					<>
						<section aria-labelledby="bets-suggestions-heading" className="mb-8" data-testid="section-suggestions">
							<div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
								<h2 id="bets-suggestions-heading" className="text-sm font-medium">
									未锁定建议 <span className={`${TABULAR_NUMS} text-muted-foreground`}>{suggestions.length}</span>
								</h2>
								<span className="text-xs text-muted-foreground">{suggestionHint}</span>
							</div>
							<BetTable
								rows={suggestions}
								timePick={(bet) => bet.created_at}
								emptyText="无未锁定建议。"
								actions={(bet) => (
									<span className="flex flex-wrap items-center gap-2">
										{bet.mode === "paper" ? (
											<button
												type="button"
												data-testid={`lock-${bet.id}`}
												className="rounded-md bg-primary px-2 py-1 text-xs font-medium text-primary-foreground disabled:opacity-40"
												disabled={lockPaper.isPending}
												onClick={() => handleLock(bet)}
											>
												锁定纸面
											</button>
										) : (
											<button
												type="button"
												data-testid={`open-actual-${bet.id}`}
												className="rounded-md border border-border px-2 py-1 text-xs transition-colors hover:bg-muted"
												onClick={() => openActualForm(bet)}
											>
												回录实际条款
											</button>
										)}
									</span>
								)}
							/>
						</section>

						<section aria-labelledby="bets-locked-heading" className="mb-8" data-testid="section-locked">
							<div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
								<h2 id="bets-locked-heading" className="text-sm font-medium">
									已锁定 <span className={`${TABULAR_NUMS} text-muted-foreground`}>{locked.length}</span>
									<span className="ml-2 text-xs font-normal text-muted-foreground">
										{locked.length > 0 ? `${locked.length} 注待开奖/结算` : "等待开奖与结算"}
									</span>
								</h2>
								<button
									type="button"
									className="rounded-md border border-border px-3 py-1.5 text-sm transition-colors hover:bg-muted disabled:opacity-40"
									disabled={settle.isPending}
									onClick={() => settle.mutate()}
								>
									结算批跑
								</button>
							</div>
							<BetTable
								rows={locked}
								timePick={(bet) => bet.locked_at ?? bet.created_at}
								emptyText="无已锁定注——建议锁定后在这里等待开奖。"
							/>
						</section>

						<section aria-labelledby="bets-settled-heading" className="mb-8" data-testid="section-settled">
							<div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
								<h2 id="bets-settled-heading" className="text-sm font-medium">
									已结算 <span className={`${TABULAR_NUMS} text-muted-foreground`}>{settled.length}</span>
								</h2>
								<span className="text-xs text-muted-foreground">复盘资格按证据链判定；盈亏红盈绿亏</span>
							</div>
							<BetTable
								rows={settled}
								timePick={(bet) => bet.settled_at ?? bet.locked_at ?? bet.created_at}
								emptyText="尚无已结算注——开奖导入并结算后在这里复盘。"
							/>
						</section>

						<section aria-labelledby="bets-slips-heading" className="mb-8" data-testid="section-slips">
							<h2 id="bets-slips-heading" className="mb-3 text-sm font-medium">
								票级状态 <span className={`${TABULAR_NUMS} text-muted-foreground`}>{slips.data?.length ?? 0}</span>
							</h2>
							{slips.data && slips.data.length > 0 ? (
								<div className="overflow-x-auto rounded-lg border border-border">
									<Table data-density="compact">
										<TableHeader>
											<TableRow>
												<TableHead>#</TableHead>
												<TableHead>模式</TableHead>
												<TableHead>注数</TableHead>
												<TableHead>票金</TableHead>
												<TableHead>票盈亏</TableHead>
												<TableHead>回录时点</TableHead>
											</TableRow>
										</TableHeader>
										<TableBody>
											{slips.data.map((slip) => (
												<TableRow key={slip.id} data-testid="slip-row">
													<TableCell className={TABULAR_NUMS}>{slip.id}</TableCell>
													<TableCell>
														<ModeBadge mode={slip.mode} />
													</TableCell>
													<TableCell className={TABULAR_NUMS}>{slip.bet_count}</TableCell>
													<TableCell className={`${TABULAR_NUMS}`}>¥{slip.stake_total.toFixed(2)}</TableCell>
													<TableCell className={`${TABULAR_NUMS} ${pnlClass(slip.profit_total)}`}>
														{slip.profit_total >= 0 ? "+" : ""}
														{slip.profit_total.toFixed(2)}
													</TableCell>
													<TableCell className={`${TABULAR_NUMS} text-muted-foreground`}>
														{shortTime(slip.created_at)}
													</TableCell>
												</TableRow>
											))}
										</TableBody>
									</Table>
								</div>
							) : (
								<p className="text-sm text-muted-foreground">无回录票。</p>
							)}
						</section>
					</>
				) : null}

				{/* 赛果同步面板（票 06 预裁决 = 同步优先，人工兜底）：同步状态区 + 待出赛果 + 人工录入 */}
				<section
					aria-labelledby="bets-results-heading"
					className="rounded-lg border border-border p-4"
					data-testid="section-results"
				>
					<h2 id="bets-results-heading" className="mb-1 text-sm font-medium">
						赛果同步
					</h2>
					<p className="mb-4 text-xs text-muted-foreground">
						开奖为唯一事实源（ADR-0001）；更正保留来源、旧新值与原因，已结记录经重算与差额冲正更新。
					</p>

					{/* 同步状态区：本票后端源未接入 → 降级态（同步端点随票 10 排期，后端源属 goalx-quant 地图） */}
					<div data-testid="sync-status" className="mb-4">
						<EmptyState
							variant="not-available"
							message="自动同步待后端源接入——当前人工兜底。"
							hint={
								<>
									接入后这里显示上次同步时间/来源与主动触发按钮（同步端点随票 10 排期）；已录赛果{" "}
									<span className={TABULAR_NUMS}>{drawResults.data?.length ?? 0}</span> 场。
								</>
							}
						/>
					</div>

					<div className="mb-4" data-testid="draw-pending">
						<h3 className="mb-2 text-xs font-medium text-muted-foreground">
							待出赛果 <span className={TABULAR_NUMS}>{pendingFixtures.length}</span> 场（已开赛、无开奖）
						</h3>
						{drawResults.isError ? (
							<p className="text-xs text-muted-foreground">开奖记录加载失败——待出判定暂缺。</p>
						) : pendingFixtures.length === 0 ? (
							<p className="text-xs text-muted-foreground">今日场次暂无待出赛果——已开赛场次会出现在这里。</p>
						) : (
							<ul className="space-y-1.5">
								{pendingFixtures.map((fixture) => (
									<li
										key={fixture.fixture_id}
										data-testid="draw-pending-row"
										className="flex flex-wrap items-center gap-2 rounded-md border border-border px-3 py-1.5 text-sm"
									>
										<span className="text-xs text-muted-foreground">{fixture.match_code}</span>
										<span>
											{fixture.home_team} vs {fixture.away_team}
										</span>
										<span className={`${TABULAR_NUMS} text-xs text-muted-foreground`}>
											{localClock(fixture.kickoff_utc)} 开赛
										</span>
										{openFixtureIds.has(fixture.fixture_id) ? (
											<span className="rounded bg-warning/10 px-1.5 py-0.5 text-xs text-foreground">挂未结注</span>
										) : null}
										<button
											type="button"
											aria-label={`录入 ${fixture.match_code}`}
											className="ml-auto rounded-md border border-border px-2 py-0.5 text-xs transition-colors hover:bg-muted"
											onClick={() => setDrawForm({ ...drawForm, fixtureId: String(fixture.fixture_id) })}
										>
											录入
										</button>
									</li>
								))}
							</ul>
						)}
					</div>

					{/* 人工录入 = 兜底/更正通道（同步冲突、无效场次、口径异常），更正必填原因 + 影响预览 */}
					<h3 className="mb-2 text-xs font-medium text-muted-foreground">人工录入（兜底/更正通道，官方口径）</h3>
					<form
						className="flex flex-wrap items-end gap-3 text-sm"
						onSubmit={(event) => {
							event.preventDefault();
							importResult.mutate();
						}}
					>
						<label className="flex flex-col gap-1">
							<span className="text-xs text-muted-foreground">场次</span>
							<select
								required
								data-testid="draw-fixture"
								className="w-56 rounded-md border border-input bg-background px-2 py-1.5 text-sm"
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
							<span className="text-xs text-muted-foreground">主队进球</span>
							<input
								required
								type="number"
								min={0}
								data-testid="draw-home"
								className="w-20 rounded-md border border-input bg-background px-2 py-1.5 text-sm"
								value={drawForm.homeGoals}
								onChange={(event) => setDrawForm({ ...drawForm, homeGoals: event.target.value })}
							/>
						</label>
						<label className="flex flex-col gap-1">
							<span className="text-xs text-muted-foreground">客队进球</span>
							<input
								required
								type="number"
								min={0}
								data-testid="draw-away"
								className="w-20 rounded-md border border-input bg-background px-2 py-1.5 text-sm"
								value={drawForm.awayGoals}
								onChange={(event) => setDrawForm({ ...drawForm, awayGoals: event.target.value })}
							/>
						</label>
						<label className="flex items-center gap-1 pb-1.5">
							<input
								type="checkbox"
								data-testid="draw-void"
								checked={drawForm.isVoid}
								onChange={(event) => setDrawForm({ ...drawForm, isVoid: event.target.checked })}
							/>
							<span className="text-xs text-muted-foreground">无效场次</span>
						</label>
						{drawForm.isVoid ? (
							<label className="flex flex-col gap-1">
								<span className="text-xs text-muted-foreground">无效原因</span>
								<input
									data-testid="draw-void-reason"
									className="w-32 rounded-md border border-input bg-background px-2 py-1.5 text-sm"
									value={drawForm.voidReason}
									onChange={(event) => setDrawForm({ ...drawForm, voidReason: event.target.value })}
								/>
							</label>
						) : null}
						{existing ? (
							<label className="flex flex-col gap-1">
								<span className="text-xs text-muted-foreground">
									更正原因（已有 {existing.home_goals}:{existing.away_goals}
									{existing.void ? " 无效" : ""}，必填）
								</span>
								<input
									required
									data-testid="draw-correction-reason"
									className="w-44 rounded-md border border-input bg-background px-2 py-1.5 text-sm"
									value={drawForm.correctionReason}
									onChange={(event) => setDrawForm({ ...drawForm, correctionReason: event.target.value })}
								/>
							</label>
						) : null}
						<button
							type="button"
							data-testid="draw-preview"
							className="rounded-md border border-border px-3 py-1.5 transition-colors hover:bg-muted disabled:opacity-40"
							disabled={doPreview.isPending || drawForm.fixtureId === ""}
							onClick={() => doPreview.mutate()}
						>
							影响预览
						</button>
						<button
							type="submit"
							data-testid="draw-import"
							className="rounded-md bg-primary px-3 py-1.5 font-medium text-primary-foreground disabled:opacity-40"
							disabled={importResult.isPending}
						>
							导入
						</button>
					</form>

					{preview ? (
						<div
							className="mt-4 rounded-md border border-border bg-muted/40 p-3 text-sm"
							data-testid="draw-preview-result"
						>
							<p className="mb-2 font-medium">
								预览（只读，导入才执行重算与冲正）：
								{preview.results.map((change) => (
									<span key={change.fixture_id} className="ml-2 text-muted-foreground">
										#{change.fixture_id}
										{change.is_correction
											? ` 更正 ${change.previous?.["home_goals"]}:${change.previous?.["away_goals"]} → ${change.replacement["home_goals"]}:${change.replacement["away_goals"]}`
											: " 首次导入"}
									</span>
								))}
							</p>
							{preview.affected_bets.length === 0 ? (
								<p className="text-muted-foreground">无受影响注。</p>
							) : (
								<table className="w-full text-xs">
									<thead>
										<tr className="text-left text-muted-foreground">
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
												<td className={`${TABULAR_NUMS} px-2 py-1`}>
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
			</div>

			{/* 真实回录 Drawer（票 06 定稿：右侧抽屉 + 提交前快照对照） */}
			<Drawer
				open={actualFor !== null}
				onOpenChange={(open) => setActualFor(open ? actualFor : null)}
				swipeDirection="right"
			>
				<DrawerContent className="mx-0 w-full sm:max-w-md" data-testid="actual-drawer">
					{actualBet ? (
						<>
							<DrawerHeader>
								<DrawerTitle>回录实际条款</DrawerTitle>
								<DrawerDescription>
									建议 #{actualBet.id}（{actualBet.mode === "live" ? "真金" : "纸面"}）——建议快照保留，账务按实际条款。
								</DrawerDescription>
							</DrawerHeader>
							<div className="flex-1 overflow-y-auto p-4 pt-2">
								<div className="mb-3 flex flex-wrap items-end gap-3">
									<label className="flex flex-col gap-1 text-xs">
										<span className="text-muted-foreground">实际金额(¥)</span>
										<input
											aria-label="实际金额"
											type="number"
											min={1}
											step="0.01"
											required
											data-testid="actual-stake"
											className="w-28 rounded-md border border-input bg-background px-2 py-1.5 text-sm"
											value={actualForm.stake}
											onChange={(event) => setActualForm({ ...actualForm, stake: event.target.value })}
										/>
									</label>
									<label className="flex flex-col gap-1 text-xs">
										<span className="text-muted-foreground">回录时点（可选）</span>
										<input
											aria-label="回录时点"
											type="datetime-local"
											data-testid="actual-placed-at"
											className="rounded-md border border-input bg-background px-2 py-1.5 text-sm"
											value={actualForm.placedAt}
											onChange={(event) => setActualForm({ ...actualForm, placedAt: event.target.value })}
										/>
									</label>
								</div>

								<p className="mb-1.5 text-xs font-medium text-muted-foreground">逐腿实际赔率（留空按锁定赔率）</p>
								<ul className="mb-4 space-y-2">
									{actualBet.legs.map((leg) => (
										<li
											key={leg.fixture_id}
											className="flex items-center justify-between gap-2 rounded-md border border-border p-2.5 text-sm"
											data-testid="actual-leg"
										>
											<span>
												<span className="text-xs text-muted-foreground">#{leg.fixture_id}</span>{" "}
												{leg.market_code === "hhad" ? `让球${leg.goal_line ?? "?"} ` : ""}
												{SELECTION_LABELS[leg.selection_code] ?? leg.selection_code}
											</span>
											<span className="flex items-center gap-2 text-xs">
												<span className={`${TABULAR_NUMS} text-muted-foreground`}>
													锁定 @{leg.locked_odds.toFixed(2)}
												</span>
												<input
													aria-label={`#${leg.fixture_id} 实际赔率`}
													type="number"
													min={1.01}
													step="0.01"
													placeholder={leg.locked_odds.toFixed(2)}
													className="w-20 rounded-md border border-input bg-background px-2 py-1 text-sm"
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
										</li>
									))}
								</ul>

								{/* 提交前对照（票 06）：建议快照 vs 实际条款；金额≠建议 → 按实际条款记账提示 */}
								<div data-testid="actual-compare" className="rounded-md border border-border bg-muted/40 p-3 text-xs">
									<p className="mb-2 font-medium">建议快照 vs 实际条款</p>
									<ul className="space-y-1 text-muted-foreground">
										<li className={TABULAR_NUMS}>
											金额 ¥{actualBet.stake.toFixed(2)} →{" "}
											{actualStakeValid ? `¥${actualStakeValue.toFixed(2)}` : "待填写"}
										</li>
										{actualBet.legs.map((leg) => {
											const raw = actualForm.oddsByFixture[leg.fixture_id]?.trim();
											const value = raw === undefined || raw === "" ? null : Number(raw);
											return (
												<li key={leg.fixture_id} className={TABULAR_NUMS}>
													#{leg.fixture_id} 赔率 {leg.locked_odds.toFixed(2)} →{" "}
													{value !== null && !Number.isNaN(value) ? value.toFixed(2) : "按锁定价"}
												</li>
											);
										})}
									</ul>
									{actualStakeValid && actualStakeValue !== actualBet.stake ? (
										<p data-testid="actual-diff-note" className="mt-2 rounded bg-warning/10 px-2 py-1 text-foreground">
											实际金额 ≠ 建议金额——按实际条款记账（真金流水按实际金额入账）。
										</p>
									) : null}
								</div>
							</div>
							<DrawerFooter>
								<button
									type="button"
									data-testid="actual-submit"
									className="w-full rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-40"
									disabled={recordLive.isPending || !actualStakeValid}
									onClick={() => submitActual()}
								>
									提交
								</button>
								<DrawerClose className="rounded-md px-1 py-0.5 text-center text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline">
									取消
								</DrawerClose>
							</DrawerFooter>
						</>
					) : null}
				</DrawerContent>
			</Drawer>
		</AppShell>
	);
}
