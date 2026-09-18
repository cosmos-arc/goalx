import { describe, expect, test } from "vitest";
import { addDays, beijingBusinessDate, dayLabel, errorText } from "./ui";

/**
 * 票 wb-03：日期/错误工具从 fixtures-page 下沉到 lib/ui 后的纯函数单测
 * （北京业务日口径、dayLabel 各档、errorText 各形态）。
 */

describe("beijingBusinessDate / addDays", () => {
	test("北京时区业务日：UTC 16:00 前后跨日", () => {
		// 北京 = UTC+8：09-16 15:59 UTC = 09-16 23:59 北京 → 业务日 09-16
		expect(beijingBusinessDate(Date.parse("2026-09-16T15:59:00Z"))).toBe("2026-09-16");
		// 09-16 16:00 UTC = 09-17 00:00 北京 → 业务日 09-17
		expect(beijingBusinessDate(Date.parse("2026-09-16T16:00:00Z"))).toBe("2026-09-17");
	});

	test("addDays 跨月进位", () => {
		expect(addDays("2026-09-30", 1)).toBe("2026-10-01");
		expect(addDays("2026-09-17", -1)).toBe("2026-09-16");
	});
});

describe("dayLabel", () => {
	const now = Date.parse("2026-09-17T10:00:00Z");

	test("今天/明天/后天三档", () => {
		expect(dayLabel("2026-09-17", now)).toBe("今天");
		expect(dayLabel("2026-09-18", now)).toBe("明天");
		expect(dayLabel("2026-09-19", now)).toBe("后天");
	});

	test("窗口外落 MM月DD", () => {
		expect(dayLabel("2026-09-21", now)).toBe("09月21");
		expect(dayLabel("2026-10-02", now)).toBe("10月02");
	});
});

describe("errorText", () => {
	test("detail 字符串/对象/非对象三形态", () => {
		expect(errorText({ detail: "fixture 1 不可投" })).toBe("fixture 1 不可投");
		expect(errorText({ detail: { reason: "sale_stopped" } })).toBe('{"reason":"sale_stopped"}');
		expect(errorText(new Error("boom"))).toContain("boom");
	});
});
