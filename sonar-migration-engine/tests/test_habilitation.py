"""Tests of the authorization check — the security core of the solution.

Covers the list mandated by the prompt (batch 2, deliverable 5): a group of
250 members (pagination), the jdupont/jdupont2 homonym, the sonar-users
group holding admin, a directory duplicate, an account unknown to the
target instance, admin via the direct path, admin via a group, refusal on
both sides, refusal on only one side — plus the target project's special
cases and the guarantee that both checks always run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from migration.habilitation import controler_habilitation, est_admin
from migration.inventaire import charger_inventaire
from migration.modele import Demande

UID = "uid-entreprise-42"
_FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def inventaire_habilitation():
    return charger_inventaire(_FIXTURES / "instances_test.yml")


def _demande(cle_source="com.alpha:x", cle_cible="grp-alpha-x"):
    return Demande(
        version=1, instance_source="entite-alpha",
        cle_source=cle_source, cle_cible=cle_cible,
        fichier="requests/entite-alpha/grp-alpha-x.yml",
    )


# --- Admin via the direct path ----------------------------------------


def test_admin_par_voie_directe(fabriquer_client_sonar, inventaire_habilitation):
    client = fabriquer_client_sonar(
        projets={"grp-alpha-x": {"id": "P1"}},
        directs=["jdupont"],
        comptes=[{"login": "jdupont", "uid": UID}],
    )
    preuve = est_admin(client, "grp-alpha-x", UID, inventaire_habilitation, cote="cible")
    assert preuve.ok is True
    assert preuve.voie == "DIRECTE"
    assert preuve.login == "jdupont"
    assert preuve.refus is None


# --- Admin via a group, with 250-member pagination -------------------


def test_admin_par_voie_de_groupe_groupe_de_250_membres(
    fabriquer_client_sonar, inventaire_habilitation
):
    membres = [f"membre{i:03d}" for i in range(249)] + ["jdupont"]
    client = fabriquer_client_sonar(
        projets={"grp-alpha-x": {"id": "P1"}},
        directs=[],
        groupes_admin=["sonar-alpha-managers"],
        membres_par_groupe={"sonar-alpha-managers": membres},
        comptes=[{"login": "jdupont", "uid": UID}],
    )
    preuve = est_admin(client, "grp-alpha-x", UID, inventaire_habilitation, cote="cible")
    assert preuve.ok is True
    assert preuve.voie == "GROUPE"
    assert preuve.groupe == "sonar-alpha-managers"
    assert preuve.login == "jdupont"


# --- Homonym trap: exact comparison, not a partial filter -----------------


def test_homonyme_jdupont2_ne_vaut_pas_jdupont(fabriquer_client_sonar, inventaire_habilitation):
    client = fabriquer_client_sonar(
        projets={"grp-alpha-x": {"id": "P1"}},
        directs=[],
        groupes_admin=["sonar-alpha-managers"],
        membres_par_groupe={"sonar-alpha-managers": ["jdupont2", "abernard"]},
        comptes=[{"login": "jdupont", "uid": UID}],
    )
    preuve = est_admin(client, "grp-alpha-x", UID, inventaire_habilitation, cote="cible")
    assert preuve.ok is False
    assert preuve.refus.code == "PAS_ADMIN"


def test_jdupont_exact_dans_un_groupe_contenant_aussi_jdupont2(
    fabriquer_client_sonar, inventaire_habilitation
):
    client = fabriquer_client_sonar(
        projets={"grp-alpha-x": {"id": "P1"}},
        directs=[],
        groupes_admin=["sonar-alpha-managers"],
        membres_par_groupe={"sonar-alpha-managers": ["jdupont2", "jdupont"]},
        comptes=[{"login": "jdupont", "uid": UID}],
    )
    preuve = est_admin(client, "grp-alpha-x", UID, inventaire_habilitation, cote="cible")
    assert preuve.ok is True
    assert preuve.login == "jdupont"


# --- Overly broad group (sonar-users) --------------------------------------


def test_groupe_sonar_users_detenteur_admin_est_refuse_et_signale(
    fabriquer_client_sonar, inventaire_habilitation
):
    client = fabriquer_client_sonar(
        projets={"grp-alpha-x": {"id": "P1"}},
        directs=[],
        groupes_admin=["sonar-users"],
        membres_par_groupe={"sonar-users": ["jdupont"]},  # member, but group is forbidden
        comptes=[{"login": "jdupont", "uid": UID}],
    )
    preuve = est_admin(client, "grp-alpha-x", UID, inventaire_habilitation, cote="cible")
    assert preuve.ok is False
    assert preuve.refus.code == "PAS_ADMIN"
    assert len(preuve.anomalies) == 1
    assert preuve.anomalies[0].code == "GROUPE_TROP_LARGE"
    assert preuve.anomalies[0].alerte is True


def test_groupe_trop_large_nempeche_pas_un_autre_groupe_legitime(
    fabriquer_client_sonar, inventaire_habilitation
):
    """The overly-broad-group flag is independent of the final verdict: if
    an OTHER, legitimate group carries the authorization, the request is
    accepted AND the flag is still surfaced."""
    client = fabriquer_client_sonar(
        projets={"grp-alpha-x": {"id": "P1"}},
        directs=[],
        groupes_admin=["sonar-users", "sonar-alpha-managers"],
        membres_par_groupe={
            "sonar-users": ["jdupont"],
            "sonar-alpha-managers": ["jdupont"],
        },
        comptes=[{"login": "jdupont", "uid": UID}],
    )
    preuve = est_admin(client, "grp-alpha-x", UID, inventaire_habilitation, cote="cible")
    assert preuve.ok is True
    assert preuve.voie == "GROUPE"
    assert preuve.groupe == "sonar-alpha-managers"
    assert len(preuve.anomalies) == 1
    assert preuve.anomalies[0].code == "GROUPE_TROP_LARGE"


# --- Directory duplicate ------------------------------------------------


def test_doublon_annuaire_est_refuse_avec_alerte(fabriquer_client_sonar, inventaire_habilitation):
    client = fabriquer_client_sonar(
        projets={"grp-alpha-x": {"id": "P1"}},
        comptes=[
            {"login": "jdupont.ancien", "uid": UID},
            {"login": "jdupont.prestataire", "uid": UID},
        ],
    )
    preuve = est_admin(client, "grp-alpha-x", UID, inventaire_habilitation, cote="cible")
    assert preuve.ok is False
    assert preuve.refus.code == "DOUBLON_ANNUAIRE"
    assert preuve.refus.alerte is True


# --- Account unknown to the instance -----------------------------------------


def test_compte_inconnu_de_linstance_cible_est_refuse_sans_alerte(
    fabriquer_client_sonar, inventaire_habilitation
):
    client = fabriquer_client_sonar(projets={"grp-alpha-x": {"id": "P1"}}, comptes=[])
    preuve = est_admin(client, "grp-alpha-x", UID, inventaire_habilitation, cote="cible")
    assert preuve.ok is False
    assert preuve.refus.code == "COMPTE_INCONNU_INSTANCE"
    assert preuve.refus.alerte is False


# --- Target project special cases --------------------------------------


def test_projet_cible_inexistant(fabriquer_client_sonar, inventaire_habilitation):
    client = fabriquer_client_sonar(projets={})
    preuve = est_admin(client, "grp-alpha-x", UID, inventaire_habilitation, cote="cible")
    assert preuve.ok is False
    assert preuve.refus.code == "PROJET_CIBLE_INCONNU"


def test_projet_source_inexistant_message_different_du_cible(
    fabriquer_client_sonar, inventaire_habilitation
):
    client = fabriquer_client_sonar(projets={})
    preuve = est_admin(client, "com.alpha:x", UID, inventaire_habilitation, cote="source")
    assert preuve.ok is False
    assert preuve.refus.code == "PROJET_SOURCE_INCONNU"


def test_projet_cible_deja_analyse(fabriquer_client_sonar, inventaire_habilitation):
    client = fabriquer_client_sonar(
        projets={"grp-alpha-x": {"id": "P1", "derniere_analyse": "2024-01-01T00:00:00+0000"}},
    )
    preuve = est_admin(client, "grp-alpha-x", UID, inventaire_habilitation, cote="cible")
    assert preuve.ok is False
    assert preuve.refus.code == "PROJET_CIBLE_DEJA_ANALYSE"


def test_projet_cible_sans_aucun_groupe_admin_est_refuse_et_signale(
    fabriquer_client_sonar, inventaire_habilitation
):
    client = fabriquer_client_sonar(
        projets={"grp-alpha-x": {"id": "P1"}}, directs=[], groupes_admin=[],
        comptes=[{"login": "jdupont", "uid": UID}],
    )
    preuve = est_admin(client, "grp-alpha-x", UID, inventaire_habilitation, cote="cible")
    assert preuve.ok is False
    assert preuve.refus.code == "PROJET_CIBLE_SANS_GROUPE_ADMIN"
    assert preuve.refus.alerte is True


# --- Overall decision: both sides, always ----------------------------------


def test_refus_des_deux_cotes(fabriquer_client_sonar, inventaire_habilitation):
    source = fabriquer_client_sonar(instance_id="entite-alpha", role="source", projets={})
    cible = fabriquer_client_sonar(instance_id="centrale", role="centrale", projets={})
    decision = controler_habilitation(
        _demande(), UID, source, cible, inventaire_habilitation
    )
    assert decision.ok is False
    assert decision.preuve_source.ok is False
    assert decision.preuve_cible.ok is False
    codes = {r.code for r in decision.refus}
    assert "PROJET_SOURCE_INCONNU" in codes
    assert "PROJET_CIBLE_INCONNU" in codes


def test_refus_dun_seul_cote_source_ok_cible_refusee(
    fabriquer_client_sonar, inventaire_habilitation
):
    source = fabriquer_client_sonar(
        instance_id="entite-alpha", role="source",
        projets={"com.alpha:x": {"id": "S1"}}, directs=["jdupont"],
        comptes=[{"login": "jdupont", "uid": UID}],
    )
    cible = fabriquer_client_sonar(instance_id="centrale", role="centrale", projets={})
    decision = controler_habilitation(
        _demande(), UID, source, cible, inventaire_habilitation
    )
    assert decision.ok is False
    assert decision.preuve_source.ok is True
    assert decision.preuve_cible.ok is False
    # The report must make it possible to know which of the two sides failed.
    assert decision.preuve_cible.refus.code == "PROJET_CIBLE_INCONNU"


def test_les_deux_cotes_sont_toujours_interroges_meme_si_le_premier_echoue(
    fabriquer_client_sonar, inventaire_habilitation
):
    """Never short-circuit on the first failure: both instances must be
    queried, so the report can say which of the two checks failed."""
    source = fabriquer_client_sonar(instance_id="entite-alpha", role="source", projets={})
    cible = fabriquer_client_sonar(
        instance_id="centrale", role="centrale",
        projets={"grp-alpha-x": {"id": "C1"}}, directs=["jdupont"],
        comptes=[{"login": "jdupont", "uid": UID}],
    )
    controler_habilitation(_demande(), UID, source, cible, inventaire_habilitation)
    assert any("projects/search" in a for a in cible.appels), (
        "the target instance was never queried even though the source "
        "failed first"
    )


def test_decision_positive_des_deux_cotes(fabriquer_client_sonar, inventaire_habilitation):
    source = fabriquer_client_sonar(
        instance_id="entite-alpha", role="source",
        projets={"com.alpha:x": {"id": "S1"}}, directs=["jdupont"],
        comptes=[{"login": "jdupont", "uid": UID}],
    )
    cible = fabriquer_client_sonar(
        instance_id="centrale", role="centrale",
        projets={"grp-alpha-x": {"id": "C1"}}, directs=[], groupes_admin=["sonar-alpha-managers"],
        membres_par_groupe={"sonar-alpha-managers": ["jdupont"]},
        comptes=[{"login": "jdupont", "uid": UID}],
    )
    decision = controler_habilitation(_demande(), UID, source, cible, inventaire_habilitation)
    assert decision.ok is True
    assert decision.refus == ()


# --- Source key collision on the central instance -----------------------


def test_collision_cle_source_sur_centrale(fabriquer_client_sonar, inventaire_habilitation):
    source = fabriquer_client_sonar(
        instance_id="entite-alpha", role="source",
        projets={"com.beta:collision": {"id": "S1"}}, directs=["jdupont"],
        comptes=[{"login": "jdupont", "uid": UID}],
    )
    # On the central instance, a project ALREADY exists under the source
    # key itself (from another entity): this is a collision, independent
    # of the target project's fate.
    cible = fabriquer_client_sonar(
        instance_id="centrale", role="centrale",
        projets={
            "grp-alpha-x": {"id": "C1"},
            "com.beta:collision": {"id": "OTHER1"},
        },
        directs=[], groupes_admin=["sonar-alpha-managers"],
        membres_par_groupe={"sonar-alpha-managers": ["jdupont"]},
        comptes=[{"login": "jdupont", "uid": UID}],
    )
    decision = controler_habilitation(
        _demande(cle_source="com.beta:collision"), UID, source, cible, inventaire_habilitation
    )
    assert decision.ok is False
    assert any(r.code == "CLE_SOURCE_COLLISION_CENTRALE" for r in decision.refus)
