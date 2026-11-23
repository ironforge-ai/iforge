import { createFileRoute, redirect } from "@tanstack/react-router";
import { CompanionDetail } from "@/components/companions/companion-detail";
import { agentSectionLink } from "@/lib/agent-routes";
import { routeHeadTitle } from "@/lib/document-title";

// S3 — one companion on one agent: its install job and log, access, updates
// and data. A child of the same section as S1 (`installed`), the Companion
// Tools index and the activity log, and gated the same way: companions are
// deployed per runtime and a runtime belongs to a hosted agent, so a
// connected-only build has nothing to show and redirects to the agent overview.
//
// The static `installed` and `activity` routes outrank this dynamic segment,
// so neither can be read as a companion key.
const IS_HOSTED_BUILD = import.meta.env.VITE_IFORGE_HOSTED === "true";

export const Route = createFileRoute("/_protected/_dashboard/agents/$id/companions/$companionKey")({
	beforeLoad: ({ params, search }) => {
		if (!IS_HOSTED_BUILD) {
			throw redirect({ ...agentSectionLink(params.id, "overview", search), replace: true });
		}
	},
	// The section's title until the catalog answers; the page then titles the
	// document with the companion's catalog name (useSetBreadcrumbTitle).
	head: () => routeHeadTitle("Companions"),
	component: AgentCompanionDetailRoute,
});

function AgentCompanionDetailRoute() {
	const { id, companionKey } = Route.useParams();
	return <CompanionDetail agentId={id} companionKey={companionKey} />;
}
