import { useEffect, useState } from "react";

/**
 * 共享滴答时钟（报告二 §八 UI 小件）：30s 一拍——页面倒计时/年龄读数
 * 不再各拿一次性 Date.now() 冻结到下次无关重渲染。组件级调用，
 * 页面间同拍一致（报告措辞：倒计时共享时钟）。
 */
export function useNow(intervalMs = 30_000): number {
	const [now, setNow] = useState(() => Date.now());
	useEffect(() => {
		const timer = setInterval(() => setNow(Date.now()), intervalMs);
		return () => clearInterval(timer);
	}, [intervalMs]);
	return now;
}
