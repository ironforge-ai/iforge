"use client";

import { type ReactElement, type ReactNode, useRef, useState } from "react";
import {
	AlertDialog,
	AlertDialogAction,
	AlertDialogCancel,
	AlertDialogContent,
	AlertDialogDescription,
	AlertDialogFooter,
	AlertDialogHeader,
	AlertDialogTitle,
	AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { buttonVariants } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { normalizeApiError } from "@/lib/api-errors";
import { cn } from "@/lib/utils";

export function ConfirmAction({
	children,
	title,
	description,
	confirmLabel = "Confirm",
	cancelLabel = "Cancel",
	secondaryAction,
	destructive = false,
	confirmDisabled = false,
	onConfirm,
	open: controlledOpen,
	onOpenChange,
}: {
	children?: ReactElement;
	open?: boolean;
	title: string;
	description: ReactNode;
	confirmLabel?: string;
	cancelLabel?: string;
	secondaryAction?: {
		label: string;
		onAction: () => unknown;
	};
	destructive?: boolean;
	/** Gate the confirm button beyond the in-flight lock — e.g. a
	 * typed-confirmation dialog keeps it disabled until the input matches. */
	confirmDisabled?: boolean;
	/** Observe open/close so callers can reset dialog-scoped state (typed
	 * confirmation values and the like). Fires on user toggles AND on the
	 * internal close-after-success path. */
	onOpenChange?: (open: boolean) => void;
	/**
	 * May be sync or async (any return). When it returns a promise the dialog
	 * stays open with a spinner until it settles, then closes. Long-running
	 * background work should resolve this promise when the action is accepted.
	 */
	onConfirm: () => unknown;
}) {
	const [internalOpen, setInternalOpen] = useState(false);
	const open = controlledOpen ?? internalOpen;
	const setOpen = (nextOpen: boolean) => {
		if (controlledOpen === undefined) setInternalOpen(nextOpen);
		onOpenChange?.(nextOpen);
	};
	const [pendingAction, setPendingAction] = useState<"confirm" | "secondary" | null>(null);
	// Rejected actions keep the dialog open; surface why, inline at the action
	// (the S3 contract sends mutation errors inline, never to a toast).
	const [errorMessage, setErrorMessage] = useState<string | null>(null);
	// Synchronous lock: `disabled` only takes effect on the next render, leaving
	// a sub-frame window where a fast double-click (or Enter repeat) could fire
	// an action twice before React repaints.
	const locked = useRef(false);
	const closingAfterSuccess = useRef(false);

	async function runAction(action: "confirm" | "secondary", onAction: () => unknown) {
		if (locked.current) return;
		locked.current = true;
		setPendingAction(action);
		setErrorMessage(null);
		try {
			await onAction();
			closingAfterSuccess.current = true;
			setOpen(false);
		} catch (error) {
			setErrorMessage(normalizeApiError(error));
			throw error;
		} finally {
			if (!closingAfterSuccess.current) {
				setPendingAction(null);
				locked.current = false;
			}
		}
	}
	const pending = pendingAction !== null;

	return (
		<AlertDialog
			open={open}
			onOpenChange={(next) => {
				if (pending) return;
				setOpen(next);
			}}
			onOpenChangeComplete={(nextOpen) => {
				if (!nextOpen) setErrorMessage(null);
				if (!nextOpen && closingAfterSuccess.current) {
					closingAfterSuccess.current = false;
					setPendingAction(null);
					locked.current = false;
				}
			}}
		>
			{children ? <AlertDialogTrigger render={children} /> : null}
			<AlertDialogContent>
				<AlertDialogHeader>
					<AlertDialogTitle>{title}</AlertDialogTitle>
					<AlertDialogDescription render={<div className="space-y-2" />}>
						{description}
					</AlertDialogDescription>
					{errorMessage ? (
						<p className="text-sm text-destructive-muted-foreground">{errorMessage}</p>
					) : null}
				</AlertDialogHeader>
				<AlertDialogFooter>
					<AlertDialogCancel disabled={pending}>{cancelLabel}</AlertDialogCancel>
					{secondaryAction ? (
						<AlertDialogAction
							variant="outline"
							onClick={(event) => {
								event.preventDefault();
								void runAction("secondary", secondaryAction.onAction).catch(() => {});
							}}
							disabled={pending}
							className="min-w-0 whitespace-normal"
						>
							{pendingAction === "secondary" ? <Spinner /> : null}
							{secondaryAction.label}
						</AlertDialogAction>
					) : null}
					<AlertDialogAction
						// Keep the dialog open until the caller settles; accepted background work
						// closes here while status polling reflects its eventual outcome.
						onClick={(event) => {
							event.preventDefault();
							// runAction rejects when onConfirm does; the dialog already stays open
							// on reject and callers own their onError, so just swallow the
							// unhandled-rejection console noise without changing behavior.
							void runAction("confirm", onConfirm).catch(() => {});
						}}
						disabled={pending || confirmDisabled}
						className={cn(
							"min-w-0 whitespace-normal",
							destructive && buttonVariants({ variant: "destructive" }),
						)}
					>
						{pendingAction === "confirm" ? <Spinner /> : null}
						{confirmLabel}
					</AlertDialogAction>
				</AlertDialogFooter>
			</AlertDialogContent>
		</AlertDialog>
	);
}
