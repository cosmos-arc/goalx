import { useChat } from "@ai-sdk/react";
import { type FormEvent, useMemo, useRef, useState } from "react";
import { createAskTransport } from "../api/agui-transport";
import type { IntelItem } from "../api/goalx";
import { shortTime, TABULAR_NUMS } from "../lib/ui";

/**
 * 票 15：追问 analyst（AG-UI 1.0 流式交互面，V4 产品形态定案）。
 *
 * useChat + 自定义 transport（@ag-ui/client，A 案冻结）；后端只注入
 * 该场已存证情报——引用徽章按证据链情报渲染（来源+时点），证据弱点
 * 由 analyst 在回答末尾明说。无情报场次走后端诚实降级话术。
 *
 * 票 04：徽章按各条回答的决策时刻过滤（面板级时点边界）——本栈
 * useChat 不给消息打 createdAt，提交时（=后端 ask 的 moment）入队
 * 盖戳，助手消息按到达顺序领戳；晚于决策时刻采集的情报不回溯挂
 * 徽章（refetch 进来的新情报不会假装当时已可知）。残余：客户端与
 * 服务端时钟偏差会平移戳点（面板级容忍，不追服务端 recorded_at）。
 */

type ChatMessage = { id: string; role: string };

export function AskAnalystPanel({ fixtureId, intels }: { fixtureId: number; intels: IntelItem[] }) {
	const transport = useMemo(() => createAskTransport(fixtureId), [fixtureId]);
	const chat = useChat({ id: `ask-${fixtureId}`, transport });
	const [input, setInput] = useState("");
	const decidedAt = useRef(new Map<string, number>());
	const pendingMoments = useRef<number[]>([]);
	for (const message of chat.messages as ChatMessage[]) {
		if (message.role === "assistant" && !decidedAt.current.has(message.id)) {
			// 提交时入队的决策时刻按序领取；队空（无对应提交）= 零徽章 fail-closed
			const moment = pendingMoments.current.shift();
			if (moment !== undefined) decidedAt.current.set(message.id, moment);
		}
	}

	// 决策时刻 as_of 过滤；无戳兜底 = 零徽章（泄漏边界 fail-closed）
	const intelsAsOf = (messageId: string): IntelItem[] => {
		const moment = decidedAt.current.get(messageId);
		return moment === undefined ? [] : intels.filter((intel) => new Date(intel.collected_at).getTime() <= moment);
	};

	function submit(event: FormEvent) {
		event.preventDefault();
		const text = input.trim();
		if (!text || chat.status === "submitted" || chat.status === "streaming") {
			return;
		}
		pendingMoments.current.push(Date.now()); // 决策时刻 = 提问时，非回答渲染时
		void chat.sendMessage({ text });
		setInput("");
	}

	return (
		<div className="mt-2 rounded-md border border-border p-3" data-testid="ask-panel">
			<p className="text-xs text-muted-foreground">
				回答只引用上方已存证情报（来源/时点随徽章）；证据覆盖不足处 analyst 会明说——advisory 参考，不改注策。
			</p>
			<div className="mt-2 space-y-2" data-testid="ask-messages">
				{chat.messages.map((message) => {
					const cited = message.role === "assistant" && chat.status !== "streaming" ? intelsAsOf(message.id) : [];
					return (
						<div
							key={message.id}
							className={
								message.role === "user"
									? "ml-auto w-fit max-w-[85%] rounded-lg bg-info/10 px-3 py-1.5 text-sm"
									: "max-w-[90%] rounded-lg border border-border bg-background px-3 py-1.5 text-sm"
							}
							data-testid={`ask-msg-${message.role}`}
						>
							<span>
								{message.parts
									.filter((part): part is { type: "text"; text: string } => part.type === "text")
									.map((part) => part.text)
									.join("")}
							</span>
							{message.role === "assistant" &&
							chat.status === "streaming" &&
							chat.messages.at(-1)?.id === message.id ? (
								<span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-info align-middle" aria-hidden />
							) : null}
							{cited.length > 0 ? (
								<span className="mt-1.5 flex flex-wrap gap-1" data-testid="ask-citations">
									{cited.map((intel) => (
										<span
											key={`${intel.kind}-${intel.collected_at}-${intel.text}`}
											className="rounded bg-info/10 px-1 text-xs text-info"
											title={intel.text}
										>
											{intel.source} · {shortTime(intel.collected_at)}
										</span>
									))}
								</span>
							) : null}
						</div>
					);
				})}
			</div>
			{chat.error ? (
				<p className="mt-1 text-xs text-destructive" data-testid="ask-error">
					{chat.error instanceof Error ? chat.error.message : "追问失败"}——可重试或稍后再问。
					<button type="button" className="ml-1 underline" onClick={() => chat.clearError()}>
						清除
					</button>
				</p>
			) : null}
			<form className="mt-2 flex gap-2" onSubmit={submit}>
				<input
					value={input}
					onChange={(event) => setInput(event.target.value)}
					placeholder="例如：这场为什么把负概率上调？"
					aria-label="追问 analyst"
					data-testid="ask-input"
					className="flex-1 rounded-md border border-input bg-background px-2 py-1.5 text-sm"
				/>
				<button
					type="submit"
					disabled={!input.trim() || chat.status === "submitted" || chat.status === "streaming"}
					data-testid="ask-submit"
					className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-40"
				>
					{chat.status === "streaming" ? "回答中…" : "追问"}
				</button>
			</form>
			<p className={`mt-1 text-xs text-muted-foreground ${TABULAR_NUMS}`}>
				每次追问经 GLM（analyst 档）计费并记入成本台账；无情报场次不调模型。
			</p>
		</div>
	);
}
