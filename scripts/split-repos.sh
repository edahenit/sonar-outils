#!/usr/bin/env bash
# Splits this development monorepo into three distinct GitLab
# repositories, preserving each folder's history.
#
# Usage:
#   scripts/split-repos.sh <gitlab_group_url>
#
# Example:
#   scripts/split-repos.sh git@gitlab.groupe.example:outils-sonar
#
# Prerequisite: the three empty GitLab projects already exist
#   <gitlab_group_url>/sonar-migration-engine
#   <gitlab_group_url>/sonar-migration-requests
#   <gitlab_group_url>/sonar-migration-runs
# with permissions already set up (see root README, permissions table) —
# this script neither creates nor configures any GitLab project, it only
# pushes code and history.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <gitlab_group_url>" >&2
  exit 1
fi

GROUPE="$1"
DOSSIERS=(sonar-migration-engine sonar-migration-requests sonar-migration-runs)

racine_git="$(git rev-parse --show-toplevel)"
cd "$racine_git"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree not clean: commit or stash before splitting." >&2
  exit 1
fi

for dossier in "${DOSSIERS[@]}"; do
  echo "== ${dossier} =="
  # git subtree split keeps, for this folder, only the commits that
  # touched it — this gives each target repository a clean history
  # rather than a copy of the whole monorepo.
  branche_temp="split/${dossier}"
  git subtree split --prefix="${dossier}" -b "${branche_temp}"

  cible="${GROUPE}/${dossier}.git"
  echo "   Pushing to ${cible} (main branch)"
  git push "${cible}" "${branche_temp}:refs/heads/main"

  git branch -D "${branche_temp}"
done

echo
echo "Split complete. Reminders before going live:"
echo "  1. On sonar-migration-requests: set Settings -> CI/CD -> CI"
echo "     configuration file to sonar-migration-engine (see root README)."
echo "  2. Set the protected and masked variables on the GitLab group"
echo "     containing sonar-migration-engine (never on the other two)."
echo "  3. Protect the main branch of all three repositories, with the"
echo "     permissions from the root README's table."
