"use client";

import { useCallback, useState } from "react";
import { PageHeader } from "@/components/page-header";
import { CENTERED_PAGE_WIDTH_CLASS } from "@/components/page-width";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { clearAdminKey, getAdminKey, setAdminKey } from "@/lib/admin-client";
import { cn } from "@/lib/utils";
import { BillingExemptionSection } from "@/pages/dashboard/admin/billing-exemption-section";
import { DeletionSection } from "@/pages/dashboard/admin/deletion-section";
import { ExportSection } from "@/pages/dashboard/admin/export-section";

/**
 * S3 — the operator admin page (D-05 / D-13). One dashboard page, three
 * sections over the X-Admin-Key admin API: Tenant Export, Tenant Deletion
 * and Billing Exemption (06.3 WR-02). The key gate precedes every section:
 * no stored key, no admin content.
 */
export default function AdminPage() {
	// Lazy init reads sessionStorage once; the gate flips this state on entry
	// and a rejected key (401/403 from any section) clears it back to null.
	const [adminKey, setAdminKeyState] = useState<string | null>(() => getAdminKey());
	const [keyRejected, setKeyRejected] = useState(false);

	const handleNeedsKey = useCallback(() => {
		clearAdminKey();
		setAdminKeyState(null);
		setKeyRejected(true);
	}, []);

	if (!adminKey) {
		return (
			<AdminKeyGate
				rejected={keyRejected}
				onSubmit={(key) => {
					setAdminKey(key);
					setKeyRejected(false);
					setAdminKeyState(key);
				}}
			/>
		);
	}

	return (
		<div className={cn(CENTERED_PAGE_WIDTH_CLASS.page, "space-y-8 px-4 lg:px-6")}>
			<PageHeader
				title="Admin"
				description="Tenant export, tenant deletion and billing exemption for the operator."
			/>

			<section className="space-y-4">
				<div className="space-y-2">
					<h2 className="text-sm font-semibold">Tenant Export</h2>
					<Separator />
				</div>
				<ExportSection onNeedsKey={handleNeedsKey} />
			</section>

			<section className="space-y-4">
				<div className="space-y-2">
					<h2 className="text-sm font-semibold">Tenant Deletion</h2>
					<Separator />
				</div>
				<DeletionSection onNeedsKey={handleNeedsKey} />
			</section>

			<section className="space-y-4">
				<div className="space-y-2">
					<h2 className="text-sm font-semibold">Billing Exemption</h2>
					<Separator />
				</div>
				<BillingExemptionSection onNeedsKey={handleNeedsKey} />
			</section>
		</div>
	);
}

function AdminKeyGate({
	rejected,
	onSubmit,
}: {
	rejected: boolean;
	onSubmit: (key: string) => void;
}) {
	const [key, setKey] = useState("");
	return (
		<div className={cn(CENTERED_PAGE_WIDTH_CLASS.page, "flex justify-center px-4 py-12 lg:px-6")}>
			<div className="w-full max-w-sm rounded-lg border border-border bg-card p-4">
				<form
					className="space-y-4"
					onSubmit={(e) => {
						e.preventDefault();
						if (key.trim()) onSubmit(key.trim());
					}}
				>
					<div className="space-y-1">
						<h1 className="text-xl font-semibold tracking-tight">Admin</h1>
						<p className="text-sm text-muted-foreground">
							Enter the admin key to manage tenant data. The key is kept for this browser session
							only.
						</p>
					</div>
					<div className="space-y-1.5">
						<Label htmlFor="admin-key">Admin key</Label>
						<Input
							id="admin-key"
							type="password"
							value={key}
							onChange={(e) => setKey(e.target.value)}
							autoComplete="off"
							autoFocus
						/>
						{rejected ? (
							<p className="text-sm text-destructive-muted-foreground">
								That key was not accepted. Check it and enter it again.
							</p>
						) : null}
					</div>
					<Button type="submit" className="w-full" disabled={!key.trim()}>
						Continue
					</Button>
				</form>
			</div>
		</div>
	);
}
