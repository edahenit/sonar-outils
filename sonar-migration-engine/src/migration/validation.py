"""Validation of a request file: YAML -> schema -> consistency -> uniqueness.

Single entry point: ``valider_fichier``. It never raises an exception for a
malformed request — an exception would mean a bug in this module, not a
requester error. Any issue on the requester's side becomes a ``Refus`` in
the returned list.

This module makes no network call and reads no secret: it is the job that
runs *before* tokens are injected (see prompt §8, "token exfiltration"
threat).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import jsonschema

from .chargement import ErreurYaml, charger_yaml_strict, verifier_taille
from .inventaire import Inventaire
from .messages import extrait, libelle_champ, refus
from .modele import Demande, Refus, slug

_ICI = Path(__file__).parent
_CHEMIN_SCHEMA = _ICI / "schema" / "demande.schema.json"

# Expected root of request paths in the sonar-migration-requests repository.
PREFIXE_DEMANDES = "requests"


def _charger_schema() -> dict[str, Any]:
    with _CHEMIN_SCHEMA.open("r", encoding="utf-8") as f:
        return json.load(f)


def _validateur() -> jsonschema.protocols.Validator:
    schema = _charger_schema()
    Validateur = jsonschema.validators.validator_for(schema)
    Validateur.check_schema(schema)
    return Validateur(schema)


def _champs_acceptes(schema: Mapping[str, Any]) -> str:
    return ", ".join(sorted(schema.get("properties", {}).keys()))


def _refus_depuis_erreur_schema(
    erreur: jsonschema.exceptions.ValidationError,
    schema: Mapping[str, Any],
    fichier: str,
) -> Refus:
    """Translates a ``jsonschema`` library ``ValidationError`` (in English,
    JSON-Schema-keyword-oriented) into a requester-facing ``Refus``.

    ``erreur.message`` is never relayed as-is: the catalogue in
    ``messages.py`` is the only source of text shown to the requester.
    """
    pointeur = "/" + "/".join(str(p) for p in erreur.absolute_path)
    champ = str(erreur.absolute_path[-1]) if erreur.absolute_path else "(root)"
    validateur = erreur.validator

    if validateur == "required":
        # One message per missing field, not one per occurrence: jsonschema
        # reports all missing required fields in the same "required"
        # message; we split them out here to stay actionable.
        manquants = [
            c for c in erreur.validator_value
            if c not in erreur.instance
        ]
        # In practice should contain only one field with recent jsonschema
        # versions, but we cover the case where several are missing at once.
        premier = manquants[0] if manquants else champ
        return refus(
            "SCHEMA_CHAMP_MANQUANT",
            fichier=fichier,
            pointeur=pointeur,
            champ=premier,
            libelle=libelle_champ(premier),
        )

    if validateur == "additionalProperties":
        # jsonschema lists the extra properties in its message; we recover
        # them cleanly by comparing against the declared keys.
        connues = set(schema.get("properties", {}).keys())
        inconnus = sorted(set(erreur.instance.keys()) - connues)
        premier = inconnus[0] if inconnus else "(unknown field)"
        return refus(
            "SCHEMA_CHAMP_INCONNU",
            fichier=fichier,
            pointeur=pointeur,
            champ=premier,
            champs_acceptes=_champs_acceptes(schema),
        )

    if validateur == "type":
        return refus(
            "SCHEMA_TYPE_INVALIDE",
            fichier=fichier,
            pointeur=pointeur,
            champ=champ,
            libelle=libelle_champ(champ),
            attendu=erreur.validator_value,
            valeur=extrait(erreur.instance),
        )

    if validateur == "maxLength":
        return refus(
            "SCHEMA_TROP_LONG",
            fichier=fichier,
            pointeur=pointeur,
            champ=champ,
            libelle=libelle_champ(champ),
            longueur=len(erreur.instance) if isinstance(erreur.instance, str) else 0,
            maximum=erreur.validator_value,
        )

    if validateur in ("pattern", "const", "enum", "minLength"):
        return refus(
            "SCHEMA_VALEUR_INVALIDE",
            fichier=fichier,
            pointeur=pointeur,
            champ=champ,
            libelle=libelle_champ(champ),
            valeur=extrait(erreur.instance),
            attendu=_attendu_lisible(champ, validateur, erreur.validator_value),
            action_specifique=_action_specifique(champ),
        )

    # Safety net: a JSON Schema keyword not anticipated here must never crash
    # validation, but must stay detectable in an audit — hence the generic
    # code with the pointer preserved.
    return refus(
        "SCHEMA_VALEUR_INVALIDE",
        fichier=fichier,
        pointeur=pointeur,
        champ=champ,
        libelle=libelle_champ(champ),
        valeur=extrait(erreur.instance),
        attendu=str(erreur.validator_value),
        action_specifique="Fix the value according to the template provided "
                          "in requests/entite-exemple/.",
    )


def _attendu_lisible(champ: str, validateur: str, valeur_attendue: Any) -> str:
    if champ in ("cle_source", "cle_cible"):
        return "letters, digits and « . _ : - », 1 to 400 characters"
    if champ == "instance_source":
        return "lowercase letters, digits and « - », 2 to 32 characters, " \
               "starting with a letter or a digit"
    if champ == "version":
        return "exactly 1"
    if champ == "ticket":
        return "a ticket code, e.g. DEVOPS-4821"
    if champ == "fenetre_souhaitee":
        return "a date in YYYY-MM-DD format"
    return str(valeur_attendue)


def _action_specifique(champ: str) -> str:
    if champ in ("cle_source", "cle_cible"):
        return "Copy the key exactly as it is displayed in SonarQube."
    if champ == "instance_source":
        return "Use the identifier published in docs/instances-disponibles.md."
    if champ == "version":
        return "Use the template provided in requests/entite-exemple/ " \
               "without modifying the « version: 1 » line."
    if champ == "fenetre_souhaitee":
        return "Use the YYYY-MM-DD format, e.g. 2026-09-15."
    return "Fix the value according to the template provided."


def valider_schema(donnees: Mapping[str, Any], fichier: str) -> list[Refus]:
    """Validates ``donnees`` against the request schema. Assumes nothing
    about the shape of ``donnees``: a YAML document whose root is not an
    object has already been refused by ``chargement.py`` before reaching
    here."""
    validateur = _validateur()
    schema = _charger_schema()
    erreurs = sorted(validateur.iter_errors(donnees), key=lambda e: list(e.absolute_path))
    return [_refus_depuis_erreur_schema(e, schema, fichier) for e in erreurs]


def valider_chemin(chemin_relatif: str, instance_source: str, cle_cible: str) -> list[Refus]:
    """Checks that the file is where its content says it should be.

    This is a defense against inconsistency, not against an attack: the
    file's content is authoritative for business logic, but an
    inconsistent path is almost always a manipulation error (copy-pasted
    from another folder) that is better flagged right away than left to
    slip through as a misfiled request.
    """
    refus_liste: list[Refus] = []
    parties = Path(chemin_relatif).parts

    if len(parties) < 3 or parties[0] != PREFIXE_DEMANDES:
        return [refus(
            "FICHIER_EMPLACEMENT",
            fichier=chemin_relatif,
        )]

    dossier = parties[1]
    nom_fichier = parties[-1]

    if dossier != instance_source:
        refus_liste.append(refus(
            "CHEMIN_DOSSIER_INCOHERENT",
            fichier=chemin_relatif,
            pointeur="/instance_source",
            dossier=dossier,
            instance_source=instance_source,
        ))

    nom_attendu = slug(cle_cible) + ".yml"
    if nom_fichier != nom_attendu:
        refus_liste.append(refus(
            "CHEMIN_NOM_FICHIER_INCOHERENT",
            fichier=chemin_relatif,
            pointeur="/cle_cible",
            nom_recu=nom_fichier,
            cle_cible=cle_cible,
            nom_attendu=nom_attendu,
        ))

    return refus_liste


def valider_coherence(demande: Demande) -> list[Refus]:
    """Cross-field rules that cannot be expressed in a JSON schema."""
    if demande.cle_source == demande.cle_cible:
        return [refus(
            "CLES_IDENTIQUES",
            fichier=demande.fichier,
            pointeur="/cle_cible",
            cle=demande.cle_source,
        )]
    return []


def valider_instance(demande: Demande, inventaire: Inventaire) -> list[Refus]:
    """Checks that the declared instance exists and is active.

    Does *not* check authorization: that is the responsibility of the batch
    2 control, which needs network calls that this module deliberately does
    not make.
    """
    source = inventaire.source(demande.instance_source)
    if source is None:
        return [refus(
            "INSTANCE_INCONNUE",
            fichier=demande.fichier,
            pointeur="/instance_source",
            instance_source=demande.instance_source,
        )]
    if not source.actif:
        return [refus(
            "INSTANCE_INACTIVE",
            fichier=demande.fichier,
            pointeur="/instance_source",
            instance_source=demande.instance_source,
            libelle=source.libelle,
        )]
    return []


def valider_unicite(demande: Demande, autres: list[Demande]) -> list[Refus]:
    """Detects collisions with the other requests already present in the
    repository (all branches combined at call time — it is the caller's
    responsibility to supply the right list, typically the state of
    ``main``).

    Three distinct collisions, not to be merged: same target = the target
    is already promised to another request; same source pair = the same
    source project already has a declared destination; same file name for
    different target keys = an ambiguous slug, an edge case but covered.
    """
    refus_liste: list[Refus] = []
    for autre in autres:
        if autre.fichier == demande.fichier:
            continue
        if autre.cle_cible == demande.cle_cible and autre.instance_source == demande.instance_source:
            refus_liste.append(refus(
                "DOUBLON_CLE_CIBLE",
                fichier=demande.fichier,
                pointeur="/cle_cible",
                cle_cible=demande.cle_cible,
                autre_fichier=autre.fichier,
            ))
        elif (autre.instance_source == demande.instance_source
              and autre.cle_source == demande.cle_source):
            refus_liste.append(refus(
                "DOUBLON_COUPLE_SOURCE",
                fichier=demande.fichier,
                pointeur="/cle_source",
                cle_source=demande.cle_source,
                instance_source=demande.instance_source,
                autre_fichier=autre.fichier,
            ))
        elif (slug(autre.cle_cible) == slug(demande.cle_cible)
              and autre.instance_source == demande.instance_source
              and autre.cle_cible != demande.cle_cible):
            refus_liste.append(refus(
                "SLUG_COLLISION",
                fichier=demande.fichier,
                pointeur="/cle_cible",
                cle_cible=demande.cle_cible,
                autre_cle=autre.cle_cible,
                autre_fichier=autre.fichier,
            ))
    return refus_liste


def valider_fichier(
    chemin_absolu: Path,
    chemin_relatif: str,
    inventaire: Inventaire | None = None,
    autres_demandes: list[Demande] | None = None,
) -> tuple[Demande | None, list[Refus]]:
    """Entry point: validates a file end to end.

    Returns ``(None, refus)`` if the request is invalid, or
    ``(demande, [])`` if it is accepted. ``inventaire`` and
    ``autres_demandes`` are optional to allow partial usage (e.g.
    validating shape only, locally, before pushing).
    """
    if not chemin_absolu.is_file():
        return None, [refus("FICHIER_INTROUVABLE", fichier=chemin_relatif)]

    try:
        verifier_taille(chemin_absolu.stat().st_size)
        texte = chemin_absolu.read_text(encoding="utf-8")
        donnees = charger_yaml_strict(texte)
    except ErreurYaml as e:
        return None, [refus(e.code, fichier=chemin_relatif, **e.valeurs)]
    except UnicodeDecodeError:
        return None, [refus(
            "YAML_ILLISIBLE",
            fichier=chemin_relatif,
            detail="the file is not UTF-8 encoded",
        )]

    refus_schema = valider_schema(donnees, chemin_relatif)
    if refus_schema:
        return None, refus_schema

    demande = Demande.depuis_dict(donnees, fichier=chemin_relatif)

    refus_liste: list[Refus] = []
    refus_liste += valider_chemin(chemin_relatif, demande.instance_source, demande.cle_cible)
    refus_liste += valider_coherence(demande)
    if inventaire is not None:
        refus_liste += valider_instance(demande, inventaire)
    if autres_demandes is not None:
        refus_liste += valider_unicite(demande, autres_demandes)

    if refus_liste:
        return None, refus_liste
    return demande, []
