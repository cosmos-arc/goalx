import { useQuery } from "@tanstack/react-query";
import { fetchBankroll, fetchTodayFixtures } from "../api/goalx";
import { AppShell } from "../components/app-shell";
import { EmptyState } from "../components/empty-state";
import { GlossaryTerm } from "../components/glossary-term";
import { BasketBar, BasketDrawer, useHadBasket } from "../components/had-basket";
import { EligibleCard, evClass, evText, isPickable, MAX_LEGS, makeLeg } from "../components/had-quote-ui";
import { MarketTabs } from "../components/market-tabs";
import { parlayAdviceInput, StakeAdviceNote } from "../components/stake-advice";
import { StatusToast } from "../components/status-toast";
import { buildHadCombo, HAD_COMBO_CONFIG, rankFixturesForFeed } from "../lib/combo-engine";
import { bankrollKey, todayFixturesKey } from "../lib/query-keys";
import { dayNoteOf, SELECTION_LABELS, TABULAR_NUMS } from "../lib/ui";

/**
 * 票 wb-03：胜平负玩法页 `/markets/had`——玩法轴第一页。
 * - 推荐流：今天起 3 天窗口的全部场次按 EV×置信排序（卡片复用场次页组件
 *   与语义编码；无正 EV 场沉底按开赛时间，非可投卡按钮禁用 + 资格徽章）；
 * - 组合头部卡：组合引擎 v1 纯函数产出（腿列表/每腿建议注额/注数/预期收益/
 *   约束说明），EV≤0 不入选，空窗诚实占位；一键带入就地选注篮（复用场次页
 *   常驻条 + Drawer 模式，服务器校验唯一权威）；
 * - bankroll 取真金余额（GET /bankroll）；纸面期组合一律 flat 档建议
 *   （2%，区间 1–5%），仓位细化（¼Kelly 等）归票 06，本页不做。
 */

/** 场次窗口与场次页同宽（同一 queryKey 共享缓存——键登记在 lib/query-keys）。 */
const MARKET_WINDOW_DAYS = 3;

export function MarketHadPage() {
	const fixturesQuery = useQuery({
		queryKey: todayFixturesKey(MARKET_WINDOW_DAYS),
		queryFn: () => fetchTodayFixtures(undefined, MARKET_WINDOW_DAYS),
	});
	const bankrollQuery = useQuery({ queryKey: bankrollKey, queryFn: () => fetchBankroll() });
	const now = Date.now();
	const fixtures = fixturesQuery.data ?? [];
	const basket = useHadBasket(now, fixtures);
	const { legs, message, pick } = basket;

	// 推荐流只依赖场次（资金池未就绪也能先看排序）；组合注额需 bankroll 到位才给建议
	const feedOrder = fixturesQuery.data !== undefined ? rankFixturesForFeed(fixtures) : [];
	const combo =
		fixturesQuery.data !== undefined && bankrollQuery.isSuccess
			? buildHadCombo({
					candidates: fixtures.map((fixture) => ({ fixture, pickable: isPickable(fixture, now) })),
					bankroll: bankrollQuery.data.balance ?? 0,
				})
			: null;
	// 组合卡建议仓位（票 wb-06）：1 注=单关口径；2 注=按"一注 2串1"整注联合口径
	const comboAdvice =
		combo && combo.picks.length === 2
			? parlayAdviceInput(combo.picks.map((pick) => ({ ev: pick.ev, odds: pick.odds })))
			: { ev: combo?.picks[0]?.ev ?? null, odds: combo?.picks[0]?.odds ?? null };

	/** 一键带入：组合腿就地进本页选注篮并展开（组合引擎已保证同场不重复、≤2 腿）。 */
	function applyCombo() {
		if (combo === null || combo.picks.length === 0) {
			return;
		}
		basket.setLegs(combo.picks.map((pickEntry) => makeLeg(pickEntry.fixture, pickEntry.selection, pickEntry.odds)));
		basket.setStake(String(combo.picks[0]?.stake ?? HAD_COMBO_CONFIG.stake.minStakeCny));
		basket.setMessage(
			combo.picks.length === MAX_LEGS
				? "已带入 2 条推荐——选注篮按一注 2串1 提交；组合卡预期收益按两条独立单关口径（EV×注额）合计"
				: `已带入 1 条推荐（建议注额 ¥${combo.picks[0]?.stake.toFixed(2)}，预期收益按 EV×注额 共识口径）`,
		);
		basket.setBasketOpen(true);
	}

	return (
		<AppShell title="胜平负">
			<div className="pb-24">
				<MarketTabs />
				<header className="mb-5">
					<div className="flex flex-wrap items-baseline justify-between gap-3">
						<p className="text-sm text-muted-foreground">
							今天起 3 天全部场次的胜平负机会，按 EV×置信排序。EV 是市场共识的诊断量，不是机会信号；红涨绿跌，|EV|≥5%
							标偏差。
						</p>
						{/* flat 档口径：bankroll 真金余额 + 组合引擎建议注额 */}
						<p className="text-xs text-muted-foreground" data-testid="market-bankroll-note">
							{bankrollQuery.isError ? (
								<span>资金池读取失败——按竞彩最低注建议，重试可去资金页</span>
							) : (
								<span className={TABULAR_NUMS}>
									flat 档单注 ¥
									{combo?.picks[0] !== undefined
										? combo.picks[0].stake.toFixed(2)
										: HAD_COMBO_CONFIG.stake.minStakeCny.toFixed(2)}
									（bankroll ¥{(bankrollQuery.data?.balance ?? 0).toFixed(2)} ·{" "}
									{(HAD_COMBO_CONFIG.stake.flatFraction * 100).toFixed(0)}%）
								</span>
							)}
						</p>
					</div>
					<p className="mt-1 text-xs text-muted-foreground">
						配色：红 = 正向 EV · 绿 = 负向 EV · 琥珀 = 数据警示（过期/共识低置信/偏差） · 蓝 = 可投资格
					</p>
				</header>

				{message ? <StatusToast testid="market-message">{message}</StatusToast> : null}

				{fixturesQuery.isPending ? (
					<div data-testid="market-loading" className="space-y-4">
						<span className="sr-only">加载玩法推荐…</span>
						<div className="h-5 w-64 animate-pulse rounded bg-muted" />
						<div className="h-32 animate-pulse rounded-lg bg-muted" />
						<div className="grid gap-3 sm:grid-cols-2">
							<div className="h-36 animate-pulse rounded-lg bg-muted" />
							<div className="h-36 animate-pulse rounded-lg bg-muted" />
						</div>
					</div>
				) : null}

				{fixturesQuery.isError ? (
					<EmptyState
						variant="backend-unavailable"
						message="连不上后端，玩法推荐加载失败。"
						hint={
							<>
								用 <code>task server</code> 启动 API；首次使用先跑 <code>task ingest-jingcai</code> 拉取竞彩数据。
							</>
						}
						action={{ label: "重试", onClick: () => void fixturesQuery.refetch() }}
					/>
				) : null}

				{fixturesQuery.data && fixturesQuery.data.length === 0 ? (
					<EmptyState
						variant="no-data"
						message="3 天内无在售场次。"
						hint={
							<>
								玩法推荐随竞彩开售更新；刚搭好环境先跑 <code>task ingest-jingcai</code>。
							</>
						}
						action={{ label: "刷新", onClick: () => void fixturesQuery.refetch() }}
					/>
				) : null}

				{fixturesQuery.data && fixturesQuery.data.length > 0 ? (
					<>
						{/* 组合头部卡（票 wb-03 v1）：引擎产出 + 约束说明 + 一键带入 */}
						<section
							aria-labelledby="market-combo-heading"
							data-testid="market-combo"
							className="mb-8 rounded-lg border border-border bg-card p-4"
						>
							<div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
								<h2 id="market-combo-heading" className="text-sm font-medium">
									组合推荐 v1{" "}
									<span className="font-normal text-muted-foreground">
										胜平负 · 注数{" "}
										<span className={TABULAR_NUMS} data-testid="market-combo-count">
											{combo?.picks.length ?? 0}
										</span>
									</span>
								</h2>
								{combo && combo.picks.length > 0 ? (
									<p className={`${TABULAR_NUMS} text-sm`} data-testid="market-combo-total">
										总注额 ¥{combo.totalStake.toFixed(2)} · 预期收益{" "}
										<span className={evClass(combo.expectedProfit)}>+¥{combo.expectedProfit.toFixed(2)}</span>
										<span className="text-xs font-normal text-muted-foreground">
											（<GlossaryTerm id="ev">EV×注额</GlossaryTerm> 共识口径）
										</span>
									</p>
								) : null}
							</div>

							{combo === null ? (
								<p className="text-sm text-muted-foreground" data-testid="market-combo-pending">
									{bankrollQuery.isError
										? "资金池读取失败——注额建议暂缺，去资金页重试后再看组合"
										: "组合计算中（等待场次与资金池数据）…"}
								</p>
							) : combo.picks.length === 0 ? (
								<p
									data-testid="market-combo-empty"
									className="rounded-md border border-dashed border-border p-4 text-sm text-muted-foreground"
								>
									当前窗口无正 EV 机会——EV≤0 不入选（EV 是诊断量非机会信号，见词典）；下方推荐流供逐场研判。
								</p>
							) : (
								<>
									<ul className="mb-3 space-y-2">
										{combo.picks.map((pickEntry) => (
											<li
												key={pickEntry.fixture.fixture_id}
												data-testid={`market-combo-pick-${pickEntry.fixture.fixture_id}`}
												className="flex flex-wrap items-baseline justify-between gap-2 rounded-md border border-border p-3 text-sm"
											>
												<span>
													<span className="text-xs text-muted-foreground">{pickEntry.fixture.match_code}</span>{" "}
													{pickEntry.fixture.home_team} vs {pickEntry.fixture.away_team} ·{" "}
													<span className="font-medium">{SELECTION_LABELS[pickEntry.selection]}</span>
													<span className={`${TABULAR_NUMS} ml-1.5 text-muted-foreground`}>
														@{pickEntry.odds.toFixed(2)}
													</span>
													<span className={`${TABULAR_NUMS} ml-2 ${evClass(pickEntry.ev)}`}>
														{evText(pickEntry.ev)}
													</span>
													<span className={`${TABULAR_NUMS} ml-2 text-xs text-muted-foreground`}>
														books {pickEntry.books} · 置信 {pickEntry.confidence.toFixed(2)}
													</span>
												</span>
												<span className={`${TABULAR_NUMS} text-xs text-muted-foreground`}>
													建议注额 ¥{pickEntry.stake.toFixed(2)} · 预期{" "}
													<span className={evClass(pickEntry.expectedProfit)}>
														+¥{pickEntry.expectedProfit.toFixed(2)}
													</span>
												</span>
											</li>
										))}
									</ul>
									{combo.bankrollNote ? (
										<p
											data-testid="market-combo-bankroll-warning"
											className="mb-3 rounded bg-warning/10 px-2 py-1 text-xs text-foreground"
										>
											{combo.bankrollNote}
										</p>
									) : null}
									<button
										type="button"
										data-testid="market-combo-apply"
										className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
										onClick={applyCombo}
									>
										一键带入选注篮
									</button>
									<div className="mt-3">
										<StakeAdviceNote
											mode="paper"
											bankroll={bankrollQuery.data?.balance ?? (bankrollQuery.isError ? null : 0)}
											ev={comboAdvice.ev}
											odds={comboAdvice.odds}
											note="组合卡按纸面 flat 档口径（2 注时带入后按一注 2串1 整注）；真金 ¼Kelly 在选注篮切真金后给出"
											testid="market-combo-stake-advice"
										/>
									</div>
								</>
							)}

							{/* 约束说明（引擎口径逐条展示）：排序/注额档位/约束/口径标注 */}
							{combo ? (
								<ul className="mt-3 space-y-0.5 text-xs text-muted-foreground" data-testid="market-combo-notes">
									{combo.notes.map((note) => (
										<li key={note}>· {note}</li>
									))}
								</ul>
							) : null}
						</section>

						{/* 推荐流：全部场次按引擎排序；无正 EV 场沉底按开赛时间 */}
						<section aria-labelledby="market-feed-heading">
							<div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
								<h2 id="market-feed-heading" className="text-sm font-medium">
									推荐流 <span className={`${TABULAR_NUMS} text-muted-foreground`}>{feedOrder.length}</span>{" "}
									<span className="text-xs font-normal text-muted-foreground">
										按 EV×置信排序；无正 EV 场按开赛时间沉底
									</span>
								</h2>
							</div>
							<div className="grid gap-3 sm:grid-cols-2" data-testid="market-feed">
								{feedOrder.map((fixture) => (
									<EligibleCard
										key={fixture.fixture_id}
										fixture={fixture}
										now={now}
										legs={legs}
										onPick={pick}
										testidBase="market-card"
										pickTestidBase="market-pick"
										dayNote={dayNoteOf(fixture.business_date, now)}
									/>
								))}
							</div>
						</section>
					</>
				) : null}
			</div>

			{/* 底部常驻选注条 + 右侧 Drawer（复用场次页模式） */}
			<BasketBar basket={basket} emptyHint="点推荐流赔率或“一键带入”加入；单关须单固，2串1 须不同场次" />
			<BasketDrawer
				basket={basket}
				bankroll={bankrollQuery.data?.balance ?? (bankrollQuery.isError ? null : 0)}
				emptyHint="未选择。点推荐流赔率或组合卡“一键带入”加入。"
			/>
		</AppShell>
	);
}
