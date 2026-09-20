import { useChat } from "@ai-sdk/react";
import { type FormEvent, useMemo, useState } from "react";
import { createAskTransport } from "../api/agui-transport";
import type { IntelItem } from "../api/goalx";
import { TABULAR_NUMS } from "../lib/ui";

/**
 * 票 15：追问 analyst（AG-UI 1.0 流式交互面，V4 产品形态定案）。
 *
 * useChat + 自定义 transport（@ag-ui/client，A 案冻结）；后端只注入
 * 该场已存证情报——引用徽章按证据链情报渲染（来源+时点），证据弱点
 * 由 analyst 在回答末尾明说。无情报场次走后端诚实降级话术。
 */

export function AskAnalystPanel({ fixtureId, intels }: { fixtureId: number; intels: IntelItem[] }) {
	const transport = useMemo(() => createAskTransport(fixtureId), [fixtureId]);
	const chat = useChat({ id: `ask-${fixtureId}`, transport });
	const [input, setInput] = useState("");

	function submit(event: FormEvent) {
		event.preventDefault();
		const text = input.trim();
		if (!text || chat.status === "submitted" || chat.status === "streaming") {
			return;
		}
		void chat.sendMessage({ text });
		setInput("");
	}

	return (
		<div className="mt-2 rounded-md border border-border p-3" data-testid="ask-panel">
			<p className="text-xs text-muted-foreground">
				回答只引用上方已存证情报（来源/时点随徽章）；证据覆盖不足处 analyst 会明说——advisory 参考，不改注策。
			</p>
			<div className="mt-2 space-y-2" data-testid="ask-messages">
				{chat.messages.map((message) => (
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
						{message.role === "assistant" && chat.status === "streaming" && chat.messages.at(-1)?.id === message.id ? (
							<span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-info align-middle" aria-hidden />
						) : null}
						{message.role === "assistant" && intels.length > 0 && chat.status !== "streaming" ? (
							<span className="mt-1.5 flex flex-wrap gap-1" data-testid="ask-citations">
								{intels.map((intel) => (
									<span
										key={`${intel.kind}-${intel.collected_at}-${intel.text}`}
										className="rounded bg-info/10 px-1 text-xs text-info"
										title={intel.text}
									>
										{intel.source} · {intel.collected_at.slice(5, 16).replace("T", " ")}
									</span>
								))}
							</span>
						) : null}
					</div>
				))}
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
