"""Tests of operational metrics computation (batch 5, deliverable 12).

Reads the journals of several runs (already in the format tested by
test_journal.py) and derives aggregated statistics from them. No network or
git dependency: this module only reads files already present on disk.
"""

from __future__ import annotations

from pathlib import Path

from migration.journal import construire_entree, ecrire_entree
from migration.metriques import calculer_metriques, rendre_metriques_markdown


def _run_done(racine: Path, run_id: str, debut: str, fin: str) -> None:
    ecrire_entree(racine, construire_entree(run_id=run_id, etat="RECEIVED", acteur="p", horodatage=debut))
    ecrire_entree(racine, construire_entree(run_id=run_id, etat="AUTHZ_PASSED", acteur="p", horodatage=debut))
    ecrire_entree(racine, construire_entree(run_id=run_id, etat="DONE", acteur="p", horodatage=fin))


def _run_rejete(racine: Path, run_id: str, codes_refus: list[str]) -> None:
    ecrire_entree(racine, construire_entree(run_id=run_id, etat="RECEIVED", acteur="p"))
    ecrire_entree(racine, construire_entree(
        run_id=run_id, etat="AUTHZ_REJECTED", acteur="p",
        detail={"refus": [{"code": c} for c in codes_refus]},
    ))


def _run_echoue(racine: Path, run_id: str, etat_atteint: str) -> None:
    ecrire_entree(racine, construire_entree(run_id=run_id, etat="RECEIVED", acteur="p"))
    ecrire_entree(racine, construire_entree(run_id=run_id, etat="AUTHZ_PASSED", acteur="p"))
    ecrire_entree(racine, construire_entree(
        run_id=run_id, etat="FAILED", acteur="p", etat_atteint=etat_atteint,
    ))


def _run_en_cours(racine: Path, run_id: str) -> None:
    ecrire_entree(racine, construire_entree(run_id=run_id, etat="RECEIVED", acteur="p"))
    ecrire_entree(racine, construire_entree(run_id=run_id, etat="AUTHZ_PASSED", acteur="p"))
    ecrire_entree(racine, construire_entree(run_id=run_id, etat="PREFLIGHT_OK", acteur="p"))


# --- Empty case -----------------------------------------------------------


def test_aucun_run_metriques_a_zero(tmp_path):
    m = calculer_metriques(tmp_path)
    assert m.nombre_runs == 0
    assert m.taux_echec == 0.0
    assert m.duree_moyenne_secondes is None
    assert m.duree_mediane_secondes is None
    assert m.causes_rejet_habilitation == {}
    assert m.etats_echec == {}


# --- Individual runs ---------------------------------------------------


def test_run_reussi_compte_dans_duree(tmp_path):
    _run_done(tmp_path, "entite-alpha/grp-alpha-a", "2026-09-15T09:00:00Z", "2026-09-15T09:10:00Z")
    m = calculer_metriques(tmp_path)
    assert m.nombre_runs == 1
    assert m.nombre_reussis == 1
    assert m.duree_moyenne_secondes == 600.0
    assert m.duree_mediane_secondes == 600.0
    assert m.taux_echec == 0.0


def test_run_rejete_habilitation_compte_les_causes(tmp_path):
    _run_rejete(tmp_path, "entite-alpha/grp-alpha-b", ["PAS_ADMIN", "PROJET_CIBLE_INCONNU"])
    m = calculer_metriques(tmp_path)
    assert m.nombre_runs == 1
    assert m.nombre_rejetes_habilitation == 1
    assert m.causes_rejet_habilitation == {"PAS_ADMIN": 1, "PROJET_CIBLE_INCONNU": 1}
    assert m.taux_echec == 1.0
    # A refused run has no usable duration on the "successful" side.
    assert m.duree_moyenne_secondes is None


def test_run_echoue_compte_letat_atteint(tmp_path):
    _run_echoue(tmp_path, "entite-alpha/grp-alpha-c", "TRANSFERRED")
    m = calculer_metriques(tmp_path)
    assert m.nombre_runs == 1
    assert m.nombre_echoues == 1
    assert m.etats_echec == {"TRANSFERRED": 1}
    assert m.taux_echec == 1.0


def test_run_en_cours_nest_ni_reussi_ni_echoue(tmp_path):
    _run_en_cours(tmp_path, "entite-alpha/grp-alpha-d")
    m = calculer_metriques(tmp_path)
    assert m.nombre_runs == 1
    assert m.nombre_en_cours == 1
    assert m.nombre_reussis == 0
    assert m.nombre_echoues == 0
    assert m.nombre_rejetes_habilitation == 0
    # A still-in-progress run must not skew the failure rate.
    assert m.taux_echec == 0.0


# --- Aggregation across several runs -----------------------------------


def test_taux_dechec_et_duree_mediane_sur_plusieurs_runs(tmp_path):
    _run_done(tmp_path, "entite-alpha/grp-alpha-a", "2026-09-15T09:00:00Z", "2026-09-15T09:05:00Z")   # 300s
    _run_done(tmp_path, "entite-alpha/grp-alpha-b", "2026-09-15T09:00:00Z", "2026-09-15T09:15:00Z")   # 900s
    _run_done(tmp_path, "entite-beta/grp-beta-a", "2026-09-15T09:00:00Z", "2026-09-15T09:10:00Z")     # 600s
    _run_rejete(tmp_path, "entite-alpha/grp-alpha-c", ["PAS_ADMIN"])
    _run_echoue(tmp_path, "entite-beta/grp-beta-b", "IMPORTED")

    m = calculer_metriques(tmp_path)
    assert m.nombre_runs == 5
    assert m.nombre_reussis == 3
    assert m.nombre_rejetes_habilitation == 1
    assert m.nombre_echoues == 1
    assert m.taux_echec == 2 / 5
    assert m.duree_mediane_secondes == 600.0  # median of [300, 600, 900]
    assert m.causes_rejet_habilitation == {"PAS_ADMIN": 1}
    assert m.etats_echec == {"IMPORTED": 1}


def test_rendu_markdown_contient_les_chiffres_cles(tmp_path):
    _run_done(tmp_path, "entite-alpha/grp-alpha-a", "2026-09-15T09:00:00Z", "2026-09-15T09:10:00Z")
    _run_rejete(tmp_path, "entite-alpha/grp-alpha-b", ["PAS_ADMIN"])
    m = calculer_metriques(tmp_path)
    texte = rendre_metriques_markdown(m)
    assert "2" in texte  # nombre_runs
    assert "PAS_ADMIN" in texte
    assert "50" in texte or "0.5" in texte  # failure rate in some readable form
