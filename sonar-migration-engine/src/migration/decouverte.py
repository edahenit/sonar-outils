"""Discovery of the request file carried by a commit.

The check job runs on ``main`` after a merge (pipeline triggered by a push,
not by a merge request): it therefore does not directly have the list of
files touched by the request. This module obtains it from git itself (the
commit is authoritative, never a path passed in by a third party), then
isolates the request file it contains.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class ErreurDecouverte(Exception):
    """The commit does not carry exactly one identifiable request."""


def chemins_modifies_par_commit(depot: Path, commit_sha: str) -> list[str]:
    """Paths (relative to the repository root) modified by this commit.

    Uses ``git diff-tree``: this reads the commit's actual content, it is
    not a supposition about what the merge request must have contained.
    """
    resultat = subprocess.run(
        ["git", "-C", str(depot), "diff-tree", "--no-commit-id", "--name-only", "-r", commit_sha],
        capture_output=True, text=True, check=True,
    )
    return [ligne for ligne in resultat.stdout.splitlines() if ligne.strip()]


def fichier_demande_du_commit(chemins_modifies: list[str]) -> str:
    """Isolates, among the modified paths, the single request file
    (``requests/<instance_source>/<slug(cle_cible)>.yml``).

    A request is a file (root README, § security, "replay" threat): zero
    or several candidates are both refusals, never an arbitrary choice of
    the first one found.
    """
    candidats = [
        chemin for chemin in chemins_modifies
        if chemin.startswith("requests/") and chemin.endswith(".yml")
        and chemin.count("/") == 2
    ]
    if len(candidats) == 0:
        raise ErreurDecouverte(
            "no request file (requests/<instance>/<key>.yml) among the "
            "files modified by this commit."
        )
    if len(candidats) > 1:
        raise ErreurDecouverte(
            "several request files modified by the same commit ({}): "
            "a merge request must carry only one "
            "request.".format(", ".join(candidats))
        )
    return candidats[0]
