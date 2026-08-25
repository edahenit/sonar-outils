"""Loading of the SonarQube instance inventory.

One entity = one instance = one set of protected variables. This module
derives the name of the variable holding the admin token from the
instance's identifier, so that the convention is written in exactly one
place (see root README, variables table).

Authoritative: ``inventaire/instances.yml`` of the engine repository. This
module never reads the inventory from the requests repository — see the
"deviation" note in the root README.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from .modele import Instance, Inventaire

_ICI = Path(__file__).parent
_CHEMIN_SCHEMA = _ICI / "schema" / "instances.schema.json"

# Variable name derivation: SONAR_SRC_<ID IN UPPERCASE, '-' -> '_'>_TOKEN
_MOTIF_VARIABLE = re.compile(r"^(SONAR_SRC_[A-Z0-9_]{1,48}_TOKEN|SONAR_CENTRALE_TOKEN)$")


class ErreurInventaire(Exception):
    """The inventory itself is invalid.

    This is never a requester error — the inventory is not in their
    repository — so this is deliberately not a ``Refus``: an error here
    must fail the job with a message for the central team, not produce an
    MR comment.
    """


def _charger_schema() -> dict[str, Any]:
    with _CHEMIN_SCHEMA.open("r", encoding="utf-8") as f:
        return json.load(f)


def _variable_token(identifiant: str, role: str, declaree: str = "") -> str:
    if declaree:
        if not _MOTIF_VARIABLE.match(declaree):
            raise ErreurInventaire(
                f"variable_token '{declaree}' does not follow convention for instance '{identifiant}': "
                "it must match ^SONAR_SRC_[A-Z0-9_]+_TOKEN$ or "
                "be SONAR_CENTRALE_TOKEN."
            )
        return declaree
    if role == "centrale":
        return "SONAR_CENTRALE_TOKEN"
    derive = "SONAR_SRC_{}_TOKEN".format(
        identifiant.upper().replace("-", "_")
    )
    if not _MOTIF_VARIABLE.match(derive):  # pragma: no cover - safety net
        raise ErreurInventaire(
            f"instance identifier '{identifiant}' produces a variable name that "
            f"does not follow convention once derived: '{derive}'."
        )
    return derive


def _instance_depuis_dict(donnees: Mapping[str, Any], role: str) -> Instance:
    identifiant = str(donnees["id"])
    return Instance(
        id=identifiant,
        libelle=str(donnees["libelle"]),
        url=str(donnees["url"]),
        api_identite=str(donnees["api_identite"]),
        fournisseur_identite_sso=str(donnees["fournisseur_identite_sso"]),
        ssh_hote=str(donnees["ssh_hote"]),
        sonarqube_home=str(donnees["sonarqube_home"]),
        variable_token=_variable_token(
            identifiant, role, str(donnees.get("variable_token", ""))
        ),
        role=role,
        actif=bool(donnees.get("actif", True)),
    )


def charger_inventaire(chemin: Path) -> Inventaire:
    """Loads and validates ``instances.yml``. Raises ``ErreurInventaire`` on
    any problem — never a ``Refus``, this file does not belong to the
    requester.
    """
    if not chemin.is_file():
        raise ErreurInventaire(f"inventory file not found: {chemin}")

    try:
        with chemin.open("r", encoding="utf-8") as f:
            donnees = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ErreurInventaire(f"invalid inventory YAML: {exc}")

    schema = _charger_schema()
    validateur_cls = jsonschema.validators.validator_for(schema)
    validateur_cls.check_schema(schema)
    validateur = validateur_cls(schema)
    erreurs = sorted(validateur.iter_errors(donnees), key=lambda e: list(e.absolute_path))
    if erreurs:
        premiere = erreurs[0]
        pointeur = "/" + "/".join(str(p) for p in premiere.absolute_path)
        raise ErreurInventaire(
            f"inventory does not conform to the schema at {pointeur}: {premiere.message}"
        )

    centrale = _instance_depuis_dict(donnees["centrale"], role="centrale")
    sources: dict[str, Instance] = {}
    for entree in donnees["instances_sources"]:
        instance = _instance_depuis_dict(entree, role="source")
        if instance.id in sources:
            raise ErreurInventaire(
                f"duplicate source instance identifier: '{instance.id}'."
            )
        if instance.id == centrale.id:
            raise ErreurInventaire(
                f"the identifier '{instance.id}' is used for both the central "
                "instance and a source instance."
            )
        sources[instance.id] = instance

    groupes_interdits = tuple(donnees["groupes_interdits"])
    return Inventaire(
        version=int(donnees["version"]),
        groupes_interdits=groupes_interdits,
        centrale=centrale,
        sources=sources,
    )


def catalogue_public(inventaire: Inventaire) -> list[dict[str, str]]:
    """Publishable extract of the inventory: id and label only.

    Used to generate ``sonar-migration-requests/docs/instances-disponibles.md``.
    Neither the URL, nor the SSH host, nor the variable name leave this
    function: those are infrastructure details of the engine repository,
    not information the requester needs to fill in their request.
    """
    return [
        {"id": i.id, "libelle": i.libelle}
        for i in sorted(inventaire.sources_actives(), key=lambda i: i.id)
    ]
