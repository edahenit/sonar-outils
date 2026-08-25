"""Tests of merge request comment rendering.

The content (English, Markdown) is what the requester actually reads: each
test checks that information required by the prompt is actually present in
the rendered text, not just that the code doesn't crash.
"""

from __future__ import annotations

from migration.habilitation import DecisionHabilitation, PreuveAdmin
from migration.journal import construire_entree
from migration.modele import Demande
from migration.rapport import rendre_commentaire_final, rendre_commentaire_habilitation


def _demande() -> Demande:
    return Demande(
        version=1, instance_source="entite-alpha",
        cle_source="com.alpha:x", cle_cible="grp-alpha-x",
        ticket="DEVOPS-1234",
        fichier="requests/entite-alpha/grp-alpha-x.yml",
    )


def _preuve_ok(cote: str) -> PreuveAdmin:
    return PreuveAdmin(
        ok=True, cote=cote, instance_id="entite-alpha" if cote == "source" else "centrale",
        projet_cle="com.alpha:x" if cote == "source" else "grp-alpha-x",
        projet_id="P1", voie="DIRECTE", login="jdupont", groupe=None,
    )


def _preuve_refusee(cote: str, code: str = "PAS_ADMIN") -> PreuveAdmin:
    from migration.messages import refus
    return PreuveAdmin(
        ok=False, cote=cote, instance_id="entite-alpha" if cote == "source" else "centrale",
        projet_cle="com.alpha:x" if cote == "source" else "grp-alpha-x",
        projet_id="P1", voie=None, login="jdupont", groupe=None,
        refus=refus(code, instance_id="x", libelle="Instance X", cle="grp-alpha-x", login="jdupont"),
    )


# --- Authorization check comment -------------------------------------------


def test_commentaire_habilitation_positive_mentionne_les_deux_cotes():
    decision = DecisionHabilitation(
        ok=True, preuve_source=_preuve_ok("source"), preuve_cible=_preuve_ok("cible"),
    )
    texte = rendre_commentaire_habilitation(_demande(), decision)
    assert "com.alpha:x" in texte
    assert "grp-alpha-x" in texte
    assert "✅" in texte or "passed" in texte.lower()


def test_commentaire_habilitation_refus_dun_seul_cote_precise_lequel():
    decision = DecisionHabilitation(
        ok=False, preuve_source=_preuve_ok("source"), preuve_cible=_preuve_refusee("cible"),
        refus=(_preuve_refusee("cible").refus,),
    )
    texte = rendre_commentaire_habilitation(_demande(), decision)
    # The requester must be able to tell, on reading it, that the source
    # side is fine and it's the target side that has a problem.
    assert "PAS_ADMIN" in texte
    assert "target" in texte.lower()


def test_commentaire_habilitation_naffiche_jamais_de_token():
    decision = DecisionHabilitation(
        ok=False, preuve_source=_preuve_refusee("source"), preuve_cible=_preuve_refusee("cible"),
        refus=(_preuve_refusee("source").refus, _preuve_refusee("cible").refus),
    )
    texte = rendre_commentaire_habilitation(_demande(), decision)
    assert "token" not in texte.lower() and "squ_" not in texte and "glpat-" not in texte


def test_commentaire_habilitation_liste_les_anomalies_signalees():
    from migration.messages import refus
    anomalie = refus(
        "GROUPE_TROP_LARGE", instance_id="centrale", libelle="Centrale",
        cle="grp-alpha-x", groupe="sonar-users", alerte=True,
    )
    decision = DecisionHabilitation(
        ok=True, preuve_source=_preuve_ok("source"), preuve_cible=_preuve_ok("cible"),
        anomalies=(anomalie,),
    )
    texte = rendre_commentaire_habilitation(_demande(), decision)
    assert "GROUPE_TROP_LARGE" in texte


# --- Final report -----------------------------------------------------------


def test_commentaire_final_done_indique_le_succes_et_la_duree():
    entrees = [
        construire_entree(run_id="entite-alpha/grp-alpha-x", etat="RECEIVED",
                           acteur="pipeline", horodatage="2026-09-15T09:00:00Z"),
        construire_entree(run_id="entite-alpha/grp-alpha-x", etat="DONE",
                           acteur="pipeline", horodatage="2026-09-15T09:12:30Z"),
    ]
    texte = rendre_commentaire_final(_demande(), entrees)
    assert "12 min" in texte or "12min" in texte or "0:12:30" in texte or "750" in texte
    assert "✅" in texte or "complete" in texte.lower()
    # The report must remind the project team what's left on their side.
    assert "sonar-project.properties" in texte or "remaining" in texte.lower()


def test_commentaire_final_failed_indique_letat_atteint():
    entrees = [
        construire_entree(run_id="entite-alpha/grp-alpha-x", etat="RECEIVED",
                           acteur="pipeline", horodatage="2026-09-15T09:00:00Z"),
        construire_entree(run_id="entite-alpha/grp-alpha-x", etat="TRANSFERRED",
                           acteur="pipeline", horodatage="2026-09-15T09:05:00Z"),
        construire_entree(run_id="entite-alpha/grp-alpha-x", etat="FAILED",
                           acteur="pipeline", horodatage="2026-09-15T09:06:00Z",
                           etat_atteint="TRANSFERRED"),
    ]
    texte = rendre_commentaire_final(_demande(), entrees)
    assert "TRANSFERRED" in texte
    assert "central team" in texte.lower() or "intervention" in texte.lower()
