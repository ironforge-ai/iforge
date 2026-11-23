import { createFileRoute, redirect } from "@tanstack/react-router";
import { ToolActivitySurface } from "@/components/companions/tool-activity-table";
import { agentSectionLink } from "@/lib/agent-routes";
import { routeHeadTitle } from "@/lib/document-title";

// Sibling of the Companions index route, and gated the same way: companions are
// deployed per runtime and a runtime belongs to a hosted agent, so a
// connected-only build has no activity log to show and redirects to the agent
// overview exactly as the index and `$section` routes do.
const IS_HOSTED_BUILD = import.meta.env.VITE_IFORGE_HOSTED === "true";

export const Route = createFileRoute("/_protected/_dashboard/agents/$id/companions/activity")({
	beforeLoad: ({ params, search }) => {
		if (!IS_HOSTED_BUILD) {
			throw redirect({ ...agentSectionLink(params.id, "overview", search), replace: true });
		}
	},
	head: () => routeHeadTitle("Tool Activity"),
	component: AgentCompanionActivityRoute,
});

function AgentCompanionActivityRoute() {
	const { id } = Route.useParams();
	return <ToolActivitySurface environmentId={id} />;
}
