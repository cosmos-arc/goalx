// correctness F3：flag→词条映射零防护——映射漂移（词条改名/删词）会整页
// 白屏（GlossaryTerm 缺词条 throw），单测钉住两映射与词典/标签集一致。
import { describe, expect, test } from "vitest";
import { GLOSSARY } from "../lib/glossary";
import { FLAG_GLOSSARY, FLAG_LABELS } from "./had-quote-ui";

describe("flag 映射一致性", () => {
	test("FLAG_GLOSSARY 值都是词典 id，键都在 FLAG_LABELS", () => {
		const ids = new Set(GLOSSARY.map((entry) => entry.id));
		for (const [flag, id] of Object.entries(FLAG_GLOSSARY)) {
			expect(ids.has(id), `词条 ${id} 不存在（flag ${flag}）`).toBe(true);
			expect(Object.hasOwn(FLAG_LABELS, flag), `flag ${flag} 缺标签`).toBe(true);
		}
	});
});
