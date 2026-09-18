import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { createBet, fetchBankroll, fetchTodayFixtures, type TodayFixture } from "../api/goalx";
import { AppShell } from "../components/app-shell";
import { EmptyState } from "../components/empty-state";
import { GlossaryTerm } from "../components/glossary-term";
import {
	EligibilityBadge,
	EligibleCard,
	evClass,
	evText,
	isPickable,
	kickoffInfo,
	type Leg,
	localTime,
	MAX_LEGS,
	makeLeg,
	minutesAgo,
	OddsButton,
	type PickableFixture,
	SELECTIONS,
	type Selection,
} from "../components/had-quote-ui";
import { parlayAdviceInput, StakeAdviceNote } from "../components/stake-advice";
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
import { addDays, beijingBusinessDate, dayLabel, errorText, SELECTION_LABELS, TABULAR_NUMS } from "../lib/ui";

/**
 * 票 wb-01：今日页升级为"场次"页（票 14 的信息分层与编码硬约束全部继承）。
 * 展示今天起 3 天的在售场次：日期 Tab（默认今天）按行内 business_date 分组，
 * 空窗日诚实显示"该日暂无"。信息分层：可投卡片置顶 + 全量 compact 对照表；
 * 编码硬约束：EV 数字永远只按正负红绿（诊断量非机会信号）、|EV|≥5% 独立琥珀
 * 徽章、资格徽章蓝=可投/红=拒绝+原因/灰框=证据未知、新鲜度>30 分钟琥珀、
 * T1 中性灰；选注篮 = 底部常驻条 + 右侧 Drawer（跨日可选，服务器校验唯一权威）。
 * 票 18 增量：表头资格/欧共识/EV/books 与单固徽章接词典 tooltip。
 * 票 wb-03：可投卡片下沉到 had-quote-ui（玩法页推荐流复用），日期工具下沉 lib/ui。
 */

/** 场次窗口天数（票 wb-01：今天起 3 天，提前研究不赶当天截止）。 */
const FIXTURES_WINDOW_DAYS = 3;

/**
 * 行的归属业务日。行内 ``business_date`` 缺失（旧后端/旧快照）时按今天兜底——
 * 双路径 e2e 兼容常驻旧后端，数据不因字段缺失而消失或炸页。
 */
function fixtureDay(fixture: TodayFixture, now: number): string {
	return fixture.business_date ?? beijingBusinessDate(now);
}

/** Tab 的日期清单：今天起 3 天 ∪ 数据实际出现的业务日（时钟偏差时不丢单）。 */
function dayTabs(fixtures: TodayFixture[], now: number): string[] {
	const today = beijingBusinessDate(now);
	const tabs = new Set([today, addDays(today, 1), addDays(today, 2)]);
	for (const fixture of fixtures) {
		tabs.add(fixtureDay(fixture, now));
	}
	return [...tabs].sort();
}

function EvSpan({ value }: { value: number | null | undefined }) {
	if (value === null || value === undefined) {
		return <span className="mr-1.5 text-muted-foreground">—</span>;
	}
	return <span className={`mr-1.5 ${evClass(value)}`}>{evText(value)}</span>;
}

export function FixturesPage() {
	const fixturesQuery = useQuery({
		queryKey: ["fixtures-window", FIXTURES_WINDOW_DAYS],
		queryFn: () => fetchTodayFixtures(undefined, FIXTURES_WINDOW_DAYS),
	});
	// 票 wb-06：选注篮建议仓位需要 bankroll（读取失败时建议块诚实降级）
	const bankrollQuery = useQuery({ queryKey: ["bankroll"], queryFn: fetchBankroll });
	const queryClient = useQueryClient();
	const [selectedDay, setSelectedDay] = useState<string | null>(null);
	const [legs, setLegs] = useState<Leg[]>([]);
	const [basketOpen, setBasketOpen] = useState(false);
	const [stake, setStake] = useState("100");
	const [strategyVersion, setStrategyVersion] = useState("");
	const [mode, setMode] = useState<"paper" | "live">("paper");
	const [message, setMessage] = useState<string | null>(null);
	const now = Date.now();

	const fixtures = fixturesQuery.data ?? [];
	const tabs = dayTabs(fixtures, now);
	// 默认今天；数据到达前 selectedDay 为空 → 落到第一个 Tab（即今天）
	const activeDay =
		selectedDay !== null && tabs.includes(selectedDay) ? selectedDay : (tabs[0] ?? beijingBusinessDate(now));
	const dayFixtures = fixtures.filter((fixture) => fixtureDay(fixture, now) === activeDay);
	const eligible = dayFixtures.filter((fixture) => isPickable(fixture, now));
	const joinedCount = dayFixtures.filter((fixture) => fixture.joined).length;
	const jcAge = dayFixtures
		.map((fixture) => minutesAgo(fixture.jc_updated_at, now))
		.filter((age): age is number => age !== null)
		.sort((a, b) => a - b)
		.at(0);
	const combinedOdds = legs.reduce((acc, leg) => acc * leg.odds, 1);

	// 建议仓位输入（票 wb-06）：1 腿=单关口径；2 腿=串关联合口径（整注一个 Kelly，不分腿）
	const legEv = (leg: Leg): number | null => {
		const fixture = fixtures.find((row) => row.fixture_id === leg.fixture_id);
		return fixture?.ev?.[leg.selection] ?? null;
	};
	const basketAdvice =
		legs.length === 2
			? {
					...parlayAdviceInput(legs.map((leg) => ({ ev: legEv(leg), odds: leg.odds }))),
					note: "串关注额=单关口径：联合 EV/联合赔率整注计算（腿间独立性假设），不分腿",
				}
			: legs.length === 1 && legs[0] !== undefined
				? { ev: legEv(legs[0]), odds: legs[0].odds, note: "单关口径：共识 EV（欧共识×竞彩价−1）" }
				: null;

	/** 规则前置（票 04）：同场换选=替换+提示；2串1 上限=行内提示；非单固首腿=提示；停售/已开赛=按钮禁用。 */
	function pick(fixture: PickableFixture, selection: Selection, odds: number) {
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
		<AppShell title="场次">
			<div className="pb-24">
				<header className="mb-5">
					<div className="flex flex-wrap items-baseline justify-between gap-3">
						<p className="text-sm text-muted-foreground">
							今天起 3 天的在售场次。竞彩价对照欧洲共识（Shin 去水）。EV
							是市场共识的诊断量，不是机会信号；红涨绿跌，|EV|≥5% 标偏差。
						</p>
						{/* 页头全局数据健康（票 04）：竞彩报价年龄（可推导）+ 欧赔接入覆盖（按所选日） */}
						<div className="flex items-center gap-3 text-xs" data-testid="fixtures-data-health">
							{jcAge === undefined ? (
								<span className="text-muted-foreground">竞彩报价 —</span>
							) : (
								<span className={jcAge > 30 ? "text-warning" : "text-muted-foreground"}>竞彩报价 {jcAge}分钟前</span>
							)}
							<span className="text-muted-foreground">
								欧赔 已接入 {joinedCount}/{dayFixtures.length} 场
							</span>
						</div>
					</div>
					{/* 配色图例行（票 04 定稿；票 39 琥珀警示含共识低置信） */}
					<p className="mt-1 text-xs text-muted-foreground">
						配色：红 = 正向 EV · 绿 = 负向 EV · 琥珀 = 数据警示（过期/共识低置信/偏差） · 蓝 = 可投资格
					</p>
				</header>

				{message ? (
					<div
						role="status"
						data-testid="fixtures-message"
						className="fixed inset-x-0 bottom-16 z-50 mx-auto w-fit max-w-[min(92vw,42rem)] rounded-md bg-muted px-3 py-1.5 text-sm text-foreground shadow-sm"
					>
						{message}
					</div>
				) : null}

				{fixturesQuery.isPending ? (
					// 加载骨架（票 14 验收）：形状与信息分层同构，sr-only 文本给读屏
					<div data-testid="fixtures-loading" className="space-y-4">
						<span className="sr-only">加载场次…</span>
						<div className="h-5 w-64 animate-pulse rounded bg-muted" />
						<div className="grid gap-3 sm:grid-cols-2">
							<div className="h-36 animate-pulse rounded-lg bg-muted" />
							<div className="h-36 animate-pulse rounded-lg bg-muted" />
						</div>
						<div className="h-44 animate-pulse rounded-lg bg-muted" />
					</div>
				) : null}

				{fixturesQuery.isError ? (
					// 票 13：后端不可用态——保留启动/灌数指引 + 重试（服务器校验仍是唯一权威）
					<EmptyState
						variant="backend-unavailable"
						message="连不上后端，场次加载失败。"
						hint={
							<>
								用 <code>task server</code> 启动 API；首次使用先跑 <code>task ingest-jingcai</code> 拉取竞彩数据。
							</>
						}
						action={{ label: "重试", onClick: () => void fixturesQuery.refetch() }}
					/>
				) : null}

				{fixturesQuery.data && fixturesQuery.data.length === 0 ? (
					// 票 13：无数据态——为何空 + 何时有 + 刷新
					<EmptyState
						variant="no-data"
						message="3 天内无在售场次。"
						hint={
							<>
								场次通常在竞彩当日开售前更新；刚搭好环境先跑 <code>task ingest-jingcai</code>。
							</>
						}
						action={{ label: "刷新", onClick: () => void fixturesQuery.refetch() }}
					/>
				) : null}

				{fixturesQuery.data && fixturesQuery.data.length > 0 ? (
					<>
						{/* 日期 Tab（票 wb-01）：默认今天；空窗日保留 Tab（计数 0）诚实可见 */}
						<fieldset className="mb-5 flex flex-wrap gap-1.5" data-testid="fixtures-day-tabs">
							<legend className="sr-only">业务日</legend>
							{tabs.map((day) => {
								const count = fixtures.filter((fixture) => fixtureDay(fixture, now) === day).length;
								return (
									<button
										key={day}
										type="button"
										data-testid={`fixtures-day-tab-${day}`}
										aria-pressed={day === activeDay}
										className={`rounded-md border px-3 py-1.5 text-sm transition-colors ${
											day === activeDay
												? "border-primary bg-primary font-medium text-primary-foreground"
												: "border-border text-muted-foreground hover:bg-muted hover:text-foreground"
										}`}
										onClick={() => setSelectedDay(day)}
									>
										{dayLabel(day, now)}
										<span className={`${TABULAR_NUMS} ml-1.5 text-xs`}>{count}</span>
									</button>
								);
							})}
						</fieldset>

						{dayFixtures.length === 0 ? (
							// 空窗日（票 wb-01）：后两日开售前常无数据——诚实占位，不静默隐藏 Tab
							<p
								data-testid="fixtures-day-empty"
								className="rounded-lg border border-dashed border-border p-6 text-center text-sm text-muted-foreground"
							>
								{dayLabel(activeDay, now)}暂无在售场次——竞彩按日开售，后两日场次随开售逐步入库。
							</p>
						) : (
							<>
								<section aria-labelledby="fixtures-eligible-heading" className="mb-8" data-testid="fixtures-eligible">
									<div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
										<h2 id="fixtures-eligible-heading" className="text-sm font-medium">
											可投场次 <span className={`${TABULAR_NUMS} text-muted-foreground`}>{eligible.length}</span>
											<span className="ml-2 text-xs font-normal text-muted-foreground">
												可投 = 证据链完整且新鲜；不等于必成交
											</span>
										</h2>
										<span className="text-xs text-muted-foreground" data-testid="fixtures-not-eligible-count">
											{dayFixtures.length - eligible.length} 场不可投（停售/已开赛/证据未知）
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

								<section aria-labelledby="fixtures-all-heading">
									<h2 id="fixtures-all-heading" className="mb-3 text-sm font-medium">
										全部场次 <span className={`${TABULAR_NUMS} text-muted-foreground`}>{dayFixtures.length}</span>
									</h2>
									<div className="overflow-x-auto rounded-lg border border-border">
										{/* 票 04 表格结构：资格列第 4 位（筛行信号前置）；compact 密度，1440 无横滚 */}
										<Table data-density="compact">
											<TableHeader>
												<TableRow>
													<TableHead>时间</TableHead>
													<TableHead>编号</TableHead>
													<TableHead>对阵</TableHead>
													{/* 票 18：表头指标名接词典 tooltip（悬停/聚焦看定义+判读方向） */}
													<TableHead>
														<GlossaryTerm id="eligibility">资格</GlossaryTerm>
													</TableHead>
													<TableHead>竞彩 H/D/A</TableHead>
													<TableHead>
														<GlossaryTerm id="eu-consensus">欧共识</GlossaryTerm>
													</TableHead>
													<TableHead>
														<GlossaryTerm id="ev">EV H/D/A</GlossaryTerm>
													</TableHead>
													<TableHead className="text-center">
														<GlossaryTerm id="books">books</GlossaryTerm>
													</TableHead>
												</TableRow>
											</TableHeader>
											<TableBody>
												{dayFixtures.map((fixture) => {
													const canPick = isPickable(fixture, now);
													const cd = kickoffInfo(fixture.kickoff_utc, now);
													const jcAgeRow = minutesAgo(fixture.jc_updated_at, now);
													return (
														// 停售/已开赛/证据未知 → 禁用 + 行弱化（票 04 规则前置；弱化用背景 wash——
														// opacity 会把整行文字对比压到 AA 以下，过不了 axe 零 serious 基调）
														<TableRow
															key={fixture.fixture_id}
															data-testid="fixtures-row"
															className={canPick ? "" : "bg-muted/50"}
														>
															<TableCell className={`${TABULAR_NUMS} whitespace-nowrap text-muted-foreground`}>
																{localTime(fixture.kickoff_utc)}
																{cd.passed ? <span className="ml-1 text-warning">已开赛</span> : null}
																<br />
																<span className="text-xs">
																	{fixture.competition}
																	{fixture.tier === "tier1" ? (
																		<span className="ml-1 rounded border border-border px-1 text-muted-foreground">
																			T1
																		</span>
																	) : null}
																	{jcAgeRow === null ? null : (
																		<span className={jcAgeRow > 30 ? "text-warning" : ""}> · 彩 {jcAgeRow}分钟前</span>
																	)}
																</span>
															</TableCell>
															<TableCell className="whitespace-nowrap">{fixture.match_code}</TableCell>
															<TableCell className="whitespace-nowrap font-medium">
																<Link
																	to="/fixtures/$id"
																	params={{ id: String(fixture.fixture_id) }}
																	className="text-foreground underline-offset-2 hover:text-primary hover:underline"
																	data-testid={`fixtures-link-${fixture.fixture_id}`}
																>
																	{fixture.home_team} vs {fixture.away_team}
																</Link>
															</TableCell>
															<TableCell>
																<EligibilityBadge quote={fixture.had_quote} />
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
																{fixture.ev
																	? SELECTIONS.map((sel) => <EvSpan key={sel} value={fixture.ev?.[sel]} />)
																	: "—"}
															</TableCell>
															<TableCell className={`${TABULAR_NUMS} text-center`}>
																{fixture.books ? (
																	fixture.flags?.includes("low_confidence") ? (
																		// 票 39：共识分母 <4——books 数字挂琥珀低置信
																		//（行内单显示位，接词条；few_books 区间已被覆盖）
																		<span
																			className="rounded bg-warning/10 px-1.5 py-0.5 text-foreground"
																			data-testid="books-low-confidence"
																		>
																			{fixture.books} <GlossaryTerm id="low-confidence" />
																		</span>
																	) : (
																		fixture.books
																	)
																) : (
																	"—"
																)}
															</TableCell>
														</TableRow>
													);
												})}
											</TableBody>
										</Table>
									</div>
								</section>
							</>
						)}
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
						{basketAdvice ? (
							<div className="mt-3">
								<StakeAdviceNote
									mode={mode}
									bankroll={bankrollQuery.data?.balance ?? (bankrollQuery.isError ? null : 0)}
									ev={basketAdvice.ev}
									odds={basketAdvice.odds}
									note={basketAdvice.note}
									testid="basket-stake-advice"
								/>
							</div>
						) : null}
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
