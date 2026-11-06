/**
 * 手册截图采集的后端探活(correctness F2):标题等待门是静态壳(app-shell 的
 * h1 不依赖 API),后端宕机时 10 张降级态页会"全绿"覆盖已入册 PNG——采集前
 * fail-loud;确要拍降级态(记录骨架态)显式 GUIDE_ALLOW_DEGRADED=1。
 */
export default async function guideGlobalSetup(): Promise<void> {
	if (process.env["GUIDE_ALLOW_DEGRADED"] === "1") return;
	const base = process.env["GOALX_GUIDE_API_BASE"] ?? "http://127.0.0.1:8000";
	const res = await fetch(`${base}/healthz`).catch(() => null);
	if (!res?.ok) {
		throw new Error(
			`后端 ${base} 不在(/healthz ${res ? res.status : "拒绝连接"})——降级态拍摄会覆盖入册 PNG;` +
				"先起 serve 栈,或显式 GUIDE_ALLOW_DEGRADED=1",
		);
	}
}
