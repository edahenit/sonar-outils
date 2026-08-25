"""Tests of the AWX (Ansible Automation Platform) client.

Context: the GitLab runners have no network path to the SonarQube hosts
(SSH blocked) — only the AWX controller does. Every Ansible step is
therefore launched as an AWX job template and polled to completion,
instead of running ``ansible-playbook`` directly on the runner. See
``docs/a-verifier.md`` for the AWX API response fields not independently
confirmed against a real instance.
"""

from __future__ import annotations

import httpx
import pytest

from migration.awx_client import ClientAWX, DelaiJobAwxDepasse, ErreurApiAwx

TOKEN = "awx-token-de-test-1234567890"
BASE_URL = "https://awx.groupe.example"


def _client(handler, dormir=None) -> ClientAWX:
    transport = httpx.MockTransport(handler)
    kwargs = {}
    if dormir is not None:
        kwargs["dormir"] = dormir
    return ClientAWX(base_url=BASE_URL, token=TOKEN, transport=transport, **kwargs)


# --- lancer(): resolving the job template by name, then launching -------


def test_lancer_resout_le_gabarit_par_nom_et_retourne_lid_du_job():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/job_templates/" and request.method == "GET":
            assert request.url.params["name"] == "preflight"
            return httpx.Response(200, json={"results": [{"id": 17}]})
        if request.url.path == "/api/v2/job_templates/17/launch/" and request.method == "POST":
            return httpx.Response(201, json={"job": 4242})
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    with _client(handler) as client:
        job_id = client.lancer("preflight", {"sonar_run_id": "entite-alpha/grp-alpha-x"})
    assert job_id == 4242


def test_lancer_transmet_les_extra_vars():
    corps_recus = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/job_templates/":
            return httpx.Response(200, json={"results": [{"id": 1}]})
        corps_recus.append(request.content)
        return httpx.Response(201, json={"job": 1})

    with _client(handler) as client:
        client.lancer("export", {"sonar_cle_source": "com.alpha:facturation-api"})
    assert b"com.alpha:facturation-api" in corps_recus[0]


def test_lancer_leve_une_erreur_si_aucun_gabarit_ne_correspond():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    with _client(handler) as client, pytest.raises(ErreurApiAwx):
        client.lancer("gabarit-inexistant", {})


def test_lancer_leve_une_erreur_si_plusieurs_gabarits_correspondent():
    """Never an arbitrary choice among several matches — same principle as
    ClientGitLab's identity resolution."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"id": 1}, {"id": 2}]})

    with _client(handler) as client, pytest.raises(ErreurApiAwx):
        client.lancer("gabarit-ambigu", {})


def test_lancer_leve_une_erreur_sur_statut_http_erreur():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "internal error"})

    with _client(handler) as client, pytest.raises(ErreurApiAwx):
        client.lancer("preflight", {})


# --- attendre(): polling until a terminal status -------------------------


def test_attendre_retourne_immediatement_si_deja_reussi():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 4242, "status": "successful"})

    with _client(handler) as client:
        resultat = client.attendre(4242)
    assert resultat.succes is True
    assert resultat.statut == "successful"


def test_attendre_reessaie_jusqua_un_statut_terminal():
    statuts = iter(["waiting", "running", "successful"])
    appels_dormir = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 4242, "status": next(statuts)})

    with _client(handler, dormir=appels_dormir.append) as client:
        resultat = client.attendre(4242, intervalle_secondes=5.0)
    assert resultat.succes is True
    assert appels_dormir == [5.0, 5.0]


def test_attendre_retourne_un_resultat_non_reussi_sur_echec_termine():
    """A failed/errored/canceled AWX job is a normal terminal outcome,
    returned to the caller — never raised: the caller (migration.cli)
    decides what that means for the run's state, exactly like a nonzero
    'ansible-playbook' exit code did before."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 4242, "status": "failed"})

    with _client(handler) as client:
        resultat = client.attendre(4242)
    assert resultat.succes is False
    assert resultat.statut == "failed"


def test_attendre_leve_delai_depasse_si_jamais_terminal():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 4242, "status": "running"})

    with _client(handler, dormir=lambda _s: None) as client, pytest.raises(DelaiJobAwxDepasse):
        client.attendre(4242, intervalle_secondes=1.0, timeout_secondes=3.0)


def test_url_ihm_pointe_vers_le_job() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 4242, "status": "successful"})

    with _client(handler) as client:
        resultat = client.attendre(4242)
    assert str(4242) in resultat.url_ihm
    assert resultat.url_ihm.startswith(BASE_URL)
