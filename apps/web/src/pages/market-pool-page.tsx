import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { fetchBankroll, fetchTodayFixtures, type TodayFixture } from "../api/goalx";
import { AppShell } from "../components/app-shell";
import { EmptyState } from "../components/empty-state";
import { GlossaryTerm } from "../components/glossary-term";
import { MarketTabs } from "../components/market-tabs";
import { parlayAdviceInput, StakeAdviceNote } from "../components/stake-advice";
import { beijingBusinessDate, SELECTION_LABELS, SELECTIONS, type Selection, TABULAR_NUMS } from "../lib/ui";

/**
 * 票 wb-07：14场任9 骨架页 `/markets/pool`——玩法轴第三页（结构先行）。
 *
 * 诚实原则（无彩池数据时整页一眼是骨架）：页头横幅级说明"彩池数据源接入中
 * （goalx-quant 立票）"；期次为演示（按业务日聚合场次窗口），14 个槽位缺
 * 数据处画虚线"待期次数据"；三向概率占位 = 欧共识去水（had 模型概率与彩池
 * 派彩口径待数据源）；推荐/搏冷标记 = 纯前端按占位概率的标注（非生成器）。
 *
 * 结构与交互完整可演示：期次选择 → 逐场三向选择（14/9，任9 ≥9 场）→
 * 三档额度建议（复用票 wb-06 stake-advice 端点：保守=flat、标准=¼Kelly
 * 上限 2%、激进=¼Kelly 上限 5%，页内标注映射）→ 提交占位（disabled +
 * 说明）。AI 证据总结/目标金额反推/搏冷生成器 = not-available 留位。
 */

/** 14 场槽位总数（任9 规则：14 场任选 9 场）。 */
const POOL_SLOTS = 14;
/** 任9 的固定选取场次数。 */
const PICK9 = 9;
/** 场次窗口与场次/玩法页同宽（同一 queryKey 共享缓存）。 */
const POOL_WINDOW_DAYS = 3;

/** 三档映射（票 wb-07 定稿，页内标注）：flat / ¼Kelly cap 2% / ¼Kelly cap 5%。 */
/** 三档定义（capFraction 省略 = flat 档不设上限参数，端点默认不生效）。 */
type PoolTier = {
	key: string;
	/** 卡片标题（完整映射在三档区口径行，卡片内用短名）。 */
	title: string;
	mode: "paper" | "live";
	capFraction?: number;
	mapping: string;
};

const TIERS: PoolTier[] = [
	{
		key: "conservative",
		title: "保守档（flat）",
		mode: "paper",
		mapping: "保守 = flat（纸面红线）",
	},
	{
		key: "standard",
		title: "标准档（¼Kelly 上限 2%）",
		mode: "live",
		capFraction: 0.02,
		mapping: "标准 = ¼Kelly，单注上限 2%",
	},
	{
		key: "aggressive",
		title: "激进档（¼Kelly 上限 5%）",
		mode: "live",
		capFraction: 0.05,
		mapping: "激进 = ¼Kelly，单注上限 5%",
	},
];

/** C(n, k)：复式组合数（每场单选 → 组合数 = C(已选场数, 9)；导出供单测）。 */
export function combinations(n: number, k: number): number {
	if (n < k || k < 0) {
		return 0;
	}
	let result = 1;
	for (let i = 1; i <= k; i += 1) {
		result = (result * (n - k + i)) / i;
	}
	return Math.round(result);
}

/** 一场占位概率最高向（推荐标记）；无概率场返回 null。 */
function bestSelection(fixture: TodayFixture): Selection | null {
	const prob = fixture.eu_prob;
	if (!prob) {
		return null;
	}
	let best: Selection | null = null;
	for (const sel of SELECTIONS) {
		const value = prob[sel];
		if (value !== null && value !== undefined && (best === null || value > (prob[best] ?? -1))) {
			best = sel;
		}
	}
	return best;
}

/** 一场占位概率最低向（搏冷标记）；无概率场返回 null。 */
function coldestSelection(fixture: TodayFixture): Selection | null {
	const prob = fixture.eu_prob;
	if (!prob) {
		return null;
	}
	let worst: Selection | null = null;
	for (const sel of SELECTIONS) {
		const value = prob[sel];
		if (value !== null && value !== undefined && (worst === null || value < (prob[worst] ?? 2))) {
			worst = sel;
		}
	}
	return worst;
}

export function MarketPoolPage() {
	const fixturesQuery = useQuery({
		queryKey: ["fixtures-window", POOL_WINDOW_DAYS],
		queryFn: () => fetchTodayFixtures(undefined, POOL_WINDOW_DAYS),
	});
	const bankrollQuery = useQuery({ queryKey: ["bankroll"], queryFn: fetchBankroll });
	const now = Date.now();
	const [period, setPeriod] = useState<string | null>(null);
	/** 已选场次 → 三向选择（一场一选，任9 口径）。 */
	const [picks, setPicks] = useState<Record<number, Selection>>({});

	const fixtures = fixturesQuery.data ?? [];
	/** 期次（演示）= 场次窗口的业务日；无数据时仍给今天起 3 日（结构骨架常在）。 */
	const periods = useMemo(() => {
		const today = beijingBusinessDate(now);
		const base = [today];
		for (const fixture of fixtures) {
			if (fixture.business_date && !base.includes(fixture.business_date)) {
				base.push(fixture.business_date);
			}
		}
		return base.sort();
	}, [fixtures, now]);
	const activePeriod = period !== null && periods.includes(period) ? period : (periods[0] ?? beijingBusinessDate(now));
	/** 期次内的场次（最多 14 场槽位；超出诚实截断并说明）。 */
	const periodFixtures = fixtures.filter((fixture) => fixture.business_date === activePeriod).slice(0, POOL_SLOTS);
	const emptySlots = Math.max(0, POOL_SLOTS - periodFixtures.length);

	const pickedEntries = periodFixtures.filter((fixture) => picks[fixture.fixture_id] !== undefined);
	const comboCount = combinations(pickedEntries.length, PICK9);
	/** 三档输入：所选组合的联合口径（占位 = 共识 EV × 竞彩价；奖池派彩待数据源）。 */
	const adviceInput = parlayAdviceInput(
		pickedEntries.map((fixture) => {
			const sel = picks[fixture.fixture_id];
			const odds = sel !== undefined ? fixture.jc_odds[sel] : null;
			return {
				ev: sel !== undefined ? (fixture.ev?.[sel] ?? null) : null,
				odds: odds ?? Number.NaN, // 缺价的向毒化联合赔率 → 下方整体降级
			};
		}),
	);
	// 缺价/EV 的选择不给比例建议（诚实降级，不伪造联合口径）
	const hasValidInput = pickedEntries.length > 0 && Number.isFinite(adviceInput.odds);

	const bankroll = bankrollQuery.data?.balance ?? (bankrollQuery.isError ? null : 0);

	function pick(fixture: TodayFixture, selection: Selection) {
		setPicks((current) => {
			const next = { ...current };
			if (next[fixture.fixture_id] === selection) {
				delete next[fixture.fixture_id]; // 再点同向 = 取消
			} else {
				next[fixture.fixture_id] = selection; // 一场一选（任9 口径）
			}
			return next;
		});
	}

	/** 按推荐标记预选（演示）：占位概率最高向填满前 9 个有数据的槽位。 */
	function prefillRecommended() {
		const next: Record<number, Selection> = {};
		for (const fixture of periodFixtures) {
			if (Object.keys(next).length >= PICK9) {
				break;
			}
			const best = bestSelection(fixture);
			if (best !== null) {
				next[fixture.fixture_id] = best;
			}
		}
		setPicks(next);
	}

	return (
		<AppShell title="14场任9">
			<div className="pb-24">
				<MarketTabs />

				{/* 骨架横幅（诚实原则：无彩池数据时整页一眼是骨架） */}
				<div
					role="note"
					data-testid="pool-skeleton-banner"
					className="mb-5 rounded-lg border border-warning/40 bg-warning/10 p-4 text-sm"
				>
					<p className="font-medium">骨架页——彩池数据源接入中（goalx-quant 立票，外部依赖）</p>
					{/* 琥珀底上 muted-foreground 对比不足（axe color-contrast）——用前景色 */}
					<p className="mt-1 text-xs text-foreground">
						当前无真实期次/彩池数据：以下期次为演示（按业务日聚合场次窗口），三向概率占位 = 欧共识去水（had
						模型概率与彩池派彩口径待数据源）；结构与交互流程完整可演示，不生成真实注单。
					</p>
				</div>

				<header className="mb-5">
					<p className="text-sm text-muted-foreground" data-testid="pool-caliber">
						14 场任选 9 场（<GlossaryTerm id="ev">EV</GlossaryTerm> 为占位口径：欧共识 × 竞彩价——奖池无固定赔率，
						派彩估计随彩池数据源接入）；推荐/搏冷标记 = 纯前端按占位概率标注，非生成器产出。
					</p>
				</header>

				{/* 期次选择（演示）+ 预选 */}
				<section aria-labelledby="pool-period-heading" className="mb-5">
					<div className="flex flex-wrap items-end justify-between gap-3">
						<h2 id="pool-period-heading" className="text-sm font-medium">
							期次{" "}
							<span className="font-normal text-muted-foreground" data-testid="pool-period-note">
								（演示：业务日聚合，真实期次随彩池数据源）
							</span>
						</h2>
						<div className="flex items-center gap-2">
							<label className="flex items-center gap-2 text-xs">
								<span className="text-muted-foreground">期次（演示）</span>
								<select
									data-testid="pool-period-select"
									className="rounded-md border border-input bg-background px-2 py-1.5 text-sm"
									value={activePeriod}
									onChange={(event) => {
										setPeriod(event.target.value);
										setPicks({});
									}}
								>
									{periods.map((day) => (
										<option key={day} value={day}>
											{day}
										</option>
									))}
								</select>
							</label>
							<button
								type="button"
								data-testid="pool-prefill-recommended"
								className="rounded-md border border-border px-3 py-1.5 text-sm transition-colors hover:bg-muted"
								onClick={prefillRecommended}
							>
								按推荐标记预选（演示）
							</button>
						</div>
					</div>
					<p className="mt-2 text-xs" data-testid="pool-pick-count">
						已选{" "}
						<span className={TABULAR_NUMS}>
							{pickedEntries.length}/{POOL_SLOTS}
						</span>{" "}
						场（任9 需 ≥{PICK9} 场；当前组合数 <span className={TABULAR_NUMS}>{comboCount}</span> 注）
					</p>
				</section>

				{/* 后端不可用降级（结构骨架照常渲染，槽位全部"待数据"） */}
				{fixturesQuery.isError ? (
					<div className="mb-5">
						<EmptyState
							variant="backend-unavailable"
							message="连不上后端，场次数据加载失败——结构骨架照常展示。"
							hint={<>用 task server 启动 API；期次数据本身还依赖彩池数据源接入。</>}
							action={{ label: "重试", onClick: () => void fixturesQuery.refetch() }}
						/>
					</div>
				) : null}

				{/* 14 场槽位列表 */}
				<section aria-label="14 场列表" data-testid="pool-slots" className="mb-8 space-y-2">
					{periodFixtures.map((fixture) => {
						const recommended = bestSelection(fixture);
						const coldest = coldestSelection(fixture);
						return (
							<article
								key={fixture.fixture_id}
								data-testid={`pool-slot-${fixture.fixture_id}`}
								className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-border bg-card p-3 text-sm"
							>
								<span className="min-w-28 text-xs text-muted-foreground">
									{fixture.match_code}
									<br />
									{fixture.home_team} vs {fixture.away_team}
								</span>
								<span className="flex gap-1" data-testid={`pool-pick-${fixture.fixture_id}`}>
									{SELECTIONS.map((sel) => {
										const prob = fixture.eu_prob?.[sel];
										const odds = fixture.jc_odds?.[sel];
										const selected = picks[fixture.fixture_id] === sel;
										return (
											<button
												key={sel}
												type="button"
												data-testid={`pool-pick-${fixture.fixture_id}-${sel}`}
												aria-pressed={selected}
												className={`rounded-md border px-2 py-1 text-xs ${TABULAR_NUMS} transition-colors ${
													selected
														? "border-primary bg-primary text-primary-foreground"
														: "border-border hover:bg-muted"
												}`}
												onClick={() => pick(fixture, sel)}
											>
												{SELECTION_LABELS[sel]}
												{prob !== null && prob !== undefined ? (
													<span className="ml-1 opacity-80">{(prob * 100).toFixed(0)}%</span>
												) : (
													<span className="ml-1 opacity-60">待数据</span>
												)}
												{odds !== null && odds !== undefined ? (
													<span className="ml-1 opacity-60">@{odds.toFixed(2)}</span>
												) : null}
												{recommended === sel ? (
													<span className="ml-1 rounded bg-info/10 px-1 text-info">推荐</span>
												) : null}
												{coldest === sel && recommended !== coldest ? (
													<span className="ml-1 rounded bg-warning/10 px-1">搏冷</span>
												) : null}
											</button>
										);
									})}
								</span>
							</article>
						);
					})}
					{Array.from({ length: emptySlots }, (_, index) => periodFixtures.length + index + 1).map((slotNo) => (
						<div
							key={`empty-${slotNo}`}
							data-testid={`pool-slot-empty-${slotNo}`}
							className="rounded-lg border border-dashed border-border p-3 text-xs text-muted-foreground"
						>
							第 {slotNo} 场：待期次数据（真实期次的 14 场对阵随彩池数据源接入）
						</div>
					))}
				</section>

				{/* 三档额度建议（复用票 wb-06 端点，映射页内标注） */}
				<section aria-labelledby="pool-tiers-heading" className="mb-8">
					<h2 id="pool-tiers-heading" className="mb-2 text-sm font-medium">
						三档额度建议 <span className="font-normal text-muted-foreground">保守 / 标准 / 激进</span>
					</h2>
					<p className="mb-3 text-xs text-muted-foreground" data-testid="pool-tier-mapping">
						映射：保守 = flat（纸面红线）/ 标准 = ¼Kelly 单注上限 2% / 激进 = ¼Kelly 单注上限
						5%——复用建议仓位端点；真金档位依赖前瞻 skill 过线，当前仅演示规则计算；EV≤0 时三档均 建议 ¥0。输入 =
						所选组合联合口径（占位：共识 EV × 竞彩价；奖池派彩估计待数据源）。
					</p>
					{pickedEntries.length === 0 ? (
						<p
							className="rounded-md border border-dashed border-border p-4 text-sm text-muted-foreground"
							data-testid="pool-tiers-empty"
						>
							先在上方 14 场列表选择（或"按推荐标记预选"）——三档额度随所选组合计算。
						</p>
					) : (
						<div className="grid gap-2 sm:grid-cols-3" data-testid="pool-tiers">
							{TIERS.map((tier) => (
								<div key={tier.key} className="flex flex-col gap-2">
									<StakeAdviceNote
										mode={tier.mode}
										bankroll={bankroll}
										ev={hasValidInput ? adviceInput.ev : null}
										odds={hasValidInput ? adviceInput.odds : null}
										{...(tier.capFraction === undefined ? {} : { capFraction: tier.capFraction })}
										label={tier.title}
										note={`组合 ${comboCount} 注 × ¥/注（每注同额，任9 复式）`}
										testid={`pool-tier-${tier.key}`}
									/>
								</div>
							))}
						</div>
					)}
				</section>

				{/* 留位：AI 证据总结 / 目标金额反推 / 搏冷生成器（not-available） */}
				<section aria-label="随 M3/v2 上线" className="mb-8 grid gap-3 sm:grid-cols-3" data-testid="pool-coming-soon">
					<EmptyState variant="not-available" message="AI 证据总结（为何这样研判）" hint="随 M3 LLM 线上线。" />
					<EmptyState variant="not-available" message="目标金额反推选择" hint="随 v2 上线（骨架落地后毕业）。" />
					<EmptyState
						variant="not-available"
						message="搏冷模式生成器"
						hint="随 v2 上线（当前搏冷标记仅为前端标注）。"
					/>
				</section>

				{/* 提交占位（disabled + 说明） */}
				<section aria-labelledby="pool-submit-heading" className="rounded-lg border border-border bg-card p-4">
					<h2 id="pool-submit-heading" className="text-sm font-medium">
						提交
					</h2>
					<p className="mt-1 text-xs text-muted-foreground">
						提交随彩池数据源与结算链路接入后开放——本页为结构骨架，不生成真实注单（复式票落库走 pool-slips
						端点，待期次数据就绪后接线）。
					</p>
					<button
						type="button"
						data-testid="pool-submit"
						disabled
						className="mt-3 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground opacity-40"
					>
						提交（占位，未开放）
					</button>
				</section>
			</div>
		</AppShell>
	);
}
