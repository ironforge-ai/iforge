import { DEFAULT_SETTINGS_SECTION, type SettingsSectionId } from "@/lib/settings-routes";

/**
 * Settings while billing-locked (06.3 D-03, UI-SPEC L4).
 *
 * API Keys (`/v1/auth/keys`), AI Usage (`/v1/companions/spend*`) and Data
 * (`/v1/me/exports`) each call a gated route that answers 403
 * `billing_required` while locked, so they are hidden rather than shown
 * disabled. General, Wallet and Compute stay; the billing sections still follow
 * the dialog's existing `showBilling` rule.
 *
 * UX only (T-03-13): the server refuses every gated route regardless.
 * OSS-safe: imports only from `@/lib/settings-routes`.
 */
export const BILLING_LOCK_HIDDEN_SETTINGS_SECTIONS: ReadonlySet<SettingsSectionId> =
	new Set<SettingsSectionId>(["api-keys", "billing-usage", "data"]);

/** Whether a Settings section is listed given the billing lock. Every section shows when unlocked. */
export function isSettingsSectionVisibleWhileLocked(
	id: SettingsSectionId,
	locked: boolean,
): boolean {
	return !locked || !BILLING_LOCK_HIDDEN_SETTINGS_SECTIONS.has(id);
}

/**
 * The section the dialog opens for `requested`.
 *
 * A visible request is kept. Otherwise a locked user lands on Compute
 * (`billing-plan`) when it is visible (L4.3) and on General when it is not (the
 * OSS build). Unlocked, an invisible request falls back to General as before.
 */
export function lockedSettingsFallback({
	requested,
	visibleIds,
	locked,
}: {
	requested: SettingsSectionId;
	visibleIds: readonly SettingsSectionId[];
	locked: boolean;
}): SettingsSectionId {
	if (visibleIds.includes(requested)) return requested;
	if (locked && visibleIds.includes("billing-plan")) return "billing-plan";
	return DEFAULT_SETTINGS_SECTION;
}
