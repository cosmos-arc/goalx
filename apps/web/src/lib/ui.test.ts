import { describe, expect, test } from "vitest";
import { addDays, beijingBusinessDate, currentQuotes, dayLabel, errorText, localTime, shortTime } from "./ui";

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

describe("currentQuotes（批A #20b：逐字段校验对齐 intentConflict）", () => {
	const quote = {
		fixture_id: 1,
		selection_code: "h",
		server_odds: 2.05,
		snapshot_id: 7,
		observed_at: "2026-11-05T10:00:00Z",
	};

	test("合法价变载荷 → 数组", () => {
		expect(currentQuotes({ detail: { message: "报价已变", current_quotes: [quote] } })).toEqual([quote]);
	});

	test("非该形态 / 空数组 → null", () => {
		expect(currentQuotes({ detail: "别的错" })).toBeNull();
		expect(currentQuotes({ detail: { current_quotes: [] } })).toBeNull();
		expect(currentQuotes(new Error("x"))).toBeNull();
	});

	test("字段畸形 → 整体 null（不 partial 放行，防消费者 .toFixed 自崩）", () => {
		const badOdds = { ...quote, server_odds: "2.05" };
		const badCode = { ...quote, selection_code: 3 };
		const nullElement = null;
		expect(currentQuotes({ detail: { current_quotes: [quote, badOdds] } })).toBeNull();
		expect(currentQuotes({ detail: { current_quotes: [badCode] } })).toBeNull();
		expect(currentQuotes({ detail: { current_quotes: [nullElement] } })).toBeNull();
	});
});

describe("localTime/shortTime（报告二 §八：显式 Asia/Shanghai）", () => {
	test("固定北京时区字面（原 shortTime 切 ISO=UTC、localTime 随浏览器）", () => {
		// 2026-09-11T10:00:00Z = 北京 18:00
		expect(localTime("2026-09-11T10:00:00+00:00")).toBe("18:00");
		expect(shortTime("2026-09-11T10:00:00+00:00")).toBe("09-11 18:00");
		// 2026-09-12T16:00:00Z = 北京 09-13 00:00（跨日界 + 零点不出 24:xx）
		expect(shortTime("2026-09-12T16:00:00+00:00")).toBe("09-13 00:00");
		expect(localTime("2026-09-12T16:00:00+00:00")).toBe("00:00");
		// 带偏移输入按绝对时刻换算（非字面平移）
		expect(localTime("2026-09-11T18:00:00+08:00")).toBe("18:00");
	});

	test("非法/空 → 占位", () => {
		expect(localTime("not-a-date")).toBe("—");
		expect(shortTime(null)).toBe("—");
	});
});
