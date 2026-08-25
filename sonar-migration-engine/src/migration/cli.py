"""Command-line interface for the control plane.

Six commands, in the order the pipeline invokes them (see
``ci/pipeline.yml``):

* ``valider-commit`` — schema + consistency, with no token at all (stage
  ``valider``).
* ``habiliter`` — identity resolution + authorization check, journal, MR
  comment (stage ``habiliter``, needs the tokens).
* ``lancer-gabarit`` — launches an AWX job template for one migration step
  and waits for its result (used by the ``preflight`` and ``executer``
  stages: the GitLab runners have no network path to the SonarQube hosts,
  only the AWX controller does — see ``awx_client.py``).
* ``enregistrer`` — adds a generic transition to the journal (used by the
  ``preflight`` and ``executer`` stages after a successful AWX job).
* ``metriques`` — computes and prints operational metrics (batch 5).
* ``rapport-final`` — renders and publishes the final execution report.

Each ``commande_*`` function takes its external dependencies as parameters
(HTTP clients, git-publishing function) rather than building them itself:
this is what makes them testable without network or a remote repository,
and it's ``main()`` that wires them to the real implementations from the
environment variables supplied by GitLab CI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .awx_client import ClientAWX, DelaiJobAwxDepasse
from .decouverte import chemins_modifies_par_commit, fichier_demande_du_commit
from .gitlab_client import ClientGitLab
from .habilitation import controler_habilitation
from .inventaire import ErreurInventaire, Inventaire, charger_inventaire
from .journal import (
    MigrationDejaReussie,
    enregistrer_transition,
    lire_entrees,
    publier_journal,
    verifier_pas_deja_reussie,
)
from .machine_etats import TransitionInterdite
from .metriques import calculer_metriques, rendre_metriques_markdown
from .modele import Demande, Instance, Refus
from .notification import ClientNotificationGitLab
from .rapport import rendre_commentaire_final, rendre_commentaire_habilitation
from .sonar_client import ClientSonar
from .validation import PREFIXE_DEMANDES, valider_fichier

_RACINE_ENGINE = Path(__file__).resolve().parents[2]
_INVENTAIRE_DEFAUT = _RACINE_ENGINE / "inventaire" / "instances.yml"


def _chemin_relatif_demande(chemin: Path) -> str:
    """Reconstructs the ``requests/<instance>/<file>.yml`` path expected by
    ``valider_chemin``, from any path passed in as an argument.
    """
    parties = chemin.resolve().parts
    if PREFIXE_DEMANDES in parties:
        indice = len(parties) - 1 - parties[::-1].index(PREFIXE_DEMANDES)
        return "/".join(parties[indice:])
    return str(chemin)


def _rendre_refus(refus_liste: list[Refus]) -> str:
    lignes = []
    for r in refus_liste:
        prefixe = "🔔 " if r.alerte else "❌ "
        lignes.append(f"{prefixe}**{r.code}** — {r.message}")
        lignes.append(f"   → {r.action}")
    return "\n".join(lignes)


# --- offline validation (batch 1's original command) ------------------------


def commande_valider(chemins: list[str], chemin_inventaire: Path) -> int:
    try:
        inventaire = charger_inventaire(chemin_inventaire)
    except ErreurInventaire as exc:
        print(f"Invalid inventory, contact the central team: {exc}", file=sys.stderr)
        return 3

    demandes_acceptees: list[Demande] = []
    resultats = []
    code_sortie = 0

    for chemin_str in chemins:
        chemin = Path(chemin_str)
        chemin_relatif = _chemin_relatif_demande(chemin)
        demande, refus_liste = valider_fichier(
            chemin_absolu=chemin, chemin_relatif=chemin_relatif,
            inventaire=inventaire, autres_demandes=demandes_acceptees,
        )
        if demande is not None:
            demandes_acceptees.append(demande)
        resultats.append((chemin_relatif, demande, refus_liste))

    for chemin_relatif, demande, refus_liste in resultats:
        print(f"## {chemin_relatif}")
        if demande is not None:
            print(f"✅ Valid request: {demande.cle_source} → {demande.cle_cible} (instance {demande.instance_source})")
        else:
            code_sortie = 2
            print(_rendre_refus(refus_liste))
        print()

    return code_sortie


# --- charger_autres_demandes: for duplicate detection -----------------------


def charger_autres_demandes(
    depot_demandes: Path, fichier_exclu_relatif: str, inventaire: Inventaire
) -> list[Demande]:
    """Loads the other requests already present in the repository, for
    duplicate detection (``validation.valider_unicite``).

    Best-effort: another file that is already invalid (should not happen,
    since every merge was validated before being accepted) is ignored
    rather than failing THIS request — it is not this execution's job to
    fix the repository's history.
    """
    racine_requests = depot_demandes / PREFIXE_DEMANDES
    if not racine_requests.is_dir():
        return []
    autres: list[Demande] = []
    for chemin in sorted(racine_requests.glob("*/*.yml")):
        chemin_relatif = "/".join(chemin.relative_to(depot_demandes).parts)
        if chemin_relatif == fichier_exclu_relatif:
            continue
        demande, _refus_liste = valider_fichier(
            chemin_absolu=chemin, chemin_relatif=chemin_relatif, inventaire=inventaire,
        )
        if demande is not None:
            autres.append(demande)
    return autres


# --- 'valider' stage: schema + consistency, no token -----------------------


def commande_valider_commit(
    depot_demandes: Path, commit_sha: str, inventaire: Inventaire
) -> int:
    """Entry point of the ``valider`` job: no token, no network call.

    A failure here is visible in the GitLab CI job's own status — no need
    for an MR comment, and certainly no need for the token that would be
    required to post one (see root README, threat table: "the jobs that
    process the requester's input run before and without the tokens").
    """
    chemins = chemins_modifies_par_commit(depot_demandes, commit_sha)
    fichier_relatif = fichier_demande_du_commit(chemins)
    autres = charger_autres_demandes(depot_demandes, fichier_relatif, inventaire)
    demande, refus_liste = valider_fichier(
        chemin_absolu=depot_demandes / fichier_relatif,
        chemin_relatif=fichier_relatif,
        inventaire=inventaire,
        autres_demandes=autres,
    )
    if demande is None:
        print(_rendre_refus(refus_liste), file=sys.stderr)
        return 2
    print(f"✅ {fichier_relatif}: valid request.")
    return 0


# --- 'habiliter' stage: identity + authorization check ---------------------


def commande_habiliter(
    depot_demandes: Path,
    commit_sha: str,
    depot_runs: Path,
    inventaire: Inventaire,
    client_gitlab: ClientGitLab,
    client_notification: ClientNotificationGitLab,
    projet_gitlab_id: int,
    fabriquer_client_sonar: Callable[[Instance], ClientSonar],
    committer: Callable[[Path, str], bool] = publier_journal,
    acteur: str = "pipeline",
    fichier_etat: Path | None = None,
) -> int:
    """Entry point of the ``habiliter`` job.

    Revalidates the request (fast, no side effect) rather than trusting an
    artifact from the previous job: the source of truth is always the
    commit's content, never a state cached between two jobs of the same
    pipeline.

    Exit codes: 0 (authorized), 1 (refused), 2 (invalid request — should
    not happen here), 4 (replay of an already-successful run).
    """
    chemins = chemins_modifies_par_commit(depot_demandes, commit_sha)
    fichier_relatif = fichier_demande_du_commit(chemins)
    autres = charger_autres_demandes(depot_demandes, fichier_relatif, inventaire)
    demande, refus_liste = valider_fichier(
        chemin_absolu=depot_demandes / fichier_relatif,
        chemin_relatif=fichier_relatif,
        inventaire=inventaire,
        autres_demandes=autres,
    )
    if demande is None:
        print(_rendre_refus(refus_liste), file=sys.stderr)
        return 2

    run_id = demande.identifiant
    try:
        verifier_pas_deja_reussie(lire_entrees(depot_runs, run_id), run_id)
    except MigrationDejaReussie as exc:
        print(str(exc), file=sys.stderr)
        return 4

    # Identity re-read from the GitLab server — never a job variable.
    demandeur = client_gitlab.resoudre_demandeur(projet_gitlab_id, commit_sha)

    enregistrer_transition(
        depot_runs, run_id, "RECEIVED", acteur=acteur,
        detail={"fichier": fichier_relatif, "auteur_gitlab": demandeur.login_gitlab},
    )

    instance_source = inventaire.source(demande.instance_source)
    client_source = fabriquer_client_sonar(instance_source)
    client_cible = fabriquer_client_sonar(inventaire.centrale)

    decision = controler_habilitation(
        demande, demandeur.extern_uid, client_source, client_cible, inventaire
    )

    etat = "AUTHZ_PASSED" if decision.ok else "AUTHZ_REJECTED"
    enregistrer_transition(depot_runs, run_id, etat, acteur=acteur, detail=decision.to_dict())
    committer(depot_runs, f"journal: {etat} {run_id}")

    commentaire = rendre_commentaire_habilitation(demande, decision)
    client_notification.publier_commentaire_mr(projet_gitlab_id, demandeur.mr_iid, commentaire)

    if decision.ok and fichier_etat is not None:
        # State propagation between the two moments (see root README): a
        # dotenv file, consumed by GitLab CI (artifacts: reports: dotenv)
        # for the following jobs of the SAME pipeline. Never written on
        # refusal — there is then nothing to resume, and its presence
        # could suggest otherwise. The source of truth remains the journal
        # (already committed above): this file is only a caching
        # convenience, with no consequence if it expires before the manual
        # job runs.
        fichier_etat.write_text(
            f"RUN_ID={run_id}\n"
            f"DEMANDE_FICHIER={fichier_relatif}\n"
            f"MR_IID={demandeur.mr_iid}\n"
            f"SONAR_PROJET_CIBLE_ID={decision.preuve_cible.projet_id}\n"
            f"SONAR_CLE_SOURCE={demande.cle_source}\n"
            f"SONAR_CLE_CIBLE={demande.cle_cible}\n"
            f"SONAR_SOURCE_HOST={demande.instance_source}\n",
            encoding="utf-8",
        )

    return 0 if decision.ok else 1


# --- 'preflight' / 'executer' stages: AWX job launch + generic transitions --


def _parser_extra_vars(brut: str) -> dict[str, str]:
    """Parses the same ``key=value key2=value2`` format ``ansible-playbook
    --extra-vars`` accepts, so ``ci/pipeline.yml``'s ``EXTRA_VARS``
    construction did not need to change shape when the pipeline switched
    from running Ansible locally to launching it through AWX."""
    resultat: dict[str, str] = {}
    for paire in brut.split():
        cle, _, valeur = paire.partition("=")
        resultat[cle] = valeur
    return resultat


def commande_lancer_gabarit(
    client_awx: ClientAWX,
    nom_gabarit: str,
    extra_vars: dict[str, Any],
    timeout_secondes: float = 1800.0,
) -> int:
    """Launches the AWX job template named ``nom_gabarit`` (matching an
    Ansible tag from ``ansible/site.yml``) and waits for its result.

    Exit codes: 0 (job successful), 1 (job failed/errored/canceled on the
    AWX side — a normal, not exceptional, outcome), 5 (still non-terminal
    after ``timeout_secondes``, see ``DelaiJobAwxDepasse``). Mirrors the
    nonzero exit code a failing local ``ansible-playbook`` call used to
    produce: the caller (the pipeline's shell script) reacts the same way
    either way.
    """
    job_id = client_awx.lancer(nom_gabarit, extra_vars)
    print(f"AWX job {job_id} launched for '{nom_gabarit}'.")
    try:
        resultat = client_awx.attendre(job_id, timeout_secondes=timeout_secondes)
    except DelaiJobAwxDepasse as exc:
        print(str(exc), file=sys.stderr)
        return 5
    print(f"AWX job {job_id} ({nom_gabarit}): {resultat.statut} — {resultat.url_ihm}")
    return 0 if resultat.succes else 1


# --- 'preflight' / 'executer' stages: generic transitions -------------------


def commande_enregistrer(
    depot_runs: Path,
    run_id: str,
    etat: str,
    acteur: str,
    etat_atteint: str | None,
    detail: dict[str, Any],
    committer: Callable[[Path, str], bool] = publier_journal,
) -> int:
    """Generic entry point used by the ``preflight`` and ``executer``
    stages (Ansible produces the facts, this command decides and records —
    see root README, § GitLab CI / Ansible split).
    """
    try:
        enregistrer_transition(
            depot_runs, run_id, etat, acteur=acteur,
            etat_atteint=etat_atteint, detail=detail,
        )
    except (TransitionInterdite, ValueError) as exc:
        print(f"Transition refused: {exc}", file=sys.stderr)
        return 3
    committer(depot_runs, f"journal: {etat} {run_id}")
    return 0


def commande_metriques(depot_runs: Path) -> int:
    """Computes and prints operational metrics (batch 5)."""
    m = calculer_metriques(depot_runs)
    print(rendre_metriques_markdown(m))
    return 0


def commande_rapport_final(
    depot_demandes: Path,
    demande_fichier: str,
    depot_runs: Path,
    run_id: str,
    client_notification: ClientNotificationGitLab,
    projet_gitlab_id: int,
    mr_iid: int,
) -> int:
    """Renders and publishes the final report (prompt step 13)."""
    texte_yaml = (depot_demandes / demande_fichier).read_text(encoding="utf-8")
    import yaml  # local import: this module only needs PyYAML for this command
    donnees = yaml.safe_load(texte_yaml)
    demande = Demande.depuis_dict(donnees, fichier=demande_fichier)

    entrees = lire_entrees(depot_runs, run_id)
    commentaire = rendre_commentaire_final(demande, entrees)
    client_notification.publier_commentaire_mr(projet_gitlab_id, mr_iid, commentaire)
    return 0


# --- main() wiring: environment variables -> real dependencies -------------


def _jeton_environnement(nom_variable: str) -> str:
    valeur = os.environ.get(nom_variable)
    if not valeur:
        raise SystemExit(
            f"Protected variable '{nom_variable}' missing from the environment. "
            "Check the engine repository's CI/CD configuration."
        )
    return valeur


def _fabriquer_client_sonar_reel(instance: Instance) -> ClientSonar:
    return ClientSonar(instance, token=_jeton_environnement(instance.variable_token))


def construire_parseur() -> argparse.ArgumentParser:
    parseur = argparse.ArgumentParser(
        prog="python -m migration.cli",
        description="Control plane for the SonarQube self-service migration.",
    )
    sous = parseur.add_subparsers(dest="commande", required=True)

    p_valider = sous.add_parser("valider", help="Validates one or more request files (local usage).")
    p_valider.add_argument("fichiers", nargs="+")
    p_valider.add_argument("--inventaire", type=Path, default=_INVENTAIRE_DEFAUT)

    p_vc = sous.add_parser("valider-commit", help="'valider' stage: no token.")
    p_vc.add_argument("--depot-demandes", type=Path, required=True)
    p_vc.add_argument("--commit-sha", required=True)
    p_vc.add_argument("--inventaire", type=Path, default=_INVENTAIRE_DEFAUT)

    p_hab = sous.add_parser("habiliter", help="'habiliter' stage: identity + authorization check.")
    p_hab.add_argument("--depot-demandes", type=Path, required=True)
    p_hab.add_argument("--commit-sha", required=True)
    p_hab.add_argument("--depot-runs", type=Path, required=True)
    p_hab.add_argument("--inventaire", type=Path, default=_INVENTAIRE_DEFAUT)
    p_hab.add_argument("--gitlab-base-url", required=True)
    p_hab.add_argument("--projet-gitlab-id", type=int, required=True)
    p_hab.add_argument("--fichier-etat", type=Path, default=None)

    p_lg = sous.add_parser(
        "lancer-gabarit",
        help="Launches an AWX job template for one migration step and waits for its result.",
    )
    p_lg.add_argument("--gabarit", required=True, help="Job template name (= Ansible tag).")
    p_lg.add_argument("--extra-vars", required=True, help="Same 'key=value ...' format as ansible-playbook.")
    p_lg.add_argument("--timeout-secondes", type=float, default=1800.0)

    p_enr = sous.add_parser("enregistrer", help="Adds a transition to the journal.")
    p_enr.add_argument("--depot-runs", type=Path, required=True)
    p_enr.add_argument("--run-id", required=True)
    p_enr.add_argument("--etat", required=True)
    p_enr.add_argument("--acteur", required=True)
    p_enr.add_argument("--etat-atteint", default=None)
    p_enr.add_argument("--detail-json", default="{}")

    p_met = sous.add_parser("metriques", help="Computes and prints operational metrics.")
    p_met.add_argument("--depot-runs", type=Path, required=True)

    p_rap = sous.add_parser("rapport-final", help="Renders and publishes the final execution report.")
    p_rap.add_argument("--depot-demandes", type=Path, required=True)
    p_rap.add_argument("--demande-fichier", required=True)
    p_rap.add_argument("--depot-runs", type=Path, required=True)
    p_rap.add_argument("--run-id", required=True)
    p_rap.add_argument("--gitlab-base-url", required=True)
    p_rap.add_argument("--projet-gitlab-id", type=int, required=True)
    p_rap.add_argument("--mr-iid", type=int, required=True)

    return parseur


def main(argv: list[str] | None = None) -> int:
    parseur = construire_parseur()
    args = parseur.parse_args(argv)

    if args.commande == "valider":
        return commande_valider(args.fichiers, args.inventaire)

    if args.commande == "valider-commit":
        inventaire = charger_inventaire(args.inventaire)
        return commande_valider_commit(args.depot_demandes, args.commit_sha, inventaire)

    if args.commande == "habiliter":
        inventaire = charger_inventaire(args.inventaire)
        client_gitlab = ClientGitLab(
            base_url=args.gitlab_base_url,
            token=_jeton_environnement("GITLAB_API_TOKEN"),
        )
        client_notification = ClientNotificationGitLab(
            base_url=args.gitlab_base_url, token=_jeton_environnement("GITLAB_API_TOKEN"),
        )
        return commande_habiliter(
            depot_demandes=args.depot_demandes, commit_sha=args.commit_sha,
            depot_runs=args.depot_runs, inventaire=inventaire,
            client_gitlab=client_gitlab, client_notification=client_notification,
            projet_gitlab_id=args.projet_gitlab_id,
            fabriquer_client_sonar=_fabriquer_client_sonar_reel,
            fichier_etat=args.fichier_etat,
        )

    if args.commande == "lancer-gabarit":
        client_awx = ClientAWX(
            base_url=_jeton_environnement("AWX_BASE_URL"),
            token=_jeton_environnement("AWX_API_TOKEN"),
        )
        return commande_lancer_gabarit(
            client_awx, args.gabarit, _parser_extra_vars(args.extra_vars),
            timeout_secondes=args.timeout_secondes,
        )

    if args.commande == "enregistrer":
        return commande_enregistrer(
            depot_runs=args.depot_runs, run_id=args.run_id, etat=args.etat,
            acteur=args.acteur, etat_atteint=args.etat_atteint,
            detail=json.loads(args.detail_json),
        )

    if args.commande == "metriques":
        return commande_metriques(args.depot_runs)

    if args.commande == "rapport-final":
        client_notification = ClientNotificationGitLab(
            base_url=args.gitlab_base_url, token=_jeton_environnement("GITLAB_API_TOKEN"),
        )
        return commande_rapport_final(
            depot_demandes=args.depot_demandes, demande_fichier=args.demande_fichier,
            depot_runs=args.depot_runs, run_id=args.run_id,
            client_notification=client_notification,
            projet_gitlab_id=args.projet_gitlab_id, mr_iid=args.mr_iid,
        )

    parseur.error("unknown command")  # pragma: no cover - unreachable
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
