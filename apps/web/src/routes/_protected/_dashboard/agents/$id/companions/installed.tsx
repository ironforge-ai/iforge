import { createFileRoute, redirect } from "@tanstack/react-router";
import { CompanionsOverview } from "@/components/companions/companions-overview";
import { agentSectionLink } from "@/lib/agent-routes";
import { routeHeadTitle } from "@/lib/document-title";

// S1 — installed and available companions. A sibling of the Companion Tools
// index and the activity log, and gated the same way: companions are deployed
// per runtime and a runtime belongs to a hosted agent, so a connected-only
// build has nothing to install and redirects to the agent overview exactly as
// the index, `activity` and `$section` routes do.
const IS_HOSTED_BUILD = import.meta.env.VITE_IFORGE_HOSTED === "true";

export const Route = createFileRoute("/_protected/_dashboard/agents/$id/companions/installed")({
	beforeLoad: ({ params, search }) => {
		if (!IS_HOSTED_BUILD) {
			throw redirect({ ...agentSectionLink(params.id, "overview", search), replace: true });
		}
	},
	head: () => routeHeadTitle("Companions"),
	component: AgentCompanionsInstalledRoute,
});

function AgentCompanionsInstalledRoute() {
	const { id } = Route.useParams();
	return <CompanionsOverview agentId={id} />;
}
