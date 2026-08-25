"""Catalogue of messages shown to the requester, in English.

A single place. Validation modules produce codes; this module produces the
text. Practical consequence: one can review everything a requester might
read without reading through the code, and tests rely on codes, never on the
wording.

Each entry carries two sentences: ``message`` says what's wrong, ``action``
says what to do. A refusal without an action is a refusal nobody can act on.
"""

from __future__ import annotations

from typing import Any

from .modele import Refus

# Human-readable field labels, so we never render a raw JSON pointer.
LIBELLES_CHAMPS = {
    "version": "contract version",
    "instance_source": "source instance identifier",
    "cle_source": "source project key",
    "cle_cible": "target project key",
    "ticket": "ticket reference",
    "fenetre_souhaitee": "requested window",
    "commentaire": "comment",
}

_CATALOGUE: dict[str, dict[str, str]] = {
    # --- Reading the file ---------------------------------------------------
    "FICHIER_INTROUVABLE": {
        "message": "The request file could not be found.",
        "action": "Check that the file was actually added to the merge request.",
    },
    "FICHIER_TROP_GROS": {
        "message": "The file is {taille} bytes, the limit is {limite} bytes.",
        "action": "A request fits in about ten lines. Start from the template "
                  "provided in requests/entite-exemple/.",
    },
    "FICHIER_EMPLACEMENT": {
        "message": "The file is not at the expected location "
                   "requests/<instance_source>/<cle_cible>.yml.",
        "action": "Move the file under requests/<your instance identifier>/ "
                  "and give it the .yml extension.",
    },
    "YAML_ILLISIBLE": {
        "message": "The file is not valid YAML: {detail}",
        "action": "Fix the syntax. Watch the indentation, which must use "
                  "spaces and never tabs.",
    },
    "YAML_MULTI_DOCUMENT": {
        "message": "The file contains {nombre} YAML documents; a request "
                   "must contain exactly one.",
        "action": "Remove the extra « --- » separators, and submit "
                  "exactly one request per file.",
    },
    "YAML_ALIAS": {
        "message": "The file uses a YAML anchor or alias (« & » / « * »), "
                   "which is refused for security reasons.",
        "action": "Write the values in full, without anchors or aliases.",
    },
    "YAML_CLE_DUPLIQUEE": {
        "message": "The field « {champ} » is declared more than once.",
        "action": "Keep only one declaration. A duplicated field is easy to "
                  "misread during review, and it is not the first occurrence "
                  "that wins.",
    },
    "YAML_RACINE_NON_OBJET": {
        "message": "The YAML document must be a set of "
                   "« name: value » fields; here it is {type_recu}.",
        "action": "Start from the template provided in requests/entite-exemple/.",
    },
    # --- Schema --------------------------------------------------------------
    "SCHEMA_CHAMP_MANQUANT": {
        "message": "The required field « {champ} » ({libelle}) is missing.",
        "action": "Add « {champ}: » with its value.",
    },
    "SCHEMA_CHAMP_INCONNU": {
        "message": "The field « {champ} » does not exist in the request contract.",
        "action": "Remove it. The accepted fields are: {champs_acceptes}. "
                  "Any extra context belongs in the merge request "
                  "description.",
    },
    "SCHEMA_TYPE_INVALIDE": {
        "message": "The field « {champ} » ({libelle}) must be of type "
                   "{attendu}, it is `{valeur}`.",
        "action": "Fix the value. If it contains « : » or « # », wrap it "
                  "in double quotes.",
    },
    "SCHEMA_VALEUR_INVALIDE": {
        "message": "The field « {champ} » ({libelle}) is `{valeur}`, which "
                   "does not match the expected format: {attendu}",
        "action": "{action_specifique}",
    },
    "SCHEMA_TROP_LONG": {
        "message": "The field « {champ} » ({libelle}) is {longueur} "
                   "characters long, the maximum is {maximum}.",
        "action": "Shorten the value.",
    },
    # --- Request consistency -------------------------------------------------
    "CHEMIN_DOSSIER_INCOHERENT": {
        "message": "The file is in the folder « {dossier} » while "
                   "« instance_source » is « {instance_source} ».",
        "action": "Move the file to requests/{instance_source}/, or "
                  "correct « instance_source ».",
    },
    "CHEMIN_NOM_FICHIER_INCOHERENT": {
        "message": "The file is named « {nom_recu} » while the target key "
                   "`{cle_cible}` requires « {nom_attendu} ».",
        "action": "Rename the file to « {nom_attendu} ».",
    },
    "CLES_IDENTIQUES": {
        "message": "The source key and the target key are identical (`{cle}`).",
        "action": "The migration deletes the project holding the target key "
                  "before importing: with two identical keys, it would "
                  "destroy the very project being migrated. Check the key "
                  "generated by the DevOps portal for your space.",
    },
    "DOUBLON_CLE_CIBLE": {
        "message": "The target key `{cle_cible}` is already claimed by "
                   "request {autre_fichier}.",
        "action": "A target key is migrated only once. If the earlier "
                  "request failed, contact the central team rather than "
                  "submitting a second one.",
    },
    "DOUBLON_COUPLE_SOURCE": {
        "message": "The source project `{cle_source}` on instance "
                   "« {instance_source} » is already claimed by "
                   "request {autre_fichier}.",
        "action": "A source project migrates to only one target. Remove "
                  "the now-obsolete request before submitting a new one.",
    },
    "SLUG_COLLISION": {
        "message": "The target key `{cle_cible}` produces the same file "
                   "name as `{autre_cle}` ({autre_fichier}).",
        "action": "Report this to the central team: two distinct keys "
                  "cannot coexist under this file name in the requests "
                  "repository.",
    },
    # --- Inventory ------------------------------------------------------------
    "INSTANCE_INCONNUE": {
        "message": "The source instance « {instance_source} » does not "
                   "appear in the inventory.",
        "action": "Use one of the identifiers published in "
                  "docs/instances-disponibles.md. If your instance is not "
                  "listed, it has not been onboarded yet: contact the "
                  "central team.",
    },
    "INSTANCE_INACTIVE": {
        "message": "The source instance « {instance_source} » ({libelle}) "
                   "is marked inactive in the inventory.",
        "action": "No migration is accepted from this instance at the "
                  "moment. Contact the central team.",
    },
    # --- Authorization check (batch 2) ---------------------------------------
    "PROJET_SOURCE_INCONNU": {
        "message": "The source project `{cle}` could not be found on "
                   "instance {libelle} ({instance_id}).",
        "action": "Check the source key declared in your request.",
    },
    "PROJET_CIBLE_INCONNU": {
        "message": "The target project `{cle}` could not be found on "
                   "instance {libelle} ({instance_id}).",
        "action": "Create the DevOps space and its project via the portal "
                  "first, then resubmit your request.",
    },
    "PROJET_CIBLE_DEJA_ANALYSE": {
        "message": "The target project `{cle}` on instance {libelle} "
                   "({instance_id}) already contains analyses.",
        "action": "The import would overwrite existing data: manual "
                  "intervention is required. Contact the central team.",
    },
    "PROJET_CIBLE_SANS_GROUPE_ADMIN": {
        "message": "No group holds the admin permission on the target "
                   "project `{cle}` of instance {libelle} ({instance_id}).",
        "action": "The project was probably not created by the DevOps "
                  "portal, or its provisioning failed. Central team: "
                  "please review before this request is retried.",
    },
    "COMPTE_INCONNU_INSTANCE": {
        "message": "Your account is not known to instance {libelle} "
                   "({instance_id}).",
        "action": "Sign in at least once on this instance via the "
                  "corporate SSO, then resubmit your request.",
    },
    "DOUBLON_ANNUAIRE": {
        "message": "Your directory identifier matches {nombre} distinct "
                   "local accounts on instance {libelle} "
                   "({instance_id}): {logins}.",
        "action": "This is a directory duplicate, to be fixed by hand. "
                  "Contact the central team: your request cannot be "
                  "processed as is.",
    },
    "PAS_ADMIN": {
        "message": "You are not an administrator of project `{cle}` on "
                   "instance {libelle} ({instance_id}) (local account: "
                   "{login}).",
        "action": "Request the Administer role on this project — either "
                  "directly, or by being added to one of the groups that "
                  "hold it — then resubmit your request.",
    },
    "GROUPE_TROP_LARGE": {
        "message": "The group `{groupe}` is an administrator of project "
                   "`{cle}` on instance {libelle} ({instance_id}); this "
                   "group is considered too broad to count as "
                   "authorization and was ignored by the check.",
        "action": "Central team: this project must be reconfigured to "
                  "remove the admin permission from this group before any "
                  "migration.",
    },
    "CLE_SOURCE_COLLISION_CENTRALE": {
        "message": "The source key `{cle_source}` already matches an "
                   "existing project on the central instance.",
        "action": "Collision with another entity: to be handled upstream "
                  "through a prefixing convention. Contact the central "
                  "team.",
    },
}


def extrait(valeur: Any, longueur: int = 80) -> str:
    """Makes a requester-supplied value safe to display.

    The report is posted as a merge request comment, i.e. as Markdown: we
    neutralize backticks and pipes, strip non-printable characters, and cap
    the length. A refused value remains an untrusted value.
    """
    if valeur is None:
        return "(absent)"
    texte = valeur if isinstance(valeur, str) else repr(valeur)
    texte = "".join(c if c.isprintable() else " " for c in texte)
    texte = texte.replace("`", "'").replace("|", "/")
    if len(texte) > longueur:
        texte = texte[:longueur] + "..."
    return texte


def libelle_champ(champ: str) -> str:
    return LIBELLES_CHAMPS.get(champ, champ)


def refus(
    code: str,
    fichier: str | None = None,
    pointeur: str = "",
    alerte: bool = False,
    **valeurs: Any
) -> Refus:
    """Builds a ``Refus`` from a code and its variables.

    A code missing from the catalogue is a programming error: it is made
    visible rather than producing an empty message.
    """
    entree = _CATALOGUE.get(code)
    if entree is None:  # pragma: no cover - safety net
        return Refus(
            code=code,
            message=f"Refusal '{code}' has no associated message.",
            action="Report this anomaly to the central team.",
            fichier=fichier,
            pointeur=pointeur,
            alerte=True,
        )
    return Refus(
        code=code,
        message=entree["message"].format(**valeurs),
        action=entree["action"].format(**valeurs),
        fichier=fichier,
        pointeur=pointeur,
        alerte=alerte,
    )


def codes_connus() -> dict[str, dict[str, str]]:
    """Exposes the catalogue: the coverage test checks that every code
    emitted by validation appears here, and vice versa."""
    return dict(_CATALOGUE)
