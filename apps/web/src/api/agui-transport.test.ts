import type { Event as AgUiEvent } from "@ag-ui/core";
import { expect, test, vi } from "vitest";

/**
 * 票 08② 取消接线单测：外部 abortSignal → fetch signal 真断流、订阅
 * 句柄保存并在收尾/取消时退订、用户取消不冒充 error chunk。
 * HttpAgent 以 vi.mock 替身（协议解析不属本测面——只钉 transport 接线）。
 */

const agentInstances: FakeAgent[] = [];

/** 悬挂开关：置 true 时下一个 run 不自动 complete（测流进行中取消）。 */
let hangNext = false;

class FakeAgent {
	opts: AgentOpts;
	abortRun = vi.fn();
	observable: FakeObservable;

	constructor(opts: AgentOpts) {
		this.opts = opts;
		this.observable = new FakeObservable(hangNext);
		agentInstances.push(this);
	}

	run() {
		return this.observable;
	}
}

type AgentOpts = {
	url: string;
	fetch: (url: string, init?: RequestInit) => Promise<Response>;
};

class FakeObservable {
	unsubscribe = vi.fn();
	observer: { next: (event: AgUiEvent) => void; error: (err: unknown) => void; complete: () => void } | null = null;
	hang: boolean;

	constructor(hang: boolean) {
		this.hang = hang;
	}

	subscribe(observer: { next: (event: AgUiEvent) => void; error: (err: unknown) => void; complete: () => void }) {
		this.observer = observer;
		const finish = () => {
			observer.next({ type: "TEXT_MESSAGE_START", messageId: "m1" } as AgUiEvent);
			observer.next({ type: "TEXT_MESSAGE_CONTENT", messageId: "m1", delta: "回答" } as AgUiEvent);
			observer.next({ type: "TEXT_MESSAGE_END", messageId: "m1" } as AgUiEvent);
			observer.complete();
		};
		if (this.hang) {
			// 悬挂：只发一个 delta，完成时机交给测试（模拟流进行中）
			queueMicrotask(() => observer.next({ type: "TEXT_MESSAGE_START", messageId: "m1" } as AgUiEvent));
		} else {
			queueMicrotask(finish);
		}
		return { unsubscribe: this.unsubscribe };
	}
}

vi.mock("@ag-ui/client", () => ({ HttpAgent: FakeAgent }));

/** 最小 UIMessage 形状（transport 只消费 id/role/parts）。 */
function userMessage() {
	return { id: "m", role: "user", parts: [] } as never;
}

/** sendMessages 首参最小构造（缺省字段按 ai 类型补全）。 */
function sendArgs(extra: Record<string, unknown> = {}) {
	return {
		trigger: "submit-message",
		chatId: "test",
		messageId: undefined,
		messages: [userMessage()],
		...extra,
	} as never;
}

test("外部 abortSignal 接到 fetch：流进行中 abort 后 fetch signal 随断", async () => {
	const external = new AbortController();
	const recordedSignals: AbortSignal[] = [];
	const fetchStub = vi.fn((_url: unknown, _init?: RequestInit) =>
		Promise.resolve(new Response("data: {}\n\n", { headers: { "content-type": "text/event-stream" } })),
	);
	vi.stubGlobal("fetch", (url: string | URL | Request, init?: RequestInit) => {
		recordedSignals.push(init?.signal as AbortSignal);
		return fetchStub(url, init);
	});
	hangNext = true; // 本用例的 run 保持流进行中（不自动 complete）
	try {
		const { createAskTransport } = await import("./agui-transport");
		const transport = createAskTransport(7);
		const stream = await transport.sendMessages(sendArgs({ abortSignal: external.signal }));
		const agent = agentInstances.at(-1);

		// HttpAgent 的 fetch 已带本地取消 signal（票 08② 死接线主断点）
		await agent!.opts.fetch("http://t/ask");
		expect(recordedSignals).toHaveLength(1);
		expect(recordedSignals[0]?.aborted).toBe(false);

		// 流仍进行中（悬挂 observable 未 complete）：外部 abort 必须传导
		external.abort();
		expect(recordedSignals[0]?.aborted).toBe(true);
		await stream.cancel("test-over").catch(() => undefined);
		expect(agent!.abortRun).toHaveBeenCalled();
	} finally {
		hangNext = false;
		vi.unstubAllGlobals();
	}
});

test("正常完成：事件流转为 chunk，收尾退订", async () => {
	const { createAskTransport } = await import("./agui-transport");
	const transport = createAskTransport(8);
	const stream = await transport.sendMessages(sendArgs());
	const chunks: string[] = [];
	const reader = stream.getReader();
	for (;;) {
		const { done, value } = await reader.read();
		if (done) break;
		chunks.push(value.type);
	}
	expect(chunks).toEqual(["start", "text-start", "text-delta", "text-end", "finish"]);
	expect(agentInstances.at(-1)?.observable.unsubscribe).toHaveBeenCalled();
});

test("流取消：abortRun + 退订一起收尾", async () => {
	const { createAskTransport } = await import("./agui-transport");
	const transport = createAskTransport(9);
	const stream = await transport.sendMessages(sendArgs());
	await stream.cancel("user-stop");
	const agent = agentInstances.at(-1);
	expect(agent?.abortRun).toHaveBeenCalled();
	expect(agent?.observable.unsubscribe).toHaveBeenCalled();
});
