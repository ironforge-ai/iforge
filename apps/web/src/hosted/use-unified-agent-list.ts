"use client";

import type { components } from "@clawdi/shared/api";
import { useCallback, useEffect, useMemo, useRef } from "react";
import { type AgentTile, selfManagedAgentTiles } from "@/components/dashboard/agents-card";
import { useLegacyEnvIds } from "@/hosted/agents/ownership-sensor";
import type { HostedInventoryStatus } from "@/hosted/hosted-agent-resolution";
import { legacyConnectedAgentTiles } from "@/hosted/legacy-agent-tiles";
import { useHostedAgentTiles } from "@/hosted/use-hosted-agent-tiles";
import { normalizeAgentId } from "@/lib/agent-ownership";

type Env = components["schemas"]["AgentResponse"];

export interface UnifiedAgentListSelection {
	tiles: AgentTile[];
	hostedTiles: AgentTile[];
	connectedTiles: AgentTile[];
	membershipResolved: boolean;
}

/**
 * Canonical membership selector for every hosted dashboard agent list.
 *
 * A Cloud deployment owns its configured environment even while that
 * environment is absent from the Cloud API response. Legacy environments are
 * bridged once, and every remaining environment is rendered as self-managed.
 * `showLegacyAgents` controls their tiles, never whether ownership must resolve.
 */
export function selectUnifiedAgentList({
	cloudEnvs,
	hostedTiles,
	claimedEnvIds,
	legacyEnvIds,
	hostedInventoryStatus,
	showLegacyAgents,
}: {
	cloudEnvs: Env[];
	hostedTiles: AgentTile[];
	claimedEnvIds: ReadonlySet<string>;
	legacyEnvIds: ReadonlySet<string> | null;
	hostedInventoryStatus: HostedInventoryStatus;
	showLegacyAgents: boolean;
}): UnifiedAgentListSelection {
	if (hostedInventoryStatus !== "resolved" || legacyEnvIds === null) {
		return {
			tiles: hostedTiles,
			hostedTiles,
			connectedTiles: [],
			membershipResolved: false,
		};
	}

	const legacyConnectedTiles = showLegacyAgents
		? legacyConnectedAgentTiles(cloudEnvs, legacyEnvIds, claimedEnvIds)
		: [];
	const dedupedSelfManaged = selfManagedAgentTiles(cloudEnvs).filter(
		(tile) => !isOwnedEnvId(tile.id, claimedEnvIds, legacyEnvIds),
	);
	const connectedTiles = [...legacyConnectedTiles, ...dedupedSelfManaged];
	return {
		tiles: [...hostedTiles, ...connectedTiles],
		hostedTiles,
		connectedTiles,
		membershipResolved: true,
	};
}

function isOwnedEnvId(
	id: string,
	claimedEnvIds: ReadonlySet<string>,
	legacyEnvIds: ReadonlySet<string>,
): boolean {
	const envId = normalizeAgentId(id);
	return Boolean(envId && (claimedEnvIds.has(envId) || legacyEnvIds.has(envId)));
}

/**
 * Membership cannot resolve and nothing is still loading: the list has
 * settled on "unavailable" (e.g. `/v2/deployments` answering 503
 * `compute_unavailable` while compute is unset). Consumers render a settled
 * state instead of a skeleton that never ends.
 */
export function unifiedAgentListUnavailable({
	membershipResolved,
	isLoading,
	error,
}: {
	membershipResolved: boolean;
	isLoading: boolean;
	error: unknown;
}): boolean {
	return !membershipResolved && !isLoading && error != null;
}

export function useUnifiedAgentList({
	cloudEnvs,
	showCloudDeployments = true,
	showLegacyAgents = false,
	eventStreamActive = false,
}: {
	cloudEnvs: Env[];
	showCloudDeployments?: boolean;
	showLegacyAgents?: boolean;
	eventStreamActive?: boolean;
}) {
	const hosted = useHostedAgentTiles({
		cloudEnvs,
		includeDeployments: showCloudDeployments,
		eventStreamActive,
	});
	const legacy = useLegacyEnvIds();
	const selection = useMemo(
		() =>
			selectUnifiedAgentList({
				cloudEnvs,
				hostedTiles: hosted.tiles,
				claimedEnvIds: hosted.claimedEnvIds,
				legacyEnvIds: legacy.envIds,
				hostedInventoryStatus: hosted.inventoryStatus,
				showLegacyAgents,
			}),
		[
			cloudEnvs,
			hosted.claimedEnvIds,
			hosted.inventoryStatus,
			hosted.tiles,
			legacy.envIds,
			showLegacyAgents,
		],
	);

	const isLoading = (showCloudDeployments && hosted.isLoading) || legacy.isLoading;
	const error = hosted.error ?? legacy.error;
	return {
		...selection,
		hasExistingDeployments: hosted.hasExistingDeployments,
		inventoryStatus: hosted.inventoryStatus,
		isFetching: hosted.isFetching,
		isLoading,
		error,
		unavailable: unifiedAgentListUnavailable({
			membershipResolved: selection.membershipResolved,
			isLoading,
			error,
		}),
		refetch: () =>
			Promise.all([...(showCloudDeployments ? [hosted.refetch()] : []), legacy.refetch()]),
	};
}

export function HostedUnifiedAgentListSensor({
	cloudEnvs,
	showCloudDeployments = true,
	showLegacyAgents = false,
	onChange,
}: {
	cloudEnvs: Env[];
	showCloudDeployments?: boolean;
	showLegacyAgents?: boolean;
	onChange: (
		tiles: AgentTile[] | null,
		membershipResolved: boolean,
		inventoryFetching: boolean,
		inventoryUnavailable: boolean,
		retry: (() => void) | null,
	) => void;
}) {
	const unified = useUnifiedAgentList({
		cloudEnvs,
		showCloudDeployments,
		showLegacyAgents,
	});
	// `refetch` is rebuilt every render; a stable retry keeps the effect quiet.
	const refetchRef = useRef(unified.refetch);
	refetchRef.current = unified.refetch;
	const retry = useCallback(() => {
		void refetchRef.current();
	}, []);

	useEffect(() => {
		onChange(
			unified.tiles,
			unified.membershipResolved,
			unified.isFetching,
			unified.unavailable,
			retry,
		);
	}, [
		onChange,
		retry,
		unified.isFetching,
		unified.membershipResolved,
		unified.tiles,
		unified.unavailable,
	]);
	useEffect(() => () => onChange(null, false, false, false, null), [onChange]);

	return null;
}
