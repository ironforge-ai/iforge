#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
fixture="$repo_root/packages/cli/tests/fixtures/runtime-official-installer-systemd/Dockerfile"
image="clawdi-systemd-command-test:local-$$"
container="clawdi-systemd-command-test-$$"
cleanup() {
	timeout --kill-after=5s 30s docker rm --force "$container" >/dev/null 2>&1 || true
	timeout --kill-after=5s 30s docker image rm "$image" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Reuse only the systemd stage; no agent packages, models or runtime installs.
# This stage needs no checkout build context.
timeout --kill-after=15s 900s docker build --quiet --target systemd --tag "$image" - < "$fixture" >/dev/null
# The fixture runs where the daemon runs, so its CPU count, not this shell's, bounds the pin.
daemon_cpus="$(timeout --kill-after=5s 30s docker info --format '{{.NCPU}}')"
if ! [[ "$daemon_cpus" =~ ^[1-9][0-9]*$ ]]; then
	echo "docker info reported a CPU count that is not a positive integer: '$daemon_cpus'" >&2
	exit 1
fi
fixture_cpus=$((daemon_cpus < 2 ? daemon_cpus : 2))
if ((fixture_cpus == 1)); then fixture_cpuset=0; else fixture_cpuset=0-1; fi
# --cpus is a quota and does not hide host cores, while bun sizes its pools from the visible (affinity) CPUs.
# The pin keeps the thread count, and so the pid cap, independent of the host's core count (G-06.1-5).
timeout --kill-after=15s 30s docker run --detach --privileged --cgroupns=private \
	--cpus="$fixture_cpus" --cpuset-cpus="$fixture_cpuset" --memory=2g --pids-limit=2048 --name "$container" \
	--tmpfs /run --tmpfs /run/lock --tmpfs /tmp:exec \
	--workdir /work "$image" >/dev/null
timeout --kill-after=15s 30s docker exec "$container" timeout 20 bash -c '
	until systemctl show --property=Version >/dev/null 2>&1; do sleep 0.1; done
'
# Never mount the unfiltered checkout in the privileged fixture. Native command
# checks need no environment examples, so exclude all .env variants as well.
timeout --kill-after=15s 120s tar -C "$repo_root" \
	--exclude=.git --exclude=.paseo --exclude=.kamal \
	--exclude=.ssh --exclude=.aws --exclude=.kube --exclude=.gnupg \
	--exclude=.npmrc --exclude=.netrc --exclude=.clawdi.env \
	--exclude=.env --exclude='.env.*' --exclude='.envrc*' \
	--exclude=node_modules --exclude=.venv --exclude=__pycache__ --exclude='*.pyc' \
	--exclude=.pytest_cache --exclude=.ruff_cache --exclude=.turbo \
	--exclude=.output --exclude=.nitro --exclude=.tanstack --exclude='.vite*' \
	--exclude=test-results --exclude=playwright-report --exclude=coverage \
	--exclude=dist --exclude=dist-native --exclude=dist-release \
	--exclude='./backend/data' --exclude=tmp --exclude='*.log' --exclude='*.tsbuildinfo' \
	--exclude='.component-tools.*' --exclude='.provider-recovery-baseline.*' \
	-cf - . | timeout --kill-after=15s 120s docker exec --interactive "$container" tar -C /work -xf -
suite_status=0
timeout --kill-after=15s 930s docker exec "$container" timeout 900 bash -euo pipefail -c '
	cd /work
	bun install --frozen-lockfile --ignore-scripts
	package_root=/work/packages/cli
	CLAWDI_TEST_SYSTEMD_COMMAND=1 timeout 60 bun test --isolate --max-concurrency=1 \
		--timeout=15000 packages/cli/src/runtime/systemd.test.ts
' || suite_status=$?
# Pass or fail, report the fixture's pid usage so the next exhaustion shows as a number.
# docker exec lands in systemd's init.scope, which systemd caps at DefaultTasksMax, 15% of pids.max.
timeout --kill-after=5s 30s docker exec "$container" bash -c '
	read_or_unavailable() { if [[ -r "$1" ]]; then cat "$1"; else echo unavailable; fi; }
	cg="$(sed -n "s/^0:://p" /proc/self/cgroup)"
	echo "runtime-systemd fixture: nproc=$(nproc)" \
		"pids.peak=$(read_or_unavailable /sys/fs/cgroup/pids.peak) pids.max=$(cat /sys/fs/cgroup/pids.max)" \
		"exec_cgroup=$cg tasks.peak=$(read_or_unavailable "/sys/fs/cgroup$cg/pids.peak")" \
		"tasks.max=$(read_or_unavailable "/sys/fs/cgroup$cg/pids.max")" >&2
' || true
exit "$suite_status"
