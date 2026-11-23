"""The companion approval policy decision, as a pure function over tenant state.

TOOL-03 requires per-tool approval policy "enforced server-side rather than by prompt instruction".
D-10 is the structural answer: the decision is made in the control plane, BEFORE the signed
pre-authorized call is minted, so the runtime executor never holds a call it could be argued out of
refusing. This module is the decision; ``app/services/companion_facade.py`` is the caller that loads
the state and acts on the answer.

**What is deliberately absent, and why the absence is the feature.** There is no database session
here, no HTTP object, and -- most importantly -- no argument payload in ``evaluate_policy``'s
signature. Agent-supplied text has no channel through which it could reach this decision at all.
That is the STRUCTURAL reason TOOL-03 holds when an agent is instructed to ignore the policy: not
that the function ignores the instruction, but that the instruction cannot be passed to it.
``backend/tests/test_companion_policy.py`` asserts the parameter names by reflection precisely so a
future refactor that threads arguments through fails there rather than in production (T-5-17).

**The inversion of the ``SessionPermission`` convention, stated explicitly.**
``app/models/session_permission.py`` records that an owner needs no row because access is implicit
-- an ABSENT row means "no grant", and the rows exist to widen access. Here the polarity is
reversed: an absent row means ``ask`` for every non-``read`` class, so a failure to insert a row
defaults SAFE rather than open. A policy engine whose missing row means "allow" grants by failing to
write (T-5-21), and that is the fail-open case this module exists to make impossible.

**Resolution order is fixed and the first refusal wins.** Destroy enablement, then the explicit
per-tool row, then the class default. Nothing downstream can re-allow a refused call. The order only
means something because there is a case where a later rule WOULD have said yes -- a row saying
``allow`` on a ``destroy`` verb for a tenant that has not enabled destroy tools.

Standard library only. Not even the registry types are imported: the class arrives as the string the
registry already validated against its closed vocabulary, so this module has one import and no way
to acquire a second.
"""

from __future__ import annotations

from dataclasses import dataclass

# The mode vocabulary a policy row may carry, mirrored by the CHECK constraint on
# ``companion_tool_policies.mode``. Two authorities for one vocabulary is how a mode ships that the
# database accepts and the decision does not understand, so the model imports this set rather than
# spelling its own.
POLICY_MODE_ALLOW = "allow"
POLICY_MODE_ASK = "ask"
POLICY_MODE_DENY = "deny"
POLICY_MODES = frozenset({POLICY_MODE_ALLOW, POLICY_MODE_ASK, POLICY_MODE_DENY})

# The mode each class resolves to when the tenant has written no row for the verb. `read` allows;
# every class that can change something asks. "Defaulting to ask-on-write" is TOOL-03's own wording,
# and it is expressed as DATA so the test that covers it can be generated from the same mapping --
# a fifth class added later arrives already covered rather than silently uncovered.
CLASS_DEFAULT_MODES: dict[str, str] = {
    "read": POLICY_MODE_ALLOW,
    "create": POLICY_MODE_ASK,
    "mutate": POLICY_MODE_ASK,
    "destroy": POLICY_MODE_ASK,
}

# The machine reason vocabulary. A decision carries WHY as a stable token rather than as prose,
# because the refusal text is a human surface that will be rewritten and the reason is what a test,
# an activity-log row and a future approvals UI all correspond on.
REASON_CLASS_DEFAULT = "class_default"
REASON_POLICY_ROW = "explicit_policy_row"
REASON_DESTROY_DISABLED = "refused_destroy_disabled"
REASON_UNCLASSIFIED = "unclassified_defaults_to_ask"
DECISION_REASONS = frozenset(
    {
        REASON_CLASS_DEFAULT,
        REASON_POLICY_ROW,
        REASON_DESTROY_DISABLED,
        REASON_UNCLASSIFIED,
    }
)


@dataclass(frozen=True)
class PolicyDecision:
    """One decision: the mode to act on, and the machine reason it was reached.

    Frozen and comparable on purpose. "Two calls with different arguments produce equal decisions"
    is only assertable if a decision has value equality, and a mutable decision could be edited
    between the gate and the mint step -- which is the one window D-10 exists to close.
    """

    mode: str
    reason: str


def evaluate_policy(
    tool_class: str, *, policy_mode: str | None, destroy_enabled: bool
) -> PolicyDecision:
    """Decide whether a call of ``tool_class`` may run, given the tenant's state.

    ``policy_mode`` is the tenant's explicit row for this exact verb, or ``None`` when no active row
    exists. ``destroy_enabled`` is the tenant's per-companion destroy switch. Both are LOADED by the
    caller: this function performs no lookup, so there is nothing here to point at the wrong tenant.

    Deliberately takes no arguments payload, no principal and no companion identifier. The tenant is
    re-derived by the caller from its own authenticated context on every call, and a lookup key that
    never enters this signature is a lookup key an argument cannot poison (T-5-22).
    """
    if tool_class == "destroy" and not destroy_enabled:
        # First, and unconditional. Evaluated before the row so an `allow` row cannot re-open a
        # companion whose destroy group the tenant has not turned on (D-06).
        return PolicyDecision(mode=POLICY_MODE_DENY, reason=REASON_DESTROY_DISABLED)

    if policy_mode is not None:
        if policy_mode not in POLICY_MODES:
            # A row carrying a mode outside the vocabulary is a row the CHECK constraint should have
            # refused. Resolving it to `ask` rather than raising keeps a corrupt row from taking the
            # endpoint down, and `ask` is the safe direction for every class including `read`.
            return PolicyDecision(mode=POLICY_MODE_ASK, reason=REASON_UNCLASSIFIED)
        return PolicyDecision(mode=policy_mode, reason=REASON_POLICY_ROW)

    default = CLASS_DEFAULT_MODES.get(tool_class)
    if default is None:
        # Unreachable through the façade: the registry parser refuses a verb row whose class is
        # outside its closed vocabulary, so an unclassified verb cannot reach a caller of this
        # function. Kept anyway, and kept as `ask`, because "never callable without approval" must
        # not depend on a parser rule living in a different module continuing to exist.
        return PolicyDecision(mode=POLICY_MODE_ASK, reason=REASON_UNCLASSIFIED)
    return PolicyDecision(mode=default, reason=REASON_CLASS_DEFAULT)
