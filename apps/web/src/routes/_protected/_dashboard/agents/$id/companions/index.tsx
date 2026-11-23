import { createFileRoute, redirect } from "@tanstack/react-router";
import { CompanionToolsSurface } from "@/components/companions/companion-tools-surface";
import { agentSectionLink } from "@/lib/agent-routes";
import { routeHeadTitle } from "@/lib/document-title";

// Explicit route for the agent Companions section. Without it the bare
// `/agents/<id>/companions` URL falls through to the sibling `$section`
// route, which renders the generic agent-detail shell instead of this
// surface. Companions are deployed per runtime and a runtime belongs to a
// hosted agent, so a connected-only build redirects to the agent overview
// exactly as `$section` does for every other hosted-only section.
const IS_HOSTED_BUILD = import.meta.env.VITE_IFORGE_HOSTED === "true";

export const Route = createFileRoute("/_protected/_dashboard/agents/$id/companions/")({
	beforeLoad: ({ params, search }) => {
		if (!IS_HOSTED_BUILD) {
			throw redirect({ ...agentSectionLink(params.id, "overview", search), replace: true });
		}
	},
	head: () => routeHeadTitle("Companion Tools"),
	component: AgentCompanionsRoute,
});

function AgentCompanionsRoute() {
	const { id } = Route.useParams();
	return <CompanionToolsSurface environmentId={id} />;
}
