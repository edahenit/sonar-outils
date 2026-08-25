"""Client for Ansible Automation Platform / AWX.

The GitLab runners have no network path to the SonarQube hosts (SSH
blocked) — only the AWX controller does. Every step normally run as
``ansible-playbook site.yml --tags <step>`` is therefore launched as an
AWX job template of the same name and polled to completion here, instead
of executing locally. ``ansible/site.yml`` and the roles under
``ansible/roles/`` do not change: AWX runs that exact same content, it
only replaces WHO invokes it — see root README, § GitLab CI / Ansible
split, which this module preserves: GitLab CI still decides and traces
between two steps, it now just does so by polling a remote job instead of
waiting on a local subprocess.

AWX response field names (``job`` on launch, ``status`` values) are taken
from AWX's documented API; not independently confirmed against a real
instance — see ``docs/a-verifier.md``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

# AWX job statuses that stop polling. "successful" is the only success;
# everything else terminal is a normal (not exceptional) failure outcome,
# left for the caller to interpret — see ResultatJobAwx.succes.
_STATUTS_TERMINAUX = {"successful", "failed", "error", "canceled"}


class ErreurApiAwx(Exception):
    """Unexpected error calling the AWX API: network failure, non-2xx
    status, or a job template name that resolves to zero or several
    matches (never an arbitrary choice — same principle as ClientGitLab's
    identity resolution)."""


class DelaiJobAwxDepasse(Exception):
    """The AWX job stayed in a non-terminal status beyond the allotted
    timeout. Never a caller error: the run stays at its last confirmed
    state, resumable — see the runbook."""


@dataclass(frozen=True)
class ResultatJobAwx:
    """Outcome of an AWX job, once it reached a terminal status."""

    job_id: int
    statut: str
    url_ihm: str

    @property
    def succes(self) -> bool:
        return self.statut == "successful"


class ClientAWX:
    """One instance per pipeline job. No secret kept beyond the HTTP
    session's lifetime."""

    def __init__(
        self,
        base_url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        dormir: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._dormir = dormir
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            timeout=timeout,
        )

    def fermer(self) -> None:
        self._client.close()

    def __enter__(self) -> ClientAWX:
        return self

    def __exit__(self, *_args: object) -> None:
        self.fermer()

    def _requete(self, methode: str, chemin: str, **kwargs: Any) -> Any:
        try:
            reponse = self._client.request(methode, chemin, **kwargs)
        except httpx.HTTPError as exc:
            raise ErreurApiAwx(f"AWX call {chemin} failed: network error: {exc}")
        if reponse.status_code >= 400:
            raise ErreurApiAwx(f"AWX call {chemin} failed: HTTP status {reponse.status_code}")
        try:
            return reponse.json()
        except ValueError:
            raise ErreurApiAwx(f"AWX call {chemin}: non-JSON response")

    def _id_gabarit(self, nom_gabarit: str) -> int:
        """Resolves a job template's numeric id from its exact name — job
        templates are looked up by name on every call, never a hardcoded
        id, so renumbering on the AWX side never silently breaks the
        pipeline."""
        donnees = self._requete("GET", "/api/v2/job_templates/", params={"name": nom_gabarit})
        resultats = donnees.get("results", [])
        if len(resultats) != 1:
            raise ErreurApiAwx(
                f"job template '{nom_gabarit}': {len(resultats)} match(es) on AWX, expected exactly 1."
            )
        return resultats[0]["id"]

    def lancer(self, nom_gabarit: str, extra_vars: dict[str, Any]) -> int:
        """Launches the job template named ``nom_gabarit`` with the given
        extra-vars and returns the new job's id.

        ``extra_vars`` never carries a SonarQube token: those are AWX's
        own credentials, attached to the job template — see
        docs/installation.md, step 8.
        """
        gabarit_id = self._id_gabarit(nom_gabarit)
        donnees = self._requete(
            "POST", f"/api/v2/job_templates/{gabarit_id}/launch/",
            json={"extra_vars": extra_vars},
        )
        return int(donnees.get("job", donnees.get("id")))

    def statut(self, job_id: int) -> ResultatJobAwx:
        donnees = self._requete("GET", f"/api/v2/jobs/{job_id}/")
        return ResultatJobAwx(
            job_id=job_id,
            statut=donnees["status"],
            url_ihm=f"{self._base_url}/#/jobs/playbook/{job_id}/output",
        )

    def attendre(
        self, job_id: int, intervalle_secondes: float = 5.0, timeout_secondes: float = 1800.0,
    ) -> ResultatJobAwx:
        """Polls until the job reaches a terminal status, or raises
        ``DelaiJobAwxDepasse`` once ``timeout_secondes`` has elapsed."""
        tentatives_max = max(1, int(timeout_secondes / intervalle_secondes))
        for _ in range(tentatives_max):
            resultat = self.statut(job_id)
            if resultat.statut in _STATUTS_TERMINAUX:
                return resultat
            self._dormir(intervalle_secondes)
        raise DelaiJobAwxDepasse(
            f"AWX job {job_id} still non-terminal after {timeout_secondes}s — see the runbook."
        )
