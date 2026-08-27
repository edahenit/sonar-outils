#!/usr/bin/env bash
#
# Banc d'essai de sonar-import-worker.
#
# Lance un faux SonarQube central et un faux Artifactory sur 127.0.0.1, puis
# déroule les scénarios d'import. Aucune instance réelle n'est touchée.
#
#   ./run-tests.sh
#
# À rejouer après toute adaptation : format de clé, nom du lien, nom du
# template de permissions, activation du contrôle d'habilitation.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../sonar-import-worker.sh"
LAB="$(mktemp -d /tmp/siw-lab.XXXXXX)"
PORT="${PORT:-18090}"
MOCK_PID=""

OK=0; KO=0
pass()  { printf '  \033[32mOK\033[0m   %s\n' "$1"; OK=$((OK+1)); }
fail()  { printf '  \033[31mKO\033[0m   %s\n' "$1"; KO=$((KO+1)); }
head2() { printf '\n\033[1m%s\033[0m\n' "$1"; }

cleanup() {
  [[ -n "$MOCK_PID" ]] && kill "$MOCK_PID" 2>/dev/null
  wait "$MOCK_PID" 2>/dev/null
  rm -rf "$LAB"
}
trap cleanup EXIT

for b in curl jq python3 sha256sum flock; do
  command -v "$b" >/dev/null || { echo "commande absente : $b"; exit 1; }
done

# --------------------------------------------------------------------------- #
#  Mise en place                                                               #
# --------------------------------------------------------------------------- #

mkdir -p "$LAB"/{importdir,state/done,state/retry,work}
mkdir -p "$LAB"/art/sonar-projects-to-migrate/espace12
mkdir -p "$LAB"/art/sonar-projects-migrated

# État initial de la fausse instance centrale.
#
#   p-espace12-mapipeline  projet vide créé par le portail   → cas nominal
#   p-espace12-actif       projet contenant déjà des analyses → doit être refusé
#   p-espace12-version     cible d'un export en 2026.1.1      → doit être refusé
#   p-espace12-plugins     cible d'un export avec cobol       → doit être refusé
#   p-espace12-droits      demandeur admin via son groupe     → doit passer
#
reset_state() { cat > "$LAB/state.json" <<'J'
{
  "version": "2026.2.1",
  "plugins": [
    {"key":"java","version":"8.0.1","editionBundled":true},
    {"key":"python","version":"4.2.0","editionBundled":true},
    {"key":"javascript","version":"10.1.0","editionBundled":true}
  ],
  "projects": {
    "p-espace12-mapipeline": {"analyses": 0},
    "p-espace12-actif":      {"analyses": 12},
    "p-espace12-plugins":    {"analyses": 0},
    "p-espace12-version":    {"analyses": 0},
    "p-espace12-droits":     {"analyses": 0}
  },
  "ce": {"CE-IMPORT-1": "SUCCESS"},
  "perm_global": ["admin.central"],
  "perm_project": {"p-espace12-droits": []},
  "perm_groups":  {"p-espace12-droits": ["equipe-app1234"]},
  "user_groups":  {"nom.prenom": ["equipe-app1234"], "autre.personne": ["equipe-x"]},
  "users": [
    {"login":"nom.prenom","email":"prenom.nom@entreprise.com",
     "externalLogin":"u123456","externalProvider":"saml","active":true},
    {"login":"autre.personne","email":"autre@entreprise.com",
     "externalLogin":"u999999","externalProvider":"saml","active":true}
  ],
  "links": {"p-espace12-mapipeline": [{"id":"L1","name":"MIGRATION","url":"https://x"}]}
}
J
}
reset_state

# Fabrique un couple archive + manifeste cohérent (sha256 calculé, pas inventé).
mk() {   # mk <suffixe> <clé_source> <clé_cible> <version> <plugins> [identité]
  local d="$LAB/art/sonar-projects-to-migrate/espace12"
  local zip="$d/com.entite_$1.zip" sha
  head -c 20000 /dev/urandom > "$zip"
  sha="$(sha256sum "$zip" | cut -d' ' -f1)"
  local ident="${6:-u123456}" mail="prenom.nom@entreprise.com"
  [[ "$ident" == "u999999" ]] && mail="autre@entreprise.com"
  cat > "$d/com.entite_$1.manifest.json" <<M
{
  "schema_version": "1.0",
  "request": {
    "id": "r-$1", "at": "2026-08-25T09:00:00Z",
    "by": {"login": "n/a", "email": "$mail",
           "external_identity": "$ident", "external_provider": "saml"},
    "declared_target_project_key": "$3",
    "espace_id": "espace12"
  },
  "source": {
    "instance": "https://sonar.entite.corp", "project_key": "$2",
    "sonar_version": "$4", "edition": "enterprise",
    "scm_repository": "app1234/$1",
    "plugins": $5,
    "export_task_id": "T-$1"
  },
  "archive": {"filename": "com.entite_$1.zip", "sha256": "$sha", "size_bytes": 20000}
}
M
}

PLUG_OK='[{"key":"java","version":"8.0.1"},{"key":"python","version":"4.2.0"}]'
PLUG_KO='[{"key":"java","version":"8.0.1"},{"key":"cobol","version":"5.3.0"}]'

seed() {
  rm -rf "$LAB/art/sonar-projects-to-migrate/espace12"
  mkdir -p "$LAB/art/sonar-projects-to-migrate/espace12"
  mk nominal "com.entite:nominal" "p-espace12-mapipeline" "2026.2.1" "$PLUG_OK"
  mk actif   "com.entite:actif"   "p-espace12-actif"      "2026.2.1" "$PLUG_OK"
  mk version "com.entite:version" "p-espace12-version"    "2026.1.1" "$PLUG_OK"
  mk plugins "com.entite:plugins" "p-espace12-plugins"    "2026.2.1" "$PLUG_KO"
  mk absent  "com.entite:absent"  "p-espace12-inexistant" "2026.2.1" "$PLUG_OK"
  mk droits  "com.entite:droits"  "p-espace12-droits"     "2026.2.1" "$PLUG_OK"
}
seed

cat > "$LAB/config.sh" <<CFG
SONAR_URL="http://127.0.0.1:$PORT"
SONAR_TOKEN="faux"
SONAR_EDITION="enterprise"
IMPORT_DIR="$LAB/importdir"
ARTIFACTORY_URL="http://127.0.0.1:$PORT/artifactory"
REPO_INBOX="sonar-projects-to-migrate"
REPO_DONE="sonar-projects-migrated"
ARTIFACTORY_TOKEN="faux"
TARGET_KEY_REGEX='^p-([A-Za-z0-9]+)-([A-Za-z0-9._-]+)\$'
LINK_NAME="MIGRATION"
PERMISSION_TEMPLATE="Template standard"
QUALITY_GATE="Groupe - Niveau 1"
DEVOPS_ALM_SETTING="gitlab-groupe"
ENFORCE_VERSION=true
ENFORCE_PLUGINS=true
ENFORCE_REQUESTER_ADMIN=false
CLEANUP_MIGRATION_LINK=true
STATE_DIR="$LAB/state"
WORK_DIR="$LAB/work"
LOCK_FILE="$LAB/lock"
MAX_ATTEMPTS=2
CE_POLL_SECONDS=1
CE_TIMEOUT_SECONDS=20
HTTP_TIMEOUT=10
LOG_LEVEL="INFO"
CFG

start_mock() { python3 "$HERE/mock_central.py" "$PORT" "$LAB" & MOCK_PID=$!; sleep 1.5; }
stop_mock()  { kill "$MOCK_PID" 2>/dev/null; wait "$MOCK_PID" 2>/dev/null; MOCK_PID=""; }

run()     { "$SCRIPT" -c "$LAB/config.sh" "$@" 2>&1; }
actions() { cat "$LAB/actions.log" 2>/dev/null; }
n_done()  { find "$LAB/art/sonar-projects-migrated" -type f 2>/dev/null | wc -l | tr -d ' '; }

# --------------------------------------------------------------------------- #
#  Scénarios                                                                   #
# --------------------------------------------------------------------------- #

echo "Banc d'essai — $LAB"
start_mock

head2 "1. Syntaxe"
bash -n "$SCRIPT" && pass "bash -n" || fail "bash -n"

head2 "2. Essai à blanc — les contrôles avant écriture"
out="$(run --dry-run)"
grep -q "actif.*deja des analyses"        <<<"$out" && pass "projet cible non vide refusé"   || fail "projet cible non vide refusé"
grep -q "version.*2026.1.1 != cible"      <<<"$out" && pass "version divergente refusée"     || fail "version divergente refusée"
grep -q "plugins.*cobol.*absent"          <<<"$out" && pass "plugin manquant refusé"         || fail "plugin manquant refusé"
grep -q "absent.*inexistant"              <<<"$out" && pass "cible absente → réessai"        || fail "cible absente → réessai"
grep -qc "tous les controles passent"     <<<"$out" && pass "cas nominal validé"             || fail "cas nominal validé"

head2 "3. L'essai à blanc n'écrit rien"
[[ -z "$(ls -A "$LAB/state/done")"  ]] && pass "aucun état écrit"      || fail "aucun état écrit"
[[ -z "$(ls -A "$LAB/state/retry")" ]] && pass "aucun réessai compté"  || fail "aucun réessai compté"
[[ "$(n_done)" == "0" ]]               && pass "rien archivé"          || fail "rien archivé"
[[ -z "$(actions)" ]]                  && pass "instance non modifiée" || fail "instance non modifiée"

head2 "4. Import réel"
rm -f "$LAB/actions.log"
out="$(run)"
grep -q "MIGRATION TERMINEE.*p-espace12-mapipeline" <<<"$out" && pass "cas nominal importé" || fail "cas nominal importé"
grep -q "MIGRATION TERMINEE.*p-espace12-droits"     <<<"$out" && pass "second projet importé" || fail "second projet importé"
[[ "$(n_done)" == "4" ]] && pass "2 archives + 2 manifestes archivés" || fail "2 archives + 2 manifestes archivés"

head2 "5. Ordre des opérations — rien n'est détruit avant de savoir"
seq="$(actions | grep -E '^(IMPORT|DELETE|RENAME) ' | grep -E 'nominal|mapipeline')"
i=$(grep -n '^IMPORT' <<<"$seq" | cut -d: -f1)
d=$(grep -n '^DELETE' <<<"$seq" | cut -d: -f1)
r=$(grep -n '^RENAME' <<<"$seq" | cut -d: -f1)
[[ -n "$i" && -n "$d" && "$i" -lt "$d" ]] && pass "import AVANT suppression" || fail "import AVANT suppression"
[[ -n "$d" && -n "$r" && "$d" -lt "$r" ]] && pass "suppression AVANT renommage" || fail "suppression AVANT renommage"

head2 "6. Reconfiguration après renommage"
a="$(actions)"
grep -q "TEMPLATE.*p-espace12-mapipeline" <<<"$a" && pass "template de permissions appliqué" || fail "template de permissions appliqué"
grep -q "GATE.*p-espace12-mapipeline"     <<<"$a" && pass "quality gate affecté"             || fail "quality gate affecté"
grep -q "BINDING.*app1234"                <<<"$a" && pass "binding GitLab recréé"            || fail "binding GitLab recréé"
grep -q "UNLINK"                          <<<"$a" && pass "lien MIGRATION retiré"            || fail "lien MIGRATION retiré"

head2 "7. Le renommage vise bien la clé déclarée"
grep -q "RENAME com.entite:nominal -> p-espace12-mapipeline" <<<"$a" \
  && pass "clé source → clé portail" || fail "clé source → clé portail"

head2 "8. Idempotence"
rm -f "$LAB/actions.log"
run >/dev/null 2>&1
[[ -z "$(actions)" ]] && pass "second passage sans effet" || fail "second passage sans effet"
[[ "$(n_done)" == "4" ]] && pass "rien republié" || fail "rien republié"

head2 "9. Contrôle d'habilitation du demandeur"
# Deux demandes : un demandeur admin via son groupe, un qui ne l'est pas.
stop_mock
rm -rf "$LAB/state"; mkdir -p "$LAB/state"/{done,retry}
rm -rf "$LAB/art/sonar-projects-to-migrate/espace12"
mkdir -p "$LAB/art/sonar-projects-to-migrate/espace12"
python3 - "$LAB" <<'PY'
import json, sys
p = sys.argv[1] + "/state.json"
s = json.load(open(p))
s["projects"]["p-espace12-droits"] = {"analyses": 0}
s["projects"]["p-espace12-refus"]  = {"analyses": 0}
s["perm_groups"]["p-espace12-refus"] = ["equipe-autre"]
json.dump(s, open(p, "w"), indent=2)
PY
mk autorise "com.entite:autorise" "p-espace12-droits" "2026.2.1" "$PLUG_OK" u123456
mk refuse   "com.entite:refuse"   "p-espace12-refus"  "2026.2.1" "$PLUG_OK" u999999
sed -i.bak 's/ENFORCE_REQUESTER_ADMIN=false/ENFORCE_REQUESTER_ADMIN=true/' "$LAB/config.sh"
start_mock
out="$(run --dry-run)"
grep -q "habilitation verifiee"                  <<<"$out" && pass "admin via groupe accepté" || fail "admin via groupe accepté"
grep -q "n est pas administrateur"               <<<"$out" && pass "non-admin refusé"         || fail "non-admin refusé"
sed -i.bak 's/ENFORCE_REQUESTER_ADMIN=true/ENFORCE_REQUESTER_ADMIN=false/' "$LAB/config.sh"

head2 "10. Intégrité de l'archive"
# On corrompt l'archive sans toucher au manifeste : l'empreinte ne doit plus
# correspondre et le dossier doit être rejeté, pas importé.
rm -rf "$LAB/state"; mkdir -p "$LAB/state"/{done,retry}
head -c 20000 /dev/urandom > "$LAB/art/sonar-projects-to-migrate/espace12/com.entite_autorise.zip"
out="$(run --manifest espace12/com.entite_autorise.manifest.json)"
grep -qi "empreinte\|sha256" <<<"$out" && pass "empreinte divergente détectée" || fail "empreinte divergente détectée"

head2 "11. Codes de sortie"
( flock -n 9 || exit 1; sleep 3 ) 9>"$LAB/lock" & CONC=$!
sleep 0.4
lockout="$(run || true)"
grep -qi "en cours" <<<"$lockout" && pass "verrou concurrent" || fail "verrou concurrent"
wait "$CONC" 2>/dev/null

stop_mock
run >/dev/null 2>&1; [[ "$?" == 2 ]] && pass "SonarQube injoignable → 2" || fail "SonarQube injoignable → 2"

start_mock
sed -i.bak "s|^ARTIFACTORY_URL=.*|ARTIFACTORY_URL=\"http://127.0.0.1:19999/artifactory\"|" "$LAB/config.sh"
run >/dev/null 2>&1; [[ "$?" == 3 ]] && pass "Artifactory injoignable → 3" || fail "Artifactory injoignable → 3"

# --------------------------------------------------------------------------- #
#  Mode cluster : --prepare puis --commit                                      #
# --------------------------------------------------------------------------- #

# Remet le laboratoire à neuf : instance, état local, dépôts, répertoire d'import.
remise_a_zero() {
  stop_mock
  rm -rf "$LAB/state" "$LAB/work" "$LAB/importdir" "$LAB/art" "$LAB/actions.log"
  mkdir -p "$LAB/state"/{done,retry} "$LAB/work" "$LAB/importdir"
  mkdir -p "$LAB"/art/sonar-projects-to-migrate/espace12 "$LAB"/art/sonar-projects-migrated
  reset_state
  sed -i.bak "s|^ARTIFACTORY_URL=.*|ARTIFACTORY_URL=\"http://127.0.0.1:$PORT/artifactory\"|" \
      "$LAB/config.sh"
  start_mock
}

head2 "12. Phase --prepare — tout contrôler sans rien écrire"
remise_a_zero
mk cluster "com.entite:cluster" "p-espace12-mapipeline" "2026.2.1" "$PLUG_OK"
out="$(run --prepare)"
[[ -z "$(actions)" ]]                            && pass "instance intacte"              || fail "instance intacte"
[[ -f "$LAB/importdir/com.entite:cluster.zip" ]] && pass "archive déposée dans import/"  || fail "archive déposée dans import/"
[[ -f "$LAB/work/pending.json" ]]                && pass "descripteur écrit"             || fail "descripteur écrit"
[[ -z "$(ls -A "$LAB/state/done")" ]]            && pass "rien marqué comme fait"        || fail "rien marqué comme fait"

head2 "13. Le descripteur est le contrat lu par Ansible"
p="$LAB/work/pending.json"
[[ "$(jq -r '.items[0].target_key'  "$p" 2>/dev/null)" == "p-espace12-mapipeline" ]] \
  && pass "clé cible portée"    || fail "clé cible portée"
[[ "$(jq -r '.items[0].source_key'  "$p" 2>/dev/null)" == "com.entite:cluster" ]] \
  && pass "clé source portée"   || fail "clé source portée"
[[ "$(jq -r '.items[0].local_path'  "$p" 2>/dev/null)" == "$LAB/importdir/com.entite:cluster.zip" ]] \
  && pass "chemin à recopier porté" || fail "chemin à recopier porté"
[[ "$(jq -r '.items[0].scm_repository' "$p" 2>/dev/null)" == "app1234/cluster" ]] \
  && pass "dépôt SCM porté"     || fail "dépôt SCM porté"

head2 "14. MAX_BATCH borne la préparation"
[[ "$(jq '.items | length' "$p")" == "1" ]] && pass "un seul projet préparé" || fail "un seul projet préparé"

head2 "15. Phase --commit — même résultat que le mode monolithique"
out="$(run --commit)"
grep -q "MIGRATION TERMINEE.*p-espace12-mapipeline" <<<"$out" && pass "migration terminée" || fail "migration terminée"
a="$(actions)"
i=$(grep -n '^IMPORT' <<<"$a" | head -1 | cut -d: -f1)
d=$(grep -n '^DELETE' <<<"$a" | head -1 | cut -d: -f1)
r=$(grep -n '^RENAME' <<<"$a" | head -1 | cut -d: -f1)
[[ -n "$i" && -n "$d" && "$i" -lt "$d" ]] && pass "import AVANT suppression"     || fail "import AVANT suppression"
[[ -n "$d" && -n "$r" && "$d" -lt "$r" ]] && pass "suppression AVANT renommage" || fail "suppression AVANT renommage"
grep -q "TEMPLATE" <<<"$a" && pass "reconfiguration appliquée" || fail "reconfiguration appliquée"
[[ ! -f "$LAB/importdir/com.entite:cluster.zip" ]] && pass "archive locale nettoyée" || fail "archive locale nettoyée"
[[ ! -f "$p" ]] && pass "descripteur consommé" || fail "descripteur consommé"
[[ "$(n_done)" == "2" ]] && pass "artefacts archivés" || fail "artefacts archivés"

head2 "16. La fenêtre entre les deux phases est surveillée"
# Le scénario redouté : une CI analyse le projet cible pendant qu'Ansible
# recopie l'archive. Le commit doit refuser plutôt qu'écraser.
remise_a_zero
mk fenetre "com.entite:fenetre" "p-espace12-mapipeline" "2026.2.1" "$PLUG_OK"
run --prepare >/dev/null 2>&1
[[ -f "$LAB/work/pending.json" ]] && pass "préparation faite" || fail "préparation faite"

stop_mock
python3 - "$LAB" <<'PY'
import json, sys
p = sys.argv[1] + "/state.json"
s = json.load(open(p))
s["projects"]["p-espace12-mapipeline"] = {"analyses": 3}   # une CI est passée
json.dump(s, open(p, "w"), indent=2)
PY
start_mock
rm -f "$LAB/actions.log"
out="$(run --commit)"
grep -q "n est plus vide" <<<"$out" && pass "commit refuse le projet rempli" || fail "commit refuse le projet rempli"
[[ -z "$(actions)" ]] && pass "aucun import déclenché"     || fail "aucun import déclenché"
[[ ! -f "$LAB/work/pending.json" ]] && pass "descripteur consommé malgré le refus" \
                                     || fail "descripteur consommé malgré le refus"

head2 "17. --commit sans descripteur ne fait rien"
rm -f "$LAB/actions.log"
out="$(run --commit)"
grep -qi "aucun descripteur" <<<"$out" && pass "sortie propre" || fail "sortie propre"
[[ -z "$(actions)" ]] && pass "instance intacte" || fail "instance intacte"

head2 "18. --commit et --dry-run sont incompatibles"
out="$(run --commit --dry-run || true)"
grep -qi "incompatibles" <<<"$out" && pass "combinaison refusée" || fail "combinaison refusée"

stop_mock

printf '\n\033[1mRésultat : %d réussis, %d échoués\033[0m\n' "$OK" "$KO"
exit $(( KO > 0 ? 1 : 0 ))
