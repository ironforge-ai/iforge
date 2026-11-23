import {
	DEFAULT_HOSTED_DEPLOY_AI_ACCESS_MODE,
	DEFAULT_HOSTED_DEPLOY_PRIMARY_MODEL,
	DEFAULT_HOSTED_DEPLOY_RUNTIME,
	hostedDeployAgentNameAfterRuntimeChange,
} from "@clawdi/shared/api";
import type { HostedRuntime } from "@/hosted/runtimes";
import { MANAGED_AI_CHOICE } from "@/hosted/v2/ai-providers/model-binding";

export type DeployWizardAiAccessMode = "unmanaged" | "configured";

// Product pre-selection for new deployments.
export const DEFAULT_DEPLOY_RUNTIME: HostedRuntime = DEFAULT_HOSTED_DEPLOY_RUNTIME;
export const DEFAULT_DEPLOY_AI_ACCESS_MODE: DeployWizardAiAccessMode =
	DEFAULT_HOSTED_DEPLOY_AI_ACCESS_MODE;
export const DEFAULT_DEPLOY_PRIMARY_PROVIDER_CHOICE = MANAGED_AI_CHOICE;
// The managed catalog supplies the real default model after it loads.
export const DEFAULT_DEPLOY_PRIMARY_MODEL = DEFAULT_HOSTED_DEPLOY_PRIMARY_MODEL;

/**
 * Whether the first Basic agent is offered on the free included slot.
 *
 * Withdrawn for now: Basic stays selectable as an ordinary paid tier at its
 * listed price, and every deploy goes through checkout. Flipping this back to
 * `true` restores the included-slot offer — the resolver, the occupancy check
 * and their tests are all still in place.
 *
 * Turning it off also removes the wizard's only reason to read the deployment
 * inventory, which is why the Compute step no longer waits on that query. The
 * "Checking your free Basic slot…" spinner and the "Free Basic agent
 * availability is unknown" alert both exist purely to explain that wait, so
 * they are gated on this too; without the gate a billing-locked user whose
 * inventory read 403s would be blocked from choosing Performance by a check
 * that no longer decides anything.
 */
export const FREE_TIER_OFFERED = false;

export function deployAgentNameAfterRuntimeChange({
	currentName,
	hasBeenEdited,
	runtime,
}: {
	currentName: string;
	hasBeenEdited: boolean;
	runtime: HostedRuntime;
}): string {
	return hostedDeployAgentNameAfterRuntimeChange({ currentName, hasBeenEdited, runtime });
}
