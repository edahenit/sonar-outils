"""Operational metrics (batch 5, § "what must be exposed").

Only reads journals already written (``journal.lire_entrees``, a format
already tested): this module has no network or git dependency, it can run
locally against a clone of ``sonar-migration-runs``, or periodically in a
dedicated job (out of scope for this batch: the computation job itself is
the central team's observability tooling, not part of this pipeline).
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .journal import EntreeJournal, lire_entrees
from .machine_etats import ETATS_TERMINAUX


def _horodatage(valeur: str) -> datetime:
    return datetime.strptime(valeur, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Metriques:
    """Snapshot computed at a point in time over all runs present in the
    runs repository. Each run counts in EXACTLY one category."""

    nombre_runs: int
    nombre_reussis: int  # last entry = DONE
    nombre_rejetes_habilitation: int  # last entry = AUTHZ_REJECTED
    nombre_echoues: int  # last entry = FAILED (after AUTHZ_PASSED)
    nombre_en_cours: int  # neither terminal nor FAILED: somewhere in the sequence
    taux_echec: float  # (rejected + failed) / runs, 0.0 if no run
    duree_moyenne_secondes: float | None  # successful runs only
    duree_mediane_secondes: float | None
    causes_rejet_habilitation: dict[str, int] = field(default_factory=dict)  # refusal code -> occurrences
    etats_echec: dict[str, int] = field(default_factory=dict)  # etat_atteint -> occurrences


def _duree_secondes(entrees: list[EntreeJournal]) -> float:
    return (_horodatage(entrees[-1].horodatage) - _horodatage(entrees[0].horodatage)).total_seconds()


def _tous_les_run_ids(racine_runs: Path) -> list[str]:
    racine_journal = racine_runs / "journal"
    if not racine_journal.is_dir():
        return []
    return sorted(
        "/".join(chemin.relative_to(racine_journal).with_suffix("").parts)
        for chemin in racine_journal.glob("*/*.jsonl")
    )


def calculer_metriques(racine_runs: Path) -> Metriques:
    """Walks every ``journal/*/*.jsonl`` file and aggregates statistics. A
    run with no entry at all does not exist for this computation (no empty
    file is ever produced by ``journal.ecrire_entree``)."""
    reussis = echoues = rejetes = en_cours = 0
    durees: list[float] = []
    causes_rejet: Counter = Counter()
    etats_echec: Counter = Counter()

    for run_id in _tous_les_run_ids(racine_runs):
        entrees = lire_entrees(racine_runs, run_id)
        if not entrees:  # pragma: no cover - safety net, should not happen
            continue
        derniere = entrees[-1]

        if derniere.etat == "DONE":
            reussis += 1
            durees.append(_duree_secondes(entrees))
        elif derniere.etat == "AUTHZ_REJECTED":
            rejetes += 1
            for refus in derniere.detail.get("refus", []):
                causes_rejet[refus["code"]] += 1
        elif derniere.etat == "FAILED":
            echoues += 1
            etats_echec[derniere.etat_atteint or "INCONNU"] += 1
        elif derniere.etat not in ETATS_TERMINAUX:
            en_cours += 1

    total = reussis + rejetes + echoues + en_cours
    total_conclus = reussis + rejetes + echoues
    taux_echec = (rejetes + echoues) / total_conclus if total_conclus else 0.0

    return Metriques(
        nombre_runs=total,
        nombre_reussis=reussis,
        nombre_rejetes_habilitation=rejetes,
        nombre_echoues=echoues,
        nombre_en_cours=en_cours,
        taux_echec=taux_echec,
        duree_moyenne_secondes=statistics.mean(durees) if durees else None,
        duree_mediane_secondes=statistics.median(durees) if durees else None,
        causes_rejet_habilitation=dict(causes_rejet),
        etats_echec=dict(etats_echec),
    )


def _duree_lisible(secondes: float | None) -> str:
    if secondes is None:
        return "n/a"
    minutes, sec = divmod(int(secondes), 60)
    return f"{minutes}min{sec:02d}s"


def rendre_metriques_markdown(m: Metriques) -> str:
    """Markdown rendering, intended for a dashboard or a periodic report
    (the distribution channel is out of scope for this batch — see runbook,
    § metrics)."""
    lignes = [
        "## Migration metrics",
        "",
        (
            f"- Runs counted: **{m.nombre_runs}** "
            f"({m.nombre_reussis} successful, {m.nombre_rejetes_habilitation} refused at "
            f"authorization, {m.nombre_echoues} interrupted, {m.nombre_en_cours} in progress)"
        ),
        f"- Failure rate (over concluded runs): **{m.taux_echec * 100:.0f} %**",
        f"- Average duration (successful runs): {_duree_lisible(m.duree_moyenne_secondes)}",
        f"- Median duration (successful runs): {_duree_lisible(m.duree_mediane_secondes)}",
    ]
    if m.causes_rejet_habilitation:
        lignes.append("")
        lignes.append("### Causes of authorization refusal")
        for code, n in sorted(m.causes_rejet_habilitation.items(), key=lambda kv: -kv[1]):
            lignes.append(f"- `{code}`: {n}")
    if m.etats_echec:
        lignes.append("")
        lignes.append("### States reached before interruption")
        for etat, n in sorted(m.etats_echec.items(), key=lambda kv: -kv[1]):
            lignes.append(f"- `{etat}`: {n}")
    return "\n".join(lignes)
