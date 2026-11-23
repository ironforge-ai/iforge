import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export function SectionLabel({
	children,
	count,
	leading,
	trailing,
	className,
}: {
	children: ReactNode;
	count?: ReactNode;
	leading?: ReactNode;
	/** Pushed to the row's far end — a chip or a control that governs the group. */
	trailing?: ReactNode;
	className?: string;
}) {
	return (
		<div className={cn("flex items-center gap-2 px-0.5", className)}>
			{leading ? <span className="shrink-0 text-sm leading-none">{leading}</span> : null}
			<span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
				{children}
			</span>
			{count !== undefined ? <span className="text-xs text-muted-foreground">{count}</span> : null}
			{trailing ? <div className="ml-auto flex shrink-0 items-center gap-2">{trailing}</div> : null}
		</div>
	);
}
