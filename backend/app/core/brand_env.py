"""The public environment contract boundary, Python half.

``IFORGE_*`` is the only spelling a user may set (D-05a). This module copies each public name
into the internal ``CLAWDI_*`` register the backend already reads, so no upstream-aligned read
site has to move -- ``app/runtime_entrypoint.py`` keeps reading ``CLAWDI_PROCESS_ROLE`` and its
existing test keeps passing unedited, which is D-01's whole point.

The map is written out verbatim rather than parsed from ``config/rebrand-env-contract.txt`` at
import time, for the same reason ``packages/cli/src/lib/brand-env.ts`` embeds it: the shipped
artifact has no repository beside it. ``backend/Dockerfile`` copies ``backend/`` alone into
``/app/backend``, so a runtime read of ``/app/config/rebrand-env-contract.txt`` would raise
``FileNotFoundError`` at import and take down ``python -m app.runtime_entrypoint`` on every
container boot. ``backend/tests/test_brand_env.py`` asserts this map equals the contract file
and shares its key set with the TypeScript map, so the three cannot drift.

Standard library only.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping

BRAND_ENV_ALIASES: dict[str, str] = {
    "IFORGE_AGENT_TYPE": "CLAWDI_AGENT_TYPE",
    "IFORGE_API_URL": "CLAWDI_API_URL",
    "IFORGE_AUTH_TOKEN": "CLAWDI_AUTH_TOKEN",
    "IFORGE_AUTH_TOKEN_ORIGIN": "CLAWDI_AUTH_TOKEN_ORIGIN",
    "IFORGE_BACKEND_IMAGE": "CLAWDI_BACKEND_IMAGE",
    "IFORGE_BACKEND_IMAGE_TAG": "CLAWDI_BACKEND_IMAGE_TAG",
    "IFORGE_CHANNEL": "CLAWDI_CHANNEL",
    "IFORGE_CLOUD_ADMIN_API_KEY": "CLAWDI_CLOUD_ADMIN_API_KEY",
    "IFORGE_CLOUD_WORKLOAD_SIGNING_PRIVATE_KEY": "CLAWDI_CLOUD_WORKLOAD_SIGNING_PRIVATE_KEY",
    "IFORGE_DAEMON_RPC_ALLOW_REMOTE": "CLAWDI_DAEMON_RPC_ALLOW_REMOTE",
    "IFORGE_DAEMON_RPC_HOST": "CLAWDI_DAEMON_RPC_HOST",
    "IFORGE_DAEMON_RPC_PORT": "CLAWDI_DAEMON_RPC_PORT",
    "IFORGE_DAEMON_RPC_TOKEN": "CLAWDI_DAEMON_RPC_TOKEN",
    "IFORGE_DEBUG": "CLAWDI_DEBUG",
    "IFORGE_DEPLOY_API_URL": "CLAWDI_DEPLOY_API_URL",
    "IFORGE_DESKTOP_ARCH": "CLAWDI_DESKTOP_ARCH",
    "IFORGE_DESKTOP_UPDATE_CHANNEL": "CLAWDI_DESKTOP_UPDATE_CHANNEL",
    "IFORGE_DESKTOP_UPDATE_FEED_URL": "CLAWDI_DESKTOP_UPDATE_FEED_URL",
    "IFORGE_DESKTOP_VERSION": "CLAWDI_DESKTOP_VERSION",
    "IFORGE_EGRESS_GID": "CLAWDI_EGRESS_GID",
    "IFORGE_EGRESS_UID": "CLAWDI_EGRESS_UID",
    "IFORGE_ENVIRONMENT_ID": "CLAWDI_ENVIRONMENT_ID",
    "IFORGE_INSTALL_PREFIX": "CLAWDI_INSTALL_PREFIX",
    "IFORGE_NO_AUTO_UPDATE": "CLAWDI_NO_AUTO_UPDATE",
    "IFORGE_NO_UPDATE_CHECK": "CLAWDI_NO_UPDATE_CHECK",
    "IFORGE_OPENCLAW_HOSTED_SOURCE": "CLAWDI_OPENCLAW_HOSTED_SOURCE",
    "IFORGE_PROCESS_ROLE": "CLAWDI_PROCESS_ROLE",
    "IFORGE_RUN_DIR": "CLAWDI_RUN_DIR",
    "IFORGE_RUNTIME_GID": "CLAWDI_RUNTIME_GID",
    "IFORGE_RUNTIME_HOME": "CLAWDI_RUNTIME_HOME",
    "IFORGE_RUNTIME_MODE": "CLAWDI_RUNTIME_MODE",
    "IFORGE_RUNTIME_UID": "CLAWDI_RUNTIME_UID",
    "IFORGE_SECRET_EXPORT_PASSPHRASE": "CLAWDI_SECRET_EXPORT_PASSPHRASE",
    "IFORGE_SERVE_DEBUG": "CLAWDI_SERVE_DEBUG",
    "IFORGE_SERVE_MODE": "CLAWDI_SERVE_MODE",
    "IFORGE_STATE_DIR": "CLAWDI_STATE_DIR",
    "IFORGE_SYSTEMD_APPLY": "CLAWDI_SYSTEMD_APPLY",
    "IFORGE_TEST_POSTGRES_CPUS": "CLAWDI_TEST_POSTGRES_CPUS",
    "IFORGE_TEST_POSTGRES_MEMORY_LIMIT": "CLAWDI_TEST_POSTGRES_MEMORY_LIMIT",
    "IFORGE_TEST_POSTGRES_PIDS_LIMIT": "CLAWDI_TEST_POSTGRES_PIDS_LIMIT",
    "IFORGE_TEST_RUNNER_CPUS": "CLAWDI_TEST_RUNNER_CPUS",
    "IFORGE_TEST_RUNNER_MEMORY_LIMIT": "CLAWDI_TEST_RUNNER_MEMORY_LIMIT",
    "IFORGE_TEST_RUNNER_PIDS_LIMIT": "CLAWDI_TEST_RUNNER_PIDS_LIMIT",
    "IFORGE_VERSION": "CLAWDI_VERSION",
    "IFORGE_WA_NETWORK_NAMESPACE_MARKER": "CLAWDI_WA_NETWORK_NAMESPACE_MARKER",
    "IFORGE_WA_SIDECAR_LOG_LEVEL": "CLAWDI_WA_SIDECAR_LOG_LEVEL",
    "IFORGE_WA_SIDECAR_SOCKET_PATH": "CLAWDI_WA_SIDECAR_SOCKET_PATH",
    "IFORGE_WA_SIDECAR_STATE_ROOT": "CLAWDI_WA_SIDECAR_STATE_ROOT",
    "IFORGE_WA_WEBSOCKET_URL": "CLAWDI_WA_WEBSOCKET_URL",
    "IFORGE_WEB_IMAGE": "CLAWDI_WEB_IMAGE",
    "IFORGE_WEB_IMAGE_TAG": "CLAWDI_WEB_IMAGE_TAG",
    "IFORGE_WINDOWS_PUBLISHER": "CLAWDI_WINDOWS_PUBLISHER",
    "IFORGE_WINDOWS_TASK_TEST": "CLAWDI_WINDOWS_TASK_TEST",
}


def refuse_user_supplied_internal_names(
    env: MutableMapping[str, str] | None = None,
) -> list[str]:
    """Delete every internal register name that arrived WITHOUT its public counterpart.

    Returns the names deleted, in map order. Mutates ``env`` in place. The Python mirror of
    ``refuseUserSuppliedInternalNames`` in ``packages/cli/src/lib/brand-env.ts`` (plan 03.1-09);
    the two rules are deliberately identical, because a boundary that is exclusive in one
    process and additive in the next is not a property of the product.

    The ``CLAWDI_*`` register is this process's own private namespace: the only supported way to
    put a value in it is to set the ``IFORGE_*`` name the contract maps onto it (D-05a). A value
    found there with no public counterpart present cannot have come from this boundary -- it was
    supplied from outside it, which is exactly what BRAND-02 says is never accepted.

    This must run BEFORE :func:`apply_brand_env_aliases`. Applying first would copy public values
    into the register and make a name this process just wrote indistinguishable from one an
    operator typed.

    The refusal is driven by the contract, never by the ``CLAWDI_`` prefix. A non-member such as
    ``CLAWDI_HOME`` (D-07/X-1) or ``CLAWDI_WA_EGRESS`` (an iptables chain name) has no public
    spelling, so it is not a name anyone could have supplied "under the wrong spelling", and it
    is left alone. An empty public value is unset everywhere else in this module, so it cannot
    rescue a supplied internal name here either.
    """
    target = os.environ if env is None else env
    refused: list[str] = []
    for public_name, internal_name in BRAND_ENV_ALIASES.items():
        if target.get(public_name):
            continue
        if internal_name not in target:
            continue
        del target[internal_name]
        refused.append(internal_name)
    return refused


def apply_brand_env_aliases(env: MutableMapping[str, str] | None = None) -> int:
    """Apply the contract to ``env`` in place and return how many pairs were applied.

    The public key it consumed is KEPT, not deleted. That is the load-bearing half of the
    exclusive boundary: the public spelling is what a child process inherits and re-derives its
    own internal register from, which is what lets :func:`refuse_user_supplied_internal_names`
    run unconditionally at every entrypoint with no provenance marker and no special case for
    "the parent set this".

    The reason the previous, additive design gave for never removing an internal name it did not
    set -- parent processes write mapped internal names into child environments, and stripping
    them at the child's entrypoint would break the daemon, the systemd units and self-update --
    is preserved by this design rather than abandoned. Those child environments now carry the
    PUBLIC spelling of every contract member, so the child's own boundary re-derives the
    register instead of inheriting it.

    An empty value is treated as unset so it cannot overwrite a resolved default. This function
    on its own never deletes an internal name it did not set; deletion belongs to
    :func:`refuse_user_supplied_internal_names`, which runs first at the entrypoint.
    """
    target = os.environ if env is None else env
    applied = 0
    for public_name, internal_name in BRAND_ENV_ALIASES.items():
        value = target.get(public_name)
        if not value:
            continue
        target[internal_name] = value
        applied += 1
    return applied
