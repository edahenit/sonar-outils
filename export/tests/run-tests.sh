#!/usr/bin/env bash
#
# Banc d'essai de sonar-export-publisher.
#
# Lance un faux SonarQube et un faux Artifactory sur 127.0.0.1, puis déroule
# dix scénarios. Aucune instance réelle n'est touchée.
#
#   ./run-tests.sh
#
# Utile avant toute modification du script, et pour valider une adaptation
# (format de clé, nom de lien, type de tâche CE) sans risque.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../sonar-export-publisher.sh"
LAB="$(mktemp -d /tmp/sep-lab.XXXXXX)"
PORT="${PORT:-18080}"
MOCK_PID=""

OK=0; KO=0
pass() { printf '  \033[32mOK\033[0m   %s\n' "$1"; OK=$((OK+1)); }
fail() { printf '  \033[31mKO\033[0m   %s\n' "$1"; KO=$((KO+1)); }
head2() { printf '\n\033[1m%s\033[0m\n' "$1"; }

cleanup() {
  [[ -n "$MOCK_PID" ]] && kill "$MOCK_PID" 2>/dev/null
  wait "$MOCK_PID" 2>/dev/null
  rm -rf "$LAB"
}
trap cleanup EXIT

# --------------------------------------------------------------------------- #
#  Mise en place                                                               #
# --------------------------------------------------------------------------- #

for b in curl jq python3 sha256sum flock; do
  command -v "$b" >/dev/null || { echo "commande absente : $b"; exit 1; }
done

mkdir -p "$LAB"/{export,state/done,state/retry,quarantine,artifactory}
cp "$HERE/scenario.json" "$LAB/scenario.json"

# Le mock lit /tmp/lab/scenario.json et écrit dans /tmp/lab/artifactory :
# on le recopie en adaptant ces chemins au répertoire temporaire du test.
sed -e "s|/tmp/lab/artifactory|$LAB/artifactory|" \
    -e "s|/tmp/lab/scenario.json|$LAB/scenario.json|" \
    -e "s|/tmp/lab/upload_order.log|$LAB/upload_order.log|" \
    "$HERE/mock_sonar.py" > "$LAB/mock.py"

# Archives factices — une par projet du scénario, sauf « archive-absente »
for k in "com.entite:mon-projet" "com.entite:sans-lien" "com.entite:mauvais-host" \
         "com.entite:mauvaise-cle" "com.entite:compte-local" "com.entite:cle-encodee"; do
  head -c 20000 /dev/urandom > "$LAB/export/${k}.zip"
done

cat > "$LAB/config.sh" <<CFG
SONAR_URL="http://127.0.0.1:$PORT"
SONAR_TOKEN="faux"
SONAR_EDITION="enterprise"
EXPORT_DIR="$LAB/export"
TARGET_HOST="sonar-centrale.groupe.corp"
TARGET_KEY_REGEX='^p-([A-Za-z0-9]+)-([A-Za-z0-9._-]+)\$'
LINK_NAME="MIGRATION"
ARTIFACTORY_URL="http://127.0.0.1:$PORT/artifactory"
ARTIFACTORY_REPO="sonar-projects-to-migrate"
ARTIFACTORY_TOKEN="faux"
STATE_DIR="$LAB/state"
QUARANTINE_DIR="$LAB/quarantine"
LOCK_FILE="$LAB/lock"
LOOKBACK_HOURS=24
MAX_ATTEMPTS=2
STABILITY_SECONDS=1
HTTP_TIMEOUT=10
CE_TASK_TYPE="PROJECT_EXPORT"
LOG_LEVEL="INFO"
CFG

start_mock() {
  python3 "$LAB/mock.py" "$1" & MOCK_PID=$!
  sleep 1.5
}
stop_mock() { kill "$MOCK_PID" 2>/dev/null; wait "$MOCK_PID" 2>/dev/null; MOCK_PID=""; }

run()      { "$SCRIPT" -c "$LAB/config.sh" "$@" 2>&1; }
n_files()  { find "$LAB/artifactory" -type f 2>/dev/null | wc -l | tr -d ' '; }

# --------------------------------------------------------------------------- #
#  Scénarios                                                                   #
# --------------------------------------------------------------------------- #

echo "Banc d'essai — $LAB"
start_mock "$PORT"

head2 "1. Syntaxe"
bash -n "$SCRIPT" && pass "bash -n" || fail "bash -n"

head2 "2. Essai à blanc — détection et rejets"
out="$(run --dry-run)"
grep -q "T-OK-1.*p-espace12-mapipeline"        <<<"$out" && pass "lien valide reconnu"        || fail "lien valide reconnu"
grep -q "T-NOLINK.*pas de lien"                <<<"$out" && pass "absence de lien détectée"   || fail "absence de lien détectée"
grep -q "T-BADHOST.*host inattendu"            <<<"$out" && pass "host étranger rejeté"       || fail "host étranger rejeté"
grep -q "T-BADKEY.*hors format"                <<<"$out" && pass "clé mal formée rejetée"     || fail "clé mal formée rejetée"
grep -q "T-LOCAL.*compte local"                <<<"$out" && pass "compte local refusé"        || fail "compte local refusé"
grep -q "T-NOFILE.*archive absente"            <<<"$out" && pass "archive absente → réessai"  || fail "archive absente → réessai"
grep -q "T-ENC.*p-espace99-mon.pipeline"       <<<"$out" && pass "clé encodée + ancre + espaces" \
                                                          || fail "clé encodée + ancre + espaces"

head2 "3. Essai à blanc sans effet de bord"
[[ -z "$(ls -A "$LAB/state/done")"  ]] && pass "aucun état écrit"        || fail "aucun état écrit"
[[ -z "$(ls -A "$LAB/state/retry")" ]] && pass "aucun réessai compté"    || fail "aucun réessai compté"
[[ "$(n_files)" == "0" ]]              && pass "aucun dépôt Artifactory" || fail "aucun dépôt Artifactory"
[[ "$(ls -1 "$LAB/export" | wc -l | tr -d ' ')" == "6" ]] \
   && pass "aucune archive supprimée" || fail "aucune archive supprimée"

head2 "4. Publication réelle"
out="$(run)"
[[ "$(n_files)" == "4" ]] && pass "2 archives + 2 manifestes déposés" || fail "2 archives + 2 manifestes déposés"
grep -q "T-OK-1.*archive locale supprimée" <<<"$out" && pass "ménage local" || fail "ménage local"

head2 "5. Le manifeste part APRÈS l'archive"
order="$(cat "$LAB/upload_order.log" 2>/dev/null)"
a=$(grep -n 'espace12.*\.zip$'           <<<"$order" | cut -d: -f1)
m=$(grep -n 'espace12.*manifest\.json$'  <<<"$order" | cut -d: -f1)
[[ -n "$a" && -n "$m" && "$a" -lt "$m" ]] && pass "ordre respecté" || fail "ordre respecté"

head2 "6. Intégrité des empreintes"
bad=0
while read -r man; do
  z="${man%.manifest.json}.zip"
  [[ "$(jq -r '.archive.sha256' "$man")" == "$(sha256sum "$z" | cut -d' ' -f1)" ]] || bad=1
done < <(find "$LAB/artifactory" -name '*.manifest.json')
[[ "$bad" == 0 ]] && pass "sha256 conformes" || fail "sha256 conformes"

head2 "7. Contenu du manifeste"
man="$(find "$LAB/artifactory" -name '*mon-projet.manifest.json' | head -1)"
[[ "$(jq -r '.request.by.external_identity' "$man")" == "u123456" ]] \
  && pass "externalIdentity présent" || fail "externalIdentity présent"
[[ "$(jq -r '.request.declared_target_project_key' "$man")" == "p-espace12-mapipeline" ]] \
  && pass "clé cible déclarée" || fail "clé cible déclarée"
[[ "$(jq -r '.source.export_task_id' "$man")" == "T-OK-1" ]] \
  && pass "identifiant de tâche tracé" || fail "identifiant de tâche tracé"

head2 "8. Idempotence"
before="$(n_files)"; run >/dev/null 2>&1; after="$(n_files)"
[[ "$before" == "$after" ]] && pass "aucune republication" || fail "aucune republication"

head2 "9. Quarantaine après MAX_ATTEMPTS"
echo 2 > "$LAB/state/retry/T-NOFILE"
run >/dev/null 2>&1
grep -q quarantined "$LAB/state/done/T-NOFILE" 2>/dev/null \
  && pass "mise en quarantaine" || fail "mise en quarantaine"

head2 "10. Codes de sortie"
# On tient le verrou nous-mêmes plutôt que de compter sur la durée d'un cycle :
# une fois l'état rempli, un cycle dure quelques millisecondes et le test
# deviendrait aléatoire.
# On capture avant de filtrer : avec « pipefail », le code 4 du script
# remonterait à travers le tube et fausserait le test.
( flock -n 9 || exit 1; sleep 3 ) 9>"$LAB/lock" & CONC=$!
sleep 0.4
lockout="$(run || true)"
grep -q "déjà en cours" <<<"$lockout" && pass "verrou concurrent" || fail "verrou concurrent"
wait "$CONC" 2>/dev/null

stop_mock
run >/dev/null 2>&1; [[ "$?" == 2 ]] && pass "SonarQube injoignable → 2" || fail "SonarQube injoignable → 2"

start_mock "$PORT"
sed -i "s|^ARTIFACTORY_URL=.*|ARTIFACTORY_URL=\"http://127.0.0.1:19999/artifactory\"|" "$LAB/config.sh"
run >/dev/null 2>&1; [[ "$?" == 3 ]] && pass "Artifactory injoignable → 3" || fail "Artifactory injoignable → 3"

printf '\n\033[1mRésultat : %d réussis, %d échoués\033[0m\n' "$OK" "$KO"
exit $(( KO > 0 ))
