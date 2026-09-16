import { StubPage } from "./stub-page";

/** 票 15（Dashboard）落地前的总览引导态：分诊页——数据健康 + 今日待办；占位期引导去今日。 */
export function OverviewPage() {
	return (
		<StubPage
			title="总览"
			message="总览页将集中数据健康与今日待办，作为每天的入口。"
			hint="随票 15（Dashboard）上线。"
			action={{ label: "先去今日看盘", to: "/today" }}
		/>
	);
}
