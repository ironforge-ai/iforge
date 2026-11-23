import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import {
	isGeneratedRuntimeSystemdFile,
	isOwnedRuntimeSystemdEntry,
	RUNTIME_SYSTEMD_DROP_IN_FILE,
} from "./systemd-user";

// This product's drop-in id lives in `systemd-user.ts` beside the other owned names
// (`10-iforge-hosted.conf`); re-exported so upstream's import sites keep one spelling.
export { RUNTIME_SYSTEMD_DROP_IN_FILE };

export type ManagedRuntimeSystemdUnitEntry = {
	unitName: string;
	path: string;
} & (
	| { kind: "base-unit"; generatedContents?: string }
	| { kind: "hosted-drop-in"; generatedContents: string }
);

export function managedRuntimeSystemdUnitEntries(
	root: string,
	readContents: (path: string) => string | null = readRuntimeSystemdContents,
	selectedUnits?: ReadonlySet<string>,
): ManagedRuntimeSystemdUnitEntry[] {
	if (!existsSync(root)) return [];
	const managed: ManagedRuntimeSystemdUnitEntry[] = [];
	for (const entry of readdirSync(root)) {
		if (
			selectedUnits &&
			!selectedUnits.has(entry.endsWith(".service.d") ? entry.slice(0, -2) : entry)
		)
			continue;
		if (entry.endsWith(".service")) {
			const path = join(root, entry);
			// Ownership is this product's owned-name prefix or its generated header, through the
			// one predicate every collector shares (serve unit exempt). Never the `clawdi-` prefix:
			// upstream claims it on the same machine, so such a unit is another product's install
			// (iforge names only, user directive 2026-09-30).
			let generatedContents: string | null | undefined;
			const owned = isOwnedRuntimeSystemdEntry(entry, () => {
				generatedContents = generatedRuntimeSystemdContents(path, readContents);
				return generatedContents !== null;
			});
			if (!owned) continue;
			managed.push(
				typeof generatedContents === "string"
					? { kind: "base-unit", unitName: entry, path, generatedContents }
					: { kind: "base-unit", unitName: entry, path },
			);
			continue;
		}
		if (!entry.endsWith(".service.d")) continue;
		const path = join(root, entry, RUNTIME_SYSTEMD_DROP_IN_FILE);
		const generatedContents = generatedRuntimeSystemdContents(path, readContents);
		if (generatedContents === null) continue;
		managed.push({
			kind: "hosted-drop-in",
			unitName: entry.slice(0, -".d".length),
			path,
			generatedContents,
		});
	}
	return managed;
}

export function isGeneratedRuntimeSystemdPath(path: string): boolean {
	return generatedRuntimeSystemdContents(path, readRuntimeSystemdContents) !== null;
}

function readRuntimeSystemdContents(path: string): string | null {
	try {
		return readFileSync(path, "utf-8");
	} catch {
		return null;
	}
}

function generatedRuntimeSystemdContents(
	path: string,
	readContents: (path: string) => string | null,
): string | null {
	const contents = readContents(path);
	return contents !== null && isGeneratedRuntimeSystemdFile(contents) ? contents : null;
}

export function systemctlPath(): string {
	return process.env.CLAWDI_SYSTEMCTL_PATH?.trim() || "systemctl";
}

export function parseSystemctlShow(output: string): Record<string, string> {
	return Object.fromEntries(
		output
			.split(/\r?\n/)
			.map((line) => line.trim())
			.filter(Boolean)
			.map((line) => {
				const separator = line.indexOf("=");
				return separator < 0 ? [line, ""] : [line.slice(0, separator), line.slice(separator + 1)];
			}),
	);
}
