"""Tests of discovering the request file modified by a commit.

Two distinct responsibilities tested separately: isolating THE request file
among an already-known list of paths (pure logic), and obtaining that list
from a real commit via git (glue, tested against a real temporary
repository rather than assumed correct).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from migration.decouverte import (
    ErreurDecouverte,
    chemins_modifies_par_commit,
    fichier_demande_du_commit,
)

# --- Isolating the request file (pure logic) -----------------------------


def test_isole_lunique_fichier_de_demande():
    chemins = ["requests/entite-alpha/grp-alpha-x.yml", "README.md"]
    assert fichier_demande_du_commit(chemins) == "requests/entite-alpha/grp-alpha-x.yml"


def test_aucun_fichier_de_demande_leve():
    with pytest.raises(ErreurDecouverte):
        fichier_demande_du_commit(["README.md", "docs/instances-disponibles.md"])


def test_plusieurs_fichiers_de_demande_leve():
    chemins = [
        "requests/entite-alpha/grp-alpha-x.yml",
        "requests/entite-beta/grp-beta-y.yml",
    ]
    with pytest.raises(ErreurDecouverte):
        fichier_demande_du_commit(chemins)


def test_fichier_hors_arborescence_requests_ignore():
    # A .yml at the root of requests/ (wrong depth) is not a valid
    # request: it is not this function's job to flag it as such
    # (validation.py handles that), only not to confuse it with a real
    # request.
    with pytest.raises(ErreurDecouverte):
        fichier_demande_du_commit(["requests/grp-alpha-x.yml"])


def test_modification_du_gitlab_ci_yml_avec_une_demande_nest_pas_une_erreur():
    """Reminder of the security principle: even if the commit also
    touches .gitlab-ci.yml (a dead file, never read — see root README),
    discovery proceeds normally on the request."""
    chemins = ["requests/entite-alpha/grp-alpha-x.yml", ".gitlab-ci.yml"]
    assert fichier_demande_du_commit(chemins) == "requests/entite-alpha/grp-alpha-x.yml"


# --- List of paths modified by a commit (real git) ------------------


def _depot_avec_deux_commits(tmp_path: Path) -> tuple[Path, str]:
    depot = tmp_path / "requests"
    depot.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=depot, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=depot, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=depot, check=True)
    (depot / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=depot, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=depot, check=True)

    (depot / "requests").mkdir()
    (depot / "requests" / "entite-alpha").mkdir()
    (depot / "requests" / "entite-alpha" / "grp-alpha-x.yml").write_text(
        "version: 1\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "requests"], cwd=depot, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "demande"], cwd=depot, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=depot, check=True, capture_output=True, text=True
    ).stdout.strip()
    return depot, sha


def test_chemins_modifies_par_commit_reel(tmp_path):
    depot, sha = _depot_avec_deux_commits(tmp_path)
    chemins = chemins_modifies_par_commit(depot, sha)
    assert chemins == ["requests/entite-alpha/grp-alpha-x.yml"]


def test_bout_en_bout_decouverte_sur_commit_reel(tmp_path):
    depot, sha = _depot_avec_deux_commits(tmp_path)
    chemins = chemins_modifies_par_commit(depot, sha)
    assert fichier_demande_du_commit(chemins) == "requests/entite-alpha/grp-alpha-x.yml"
