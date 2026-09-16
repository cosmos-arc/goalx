/** ECharts 按需注册：全项目唯一 `echarts.use` 入口，新增图表/组件在此追加（票 11）。 */

import type { BarSeriesOption, LineSeriesOption } from "echarts/charts";
import { BarChart, LineChart } from "echarts/charts";
import type {
	GridComponentOption,
	LegendComponentOption,
	MarkLineComponentOption,
	TooltipComponentOption,
} from "echarts/components";
import { GridComponent, LegendComponent, MarkLineComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([LineChart, BarChart, GridComponent, TooltipComponent, MarkLineComponent, LegendComponent, CanvasRenderer]);

export type EChartsOption = echarts.ComposeOption<
	| LineSeriesOption
	| BarSeriesOption
	| GridComponentOption
	| TooltipComponentOption
	| MarkLineComponentOption
	| LegendComponentOption
>;

export type { ECharts } from "echarts/core";
export { echarts };
