import { HttpAgent } from "@ag-ui/client";
import type { Event as AgUiEvent, Message as AgUiMessage } from "@ag-ui/core";
import type { ChatTransport, UIMessage, UIMessageChunk } from "ai";
import { apiBaseUrl } from "./client";

/**
 * 票 15：ai-sdk useChat 的 AG-UI transport（A 案定案：@ag-ui/client）。
 *
 * ai UIMessage[] → AG-UI RunAgentInput；HttpAgent.run() 的 AG-UI 1.0
 * 事件流 → UIMessageChunk（start/text-start/text-delta/text-end/finish，
 * RUN_ERROR → error chunk）。fetch 取调用时 thunk——msw 拦截才生效
 * （同 client.ts 的坑）。
 */

/** ai 消息 → AG-UI 消息（文本 parts 拼接为 content）。 */
function toAgUiMessage(message: UIMessage): AgUiMessage {
	const content = message.parts
		.filter((part): part is { type: "text"; text: string } => part.type === "text")
		.map((part) => part.text)
		.join("");
	return { id: message.id, role: message.role, content } as unknown as AgUiMessage;
}

/** run() 观察者的结构类型（避免跨隔离实例 import rxjs）。 */
interface AgUiObservable {
	subscribe(observer: { next: (event: AgUiEvent) => void; error: (err: unknown) => void; complete: () => void }): {
		unsubscribe(): void;
	};
}

export function createAskTransport(fixtureId: number): ChatTransport<UIMessage> {
	return {
		async sendMessages({ messages, abortSignal }) {
			const agent = new HttpAgent({
				url: `${apiBaseUrl}/api/v1/fixtures/${fixtureId}/ask`,
				fetch: (url, init) => globalThis.fetch(url, init),
			});
			const controller = new AbortController();
			const onAbort = () => controller.abort();
			abortSignal?.addEventListener("abort", onAbort, { once: true });

			return new ReadableStream<UIMessageChunk>({
				async start(stream) {
					stream.enqueue({ type: "start", messageId: crypto.randomUUID() });
					try {
						// run() 的返回标注是宽松 BaseEvent——按 AG-UI 判别联合消费
						const events = agent.run({
							threadId: `fixture-${fixtureId}`,
							runId: crypto.randomUUID(),
							messages: messages.map(toAgUiMessage),
							tools: [],
							context: [],
						}) as unknown as AgUiObservable;
						await new Promise<void>((resolve, reject) => {
							events.subscribe({
								next: (event) => {
									switch (event.type) {
										case "TEXT_MESSAGE_START":
											stream.enqueue({ type: "text-start", id: event.messageId });
											break;
										case "TEXT_MESSAGE_CONTENT":
											stream.enqueue({ type: "text-delta", id: event.messageId, delta: event.delta });
											break;
										case "TEXT_MESSAGE_END":
											stream.enqueue({ type: "text-end", id: event.messageId });
											break;
										case "RUN_ERROR":
											stream.enqueue({ type: "error", errorText: event.message ?? "追问失败" });
											break;
										default:
											break;
									}
								},
								error: (err: unknown) => reject(err),
								complete: () => resolve(),
							});
						});
						stream.enqueue({ type: "finish" });
					} catch (error) {
						stream.enqueue({
							type: "error",
							errorText: error instanceof Error ? error.message : "追问失败（网络不可达）",
						});
					} finally {
						abortSignal?.removeEventListener("abort", onAbort);
					}
					stream.close();
				},
				cancel() {
					agent.abortRun();
				},
			});
		},
		// 本地单用户、每次追问独立 run——无需断线重连
		async reconnectToStream() {
			return null;
		},
	};
}
