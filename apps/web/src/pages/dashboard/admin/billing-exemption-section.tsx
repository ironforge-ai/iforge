"use client";

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";
import { ConfirmAction } from "@/components/ui/confirm-action";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { type AdminUserListItem, isAdminKeyError, setBillingExemption } from "@/lib/admin-client";
import { TenantPicker } from "@/pages/dashboard/admin/export-section";

/**
 * Operator billing exemption (06.3 WR-02). Exemption is an explicit operator
 * decision, never a side effect of which call created the account, so the
 * only way to set it is this toggle (or the same admin route it calls).
 */
export const EXEMPTION_SWITCH_LABEL = "Exempt from billing";
export const EXEMPTION_REASON_LABEL = "Reason";
export const EXEMPTION_PARTNER_COPY =
	"Partner tenants are always exempt from billing. There is nothing to change here.";
export const EXEMPTION_ON_COPY =
	"This tenant uses iforge without a subscription. The billing gate does not check their plan.";
export const EXEMPTION_OFF_COPY = "This tenant needs an active subscription to use iforge.";
export const EXEMPTION_GRANT_BODY =
	"This tenant will be able to use iforge without a subscription until the exemption is removed. The reason is recorded in the audit log.";
export const EXEMPTION_REVOKE_BODY =
	"This tenant will need an active subscription to keep using iforge. If they do not have one, they are locked out on their next request. Access from a past paid subscription is cleared too, and granting the exemption again does not restore it. The reason is recorded in the audit log.";

/** Mirrors AdminBillingExemptionUpdate's reason check (06.3 WR-05): non-empty,
 * no surrounding whitespace, printable as Python's str.isprintable() defines it
 * (no Unicode Other or Separator character except the ASCII space), and at most
 * 191 characters counted in code points, as Python's len() counts them. The
 * shared fixture backend/tests/fixtures/billing_exemption_reason_cases.json
 * pins this check and the backend schema together. */
export const EXEMPTION_REASON_MAX = 191;
const NON_PRINTABLE = /(?! )[\p{C}\p{Z}]/u;
export function isValidExemptionReason(reason: string): boolean {
	const codePoints = [...reason].length;
	return (
		codePoints > 0 &&
		codePoints <= EXEMPTION_REASON_MAX &&
		reason === reason.trim() &&
		!NON_PRINTABLE.test(reason)
	);
}

export function BillingExemptionSection({ onNeedsKey }: { onNeedsKey: () => void }) {
	const [tenant, setTenant] = useState<AdminUserListItem | null>(null);

	const update = useMutation({
		mutationFn: ({
			clerkId,
			exempt,
			reason,
		}: {
			clerkId: string;
			exempt: boolean;
			reason: string;
		}) => setBillingExemption({ target_clerk_id: clerkId, exempt, reason }),
		onSuccess: (result) => {
			setTenant((current) =>
				current && current.id === result.user_id
					? { ...current, billing_exempt: result.exempt }
					: current,
			);
			toast.success(result.exempt ? "Billing exemption granted" : "Billing exemption removed");
		},
		onError: (error) => {
			if (isAdminKeyError(error)) onNeedsKey();
		},
	});

	return (
		<div className="space-y-4">
			<TenantPicker selected={tenant} onSelect={setTenant} onNeedsKey={onNeedsKey} />
			{tenant ? (
				<BillingExemptionPanel
					tenant={tenant}
					onSetExempt={(exempt, reason) => {
						if (!tenant.clerk_id) return Promise.resolve();
						return update.mutateAsync({ clerkId: tenant.clerk_id, exempt, reason });
					}}
				/>
			) : null}
		</div>
	);
}

/** Presentational panel — props in, markup out (component-testable). */
export function BillingExemptionPanel({
	tenant,
	onSetExempt,
}: {
	tenant: AdminUserListItem;
	onSetExempt: (exempt: boolean, reason: string) => Promise<unknown>;
}) {
	const [reason, setReason] = useState("");
	const exempt = tenant.billing_exempt;

	// Partner tenants have no Clerk id, so the route cannot address them, and
	// they are exempt by construction.
	if (!tenant.clerk_id) {
		return (
			<div className="flex items-center gap-3">
				<Switch checked disabled aria-label={EXEMPTION_SWITCH_LABEL} />
				<p className="text-sm text-muted-foreground">{EXEMPTION_PARTNER_COPY}</p>
			</div>
		);
	}

	const next = !exempt;
	return (
		<div className="flex items-center gap-3">
			<ConfirmAction
				title={next ? "Exempt this tenant from billing" : "Remove this tenant's billing exemption"}
				description={
					<>
						<p>{next ? EXEMPTION_GRANT_BODY : EXEMPTION_REVOKE_BODY}</p>
						<div className="space-y-1.5">
							<Label htmlFor="billing-exemption-reason">{EXEMPTION_REASON_LABEL}</Label>
							<Input
								id="billing-exemption-reason"
								value={reason}
								maxLength={EXEMPTION_REASON_MAX}
								onChange={(e) => setReason(e.target.value)}
								autoComplete="off"
							/>
						</div>
					</>
				}
				confirmLabel={next ? "Grant exemption" : "Remove exemption"}
				destructive={!next}
				confirmDisabled={!isValidExemptionReason(reason)}
				onOpenChange={(open) => {
					if (!open) setReason("");
				}}
				onConfirm={() => onSetExempt(next, reason)}
			>
				<Switch checked={exempt} aria-label={EXEMPTION_SWITCH_LABEL} />
			</ConfirmAction>
			<p className="text-sm text-muted-foreground">
				{exempt ? EXEMPTION_ON_COPY : EXEMPTION_OFF_COPY}
			</p>
		</div>
	);
}
