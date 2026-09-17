import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useState } from "react";
import { createBet, fetchFixtureResearch } from "../api/goalx";
import { AppShell } from "../components/app-shell";
import { EmptyState } from "../components/empty-state";
import { GlossaryTerm } from "../components/glossary-term";
import {
	EligibilityBadge,
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
	oddsText,
	type PickableFixture,
	SELECTIONS,
	type Selection,
} from "../components/had-quote-ui";
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
 * 票 wb-02：单场研究页 `/fixtures/$id`。研究动线 = 公司间定价分歧 → 去水共识 →
 * 模型概率/EV → 资格判定 → 选注建注：
 * - 赔率明细表逐书 H/D/A + 捕获时间，单书隐含概率与共识偏差 ≥5 个百分点琥珀高亮
 *   （方向文字标注，红涨绿跌只用于 EV 数字）；
 * - 共识与列表页同口径（同一后端字段）；模型概率/EV 缺失诚实显示"暂无模型预测"；
 * - 基本面与 AI 研判区块 = EmptyState not-available（"随 M3 到来"），不空缺不误导；
 * - 选注交互复用场次页模式（选注篮 + 建议建立，服务器校验唯一权威）。
 */

/** 单书相对共识的偏差高亮阈值（百分点）：|归一隐含 − 共识| ≥ 该值标琥珀。 */
const BOOK_DEVIATION_PP = 5;

function bookName(source: string): string {
	return source.replace(/^odds_api:/, "");
}

/** 单书归一化隐含概率（去水位按等比例归一——只用于偏差方向判读，不做共识）。 */
function normalizedImplied(odds: number): number {
	return odds > 1 ? 1 / odds : 0;
}

function pct(value: number | null | undefined, digits = 0): string {
	return value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)}%`;
}

export function FixtureResearchPage() {
	const { id } = useParams({ from: "/fixtures/$id" });
	const fixtureId = Number(id);
	const research = useQuery({
		queryKey: ["fixture-research", fixtureId],
		queryFn: () => fetchFixtureResearch(fixtureId),
		retry: false,
	});
	const queryClient = useQueryClient();
	const [legs, setLegs] = useState<Leg[]>([]);
	const [basketOpen, setBasketOpen] = useState(false);
	const [stake, setStake] = useState("100");
	const [strategyVersion, setStrategyVersion] = useState("");
	const [mode, setMode] = useState<"paper" | "live">("paper");
	const [message, setMessage] = useState<string | null>(null);
	const now = Date.now();

	const data = research.data;
	const canPick = data ? isPickable(data, now) : false;
	const combinedOdds = legs.reduce((acc, leg) => acc * leg.odds, 1);

	/** 规则前置（与场次页同规则）：同场换选=替换；上限=行内提示；非单固首腿=提示。 */
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

	const kickoff = data ? kickoffInfo(data.kickoff_utc, now) : undefined;
	const jcAge = data ? minutesAgo(data.jc_updated_at, now) : null;

	return (
		<AppShell title="场次研究">
			<div className="pb-24">
				<nav className="mb-4 text-sm" aria-label="返回">
					<Link
						to="/fixtures"
						className="text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
					>
						← 返回场次
					</Link>
				</nav>

				{message ? (
					<div
						role="status"
						data-testid="fixtures-message"
						className="fixed inset-x-0 bottom-16 z-50 mx-auto w-fit max-w-[min(92vw,42rem)] rounded-md bg-muted px-3 py-1.5 text-sm text-foreground shadow-sm"
					>
						{message}
					</div>
				) : null}

				{research.isPending ? (
					<div data-testid="research-loading" className="space-y-4">
						<span className="sr-only">加载研究视图…</span>
						<div className="h-8 w-72 animate-pulse rounded bg-muted" />
						<div className="h-40 animate-pulse rounded-lg bg-muted" />
						<div className="h-24 animate-pulse rounded-lg bg-muted" />
					</div>
				) : null}

				{research.isError
					? // 404（场次不存在/旧后端无该端点）与后端不可用分开表述，都给返回动作
						(() => {
							const status =
								typeof research.error === "object" && research.error !== null && "status" in research.error
									? Number((research.error as { status: unknown }).status)
									: 0;
							return status === 404 ? (
								<EmptyState
									variant="no-data"
									message="找不到这场研究视图。"
									hint="场次可能不存在、已下架，或后端版本较旧（研究端点未部署）。"
									action={{ label: "返回场次", onClick: () => window.history.back() }}
								/>
							) : (
								<EmptyState
									variant="backend-unavailable"
									message="连不上后端，研究视图加载失败。"
									hint={
										<>
											用 <code>task server</code> 启动 API 后重试。
										</>
									}
									action={{ label: "重试", onClick: () => void research.refetch() }}
								/>
							);
						})()
					: null}

				{data ? (
					<div data-testid="research-page" className="space-y-6">
						{/* 开赛信息 + 资格判定（研究页头部） */}
						<header className="rounded-lg border border-border p-4" data-testid="research-header">
							<div className="flex flex-wrap items-baseline justify-between gap-2">
								<h2 className="text-base font-medium">
									{data.home_team} <span className="text-muted-foreground">vs</span> {data.away_team}
									<span className="ml-2 text-sm font-normal text-muted-foreground">
										{data.match_code} · {data.competition}
										{data.tier === "tier1" ? (
											<span className="ml-1 rounded border border-border px-1 text-xs text-muted-foreground">T1</span>
										) : null}
									</span>
								</h2>
								<EligibilityBadge quote={data.had_quote} />
							</div>
							<p
								className={`mt-1 text-sm ${TABULAR_NUMS} ${kickoff?.urgent ? "font-medium text-warning" : "text-muted-foreground"}`}
							>
								开赛 {localTime(data.kickoff_utc)}（北京时间）· {kickoff?.text ?? "—"}
								{jcAge === null ? null : (
									<span className={jcAge > 30 ? "text-warning" : ""}> · 竞彩调盘 {jcAge}分钟前</span>
								)}
								{data.joined ? null : " · 未接入欧赔"}
							</p>
						</header>

						{/* 赔率明细：竞彩 + 逐书 H/D/A + 捕获时间；偏差 ≥5pp 琥珀高亮 + 方向 */}
						<section aria-labelledby="research-books-heading">
							<h3 id="research-books-heading" className="mb-2 text-sm font-medium">
								赔率明细{" "}
								<span className="font-normal text-muted-foreground">
									<GlossaryTerm id="books">books</GlossaryTerm>
									<span className={TABULAR_NUMS}> {(data.books ?? []).length}</span> 家 · 与共识偏差 ≥5 个百分点琥珀标注
								</span>
							</h3>
							{(data.books ?? []).length === 0 ? (
								<p className="rounded-lg border border-dashed border-border p-4 text-sm text-muted-foreground">
									暂无欧赔逐书报价——该场未接入欧赔或尚无快照，共识与偏差不可判读。
								</p>
							) : (
								<div className="overflow-x-auto rounded-lg border border-border" data-testid="research-books">
									<Table data-density="compact">
										<TableHeader>
											<TableRow>
												<TableHead>来源</TableHead>
												<TableHead>主胜</TableHead>
												<TableHead>平</TableHead>
												<TableHead>客胜</TableHead>
												<TableHead>捕获</TableHead>
											</TableRow>
										</TableHeader>
										<TableBody>
											{/* 竞彩参考行：我们的下注价，置顶对照 */}
											<TableRow data-testid="research-jc-row">
												<TableCell className="font-medium">竞彩（投注价）</TableCell>
												{SELECTIONS.map((sel) => (
													<TableCell key={sel} className={`${TABULAR_NUMS}`}>
														{oddsText(data.jc_odds[sel])}
													</TableCell>
												))}
												<TableCell className="text-xs text-muted-foreground">
													{data.jc_updated_at ? localTime(data.jc_updated_at) : "—"}
												</TableCell>
											</TableRow>
											{(data.books ?? []).map((book) => (
												<TableRow key={book.book} data-testid="research-book-row">
													<TableCell className="whitespace-nowrap">{bookName(book.book)}</TableCell>
													{SELECTIONS.map((sel) => {
														const odds = book.odds[sel];
														const consensusProb = data.consensus?.probability[sel];
														const dev =
															odds !== null &&
															odds !== undefined &&
															consensusProb !== null &&
															consensusProb !== undefined
																? normalizedImplied(odds) - consensusProb
																: null;
														const flagged = dev !== null && Math.abs(dev * 100) >= BOOK_DEVIATION_PP;
														return (
															<TableCell
																key={sel}
																className={TABULAR_NUMS}
																data-testid={`book-odds-${sel}`}
																title={
																	flagged && dev !== null
																		? `隐含 ${pct(normalizedImplied(odds ?? 0), 1)} vs 共识 ${pct(consensusProb ?? undefined, 1)}：偏${dev > 0 ? "高" : "低"} ${Math.abs(dev * 100).toFixed(1)} 个百分点`
																		: undefined
																}
															>
																{odds === null || odds === undefined ? (
																	"—"
																) : flagged ? (
																	<span className="rounded bg-warning/10 px-1 text-foreground">
																		{odds.toFixed(2)}
																		<span className="ml-1 text-xs">{dev && dev > 0 ? "↑" : "↓"}</span>
																	</span>
																) : (
																	odds.toFixed(2)
																)}
															</TableCell>
														);
													})}
													<TableCell className={`${TABULAR_NUMS} text-xs text-muted-foreground`}>
														{book.captured_at ? localTime(book.captured_at) : "—"}
													</TableCell>
												</TableRow>
											))}
										</TableBody>
									</Table>
								</div>
							)}
						</section>

						{/* 共识 + 模型并排：研究页的两个基准 */}
						<div className="grid gap-4 lg:grid-cols-2">
							<section
								aria-labelledby="research-consensus-heading"
								className="rounded-lg border border-border p-4"
								data-testid="research-consensus"
							>
								<h3 id="research-consensus-heading" className="mb-2 text-sm font-medium">
									<GlossaryTerm id="eu-consensus">去水共识</GlossaryTerm>
								</h3>
								{data.consensus ? (
									<>
										<p className={`${TABULAR_NUMS} text-sm`}>
											{SELECTIONS.map((sel) => (
												<span key={sel} className="mr-3">
													{SELECTION_LABELS[sel]} {pct(data.consensus?.probability[sel], 1)}
												</span>
											))}
										</p>
										<p className="mt-1 text-xs text-muted-foreground">
											<GlossaryTerm id="books">books</GlossaryTerm> {data.consensus.books} 家三向均价 → Shin 去水；
											{data.consensus.books < 3 ? " 样本少（<3），共识可信度下降。" : " 共识是市场对真实概率的估计。"}
										</p>
									</>
								) : (
									<p className="text-sm text-muted-foreground">共识不可得——三向报价不全或无欧赔。</p>
								)}
							</section>

							<section
								aria-labelledby="research-model-heading"
								className="rounded-lg border border-border p-4"
								data-testid="research-model"
							>
								<h3 id="research-model-heading" className="mb-2 text-sm font-medium">
									<GlossaryTerm id="model-prob">模型概率与模型 EV</GlossaryTerm>
								</h3>
								{data.model ? (
									<>
										<p className={`${TABULAR_NUMS} text-sm`}>
											{SELECTIONS.map((sel) => (
												<span key={sel} className="mr-3">
													{SELECTION_LABELS[sel]} {pct(data.model?.probability[sel], 1)}
												</span>
											))}
										</p>
										<p className={`${TABULAR_NUMS} mt-1 text-sm`}>
											<span className="mr-1.5 text-muted-foreground">模型 EV</span>
											{data.model.ev
												? SELECTIONS.map((sel) => {
														const value = data.model?.ev?.[sel];
														return value === null || value === undefined ? (
															<span key={sel} className="mr-1.5 text-muted-foreground">
																—
															</span>
														) : (
															<span key={sel} className={`mr-1.5 ${evClass(value)}`}>
																{SELECTION_LABELS[sel]} {evText(value)}
															</span>
														);
													})
												: "—"}
										</p>
										<p className="mt-1 text-xs text-muted-foreground">
											{data.model.model_version} · {localTime(data.model.issued_at)}（UTC）发出 · 模型 EV = 模型概率 ×
											竞彩价 − 1
										</p>
									</>
								) : (
									<p className="text-sm text-muted-foreground" data-testid="research-model-missing">
										暂无模型预测——模型每日对五大联赛在售场次生成，其余联赛暂未覆盖。
									</p>
								)}
							</section>
						</div>

						{/* 就地选注（复用场次页交互）：竞彩三向 + 选注篮 */}
						<section aria-labelledby="research-pick-heading" className="rounded-lg border border-border p-4">
							<h3 id="research-pick-heading" className="mb-2 text-sm font-medium">
								选注{" "}
								<span className="font-normal text-muted-foreground">竞彩 H/D/A · 就地建建议（服务器校验唯一权威）</span>
							</h3>
							<div className="flex items-center gap-2">
								{SELECTIONS.map((sel) => (
									<OddsButton
										key={sel}
										fixture={data}
										selection={sel}
										value={data.jc_odds[sel]}
										selected={legs.some((leg) => leg.fixture_id === data.fixture_id && leg.selection === sel)}
										disabled={!canPick}
										onPick={pick}
										testid={`pick-${data.fixture_id}-${sel}`}
										size="md"
									/>
								))}
								{!canPick ? (
									<span className="ml-2 text-xs text-muted-foreground">停售/已开赛/证据未知时不可选注</span>
								) : null}
							</div>
						</section>

						{/* 基本面与 AI 研判：留位标注（EmptyState not-available，随 M3 到来） */}
						<div className="grid gap-4 lg:grid-cols-2">
							<section aria-labelledby="research-fundamentals-heading" data-testid="research-fundamentals">
								<h3 id="research-fundamentals-heading" className="sr-only">
									基本面
								</h3>
								<EmptyState
									variant="not-available"
									message="基本面研究尚未接入。"
									hint="伤停/阵型/赛程密度等基本面包块随 M3 数据线到来——当前不做空白或误导。"
								/>
							</section>
							<section aria-labelledby="research-ai-heading" data-testid="research-ai">
								<h3 id="research-ai-heading" className="sr-only">
									AI 研判
								</h3>
								<EmptyState
									variant="not-available"
									message="AI 研判尚未接入。"
									hint="模型×市场对照的证据总结随 M3 LLM 线到来——落地前以本页数字为准。"
								/>
							</section>
						</div>
					</div>
				) : null}
			</div>

			{/* 底部常驻选注条 + 右侧 Drawer（复用场次页模式：常驻可见已选腿） */}
			<div className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-background/95 backdrop-blur">
				<div className="mx-auto flex max-w-6xl items-center gap-3 px-6 py-3">
					<span className="text-sm">
						选注篮{" "}
						<span className={`${TABULAR_NUMS} font-medium`} data-testid="basket-count">
							{legs.length}/{MAX_LEGS}
						</span>
					</span>
					<span
						className={`${TABULAR_NUMS} hidden text-xs text-muted-foreground sm:inline`}
						data-testid="basket-summary"
					>
						{legs.map((leg) => `${leg.match_code} ${SELECTION_LABELS[leg.selection]}`).join(" × ")}
						{legs.length === MAX_LEGS ? ` · 组合赔率 ${combinedOdds.toFixed(2)}` : ""}
					</span>
					<button
						type="button"
						data-testid="basket-open"
						className="ml-auto rounded-md border border-border bg-background px-3 py-1.5 text-sm font-medium transition-colors hover:bg-muted"
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
							<p className="text-sm text-muted-foreground">未选择。点上方竞彩赔率加入。</p>
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
