"""Tests of the execution journal: entry format, append-only, resumption
guarantees, and publishing to the sonar-migration-runs repository (real git).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from migration.journal import (
    EntreeJournal,
    MigrationDejaReussie,
    committer_journal,
    construire_entree,
    dernier_etat_confirme,
    ecrire_entree,
    enregistrer_transition,
    lire_entrees,
    verifier_pas_deja_reussie,
)
from migration.machine_etats import TransitionInterdite

RUN_ID = "entite-alpha/grp-alpha-x"


# --- Building and serializing an entry -----------------------


def test_construire_entree_nominale():
    entree = construire_entree(
        run_id=RUN_ID, etat="RECEIVED", acteur="pipeline",
        horodatage="2026-09-15T09:00:00Z",
    )
    assert entree.etat_atteint is None
    d = entree.to_dict()
    assert EntreeJournal.depuis_dict(d) == entree


def test_construire_entree_failed_exige_etat_atteint():
    with pytest.raises(ValueError):
        construire_entree(run_id=RUN_ID, etat="FAILED", acteur="pipeline")


def test_construire_entree_etat_atteint_interdit_hors_failed():
    with pytest.raises(ValueError):
        construire_entree(
            run_id=RUN_ID, etat="RECEIVED", acteur="pipeline", etat_atteint="RECEIVED"
        )


# --- Reading / writing to disk ---------------------------------------


def test_lire_entrees_fichier_absent_retourne_liste_vide(tmp_path):
    assert lire_entrees(tmp_path, RUN_ID) == []


def test_ecrire_puis_lire_entree(tmp_path):
    entree = construire_entree(run_id=RUN_ID, etat="RECEIVED", acteur="pipeline")
    ecrire_entree(tmp_path, entree)
    entrees = lire_entrees(tmp_path, RUN_ID)
    assert entrees == [entree]


def test_ecriture_est_append_only_et_preserve_lordre(tmp_path):
    e1 = construire_entree(run_id=RUN_ID, etat="RECEIVED", acteur="pipeline")
    e2 = construire_entree(run_id=RUN_ID, etat="AUTHZ_PASSED", acteur="pipeline")
    ecrire_entree(tmp_path, e1)
    contenu_apres_e1 = (tmp_path / "journal" / "entite-alpha" / "grp-alpha-x.jsonl").read_text()
    ecrire_entree(tmp_path, e2)
    contenu_apres_e2 = (tmp_path / "journal" / "entite-alpha" / "grp-alpha-x.jsonl").read_text()
    assert contenu_apres_e2.startswith(contenu_apres_e1)
    assert lire_entrees(tmp_path, RUN_ID) == [e1, e2]


def test_fichier_journal_est_jsonl_une_ligne_par_entree(tmp_path):
    ecrire_entree(tmp_path, construire_entree(run_id=RUN_ID, etat="RECEIVED", acteur="pipeline"))
    ecrire_entree(tmp_path, construire_entree(run_id=RUN_ID, etat="AUTHZ_PASSED", acteur="pipeline"))
    chemin = tmp_path / "journal" / "entite-alpha" / "grp-alpha-x.jsonl"
    lignes = chemin.read_text(encoding="utf-8").splitlines()
    assert len(lignes) == 2
    for ligne in lignes:
        json.loads(ligne)  # each line is a standalone JSON object


# --- Last confirmed state (resumption point) -----------------------------


def test_dernier_etat_confirme_aucune_entree():
    assert dernier_etat_confirme([]) is None


def test_dernier_etat_confirme_cas_normal():
    entrees = [
        construire_entree(run_id=RUN_ID, etat="RECEIVED", acteur="p"),
        construire_entree(run_id=RUN_ID, etat="AUTHZ_PASSED", acteur="p"),
    ]
    assert dernier_etat_confirme(entrees) == "AUTHZ_PASSED"


def test_dernier_etat_confirme_apres_failed_retourne_etat_atteint():
    entrees = [
        construire_entree(run_id=RUN_ID, etat="RECEIVED", acteur="p"),
        construire_entree(run_id=RUN_ID, etat="AUTHZ_PASSED", acteur="p"),
        construire_entree(run_id=RUN_ID, etat="FAILED", acteur="p", etat_atteint="AUTHZ_PASSED"),
    ]
    assert dernier_etat_confirme(entrees) == "AUTHZ_PASSED"


# --- Replay lock (no distributed lock: see CI resource_group) ---


def test_verifier_pas_deja_reussie_ok_si_aucune_entree():
    verifier_pas_deja_reussie([], RUN_ID)  # must not raise


def test_verifier_pas_deja_reussie_ok_si_en_cours():
    entrees = [construire_entree(run_id=RUN_ID, etat="RECEIVED", acteur="p")]
    verifier_pas_deja_reussie(entrees, RUN_ID)  # must not raise


def test_verifier_pas_deja_reussie_leve_si_done():
    entrees = [
        construire_entree(run_id=RUN_ID, etat="RECEIVED", acteur="p"),
        construire_entree(run_id=RUN_ID, etat="DONE", acteur="p"),
    ]
    with pytest.raises(MigrationDejaReussie):
        verifier_pas_deja_reussie(entrees, RUN_ID)


# --- enregistrer_transition: reads, validates, writes -----------------------


def test_enregistrer_transition_premiere_doit_etre_received(tmp_path):
    with pytest.raises(TransitionInterdite):
        enregistrer_transition(tmp_path, RUN_ID, "AUTHZ_PASSED", acteur="pipeline")


def test_enregistrer_transition_nominale(tmp_path):
    enregistrer_transition(tmp_path, RUN_ID, "RECEIVED", acteur="pipeline")
    entree = enregistrer_transition(tmp_path, RUN_ID, "AUTHZ_PASSED", acteur="pipeline")
    assert entree.etat == "AUTHZ_PASSED"
    assert dernier_etat_confirme(lire_entrees(tmp_path, RUN_ID)) == "AUTHZ_PASSED"


def test_enregistrer_transition_refuse_un_saut_detat(tmp_path):
    enregistrer_transition(tmp_path, RUN_ID, "RECEIVED", acteur="pipeline")
    with pytest.raises(TransitionInterdite):
        enregistrer_transition(tmp_path, RUN_ID, "PREFLIGHT_OK", acteur="pipeline")


def test_enregistrer_transition_refuse_apres_etat_terminal(tmp_path):
    enregistrer_transition(tmp_path, RUN_ID, "RECEIVED", acteur="pipeline")
    enregistrer_transition(tmp_path, RUN_ID, "AUTHZ_REJECTED", acteur="pipeline")
    with pytest.raises(TransitionInterdite):
        enregistrer_transition(tmp_path, RUN_ID, "AUTHZ_PASSED", acteur="pipeline")


# --- Git publishing (real repository, no remote) -----------------------------


def _init_depot_git(tmp_path: Path) -> Path:
    depot = tmp_path / "runs"
    depot.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=depot, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=depot, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=depot, check=True)
    (depot / "README.md").write_text("test repository\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=depot, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=depot, check=True)
    return depot


def test_committer_journal_cree_un_commit(tmp_path):
    depot = _init_depot_git(tmp_path)
    ecrire_entree(depot, construire_entree(run_id=RUN_ID, etat="RECEIVED", acteur="pipeline"))
    cree = committer_journal(depot, "journal: RECEIVED entite-alpha/grp-alpha-x")
    assert cree is True
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=depot, check=True, capture_output=True, text=True
    ).stdout
    assert "journal: RECEIVED" in log
    statut = subprocess.run(
        ["git", "status", "--porcelain"], cwd=depot, check=True, capture_output=True, text=True
    ).stdout
    assert statut.strip() == ""  # nothing pending after the commit


def test_committer_journal_sans_changement_ne_cree_rien(tmp_path):
    depot = _init_depot_git(tmp_path)
    ecrire_entree(depot, construire_entree(run_id=RUN_ID, etat="RECEIVED", acteur="pipeline"))
    committer_journal(depot, "first commit")
    cree = committer_journal(depot, "second call, nothing new")
    assert cree is False
