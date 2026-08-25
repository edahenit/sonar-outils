"""Tests of the state machine (prompt §6).

Two properties to guarantee above all: the nominal sequence cannot be
skipped (a non-consecutive transition is refused), and the anti-rollback
guard from IMPORTED onward is a real code-level refusal, not just a runbook
note.
"""

from __future__ import annotations

import pytest

from migration.machine_etats import (
    ETATS_SEQUENCE,
    ETATS_TERMINAUX,
    InterditRollback,
    TransitionInterdite,
    est_transition_autorisee,
    etat_suivant,
    interdire_suppression_projet_importe,
)


def test_sequence_complete_dans_lordre_du_prompt():
    assert ETATS_SEQUENCE == (
        "RECEIVED", "AUTHZ_PASSED", "PREFLIGHT_OK", "EXPORTED", "TRANSFERRED",
        "TARGET_CAPTURED", "TARGET_DELETED", "PLACEHOLDER_CREATED", "IMPORTED",
        "KEY_RENAMED", "CONFIG_APPLIED", "DONE",
    )


@pytest.mark.parametrize("depuis,vers", [
    ("RECEIVED", "AUTHZ_PASSED"),
    ("AUTHZ_PASSED", "PREFLIGHT_OK"),
    ("PREFLIGHT_OK", "EXPORTED"),
    ("EXPORTED", "TRANSFERRED"),
    ("TRANSFERRED", "TARGET_CAPTURED"),
    ("TARGET_CAPTURED", "TARGET_DELETED"),
    ("TARGET_DELETED", "PLACEHOLDER_CREATED"),
    ("PLACEHOLDER_CREATED", "IMPORTED"),
    ("IMPORTED", "KEY_RENAMED"),
    ("KEY_RENAMED", "CONFIG_APPLIED"),
    ("CONFIG_APPLIED", "DONE"),
])
def test_etat_suivant_nominal(depuis, vers):
    assert etat_suivant(depuis) == vers
    assert est_transition_autorisee(depuis, vers) is True


def test_etat_suivant_leve_apres_done():
    with pytest.raises(TransitionInterdite):
        etat_suivant("DONE")


@pytest.mark.parametrize("etat_terminal", ["AUTHZ_REJECTED", "FAILED", "DONE"])
def test_etat_suivant_leve_pour_etat_terminal(etat_terminal):
    with pytest.raises(TransitionInterdite):
        etat_suivant(etat_terminal)


def test_bifurcation_authz_rejete_autorisee_depuis_received():
    assert est_transition_autorisee("RECEIVED", "AUTHZ_REJECTED") is True


def test_transition_qui_saute_un_etat_est_refusee():
    # RECEIVED -> PREFLIGHT_OK skips AUTHZ_PASSED: never legitimate.
    assert est_transition_autorisee("RECEIVED", "PREFLIGHT_OK") is False


def test_transition_qui_revient_en_arriere_est_refusee():
    assert est_transition_autorisee("TRANSFERRED", "EXPORTED") is False


@pytest.mark.parametrize("depuis", [
    "RECEIVED", "PREFLIGHT_OK", "TARGET_CAPTURED", "IMPORTED", "CONFIG_APPLIED",
])
def test_transition_vers_failed_autorisee_depuis_etat_non_terminal(depuis):
    assert est_transition_autorisee(depuis, "FAILED") is True


@pytest.mark.parametrize("etat_terminal", ["DONE", "AUTHZ_REJECTED", "FAILED"])
def test_aucune_transition_possible_depuis_un_etat_terminal(etat_terminal):
    assert est_transition_autorisee(etat_terminal, "FAILED") is False
    assert est_transition_autorisee(etat_terminal, "DONE") is False


def test_tous_les_etats_terminaux_listes():
    assert set(ETATS_TERMINAUX) == {"DONE", "AUTHZ_REJECTED", "FAILED"}


# --- Anti-rollback guard (§6: forbidden from IMPORTED onward) -----------


@pytest.mark.parametrize("etat", ["IMPORTED", "KEY_RENAMED", "CONFIG_APPLIED", "DONE"])
def test_suppression_interdite_a_partir_dimporte(etat):
    with pytest.raises(InterditRollback):
        interdire_suppression_projet_importe(etat)


@pytest.mark.parametrize("etat", [
    "RECEIVED", "AUTHZ_PASSED", "PREFLIGHT_OK", "EXPORTED", "TRANSFERRED",
    "TARGET_CAPTURED", "TARGET_DELETED", "PLACEHOLDER_CREATED",
])
def test_suppression_autorisee_avant_importe(etat):
    interdire_suppression_projet_importe(etat)  # must not raise
