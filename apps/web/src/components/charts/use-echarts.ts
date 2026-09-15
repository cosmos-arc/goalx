/** use-echarts：init/dispose/setOption/ResizeObserver 薄封装（票 11）。
 *
 * 测试约定：jsdom 无 canvas，单测 `vi.mock` 本模块依赖的 `echarts/core`；
 * 数据 → option 的映射写成纯函数直接断言，渲染交给 Playwright e2e。
 */
import { type RefObject, useEffect, useRef } from "react";
import { type ECharts, type EChartsOption, echarts } from "./echarts";

export function useECharts(option: EChartsOption): RefObject<HTMLDivElement | null> {
	const containerRef = useRef<HTMLDivElement>(null);
	const chartRef = useRef<ECharts | null>(null);

	useEffect(() => {
		const container = containerRef.current;
		if (!container) {
			return;
		}
		const chart = echarts.init(container);
		chartRef.current = chart;
		const observer = new ResizeObserver(() => chart.resize());
		observer.observe(container);
		return () => {
			observer.disconnect();
			chart.dispose();
			chartRef.current = null;
		};
	}, []);

	useEffect(() => {
		// notMerge：option 整体替换，避免 series 数量变化时残留旧系列。
		chartRef.current?.setOption(option, { notMerge: true });
	});

	return containerRef;
}
