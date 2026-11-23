"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useId, useRef, useState } from "react";
import { toast } from "sonner";
import {
	AlertDialog,
	AlertDialogCancel,
	AlertDialogContent,
	AlertDialogDescription,
	AlertDialogFooter,
	AlertDialogHeader,
	AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { useApi } from "@/lib/api";
import {
	companionRequests,
	runExportFirst,
	runUninstall,
	settleUninstall,
	UNINSTALL_COPY,
	UNINSTALL_TYPE_PROMPT,
	uninstallConfirmMatches,
	uninstallConsequences,
	uninstallInputLabel,
	uninstallTitle,
} from "./companion-detail.logic";
import { COMPANION_INSTALLATIONS_QUERY_KEY } from "./companions-overview.logic";

export type ExportOfferState = "idle" | "pending" | "started";

/**
 * S5 — uninstall one companion, with the data export offered inside the same
 * dialog (APP-07).
 *
 * This is `ConfirmAction`'s contract reproduced on the `AlertDialog`
 * primitives rather than `ConfirmAction` itself, for three reasons the
 * contract forces (recorded in the 06-27 SUMMARY):
 * - `ConfirmAction`'s secondary action CLOSES the dialog when it succeeds; the
 *   export offer must leave it open and swap a sentence.
 * - its secondary action renders as an outline button between Cancel and
 *   confirm; the offer must be a ghost button left of the footer, subordinate
 *   to Cancel.
 * - it owns its trigger, and S5 opens from a row-menu item that unmounts as
 *   the menu closes, so S5 is opened by the page instead.
 * The rest is kept exactly: the confirm holds the dialog open with a
 * `Spinner` until the uninstall is accepted, a double click cannot send it
 * twice, dismissal is refused while it is in flight, and a refusal renders
 * inline in the still-open dialog, never as a toast.
 *
 * The export is an offer, never a precondition: untaken, the confirm is gated
 * only by the typed name. Once taken, the confirm stays disabled for the rest
 * of this open, because uninstalling would destroy the data the export is
 * copying; the server refuses such an uninstall too (409
 * `companion_export_in_progress`, CR-05), rendered inline here.
 *
 * Each open mounts a fresh dialog (the page keys it per open), so the typed
 * name, the export state and any refusal never outlive one open.
 */
export function UninstallCompanionDialog({
	installationId,
	name,
	open,
	onOpenChange,
}: {
	installationId: string;
	name: string;
	open: boolean;
	onOpenChange: (open: boolean) => void;
}) {
	const api = useApi();
	const queryClient = useQueryClient();
	const [typed, setTyped] = useState("");
	const [exportState, setExportState] = useState<ExportOfferState>("idle");
	const [exportError, setExportError] = useState<string | null>(null);
	const [pending, setPending] = useState(false);
	const [rejection, setRejection] = useState<string | null>(null);
	// Synchronous locks: `disabled` only lands on the next render.
	const confirmLock = useRef(false);
	const exportLock = useRef(false);
	const requests = companionRequests(api, installationId);

	function handleOpenChange(next: boolean) {
		if (pending) return;
		onOpenChange(next);
	}

	async function refetch() {
		await queryClient.invalidateQueries({ queryKey: COMPANION_INSTALLATIONS_QUERY_KEY });
	}

	async function exportFirst() {
		if (exportLock.current) return;
		exportLock.current = true;
		setExportState("pending");
		setExportError(null);
		const outcome = await runExportFirst({
			requestExport: requests.startExport,
			onStarted: () => {
				toast.success(UNINSTALL_COPY.exportStartedToast);
			},
		});
		exportLock.current = false;
		if (outcome.ok) {
			setExportState("started");
		} else {
			setExportState("idle");
			setExportError(outcome.message);
		}
	}

	async function confirm() {
		// A taken export offer holds the uninstall for the rest of this open (CR-05).
		if (confirmLock.current || exportState !== "idle" || !uninstallConfirmMatches(typed, name)) {
			return;
		}
		confirmLock.current = true;
		setPending(true);
		setRejection(null);
		const outcome = await runUninstall({
			typed,
			name,
			requestUninstall: requests.uninstall,
			onStarted: async () => {
				toast.success(UNINSTALL_COPY.uninstallStartedToast);
				await refetch();
			},
		});
		confirmLock.current = false;
		setPending(false);
		// Already uninstalling (409) is not a second success: refetch and close, no toast.
		await settleUninstall(outcome, {
			close: () => onOpenChange(false),
			refetch,
			showRejection: setRejection,
		});
	}

	return (
		<AlertDialog open={open} onOpenChange={handleOpenChange}>
			<AlertDialogContent>
				<UninstallCompanionDialogBody
					name={name}
					typed={typed}
					onTypedChange={(next) => {
						setTyped(next);
						setRejection(null);
					}}
					exportState={exportState}
					exportError={exportError}
					onExport={() => {
						void exportFirst();
					}}
					pending={pending}
					rejection={rejection}
					onConfirm={() => {
						void confirm();
					}}
				/>
			</AlertDialogContent>
		</AlertDialog>
	);
}

export interface UninstallCompanionDialogBodyProps {
	name: string;
	typed: string;
	onTypedChange?: (next: string) => void;
	exportState?: ExportOfferState;
	exportError?: string | null;
	onExport?: () => void;
	pending?: boolean;
	/** The inline uninstall refusal, or null. */
	rejection?: string | null;
	onConfirm?: () => void;
}

/**
 * S5's header, description, typed confirmation and footer. Props only (it
 * needs just the `AlertDialog` root around it), so every S5 state renders in a
 * test without a portal or a network.
 */
export function UninstallCompanionDialogBody({
	name,
	typed,
	onTypedChange,
	exportState = "idle",
	exportError = null,
	onExport,
	pending = false,
	rejection = null,
	onConfirm,
}: UninstallCompanionDialogBodyProps) {
	const inputId = useId();
	const matches = uninstallConfirmMatches(typed, name);
	return (
		<>
			<AlertDialogHeader>
				<AlertDialogTitle>{uninstallTitle(name)}</AlertDialogTitle>
				<AlertDialogDescription
					render={<div className="space-y-2" data-slot="uninstall-description" />}
				>
					<p>{uninstallConsequences(name)}</p>
					{exportState === "started" ? (
						<p data-slot="uninstall-export-sentence" data-export="started">
							{UNINSTALL_COPY.exportPreparing}
						</p>
					) : (
						<p
							className="font-semibold text-foreground"
							data-slot="uninstall-export-sentence"
							data-export={exportState}
						>
							{UNINSTALL_COPY.exportFirst}
						</p>
					)}
					<p>
						<label htmlFor={inputId}>
							{UNINSTALL_TYPE_PROMPT.before}{" "}
							<span className="font-mono text-foreground">{name}</span>{" "}
							{UNINSTALL_TYPE_PROMPT.after}
						</label>
					</p>
				</AlertDialogDescription>
			</AlertDialogHeader>

			<Input
				id={inputId}
				value={typed}
				onChange={(event) => onTypedChange?.(event.target.value)}
				aria-label={uninstallInputLabel(name)}
				autoComplete="off"
				autoCapitalize="off"
				spellCheck={false}
				disabled={pending}
				data-slot="uninstall-confirm-input"
			/>

			{exportError || rejection ? (
				<div className="space-y-1" data-slot="uninstall-errors">
					{exportError ? (
						<p
							role="alert"
							className="text-sm text-destructive-muted-foreground"
							data-slot="uninstall-export-error"
						>
							{exportError}
						</p>
					) : null}
					{rejection ? (
						<p
							role="alert"
							className="text-sm text-destructive-muted-foreground"
							data-slot="uninstall-rejection"
						>
							{rejection}
						</p>
					) : null}
				</div>
			) : null}

			<AlertDialogFooter>
				{/*
				 * The offer: ghost, left of the footer, subordinate to Cancel. Untaken it
				 * gates nothing; taken, it holds the confirm below disabled (CR-05).
				 */}
				<Button
					type="button"
					variant="ghost"
					className="min-w-0 whitespace-normal sm:mr-auto"
					onClick={onExport}
					disabled={exportState !== "idle" || pending}
					aria-busy={exportState === "pending"}
					data-slot="uninstall-export"
				>
					{exportState === "pending" ? <Spinner /> : null}
					{UNINSTALL_COPY.exportAction}
				</Button>
				<AlertDialogCancel disabled={pending}>{UNINSTALL_COPY.cancel}</AlertDialogCancel>
				<Button
					type="button"
					variant="destructive"
					className="min-w-0 whitespace-normal"
					onClick={onConfirm}
					disabled={!matches || pending || exportState !== "idle"}
					aria-busy={pending}
					data-slot="uninstall-confirm"
				>
					{pending ? <Spinner /> : null}
					{UNINSTALL_COPY.confirmLabel}
				</Button>
			</AlertDialogFooter>
		</>
	);
}
