import type { UseQueryResult } from "@tanstack/react-query";
import type { PoolSlipPreview } from "../api/goalx";

/**
 * 池票预览新鲜度门（报告审计票 07）：提交金额只认当前 picks 签名的预览。
 * placeholderData 旧占位可保留展示（体验），但提交门不得放行陈旧占位——
 * 9 场"1 注 ¥2"加选后仍按旧金额提交、后端实建 10 注 ¥20 的错位由这里堵。
 * 任9 / goals4 两页共用（门 + 标签 + 状态行），忌两份漂移实现。
 */
export type PoolPreviewGate = {
	/** 当前签名预览已完成且非占位——提交门唯一放行条件。 */
	submittable: boolean;
	/** null = 新鲜；loading = 未完成或旧占位；error = 请求失败（可重试）。 */
	status: "loading" | "error" | null;
};

export function poolPreviewGate(query: UseQueryResult<PoolSlipPreview, Error>): PoolPreviewGate {
	if (query.isError) {
		return { submittable: false, status: "error" };
	}
	// isPlaceholderData = 展示旧签名占位；isPending = 当前签名尚无数据（含
	// enabled=false）。同签名后台重拍不算陈旧——组合数对同载荷是确定的。
	if (query.isPending || query.isPlaceholderData) {
		return { submittable: false, status: "loading" };
	}
	return { submittable: true, status: null };
}

/** 门未放行时的按钮标签；放行时 null（页面用缺场/提交中等自己的禁用项优先）。 */
export function poolPreviewBlockLabel(gate: PoolPreviewGate): string | null {
	if (gate.submittable) {
		return null;
	}
	return gate.status === "error" ? "预览失败（不可提交）" : "预览更新中…";
}

/** 门未放行的状态行（失败带重试）；testid 派生 `${testid}-retry` 重试按钮。 */
export function PoolPreviewStatus({
	gate,
	onRetry,
	testid,
}: {
	gate: PoolPreviewGate;
	onRetry: () => void;
	testid: string;
}) {
	if (gate.status === null) {
		return null;
	}
	return (
		<p className="mt-2 text-xs text-muted-foreground" data-testid={testid}>
			{gate.status === "error" ? (
				<>
					预览失败：组合数/金额未确认（旧金额不可提交）。
					<button
						type="button"
						data-testid={`${testid}-retry`}
						className="ml-2 rounded-md border border-border px-2 py-0.5 transition-colors hover:bg-muted"
						onClick={onRetry}
					>
						重试
					</button>
				</>
			) : (
				"预览更新中：确认金额以新预览为准，未完成前不可提交。"
			)}
		</p>
	);
}
