import type { ReactNode } from "react";
import { useId, useState } from "react";
import { type GlossaryId, glossaryEntry } from "../lib/glossary";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "./ui/tooltip";

/**
 * 票 18 页内指标名 tooltip（票 08 定稿形态）：指标名虚线下划线，悬停/聚焦出
 * Popover 气泡显示定义 + 判读方向。触发器是原生 button——键盘 Tab 可聚焦、
 * 聚焦即展开（Base UI useFocus）。aria-describedby 由本组件显式接线（仅展开时
 * 挂到触发器上，指向气泡 id；Base UI 1.8 把 tooltip 视为纯视觉元素不自动挂，
 * 关闭时不挂避免悬空 idref 触发 axe）。
 * 克制约定：只接指标名，不逐词到处挂。
 * 气泡只放定义 + 判读方向（票 08 定稿两要素）；数字实例与口径警示在词典页词条卡上。
 */
export function GlossaryTerm({ id, children }: { id: GlossaryId; children?: ReactNode }) {
	const entry = glossaryEntry(id);
	if (!entry) {
		throw new Error(`glossary.ts 缺少词条 ${id}——页内 tooltip 接了不存在的指标名。`);
	}
	const [open, setOpen] = useState(false);
	const popupId = useId();
	return (
		<TooltipProvider>
			<Tooltip open={open} onOpenChange={setOpen}>
				<TooltipTrigger
					data-testid={`glossary-term-${id}`}
					aria-describedby={open ? popupId : undefined}
					className="cursor-help appearance-none border-0 bg-transparent p-0 text-left font-normal text-inherit underline decoration-dashed decoration-border underline-offset-4 hover:decoration-muted-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
				>
					{children ?? entry.term}
				</TooltipTrigger>
				<TooltipContent id={popupId} className="flex w-72 flex-col items-start gap-1.5 text-left">
					<span className="font-medium">{entry.term}</span>
					<span className="leading-relaxed">{entry.definition}</span>
					<span className="leading-relaxed opacity-90">判读：{entry.direction}</span>
					<span className="opacity-70">例与口径警示见词典页。</span>
				</TooltipContent>
			</Tooltip>
		</TooltipProvider>
	);
}
