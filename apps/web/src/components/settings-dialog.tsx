"use client";

import type { ShouldBlockFn } from "@tanstack/react-router";
import {
	Archive,
	BarChart3,
	CreditCard,
	Key,
	type LucideIcon,
	SlidersHorizontal,
	WalletCards,
	XIcon,
} from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { type ApiErrorNormalizer, ApiErrorPanel } from "@/components/api-error-panel";
import type { AgentTile } from "@/components/dashboard/agents-card";
import { IconChip } from "@/components/icon-chip";
import { RouteLoadingSkeleton } from "@/components/route-loading-skeleton";
import { ApiKeysPanel } from "@/components/settings/api-keys-panel";
import { GeneralPanel } from "@/components/settings/general-panel";
import { type SettingsEditState, SettingsEditStateContext } from "@/components/settings-edit-state";
import { SettingsPanelErrorBoundary } from "@/components/settings-panel-error-boundary";
import { Button } from "@/components/ui/button";
import {
	Dialog,
	DialogClose,
	DialogContent,
	DialogDescription,
	DialogHeader,
	DialogTitle,
} from "@/components/ui/dialog";
import { UnsavedNavigationGuard } from "@/components/unsaved-navigation-guard";
import { useBillingLocked } from "@/lib/billing-lock"; // iforge (06.3 L4)
import {
	isSettingsSectionVisibleWhileLocked,
	lockedSettingsFallback,
} from "@/lib/billing-lock-settings"; // iforge (06.3 L4)
import { useProductAccess } from "@/lib/product-access";
import {
	DEFAULT_SETTINGS_SECTION,
	SETTINGS_SECTION_IDS,
	type SettingsSectionId,
	settingsDraftOwnerChanges,
	shouldCanonicalizeCloudSettings,
} from "@/lib/settings-routes";
import { cn } from "@/lib/utils";

const IS_HOSTED_BUILD = import.meta.env.VITE_IFORGE_HOSTED === "true";

const HOSTED_ACCESS_ERROR_NORMALIZER: ApiErrorNormalizer = {
	isAuthError: () => false,
	normalizeError: () => "Check your connection, then retry the billing access check.",
};

// iforge (06.1 UI-REVIEW): billing settings that wait on a failed hosted inventory.
const CLOUD_INVENTORY_ERROR_NORMALIZER: ApiErrorNormalizer = {
	isAuthError: () => false,
	normalizeError: () => "Billing settings need your agent list. Check your connection, then retry.",
};

/**
 * iforge (06.1 UI-REVIEW): true when a billing section is pending only on the
 * hosted inventory and that inventory has settled as unavailable (e.g.
 * `/v2/deployments` answering 503), so the panel settles on an error with Retry
 * instead of a skeleton that never resolves.
 */
export function billingInventoryUnavailable({
	billingAccessPending,
	mounted,
	accessLoading,
	inventoryUnavailable,
}: {
	billingAccessPending: boolean;
	mounted: boolean;
	accessLoading: boolean;
	inventoryUnavailable: boolean;
}): boolean {
	return billingAccessPending && mounted && !accessLoading && inventoryUnavailable;
}

const WalletPage = IS_HOSTED_BUILD
	? lazy(() =>
			import("@/hosted/billing/wallet/wallet-page").then((m) => ({ default: m.WalletPage })),
		)
	: null;

const SubscriptionPage = IS_HOSTED_BUILD
	? lazy(() =>
			import("@/hosted/billing/subscription/subscription-page").then((m) => ({
				default: m.SubscriptionPage,
			})),
		)
	: null;

const UsagePage = IS_HOSTED_BUILD
	? lazy(() => import("@/hosted/billing/usage/usage-page").then((m) => ({ default: m.UsagePage })))
	: null;

const DataPanel = IS_HOSTED_BUILD
	? lazy(() => import("@/components/settings/data-panel").then((m) => ({ default: m.DataPanel })))
	: null;

type SettingsNavItem = {
	id: SettingsSectionId;
	label: string;
	description: string;
	icon: LucideIcon;
	/** Hosted build only; the OSS build never lists it. */
	cloudOnly?: boolean;
	/** Additionally needs billing access (Cloud agents), as the billing panels do. */
	billing?: boolean;
};

const SETTINGS_NAV: SettingsNavItem[] = [
	{
		id: "general",
		label: "General",
		description: "Account and appearance",
		icon: SlidersHorizontal,
	},
	{
		id: "api-keys",
		label: "API Keys",
		description: "CLI and server tokens",
		icon: Key,
	},
	{
		id: "billing-wallet",
		label: "Wallet",
		description: "Balance, top-ups, and payment methods",
		icon: WalletCards,
		cloudOnly: true,
		billing: true,
	},
	{
		id: "billing-plan",
		label: "Compute",
		description: "Subscriptions and plans",
		icon: CreditCard,
		cloudOnly: true,
		billing: true,
	},
	{
		id: "billing-usage",
		label: "AI Usage",
		description: "LLM spend in USD, paid from Wallet",
		icon: BarChart3,
		cloudOnly: true,
		billing: true,
	},
	// Spread on the build flag so the OSS bundle carries no trace of the section.
	...(IS_HOSTED_BUILD
		? [
				{
					// Every hosted tenant may take their data out, whatever their
					// billing access; the companion surfaces that link here are
					// hosted-only.
					id: "data",
					label: "Data",
					description: "Exports of your account data",
					icon: Archive,
					cloudOnly: true,
				} satisfies SettingsNavItem,
			]
		: []),
];

export function SettingsDialog({
	open,
	section,
	agentTiles,
	hasExistingCloudAgents = false,
	cloudInventoryResolved = true,
	cloudInventoryUnavailable = false, // iforge (06.1 UI-REVIEW)
	onRetryCloudInventory = null, // iforge (06.1 UI-REVIEW)
	onSectionChange,
	onOpenChange,
}: {
	open: boolean;
	section: SettingsSectionId;
	agentTiles: readonly AgentTile[];
	hasExistingCloudAgents?: boolean;
	cloudInventoryResolved?: boolean;
	/** iforge (06.1 UI-REVIEW): the hosted inventory settled without resolving. */
	cloudInventoryUnavailable?: boolean;
	onRetryCloudInventory?: (() => void) | null;
	onSectionChange: (section: SettingsSectionId) => void;
	onOpenChange: (open: boolean) => void;
}) {
	const activeButtonRef = useRef<HTMLButtonElement | null>(null);
	const hostedAccess = useProductAccess();
	// iforge (06.3 L4): while billing-locked Settings lists only the sections a
	// locked user can use, and a hidden deep link opens Compute.
	const billingLocked = useBillingLocked();
	const [mounted, setMounted] = useState(false);
	const [editStates, setEditStates] = useState<Map<symbol, SettingsEditState>>(() => new Map());
	const registerEditState = useCallback((token: symbol, state: SettingsEditState | null) => {
		setEditStates((current) => {
			const next = new Map(current);
			if (state && (state.dirty || state.busy)) next.set(token, state);
			else next.delete(token);
			return next;
		});
	}, []);
	const hasUnsavedChanges = [...editStates.values()].some((state) => state.dirty);
	const hasPendingSave = [...editStates.values()].some((state) => state.busy);
	const shouldBlockNavigation: ShouldBlockFn = useCallback(
		({ current, next }) => settingsDraftOwnerChanges(current, next),
		[],
	);
	useEffect(() => {
		setMounted(true);
	}, []);
	const requestedBillingSection = section.startsWith("billing-");
	const showBilling =
		IS_HOSTED_BUILD &&
		(hostedAccess.canCreateCloudAgents ||
			hasExistingCloudAgents ||
			(requestedBillingSection && (!mounted || hostedAccess.isLoading || hostedAccess.isError)));
	const items = SETTINGS_NAV.filter(
		(item) =>
			(!item.cloudOnly || IS_HOSTED_BUILD) &&
			(!item.billing || showBilling) &&
			isSettingsSectionVisibleWhileLocked(item.id, billingLocked), // iforge (06.3 L4)
	);
	// iforge (06.3 L4): locked, a hidden request opens Compute; unlocked, General as before.
	const activeSection = lockedSettingsFallback({
		requested: section,
		visibleIds: items.map((item) => item.id),
		locked: billingLocked,
	});
	const billingAccessPending =
		requestedBillingSection &&
		IS_HOSTED_BUILD &&
		(!mounted ||
			hostedAccess.isLoading ||
			(!cloudInventoryResolved && !hostedAccess.canCreateCloudAgents)); // iforge (06.2 H1): inventory only decides billing for users v2 access does not cover
	const billingInventoryFailed = billingInventoryUnavailable({
		billingAccessPending,
		mounted,
		accessLoading: hostedAccess.isLoading,
		inventoryUnavailable: cloudInventoryUnavailable,
	}); // iforge (06.1 UI-REVIEW)
	const billingAccessError =
		requestedBillingSection &&
		IS_HOSTED_BUILD &&
		mounted &&
		hostedAccess.isError &&
		!hostedAccess.canCreateCloudAgents &&
		!hasExistingCloudAgents;

	useEffect(() => {
		if (!open) return;
		const frame = window.requestAnimationFrame(() => {
			const activeButton = activeButtonRef.current;
			if (!activeButton) return;
			const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
			activeButton.scrollIntoView({
				behavior: reduceMotion ? "auto" : "smooth",
				block: "nearest",
				inline: "nearest",
			});
		});
		return () => window.cancelAnimationFrame(frame);
	}, [open, activeSection]);

	useEffect(() => {
		if (
			open &&
			mounted &&
			shouldCanonicalizeCloudSettings({
				section,
				accessLoading: hostedAccess.isLoading,
				accessError: hostedAccess.isError,
				canCreateCloudAgents: hostedAccess.canCreateCloudAgents,
				inventoryResolved: cloudInventoryResolved,
				hasExistingCloudAgents,
			})
		) {
			onSectionChange(DEFAULT_SETTINGS_SECTION);
		}
	}, [
		cloudInventoryResolved,
		hasExistingCloudAgents,
		hostedAccess.canCreateCloudAgents,
		hostedAccess.isError,
		hostedAccess.isLoading,
		mounted,
		onSectionChange,
		open,
		section,
	]);

	function requestClose(nextOpen: boolean) {
		onOpenChange(nextOpen);
	}

	function requestSectionChange(nextSection: SettingsSectionId) {
		if (nextSection === activeSection) return;
		onSectionChange(nextSection);
	}

	return (
		<SettingsEditStateContext.Provider value={registerEditState}>
			<Dialog open={open} onOpenChange={requestClose}>
				<DialogContent
					data-testid="settings-dialog"
					initialFocus={activeButtonRef}
					showCloseButton={false}
					className="h-[min(820px,calc(100dvh-2rem))] w-[calc(100vw-2rem)] max-w-6xl gap-0 overflow-hidden p-0 sm:max-w-6xl"
				>
					<div className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)]">
						<DialogHeader className="flex h-14 shrink-0 flex-row items-center justify-between gap-3 border-b px-4 text-left md:px-5">
							<DialogTitle className="truncate text-sm font-semibold">Settings</DialogTitle>
							<DialogDescription className="sr-only">
								Account, billing, and application settings.
							</DialogDescription>
							{!hasPendingSave ? <SettingsDialogCloseButton /> : null}
						</DialogHeader>

						<div className="grid min-h-0 grid-rows-[auto_minmax(0,1fr)] md:grid-cols-[15rem_minmax(0,1fr)] md:grid-rows-1">
							<aside className="flex min-w-0 flex-col border-b bg-muted/30 md:border-r md:border-b-0">
								<div className="relative min-w-0 md:min-h-0 md:flex-1">
									<nav
										aria-label="Settings sections"
										className="flex gap-1 overflow-x-auto px-3 py-3 [scrollbar-width:thin] md:min-h-0 md:flex-1 md:flex-col md:overflow-y-auto"
									>
										{items.map((item) => {
											const Icon = item.icon;
											const active = activeSection === item.id;
											return (
												<Button
													key={item.id}
													ref={active ? activeButtonRef : undefined}
													type="button"
													variant="ghost"
													aria-current={active ? "page" : undefined}
													data-active={active}
													onClick={() => requestSectionChange(item.id)}
													className={cn(
														"h-auto min-w-28 shrink-0 justify-start gap-2 rounded-md px-2.5 py-2 text-left text-sm text-muted-foreground hover:bg-background/70 hover:text-foreground md:min-w-0 md:gap-3 md:px-3",
														"data-[active=true]:bg-background data-[active=true]:text-foreground data-[active=true]:shadow-xs",
													)}
												>
													<IconChip
														size="sm"
														tint={
															active
																? "bg-primary text-primary-foreground"
																: "bg-background text-foreground"
														}
													>
														<Icon />
													</IconChip>
													<span className="grid min-w-0 flex-1 leading-tight">
														<span className="truncate font-medium">{item.label}</span>
														<span className="hidden truncate text-xs text-muted-foreground md:block">
															{item.description}
														</span>
													</span>
												</Button>
											);
										})}
									</nav>
									<div className="pointer-events-none absolute inset-y-0 right-0 w-8 bg-linear-to-l from-muted/30 to-transparent md:hidden" />
								</div>
							</aside>

							<section className="min-h-0 min-w-0 overflow-y-auto py-6 md:py-8">
								<div className="mx-auto w-full max-w-4xl">
									{billingInventoryFailed ? (
										// iforge (06.1 UI-REVIEW): settle, as the agent rail does.
										<div className="px-5 sm:px-6 lg:px-8">
											<ApiErrorPanel
												error={null}
												normalizer={CLOUD_INVENTORY_ERROR_NORMALIZER}
												onRetry={onRetryCloudInventory ?? undefined}
												title="iforge Cloud inventory unavailable"
											/>
										</div>
									) : billingAccessPending ? (
										<RouteLoadingSkeleton />
									) : billingAccessError ? (
										<div className="px-5 sm:px-6 lg:px-8">
											<ApiErrorPanel
												error={hostedAccess.error}
												normalizer={HOSTED_ACCESS_ERROR_NORMALIZER}
												onRetry={() => void hostedAccess.refetch()}
												title="Couldn’t verify billing access"
											/>
										</div>
									) : (
										<SettingsPanelErrorBoundary key={activeSection}>
											<SettingsPanel section={activeSection} agentTiles={agentTiles} />
										</SettingsPanelErrorBoundary>
									)}
								</div>
							</section>
						</div>
					</div>
				</DialogContent>
			</Dialog>

			<UnsavedNavigationGuard
				dirty={hasUnsavedChanges}
				busy={hasPendingSave}
				shouldBlockFn={shouldBlockNavigation}
				description="Your auto-reload settings will return to the last values saved on the server."
			/>
		</SettingsEditStateContext.Provider>
	);
}

function SettingsDialogCloseButton({ className }: { className?: string }) {
	return (
		<DialogClose
			className={className}
			render={<Button type="button" variant="ghost" size="icon-sm" />}
		>
			<XIcon />
			<span className="sr-only">Close</span>
		</DialogClose>
	);
}

function SettingsPanel({
	section,
	agentTiles,
}: {
	section: SettingsSectionId;
	agentTiles: readonly AgentTile[];
}) {
	if (!SETTINGS_SECTION_IDS.includes(section)) return <GeneralPanel />;

	switch (section) {
		case "api-keys":
			return <ApiKeysPanel />;
		case "billing-wallet":
			return WalletPage ? (
				<Suspense fallback={<RouteLoadingSkeleton />}>
					<WalletPage />
				</Suspense>
			) : (
				<GeneralPanel />
			);
		case "billing-plan":
			return SubscriptionPage ? (
				<Suspense fallback={<RouteLoadingSkeleton />}>
					<SubscriptionPage agentTiles={agentTiles} />
				</Suspense>
			) : (
				<GeneralPanel />
			);
		case "billing-usage":
			return UsagePage ? (
				<Suspense fallback={<RouteLoadingSkeleton />}>
					<UsagePage agentTiles={agentTiles} />
				</Suspense>
			) : (
				<GeneralPanel />
			);
		case "data":
			return DataPanel ? (
				<Suspense fallback={<RouteLoadingSkeleton />}>
					<DataPanel />
				</Suspense>
			) : (
				<GeneralPanel />
			);
		default:
			return <GeneralPanel />;
	}
}
