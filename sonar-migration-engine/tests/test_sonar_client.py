"""Tests of the SonarQube client: pagination, project lookup, identity
resolution (v1 and v2).

The HTTP transport is replaced by an ``httpx.MockTransport``: requests
really go through the client (URL construction, headers, pagination), only
the network response is simulated. Assertions are never made on the mock
itself, only on what the client derives from it.
"""

from __future__ import annotations

import httpx
import pytest

from migration.modele import Instance
from migration.sonar_client import ClientSonar, ErreurApiSonar

TOKEN = "squ_secret_de_test_1234567890"


def _instance(api_identite: str = "v1") -> Instance:
    return Instance(
        id="entite-alpha",
        libelle="Entity Alpha",
        url="https://sonar.alpha.test",
        api_identite=api_identite,
        fournisseur_identite_sso="saml-entreprise",
        ssh_hote="sonar-alpha.test",
        sonarqube_home="/opt/sonarqube",
        variable_token="SONAR_SRC_ENTITE_ALPHA_TOKEN",
        role="source",
    )


def _client(handler, api_identite: str = "v1") -> ClientSonar:
    transport = httpx.MockTransport(handler)
    return ClientSonar(_instance(api_identite), token=TOKEN, transport=transport)


# --- Project lookup ----------------------------------------------------------


def test_recherche_projet_trouve_avec_derniere_analyse():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["projects"] == "com.alpha:x"
        return httpx.Response(200, json={
            "paging": {"pageIndex": 1, "pageSize": 100, "total": 1},
            "components": [{
                "id": "AB12", "key": "com.alpha:x", "name": "X",
                "lastAnalysisDate": "2024-01-01T00:00:00+0000",
            }],
        })

    with _client(handler) as client:
        projet = client.rechercher_projet("com.alpha:x")
    assert projet is not None
    assert projet.id == "AB12"
    assert projet.cle == "com.alpha:x"
    assert projet.derniere_analyse == "2024-01-01T00:00:00+0000"


def test_recherche_projet_jamais_analyse():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "paging": {"pageIndex": 1, "pageSize": 100, "total": 1},
            "components": [{"id": "AB12", "key": "com.alpha:x", "name": "X"}],
        })

    with _client(handler) as client:
        projet = client.rechercher_projet("com.alpha:x")
    assert projet is not None
    assert projet.derniere_analyse is None


def test_recherche_projet_absent_retourne_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "paging": {"pageIndex": 1, "pageSize": 100, "total": 0},
            "components": [],
        })

    with _client(handler) as client:
        projet = client.rechercher_projet("com.alpha:absent")
    assert projet is None


def test_recherche_projet_ambigu_traite_comme_absent():
    """Should never happen (SonarQube keys are unique), but a security
    control that assumes uniqueness without verifying it is a fragile
    control: we refuse to pick arbitrarily."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "paging": {"pageIndex": 1, "pageSize": 100, "total": 2},
            "components": [
                {"id": "AB12", "key": "com.alpha:x"},
                {"id": "CD34", "key": "com.alpha:x"},
            ],
        })

    with _client(handler) as client:
        projet = client.rechercher_projet("com.alpha:x")
    assert projet is None


# --- Pagination --------------------------------------------------------


def test_pagination_permissions_admin_utilisateurs_250_membres():
    total = 250

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["p"])
        taille_page = int(request.url.params["ps"])
        assert request.url.params["projectKey"] == "grp-alpha-x"
        assert request.url.params["permission"] == "admin"
        debut = (page - 1) * taille_page
        fin = min(debut + taille_page, total)
        users = [{"login": f"user{i:03d}"} for i in range(debut, fin)]
        return httpx.Response(200, json={
            "paging": {"pageIndex": page, "pageSize": taille_page, "total": total},
            "users": users,
        })

    with _client(handler) as client:
        logins = client.permissions_admin_utilisateurs("grp-alpha-x")
    assert len(logins) == total
    assert len(set(logins)) == total  # no duplicate, no missed page
    assert "user000" in logins
    assert "user249" in logins


def test_pagination_permissions_admin_groupes():
    total = 3

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["p"])
        taille_page = int(request.url.params["ps"])
        debut = (page - 1) * taille_page
        fin = min(debut + taille_page, total)
        groups = [{"name": f"groupe-{i}"} for i in range(debut, fin)]
        return httpx.Response(200, json={
            "paging": {"pageIndex": page, "pageSize": taille_page, "total": total},
            "groups": groups,
        })

    with _client(handler) as client:
        noms = client.permissions_admin_groupes("grp-alpha-x")
    assert noms == ["groupe-0", "groupe-1", "groupe-2"]


def test_pagination_membres_groupe_250_utilise_selected():
    total = 250

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["name"] == "sonar-alpha-managers"
        # Never a 'q' filter: the comparison is done on the full list,
        # never on a partial server-side search.
        assert "q" not in request.url.params
        assert request.url.params["selected"] == "selected"
        page = int(request.url.params["p"])
        taille_page = int(request.url.params["ps"])
        debut = (page - 1) * taille_page
        fin = min(debut + taille_page, total)
        users = [{"login": f"membre{i:03d}"} for i in range(debut, fin)]
        return httpx.Response(200, json={
            "paging": {"pageIndex": page, "pageSize": taille_page, "total": total},
            "users": users,
        })

    with _client(handler) as client:
        logins = client.membres_groupe("sonar-alpha-managers")
    assert len(logins) == total
    assert "membre249" in logins


# --- Identity resolution v1 ------------------------------------------


def _reponse_users_v1(comptes):
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("p", "1"))
        taille_page = int(request.url.params.get("ps", "100"))
        debut = (page - 1) * taille_page
        fin = min(debut + taille_page, len(comptes))
        return httpx.Response(200, json={
            "paging": {"pageIndex": page, "pageSize": taille_page, "total": len(comptes)},
            "users": comptes[debut:fin],
        })
    return handler


def test_resolution_v1_login_unique():
    comptes = [
        {"login": "jdupont", "externalIdentity": {"provider": "saml-entreprise", "login": "uid-42"}},
        {"login": "abernard", "externalIdentity": {"provider": "saml-entreprise", "login": "uid-99"}},
    ]
    with _client(_reponse_users_v1(comptes), api_identite="v1") as client:
        resolution = client.resoudre_login_par_uid("uid-42")
    assert resolution.trouve is True
    assert resolution.login == "jdupont"
    assert resolution.doublon is False


def test_resolution_v1_ignore_identite_dun_autre_fournisseur():
    """An account may have an external identity from ANOTHER IdP (e.g.
    a personal GitHub) : it must never be confused with the corporate
    directory."""
    comptes = [
        {"login": "jdupont", "externalIdentity": {"provider": "github", "login": "uid-42"}},
    ]
    with _client(_reponse_users_v1(comptes), api_identite="v1") as client:
        resolution = client.resoudre_login_par_uid("uid-42")
    assert resolution.trouve is False
    assert resolution.doublon is False


def test_resolution_v1_aucun_resultat_compte_inconnu():
    with _client(_reponse_users_v1([]), api_identite="v1") as client:
        resolution = client.resoudre_login_par_uid("uid-jamais-vu")
    assert resolution.trouve is False
    assert resolution.doublon is False
    assert resolution.logins == ()


def test_resolution_v1_doublon_annuaire():
    comptes = [
        {"login": "jdupont.ancien", "externalIdentity": {"provider": "saml-entreprise", "login": "uid-42"}},
        {"login": "jdupont.prestataire", "externalIdentity": {"provider": "saml-entreprise", "login": "uid-42"}},
    ]
    with _client(_reponse_users_v1(comptes), api_identite="v1") as client:
        resolution = client.resoudre_login_par_uid("uid-42")
    assert resolution.trouve is False
    assert resolution.doublon is True
    assert set(resolution.logins) == {"jdupont.ancien", "jdupont.prestataire"}


# --- Identity resolution v2 (best-effort, see docs/a-verifier.md) --------


def test_resolution_v2_login_unique():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("pageIndex", "1"))
        taille_page = int(request.url.params.get("pageSize", "100"))
        comptes = [
            {"login": "jdupont", "externalLogin": "uid-42", "externalProvider": "saml-entreprise"},
        ]
        debut = (page - 1) * taille_page
        fin = min(debut + taille_page, len(comptes))
        return httpx.Response(200, json={
            "page": {"pageIndex": page, "pageSize": taille_page, "total": len(comptes)},
            "users": comptes[debut:fin],
        })

    with _client(handler, api_identite="v2") as client:
        resolution = client.resoudre_login_par_uid("uid-42")
    assert resolution.trouve is True
    assert resolution.login == "jdupont"


# --- Errors ---------------------------------------------------------------


def test_erreur_http_leve_erreur_api_sans_exposer_le_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    with _client(handler) as client, pytest.raises(ErreurApiSonar) as exc_info:
        client.rechercher_projet("com.alpha:x")
    assert TOKEN not in str(exc_info.value)
