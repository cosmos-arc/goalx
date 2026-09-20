import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import {
	fetchFixtureEvidence,
	fetchReviewQueue,
	type ReviewQueueItem,
	submitBlindReview,
	submitReviewVerdict,
} from "../api/goalx";
import { AppShell } from "../components/app-shell";
import { EmptyState } from "../components/empty-state";
import { TABULAR_NUMS } from "../lib/ui";

/**
 * 票 14：复核页点亮（复核闭环人工面 + 盲评入口目的地）。
 *
 * - 复核队列：赛前 JS 路由 / 赛后一对一错两类 open 项，结论三分类
 *   （关键贡献/无关/误导）只进评测集（票 05 冻结），不改预测工件；
 * - 盲评：双周匿名二选一（甲/乙 = ML 摘要 vs 情报线证据卡，顺序随机）
 *   ——匿名是方法要求：选完才揭晓对应轨道；同周期同场次幂等。
 */

const VERDICTS = [
	{ value: "key_contribution", label: "关键贡献" },
	{ value: "irrelevant", label: "无关" },
	{ value: "misleading", label: "误导" },
] as const;

/** 双周期次标签：2026 年第 N 个双周（B1 起）。 */
function biweekCycle(now = new Date()): string {
	const start = Date.UTC(now.getUTCFullYear(), 0, 1);
	const day = Math.floor((now.getTime() - start) / 86_400_000);
	return `${now.getUTCFullYear()}-B${Math.floor(day / 14) + 1}`;
}

function shortTime(iso: string): string {
	return iso ? iso.slice(5, 16).replace("T", " ") : "—";
}

function pct(value: number | null | undefined): string {
	return value === null || value === undefined ? "—" : `${(value * 100).toFixed(0)}%`;
}

function QueueRow({ item }: { item: ReviewQueueItem }) {
	const queryClient = useQueryClient();
	const [message, setMessage] = useState<string | null>(null);
	const verdict = useMutation({
		mutationFn: (classification: string) =>
			submitReviewVerdict(item.id, {
				classification: classification as "key_contribution" | "irrelevant" | "misleading",
			}),
		onSuccess: () => {
			setMessage("已记录（只进评测集）");
			void queryClient.invalidateQueries({ queryKey: ["review-queue"] });
		},
		onError: () => setMessage("提交失败，稍后重试"),
	});
	return (
		<tr data-testid="review-queue-row">
			<td className="border-t border-border px-2 py-2">
				{item.home_team} vs {item.away_team}
				<span className="ml-1 text-xs text-muted-foreground">{item.competition}</span>
			</td>
			<td className="border-t border-border px-2 py-2 text-xs">
				{item.route === "pre_match" ? "赛前分歧" : "赛后一对一错"}
				{item.js_value !== null ? <span className={`${TABULAR_NUMS} ml-1`}>JS {item.js_value.toFixed(3)}</span> : null}
			</td>
			<td className="border-t border-border px-2 py-2 text-xs text-muted-foreground">{shortTime(item.created_at)}</td>
			<td className="border-t border-border px-2 py-2">
				<div className="flex flex-wrap items-center gap-1.5">
					{VERDICTS.map((v) => (
						<button
							key={v.value}
							type="button"
							data-testid={`verdict-${v.value}`}
							disabled={verdict.isPending}
							className="rounded-md border border-border px-2 py-1 text-xs transition-colors hover:bg-muted disabled:opacity-40"
							onClick={() => verdict.mutate(v.value)}
						>
							{v.label}
						</button>
					))}
					{message ? <span className="text-xs text-info">{message}</span> : null}
				</div>
			</td>
		</tr>
	);
}

function BlindReviewPanel() {
	const cycle = useMemo(() => biweekCycle(), []);
	const queueQuery = useQuery({ queryKey: ["review-queue"], queryFn: fetchReviewQueue });
	const items = queueQuery.data?.items ?? [];
	const [fixtureId, setFixtureId] = useState<number | null>(null);
	const activeId = fixtureId ?? items[0]?.fixture_id ?? null;
	const evidenceQuery = useQuery({
		queryKey: ["fixture-evidence", activeId],
		queryFn: () => fetchFixtureEvidence(activeId as number),
		enabled: activeId !== null,
	});
	const queryClient = useQueryClient();
	const [order] = useState<["ml", "llm"] | ["llm", "ml"]>(() => (Math.random() < 0.5 ? ["ml", "llm"] : ["llm", "ml"]));
	const [result, setResult] = useState<string | null>(null);
	const [revealed, setRevealed] = useState(false);
	const blind = useMutation({
		mutationFn: (choice: "ml" | "llm") => submitBlindReview({ cycle, fixture_id: activeId as number, choice }),
		onSuccess: (res) => {
			setRevealed(true);
			setResult(
				res.recorded
					? `已记录（${cycle} · 场次 ${activeId}）——两份研判的轨道已揭晓。`
					: "本期该场次已有盲评记录（幂等吸收），轨道照常揭晓。",
			);
			void queryClient.invalidateQueries({ queryKey: ["fixture-evidence", activeId] });
		},
		onError: () => setResult("提交失败，稍后重试"),
	});

	const evidence = evidenceQuery.data;
	const intelText =
		evidence && evidence.intels.length > 0 ? evidence.intels.map((i) => i.text).join("；") : "（本场无已存证情报）";

	return (
		<section
			aria-labelledby="review-blind-heading"
			className="rounded-lg border border-border bg-card p-4"
			data-testid="review-blind"
		>
			<h2 id="review-blind-heading" className="text-sm font-medium">
				盲评（双周匿名二选一）
			</h2>
			<p className="mt-1 text-xs text-muted-foreground">
				两份匿名研判（一份只看量化模型、一份看情报线证据卡），选更有说服力的那份——选完揭晓对应轨道。结果只进评测集作定性参考，不作证明支柱。
				当前周期 <span className={TABULAR_NUMS}>{cycle}</span>。
			</p>
			{queueQuery.isPending ? (
				<p className="mt-3 text-xs text-muted-foreground">场次加载中…</p>
			) : items.length === 0 && activeId === null ? (
				<p className="mt-3 text-xs text-muted-foreground" data-testid="blind-no-fixture">
					暂无可盲评场次——待复核队列为空；可直接在证据链区块复制场次 id 后从 URL 进入。
				</p>
			) : (
				<>
					<label className="mt-3 flex items-center gap-2 text-xs">
						<span className="text-muted-foreground">场次</span>
						<select
							data-testid="blind-fixture-select"
							className="rounded-md border border-input bg-background px-2 py-1 text-sm"
							value={activeId ?? ""}
							onChange={(event) => {
								setFixtureId(Number(event.target.value));
								setRevealed(false);
								setResult(null);
							}}
						>
							{items.map((item) => (
								<option key={item.fixture_id} value={item.fixture_id}>
									{item.fixture_id} · {item.home_team} vs {item.away_team}
								</option>
							))}
						</select>
					</label>
					{evidence ? (
						<div className="mt-3 grid gap-3 sm:grid-cols-2">
							{order.map((side, idx) => {
								const label = idx === 0 ? "甲" : "乙";
								const probs =
									side === "ml" ? evidence.tracks["ml"] : (evidence.tracks["fused"] ?? evidence.tracks["llm"]);
								const text =
									side === "ml"
										? "仅量化模型（时间衰减 Dixon-Coles）历史强度推导，无情报输入。"
										: `情报线：${intelText}`;
								return (
									<BlindPickCard
										key={label}
										label={label}
										side={side}
										text={text}
										probs={probs ?? { h: 0, d: 0, a: 0 }}
										revealed={revealed}
										pending={blind.isPending}
										onPick={() => blind.mutate(side)}
									/>
								);
							})}
						</div>
					) : evidenceQuery.isError ? (
						<p className="mt-3 text-xs text-muted-foreground" data-testid="blind-evidence-error">
							该场次证据不可得（后端不可达或无数据）——盲评需要两轨研判，暂不能进行。
						</p>
					) : (
						<p className="mt-3 text-xs text-muted-foreground">研判卡加载中…</p>
					)}
					{result ? (
						<p className="mt-2 text-xs text-info" data-testid="blind-result">
							{result}
						</p>
					) : null}
				</>
			)}
		</section>
	);
}

function BlindPickCard({
	label,
	side,
	text,
	probs,
	revealed,
	pending,
	onPick,
}: {
	label: string;
	side: "ml" | "llm";
	text: string;
	probs: { h: number; d: number; a: number };
	revealed: boolean;
	pending: boolean;
	onPick: () => void;
}) {
	return (
		<div className="rounded-md border border-border p-3 text-sm" data-testid={`blind-card-${label}`}>
			<p className="mb-1 font-medium">
				研判 {label}
				{revealed ? (
					<span className="ml-2 rounded bg-info/10 px-1 text-xs text-info" data-testid={`blind-reveal-${label}`}>
						{side === "ml" ? "量化模型" : "情报线证据卡"}
					</span>
				) : null}
			</p>
			<p className="text-xs text-muted-foreground">{text}</p>
			<p className={`${TABULAR_NUMS} mt-2`}>
				胜 {pct(probs.h)} / 平 {pct(probs.d)} / 负 {pct(probs.a)}
			</p>
			<button
				type="button"
				data-testid={`blind-pick-${label}`}
				disabled={pending || revealed}
				className="mt-2 rounded-md border border-info px-2 py-1 text-xs text-info disabled:opacity-40"
				onClick={onPick}
			>
				选这份
			</button>
		</div>
	);
}

export function ReviewPage() {
	const queueQuery = useQuery({ queryKey: ["review-queue"], queryFn: fetchReviewQueue });
	const items = queueQuery.data?.items ?? [];
	return (
		<AppShell title="复核">
			<div className="space-y-6 pb-24">
				<p className="text-sm text-muted-foreground" data-testid="review-caliber">
					复核是 M3
					评测协议的人工环节：赛前双轨显著分歧与赛后一对一错场次入队，结论三分类只进评测集——不改任何预测工件，不影响真钱资格判定。
				</p>

				<section aria-labelledby="review-queue-heading" data-testid="review-queue">
					<h2 id="review-queue-heading" className="mb-2 text-sm font-medium">
						待复核队列{" "}
						<span className="font-normal text-muted-foreground">
							{queueQuery.isPending ? "加载中…" : `${items.length} 项 open`}
						</span>
					</h2>
					{queueQuery.isError ? (
						<EmptyState
							variant="backend-unavailable"
							message="连不上后端，复核队列加载失败。"
							hint={
								<>
									用 <code>task server</code> 启动 API 后重试。
								</>
							}
							action={{ label: "重试", onClick: () => void queueQuery.refetch() }}
						/>
					) : items.length === 0 && !queueQuery.isPending ? (
						<p
							className="rounded-md border border-dashed border-border p-4 text-sm text-muted-foreground"
							data-testid="review-queue-empty"
						>
							暂无待复核项——gate 未路由显著分歧（JS≤0.06），赛后亦无一单一错场次。
						</p>
					) : (
						<div className="overflow-x-auto rounded-lg border border-border">
							<table className="w-full text-sm">
								<thead>
									<tr className="text-left text-xs text-muted-foreground">
										<th className="px-2 py-1.5 font-medium">场次</th>
										<th className="px-2 py-1.5 font-medium">来源</th>
										<th className="px-2 py-1.5 font-medium">入队</th>
										<th className="px-2 py-1.5 font-medium">结论三分类</th>
									</tr>
								</thead>
								<tbody>
									{items.map((item) => (
										<QueueRow key={item.id} item={item} />
									))}
								</tbody>
							</table>
						</div>
					)}
				</section>

				<BlindReviewPanel />
			</div>
		</AppShell>
	);
}
