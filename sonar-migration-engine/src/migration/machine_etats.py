"""State machine of a migration's execution (prompt §6).

Nominal sequence, linear, never skippable: a transition is legitimate only
toward the state that immediately follows the current state. The only fork
is ``RECEIVED -> AUTHZ_REJECTED`` (the authorization check may refuse
instead of continuing), and ``FAILED`` is reachable from any non-terminal
state (an execution can be interrupted at any moment).

``TARGET_CAPTURED`` is the last fully reversible state. From ``IMPORTED``
onward, deleting the target project would destroy the imported history:
this is forbidden by construction, not just documented — see
``interdire_suppression_projet_importe``.
"""

from __future__ import annotations

ETATS_SEQUENCE: tuple[str, ...] = (
    "RECEIVED",
    "AUTHZ_PASSED",
    "PREFLIGHT_OK",
    "EXPORTED",
    "TRANSFERRED",
    "TARGET_CAPTURED",
    "TARGET_DELETED",
    "PLACEHOLDER_CREATED",
    "IMPORTED",
    "KEY_RENAMED",
    "CONFIG_APPLIED",
    "DONE",
)

# States that lead to no further transition. AUTHZ_REJECTED and FAILED do
# not appear in ETATS_SEQUENCE: they are outcomes, not positions in the
# nominal sequence.
ETATS_TERMINAUX: tuple[str, ...] = ("DONE", "AUTHZ_REJECTED", "FAILED")

# Last state for which a rollback (clean abandonment, with no lasting
# consequence) remains possible.
ETAT_DERNIER_REVERSIBLE = "TARGET_CAPTURED"

# First state from which the history is imported: no deletion of the
# target project is allowed beyond this point.
ETAT_POINT_NON_RETOUR_IMPORT = "IMPORTED"


class TransitionInterdite(Exception):
    """A requested transition matches no authorized rule."""


class InterditRollback(Exception):
    """Attempt to delete the target project after the import has already
    taken place. Always a caller code error, never a decision to
    reconsider case by case — see the runbook, § incident recovery."""


def _index(etat: str) -> int:
    return ETATS_SEQUENCE.index(etat)


def etat_suivant(etat_actuel: str) -> str:
    """Next state in the nominal sequence.

    Raises ``TransitionInterdite`` if ``etat_actuel`` is a terminal state
    (``DONE`` included: nothing follows the end of the sequence) or does
    not belong to the sequence.
    """
    if etat_actuel in ETATS_TERMINAUX:
        raise TransitionInterdite(
            f"no state follows a terminal state ({etat_actuel})."
        )
    try:
        indice = _index(etat_actuel)
    except ValueError:
        raise TransitionInterdite(f"state unknown to the sequence: {etat_actuel}")
    if indice + 1 >= len(ETATS_SEQUENCE):
        raise TransitionInterdite(f"no state follows {etat_actuel}.")
    return ETATS_SEQUENCE[indice + 1]


def est_transition_autorisee(depuis: str, vers: str) -> bool:
    """True if moving from ``depuis`` to ``vers`` respects the state
    machine.

    Never raises: this is a decision function, used to validate a
    transition before writing it to the journal.
    """
    if depuis in ETATS_TERMINAUX:
        return False
    if vers == "FAILED":
        return True
    if depuis == "RECEIVED" and vers == "AUTHZ_REJECTED":
        return True
    try:
        return vers == etat_suivant(depuis)
    except TransitionInterdite:
        return False


def interdire_suppression_projet_importe(dernier_etat_confirme: str) -> None:
    """Explicit guard from §6: call before ANY action deleting the target
    project during execution or an incident recovery.

    Raises ``InterditRollback`` if ``dernier_etat_confirme`` is
    ``IMPORTED`` or later in the sequence — the history is already
    imported, a deletion would destroy it beyond recovery. Raises nothing
    for an earlier state (deletion still safe) nor for a state outside the
    sequence (``AUTHZ_REJECTED``, ``FAILED``): this guard only concerns the
    progress of the import, not the outcomes.
    """
    try:
        indice = _index(dernier_etat_confirme)
    except ValueError:
        return
    if indice >= _index(ETAT_POINT_NON_RETOUR_IMPORT):
        raise InterditRollback(
            "target project deletion refused: the confirmed state is "
            f"'{dernier_etat_confirme}', already at or after '{ETAT_POINT_NON_RETOUR_IMPORT}'. The imported "
            "history would be destroyed with no recourse — see the "
            "runbook, never roll back by deletion after IMPORTED."
        )
