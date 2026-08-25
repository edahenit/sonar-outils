#!/usr/bin/env python3
"""Dynamic Ansible inventory, derived from ``inventaire/instances.yml``.

A single source of truth: this script only translates the Python inventory
(``migration.inventaire``, already validated against its schema) into the
JSON format Ansible expects from an external inventory script (groups +
``_meta.hostvars``, Ansible's documented convention to avoid one ``--host``
call per machine). Adding an instance to ``instances.yml`` is enough to
make it appear here — nothing to duplicate.

Never carries a secret: ``sonar_variable_token`` is the NAME of the
protected variable to use for this instance, never its value (read by the
Ansible roles from the pipeline's environment, at call time, exactly as on
the Python side — see ``cli._jeton_environnement``).

Usage (standard contract of a dynamic Ansible inventory):
    depuis_instances.py --list
    depuis_instances.py --host <name>   # always {}: everything is in _meta
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from migration.inventaire import charger_inventaire
from migration.modele import Instance

_INVENTAIRE_DEFAUT = Path(__file__).resolve().parents[2] / "inventaire" / "instances.yml"


def _hostvars(instance: Instance) -> dict[str, Any]:
    return {
        "ansible_host": instance.ssh_hote,
        "sonar_url": instance.url,
        "sonar_home": instance.sonarqube_home,
        "sonar_api_identite": instance.api_identite,
        "sonar_fournisseur_identite_sso": instance.fournisseur_identite_sso,
        "sonar_role": instance.role,
        "sonar_variable_token": instance.variable_token,
        "sonar_actif": instance.actif,
    }


def construire_inventaire(chemin_instances: Path) -> dict[str, Any]:
    inv = charger_inventaire(chemin_instances)
    hostvars = {inv.centrale.id: _hostvars(inv.centrale)}
    hostvars.update({i.id: _hostvars(i) for i in inv.sources.values()})
    return {
        "sonar_centrale": {"hosts": [inv.centrale.id]},
        "sonar_source": {"hosts": sorted(inv.sources.keys())},
        "_meta": {"hostvars": hostvars},
    }


def main(argv: list[str] | None = None) -> int:
    parseur = argparse.ArgumentParser(description=__doc__)
    groupe = parseur.add_mutually_exclusive_group(required=True)
    groupe.add_argument("--list", action="store_true")
    groupe.add_argument("--host")
    parseur.add_argument("--inventaire", type=Path, default=_INVENTAIRE_DEFAUT)
    args = parseur.parse_args(argv)

    if args.host:
        # All variables are already served via _meta.hostvars under
        # --list: Ansible only calls --host if _meta is absent.
        print(json.dumps({}))
        return 0

    print(json.dumps(construire_inventaire(args.inventaire)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
