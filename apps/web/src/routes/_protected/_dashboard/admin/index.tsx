import { createFileRoute } from "@tanstack/react-router";
import { routeHeadTitle } from "@/lib/document-title";
import AdminPage from "@/pages/dashboard/admin/page";

export const Route = createFileRoute("/_protected/_dashboard/admin/")({
	head: () => routeHeadTitle("Admin"),
	component: AdminPage,
});
