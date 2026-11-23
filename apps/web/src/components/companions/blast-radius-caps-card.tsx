"use client";

import { useMemo, useState } from "react";
import { ApiErrorPanel } from "@/components/api-error-panel";
import { SectionLabel } from "@/components/section-label";
import { TimeTooltip } from "@/components/time-tooltip";
import { Button } from "@/components/ui/button";
import { ConfirmAction } from "@/components/ui/confirm-action";
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import {
	buildCapsPayload,
	byteUnitsFor,
	CAP_FIELD_DEFINITIONS,
	CAPS_COPY,
	CAPS_SAVE_ERROR_COPY,
	type CapDraft,
	type CapField,
	type CapVector,
	type CapVectorRequest,
	type ConsumptionItem,
	capDraftFrom,
	capFieldErrors,
	capsAreDirty,
	capsReadoutRows,
	formatWindowStart,
	RAISE_BUDGET_COPY,
	raisesSessionBudget,
	windowHasConsumption,
} from "./caps-form.logic";

/**
 * The blast-radius caps card: six ceilings, and what the current window has
 * spent against the three per-session ones.
 *
 * The card exposes NOTHING an agent argument could set. There is no per-call
 * max-records tool parameter, and no label, helper or field here names one —
 * the façade decides these before a call is authorized, and an agent asking
 * for a larger budget does not change the answer.
 */
export function BlastRadiusCapsCard({
	companionKey,
	vector,
	consumption,
	windowStartedAt,
	loading = false,
	error,
	onRetry,
	onSave,
}: {
	companionKey: string;
	vector: CapVector | undefined;
	consumption: ConsumptionItem | undefined;
	windowStartedAt: string | null | undefined;
	loading?: boolean;
	error?: unknown;
	onRetry?: () => void;
	/** Resolves when the server has accepted all six; rejects otherwise. */
	onSave: (payload: CapVectorRequest) => Promise<unknown>;
}) {
	if (error) {
		return (
			<section data-slot="blast-radius-caps" className="space-y-3">
				<SectionLabel>{CAPS_COPY.heading}</SectionLabel>
				<ApiErrorPanel error={error} onRetry={onRetry} />
			</section>
		);
	}

	if (loading || !vector) {
		return (
			<section data-slot="blast-radius-caps" className="space-y-3">
				<SectionLabel>{CAPS_COPY.heading}</SectionLabel>
				<div className="space-y-4 rounded-lg border border-border p-4">
					<Skeleton className="h-4 w-72 max-w-full" />
					<div className="grid gap-4 sm:grid-cols-3">
						{CAP_FIELD_DEFINITIONS.map((definition) => (
							<div key={definition.field} className="max-w-2xl space-y-1.5">
								<Skeleton className="h-4 w-20" />
								<Skeleton className="h-9 w-full" />
							</div>
						))}
					</div>
					<Button type="button" disabled>
						{CAPS_COPY.save}
					</Button>
				</div>
			</section>
		);
	}

	return (
		<CapsForm
			companionKey={companionKey}
			vector={vector}
			consumption={consumption}
			windowStartedAt={windowStartedAt}
			onSave={onSave}
		/>
	);
}

function CapsForm({
	companionKey,
	vector,
	consumption,
	windowStartedAt,
	onSave,
}: {
	companionKey: string;
	vector: CapVector;
	consumption: ConsumptionItem | undefined;
	windowStartedAt: string | null | undefined;
	onSave: (payload: CapVectorRequest) => Promise<unknown>;
}) {
	const byteUnits = useMemo(() => byteUnitsFor(vector), [vector]);
	const [draft, setDraft] = useState<CapDraft>(() => capDraftFrom(vector));
	const [saveError, setSaveError] = useState<string | null>(null);

	const dirty = capsAreDirty(draft, vector);
	const fieldErrors = capFieldErrors(draft);
	const payload = buildCapsPayload(draft, byteUnits);
	const canSave = dirty && payload !== null;
	const needsRaiseConfirmation =
		canSave && windowHasConsumption(consumption) && raisesSessionBudget(draft, vector, byteUnits);

	const sinceLabel = formatWindowStart(windowStartedAt);
	const readout = capsReadoutRows(consumption, vector, sinceLabel);

	function setField(field: CapField, value: string) {
		setDraft((previous) => ({ ...previous, [field]: value }));
		setSaveError(null);
	}

	async function save() {
		if (!payload) return;
		try {
			await onSave(payload);
			setSaveError(null);
		} catch (cause) {
			// Inline beneath the form, never a toast: a failed save leaves the
			// OLD limits in force and the user has to be told which limits
			// those are, at the control they just used.
			setSaveError(CAPS_SAVE_ERROR_COPY);
			throw cause;
		}
	}

	return (
		<section data-slot="blast-radius-caps" className="space-y-3">
			<SectionLabel>{CAPS_COPY.heading}</SectionLabel>
			<div className="space-y-4 rounded-lg border border-border p-4">
				<div className="flex flex-wrap items-center gap-2">
					<span className="font-mono text-xs text-muted-foreground">{companionKey}</span>
					{/* `is_default` is the server's ROW-EXISTENCE marker. A tenant
					    who saved the shipped numbers verbatim made a choice, and
					    this never re-derives the flag by comparing values. */}
					<StatusBadge status={vector.is_default ? "neutral" : "info"}>
						{vector.is_default ? CAPS_COPY.defaultsMarker : CAPS_COPY.savedMarker}
					</StatusBadge>
				</div>
				<p className="text-muted-foreground text-sm">{CAPS_COPY.helper}</p>
				<dl data-slot="caps-readout" className="space-y-1">
					{readout.map((line) => (
						<div key={line.unit} className="text-sm tabular-nums">
							{windowStartedAt ? (
								<TimeTooltip value={windowStartedAt}>
									<span>{line.sentence}</span>
								</TimeTooltip>
							) : (
								<span>{line.sentence}</span>
							)}
						</div>
					))}
				</dl>
				<CapScopeGrid
					scope="per_call"
					label={CAPS_COPY.perCallLabel}
					helper={CAPS_COPY.perCallHelper}
					draft={draft}
					errors={fieldErrors}
					byteUnits={byteUnits}
					onChange={setField}
				/>
				<CapScopeGrid
					scope="per_session"
					label={CAPS_COPY.perSessionLabel}
					helper={CAPS_COPY.perSessionHelper}
					draft={draft}
					errors={fieldErrors}
					byteUnits={byteUnits}
					onChange={setField}
				/>
				{needsRaiseConfirmation ? (
					<ConfirmAction
						title={RAISE_BUDGET_COPY.title}
						description={RAISE_BUDGET_COPY.description}
						confirmLabel={RAISE_BUDGET_COPY.confirmLabel}
						onConfirm={save}
					>
						<Button type="button">{CAPS_COPY.save}</Button>
					</ConfirmAction>
				) : (
					<Button
						type="button"
						disabled={!canSave}
						onClick={() => {
							void save().catch(() => {});
						}}
					>
						{CAPS_COPY.save}
					</Button>
				)}
				{saveError ? (
					<p data-slot="caps-save-error" className="text-destructive-muted-foreground text-sm">
						{saveError}
					</p>
				) : null}
			</div>
		</section>
	);
}

function CapScopeGrid({
	scope,
	label,
	helper,
	draft,
	errors,
	byteUnits,
	onChange,
}: {
	scope: "per_call" | "per_session";
	label: string;
	helper: string;
	draft: CapDraft;
	errors: Partial<Record<CapField, string>>;
	byteUnits: ReturnType<typeof byteUnitsFor>;
	onChange: (field: CapField, value: string) => void;
}) {
	return (
		<fieldset data-slot={`caps-${scope}`} className="space-y-2">
			<legend className="text-sm font-semibold">{label}</legend>
			<p className="text-muted-foreground text-xs">{helper}</p>
			<div className="grid gap-4 sm:grid-cols-3">
				{CAP_FIELD_DEFINITIONS.filter((definition) => definition.scope === scope).map(
					(definition) => {
						const id = `cap-${definition.field}`;
						const isBytes = definition.unit === "bytes";
						const suffix = isBytes
							? byteUnits[definition.field as "per_call_bytes" | "per_session_bytes"]
							: null;
						const error = errors[definition.field];
						return (
							<div key={definition.field} className="max-w-2xl space-y-1.5">
								<Label htmlFor={id}>{definition.label}</Label>
								<InputGroup>
									<InputGroupInput
										id={id}
										name={definition.field}
										type="number"
										inputMode="numeric"
										min={0}
										step={1}
										value={draft[definition.field]}
										aria-invalid={error ? true : undefined}
										onChange={(event) => onChange(definition.field, event.target.value)}
									/>
									{suffix ? <InputGroupAddon align="inline-end">{suffix}</InputGroupAddon> : null}
								</InputGroup>
								{error ? (
									<p className="text-destructive-muted-foreground text-xs">{error}</p>
								) : null}
							</div>
						);
					},
				)}
			</div>
		</fieldset>
	);
}
