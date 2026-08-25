"""Resolution of the requester's real identity, on the GitLab side.

Founding rule of this whole module (see prompt §3): no security decision is
based on a job environment variable. Neither ``CI_COMMIT_AUTHOR``
(forgeable via ``git config``), nor ``GITLAB_USER_LOGIN`` alone (holds the
merger, not the requester, on a branch pipeline triggered by a merge).
Identity is re-read from the GitLab server:

    GET /projects/:id/repository/commits/:sha/merge_requests  -> the originating MR
    -> author.id field
    -> GET /users/:id -> identities[] -> extern_uid

Any ambiguity at any step of this chain (no MR for the commit, several MRs,
no identity from the corporate SSO provider, several identities from that
same provider) is a refusal. This module never picks arbitrarily among
several possible results: an arbitrary choice here would amount to guessing
who made the request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class ErreurResolutionDemandeur(Exception):
    """The identity resolution chain failed or is ambiguous.

    This is never a requester error in the sense of the request file: it is
    an anomaly that must interrupt the authorization check before it even
    starts, and be logged as such.
    """


@dataclass(frozen=True)
class DemandeurResolu:
    """Requester identity, established server-side."""

    id_utilisateur: int
    login_gitlab: str
    extern_uid: str
    fournisseur: str
    mr_iid: int  # to post the report on the right merge request, without an extra query


class ClientGitLab:
    """Client to the GitLab API, for identity resolution only.

    Every account here is expected to carry exactly **one** external
    identity (single SSO integration — confirmed, not assumed): this
    client therefore does not need to be told which provider name to
    expect, unlike the SonarQube side (``sonar_client.py``, which
    searches across *every* account on an instance by UID and must
    disambiguate by provider name — ``instance.fournisseur_identite_sso``
    — to avoid a false match against an unrelated account). Zero or
    several identities on an account is a directory anomaly here, refused
    rather than resolved by picking one arbitrarily.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={"PRIVATE-TOKEN": token},
            transport=transport,
            timeout=timeout,
        )

    def fermer(self) -> None:
        self._client.close()

    def __enter__(self) -> ClientGitLab:
        return self

    def __exit__(self, *_args: object) -> None:
        self.fermer()

    def _get(self, chemin: str) -> Any:
        try:
            reponse = self._client.get(chemin)
        except httpx.HTTPError as exc:
            raise ErreurResolutionDemandeur(
                f"GitLab call {chemin} failed: network error: {exc}"
            )
        if reponse.status_code != 200:
            raise ErreurResolutionDemandeur(
                f"GitLab call {chemin} failed: HTTP status {reponse.status_code}"
            )
        try:
            return reponse.json()
        except ValueError:
            raise ErreurResolutionDemandeur(
                f"GitLab call {chemin}: non-JSON response"
            )

    def _mr_associee(self, projet_id: int, commit_sha: str) -> dict[str, Any]:
        chemin = f"/api/v4/projects/{projet_id}/repository/commits/{commit_sha}/merge_requests"
        merge_requests: list[dict[str, Any]] = self._get(chemin)
        if len(merge_requests) == 0:
            raise ErreurResolutionDemandeur(
                f"no merge request associated with commit {commit_sha} of project {projet_id}: "
                "impossible to establish a requester."
            )
        if len(merge_requests) > 1:
            raise ErreurResolutionDemandeur(
                f"{len(merge_requests)} merge requests associated with commit {commit_sha} of project {projet_id}: "
                "ambiguous situation, no arbitrary choice will be made."
            )
        return merge_requests[0]

    def _identite_externe(self, id_utilisateur: int) -> tuple[str, str]:
        """Returns ``(extern_uid, provider)`` for the account's single
        external identity. ``provider`` is purely informational here
        (carried into ``DemandeurResolu.fournisseur`` for the journal),
        never compared against a configured value — see the class
        docstring."""
        chemin = f"/api/v4/users/{id_utilisateur}"
        utilisateur = self._get(chemin)
        identites = utilisateur.get("identities", [])
        if len(identites) == 0:
            raise ErreurResolutionDemandeur(
                f"GitLab user {id_utilisateur} has no external identity."
            )
        if len(identites) > 1:
            raise ErreurResolutionDemandeur(
                f"GitLab user {id_utilisateur} has {len(identites)} external identities: "
                "directory anomaly, to be handled by hand."
            )
        identite = identites[0]
        return str(identite["extern_uid"]), str(identite.get("provider", ""))

    def resoudre_demandeur(self, projet_id: int, commit_sha: str) -> DemandeurResolu:
        """Resolves the real identity of the author of the merge request
        behind ``commit_sha`` on ``projet_id``.

        ``commit_sha`` is the SHA of the merge commit that triggered the
        pipeline (``CI_COMMIT_SHA``) — never read as a decision variable
        here, only passed in by the caller to query the server, which
        renders the verdict.
        """
        merge_request = self._mr_associee(projet_id, commit_sha)
        auteur = merge_request["author"]
        id_utilisateur = int(auteur["id"])
        extern_uid, fournisseur = self._identite_externe(id_utilisateur)
        return DemandeurResolu(
            id_utilisateur=id_utilisateur,
            login_gitlab=str(auteur["username"]),
            extern_uid=extern_uid,
            fournisseur=fournisseur,
            mr_iid=int(merge_request["iid"]),
        )
