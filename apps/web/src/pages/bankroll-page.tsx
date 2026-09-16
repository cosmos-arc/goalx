import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
	type BankrollEvent,
	createDeposit,
	type DepositCreated,
	type DepositInput,
	fetchBankroll,
	fetchCostSummary,
} from "../api/goalx";
import { AppShell } from "../components/app-shell";
import type { EChartsOption } from "../components/charts/echarts";
import { useECharts } from "../components/charts/use-echarts";
import { EmptyState } from "../components/empty-state";
import { Button } from "../components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { errorText, TABULAR_NUMS } from "../lib/ui";

/**
 * 票 20：资金页重设计落地（票 09 定稿 = 唯一事实源）。
 *
 * 形态 = 余额大数字 + 近 30 天余额迷你曲线（ECharts；无流水不出图）+
 * 流水 compact 表 + 成本摘要（¥ 与 credits 分列、"未记录成本标缺失"警示保留）+
 * 空态入金引导（`POST /api/v1/bankroll/deposits`，票 10 定稿的唯一 contract 增量）。
 *
 * 口径红线（票 09/20 不变量）：
 * - Bankroll 只受真金（live）模式影响；纸面收益是模拟收益，不进入本页主呈现。
 * - 纸面锁定不产生资金流水（e2e:loop 断言依赖）。
 * - 成本摘要只统计已记账成本：未记录的成本标缺失，不视为总成本已覆盖。
 */

const KIND_LABELS: Record<string, string> = {
	deposit: "入金",
	withdraw: "出金",
	bet_stake: "投注",
	bet_payout: "兑付",
	cost: "成本",
};

const CATEGORY_LABELS: Record<string, string> = {
	odds_api_credit: "The Odds API credits",
	llm_api: "LLM 调用",
	data_subscription: "数据订阅",
};

// ---- 纯函数：余额曲线（use-echarts 测试约定：数据 → option 映射直接断言） ----

export type BalancePoint = { date: string; balance: number };

const THIRTY_DAYS_MS = 30 * 86_400_000;

/**
 * 近 30 天余额点（时间正序）。events 后端新→旧，这里统一按时间排序；
 * 窗口外/不可解析的时间剔除，不用冒充的数据凑点。
 */
export function balancePoints(events: readonly BankrollEvent[], now: Date = new Date()): BalancePoint[] {
	const cutoff = now.getTime() - THIRTY_DAYS_MS;
	return events
		.filter((event) => {
			const at = new Date(event.occurred_at).getTime();
			return !Number.isNaN(at) && at >= cutoff && at <= now.getTime() + 60_000;
		})
		.map((event) => ({ date: event.occurred_at, balance: event.balance_after }))
		.sort((a, b) => new Date(a.date).getTime() - new Date(b.date).getTime());
}

/** 余额点 → option 纯映射：单线迷你趋势（面积弱化），x 轴 MM-DD。 */
export function balanceCurveOption(points: BalancePoint[], lineColor: string, axisColor: string): EChartsOption {
	return {
		grid: { left: 8, right: 16, top: 12, bottom: 8, containLabel: true },
		tooltip: { trigger: "axis" },
		xAxis: {
			type: "category",
			data: points.map((point) => point.date.slice(5, 10)),
			axisLine: { lineStyle: { color: axisColor } },
			axisTick: { show: false },
			axisLabel: { color: axisColor, fontSize: 11 },
		},
		yAxis: {
			type: "value",
			scale: true,
			axisLabel: { color: axisColor, fontSize: 11, formatter: (value: number) => `¥${Math.round(value)}` },
			splitLine: { lineStyle: { color: axisColor, opacity: 0.3 } },
		},
		series: [
			{
				name: "余额",
				type: "line",
				data: points.map((point) => point.balance),
				symbol: "circle",
				symbolSize: 5,
				lineStyle: { color: lineColor, width: 2 },
				itemStyle: { color: lineColor },
				areaStyle: { opacity: 0.08 },
			},
		],
	};
}

/** canvas 取不到 CSS 变量，option 构建时解析语义 token（与验证/历史页同法）。 */
function cssVar(name: string, fallback: string): string {
	const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
	return raw === "" ? fallback : raw;
}

/**
 * 余额迷你曲线独立子组件（票 17/19 同模式）：useECharts 的 init effect 只在挂载时
 * 跑一次（deps=[]），容器必须随组件一起挂载——点数 ≥2 才挂，否则 init 拿不到容器。
 */
function BalanceChart({ points }: { points: BalancePoint[] }) {
	const ref = useECharts(
		balanceCurveOption(points, cssVar("--chart-1", "#2563eb"), cssVar("--muted-foreground", "#6b7280")),
	);
	return (
		<div ref={ref} data-testid="balance-curve" role="img" aria-label="近 30 天余额迷你曲线" className="h-32 w-full" />
	);
}

// ---- 纯函数：流水语义色（票 09 定稿：投注/兑付与盈亏色一致，入金/出金中性） ----

/** 红涨绿跌（票 02 钱层）：只有投注/兑付按盈亏着色，出入金是资金调度不改盈亏语义。 */
export function amountTone(kind: string, amount: number): "profit" | "loss" | "neutral" {
	if (kind === "bet_stake" || kind === "bet_payout") {
		if (amount > 0) return "profit";
		if (amount < 0) return "loss";
	}
	return "neutral";
}

const TONE_CLASS: Record<"profit" | "loss" | "neutral", string> = {
	profit: "text-profit",
	loss: "text-loss",
	neutral: "text-foreground",
};

// ---- 入金表单（空态引导与次级入口共用；金额必填 >0，日期/备注可选） ----

type DepositFormProps = {
	onSuccess: (result: DepositCreated) => void;
	onCancel: () => void;
};

function DepositForm({ onSuccess, onCancel }: DepositFormProps) {
	const [amount, setAmount] = useState("");
	const [occurredAt, setOccurredAt] = useState("");
	const [note, setNote] = useState("");
	const [problem, setProblem] = useState<string | null>(null);
	const queryClient = useQueryClient();

	const deposit = useMutation({
		mutationFn: (input: DepositInput) => createDeposit(input),
		onSuccess: (result) => {
			void queryClient.invalidateQueries({ queryKey: ["bankroll"] });
			onSuccess(result);
		},
		onError: (error) => setProblem(`入金失败：${errorText(error)}`),
	});

	function submit() {
		const amountValue = Number(amount);
		if (amount.trim() === "" || Number.isNaN(amountValue) || amountValue <= 0) {
			setProblem("金额必须大于 0。");
			return;
		}
		setProblem(null);
		const rawDate = occurredAt.trim();
		const parsed = rawDate === "" ? null : new Date(rawDate);
		deposit.mutate({
			amount_cny: amountValue,
			occurred_at: parsed && !Number.isNaN(parsed.getTime()) ? parsed.toISOString() : null,
			note: note.trim() === "" ? null : note.trim(),
		});
	}

	return (
		<form
			data-testid="deposit-form"
			className="mt-4 rounded-lg border border-border bg-card p-4"
			onSubmit={(event) => {
				event.preventDefault();
				submit();
			}}
		>
			<p className="text-sm font-medium">记录一笔入金</p>
			<p className="mt-1 text-xs text-muted-foreground">
				入金是 live 资金的起点：金额必填（&gt;0），时间缺省记为现在，备注可选。
			</p>
			<div className="mt-3 flex flex-wrap items-end gap-3">
				<div>
					<label htmlFor="deposit-amount" className="block text-xs text-muted-foreground">
						金额（¥）
					</label>
					<input
						id="deposit-amount"
						data-testid="deposit-amount"
						type="number"
						step="0.01"
						value={amount}
						onChange={(event) => setAmount(event.target.value)}
						className={`w-32 rounded-md border border-input bg-background px-2 py-1.5 text-sm ${TABULAR_NUMS}`}
					/>
				</div>
				<div>
					<label htmlFor="deposit-date" className="block text-xs text-muted-foreground">
						发生时间（留空 = 现在）
					</label>
					<input
						id="deposit-date"
						data-testid="deposit-date"
						type="datetime-local"
						value={occurredAt}
						onChange={(event) => setOccurredAt(event.target.value)}
						className="w-56 rounded-md border border-input bg-background px-2 py-1.5 text-sm"
					/>
				</div>
				<div>
					<label htmlFor="deposit-note" className="block text-xs text-muted-foreground">
						备注
					</label>
					<input
						id="deposit-note"
						data-testid="deposit-note"
						type="text"
						value={note}
						onChange={(event) => setNote(event.target.value)}
						className="w-44 rounded-md border border-input bg-background px-2 py-1.5 text-sm"
					/>
				</div>
				<Button type="submit" size="sm" disabled={deposit.isPending} data-testid="deposit-submit">
					{deposit.isPending ? "提交中…" : "提交入金"}
				</Button>
				<Button type="button" variant="ghost" size="sm" onClick={onCancel} data-testid="deposit-cancel">
					取消
				</Button>
			</div>
			{problem ? (
				<p data-testid="deposit-problem" className="mt-2 text-xs text-loss" role="alert">
					{problem}
				</p>
			) : null}
		</form>
	);
}

// ---- 页面 ----

export function BankrollPage() {
	const bankroll = useQuery({ queryKey: ["bankroll"], queryFn: () => fetchBankroll() });
	const costs = useQuery({ queryKey: ["costs"], queryFn: () => fetchCostSummary() });
	const [depositOpen, setDepositOpen] = useState(false);
	const [message, setMessage] = useState<string | null>(null);

	const data = bankroll.data ?? null;
	const events = data?.events ?? [];
	const points = data ? balancePoints(events) : [];

	function openDeposit() {
		setMessage(null);
		setDepositOpen(true);
	}

	return (
		<AppShell title="资金">
			{/* 口径行（票 09/20 不变量逐字落呈）：纸面不进本页主呈现 */}
			<p data-testid="bankroll-caliber" className="mb-4 text-sm text-muted-foreground">
				Bankroll 只受真金（live）模式影响；纸面收益是模拟收益，不进入本页。纸面与真实流水相互隔离。
			</p>
			{message ? (
				<p data-testid="bankroll-message" className="mb-4 text-sm">
					{message}
				</p>
			) : null}

			{bankroll.isPending ? (
				<div className="space-y-4" data-testid="bankroll-loading">
					<span className="sr-only">加载资金状态…</span>
					<div className="h-12 w-48 animate-pulse rounded-lg bg-muted" />
					<div className="h-32 animate-pulse rounded-lg bg-muted" />
				</div>
			) : null}

			{bankroll.isError ? (
				<div data-testid="bankroll-error">
					<EmptyState
						variant="backend-unavailable"
						message="资金状态加载失败。"
						hint={
							<>
								用 <code>task server</code> 启动 API。
							</>
						}
						action={{ label: "重试", onClick: () => void bankroll.refetch() }}
					/>
				</div>
			) : null}

			{data ? (
				<>
					{/* 余额大数字 + 近 30 天迷你曲线（点数 ≥2 才挂图，无流水不显示） */}
					<section aria-labelledby="bankroll-balance-heading" className="mb-8">
						<h2 id="bankroll-balance-heading" className="sr-only">
							当前余额
						</h2>
						<div className="flex flex-wrap items-end justify-between gap-3">
							<div>
								<p className="text-xs text-muted-foreground">当前余额（真金）</p>
								<p data-testid="bankroll-balance" className={`text-4xl font-semibold ${TABULAR_NUMS}`}>
									{data.balance === null ? "尚未入金" : `¥${data.balance.toFixed(2)}`}
								</p>
							</div>
							{/* 已有余额时的次级入金入口：不抢余额大数字的视觉焦点 */}
							{data.balance !== null && !depositOpen ? (
								<Button variant="outline" size="sm" onClick={openDeposit} data-testid="deposit-open">
									记录入金
								</Button>
							) : null}
						</div>
						{points.length >= 2 ? (
							<div className="mt-4 rounded-lg border border-border bg-card p-4">
								<p className="mb-2 text-xs text-muted-foreground">近 30 天余额</p>
								<BalanceChart points={points} />
							</div>
						) : events.length > 0 ? (
							<p data-testid="balance-curve-empty" className="mt-3 text-xs text-muted-foreground">
								近 30 天流水不足 2 笔，余额趋势待积累。
							</p>
						) : null}
						{depositOpen ? (
							<DepositForm
								onSuccess={(result) => {
									setDepositOpen(false);
									setMessage(`已入金 ¥${result.event.amount_cny.toFixed(2)}，最新余额 ¥${result.balance.toFixed(2)}`);
								}}
								onCancel={() => setDepositOpen(false)}
							/>
						) : null}
					</section>

					{/* 流水 compact 表 / 空态入金引导 */}
					{events.length === 0 ? (
						<div data-testid="bankroll-empty" className="mb-8">
							<EmptyState
								variant="no-data"
								message="还没有真金流水——入金是记账的起点。"
								hint="记录第一笔入金后，这里会出现余额、流水与趋势；纸面锁定不产生资金变动。"
								action={depositOpen ? undefined : { label: "记录第一笔入金", onClick: openDeposit }}
							/>
						</div>
					) : (
						<section aria-labelledby="bankroll-events-heading" className="mb-8">
							<h2 id="bankroll-events-heading" className="mb-3 text-sm font-medium">
								真金流水
								<span className="ml-2 text-xs font-normal text-muted-foreground">
									投注/兑付按盈亏着色；入金/出金是资金调度，中性呈现
								</span>
							</h2>
							<div className="overflow-x-auto rounded-lg border border-border">
								<Table data-density="compact">
									<TableHeader>
										<TableRow>
											<TableHead>时间</TableHead>
											<TableHead>类型</TableHead>
											<TableHead>金额（¥）</TableHead>
											<TableHead>余额（¥）</TableHead>
											<TableHead>备注</TableHead>
										</TableRow>
									</TableHeader>
									<TableBody>
										{events.map((event) => {
											const tone = amountTone(event.kind, event.amount_cny);
											return (
												<tr key={event.id} data-testid="bankroll-event">
													<td className={`${TABULAR_NUMS} text-muted-foreground`}>
														{event.occurred_at.slice(0, 16).replace("T", " ")}
													</td>
													<td>{KIND_LABELS[event.kind] ?? event.kind}</td>
													<td className={`${TABULAR_NUMS} font-medium ${TONE_CLASS[tone]}`}>
														{event.amount_cny >= 0 ? "+" : ""}
														{event.amount_cny.toFixed(2)}
													</td>
													<td className={TABULAR_NUMS}>{event.balance_after.toFixed(2)}</td>
													<td className="text-muted-foreground">{event.note ?? "—"}</td>
												</tr>
											);
										})}
									</TableBody>
								</Table>
							</div>
						</section>
					)}
				</>
			) : null}

			{/* 期间成本摘要：¥ 与 credits 分列；未记录成本标缺失，不冒充已覆盖 */}
			<section className="rounded-lg border border-border p-4" data-testid="cost-summary">
				<h2 className="mb-2 text-sm font-semibold">期间成本摘要（金额与 credits 分列）</h2>
				<p className="mb-3 text-xs text-muted-foreground">仅统计已记账成本；未记录的成本标缺失，不视为总成本已覆盖。</p>
				{costs.isError ? (
					<p data-testid="cost-error" className="text-sm text-muted-foreground">
						成本摘要不可用。
					</p>
				) : costs.data ? (
					<div className="flex flex-wrap gap-6 text-sm">
						<p data-testid="cost-total" className={TABULAR_NUMS}>
							已记账金额：<span className="font-medium">¥{costs.data.total_cny.toFixed(2)}</span>
						</p>
						<p data-testid="cost-credits" className={TABULAR_NUMS}>
							API credits：<span className="font-medium">{costs.data.credits_used}</span>
						</p>
					</div>
				) : null}
				{costs.data && costs.data.items.length > 0 ? (
					<Table data-density="compact" className="mt-3 w-full">
						<TableHeader>
							<TableRow>
								<TableHead>类别</TableHead>
								<TableHead>units/credits</TableHead>
								<TableHead>金额（¥）</TableHead>
								<TableHead>笔数</TableHead>
							</TableRow>
						</TableHeader>
						<TableBody>
							{costs.data.items.map((item) => (
								<TableRow key={item.category} data-testid="cost-item">
									<TableCell>{CATEGORY_LABELS[item.category] ?? item.category}</TableCell>
									<TableCell className={TABULAR_NUMS}>{item.units}</TableCell>
									<TableCell className={TABULAR_NUMS}>{item.amount_cny.toFixed(2)}</TableCell>
									<TableCell className={TABULAR_NUMS}>{item.entries}</TableCell>
								</TableRow>
							))}
						</TableBody>
					</Table>
				) : (
					<p data-testid="cost-missing" className="mt-3 text-sm text-muted-foreground">
						暂无已记账成本（未知成本不在此冒充已覆盖）。
					</p>
				)}
			</section>
		</AppShell>
	);
}
