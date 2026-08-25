"""Tests of the requester identity resolution on the GitLab side.

Principle tested throughout: identity comes from the server (the merge
requests API, then the users API), never from a job environment variable,
and any ambiguity (several MRs for a commit, several external identities
on the same account) is a refusal, never an arbitrary choice.

Every GitLab account here carries exactly one external identity (single
SSO integration, confirmed) — so unlike the SonarQube side
(``sonar_client.py``, which searches across *every* account on the
instance and must disambiguate by provider name to avoid a false match),
this client does not need to be told which provider name to expect: it
simply requires there to be exactly one identity on the already-known
account, and refuses if there are zero or several.
"""

from __future__ import annotations

import httpx
import pytest

from migration.gitlab_client import ClientGitLab, ErreurResolutionDemandeur

TOKEN = "glpat-secret-de-test-1234567890"


def _client(handler) -> ClientGitLab:
    transport = httpx.MockTransport(handler)
    return ClientGitLab(
        base_url="https://gitlab.groupe.example",
        token=TOKEN,
        transport=transport,
    )


def _route(mrs=None, users=None):
    mrs = mrs if mrs is not None else []
    users = users or {}

    def handler(request: httpx.Request) -> httpx.Response:
        chemin = request.url.path
        if chemin.endswith("/merge_requests") and "/repository/commits/" in chemin:
            return httpx.Response(200, json=mrs)
        if "/users/" in chemin:
            user_id = int(chemin.rsplit("/", 1)[-1])
            if user_id in users:
                return httpx.Response(200, json=users[user_id])
            return httpx.Response(404, json={"message": "404 Not found"})
        raise AssertionError(f"unexpected route: {chemin}")

    return handler


# --- Nominal case -------------------------------------------------------


def test_resolution_nominale():
    handler = _route(
        mrs=[{"iid": 42, "author": {"id": 7, "username": "jdupont"}}],
        users={7: {
            "id": 7, "username": "jdupont",
            "identities": [{"provider": "group_saml", "extern_uid": "uid-entreprise-42"}],
        }},
    )
    with _client(handler) as client:
        demandeur = client.resoudre_demandeur(projet_id=100, commit_sha="abc123")
    assert demandeur.id_utilisateur == 7
    assert demandeur.login_gitlab == "jdupont"
    assert demandeur.extern_uid == "uid-entreprise-42"
    # No provider name is configured up front: it is read from whichever
    # identity was actually found, purely informational (carried for the
    # journal/report, never compared against anything).
    assert demandeur.fournisseur == "group_saml"
    # The notification (rapport.py / notification.py) must know which MR
    # to post the comment on, without redoing this call: carried here.
    assert demandeur.mr_iid == 42


def test_requete_porte_sur_lid_auteur_pas_le_username():
    """The second call must target /users/<id>, never /users?username=...,
    which would depend on a username that might have been renamed since."""
    appels = []

    def handler(request: httpx.Request) -> httpx.Response:
        appels.append(request.url.path)
        if request.url.path.endswith("/merge_requests"):
            return httpx.Response(200, json=[{"iid": 1, "author": {"id": 7, "username": "jdupont"}}])
        return httpx.Response(200, json={
            "id": 7, "username": "jdupont",
            "identities": [{"provider": "group_saml", "extern_uid": "uid-42"}],
        })

    with _client(handler) as client:
        client.resoudre_demandeur(projet_id=100, commit_sha="abc123")
    assert any(a.endswith("/users/7") for a in appels)


# --- Ambiguity and absence: always a refusal, never a choice -----------


def test_aucune_mr_associee_au_commit_leve_une_erreur():
    handler = _route(mrs=[])
    with _client(handler) as client, pytest.raises(ErreurResolutionDemandeur):
        client.resoudre_demandeur(projet_id=100, commit_sha="orphelin")


def test_plusieurs_mr_associees_au_commit_leve_une_erreur():
    handler = _route(mrs=[
        {"iid": 1, "author": {"id": 7, "username": "jdupont"}},
        {"iid": 2, "author": {"id": 9, "username": "abernard"}},
    ])
    with _client(handler) as client, pytest.raises(ErreurResolutionDemandeur):
        client.resoudre_demandeur(projet_id=100, commit_sha="ambigu")


def test_utilisateur_sans_aucune_identite_leve_une_erreur():
    handler = _route(
        mrs=[{"iid": 1, "author": {"id": 7, "username": "jdupont"}}],
        users={7: {"id": 7, "username": "jdupont", "identities": []}},
    )
    with _client(handler) as client, pytest.raises(ErreurResolutionDemandeur):
        client.resoudre_demandeur(projet_id=100, commit_sha="abc123")


def test_plusieurs_identites_leve_une_erreur():
    """Directory anomaly: an account is expected to carry exactly one
    external identity here. Several — regardless of their provider names
    — is refused rather than resolved by picking the first arbitrarily."""
    handler = _route(
        mrs=[{"iid": 1, "author": {"id": 7, "username": "jdupont"}}],
        users={7: {
            "id": 7, "username": "jdupont",
            "identities": [
                {"provider": "group_saml", "extern_uid": "uid-1"},
                {"provider": "ldap-legacy", "extern_uid": "uid-2"},
            ],
        }},
    )
    with _client(handler) as client, pytest.raises(ErreurResolutionDemandeur):
        client.resoudre_demandeur(projet_id=100, commit_sha="abc123")


def test_erreur_ne_contient_pas_le_token():
    handler = _route(mrs=[])
    with _client(handler) as client, pytest.raises(ErreurResolutionDemandeur) as exc_info:
        client.resoudre_demandeur(projet_id=100, commit_sha="orphelin")
    assert TOKEN not in str(exc_info.value)
