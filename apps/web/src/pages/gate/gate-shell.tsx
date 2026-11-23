import type { ReactNode } from "react";

/**
 * Shared chassis for the S2 (deactivated) and S2b (access unverified) gate screens:
 * full-viewport centered column on --background, brand mark above a single
 * hero-tier card (rounded-xl p-5, hairline border, shadow-xs max), content
 * capped max-w-2xl. No sidebar/nav — pre-provisioned and fenced users have
 * no dashboard to navigate (UI-SPEC S1/S2).
 */
export function GateShell({ children }: { children: ReactNode }) {
	return (
		<main className="flex min-h-dvh flex-col items-center justify-center bg-background px-4 py-8">
			<div className="flex w-full max-w-2xl flex-col items-center gap-6">
				<img
					src="/iforge-logo-transparent.png"
					alt="iforge"
					className="size-10 shrink-0 rounded-lg"
				/>
				<section className="w-full rounded-xl border border-border bg-card p-5 shadow-xs">
					{children}
				</section>
			</div>
		</main>
	);
}
