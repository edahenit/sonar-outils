"""Requester notification: comment on the request merge request.

The only channel used (see prompt environment table). This module builds no
text: it carries a body already rendered by ``rapport.py`` to the GitLab
API, never interpreting or escaping it differently — a malformed Markdown
comment is a rendering bug, to be fixed in ``rapport.py``, not here.
"""

from __future__ import annotations

import httpx


class ErreurNotification(Exception):
    """Publishing the comment failed. Never contains the token."""


class ClientNotificationGitLab:
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

    def __enter__(self) -> ClientNotificationGitLab:
        return self

    def __exit__(self, *_args: object) -> None:
        self.fermer()

    def publier_commentaire_mr(self, projet_id: int, mr_iid: int, corps: str) -> None:
        """Adds ``corps`` (Markdown) as a note on the merge request.

        A note, never an edit of an existing note: each execution (check,
        then final report) leaves its own trace, timestamped by GitLab,
        rather than editing a comment already read by the requester.
        """
        chemin = f"/api/v4/projects/{projet_id}/merge_requests/{mr_iid}/notes"
        try:
            reponse = self._client.post(chemin, json={"body": corps})
        except httpx.HTTPError as exc:
            raise ErreurNotification(
                f"comment publishing failed ({chemin}): network error: {exc}"
            )
        if reponse.status_code not in (200, 201):
            raise ErreurNotification(
                f"comment publishing failed ({chemin}): HTTP status {reponse.status_code}"
            )
