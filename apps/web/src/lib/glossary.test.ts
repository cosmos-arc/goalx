import { expect, test } from "vitest";
import { filterGlossary, GLOSSARY, glossaryEntry } from "./glossary";

/**
 * 票 18 数据完整性门（"三要素齐全才上线"）：词条齐、id 唯一、
 * definition / direction / example / caution 非空——判读方向为必填要素，
 * 缺失的词条不上线（票 18 不变量）。票 wb-02 增补研究页两词条（12 条）。
 */
test("all agreed entries are present with unique ids", () => {
	expect(GLOSSARY).toHaveLength(12);
	expect(GLOSSARY.map((entry) => entry.id)).toEqual([
		"ev",
		"eu-consensus",
		"eligibility",
		"single",
		"books",
		"paper-vs-live",
		"pnl-roi",
		"forward-inclusion",
		"clv",
		"skill",
		"model-prob",
		"book-deviation",
	]);
	expect(new Set(GLOSSARY.map((entry) => entry.id)).size).toBe(GLOSSARY.length);
});

test("every entry carries the full three elements plus caution (no gaps allowed)", () => {
	for (const entry of GLOSSARY) {
		expect(entry.term.trim(), `${entry.id}: term`).not.toBe("");
		expect(entry.definition.trim(), `${entry.id}: definition`).not.toBe("");
		// 判读方向必填（票 18 不变量）
		expect(entry.direction.trim(), `${entry.id}: direction`).not.toBe("");
		expect(entry.example.trim(), `${entry.id}: example`).not.toBe("");
		// 口径警示不得删减（如"可投≠必成交""EV 是诊断量""三条件只认前瞻"）
		expect(entry.caution.trim(), `${entry.id}: caution`).not.toBe("");
	}
});

test("agreed calibers keep their warnings verbatim", () => {
	const byId = new Map(GLOSSARY.map((entry) => [entry.id, entry]));
	expect(byId.get("eligibility")?.caution).toContain("可投 ≠ 必成交");
	expect(byId.get("ev")?.caution).toContain("诊断量，不是机会信号");
	expect(byId.get("skill")?.caution).toContain("三条件只认前瞻");
	expect(byId.get("skill")?.caution).toContain("不可作实盘依据");
	expect(byId.get("forward-inclusion")?.definition).toContain("post_kickoff_only");
	expect(byId.get("clv")?.caution).toContain("腿数不凑");
});

test("glossaryEntry resolves by id and misses cleanly", () => {
	expect(glossaryEntry("ev")?.term).toBe("EV");
	expect(glossaryEntry("nope")).toBeUndefined();
});

test("filterGlossary matches term, alias and definition, case-insensitively", () => {
	// 空查询 = 全部（保持原顺序）
	expect(filterGlossary("")).toHaveLength(12);
	expect(filterGlossary("   ")).toHaveLength(12);
	// term 命中
	expect(filterGlossary("单固").map((entry) => entry.id)).toEqual(["single"]);
	// 别名命中（大小写不敏感）
	expect(filterGlossary("clv_prob").map((entry) => entry.id)).toEqual(["clv"]);
	expect(filterGlossary("PAPER").map((entry) => entry.id)).toEqual(["paper-vs-live"]);
	// definition 命中
	const walkForward = filterGlossary("walk-forward").map((entry) => entry.id);
	expect(walkForward).toContain("skill");
	// 无命中 = 空
	expect(filterGlossary("量子纠缠")).toEqual([]);
});
