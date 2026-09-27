import { Badge } from "./ui/badge";

/** 注状态徽标（lean-audit 票 03 收敛：投注/历史两页逐字同实现，5 键超集）。 */
const STATUS_META: Record<string, { label: string; className: string }> = {
	open: { label: "未结", className: "bg-muted text-muted-foreground" },
	won: { label: "胜", className: "bg-profit/10 text-foreground" },
	lost: { label: "负", className: "bg-loss/10 text-foreground" },
	void: { label: "退款", className: "bg-muted text-muted-foreground" },
	partial: { label: "部分", className: "bg-warning/10 text-foreground" },
};

export function StatusBadge({ status }: { status: string }) {
	const meta = STATUS_META[status];
	if (!meta) {
		return <span className="text-xs text-muted-foreground">{status}</span>;
	}
	return (
		<Badge variant="outline" className={`border-transparent ${meta.className}`}>
			{meta.label}
		</Badge>
	);
}
