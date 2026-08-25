"""Hardened YAML reading, shared by the request and the inventory.

``yaml.safe_load`` protects against arbitrary code execution, nothing else.
Three PyYAML default behaviors are refused here:

* **anchors and aliases** (``&x`` / ``*x``), which enable the so-called
  "billion laughs" exponential expansion from a file only a few lines long;
* **duplicate keys**, which PyYAML silently accepts while keeping the last
  one — a request where ``cle_source`` appears twice is easy to misread
  during review and does not mean what the reviewer thinks it does;
* **implicit date resolution**, which turns ``2026-09-15`` into a
  ``datetime.date`` and makes the schema's ``type: string`` validation fail
  on an otherwise correct value.

The file is also size-capped before it is read.
"""

from __future__ import annotations

from typing import Any

import yaml

# A request fits in about ten lines; the inventory in a few hundred. 16 KiB
# leaves a comfortable margin without opening the door to a file crafted to
# saturate the parser.
TAILLE_MAX_OCTETS = 16 * 1024


class ErreurYaml(Exception):
    """Reading error, carrying a code from the message catalogue."""

    def __init__(self, code: str, **valeurs: Any) -> None:
        super().__init__(code)
        self.code = code
        self.valeurs: dict[str, Any] = valeurs


class _LoaderStrict(yaml.SafeLoader):
    """SafeLoader without aliases, without duplicate keys, without implicit dates."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.events.AliasEvent):
            raise ErreurYaml("YAML_ALIAS")
        return super().compose_node(parent, index)

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        vues = set()
        for cle_node, _ in node.value:
            cle = self.construct_object(cle_node, deep=deep)
            if cle in vues:
                raise ErreurYaml("YAML_CLE_DUPLIQUEE", champ=str(cle))
            vues.add(cle)
        return super().construct_mapping(node, deep=deep)


# Removal of the implicit "timestamp" resolver: dates stay strings, which is
# what the schema expects and what the JSON journal can serialize as-is.
_LoaderStrict.yaml_implicit_resolvers = {
    prefixe: [(tag, motif) for tag, motif in resolveurs
              if tag != "tag:yaml.org,2002:timestamp"]
    for prefixe, resolveurs in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


_TYPES_LISIBLES = {
    list: "a list",
    str: "a string of characters",
    int: "a number",
    float: "a number",
    bool: "a boolean",
    type(None): "nothing (empty document)",
}


def type_lisible(valeur: Any) -> str:
    return _TYPES_LISIBLES.get(type(valeur), type(valeur).__name__)


def charger_yaml_strict(texte: str) -> dict[str, Any]:
    """Loads a single YAML document and returns its root object.

    Raises ``ErreurYaml`` with a catalogue code on refusal.
    """
    try:
        documents: list[Any | None] = list(
            yaml.load_all(texte, Loader=_LoaderStrict)
        )
    except ErreurYaml:
        raise
    except yaml.YAMLError as exc:
        raise ErreurYaml("YAML_ILLISIBLE", detail=_detail_pyyaml(exc))

    if len(documents) != 1:
        raise ErreurYaml("YAML_MULTI_DOCUMENT", nombre=len(documents))

    racine = documents[0]
    if not isinstance(racine, dict):
        raise ErreurYaml("YAML_RACINE_NON_OBJET", type_recu=type_lisible(racine))
    return racine


def verifier_taille(taille: int) -> None:
    """Refuses an oversized file before it is even read."""
    if taille > TAILLE_MAX_OCTETS:
        raise ErreurYaml(
            "FICHIER_TROP_GROS", taille=taille, limite=TAILLE_MAX_OCTETS
        )


def _detail_pyyaml(exc: yaml.YAMLError) -> str:
    """Extracts a short, localized detail from the PyYAML exception.

    Keeps the line number, which is useful, and drops the rest of PyYAML's
    message, which is written in low-level lexical-analysis terms not meant
    for the end reader.
    """
    marque = getattr(exc, "problem_mark", None)
    probleme = getattr(exc, "problem", None) or "invalid syntax"
    if marque is not None:
        return f"line {marque.line + 1}, column {marque.column + 1} ({probleme})"
    return str(probleme)
