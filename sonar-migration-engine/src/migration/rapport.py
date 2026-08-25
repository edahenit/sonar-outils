"""Rendering of merge request comments, in English, in Markdown.

Two distinct moments (prompt §4 and §13):

* right after the merge, the authorization check verdict
  (``rendre_commentaire_habilitation``) — no side effect, within minutes;
* after execution (successful or interrupted), the final report
  (``rendre_commentaire_final``) — outcome, duration, gaps, what remains for
  the project team to do.

This module makes no network call: it turns already-computed objects
(``DecisionHabilitation``, ``EntreeJournal``) into text. Publishing itself is
the role of ``notification.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .habilitation import DecisionHabilitation, PreuveAdmin
from .journal import EntreeJournal
from .modele import Demande


def _horodatage_lisible(valeur: str) -> datetime:
    return datetime.strptime(valeur, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _duree_lisible(entrees: list[EntreeJournal]) -> str:
    if len(entrees) < 2:
        return "duration unknown"
    debut = _horodatage_lisible(entrees[0].horodatage)
    fin = _horodatage_lisible(entrees[-1].horodatage)
    secondes = int((fin - debut).total_seconds())
    minutes, sec = divmod(secondes, 60)
    heures, minutes = divmod(minutes, 60)
    if heures:
        return f"{heures}h{minutes:02d}min{sec:02d}s ({secondes} s)"
    return f"{minutes}min{sec:02d}s ({secondes} s)"


def _ligne_preuve(preuve: PreuveAdmin) -> str:
    cote_lisible = "source" if preuve.cote == "source" else "target"
    if preuve.ok:
        voie = "directly" if preuve.voie == "DIRECTE" else f"via group `{preuve.groupe}`"
        return f"- **{cote_lisible}** side (`{preuve.projet_cle}` on `{preuve.instance_id}`): ✅ admin {voie} (local account `{preuve.login}`)."
    return "- **{}** side (`{}` on `{}`): ❌ {}".format(
        cote_lisible, preuve.projet_cle, preuve.instance_id,
        preuve.refus.message if preuve.refus else "refused",
    )


def rendre_commentaire_habilitation(demande: Demande, decision: DecisionHabilitation) -> str:
    """Verdict of the authorization check, posted right after the merge.

    Always renders the state of BOTH sides, even when only one failed: the
    requester must know which one to fix, never just "refused" (prompt §5).
    """
    lignes = []
    if decision.ok:
        lignes.append(
            "## ✅ Authorization check passed\n\n"
            f"Migration from `{demande.cle_source}` to `{demande.cle_cible}` — you are indeed an "
            "administrator on both sides."
        )
    else:
        lignes.append(
            "## ❌ Authorization check refused\n\n"
            "No action has been taken on any instance. Fix the issue "
            "described below, then submit a new request."
        )

    lignes.append("")
    lignes.append(_ligne_preuve(decision.preuve_source))
    lignes.append(_ligne_preuve(decision.preuve_cible))

    if not decision.ok:
        lignes.append("")
        lignes.append("### What to do")
        for r in decision.refus:
            lignes.append(f"- **{r.code}**: {r.action}")

    if decision.anomalies:
        lignes.append("")
        lignes.append(
            "### Flagged for the central team (does not block this request "
            "as long as a legitimate authorization path remains)"
        )
        for a in decision.anomalies:
            lignes.append(f"- **{a.code}**: {a.message}")

    if demande.ticket:
        lignes.append("")
        lignes.append(f"Associated ticket: {demande.ticket}")

    return "\n".join(lignes)


def rendre_commentaire_final(demande: Demande, entrees: list[EntreeJournal]) -> str:
    """Final report, posted after execution (successful or interrupted).

    Content mandated by the prompt (§4, step 13): outcome, duration,
    observed gaps, actions remaining for the project team.
    """
    derniere = entrees[-1] if entrees else None
    duree = _duree_lisible(entrees)

    if derniere is None:
        return (
            f"## Migration report `{demande.cle_source}` → `{demande.cle_cible}`\n\n"
            "No execution recorded."
        )

    if derniere.etat == "DONE":
        lignes = [
            f"## ✅ Migration complete: `{demande.cle_source}` → `{demande.cle_cible}`",
            "",
            f"Total duration: {duree}.",
            "",
            "### Remaining on your side",
            (
                "- Point your analyses (`sonar-project.properties`, your "
                "projects' CI configuration) to the new key "
                f"`{demande.cle_cible}` — the migration does not modify your code repositories."
            ),
        ]
    else:
        etat_atteint = derniere.etat_atteint if derniere.etat == "FAILED" else derniere.etat
        lignes = [
            f"## ⚠️ Migration interrupted: `{demande.cle_source}` → `{demande.cle_cible}`",
            "",
            f"Last confirmed state: **{etat_atteint}**. Duration before interruption: {duree}.",
            "",
            (
                "Central team intervention is required before any resumption. "
                "No action is needed on your part for now."
            ),
        ]

    if demande.ticket:
        lignes.append("")
        lignes.append(f"Associated ticket: {demande.ticket}")

    return "\n".join(lignes)
