import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { fetchBankroll, fetchFixtureResearch } from "../api/goalx";
import { AppShell } from "../components/app-shell";
import { EmptyState } from "../components/empty-state";
import { EvidenceChainSection } from "../components/evidence-chain";
import { GlossaryTerm } from "../components/glossary-term";
import { BasketBar, BasketDrawer, useHadBasket } from "../components/had-basket";
import {
	EligibilityBadge,
	evClass,
	evText,
	isPickable,
	kickoffInfo,
	localTime,
	minutesAgo,
	OddsButton,
	oddsText,
	SELECTIONS,
} from "../components/had-quote-ui";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { pct, SELECTION_LABELS, TABULAR_NUMS } from "../lib/ui";

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

export function FixtureResearchPage() {
	const { id } = useParams({ from: "/fixtures/$id" });
	const fixtureId = Number(id);
	const research = useQuery({
		queryKey: ["fixture-research", fixtureId],
		queryFn: () => fetchFixtureResearch(fixtureId),
		retry: false,
	});
	// 票 wb-06：选注篮建议仓位需要 bankroll（读取失败时建议块诚实降级）
	const bankrollQuery = useQuery({ queryKey: ["bankroll"], queryFn: fetchBankroll });
	const now = Date.now();

	const data = research.data;
	const canPick = data ? isPickable(data, now) : false;
	// 选注篮（review-20260928 票 06：接 useHadBasket，legEv 注入研究页共识口径 =
	// 去水共识概率 × 所选竞彩价 − 1，与列表页 fixture.ev 同语义不同数据源）
	const basket = useHadBasket(now, [], (leg) => {
		const probability = data?.consensus?.probability[leg.selection];
		return probability === null || probability === undefined ? null : probability * leg.odds - 1;
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

				{basket.message ? (
					<div
						role="status"
						data-testid="fixtures-message"
						className="fixed inset-x-0 bottom-16 z-50 mx-auto w-fit max-w-[min(92vw,42rem)] rounded-md bg-muted px-3 py-1.5 text-sm text-foreground shadow-sm"
					>
						{basket.message}
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
											{data.consensus.low_confidence ? (
												// 票 39：共识分母护栏命中（books<4）——琥珀低置信（接词条），
												// 与 few_books 合并为一个显示位；共识概率本身不变
												<span
													className="rounded bg-warning/10 px-1.5 py-0.5 text-foreground"
													data-testid="consensus-low-confidence"
												>
													<GlossaryTerm id="low-confidence" />
												</span>
											) : (
												" 共识是市场对真实概率的估计。"
											)}
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
										selected={basket.legs.some((leg) => leg.fixture_id === data.fixture_id && leg.selection === sel)}
										disabled={!canPick}
										onPick={basket.pick}
										testid={`pick-${data.fixture_id}-${sel}`}
										size="md"
									/>
								))}
								{!canPick ? (
									<span className="ml-2 text-xs text-muted-foreground">停售/已开赛/证据未知时不可选注</span>
								) : null}
							</div>
						</section>

						{/* 证据链（票 14 V2：三轨对照+JS 徽章+情报时间线+复核结论+追问占位+盲评入口） */}
						<EvidenceChainSection fixtureId={fixtureId} />
					</div>
				) : null}
			</div>

			{/* 底部常驻选注条 + 右侧 Drawer（共享件，review-20260928 票 06 收编） */}
			<BasketBar basket={basket} emptyHint="未选择。点上方竞彩赔率加入。" />
			<BasketDrawer
				basket={basket}
				bankroll={bankrollQuery.data?.balance ?? (bankrollQuery.isError ? null : 0)}
				emptyHint="未选择。点上方竞彩赔率加入。"
			/>
		</AppShell>
	);
}
