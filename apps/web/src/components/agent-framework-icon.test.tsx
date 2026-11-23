import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { AgentFrameworkIcon } from "@/components/agent-framework-icon";
import { FRAMEWORK_BRAND_ICON_IDS } from "@/components/entity-brand-icon-ids";
import { frameworkBrandIcon } from "@/components/entity-brand-icons";

// The framework id set spans two sources: six vendor marks from LobeHub and one
// first-party mark authored in this repo. Provenance is therefore asserted per
// partition — a single global `data-icon-source` assertion would let the
// first-party mark pass by inheriting the vendor's value, which would be a lie.
const FRAMEWORKS = {
	openclaw: { iconSource: "lobehub", label: "OpenClaw" },
	hermes: { iconSource: "lobehub", label: "Hermes Agent" },
	"claude-code": { iconSource: "lobehub", label: "Claude Code" },
	codex: { iconSource: "lobehub", label: "Codex" },
	pi: { iconSource: "lobehub", label: "Pi" },
	opencode: { iconSource: "lobehub", label: "OpenCode" },
	ironhermes: { iconSource: "clawdi", label: "IronHermes" },
} as const;

function renderFramework(id: string): string {
	return renderToStaticMarkup(
		<AgentFrameworkIcon agent={id} pixelSize={40} boxClassName="size-10" />,
	);
}

/** The `d` of every `<path>` in the markup, so two glyphs can be compared. */
function glyphPaths(markup: string): string[] {
	return [...markup.matchAll(/<path[^>]*\sd="([^"]*)"/g)].map((match) => match[1] ?? "");
}

describe("AgentFrameworkIcon", () => {
	test("renders all supported framework IDs as accessible inline SVG components", () => {
		expect(Object.keys(FRAMEWORKS)).toEqual([...FRAMEWORK_BRAND_ICON_IDS]);
		for (const id of FRAMEWORK_BRAND_ICON_IDS) {
			const { iconSource, label } = FRAMEWORKS[id];
			expect(frameworkBrandIcon(id)?.label).toBe(label);
			const markup = renderFramework(id);
			expect(markup).toContain("<svg");
			expect(markup).toContain('role="img"');
			expect(markup).toContain(`aria-label="${label}"`);
			expect(markup).toContain(`<title>${id === "opencode" ? "opencode" : label}</title>`);
			expect(markup).toContain(`data-icon-source="${iconSource}"`);
			expect(markup).not.toContain("<img");
		}
	});

	test("attributes each framework mark to its real source rather than one global vendor", () => {
		const bySource: Record<string, string[]> = { clawdi: [], lobehub: [] };
		for (const id of FRAMEWORK_BRAND_ICON_IDS) {
			const markup = renderFramework(id);
			for (const source of Object.keys(bySource)) {
				if (markup.includes(`data-icon-source="${source}"`)) bySource[source]?.push(id);
			}
		}
		expect(bySource.lobehub).toEqual([
			"openclaw",
			"hermes",
			"claude-code",
			"codex",
			"pi",
			"opencode",
		]);
		expect(bySource.clawdi).toEqual(["ironhermes"]);
	});

	test("never dresses IronHermes in the upstream Hermes identity", () => {
		const ironHermes = renderFramework("ironhermes");
		const hermes = renderFramework("hermes");
		// Different plate: Hermes is pinned to a forced-white tile with a black glyph.
		expect(hermes).toContain("bg-white");
		expect(hermes).toContain("text-black");
		expect(ironHermes).not.toContain("bg-white");
		expect(ironHermes).not.toContain("text-black");
		// Different glyph: not one path is shared between the two marks.
		const ironHermesPaths = glyphPaths(ironHermes);
		expect(ironHermesPaths).toHaveLength(1);
		expect(ironHermesPaths[0]?.length).toBeGreaterThan(0);
		for (const path of glyphPaths(hermes)) {
			expect(ironHermesPaths).not.toContain(path);
		}
	});

	test("keeps the IronHermes mark monochrome and self-contained", () => {
		const markup = renderFramework("ironhermes");
		expect(markup).toContain('fill="currentColor"');
		expect(markup).toContain('fill-rule="evenodd"');
		expect(markup).toContain('viewBox="0 0 24 24"');
		expect(markup).not.toContain("<defs");
		expect(markup).not.toContain("linearGradient");
		expect(markup).not.toContain("<image");
	});

	test("preserves the claude_code wire alias", () => {
		expect(frameworkBrandIcon("claude_code")?.icon).toBe(frameworkBrandIcon("claude-code")?.icon);
	});

	test("renders custom avatars with their accessible label", () => {
		const markup = renderToStaticMarkup(
			<AgentFrameworkIcon
				agent="codex"
				pixelSize={32}
				boxClassName="size-8 rounded-md"
				avatarUrl="https://example.test/avatar.png"
				alt="Custom avatar"
			/>,
		);
		expect(markup).toContain('<img src="https://example.test/avatar.png"');
		expect(markup).toContain('alt="Custom avatar"');
		expect(markup).not.toContain("<svg");
		expect(markup).not.toContain('data-icon-source="lobehub"');
	});
});
