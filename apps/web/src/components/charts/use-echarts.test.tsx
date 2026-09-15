/** use-echarts 生命周期单测：echarts/core 全 mock，ResizeObserver 用可触发替身（票 11 测试策略示范）。 */
import { act, type RenderResult, render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { EChartsOption } from "./echarts";
import { useECharts } from "./use-echarts";

const initMock = vi.fn();

vi.mock("echarts/core", () => ({
	init: (...args: unknown[]) => initMock(...args),
	use: vi.fn(),
}));

const chartMock = {
	setOption: vi.fn(),
	dispose: vi.fn(),
	resize: vi.fn(),
};

class ResizeObserverMock {
	static instances: ResizeObserverMock[] = [];
	observe = vi.fn();
	unobserve = vi.fn();
	disconnect = vi.fn();
	callback: ResizeObserverCallback;
	constructor(callback: ResizeObserverCallback) {
		this.callback = callback;
		ResizeObserverMock.instances.push(this);
	}
}

vi.stubGlobal("ResizeObserver", ResizeObserverMock);

const baseOption: EChartsOption = { series: [{ type: "line", data: [1, 2, 3] }] };

function Chart({ option }: { option: EChartsOption }) {
	const ref = useECharts(option);
	return <div ref={ref} data-testid="chart" />;
}

describe("useECharts", () => {
	beforeEach(() => {
		initMock.mockReset();
		initMock.mockReturnValue(chartMock);
		chartMock.setOption.mockClear();
		chartMock.dispose.mockClear();
		chartMock.resize.mockClear();
		ResizeObserverMock.instances = [];
	});

	function renderChart(option: EChartsOption = baseOption): RenderResult {
		return render(<Chart option={option} />);
	}

	it("mount 时 init 容器并应用 option", () => {
		const { getByTestId } = renderChart();

		expect(initMock).toHaveBeenCalledTimes(1);
		expect(initMock).toHaveBeenCalledWith(getByTestId("chart"));
		expect(chartMock.setOption).toHaveBeenCalledWith(baseOption, { notMerge: true });
	});

	it("option 变化时 setOption 整体替换，不重复 init", () => {
		const { rerender } = renderChart();
		const next: EChartsOption = { series: [{ type: "bar", data: [4] }] };

		rerender(<Chart option={next} />);

		expect(initMock).toHaveBeenCalledTimes(1);
		expect(chartMock.setOption).toHaveBeenLastCalledWith(next, { notMerge: true });
	});

	it("容器尺寸变化时经 ResizeObserver 触发 resize", () => {
		renderChart();
		const observer = ResizeObserverMock.instances[0];

		expect(observer?.observe).toHaveBeenCalledWith(expect.any(HTMLDivElement));
		act(() => {
			observer?.callback([], observer as unknown as ResizeObserver);
		});

		expect(chartMock.resize).toHaveBeenCalledTimes(1);
	});

	it("unmount 时断开 observer 并 dispose", () => {
		const { unmount } = renderChart();

		unmount();

		expect(ResizeObserverMock.instances[0]?.disconnect).toHaveBeenCalledTimes(1);
		expect(chartMock.dispose).toHaveBeenCalledTimes(1);
	});

	it("ref 未挂载容器时不 init", () => {
		const Probe = () => {
			useECharts(baseOption);
			return null;
		};
		render(<Probe />);

		expect(initMock).not.toHaveBeenCalled();
		expect(chartMock.dispose).not.toHaveBeenCalled();
	});
});
