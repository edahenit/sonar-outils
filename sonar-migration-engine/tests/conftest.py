"""Shared fixtures for the migration package's tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from migration.inventaire import charger_inventaire
from migration.modele import Inventaire

_FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def inventaire_test() -> Inventaire:
    return charger_inventaire(_FIXTURES / "instances_test.yml")


@pytest.fixture()
def ecrire_demande(tmp_path: Path):
    """Writes a request file under ``requests/<instance>/<name>`` in a
    temporary directory and returns (absolute_path, relative_path)."""

    def _ecrire(instance: str, nom_fichier: str, contenu: str):
        dossier = tmp_path / "requests" / instance
        dossier.mkdir(parents=True, exist_ok=True)
        chemin = dossier / nom_fichier
        chemin.write_text(contenu, encoding="utf-8")
        chemin_relatif = f"requests/{instance}/{nom_fichier}"
        return chemin, chemin_relatif

    return _ecrire


# --- Fake Sonar client factory, for authorization tests --------------------

import httpx

from migration.modele import Instance
from migration.sonar_client import ClientSonar


def _instance_test(instance_id: str, role: str, api_identite: str = "v1") -> Instance:
    return Instance(
        id=instance_id,
        libelle=f"Instance {instance_id}",
        url=f"https://{instance_id}.test",
        api_identite=api_identite,
        fournisseur_identite_sso="saml-entreprise",
        ssh_hote=f"{instance_id}.test",
        sonarqube_home="/opt/sonarqube",
        variable_token=(
            "SONAR_CENTRALE_TOKEN" if role == "centrale"
            else "SONAR_SRC_{}_TOKEN".format(instance_id.upper().replace("-", "_"))
        ),
        role=role,
    )


@pytest.fixture()
def fabriquer_client_sonar():
    """Builds a ``ClientSonar`` whose responses are driven by a small
    in-memory backend, rather than rewriting a full httpx handler in every
    authorization test.

    ``projet``: ``None`` (absent), or ``{"id": ..., "derniere_analyse": ...}``.
    ``directs``: logins holding direct admin on the project.
    ``groupes_admin``: names of the groups holding admin on the project.
    ``membres_par_groupe``: ``{group_name: [logins...]}``.
    ``comptes``: list of ``{"login": ..., "uid": ...}`` for identity
    resolution (directory UID -> local login, provider always that of the
    fake instance).
    """

    def _fabriquer(
        instance_id: str = "entite-alpha",
        role: str = "source",
        projets=None,
        directs=(),
        groupes_admin=(),
        membres_par_groupe=None,
        comptes=(),
    ):
        # 'projets' is a mapping {project_key: {"id":..., "derniere_analyse":...}}.
        # Essential: the same central client is sometimes queried for TWO
        # different keys within the same check (the target, then the
        # source for the collision check) — a response that ignored the
        # requested key would answer "found" to both, producing a false
        # collision.
        projets = projets or {}
        membres_par_groupe = membres_par_groupe or {}
        instance = _instance_test(instance_id, role)
        appels = []

        def handler(request: httpx.Request) -> httpx.Response:
            appels.append(request.url.path)
            chemin = request.url.path
            params = request.url.params

            if chemin.endswith("api/projects/search"):
                cle_demandee = params["projects"]
                projet = projets.get(cle_demandee)
                if projet is None:
                    return httpx.Response(200, json={
                        "paging": {"pageIndex": 1, "pageSize": 100, "total": 0},
                        "components": [],
                    })
                composant = {"id": projet["id"], "key": cle_demandee}
                if projet.get("derniere_analyse") is not None:
                    composant["lastAnalysisDate"] = projet["derniere_analyse"]
                return httpx.Response(200, json={
                    "paging": {"pageIndex": 1, "pageSize": 100, "total": 1},
                    "components": [composant],
                })

            if chemin.endswith("api/users/search"):
                page = int(params.get("p", "1"))
                taille = int(params.get("ps", "100"))
                debut = (page - 1) * taille
                fin = min(debut + taille, len(comptes))
                lot = [
                    {
                        "login": c["login"],
                        "externalIdentity": {
                            "provider": c.get("provider", "saml-entreprise"),
                            "login": c["uid"],
                        },
                    }
                    for c in comptes[debut:fin]
                ]
                return httpx.Response(200, json={
                    "paging": {"pageIndex": page, "pageSize": taille, "total": len(comptes)},
                    "users": lot,
                })

            if chemin.endswith("api/permissions/users"):
                page = int(params.get("p", "1"))
                taille = int(params.get("ps", "100"))
                debut = (page - 1) * taille
                fin = min(debut + taille, len(directs))
                lot = [{"login": u} for u in list(directs)[debut:fin]]
                return httpx.Response(200, json={
                    "paging": {"pageIndex": page, "pageSize": taille, "total": len(directs)},
                    "users": lot,
                })

            if chemin.endswith("api/permissions/groups"):
                page = int(params.get("p", "1"))
                taille = int(params.get("ps", "100"))
                debut = (page - 1) * taille
                fin = min(debut + taille, len(groupes_admin))
                lot = [{"name": g} for g in list(groupes_admin)[debut:fin]]
                return httpx.Response(200, json={
                    "paging": {"pageIndex": page, "pageSize": taille, "total": len(groupes_admin)},
                    "groups": lot,
                })

            if chemin.endswith("api/user_groups/users"):
                nom = params["name"]
                membres = membres_par_groupe.get(nom, [])
                page = int(params.get("p", "1"))
                taille = int(params.get("ps", "100"))
                debut = (page - 1) * taille
                fin = min(debut + taille, len(membres))
                lot = [{"login": m} for m in membres[debut:fin]]
                return httpx.Response(200, json={
                    "paging": {"pageIndex": page, "pageSize": taille, "total": len(membres)},
                    "users": lot,
                })

            raise AssertionError(f"unexpected fake Sonar route: {chemin}")

        client = ClientSonar(instance, token="test-token", transport=httpx.MockTransport(handler))
        client.appels = appels  # exposed for tests that check "always queried"
        return client

    return _fabriquer
