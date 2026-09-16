import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";
import type { AppRoute } from "../lib/ui";
import { Button, buttonVariants } from "./ui/button";

/**
 * 票 03 定稿的三态空状态（票 13 统一组件）：一句人话 + 至多一个动作按钮，
 * 不卖萌不插画。三态：
 * - no-data：无数据——说明为何空、何时有，配刷新/重试；
 * - backend-unavailable：后端不可用——保留 task server / task ingest-jingcai 指引 + 重试；
 * - not-available：功能未达——一句话说明这里是什么、何时来，不装死页。
 */
export type EmptyStateVariant = "no-data" | "backend-unavailable" | "not-available";

/** 动作二选一：页内回调（刷新/重试）或站内跳转。 */
export type EmptyStateAction = { label: string; onClick: () => void } | { label: string; to: AppRoute };

type EmptyStateProps = {
	variant: EmptyStateVariant;
	/** 一句人话：为什么空 / 这里是什么。 */
	message: string;
	/** 补充一行：何时有数据、启动指引等（可含 code 片段）。 */
	hint?: ReactNode | undefined;
	/** 唯一动作；功能未达态可省。 */
	action?: EmptyStateAction | undefined;
};

export function EmptyState({ variant, message, hint, action }: EmptyStateProps) {
	return (
		<div
			data-testid="empty-state"
			data-variant={variant}
			className="rounded-lg border border-dashed border-border p-8 text-center"
		>
			<p className="text-sm text-foreground">{message}</p>
			{hint ? <p className="mt-1 text-xs text-muted-foreground">{hint}</p> : null}
			{action ? (
				<div className="mt-4 flex justify-center">
					{"to" in action ? (
						<Link to={action.to} className={buttonVariants({ variant: "outline", size: "sm" })}>
							{action.label}
						</Link>
					) : (
						<Button variant="outline" size="sm" onClick={action.onClick}>
							{action.label}
						</Button>
					)}
				</div>
			) : null}
		</div>
	);
}
