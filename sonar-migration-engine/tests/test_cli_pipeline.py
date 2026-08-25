"""Tests of the CLI commands that orchestrate the pipeline (§ batch 3).

External dependencies (HTTP, remote git) are injected: httpx via
``MockTransport`` as in the other tests, git via real temporary
repositories with no remote (we commit, we never push in these tests).
These are therefore real behavior tests, not mock tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest

from migration.awx_client import ClientAWX
from migration.cli import (
    _parser_extra_vars,
    charger_autres_demandes,
    commande_enregistrer,
    commande_habiliter,
    commande_lancer_gabarit,
    commande_rapport_final,
    commande_valider_commit,
)
from migration.gitlab_client import ClientGitLab
from migration.inventaire import charger_inventaire
from migration.journal import committer_journal, lire_entrees
from migration.notification import ClientNotificationGitLab
from migration.sonar_client import ClientSonar

_FIXTURES = Path(__file__).parent / "fixtures"
UID = "uid-entreprise-42"


@pytest.fixture()
def inventaire_test():
    return charger_inventaire(_FIXTURES / "instances_test.yml")


def _depot_demandes(tmp_path: Path, contenu: str, chemin_relatif: str) -> tuple[Path, str]:
    depot = tmp_path / "demandes"
    depot.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=depot, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=depot, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=depot, check=True)
    (depot / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=depot, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=depot, check=True)

    chemin = depot / chemin_relatif
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(contenu, encoding="utf-8")
    subprocess.run(["git", "add", chemin_relatif], cwd=depot, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "demande"], cwd=depot, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=depot, check=True, capture_output=True, text=True
    ).stdout.strip()
    return depot, sha


def _depot_runs_vide(tmp_path: Path) -> Path:
    depot = tmp_path / "runs"
    depot.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=depot, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=depot, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=depot, check=True)
    (depot / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=depot, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=depot, check=True)
    return depot


DEMANDE_VALIDE = (
    "version: 1\n"
    "instance_source: entite-alpha\n"
    "cle_source: com.alpha:x\n"
    "cle_cible: grp-alpha-x\n"
)


# --- charger_autres_demandes ------------------------------------------


def test_charger_autres_demandes_ignore_le_fichier_exclu_et_les_invalides(
    tmp_path, inventaire_test
):
    depot = tmp_path
    (depot / "requests" / "entite-alpha").mkdir(parents=True)
    (depot / "requests" / "entite-alpha" / "grp-alpha-a.yml").write_text(
        "version: 1\ninstance_source: entite-alpha\ncle_source: com.alpha:a\ncle_cible: grp-alpha-a\n",
        encoding="utf-8",
    )
    (depot / "requests" / "entite-alpha" / "grp-alpha-x.yml").write_text(
        DEMANDE_VALIDE, encoding="utf-8",  # this one will be excluded
    )
    (depot / "requests" / "entite-alpha" / "cassee.yml").write_text(
        "[\n", encoding="utf-8",  # invalid: must be ignored, not crash
    )
    autres = charger_autres_demandes(
        depot, "requests/entite-alpha/grp-alpha-x.yml", inventaire_test
    )
    assert [d.cle_cible for d in autres] == ["grp-alpha-a"]


# --- commande_valider_commit (no token) --------------------------------


def test_valider_commit_demande_valide(tmp_path, inventaire_test, capsys):
    depot, sha = _depot_demandes(tmp_path, DEMANDE_VALIDE, "requests/entite-alpha/grp-alpha-x.yml")
    code = commande_valider_commit(depot, sha, inventaire_test)
    assert code == 0


def test_valider_commit_demande_invalide(tmp_path, inventaire_test):
    contenu = "version: 1\ninstance_source: entite-alpha\ncle_source: com.alpha:x\n"  # cle_cible missing
    depot, sha = _depot_demandes(tmp_path, contenu, "requests/entite-alpha/grp-alpha-x.yml")
    code = commande_valider_commit(depot, sha, inventaire_test)
    assert code == 2


# --- commande_habiliter (end to end, with injected fakes) ---------------


def _client_gitlab_fake(mr_iid=42, uid=UID):
    def handler(request: httpx.Request) -> httpx.Response:
        chemin = request.url.path
        if chemin.endswith("/merge_requests"):
            return httpx.Response(200, json=[{"iid": mr_iid, "author": {"id": 7, "username": "jdupont"}}])
        if "/users/" in chemin:
            return httpx.Response(200, json={
                "id": 7, "username": "jdupont",
                "identities": [{"provider": "group_saml", "extern_uid": uid}],
            })
        raise AssertionError(chemin)

    return ClientGitLab(
        base_url="https://gitlab.test", token="glpat-x",
        transport=httpx.MockTransport(handler),
    )


def _client_sonar_admin_direct(instance, uid=UID):
    # The real key of THIS instance: the central instance only knows the
    # target, the source only knows the source — otherwise the collision
    # check (central instance queried about cle_source) would always find
    # "a project", whatever key was requested, producing a false collision.
    cle_reelle = "grp-alpha-x" if instance.role == "centrale" else "com.alpha:x"

    def handler(request: httpx.Request) -> httpx.Response:
        chemin = request.url.path
        params = request.url.params
        if chemin.endswith("api/projects/search"):
            if params["projects"] != cle_reelle:
                return httpx.Response(200, json={
                    "paging": {"pageIndex": 1, "pageSize": 100, "total": 0},
                    "components": [],
                })
            return httpx.Response(200, json={
                "paging": {"pageIndex": 1, "pageSize": 100, "total": 1},
                "components": [{"id": "P1", "key": params["projects"]}],
            })
        if chemin.endswith("api/users/search"):
            return httpx.Response(200, json={
                "paging": {"pageIndex": 1, "pageSize": 100, "total": 1},
                "users": [{"login": "jdupont", "externalIdentity": {"provider": "saml-entreprise", "login": uid}}],
            })
        if chemin.endswith("api/v2/users-management/users"):
            return httpx.Response(200, json={
                "page": {"pageIndex": 1, "pageSize": 100, "total": 1},
                "users": [{"login": "jdupont", "externalLogin": uid, "externalProvider": "saml-entreprise"}],
            })
        if chemin.endswith("api/permissions/users"):
            return httpx.Response(200, json={
                "paging": {"pageIndex": 1, "pageSize": 100, "total": 1},
                "users": [{"login": "jdupont"}],
            })
        if chemin.endswith("api/permissions/groups"):
            return httpx.Response(200, json={
                "paging": {"pageIndex": 1, "pageSize": 100, "total": 0}, "groups": [],
            })
        raise AssertionError(chemin)

    return ClientSonar(instance, token="squ-x", transport=httpx.MockTransport(handler))


def _client_sonar_projet_absent(instance):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "paging": {"pageIndex": 1, "pageSize": 100, "total": 0}, "components": [],
        })
    return ClientSonar(instance, token="squ-x", transport=httpx.MockTransport(handler))


def _client_notification_capturant(captures: list):
    def handler(request: httpx.Request) -> httpx.Response:
        captures.append(request)
        return httpx.Response(201, json={"id": 1})
    return ClientNotificationGitLab(
        base_url="https://gitlab.test", token="glpat-x", transport=httpx.MockTransport(handler)
    )


def test_habiliter_accepte_et_journalise_authz_passed(tmp_path, inventaire_test):
    depot_demandes, sha = _depot_demandes(
        tmp_path, DEMANDE_VALIDE, "requests/entite-alpha/grp-alpha-x.yml"
    )
    depot_runs = _depot_runs_vide(tmp_path)
    notes = []
    fichier_etat = tmp_path / "etat.env"

    code = commande_habiliter(
        depot_demandes=depot_demandes, commit_sha=sha, depot_runs=depot_runs,
        inventaire=inventaire_test, client_gitlab=_client_gitlab_fake(),
        client_notification=_client_notification_capturant(notes),
        projet_gitlab_id=100,
        fabriquer_client_sonar=_client_sonar_admin_direct,
        committer=committer_journal,
        fichier_etat=fichier_etat,
    )
    assert code == 0
    entrees = lire_entrees(depot_runs, "entite-alpha/grp-alpha-x")
    assert [e.etat for e in entrees] == ["RECEIVED", "AUTHZ_PASSED"]
    assert len(notes) == 1
    assert b"grp-alpha-x" in notes[0].content

    # State propagation between the two moments: a dotenv file, consumed
    # by GitLab CI (artifacts: reports: dotenv) for the following jobs of
    # the SAME pipeline. The source of truth remains the journal (see
    # above); this file is only a caching convenience.
    contenu = fichier_etat.read_text(encoding="utf-8")
    assert "RUN_ID=entite-alpha/grp-alpha-x" in contenu
    assert "DEMANDE_FICHIER=requests/entite-alpha/grp-alpha-x.yml" in contenu
    assert "MR_IID=42" in contenu
    assert "SONAR_PROJET_CIBLE_ID=P1" in contenu
    # Needed by the ansible-playbook invocations of the following jobs
    # (ci/pipeline.yml): the 'habiliter' job already has everything needed
    # to provide them, no need to rederive them elsewhere.
    assert "SONAR_CLE_SOURCE=com.alpha:x" in contenu
    assert "SONAR_CLE_CIBLE=grp-alpha-x" in contenu
    assert "SONAR_SOURCE_HOST=entite-alpha" in contenu


def test_habiliter_necrit_pas_de_fichier_detat_en_cas_de_refus(tmp_path, inventaire_test):
    depot_demandes, sha = _depot_demandes(
        tmp_path, DEMANDE_VALIDE, "requests/entite-alpha/grp-alpha-x.yml"
    )
    depot_runs = _depot_runs_vide(tmp_path)
    fichier_etat = tmp_path / "etat.env"

    commande_habiliter(
        depot_demandes=depot_demandes, commit_sha=sha, depot_runs=depot_runs,
        inventaire=inventaire_test, client_gitlab=_client_gitlab_fake(),
        client_notification=_client_notification_capturant([]),
        projet_gitlab_id=100,
        fabriquer_client_sonar=_client_sonar_projet_absent,
        committer=committer_journal,
        fichier_etat=fichier_etat,
    )
    # A refusal must never suggest there is a state to resume.
    assert not fichier_etat.exists()


def test_habiliter_refuse_et_journalise_authz_rejected(tmp_path, inventaire_test):
    depot_demandes, sha = _depot_demandes(
        tmp_path, DEMANDE_VALIDE, "requests/entite-alpha/grp-alpha-x.yml"
    )
    depot_runs = _depot_runs_vide(tmp_path)
    notes = []

    code = commande_habiliter(
        depot_demandes=depot_demandes, commit_sha=sha, depot_runs=depot_runs,
        inventaire=inventaire_test, client_gitlab=_client_gitlab_fake(),
        client_notification=_client_notification_capturant(notes),
        projet_gitlab_id=100,
        fabriquer_client_sonar=_client_sonar_projet_absent,
        committer=committer_journal,
    )
    assert code == 1
    entrees = lire_entrees(depot_runs, "entite-alpha/grp-alpha-x")
    assert [e.etat for e in entrees] == ["RECEIVED", "AUTHZ_REJECTED"]
    assert len(notes) == 1


def test_habiliter_refuse_le_rejeu_dune_migration_deja_reussie(tmp_path, inventaire_test):
    depot_demandes, sha = _depot_demandes(
        tmp_path, DEMANDE_VALIDE, "requests/entite-alpha/grp-alpha-x.yml"
    )
    depot_runs = _depot_runs_vide(tmp_path)
    from migration.journal import construire_entree, ecrire_entree
    ecrire_entree(depot_runs, construire_entree(
        run_id="entite-alpha/grp-alpha-x", etat="RECEIVED", acteur="pipeline"))
    ecrire_entree(depot_runs, construire_entree(
        run_id="entite-alpha/grp-alpha-x", etat="DONE", acteur="pipeline"))

    code = commande_habiliter(
        depot_demandes=depot_demandes, commit_sha=sha, depot_runs=depot_runs,
        inventaire=inventaire_test, client_gitlab=_client_gitlab_fake(),
        client_notification=_client_notification_capturant([]),
        projet_gitlab_id=100,
        fabriquer_client_sonar=_client_sonar_admin_direct,
        committer=committer_journal,
    )
    assert code == 4


# --- commande_enregistrer -------------------------------------------------


def test_commande_enregistrer_ecrit_et_committe(tmp_path):
    depot_runs = _depot_runs_vide(tmp_path)
    code = commande_enregistrer(
        depot_runs=depot_runs, run_id="entite-alpha/grp-alpha-x", etat="RECEIVED",
        acteur="pipeline", etat_atteint=None, detail={}, committer=committer_journal,
    )
    assert code == 0
    assert [e.etat for e in lire_entrees(depot_runs, "entite-alpha/grp-alpha-x")] == ["RECEIVED"]


def test_commande_enregistrer_refuse_transition_illegitime(tmp_path):
    depot_runs = _depot_runs_vide(tmp_path)
    code = commande_enregistrer(
        depot_runs=depot_runs, run_id="entite-alpha/grp-alpha-x", etat="PREFLIGHT_OK",
        acteur="pipeline", etat_atteint=None, detail={}, committer=committer_journal,
    )
    assert code != 0


# --- commande_rapport_final -----------------------------------------------


def test_commande_rapport_final_publie_le_commentaire(tmp_path, inventaire_test):
    depot_demandes, _sha = _depot_demandes(
        tmp_path, DEMANDE_VALIDE, "requests/entite-alpha/grp-alpha-x.yml"
    )
    depot_runs = _depot_runs_vide(tmp_path)
    from migration.journal import construire_entree, ecrire_entree
    ecrire_entree(depot_runs, construire_entree(
        run_id="entite-alpha/grp-alpha-x", etat="RECEIVED", acteur="pipeline",
        horodatage="2026-09-15T09:00:00Z"))
    ecrire_entree(depot_runs, construire_entree(
        run_id="entite-alpha/grp-alpha-x", etat="DONE", acteur="pipeline",
        horodatage="2026-09-15T09:10:00Z"))
    notes = []

    code = commande_rapport_final(
        depot_demandes=depot_demandes,
        demande_fichier="requests/entite-alpha/grp-alpha-x.yml",
        depot_runs=depot_runs, run_id="entite-alpha/grp-alpha-x",
        client_notification=_client_notification_capturant(notes),
        projet_gitlab_id=100, mr_iid=42,
    )
    assert code == 0
    assert len(notes) == 1
    assert b"grp-alpha-x" in notes[0].content


# --- commande_lancer_gabarit / _parser_extra_vars -------------------------
# The runners have no network path to the SonarQube hosts: these steps
# launch an AWX job template and poll it, instead of running
# 'ansible-playbook' locally — see migration.awx_client.


def _client_awx(statut_final: str) -> ClientAWX:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/job_templates/":
            return httpx.Response(200, json={"results": [{"id": 1}]})
        if request.url.path.endswith("/launch/"):
            return httpx.Response(201, json={"job": 99})
        return httpx.Response(200, json={"id": 99, "status": statut_final})

    return ClientAWX(
        base_url="https://awx.groupe.example", token="awx-token-test",
        transport=httpx.MockTransport(handler), dormir=lambda _s: None,
    )


def test_lancer_gabarit_reussi_retourne_0():
    code = commande_lancer_gabarit(
        _client_awx("successful"), "preflight", {"sonar_run_id": "entite-alpha/grp-alpha-x"},
    )
    assert code == 0


def test_lancer_gabarit_echoue_retourne_1():
    code = commande_lancer_gabarit(_client_awx("failed"), "export", {})
    assert code == 1


def test_lancer_gabarit_delai_depasse_retourne_5():
    code = commande_lancer_gabarit(
        _client_awx("running"), "export", {}, timeout_secondes=0.001,
    )
    assert code == 5


def test_parser_extra_vars_meme_format_quansible_playbook():
    assert _parser_extra_vars(
        "sonar_run_id=entite-alpha/grp-alpha-x sonar_cle_source=com.alpha:facturation-api"
    ) == {
        "sonar_run_id": "entite-alpha/grp-alpha-x",
        "sonar_cle_source": "com.alpha:facturation-api",
    }
