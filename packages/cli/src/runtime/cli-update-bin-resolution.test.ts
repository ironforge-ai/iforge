import { describe, expect, test } from "bun:test";
import { chmodSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { installedCliBin } from "./cli-update";

/**
 * The hosted CLI installer resolved only `bin/clawdi`, while a package built
 * from this repository installs `bin/iforge` — so `installCliPackage` rejected
 * its own freshly installed CLI with "iforge bin is missing", naming a path it
 * had never been going to find. Verified against a real `npm install -g
 * --prefix` of this package's tarball, which produces `bin/iforge` only.
 *
 * Nothing caught it because the sole package carrying `bin/clawdi` is the
 * pre-rebrand published one, and no test installs a locally built tarball.
 */

function prefixWithBins(...names: string[]): string {
	const root = mkdtempSync(join(tmpdir(), "cli-bin-resolution-"));
	mkdirSync(join(root, "bin"), { recursive: true });
	for (const name of names) {
		const file = join(root, "bin", name);
		writeFileSync(file, "#!/bin/sh\nexit 0\n");
		chmodSync(file, 0o755);
	}
	return root;
}

describe("installed CLI bin resolution", () => {
	test("resolves the iforge bin a package built from this repo installs", () => {
		const root = prefixWithBins("iforge");
		expect(installedCliBin(root)).toBe(join(root, "bin", "iforge"));
	});

	test("does not resolve upstream clawdi's bin", () => {
		// iforge names only (user directive, 2026-09-30): a prefix carrying only `bin/clawdi` is
		// not this product's install.
		expect(installedCliBin(prefixWithBins("clawdi"))).toBeNull();
	});

	test("resolves iforge when a prefix carries both spellings", () => {
		const root = prefixWithBins("clawdi", "iforge");
		expect(installedCliBin(root)).toBe(join(root, "bin", "iforge"));
	});

	test("returns null for a prefix with no CLI bin at all", () => {
		expect(installedCliBin(prefixWithBins())).toBeNull();
	});

	test("ignores a non-executable file with the right name", () => {
		// npm leaves a symlink it makes executable; a plain unexecutable file is
		// not an install this runtime may swap onto cliManagedBin.
		const root = mkdtempSync(join(tmpdir(), "cli-bin-resolution-"));
		mkdirSync(join(root, "bin"), { recursive: true });
		writeFileSync(join(root, "bin", "iforge"), "not executable\n");
		chmodSync(join(root, "bin", "iforge"), 0o644);
		expect(installedCliBin(root)).toBeNull();
	});
});
