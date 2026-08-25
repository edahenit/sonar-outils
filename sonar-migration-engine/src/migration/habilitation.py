"""Authorization check — the security core of the solution (prompt §5).

Checks that the requester is an administrator of the source project on the
source instance AND an administrator of the target project on the central
instance. The "AND" is the only safeguard against importing the history of
a project that does not belong to the requester into their own space: both
checks are therefore always run in full, never short-circuited by one
another.

This module makes no assumption about how admin rights are held: directly,
or through a group (the nominal case on the central instance, where it's
the ``sonar-<space_id>-managers`` group created by the DevOps portal that
carries the permission — see ``modele.Inventaire.groupes_interdits`` for
groups whose holding never counts as authorization).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .messages import refus as fabriquer_refus
from .modele import Demande, Inventaire, Refus
from .sonar_client import ClientSonar

Cote = str  # "source" | "cible"


@dataclass(frozen=True)
class PreuveAdmin:
    """Result of the check on ONE instance, for ONE project.

    Serializable as-is into the audit journal: never contains a secret,
    only project identifiers, logins, and group names.
    """

    ok: bool
    cote: Cote
    instance_id: str
    projet_cle: str
    projet_id: str | None
    voie: str | None  # "DIRECTE" | "GROUPE" | None
    login: str | None
    groupe: str | None
    groupes_examines: tuple[str, ...] = ()
    anomalies: tuple[Refus, ...] = ()
    refus: Refus | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "cote": self.cote,
            "instance_id": self.instance_id,
            "projet_cle": self.projet_cle,
            "projet_id": self.projet_id,
            "voie": self.voie,
            "login": self.login,
            "groupe": self.groupe,
            "groupes_examines": list(self.groupes_examines),
            "anomalies": [a.to_dict() for a in self.anomalies],
            "refus": self.refus.to_dict() if self.refus else None,
        }


@dataclass(frozen=True)
class DecisionHabilitation:
    """Overall decision, aggregating both sides and the key collision
    check. ``ok`` is true only if everything is true."""

    ok: bool
    preuve_source: PreuveAdmin
    preuve_cible: PreuveAdmin
    refus: tuple[Refus, ...] = ()
    anomalies: tuple[Refus, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "preuve_source": self.preuve_source.to_dict(),
            "preuve_cible": self.preuve_cible.to_dict(),
            "refus": [r.to_dict() for r in self.refus],
            "anomalies": [a.to_dict() for a in self.anomalies],
        }


def _preuve_refusee(
    cote: Cote,
    instance_id: str,
    projet_cle: str,
    code: str,
    projet_id: str | None = None,
    login: str | None = None,
    groupes_examines: tuple[str, ...] = (),
    anomalies: tuple[Refus, ...] = (),
    **valeurs_message: object,
) -> PreuveAdmin:
    return PreuveAdmin(
        ok=False,
        cote=cote,
        instance_id=instance_id,
        projet_cle=projet_cle,
        projet_id=projet_id,
        voie=None,
        login=login,
        groupe=None,
        groupes_examines=groupes_examines,
        anomalies=anomalies,
        refus=fabriquer_refus(
            code, instance_id=instance_id, cle=projet_cle, login=login,
            **valeurs_message
        ),
    )


def est_admin(
    client: ClientSonar,
    cle_projet: str,
    uid: str,
    inventaire: Inventaire,
    cote: Cote,
) -> PreuveAdmin:
    """Checks that the holder of ``uid`` is an administrator of
    ``cle_projet`` on ``client``'s instance. Never presupposes the outcome:
    each step can refuse, and the first failing step determines the reason
    returned.
    """
    instance = client.instance
    libelle = instance.libelle

    projet = client.rechercher_projet(cle_projet)
    if projet is None:
        code = "PROJET_CIBLE_INCONNU" if cote == "cible" else "PROJET_SOURCE_INCONNU"
        return _preuve_refusee(cote, instance.id, cle_projet, code, libelle=libelle)

    # Target side only, special case: a project already analyzed must never
    # receive an import, regardless of the requester's authorization.
    if cote == "cible" and projet.derniere_analyse is not None:
        return _preuve_refusee(
            cote, instance.id, cle_projet, "PROJET_CIBLE_DEJA_ANALYSE",
            projet_id=projet.id, libelle=libelle,
        )

    resolution = client.resoudre_login_par_uid(uid)
    if resolution.doublon:
        return _preuve_refusee(
            cote, instance.id, cle_projet, "DOUBLON_ANNUAIRE",
            projet_id=projet.id, libelle=libelle,
            uid=uid, nombre=len(resolution.logins),
            logins=", ".join(sorted(resolution.logins)),
            alerte=True,
        )
    if not resolution.trouve:
        return _preuve_refusee(
            cote, instance.id, cle_projet, "COMPTE_INCONNU_INSTANCE",
            projet_id=projet.id, libelle=libelle,
        )
    login = resolution.login

    # Direct path.
    directs = client.permissions_admin_utilisateurs(cle_projet)
    if login in set(directs):
        return PreuveAdmin(
            ok=True, cote=cote, instance_id=instance.id, projet_cle=cle_projet,
            projet_id=projet.id, voie="DIRECTE", login=login, groupe=None,
        )

    groupes_bruts = tuple(client.permissions_admin_groupes(cle_projet))

    # Target side only, special case: no group at all signals a missed
    # portal-side provisioning, not a requester membership defect — the
    # central team must be alerted, not just have the requester refused.
    if cote == "cible" and len(groupes_bruts) == 0:
        return _preuve_refusee(
            cote, instance.id, cle_projet, "PROJET_CIBLE_SANS_GROUPE_ADMIN",
            projet_id=projet.id, login=login, libelle=libelle, alerte=True,
        )

    anomalies = []
    for nom_groupe in groupes_bruts:
        if inventaire.est_groupe_interdit(nom_groupe):
            # Flagged independently of the final verdict: even if another
            # legitimate group validates the request, an admin permission
            # held by an overly broad group is a configuration anomaly to
            # be fixed on the entity's side.
            anomalies.append(fabriquer_refus(
                "GROUPE_TROP_LARGE", instance_id=instance.id, libelle=libelle,
                cle=cle_projet, groupe=nom_groupe, alerte=True,
            ))
            continue
        membres = set(client.membres_groupe(nom_groupe))
        if login in membres:
            return PreuveAdmin(
                ok=True, cote=cote, instance_id=instance.id, projet_cle=cle_projet,
                projet_id=projet.id, voie="GROUPE", login=login, groupe=nom_groupe,
                groupes_examines=groupes_bruts, anomalies=tuple(anomalies),
            )

    return _preuve_refusee(
        cote, instance.id, cle_projet, "PAS_ADMIN",
        projet_id=projet.id, login=login, libelle=libelle,
        groupes_examines=groupes_bruts, anomalies=tuple(anomalies),
    )


def controler_habilitation(
    demande: Demande,
    uid: str,
    client_source: ClientSonar,
    client_cible: ClientSonar,
    inventaire: Inventaire,
) -> DecisionHabilitation:
    """Entry point of batch 2: runs both checks (source, target) in full,
    plus the key collision check, and aggregates everything into a single
    decision.

    Both calls to ``est_admin`` are always made, even if the first one
    fails: the report must let the requester know which of the two checks
    was missing, never just "refused".
    """
    preuve_source = est_admin(client_source, demande.cle_source, uid, inventaire, cote="source")
    preuve_cible = est_admin(client_cible, demande.cle_cible, uid, inventaire, cote="cible")

    refus_liste = []
    if preuve_source.refus is not None:
        refus_liste.append(preuve_source.refus)
    if preuve_cible.refus is not None:
        refus_liste.append(preuve_cible.refus)

    # Collision: the target project may well be correctly provisioned
    # (concern above) while the SOURCE KEY already matches an existing
    # project on the central instance — a collision with another entity,
    # independent of the target project's state.
    collision = client_cible.rechercher_projet(demande.cle_source)
    if collision is not None:
        refus_liste.append(fabriquer_refus(
            "CLE_SOURCE_COLLISION_CENTRALE", cle_source=demande.cle_source,
        ))

    anomalies = preuve_source.anomalies + preuve_cible.anomalies
    ok = preuve_source.ok and preuve_cible.ok and collision is None

    return DecisionHabilitation(
        ok=ok,
        preuve_source=preuve_source,
        preuve_cible=preuve_cible,
        refus=tuple(refus_liste),
        anomalies=anomalies,
    )
