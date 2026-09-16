import * as jestDomMatchers from "@testing-library/jest-dom/matchers";
import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, expect, vi } from "vitest";
import { server } from "../mocks/server";

// The matcher-only entry point is workspace-isolation safe: unlike the
// convenience `/vitest` entry point, it does not resolve Vitest from inside
// jest-dom's package directory.
expect.extend(jestDomMatchers);

// jsdom 三件套（票 11）：
// 1) pointer-capture —— Base UI 组件依赖 hasPointerCapture，jsdom 未实现；
// 2) ResizeObserver —— jsdom 未实现，图表/组件测试如需触发回调，在测试内
//    用 vi.stubGlobal 覆盖为可驱动版本（见 use-echarts.test.tsx）；
// 3) canvas/echarts —— 无全局 mock：数据→option 映射保持纯函数直接单测，
//    hook 层在测试文件内 vi.mock("echarts/core")，渲染交给 Playwright e2e。
Object.defineProperties(window.HTMLElement.prototype, {
	hasPointerCapture: { value: () => false },
	setPointerCapture: { value: () => {} },
	releasePointerCapture: { value: () => {} },
});

class ResizeObserverStub implements ResizeObserver {
	observe(): void {}
	unobserve(): void {}
	disconnect(): void {}
}
vi.stubGlobal("ResizeObserver", ResizeObserverStub);

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
	server.resetHandlers();
	cleanup();
});
afterAll(() => server.close());
