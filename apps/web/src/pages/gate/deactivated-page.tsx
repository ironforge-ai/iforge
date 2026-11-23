import { DESTINATIONS } from "@clawdi/shared";
import { Button } from "@/components/ui/button";
import { GateShell } from "@/pages/gate/gate-shell";

/**
 * S2 — fenced / locked-out screen. Same chassis as S1, separate copy, and
 * deliberately NO code input: a fenced principal must never see an invite
 * form that implies re-entry (UI-SPEC S2). Static house copy only — no user
 * or data text renders on this surface.
 */
export function DeactivatedPage() {
	return (
		<GateShell>
			<div className="flex flex-col items-start gap-3">
				<h1 className="text-xl leading-[1.1] font-semibold tracking-tight">
					This account is deactivated
				</h1>
				<p className="text-sm text-muted-foreground">
					Your account has been closed and can no longer access iforge. If you think this is a
					mistake, contact support.
				</p>
				<Button
					variant="outline"
					render={<a href={`mailto:${DESTINATIONS.supportEmail}`} />}
					nativeButton={false}
					className="mt-1"
				>
					Contact support
				</Button>
			</div>
		</GateShell>
	);
}
