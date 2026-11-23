import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { IronHermesAdapter } from "../../src/adapters/ironhermes";
import { tarSkillDir } from "../../src/lib/tar";
import {
	type AgentHomeOverrideSnapshot,
	restoreAgentHomeOverrides,
	snapshotAndClearAgentHomeOverrides,
} from "../commands/helpers";

/**
 * IRON-03: skills round-trip through IronHermes with their NESTED layout
 * intact, and every write resolves under the ROOT install (D-13).
 *
 * The skills surface was copied wholesale from the Hermes adapter in plan
 * 02-01 because the `skills/<category>/<name>/SKILL.md` traversal already
 * exists for exactly this layout (D-08). This file is the proof that the copy
 * behaves — collection, `listSkillKeys` parity, exclusions at depth, the
 * catalog and trajectory trees, the empty and encoding edges, and the
 * archive round trip.
 */

let tmpHome: string;
let origHome: string | undefined;
let origHomeOverrides: AgentHomeOverrideSnapshot = {};

function installRoot(): string {
	return join(tmpHome, ".ironhermes");
}

function skillsRoot(): string {
	return join(installRoot(), "skills");
}

/** Plant `<skills>/<relativeKey>/SKILL.md`. `relativeKey` may be nested. */
function writeSkill(relativeKey: string, body = "---\nname: demo\n---\n"): string {
	const dir = join(skillsRoot(), ...relativeKey.split("/"));
	mkdirSync(dir, { recursive: true });
	writeFileSync(join(dir, "SKILL.md"), body);
	return dir;
}

/** Plant a SKILL.md anywhere under the install root, outside `skills/`. */
function writeFileUnderInstall(relativePath: string, body: string): void {
	const full = join(installRoot(), ...relativePath.split("/"));
	mkdirSync(join(full, ".."), { recursive: true });
	writeFileSync(full, body);
}

async function keys(adapter: IronHermesAdapter): Promise<string[]> {
	return (await adapter.skills.collect()).map((s) => s.skillKey).sort();
}

beforeEach(() => {
	origHome = process.env.HOME;
	origHomeOverrides = snapshotAndClearAgentHomeOverrides();
	tmpHome = mkdtempSync(join(tmpdir(), "clawdi-ironhermes-skills-"));
	process.env.HOME = tmpHome;
	mkdirSync(skillsRoot(), { recursive: true });
});

afterEach(() => {
	if (origHome) process.env.HOME = origHome;
	else delete process.env.HOME;
	restoreAgentHomeOverrides(origHomeOverrides);
	origHomeOverrides = {};
	rmSync(tmpHome, { recursive: true, force: true });
});

describe("IronHermesAdapter.collectSkills — nested layout (IRON-03, D-08)", () => {
	it("collects skills/<category>/<name>/SKILL.md with the nested key preserved", async () => {
		writeSkill("git/commit-helper", "---\nname: commit helper\n---\n");
		writeSkill("git/rebase-helper", "---\nname: rebase helper\n---\n");

		const adapter = new IronHermesAdapter();
		const skills = await adapter.skills.collect();

		expect(skills.map((s) => s.skillKey).sort()).toEqual([
			"git/commit-helper",
			"git/rebase-helper",
		]);
		// `name` is the leaf directory, not the whole key — the category is
		// carried by the key alone.
		expect(skills.find((s) => s.skillKey === "git/commit-helper")?.name).toBe("commit-helper");
		expect(skills.find((s) => s.skillKey === "git/commit-helper")?.content).toContain(
			"commit helper",
		);
		expect(skills.find((s) => s.skillKey === "git/commit-helper")?.filePath).toBe(
			join(skillsRoot(), "git", "commit-helper", "SKILL.md"),
		);
	});

	it("listSkillKeys() returns exactly the key set collectSkills() returns", async () => {
		writeSkill("git/commit-helper");
		writeSkill("git/rebase-helper");
		writeSkill("flat-skill");
		writeSkill("deep/nested/leaf");

		const adapter = new IronHermesAdapter();
		const collected = await keys(adapter);
		const listed = (await adapter.skills.listKeys()).sort();

		expect(listed).toEqual(collected);
		expect(listed).toEqual([
			"deep/nested/leaf",
			"flat-skill",
			"git/commit-helper",
			"git/rebase-helper",
		]);
	});

	it("excludes SKIP_DIRS and dot-directories at EVERY depth, not just the top level", async () => {
		writeSkill("git/commit-helper");
		// SKIP_DIRS two levels deep inside skills/.
		writeSkill("git/node_modules/x");
		writeSkill("node_modules/y");
		writeSkill("a/b/dist/z");
		// Dot-directories at the root and at depth.
		writeSkill(".hidden/y");
		writeSkill("git/.archive/old");

		const adapter = new IronHermesAdapter();

		expect(await keys(adapter)).toEqual(["git/commit-helper"]);
		expect(await adapter.skills.listKeys()).toEqual(["git/commit-helper"]);
	});

	it("never reads the optional skills catalog that ships beside skills/ (D-09)", async () => {
		writeSkill("git/commit-helper");
		// Upstream ships this catalog; its own reference documentation states the
		// entries are NOT active until installed explicitly. Reporting them would
		// tell the user their agent has capabilities it does not have (T-02-14).
		const catalog = join(installRoot(), "optional-skills", "research", "deep-dive");
		mkdirSync(catalog, { recursive: true });
		writeFileSync(join(catalog, "SKILL.md"), "---\nname: deep dive\n---\n");
		const catalogFlat = join(installRoot(), "optional-skills", "flat-catalog-entry");
		mkdirSync(catalogFlat, { recursive: true });
		writeFileSync(join(catalogFlat, "SKILL.md"), "---\nname: flat\n---\n");

		const adapter = new IronHermesAdapter();

		expect(await keys(adapter)).toEqual(["git/commit-helper"]);
		expect(await adapter.skills.listKeys()).toEqual(["git/commit-helper"]);
	});

	it("never reads sessions/ as a skills source (D-09)", async () => {
		writeSkill("git/commit-helper");
		// Trajectory files — tool-call traces, not conversation messages. The
		// conversation surface is the store's `messages` table.
		writeFileUnderInstall("sessions/2026-08-18/trace.jsonl", '{"tool":"bash"}\n');
		const sessionSkill = join(installRoot(), "sessions", "impostor");
		mkdirSync(sessionSkill, { recursive: true });
		writeFileSync(join(sessionSkill, "SKILL.md"), "---\nname: impostor\n---\n");

		const adapter = new IronHermesAdapter();

		expect(await keys(adapter)).toEqual(["git/commit-helper"]);
		expect(await adapter.skills.listKeys()).toEqual(["git/commit-helper"]);
	});
});

describe("IronHermesAdapter.collectSkills — empty edge probe (IRON-03 / empty)", () => {
	it("yields [] when the skills directory is absent, and does not throw", async () => {
		rmSync(skillsRoot(), { recursive: true, force: true });
		const adapter = new IronHermesAdapter();

		expect(await adapter.skills.collect()).toEqual([]);
		expect(await adapter.skills.listKeys()).toEqual([]);
	});

	it("yields [] for an empty skills directory", async () => {
		const adapter = new IronHermesAdapter();

		expect(await adapter.skills.collect()).toEqual([]);
		expect(await adapter.skills.listKeys()).toEqual([]);
	});

	it("yields exactly one key for a single-skill tree", async () => {
		writeSkill("solo");
		const adapter = new IronHermesAdapter();

		expect(await keys(adapter)).toEqual(["solo"]);
		expect(await adapter.skills.listKeys()).toEqual(["solo"]);
	});

	it("yields no key — and does not throw — for a category holding no SKILL.md at any depth", async () => {
		mkdirSync(join(skillsRoot(), "empty-category", "deeper", "deepest"), { recursive: true });
		writeFileSync(join(skillsRoot(), "empty-category", "README.md"), "# not a skill\n");
		const adapter = new IronHermesAdapter();

		expect(await adapter.skills.collect()).toEqual([]);
		expect(await adapter.skills.listKeys()).toEqual([]);
	});
});

describe("IronHermesAdapter.collectSkills — encoding edge probe (IRON-03 / encoding)", () => {
	it("applies no case folding: two siblings differing only in case are two distinct keys", async () => {
		writeSkill("Alpha", "---\nname: upper\n---\n");
		writeSkill("beta", "---\nname: lower\n---\n");

		const adapter = new IronHermesAdapter();

		// If the adapter case-folded, `Alpha` would collapse to `alpha`.
		expect(await keys(adapter)).toEqual(["Alpha", "beta"]);
	});

	it("stores the exact byte sequence the directory entry gave it", async () => {
		writeSkill("Mixed.Case-Key_v2");

		const adapter = new IronHermesAdapter();
		const collected = await adapter.skills.collect();

		expect(collected).toHaveLength(1);
		const key = collected[0]?.skillKey ?? "";
		expect(Buffer.from(key, "utf8")).toEqual(Buffer.from("Mixed.Case-Key_v2", "utf8"));
		expect(key.normalize("NFC")).toBe(key);
	});

	it("applies no Unicode normalization: both normalization forms are filtered identically", async () => {
		// `cafe` + acute, composed (U+00E9) versus decomposed (e + U+0301). macOS
		// can hand back the decomposed form where the same name typed elsewhere
		// composes, so two logically identical names can arrive as different byte
		// sequences. The adapter normalizes NEITHER — `isValidSkillKey` is the
		// only filter, and its pattern is ASCII-only, so both forms are rejected
		// identically rather than one being silently merged into the other.
		// Written as escapes so the two forms stay distinguishable in source.
		const composed = "caf\u00e9";
		const decomposed = "cafe\u0301";
		writeSkill(composed, "---\nname: composed\n---\n");
		writeSkill(decomposed, "---\nname: decomposed\n---\n");
		writeSkill("ascii-sibling");

		expect(composed).not.toBe(decomposed);
		expect(composed.normalize("NFD")).toBe(decomposed);

		const adapter = new IronHermesAdapter();

		expect(await keys(adapter)).toEqual(["ascii-sibling"]);
		expect(await adapter.skills.listKeys()).toEqual(["ascii-sibling"]);
	});
});

describe("IronHermesAdapter.writeSkillArchive — nested round trip (Success Criterion 4)", () => {
	it("round-trips a NESTED key: written under <category>/<name>, rescanned with that exact key", async () => {
		const sourceDir = writeSkill("git/commit-helper", "---\nname: commit helper\n---\nbody\n");
		writeFileSync(join(sourceDir, "extra.md"), "# extra\n");
		const tarBytes = await tarSkillDir(sourceDir, skillsRoot(), "git/commit-helper");

		// Remove the source so a pass can only come from re-extraction.
		rmSync(join(skillsRoot(), "git"), { recursive: true, force: true });

		const adapter = new IronHermesAdapter();
		await adapter.skills.writeArchive("git/commit-helper", tarBytes);

		const extracted = join(skillsRoot(), "git", "commit-helper", "SKILL.md");
		expect(existsSync(extracted)).toBe(true);
		expect(readFileSync(extracted, "utf-8")).toContain("commit helper");
		expect(existsSync(join(skillsRoot(), "git", "commit-helper", "extra.md"))).toBe(true);

		expect(await keys(adapter)).toEqual(["git/commit-helper"]);
		expect(await adapter.skills.listKeys()).toEqual(["git/commit-helper"]);
	});

	it("getSkillPath anchors the nested key under the root install's skills directory", () => {
		const adapter = new IronHermesAdapter();
		expect(adapter.skills.path("git/commit-helper")).toBe(
			join(skillsRoot(), "git", "commit-helper", "SKILL.md"),
		);
	});
});

describe("IronHermesAdapter — writes pin to the ROOT install, never a profile (D-13)", () => {
	beforeEach(() => {
		// Profile directories with their own skills trees. Reads pool across every
		// store (02-02); writes resolve ONE workspace, exactly as the OpenClaw
		// precedent does. Profile skill directories are a small fixed overlay that
		// already exists at root — not user-authored content.
		const profileSkill = join(installRoot(), "profiles", "dev", "skills", "kanban-worker");
		mkdirSync(profileSkill, { recursive: true });
		writeFileSync(join(profileSkill, "SKILL.md"), "---\nname: kanban\n---\n");
		const nestedProfileSkill = join(
			installRoot(),
			"profiles",
			"ops",
			"skills",
			"infra",
			"terraform",
		);
		mkdirSync(nestedProfileSkill, { recursive: true });
		writeFileSync(join(nestedProfileSkill, "SKILL.md"), "---\nname: terraform\n---\n");
	});

	it("does not collect skills that live under profiles/", async () => {
		writeSkill("git/commit-helper");
		const adapter = new IronHermesAdapter();

		const collected = await keys(adapter);
		expect(collected).toEqual(["git/commit-helper"]);
		expect(collected).not.toContain("kanban-worker");
		expect(collected).not.toContain("infra/terraform");
		expect(await adapter.skills.listKeys()).toEqual(["git/commit-helper"]);
	});

	it("resolves every write target under the root install", async () => {
		const adapter = new IronHermesAdapter();

		expect(adapter.skills.rootDir()).toBe(skillsRoot());
		expect(adapter.skills.path("git/commit-helper")).toStartWith(`${skillsRoot()}/`);
		expect(adapter.skills.sharedPath("demo", "owner")).toStartWith(`${skillsRoot()}/`);
		for (const target of [
			adapter.skills.rootDir(),
			adapter.skills.path("git/commit-helper"),
			adapter.skills.sharedPath("demo", "owner"),
		]) {
			expect(target).not.toContain(`${join(installRoot(), "profiles")}/`);
		}
	});

	it("writeSkillArchive lands under the root install and leaves every profile tree untouched", async () => {
		const sourceDir = writeSkill("git/commit-helper", "---\nname: commit helper\n---\n");
		const tarBytes = await tarSkillDir(sourceDir, skillsRoot(), "git/commit-helper");
		rmSync(join(skillsRoot(), "git"), { recursive: true, force: true });

		const adapter = new IronHermesAdapter();
		await adapter.skills.writeArchive("git/commit-helper", tarBytes);

		expect(existsSync(join(skillsRoot(), "git", "commit-helper", "SKILL.md"))).toBe(true);
		expect(
			existsSync(join(installRoot(), "profiles", "dev", "skills", "git", "commit-helper")),
		).toBe(false);
		// The pre-existing profile overlay is exactly as it was.
		expect(
			readFileSync(
				join(installRoot(), "profiles", "dev", "skills", "kanban-worker", "SKILL.md"),
				"utf-8",
			),
		).toContain("kanban");
	});

	it("removeLocalSkill deletes only under the root install", async () => {
		writeSkill("git/commit-helper");
		const adapter = new IronHermesAdapter();

		await adapter.skills.remove("git/commit-helper");

		expect(existsSync(join(skillsRoot(), "git", "commit-helper"))).toBe(false);
		expect(
			existsSync(join(installRoot(), "profiles", "dev", "skills", "kanban-worker", "SKILL.md")),
		).toBe(true);
	});
});
