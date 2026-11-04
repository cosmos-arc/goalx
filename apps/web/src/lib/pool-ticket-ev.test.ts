import { expect, test } from "vitest";
import { POOL_RETURN_RATE, poolTicketAdvice } from "./pool-ticket-ev";

/**
 * 彩池票级 EV 数值锚（报告审计票 08）：返奖率整票只扣一次。
 * 报告二判别样例：四场各 p/s=1.25 → 票级 +58.69%；串关连乘（R⁴）会得
 * −56.42%——正负判反。后端镜像 = pool_strategy.ticket_view（同数字测试）。
 */

test("ticket EV applies the return rate once across the whole ticket", () => {
	const legs = Array.from({ length: 4 }, () => ({ prob: 0.5, share: 0.4 })); // p/s = 1.25
	const advice = poolTicketAdvice(legs);
	expect(advice).not.toBeNull();
	expect(advice?.ev).toBeCloseTo(0.65 * 1.25 ** 4 - 1, 6);
	expect(advice?.ev).toBeCloseTo(0.5869, 4);
	expect(advice?.odds).toBeCloseTo(POOL_RETURN_RATE / 0.4 ** 4, 6);
});

test("missing prob or share degrades to null (维持缺份额降级语义)", () => {
	expect(poolTicketAdvice([{ prob: 0.5, share: null }])).toBeNull();
	expect(poolTicketAdvice([{ prob: null, share: 0.4 }])).toBeNull();
	expect(poolTicketAdvice([{ prob: 0.5, share: 0 }])).toBeNull();
	// 单腿缺数据毒化整票
	expect(
		poolTicketAdvice([
			{ prob: 0.5, share: 0.4 },
			{ prob: 0.5, share: null },
		]),
	).toBeNull();
});
