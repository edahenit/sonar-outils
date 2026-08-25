"""Tests of publishing comments on the request merge request.

This is the only notification channel used (see prompt § environment): no
other system is involved.
"""

from __future__ import annotations

import json

import httpx
import pytest

from migration.notification import ClientNotificationGitLab, ErreurNotification

TOKEN = "glpat-secret-de-test"


def _client(handler) -> ClientNotificationGitLab:
    transport = httpx.MockTransport(handler)
    return ClientNotificationGitLab(
        base_url="https://gitlab.groupe.example", token=TOKEN, transport=transport,
    )


def test_publier_commentaire_poste_sur_le_bon_endpoint():
    appels = []

    def handler(request: httpx.Request) -> httpx.Response:
        appels.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/v4/projects/100/merge_requests/42/notes"
        corps = json.loads(request.content)
        assert corps == {"body": "Hello, this is a test comment."}
        return httpx.Response(201, json={"id": 999})

    with _client(handler) as client:
        client.publier_commentaire_mr(
            projet_id=100, mr_iid=42, corps="Hello, this is a test comment."
        )
    assert len(appels) == 1


def test_publier_commentaire_erreur_http_leve_sans_exposer_le_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "403 Forbidden"})

    with _client(handler) as client, pytest.raises(ErreurNotification) as exc_info:
        client.publier_commentaire_mr(projet_id=100, mr_iid=42, corps="x")
    assert TOKEN not in str(exc_info.value)
