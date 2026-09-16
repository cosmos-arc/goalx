import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { createBet, fetchTodayFixtures, type TodayFixture } from "../api/goalx";
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
 * 票 14：今日页重设计落地（票 04 定稿 = 唯一事实源，原型 proto/ui-04-today）。
 * 信息分层：可投卡片置顶 + 全量 compact 对照表（无视图切换）；编码硬约束：
 * EV 数字永远只按正负红绿（text-profit/text-loss，诊断量非机会信号）、|EV|≥5%
 * 独立琥珀徽章、资格徽章蓝=可投/红=拒绝+原因/灰框=证据未知、新鲜度>30 分钟琥珀、
 * T1 中性灰；选注篮 = 底部常驻条 + 右侧 Drawer；竞彩规则在选择时前置传达，
 * 服务器校验仍为唯一权威（前端提示不替代后端判定）。
 */

type Selection = "h" | "d" | "a";

const SELECTIONS: Selection[] = ["h", "d", "a"];
const MAX_LEGS = 2;

const FLAG_LABELS: Record<string, string> = {
	ev_deviation: "EV 偏差≥5%",
	few_books: "样本少",
	not_joined: "未 join 欧赔",
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

type Leg = {
	fixture_id: number;
	match_code: string;
	home_team: string;
	away_team: string;
	selection: Selection;
	odds: number;
	single_eligible: boolean | null;
};

function oddsText(value: number | null | undefined): string {
	return value === null || value === undefined ? "—" : value.toFixed(2);
}

/** EV 数字永远只按正负红绿（票 04 定稿）：红 = 正、绿 = 负，近零中性。 */
function evClass(value: number): string {
	if (value > 0.002) return "text-profit";
	if (value < -0.002) return "text-loss";
	return "text-muted-foreground";
}

function evText(value: number): string {
	return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(1)}%`;
}

function localTime(utc: string): string {
	const date = new Date(utc);
	if (Number.isNaN(date.getTime())) {
		return "—";
	}
	return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

/** 开赛倒计时（前端可推导，不加 API 字段）：已开赛/分钟（<2h 琥珀紧迫）/小时。 */
function kickoffInfo(utc: string, now: number): { passed: boolean; text: string; urgent: boolean } {
	const kickoff = new Date(utc).getTime();
	if (Number.isNaN(kickoff)) {
		return { passed: false, text: "—", urgent: false };
	}
	const offsetMin = (kickoff - now) / 60_000;
	if (offsetMin <= 0) {
		return { passed: true, text: "已开赛", urgent: false };
	}
	if (offsetMin < 120) {
		return { passed: false, text: `${Math.round(offsetMin)} 分钟后`, urgent: true };
	}
	return { passed: false, text: `${Math.floor(offsetMin / 60)} 小时后`, urgent: false };
}

/** 报价年龄（分钟）；jc_updated_at 缺失时返回 null（时间格省略该段）。 */
function minutesAgo(iso: string | null | undefined, now: number): number | null {
	if (!iso) {
		return null;
	}
	const at = new Date(iso).getTime();
	if (Number.isNaN(at)) {
		return null;
	}
	return Math.max(0, Math.round((now - at) / 60_000));
}

/** 可投 = 判定 valid + 在售 + 未开赛（证据链完整且新鲜，不等于必成交）。 */
function isPickable(fixture: TodayFixture, now: number): boolean {
	const quote = fixture.had_quote;
	if (quote?.status !== "valid" || quote.sale_state !== "on_sale") {
		return false;
	}
	const kickoff = new Date(fixture.kickoff_utc).getTime();
	return !Number.isNaN(kickoff) && kickoff > now;
}

function makeLeg(fixture: TodayFixture, selection: Selection, odds: number): Leg {
	return {
		fixture_id: fixture.fixture_id,
		match_code: fixture.match_code,
		home_team: fixture.home_team,
		away_team: fixture.away_team,
		selection,
		odds,
		single_eligible: fixture.had_quote?.single_eligible ?? null,
	};
}

/** 资格徽章（票 04 编码）：蓝 = 可投 / 红 = 拒绝 + 原因 / 灰框 = 证据未知；无判定弱化。 */
function EligibilityBadge({ fixture }: { fixture: TodayFixture }) {
	const quote = fixture.had_quote;
	if (!quote) {
		return <span className="text-xs text-muted-foreground">无判定</span>;
	}
	const reasons = (quote.reasons ?? []).map((reason) => REASON_LABELS[reason] ?? reason).join("/");
	if (quote.status === "valid") {
		return (
			<span className="flex flex-wrap items-center gap-1" data-testid="had-quote-valid">
				<Badge className="bg-info/10 text-info">可投</Badge>
				{quote.single_eligible === true ? (
					<span className="rounded border border-border px-1 text-xs text-muted-foreground">单固</span>
				) : null}
			</span>
		);
	}
	if (quote.status === "rejected") {
		return (
			<span className="flex flex-wrap items-center gap-1" data-testid="had-quote-rejected">
				<Badge variant="outline" className="border-destructive/40 text-destructive">
					拒绝
				</Badge>
				{reasons ? <span className="text-xs text-muted-foreground">{reasons}</span> : null}
			</span>
		);
	}
	return (
		<span className="flex flex-wrap items-center gap-1" data-testid="had-quote-unknown">
			<Badge variant="outline">证据未知</Badge>
			{reasons ? <span className="text-xs text-muted-foreground">{reasons}</span> : null}
		</span>
	);
}

function OddsButton({
	fixture,
	selection,
	value,
	selected,
	disabled,
	onPick,
	testid,
	size = "sm",
}: {
	fixture: TodayFixture;
	selection: Selection;
	value: number | null | undefined;
	selected: boolean;
	disabled: boolean;
	onPick: (fixture: TodayFixture, selection: Selection, odds: number) => void;
	/** 卡片与表格各有一组选注钮：卡片用 pick-card-* 前缀，表格保留 pick-*（e2e 契约）。 */
	testid: string;
	size?: "sm" | "md";
}) {
	return (
		<button
			type="button"
			disabled={disabled || value === null || value === undefined}
			data-testid={testid}
			aria-label={`${fixture.match_code} ${SELECTION_LABELS[selection]} @${oddsText(value)}`}
			className={`rounded-md border ${TABULAR_NUMS} transition-colors ${
				size === "md" ? "px-3 py-1.5 text-sm" : "px-2 py-1 text-xs"
			} ${
				selected
					? "border-primary bg-primary text-primary-foreground"
					: disabled
						? "border-transparent text-muted-foreground/40"
						: "border-border hover:bg-muted"
			}`}
			onClick={() => {
				if (value !== null && value !== undefined) {
					onPick(fixture, selection, value);
				}
			}}
		>
			{oddsText(value)}
		</button>
	);
}

function EvSpan({ value }: { value: number | null | undefined }) {
	if (value === null || value === undefined) {
		return <span className="mr-1.5 text-muted-foreground">—</span>;
	}
	return <span className={`mr-1.5 ${evClass(value)}`}>{evText(value)}</span>;
}

/** 可投卡片（票 04）：联赛/倒计时/EV 三向 + 最强标注/快捷选注/仅串关标记；区块即过滤结果，不重复"可投"徽章。 */
function EligibleCard({
	fixture,
	now,
	legs,
	onPick,
}: {
	fixture: TodayFixture;
	now: number;
	legs: Leg[];
	onPick: (fixture: TodayFixture, selection: Selection, odds: number) => void;
}) {
	const cd = kickoffInfo(fixture.kickoff_utc, now);
	const evValues = SELECTIONS.map((sel) => fixture.ev?.[sel]).filter((v): v is number => v !== null && v !== undefined);
	const best = evValues.length > 0 ? SELECTIONS.find((sel) => fixture.ev?.[sel] === Math.max(...evValues)) : undefined;
	const selected = (sel: Selection) =>
		legs.some((leg) => leg.fixture_id === fixture.fixture_id && leg.selection === sel);
	return (
		<article className="rounded-lg border border-border bg-card p-4" data-testid={`today-card-${fixture.fixture_id}`}>
			<div className="mb-2 flex items-center justify-between gap-2">
				<span className="flex items-center gap-1.5 text-xs text-muted-foreground">
					{fixture.competition}
					{fixture.tier === "tier1" ? (
						<span className="rounded border border-border px-1 text-muted-foreground">T1</span>
					) : null}
					{fixture.had_quote?.single_eligible === true ? null : (
						<span className="rounded border border-border px-1 text-muted-foreground">仅串关</span>
					)}
				</span>
				<span className={`text-xs ${TABULAR_NUMS} ${cd.urgent ? "font-medium text-warning" : "text-muted-foreground"}`}>
					{localTime(fixture.kickoff_utc)} · {cd.text}
				</span>
			</div>
			<p className="mb-1 text-sm font-medium">
				{fixture.home_team} <span className="text-muted-foreground">vs</span> {fixture.away_team}
				<span className="ml-2 text-xs font-normal text-muted-foreground">{fixture.match_code}</span>
			</p>
			<div className="mb-3 flex flex-wrap items-baseline gap-2 text-xs">
				<span className="text-muted-foreground">EV</span>
				{SELECTIONS.map((sel) => {
					const value = fixture.ev?.[sel];
					return value === null || value === undefined ? (
						<span key={sel} className={`${TABULAR_NUMS} text-muted-foreground`}>
							{SELECTION_LABELS[sel]} —
						</span>
					) : (
						<span key={sel} className={`${TABULAR_NUMS} ${evClass(value)}`}>
							{SELECTION_LABELS[sel]} {evText(value)}
						</span>
					);
				})}
				{(fixture.flags ?? [])
					.filter((flag) => flag in FLAG_LABELS)
					.map((flag) => (
						<span
							key={flag}
							data-testid={`flag-${flag}`}
							className="rounded bg-warning/10 px-1.5 py-0.5 text-foreground"
						>
							{FLAG_LABELS[flag]}
						</span>
					))}
				{best ? (
					<span className={`${TABULAR_NUMS} ml-auto text-muted-foreground`} title="共识 EV 最高的一向">
						最强 {SELECTION_LABELS[best]}
					</span>
				) : null}
			</div>
			<div className="flex items-center gap-2">
				{SELECTIONS.map((sel) => (
					<OddsButton
						key={sel}
						fixture={fixture}
						selection={sel}
						value={fixture.jc_odds[sel]}
						selected={selected(sel)}
						disabled={false}
						onPick={onPick}
						testid={`pick-card-${fixture.fixture_id}-${sel}`}
						size="md"
					/>
				))}
			</div>
		</article>
	);
}

export function TodayPage() {
	const today = useQuery({ queryKey: ["today"], queryFn: () => fetchTodayFixtures() });
	const queryClient = useQueryClient();
	const [legs, setLegs] = useState<Leg[]>([]);
	const [basketOpen, setBasketOpen] = useState(false);
	const [stake, setStake] = useState("100");
	const [strategyVersion, setStrategyVersion] = useState("");
	const [mode, setMode] = useState<"paper" | "live">("paper");
	const [message, setMessage] = useState<string | null>(null);
	const now = Date.now();

	const fixtures = today.data ?? [];
	const eligible = fixtures.filter((fixture) => isPickable(fixture, now));
	const joinedCount = fixtures.filter((fixture) => fixture.joined).length;
	const jcAge = fixtures
		.map((fixture) => minutesAgo(fixture.jc_updated_at, now))
		.filter((age): age is number => age !== null)
		.sort((a, b) => a - b)
		.at(0);
	const combinedOdds = legs.reduce((acc, leg) => acc * leg.odds, 1);

	/** 规则前置（票 04）：同场换选=替换+提示；2串1 上限=行内提示；非单固首腿=提示；停售/已开赛=按钮禁用。 */
	function pick(fixture: TodayFixture, selection: Selection, odds: number) {
		if (!isPickable(fixture, now)) {
			return;
		}
		const existing = legs.find((leg) => leg.fixture_id === fixture.fixture_id);
		if (existing && existing.selection === selection) {
			setLegs(legs.filter((leg) => leg.fixture_id !== fixture.fixture_id));
			setMessage(null);
			return;
		}
		if (legs.length >= MAX_LEGS) {
			setMessage(`已达 ${MAX_LEGS}串1 上限——先在选注篮移除一腿`);
			return;
		}
		if (existing) {
			setLegs(legs.map((leg) => (leg.fixture_id === fixture.fixture_id ? makeLeg(fixture, selection, odds) : leg)));
			setMessage("同场只能选一腿（竞彩禁同场串关），已替换原选择");
			return;
		}
		if (legs.length === 0 && fixture.had_quote && fixture.had_quote.single_eligible !== true) {
			setMessage(`${fixture.match_code} 非单固：只能作为串关第二腿（先选单固场，提交时服务器校验）`);
		} else {
			setMessage(null);
		}
		setLegs([...legs, makeLeg(fixture, selection, odds)]);
	}

	function removeLeg(fixtureId: number) {
		setLegs(legs.filter((leg) => leg.fixture_id !== fixtureId));
		setMessage(null);
	}

	const createSuggestion = useMutation({
		mutationFn: () =>
			createBet({
				mode,
				stake: Number(stake),
				strategy_version: strategyVersion.trim() === "" ? null : strategyVersion.trim(),
				legs: legs.map((leg) => ({
					fixture_id: leg.fixture_id,
					market_code: "had",
					selection_code: leg.selection,
					locked_odds: leg.odds,
				})),
			}),
		onSuccess: (bet) => {
			setMessage(`已建建议 #${bet.id}（${legs.length === 1 ? "单关" : "2串1"}）— 去投注页锁定`);
			setLegs([]);
			setBasketOpen(false);
			void queryClient.invalidateQueries({ queryKey: ["bets"] });
		},
		onError: (error) => setMessage(`建注失败：${errorText(error)}`),
	});

	return (
		<AppShell title="今日">
			<div className="pb-24">
				<header className="mb-5">
					<div className="flex flex-wrap items-baseline justify-between gap-3">
						<p className="text-sm text-muted-foreground">
							竞彩价对照欧洲共识（Shin 去水）。EV 是市场共识的诊断量，不是机会信号；红涨绿跌，|EV|≥5% 标偏差。
						</p>
						{/* 页头全局数据健康（票 04）：竞彩报价年龄（可推导）+ 欧赔接入覆盖 */}
						<div className="flex items-center gap-3 text-xs" data-testid="today-data-health">
							{jcAge === undefined ? (
								<span className="text-muted-foreground">竞彩报价 —</span>
							) : (
								<span className={jcAge > 30 ? "text-warning" : "text-muted-foreground"}>竞彩报价 {jcAge}分钟前</span>
							)}
							<span className="text-muted-foreground">
								欧赔 已接入 {joinedCount}/{fixtures.length} 场
							</span>
						</div>
					</div>
					{/* 配色图例行（票 04 定稿） */}
					<p className="mt-1 text-xs text-muted-foreground">
						配色：红 = 正向 EV · 绿 = 负向 EV · 琥珀 = 数据警示（过期/样本少/偏差） · 蓝 = 可投资格
					</p>
				</header>

				{message ? (
					<div
						role="status"
						data-testid="today-message"
						className="fixed inset-x-0 bottom-16 z-50 mx-auto w-fit max-w-[min(92vw,42rem)] rounded-md bg-muted px-3 py-1.5 text-sm text-foreground shadow-sm"
					>
						{message}
					</div>
				) : null}

				{today.isPending ? (
					// 加载骨架（票 14 验收）：形状与信息分层同构，sr-only 文本给读屏
					<div data-testid="today-loading" className="space-y-4">
						<span className="sr-only">加载今日场次…</span>
						<div className="h-5 w-64 animate-pulse rounded bg-muted" />
						<div className="grid gap-3 sm:grid-cols-2">
							<div className="h-36 animate-pulse rounded-lg bg-muted" />
							<div className="h-36 animate-pulse rounded-lg bg-muted" />
						</div>
						<div className="h-44 animate-pulse rounded-lg bg-muted" />
					</div>
				) : null}

				{today.isError ? (
					// 票 13：后端不可用态——保留启动/灌数指引 + 重试（服务器校验仍是唯一权威）
					<EmptyState
						variant="backend-unavailable"
						message="连不上后端，今日场次加载失败。"
						hint={
							<>
								用 <code>task server</code> 启动 API；首次使用先跑 <code>task ingest-jingcai</code> 拉取竞彩数据。
							</>
						}
						action={{ label: "重试", onClick: () => void today.refetch() }}
					/>
				) : null}

				{today.data && today.data.length === 0 ? (
					// 票 13：无数据态——为何空 + 何时有 + 刷新
					<EmptyState
						variant="no-data"
						message="当日无在售场次。"
						hint={
							<>
								场次通常在竞彩当日开售前更新；刚搭好环境先跑 <code>task ingest-jingcai</code>。
							</>
						}
						action={{ label: "刷新", onClick: () => void today.refetch() }}
					/>
				) : null}

				{today.data && today.data.length > 0 ? (
					<>
						<section aria-labelledby="today-eligible-heading" className="mb-8" data-testid="today-eligible">
							<div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
								<h2 id="today-eligible-heading" className="text-sm font-medium">
									可投场次 <span className={`${TABULAR_NUMS} text-muted-foreground`}>{eligible.length}</span>
									<span className="ml-2 text-xs font-normal text-muted-foreground">
										可投 = 证据链完整且新鲜；不等于必成交
									</span>
								</h2>
								<span className="text-xs text-muted-foreground" data-testid="today-not-eligible-count">
									{fixtures.length - eligible.length} 场不可投（停售/已开赛/证据未知）
								</span>
							</div>
							{eligible.length === 0 ? (
								<p className="rounded-lg border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
									当前无可投场次——可投要求在售、未开赛且证据新鲜；竞彩通常 10 点后上架。
								</p>
							) : (
								<div className="grid gap-3 sm:grid-cols-2">
									{eligible.map((fixture) => (
										<EligibleCard key={fixture.fixture_id} fixture={fixture} now={now} legs={legs} onPick={pick} />
									))}
								</div>
							)}
						</section>

						<section aria-labelledby="today-all-heading">
							<h2 id="today-all-heading" className="mb-3 text-sm font-medium">
								全部场次 <span className={`${TABULAR_NUMS} text-muted-foreground`}>{fixtures.length}</span>
							</h2>
							<div className="overflow-x-auto rounded-lg border border-border">
								{/* 票 04 表格结构：资格列第 4 位（筛行信号前置）；compact 密度，1440 无横滚 */}
								<Table data-density="compact">
									<TableHeader>
										<TableRow>
											<TableHead>时间</TableHead>
											<TableHead>编号</TableHead>
											<TableHead>对阵</TableHead>
											<TableHead>资格</TableHead>
											<TableHead>竞彩 H/D/A</TableHead>
											<TableHead>欧共识</TableHead>
											<TableHead>EV H/D/A</TableHead>
											<TableHead className="text-center">books</TableHead>
										</TableRow>
									</TableHeader>
									<TableBody>
										{fixtures.map((fixture) => {
											const canPick = isPickable(fixture, now);
											const cd = kickoffInfo(fixture.kickoff_utc, now);
											const jcAgeRow = minutesAgo(fixture.jc_updated_at, now);
											return (
												// 停售/已开赛/证据未知 → 禁用 + 行弱化（票 04 规则前置；弱化用背景 wash——
												// opacity 会把整行文字对比压到 AA 以下，过不了 axe 零 serious 基调）
												<TableRow
													key={fixture.fixture_id}
													data-testid="today-row"
													className={canPick ? "" : "bg-muted/50"}
												>
													<TableCell className={`${TABULAR_NUMS} whitespace-nowrap text-muted-foreground`}>
														{localTime(fixture.kickoff_utc)}
														{cd.passed ? <span className="ml-1 text-warning">已开赛</span> : null}
														<br />
														<span className="text-xs">
															{fixture.competition}
															{fixture.tier === "tier1" ? (
																<span className="ml-1 rounded border border-border px-1 text-muted-foreground">T1</span>
															) : null}
															{jcAgeRow === null ? null : (
																<span className={jcAgeRow > 30 ? "text-warning" : ""}> · 彩 {jcAgeRow}分钟前</span>
															)}
														</span>
													</TableCell>
													<TableCell className="whitespace-nowrap">{fixture.match_code}</TableCell>
													<TableCell className="whitespace-nowrap font-medium">
														{fixture.home_team} vs {fixture.away_team}
													</TableCell>
													<TableCell>
														<EligibilityBadge fixture={fixture} />
													</TableCell>
													<TableCell>
														<span className="flex gap-1">
															{SELECTIONS.map((sel) => (
																<OddsButton
																	key={sel}
																	fixture={fixture}
																	selection={sel}
																	value={fixture.jc_odds[sel]}
																	selected={legs.some(
																		(leg) => leg.fixture_id === fixture.fixture_id && leg.selection === sel,
																	)}
																	disabled={!canPick}
																	onPick={pick}
																	testid={`pick-${fixture.fixture_id}-${sel}`}
																/>
															))}
														</span>
													</TableCell>
													<TableCell className={`${TABULAR_NUMS} whitespace-nowrap text-muted-foreground`}>
														{fixture.eu_prob
															? `${((fixture.eu_prob.h ?? 0) * 100).toFixed(0)}/${((fixture.eu_prob.d ?? 0) * 100).toFixed(0)}/${((fixture.eu_prob.a ?? 0) * 100).toFixed(0)}`
															: "—"}
													</TableCell>
													<TableCell className={`${TABULAR_NUMS} whitespace-nowrap`} data-testid="ev-cell">
														{fixture.ev ? SELECTIONS.map((sel) => <EvSpan key={sel} value={fixture.ev?.[sel]} />) : "—"}
													</TableCell>
													<TableCell className={`${TABULAR_NUMS} text-center`}>{fixture.books || "—"}</TableCell>
												</TableRow>
											);
										})}
									</TableBody>
								</Table>
							</div>
						</section>
					</>
				) : null}
			</div>

			{/* 底部常驻选注条（票 04）：浏览全程可见已选腿摘要 + 组合赔率 */}
			<div className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-background/95 backdrop-blur">
				<div className="mx-auto flex max-w-6xl items-center gap-3 px-6 py-3">
					<span className="text-sm">
						选注篮{" "}
						<span className={`${TABULAR_NUMS} font-medium`} data-testid="basket-count">
							{legs.length}/{MAX_LEGS}
						</span>
					</span>
					{legs.length > 0 ? (
						<span
							className={`${TABULAR_NUMS} hidden text-xs text-muted-foreground sm:inline`}
							data-testid="basket-summary"
						>
							{legs.map((leg) => `${leg.match_code} ${SELECTION_LABELS[leg.selection]}`).join(" × ")}
							{legs.length === MAX_LEGS ? ` · 组合赔率 ${combinedOdds.toFixed(2)}` : ""}
						</span>
					) : (
						<span className="hidden text-xs text-muted-foreground sm:inline">
							点任意赔率加入；单关须单固，2串1 须不同场次
						</span>
					)}
					<button
						type="button"
						data-testid="basket-open"
						className="ml-auto rounded-md border border-border bg-background px-3 py-1.5 text-sm font-medium transition-colors hover:bg-muted disabled:pointer-events-none disabled:opacity-50"
						disabled={legs.length === 0}
						onClick={() => setBasketOpen(true)}
					>
						展开选注篮
					</button>
				</div>
			</div>

			<Drawer open={basketOpen} onOpenChange={setBasketOpen} swipeDirection="right">
				<DrawerContent className="mx-0 w-full sm:max-w-md">
					<DrawerHeader>
						<DrawerTitle>选注篮</DrawerTitle>
						<DrawerDescription>
							{legs.length === 1 && legs[0]?.single_eligible !== true
								? "当前腿非单固，只能作为串关腿（提交时服务器校验）"
								: "单关须单固；2串1 须不同场次且共同可投（提交时服务器校验）"}
						</DrawerDescription>
					</DrawerHeader>
					<div className="flex-1 overflow-y-auto p-4">
						{legs.length === 0 ? (
							<p className="text-sm text-muted-foreground">未选择。点页面里任意赔率加入。</p>
						) : (
							<ul className="space-y-2">
								{legs.map((leg) => (
									<li
										key={leg.fixture_id}
										className="flex items-center justify-between rounded-md border border-border p-3 text-sm"
										data-testid="basket-leg"
									>
										<span>
											<span className="text-xs text-muted-foreground">{leg.match_code}</span>
											<br />
											{leg.home_team} vs {leg.away_team}
											<br />
											<span className="font-medium">{SELECTION_LABELS[leg.selection]}</span>
											<span className={`${TABULAR_NUMS} ml-2 text-muted-foreground`}>@{leg.odds.toFixed(2)}</span>
										</span>
										<button
											type="button"
											className="rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
											aria-label={`移除 ${leg.match_code}`}
											onClick={() => removeLeg(leg.fixture_id)}
										>
											移除
										</button>
									</li>
								))}
							</ul>
						)}
					</div>
					<DrawerFooter>
						<div className="flex flex-wrap items-end gap-3">
							<label className="flex flex-col gap-1 text-xs">
								<span className="text-muted-foreground">模式</span>
								<select
									data-testid="basket-mode"
									className="rounded-md border border-input bg-background px-2 py-1.5 text-sm"
									value={mode}
									onChange={(event) => setMode(event.target.value === "live" ? "live" : "paper")}
								>
									<option value="paper">纸面</option>
									<option value="live">真金</option>
								</select>
							</label>
							<label className="flex flex-col gap-1 text-xs">
								<span className="text-muted-foreground">金额(¥)</span>
								<input
									required
									type="number"
									min={1}
									step="0.01"
									data-testid="basket-stake"
									className="w-24 rounded-md border border-input bg-background px-2 py-1.5 text-sm"
									value={stake}
									onChange={(event) => setStake(event.target.value)}
								/>
							</label>
							<label className="flex flex-col gap-1 text-xs">
								<span className="text-muted-foreground">策略版本(可选)</span>
								<input
									data-testid="basket-strategy"
									placeholder="手动"
									className="w-32 rounded-md border border-input bg-background px-2 py-1.5 text-sm"
									value={strategyVersion}
									onChange={(event) => setStrategyVersion(event.target.value)}
								/>
							</label>
						</div>
						<button
							type="button"
							data-testid="basket-submit"
							className="w-full rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-40"
							disabled={createSuggestion.isPending || legs.length === 0}
							onClick={() => createSuggestion.mutate()}
						>
							建立建议
						</button>
						<div className="flex items-center justify-between text-xs">
							<DrawerClose className="rounded-md px-1 py-0.5 text-muted-foreground underline-offset-2 hover:text-foreground hover:underline">
								继续浏览
							</DrawerClose>
							<Link to="/bets" className="text-primary underline-offset-2 hover:underline">
								去投注页锁定 →
							</Link>
						</div>
					</DrawerFooter>
				</DrawerContent>
			</Drawer>
		</AppShell>
	);
}
