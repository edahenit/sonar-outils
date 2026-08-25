"""HTTP client to a SonarQube instance.

This module decides nothing: it exposes reads (project lookup, permissions,
group membership, identity resolution) in typed form. Authorization
decisions live in ``habilitation.py``.

Every request goes through the Python equivalent of the ``uri`` module
(httpx), never through a shell call. The transport is injectable for tests
(``httpx.MockTransport``) and to allow, later, swapping in an instrumented
transport (logging, retries) without touching the rest of the module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from .modele import Instance

TAILLE_PAGE = 100


class ErreurApiSonar(Exception):
    """Failure of a call to the SonarQube API.

    The message never contains the token: only the instance, the path
    called, and the code or nature of the failure appear in it. This
    message may end up in a log or an alert.
    """

    def __init__(self, instance_id: str, chemin: str, detail: str) -> None:
        super().__init__(
            f"SonarQube [{instance_id}] {chemin}: {detail}"
        )
        self.instance_id = instance_id
        self.chemin = chemin


@dataclass(frozen=True)
class ProjetInfo:
    """Extract of ``api/projects/search`` for a project identified by its key."""

    id: str
    cle: str
    derniere_analyse: str | None  # None if the project was never analyzed


@dataclass(frozen=True)
class ResolutionLogin:
    """Result of resolving a directory UID into zero, one, or more local
    logins of the queried instance."""

    logins: tuple[str, ...]

    @property
    def trouve(self) -> bool:
        return len(self.logins) == 1

    @property
    def doublon(self) -> bool:
        return len(self.logins) > 1

    @property
    def login(self) -> str:
        if not self.trouve:
            raise ValueError(
                "login requested while resolution did not find "
                f"exactly one account (found: {len(self.logins)})"
            )
        return self.logins[0]


class ClientSonar:
    """Client for a given SonarQube instance, with its own token."""

    def __init__(
        self,
        instance: Instance,
        token: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.instance = instance
        self._client = httpx.Client(
            base_url=instance.url,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            timeout=timeout,
        )

    def fermer(self) -> None:
        self._client.close()

    def __enter__(self) -> ClientSonar:
        return self

    def __exit__(self, *_args: object) -> None:
        self.fermer()

    # --- Low-level call -------------------------------------------------

    def _get(self, chemin: str, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            reponse = self._client.get(chemin, params=dict(params))
        except httpx.HTTPError as exc:
            raise ErreurApiSonar(self.instance.id, chemin, f"network error: {exc}")
        if reponse.status_code != 200:
            raise ErreurApiSonar(
                self.instance.id, chemin,
                f"HTTP status {reponse.status_code}",
            )
        try:
            return reponse.json()
        except ValueError:
            raise ErreurApiSonar(self.instance.id, chemin, "non-JSON response")

    def _paginer_v1(
        self, chemin: str, params: Mapping[str, Any], cle_liste: str
    ) -> list[dict[str, Any]]:
        """Pagination in the v1 format (``paging: {pageIndex, pageSize,
        total}``, ``p``/``ps`` parameters), used by nearly all of the
        historical SonarQube API (permissions, groups, users)."""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            reponse = self._get(chemin, {**params, "p": page, "ps": TAILLE_PAGE})
            page_items = reponse.get(cle_liste, [])
            items.extend(page_items)
            pagination = reponse.get("paging", {})
            total = pagination.get("total", len(items))
            if not page_items or len(items) >= total:
                break
            page += 1
        return items

    def _paginer_v2(
        self, chemin: str, params: Mapping[str, Any], cle_liste: str
    ) -> list[dict[str, Any]]:
        """Pagination in the v2 format (``page: {pageIndex, pageSize,
        total}``, ``pageIndex``/``pageSize`` parameters) — see
        docs/a-verifier.md, point 2: shape not confirmed on a real
        instance."""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            reponse = self._get(
                chemin, {**params, "pageIndex": page, "pageSize": TAILLE_PAGE}
            )
            page_items = reponse.get(cle_liste, [])
            items.extend(page_items)
            pagination = reponse.get("page", {})
            total = pagination.get("total", len(items))
            if not page_items or len(items) >= total:
                break
            page += 1
        return items

    # --- Project ---------------------------------------------------------

    def rechercher_projet(self, cle: str) -> ProjetInfo | None:
        """Returns the project holding exactly this key, or ``None`` if it
        does not exist. Also returns ``None`` on ambiguity (several
        components for a key that is supposed to be unique): a security
        control must never pick arbitrarily among several results."""
        reponse = self._get("api/projects/search", {"projects": cle})
        composants = reponse.get("components", [])
        if len(composants) != 1:
            return None
        composant = composants[0]
        return ProjetInfo(
            id=str(composant["id"]),
            cle=str(composant["key"]),
            derniere_analyse=composant.get("lastAnalysisDate"),
        )

    # --- Permissions and groups -------------------------------------------

    def permissions_admin_utilisateurs(self, cle_projet: str) -> list[str]:
        """Logins holding the ``admin`` permission directly on this
        project, across all pages."""
        items = self._paginer_v1(
            "api/permissions/users",
            {"projectKey": cle_projet, "permission": "admin"},
            "users",
        )
        return [str(u["login"]) for u in items]

    def permissions_admin_groupes(self, cle_projet: str) -> list[str]:
        """Names of the groups holding the ``admin`` permission on this
        project, across all pages."""
        items = self._paginer_v1(
            "api/permissions/groups",
            {"projectKey": cle_projet, "permission": "admin"},
            "groups",
        )
        return [str(g["name"]) for g in items]

    def membres_groupe(self, nom_groupe: str) -> list[str]:
        """Logins of members of this group, across all pages.

        Deliberately uses ``selected=selected`` and never sets a ``q``
        parameter: ``q`` performs a partial server-side search
        (``jdupont`` can match ``jdupont2``), unusable for an exact
        membership comparison.
        """
        items = self._paginer_v1(
            "api/user_groups/users",
            {"name": nom_groupe, "selected": "selected"},
            "users",
        )
        return [str(u["login"]) for u in items]

    # --- Identity resolution -----------------------------------------------

    def resoudre_login_par_uid(self, uid: str) -> ResolutionLogin:
        """Resolves the directory UID into 0, 1, or several local logins of
        this instance, keeping only external identities coming from the
        corporate SSO provider declared for this instance
        (``instance.fournisseur_identite_sso``) — an account may carry an
        external identity from a completely different IdP, which must
        never be confused with the corporate directory.

        Dispatched according to ``instance.api_identite``: see
        docs/a-verifier.md, point 2, for the verification status of each
        path.
        """
        if self.instance.api_identite == "v1":
            return self._resoudre_login_v1(uid)
        if self.instance.api_identite == "v2":
            return self._resoudre_login_v2(uid)
        raise ErreurApiSonar(  # pragma: no cover - guaranteed by the inventory schema
            self.instance.id, "resoudre_login_par_uid",
            f"unknown api_identite: {self.instance.api_identite}",
        )

    def _resoudre_login_v1(self, uid: str) -> ResolutionLogin:
        """``api/users/search``. No server-side filter by external identity
        is known on this path: every account is walked and filtered
        client-side on ``externalIdentity.{provider,login}``. See
        docs/a-verifier.md, point 2, on the cost of this full walk."""
        comptes = self._paginer_v1("api/users/search", {}, "users")
        provider = self.instance.fournisseur_identite_sso
        correspondances = [
            str(c["login"]) for c in comptes
            if (identite := c.get("externalIdentity")) is not None
            and identite.get("provider") == provider
            and identite.get("login") == uid
        ]
        return ResolutionLogin(logins=tuple(correspondances))

    def _resoudre_login_v2(self, uid: str) -> ResolutionLogin:
        """``api/v2/users-management/users``. Response shape and field
        names not confirmed on a real instance — see docs/a-verifier.md,
        point 2. Implemented by analogy with known public documentation for
        this API (flat fields ``externalLogin`` / ``externalProvider``,
        ``page`` pagination envelope)."""
        comptes = self._paginer_v2("api/v2/users-management/users", {}, "users")
        provider = self.instance.fournisseur_identite_sso
        correspondances = [
            str(c["login"]) for c in comptes
            if c.get("externalProvider") == provider
            and c.get("externalLogin") == uid
        ]
        return ResolutionLogin(logins=tuple(correspondances))
