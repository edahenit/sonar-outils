"""Execution journal — append-only, one JSON line per transition.

Every transition is written here **before** it is attempted, never after
(prompt §6): ``enregistrer_transition`` validates and persists the entry,
the caller only starts the corresponding action once this call has
returned without raising.

The file lives under ``journal/<run_id>.jsonl`` at the root of the
``sonar-migration-runs`` repository (``run_id`` = ``Demande.identifiant``,
see ``modele.py`` — ``<instance_source>/<slug(cle_cible)>``). One line, one
JSON object, never rewritten: a resumption adds lines, never modifies any.

No distributed lock here: mutual exclusion between two concurrent
executions of the same ``run_id`` is handled by GitLab CI's
``resource_group`` (native, already correct for this need — see
``ci/pipeline.yml``). This module only handles REPLAY: refusing to restart
a run that already completed successfully.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .machine_etats import TransitionInterdite, est_transition_autorisee

_RACINE_JOURNAL = "journal"


def _chemin_relatif(run_id: str) -> Path:
    return Path(_RACINE_JOURNAL) / (run_id + ".jsonl")


def _horodatage_actuel() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class EntreeJournal:
    """One journal line. Never contains a secret: only run, state, and
    actor identifiers, plus a structured detail."""

    horodatage: str
    run_id: str
    etat: str
    acteur: str
    etat_atteint: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def depuis_dict(cls, donnees: dict[str, Any]) -> EntreeJournal:
        return cls(
            horodatage=donnees["horodatage"],
            run_id=donnees["run_id"],
            etat=donnees["etat"],
            acteur=donnees["acteur"],
            etat_atteint=donnees.get("etat_atteint"),
            detail=donnees.get("detail", {}),
        )


def construire_entree(
    run_id: str,
    etat: str,
    acteur: str,
    etat_atteint: str | None = None,
    detail: dict[str, Any] | None = None,
    horodatage: str | None = None,
) -> EntreeJournal:
    """Builds a valid entry. ``etat_atteint`` only makes sense for
    ``etat == "FAILED"`` (the last confirmed state before the failure):
    making it mandatory in that case, and forbidden in every other, avoids
    an ambiguous journal when reread months later."""
    if etat == "FAILED" and etat_atteint is None:
        raise ValueError(
            "a 'FAILED' entry must specify 'etat_atteint' (the last "
            "confirmed state before the failure)."
        )
    if etat != "FAILED" and etat_atteint is not None:
        raise ValueError(
            "'etat_atteint' is only meaningful for a 'FAILED' entry "
            f"(received for state '{etat}')."
        )
    return EntreeJournal(
        horodatage=horodatage or _horodatage_actuel(),
        run_id=run_id,
        etat=etat,
        acteur=acteur,
        etat_atteint=etat_atteint,
        detail=detail or {},
    )


def lire_entrees(racine_runs: Path, run_id: str) -> list[EntreeJournal]:
    """Reads every entry of the run, in write order. Returns an empty list
    if the file does not exist yet (run never started)."""
    chemin = racine_runs / _chemin_relatif(run_id)
    if not chemin.is_file():
        return []
    entrees = []
    with chemin.open("r", encoding="utf-8") as f:
        for ligne in f:
            ligne = ligne.strip()
            if ligne:
                entrees.append(EntreeJournal.depuis_dict(json.loads(ligne)))
    return entrees


def ecrire_entree(racine_runs: Path, entree: EntreeJournal) -> None:
    """Appends a line to the run's file. Never opens in overwrite mode:
    append mode (``a``) guarantees that already-written lines stay intact."""
    chemin = racine_runs / _chemin_relatif(entree.run_id)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    with chemin.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entree.to_dict(), ensure_ascii=False, sort_keys=True))
        f.write("\n")


def dernier_etat_confirme(entrees: list[EntreeJournal]) -> str | None:
    """The resumption point: the state to restart from. ``None`` if the run
    never started. If the last entry is ``FAILED``, it's ``etat_atteint``
    that is authoritative, never ``FAILED`` itself — ``FAILED`` is an
    outcome, not a position in the sequence."""
    if not entrees:
        return None
    derniere = entrees[-1]
    return derniere.etat_atteint if derniere.etat == "FAILED" else derniere.etat


class MigrationDejaReussie(Exception):
    """The run already reached ``DONE``: replaying it would overwrite an
    already-logged success and could re-import history already in place."""


def verifier_pas_deja_reussie(entrees: list[EntreeJournal], run_id: str) -> None:
    """To be called before starting a new run for ``run_id``. Blocks
    neither a run that never started, nor an interrupted or in-progress
    run: only an already-recorded success (``DONE``) is grounds for
    refusal, in line with "one request, one file, once" (prompt §8,
    "replay" threat)."""
    if entrees and entrees[-1].etat == "DONE":
        raise MigrationDejaReussie(
            f"run '{run_id}' already completed successfully on {entrees[-1].horodatage}: replaying it is "
            "refused."
        )


def enregistrer_transition(
    racine_runs: Path,
    run_id: str,
    etat: str,
    acteur: str,
    etat_atteint: str | None = None,
    detail: dict[str, Any] | None = None,
) -> EntreeJournal:
    """Validates the transition against the last confirmed state, then
    writes it. This is the only correct way to add an entry: it prevents
    an inconsistent journal by construction (skipped state, transition
    from a terminal state)."""
    entrees = lire_entrees(racine_runs, run_id)
    etat_actuel = dernier_etat_confirme(entrees)
    if etat_actuel is None:
        if etat != "RECEIVED":
            raise TransitionInterdite(
                f"run '{run_id}' has no entry yet: the first "
                f"transition must be 'RECEIVED', not '{etat}'."
            )
    elif not est_transition_autorisee(etat_actuel, etat):
        raise TransitionInterdite(
            f"transition refused for run '{run_id}': '{etat_actuel}' -> '{etat}' is not "
            "an authorized transition from the confirmed state."
        )
    entree = construire_entree(
        run_id=run_id, etat=etat, acteur=acteur,
        etat_atteint=etat_atteint, detail=detail,
    )
    ecrire_entree(racine_runs, entree)
    return entree


# --- Publishing to the sonar-migration-runs repository -----------------

def committer_journal(racine_runs: Path, message_commit: str) -> bool:
    """Commits the changes under ``journal/`` of the ``racine_runs``
    repository.

    Uses ``git`` as a subprocess with argument lists (never ``shell=True``,
    never string interpolation into a command): this is not a shell
    workaround for an API call, it is the only way to drive a git
    repository — ``migration.journal`` remains the sole source of the
    decision (what to write, when), git is only the persistence medium.

    Returns ``False`` without committing anything if there is no change
    (the normal case for a rerun job that had nothing new to log).
    """
    statut = subprocess.run(
        ["git", "-C", str(racine_runs), "status", "--porcelain", _RACINE_JOURNAL],
        capture_output=True, text=True, check=True,
    )
    if not statut.stdout.strip():
        return False
    subprocess.run(
        ["git", "-C", str(racine_runs), "add", _RACINE_JOURNAL], check=True
    )
    subprocess.run(
        ["git", "-C", str(racine_runs), "commit", "-m", message_commit], check=True
    )
    return True


def pousser_journal(racine_runs: Path) -> None:
    """Pushes local commits to the remote repository. Kept separate from
    ``committer_journal`` to stay testable without a real remote."""
    subprocess.run(["git", "-C", str(racine_runs), "push"], check=True)


def publier_journal(racine_runs: Path, message_commit: str) -> bool:
    """Commits then pushes. Returns ``False`` without pushing anything if
    there was nothing to commit."""
    if committer_journal(racine_runs, message_commit):
        pousser_journal(racine_runs)
        return True
    return False
