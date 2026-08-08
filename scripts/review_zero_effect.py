"""One shared definition of the zero-effect review gate shape.

A review attempt can terminate *before* the provider is launched: the preflight
raised, no lane was bound, no callback was ingested, and no operation row was
created.  Such an attempt owns no durable effect, so the next invocation may
supersede it — including at an unchanged HEAD, where a normal terminal attempt
would instead be replayed as a receipt.

That admission is deliberately narrow, and it used to be re-derived at each
site that needed it (the current-review identity check, the harness diagnostic
classifier, and the same-HEAD attempt retry).  The three copies disagreed about
which fields participate and about whether ``None`` counts as empty, so a gate
could be zero-effect to one caller and not to another.  This module is the
single owner of that decision.

The exact predicate
-------------------

A gate state is zero-effect when **all** of the following hold:

* ``status`` is ``attention-required``;
* ``lanes`` is an empty list — no reviewer lane was ever bound;
* ``round_results``, ``final_results``, and ``evidence`` are each empty;
* ``attempt.status`` is ``terminal``;
* ``attempt.terminal.result`` is ``attention-required``;
* ``attempt.terminal.lane_results`` is an empty list.

``None`` policy: an absent mapping and an empty mapping are treated **the same**
for ``round_results``, ``final_results``, and ``evidence``.  A gate written
before those keys were materialized is as effect-free as one that materialized
them empty, and refusing ``None`` only meant the oldest gates could never be
superseded.  Any non-empty value — of any type — is an effect.

``execution_protocol`` is intentionally *not* part of the shape.  Callers that
require a specific protocol pass ``execution_protocol=`` to assert it on top of
the shape, which keeps "is this effect-free?" separate from "is this the
protocol I speak?".

Owning no durable effect is a necessary but not sufficient condition for
superseding a lineage: callers must still prove no operation row exists.  See
``zero_effect_attempt_is_quiescent`` in ``task_review_identity``.
"""

from __future__ import annotations

from typing import Any, Mapping


ZERO_EFFECT_STATUS = "attention-required"
ZERO_EFFECT_RESULT = "attention-required"
EXACT_HEAD_ATTEMPT_PROTOCOL = "exact-head-attempt-v1"

#: Gate fields that must be empty for the state to own no effect.
ZERO_EFFECT_EMPTY_FIELDS = ("round_results", "final_results", "evidence")


def _is_empty(value: object) -> bool:
    """Treat an absent mapping and an empty mapping as the same emptiness."""

    if value is None:
        return True
    if isinstance(value, (Mapping, list, tuple, set, str)):
        return len(value) == 0
    return False


def zero_effect_gate_shape(
    gate_state: Mapping[str, Any],
    *,
    execution_protocol: str = "",
) -> bool:
    """Recognize the exact pre-provider terminal gate shape.

    ``execution_protocol`` is an optional extra requirement; when given, the
    gate must also declare that protocol.  It never relaxes the shape.
    """

    if not isinstance(gate_state, Mapping):
        return False
    if gate_state.get("status") != ZERO_EFFECT_STATUS:
        return False
    if gate_state.get("lanes") != []:
        return False
    if not all(
        _is_empty(gate_state.get(field)) for field in ZERO_EFFECT_EMPTY_FIELDS
    ):
        return False
    if execution_protocol and gate_state.get("execution_protocol") != execution_protocol:
        return False
    attempt = gate_state.get("attempt")
    if not isinstance(attempt, Mapping) or attempt.get("status") != "terminal":
        return False
    terminal = attempt.get("terminal")
    if not isinstance(terminal, Mapping):
        return False
    return (
        terminal.get("result") == ZERO_EFFECT_RESULT
        and terminal.get("lane_results") == []
    )


def zero_effect_terminal_attempt(
    gate_state: Mapping[str, Any],
    terminal_result: str,
    lane_results: object,
) -> bool:
    """Recognize the same shape from an already-parsed attempt projection.

    ``task_review_flow`` holds a typed ``ReviewAttempt`` rather than the raw
    ``attempt`` mapping, so it proves the terminal half from its own values and
    the gate half from the shared predicate.  Both halves stay in this module so
    the two entry points cannot drift apart.
    """

    if terminal_result != ZERO_EFFECT_RESULT or lane_results:
        return False
    if not isinstance(gate_state, Mapping):
        return False
    return (
        gate_state.get("status") == ZERO_EFFECT_STATUS
        and gate_state.get("lanes") == []
        and all(
            _is_empty(gate_state.get(field))
            for field in ZERO_EFFECT_EMPTY_FIELDS
        )
    )
