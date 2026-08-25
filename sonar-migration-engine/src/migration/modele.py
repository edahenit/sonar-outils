"""Domain types, all immutable and serializable.

Cross-cutting rule: no object in this module carries a secret. Tokens are
read from the environment at the moment of the HTTP call, never stored in a
structure that could end up in the journal or a report.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

# Characters allowed in a request file name. Everything else is replaced
# with a dash: SonarQube keys accept « : » and « / », a portable git tree
# does not.
_HORS_NOM_FICHIER = re.compile(r"[^A-Za-z0-9_.-]")

# Whitelist for project keys, identical to the JSON schema's pattern.
# Deliberately duplicated here: the code must never depend on the schema
# alone to refuse a key.
MOTIF_CLE = re.compile(r"^[A-Za-z0-9_.:-]{1,400}$")

# Instance identifier, also used as the entity's folder name.
MOTIF_INSTANCE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")


def slug(cle: str) -> str:
    """Deterministic file name for a project key.

    ``com.alpha:facturation-api`` -> ``com.alpha-facturation-api``
    """
    return _HORS_NOM_FICHIER.sub("-", cle)


@dataclass(frozen=True)
class Refus:
    """A reason for rejection, returned as-is to the requester.

    A refusal always says three things: what's wrong (``message``), where
    (``fichier`` + ``pointeur``), and what the requester must do
    (``action``). Without the third, the report is useless.
    """

    code: str
    message: str
    action: str
    fichier: str | None = None
    pointeur: str = ""
    # True for abnormal situations that call for central team intervention
    # (directory duplicate, overly broad group, unprovisioned target
    # project). A plain "not admin" is a normal refusal: it alerts no one.
    alerte: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Demande:
    """A migration request, after full validation."""

    version: int
    instance_source: str
    cle_source: str
    cle_cible: str
    ticket: str | None = None
    fenetre_souhaitee: str | None = None
    commentaire: str | None = None
    # Path relative to the requests repository root. Serves as the natural
    # identifier: a request is a file, it exists exactly once.
    fichier: str = ""

    @property
    def identifiant(self) -> str:
        """Stable run identifier, used by the lock and the journal."""
        return f"{self.instance_source}/{slug(self.cle_cible)}"

    @classmethod
    def depuis_dict(cls, donnees: Mapping[str, Any], fichier: str) -> Demande:
        return cls(
            version=int(donnees["version"]),
            instance_source=str(donnees["instance_source"]),
            cle_source=str(donnees["cle_source"]),
            cle_cible=str(donnees["cle_cible"]),
            ticket=donnees.get("ticket"),
            fenetre_souhaitee=donnees.get("fenetre_souhaitee"),
            commentaire=donnees.get("commentaire"),
            fichier=fichier,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Instance:
    """A reachable SonarQube instance, source or central."""

    id: str
    libelle: str
    url: str
    api_identite: str  # "v1" | "v2" — see docs/a-verifier.md
    fournisseur_identite_sso: str  # IdP provider name as known to THIS instance
    ssh_hote: str
    sonarqube_home: str
    variable_token: str
    role: str  # "centrale" | "source"
    actif: bool = True

    @property
    def repertoire_export(self) -> str:
        return f"{self.sonarqube_home}/data/governance/project_dumps/export"

    @property
    def repertoire_import(self) -> str:
        return f"{self.sonarqube_home}/data/governance/project_dumps/import"

    def to_dict(self) -> dict[str, Any]:
        """Serialization for the journal. ``variable_token`` is the *name*
        of the variable, never its value: publishing it is harmless, and
        useful for auditing."""
        return asdict(self)


@dataclass(frozen=True)
class Inventaire:
    """Instance catalogue. Authoritative, and lives in the engine repo."""

    version: int
    groupes_interdits: tuple[str, ...]
    centrale: Instance
    sources: Mapping[str, Instance] = field(default_factory=dict)

    def source(self, identifiant: str) -> Instance | None:
        return self.sources.get(identifiant)

    def est_groupe_interdit(self, nom: str) -> bool:
        """Case-insensitive comparison: SonarQube does not enforce a case
        on group names, and « Sonar-Users » must be refused just like
        « sonar-users »."""
        cible = nom.casefold()
        return any(g.casefold() == cible for g in self.groupes_interdits)

    def sources_actives(self) -> list[Instance]:
        return [i for i in self.sources.values() if i.actif]
