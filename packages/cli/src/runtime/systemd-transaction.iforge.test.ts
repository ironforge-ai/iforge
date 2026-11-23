import { describe, expect, test } from "bun:test";
import { runCommandResult, SystemdReobservationRequiredError } from "./systemd-transaction";

/**
 * Fork-owned pins for G-06.1-5: a systemctl spawn that fails must throw with
 * its error code instead of returning empty output.
 *
 * On a 24-core host the privileged systemd fixture hit its pid cap, `clone`
 * failed with EAGAIN, and `runCommandResult` came back with an empty stdout
 * that the native tests compared as systemd's answer. Only ENOENT (a missing
 * `/usr/bin/timeout`) used to throw. E2BIG and ENOBUFS reproduce the same
 * silent result deterministically on any Linux host, so they stand in for the
 * pid exhaustion here.
 *
 * The file lives beside the upstream-tracked `systemd.test.ts` instead of in
 * it, so an upstream rewrite of that file cannot drop these pins in a merge
 * resolution (03.1 D-02, the 06.1-13 `*.iforge.test.ts` precedent).
 */

function thrownBy(run: () => unknown): unknown {
	try {
		run();
	} catch (error) {
		return error;
	}
	return null;
}

describe.skipIf(process.platform !== "linux")("runCommandResult spawn failures (G-06.1-5)", () => {
	test("throws, naming E2BIG, when the command cannot be spawned instead of returning empty output", () => {
		const oversized = "x".repeat(4 * 1024 * 1024);
		expect(() =>
			runCommandResult("/bin/sh", ["-c", "true", oversized], undefined, 5_000),
		).toThrow();
		const error = thrownBy(() =>
			runCommandResult("/bin/sh", ["-c", "true", oversized], undefined, 5_000),
		);
		expect(error).toBeInstanceOf(Error);
		expect(error).not.toBeInstanceOf(SystemdReobservationRequiredError);
		const message = (error as Error).message;
		expect(message).toContain("E2BIG");
		expect(message).toContain("/bin/sh");
		// The 4 MiB argument is never echoed back into the message.
		expect(message.length).toBeLessThan(2_000);
	});

	test("throws, naming ENOBUFS, when the output overflows spawnSync's buffer instead of returning it truncated", () => {
		const run = () =>
			runCommandResult("/bin/sh", ["-c", "head -c 3000000 /dev/zero"], undefined, 5_000);
		expect(run).toThrow();
		const error = thrownBy(run);
		expect(error).toBeInstanceOf(Error);
		expect(error).not.toBeInstanceOf(SystemdReobservationRequiredError);
		expect((error as Error).message).toContain("ENOBUFS");
	});

	test("still returns a normal result and still raises reobservation for a killed client", () => {
		expect(runCommandResult("/bin/sh", ["-c", "printf ok"], undefined, 1_000)).toMatchObject({
			status: 0,
			stdout: "ok",
		});
		expect(runCommandResult("/bin/sh", ["-c", "exit 3"], undefined, 1_000)).toMatchObject({
			status: 3,
		});
		const start = Date.now();
		expect(() =>
			runCommandResult(
				"/bin/sh",
				["-c", "trap '' TERM; sh -c 'trap \"\" TERM; while :; do :; done' & wait"],
				undefined,
				50,
			),
		).toThrow(SystemdReobservationRequiredError);
		expect(Date.now() - start).toBeLessThan(5_000);
	});
});
