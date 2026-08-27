#!/usr/bin/env bash
#
# sonar-export-publisher — version shell
# =====================================
#
# Publie vers Artifactory les exports de projets SonarQube destinés à la
# migration vers l'instance centrale.
#
# Principe
#   Le script ne scanne PAS le répertoire d'export : il part de la liste des
#   tâches Compute Engine de type PROJECT_EXPORT. Une tâche porte l'identité de
#   la personne qui a lancé l'export, ce qu'un fichier ne fait pas.
#
#   Un export n'est considéré comme une demande de migration que si le projet
#   source porte un lien nommé « MIGRATION » pointant vers le dashboard du
#   projet cible. Poser ce lien exige le droit Administer sur le projet : c'est
#   le premier contrôle d'habilitation, et il est gratuit.
#
#   L'archive est publiée d'abord, le manifeste ensuite. Le script central ne
#   réagit qu'au manifeste : il ne peut donc jamais traiter une archive
#   incomplète.
#
# Dépendances : bash 4+, curl, jq, sha256sum, flock, awk
#
# Usage
#   sonar-export-publisher.sh -c /etc/sonar-export-publisher/config.sh
#   sonar-export-publisher.sh -c ... --dry-run
#   sonar-export-publisher.sh -c ... --task AY8xxxx
#
# Codes de sortie
#   0  cycle terminé      2  SonarQube injoignable
#   1  configuration      3  Artifactory injoignable      4  verrou déjà pris
#

set -euo pipefail

# --------------------------------------------------------------------------- #
#  Valeurs par défaut — surchargeables dans le fichier de configuration        #
# --------------------------------------------------------------------------- #

SONAR_URL=""
SONAR_EDITION="enterprise"
EXPORT_DIR=""
TARGET_HOST=""
TARGET_KEY_REGEX='^p-([A-Za-z0-9]+)-([A-Za-z0-9._-]+)$'
LINK_NAME="MIGRATION"
ARTIFACTORY_URL=""
ARTIFACTORY_REPO="sonar-projects-to-migrate"
STATE_DIR="/var/lib/sonar-export-publisher"
QUARANTINE_DIR="/var/lib/sonar-export-publisher/quarantine"
LOCK_FILE="/var/lock/sonar-export-publisher.lock"
LOOKBACK_HOURS=24
MAX_ATTEMPTS=6
STABILITY_SECONDS=5
HTTP_TIMEOUT=30
CE_TASK_TYPE="PROJECT_EXPORT"     # à vérifier sur votre version
LOG_LEVEL="INFO"

DRY_RUN=0
ONLY_TASK=""
CONFIG=""

# --------------------------------------------------------------------------- #
#  Journalisation                                                              #
# --------------------------------------------------------------------------- #

log()  { printf '%s %-7s %s\n' "$(date -Is)" "$1" "${*:2}" >&2; }
info() { log INFO  "$@"; }
warn() { log WARN  "$@"; }
err()  { log ERROR "$@"; }
dbg()  { [[ "$LOG_LEVEL" == "DEBUG" ]] && log DEBUG "$@" || true; }

die()  { err "$1"; exit "${2:-1}"; }

# --------------------------------------------------------------------------- #
#  Arguments                                                                   #
# --------------------------------------------------------------------------- #

while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--config) CONFIG="$2"; shift 2 ;;
    --dry-run)   DRY_RUN=1; shift ;;
    --task)      ONLY_TASK="$2"; shift 2 ;;
    -h|--help)   sed -n '2,40p' "$0"; exit 0 ;;
    *)           die "argument inconnu : $1" ;;
  esac
done

[[ -n "$CONFIG" ]] || die "usage : $0 -c /chemin/config.sh"
[[ -r "$CONFIG"  ]] || die "configuration illisible : $CONFIG"
# shellcheck disable=SC1090
source "$CONFIG"

for bin in curl jq sha256sum flock awk; do
  command -v "$bin" >/dev/null || die "commande absente : $bin"
done

# Attention : ne jamais mettre d'apostrophe dans un message ${VAR:?...},
# le parseur de bash s'y perd. D'où cette vérification explicite.
for v in SONAR_URL SONAR_TOKEN EXPORT_DIR TARGET_HOST \
         ARTIFACTORY_URL ARTIFACTORY_TOKEN; do
  [[ -n "${!v:-}" ]] || die "$v non défini (fichier de configuration ou variable d environnement)"
done
unset v

SONAR_URL="${SONAR_URL%/}"
ARTIFACTORY_URL="${ARTIFACTORY_URL%/}"

mkdir -p "$STATE_DIR/done" "$STATE_DIR/retry" "$QUARANTINE_DIR"

# --------------------------------------------------------------------------- #
#  Verrou — un seul cycle à la fois                                            #
# --------------------------------------------------------------------------- #
# Un export volumineux dépasse facilement l'intervalle de la minuterie.

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  warn "un cycle est déjà en cours, on sort"
  exit 4
fi

# --------------------------------------------------------------------------- #
#  Appels HTTP                                                                 #
# --------------------------------------------------------------------------- #

sq() {   # sq <chemin+querystring>
  curl --fail --silent --show-error --max-time "$HTTP_TIMEOUT" \
       -u "${SONAR_TOKEN}:" "${SONAR_URL}$1"
}

art_put() {   # art_put <fichier local> <espace_id> [sha256]
  local file="$1" espace="$2" sha="${3:-}"
  local url="${ARTIFACTORY_URL}/${ARTIFACTORY_REPO}/${espace}/$(basename "$file")"
  local -a hdr=(-H "Authorization: Bearer ${ARTIFACTORY_TOKEN}")
  # Artifactory valide le checksum au dépôt : un transfert tronqué est rejeté
  # tout de suite, pas découvert trois heures plus tard.
  [[ -n "$sha" ]] && hdr+=(-H "X-Checksum-Sha256: ${sha}")
  curl --fail --silent --show-error --max-time $((HTTP_TIMEOUT * 20)) \
       "${hdr[@]}" -T "$file" "$url" >/dev/null
  printf '%s\n' "$url"
}

# --------------------------------------------------------------------------- #
#  Décodage et validation du lien MIGRATION                                    #
# --------------------------------------------------------------------------- #

urldecode() {
  # Une clé collée depuis le navigateur arrive encodée : les « : » deviennent
  # %3A. Sans ce décodage, la clé cible est fausse et l'erreur n'apparaît qu'à
  # l'import, côté central.
  local s="${1//+/ }"
  printf '%b' "${s//%/\\x}"
}

url_host() {
  local u="${1#*://}"          # retire le schéma
  u="${u%%/*}"                 # retire le chemin
  u="${u%%\?*}"                # au cas où il n'y aurait pas de chemin
  u="${u##*@}"                 # retire un éventuel user:pass@
  u="${u%%:*}"                 # retire le port
  printf '%s' "${u,,}"         # minuscules
}

url_param_id() {
  local q="${1#*\?}"           # querystring
  [[ "$q" == "$1" ]] && { printf ''; return; }   # pas de « ? » dans l'URL
  q="${q%%#*}"                 # retire l'ancre
  local kv
  IFS='&' read -ra kv <<< "$q"
  local pair
  for pair in "${kv[@]}"; do
    [[ "${pair%%=*}" == "id" ]] && { urldecode "${pair#*=}"; return; }
  done
  printf ''
}

# parse_link <url> → écrit "clé_cible espace_id" sur stdout, ou échoue
parse_link() {
  local url host key
  url="$(printf '%s' "$1" | awk '{$1=$1};1')"     # trim

  host="$(url_host "$url")"
  [[ "$host" == "${TARGET_HOST,,}" ]] \
    || { printf 'host inattendu « %s », attendu « %s »' "$host" "$TARGET_HOST" >&2
         return 1; }

  key="$(url_param_id "$url")"
  key="$(printf '%s' "$key" | awk '{$1=$1};1')"
  [[ -n "$key" ]] || { printf 'paramètre « id » absent de l'\''URL' >&2; return 1; }

  [[ "$key" =~ $TARGET_KEY_REGEX ]] \
    || { printf 'clé cible « %s » hors format attendu' "$key" >&2; return 1; }

  printf '%s %s' "$key" "${BASH_REMATCH[1]}"
}

# --------------------------------------------------------------------------- #
#  Localisation et stabilité de l'archive                                      #
# --------------------------------------------------------------------------- #

find_archive() {
  # SonarQube nomme l'archive d'après la clé du projet ; les caractères
  # spéciaux peuvent être substitués selon la version, d'où les variantes.
  local key="$1" cand
  for cand in \
      "$key" \
      "${key//:/_}" \
      "${key//:/-}" \
      "$(printf '%s' "$key" | tr -c 'A-Za-z0-9._-' '_')" ; do
    [[ -f "$EXPORT_DIR/${cand}.zip" ]] && { printf '%s' "$EXPORT_DIR/${cand}.zip"; return 0; }
  done
  return 1
}

is_stable() {
  # Un fichier encore en cours d'écriture par SonarQube changera de taille.
  local f="$1" a b
  a=$(stat -c %s "$f" 2>/dev/null || echo 0)
  sleep "$STABILITY_SECONDS"
  b=$(stat -c %s "$f" 2>/dev/null || echo -1)
  [[ "$a" == "$b" && "$a" -gt 0 ]]
}

# --------------------------------------------------------------------------- #
#  État local — idempotence                                                    #
# --------------------------------------------------------------------------- #

state_is_done() { [[ -f "$STATE_DIR/done/$1" ]]; }
state_attempts() { cat "$STATE_DIR/retry/$1" 2>/dev/null || echo 0; }

state_finish() {   # state_finish <task_id> <statut> <détail>
  [[ "$DRY_RUN" == "1" ]] && return 0        # un essai à blanc n'écrit rien
  printf '%s %s\n' "$2" "$3" > "$STATE_DIR/done/$1"
  rm -f "$STATE_DIR/retry/$1"
}

state_retry() {    # state_retry <task_id>
  local n; n=$(( $(state_attempts "$1") + 1 ))
  [[ "$DRY_RUN" == "1" ]] || printf '%s\n' "$n" > "$STATE_DIR/retry/$1"
  printf '%s\n' "$n"
}

# --------------------------------------------------------------------------- #
#  Faits techniques de l'instance — calculés une fois par cycle                #
# --------------------------------------------------------------------------- #

SQ_VERSION=""
SQ_PLUGINS_FP=""

collect_instance_facts() {
  # Vérifications explicites : dans « f || die », bash désactive errexit
  # à l'intérieur de f. Un échec silencieux donnerait un message trompeur
  # cinquante lignes plus loin.
  local v plugins
  v="$(sq "/api/server/version" | tr -d '[:space:]')" || return 1
  [[ -n "$v" ]] || return 1
  SQ_VERSION="$v"

  plugins="$(sq "/api/plugins/installed")" || return 1
  SQ_PLUGINS_FP="sha256:$(
    jq -r '.plugins[]? | "\(.key):\(.version // "")"' <<< "$plugins" \
      | LC_ALL=C sort | paste -sd'|' - | sha256sum | cut -d' ' -f1
  )"
  dbg "version=$SQ_VERSION plugins=$SQ_PLUGINS_FP"
  return 0
}

# --------------------------------------------------------------------------- #
#  Identité du demandeur                                                       #
# --------------------------------------------------------------------------- #

# resolve_user <login> → JSON de l'utilisateur, ou échec
resolve_user() {
  local login="$1" enc json
  enc="$(jq -rn --arg v "$login" '$v|@uri')"

  # API v2, présente sur les versions récentes
  json="$(curl --fail --silent --max-time "$HTTP_TIMEOUT" -u "${SONAR_TOKEN}:" \
        "${SONAR_URL}/api/v2/users-management/users?q=${enc}&pageSize=50" 2>/dev/null || true)"
  if [[ -n "$json" ]]; then
    local hit
    hit="$(jq -c --arg l "$login" '
      [.users[]? | select(.login == $l)][0] // empty
      | { login, name, email,
          external_identity: (.externalLogin // .externalIdentity),
          external_provider: .externalProvider,
          local: (.local // false),
          managed: (.managed // false),
          active: (.active // true),
          api: "v2" }' <<< "$json")"
    [[ -n "$hit" ]] && { printf '%s' "$hit"; return 0; }
  fi

  # Repli API v1
  json="$(sq "/api/users/search?q=${enc}&ps=50")" || return 1
  jq -ce --arg l "$login" '
    [.users[]? | select(.login == $l)][0] // empty
    | { login, name, email,
        external_identity: .externalIdentity,
        external_provider: .externalProvider,
        local: (.local // false),
        managed: false,
        active: (.active // true),
        api: "v1" }' <<< "$json"
}

# --------------------------------------------------------------------------- #
#  Mise en quarantaine                                                         #
# --------------------------------------------------------------------------- #

quarantine() {   # quarantine <task_id> <project_key>
  [[ "$DRY_RUN" == "1" ]] && return 0
  local archive
  if archive="$(find_archive "$2")"; then
    mv -f "$archive" "$QUARANTINE_DIR/$1_$(basename "$archive")"
    err "[$1] archive mise en quarantaine : $QUARANTINE_DIR/$1_$(basename "$archive")"
  fi
}

# --------------------------------------------------------------------------- #
#  Traitement d'une tâche                                                      #
# --------------------------------------------------------------------------- #
# Codes de retour : 0 publié · 10 ignoré définitivement · 20 à réessayer

process_task() {
  local task_json="$1"
  local tid pkey submitter
  tid="$(jq -r '.id' <<< "$task_json")"
  pkey="$(jq -r '.componentKey // empty' <<< "$task_json")"
  submitter="$(jq -r '.submitterLogin // empty' <<< "$task_json")"

  [[ -n "$pkey" ]] || { REASON="tâche sans componentKey"; return 10; }

  # --- 1. la déclaration : le lien MIGRATION ------------------------------ #
  local links link_url
  links="$(sq "/api/project_links/search?projectKey=$(jq -rn --arg v "$pkey" '$v|@uri')")" \
    || { REASON="lecture des liens impossible"; return 20; }

  link_url="$(jq -r --arg n "${LINK_NAME^^}" '
      [ .links[]?
        | select( ((.name // "")
                   | ascii_upcase
                   | sub("^[[:space:]]+";"")
                   | sub("[[:space:]]+$";"")) == $n )
      ][0].url // empty
    ' <<< "$links")"

  [[ -n "$link_url" ]] || { REASON="pas de lien « $LINK_NAME » sur $pkey"; return 10; }

  local parsed target_key espace_id
  if ! parsed="$(parse_link "$link_url" 2>/tmp/pl.$$)"; then
    REASON="$(cat /tmp/pl.$$)"; rm -f /tmp/pl.$$; return 10
  fi
  rm -f /tmp/pl.$$
  read -r target_key espace_id <<< "$parsed"
  info "[$tid] $pkey → $target_key (espace $espace_id)"

  # --- 2. l'identité du demandeur ----------------------------------------- #
  [[ -n "$submitter" ]] \
    || { REASON="tâche sans submitterLogin"; return 10; }

  local user
  user="$(resolve_user "$submitter")" \
    || { REASON="utilisateur « $submitter » non résolu"; return 20; }

  if [[ "$(jq -r '.local' <<< "$user")" == "true" ]]; then
    REASON="« $submitter » est un compte local — une migration doit être demandée par une personne"
    return 10
  fi

  # --- 3. l'archive -------------------------------------------------------- #
  local archive
  archive="$(find_archive "$pkey")" \
    || { REASON="archive absente du répertoire d'export"; return 20; }

  is_stable "$archive" \
    || { REASON="archive encore en cours d'écriture"; return 20; }

  local sha size
  sha="$(sha256sum "$archive" | cut -d' ' -f1)"
  size="$(stat -c %s "$archive")"

  # --- 4. le manifeste ----------------------------------------------------- #
  local manifest
  manifest="$(jq -n \
    --arg rid       "$(cat /proc/sys/kernel/random/uuid)" \
    --arg at        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --argjson by    "$user" \
    --arg target    "$target_key" \
    --arg espace    "$espace_id" \
    --arg inst      "$SONAR_URL" \
    --arg pkey      "$pkey" \
    --arg ver       "$SQ_VERSION" \
    --arg ed        "$SONAR_EDITION" \
    --arg fp        "$SQ_PLUGINS_FP" \
    --arg tid       "$tid" \
    --arg texec     "$(jq -r '.executedAt // ""' <<< "$task_json")" \
    --arg fname     "$(basename "$archive")" \
    --arg sha       "$sha" \
    --argjson size  "$size" \
    '{
      schema_version: "1.0",
      request: {
        id: $rid,
        at: $at,
        by: ($by + { resolved_from: "ce_task.submitterLogin" }),
        declared_target_project_key: $target,
        espace_id: $espace
      },
      source: {
        instance: $inst,
        project_key: $pkey,
        sonar_version: $ver,
        edition: $ed,
        plugins_fingerprint: $fp,
        export_task_id: $tid,
        export_executed_at: $texec
      },
      archive: { filename: $fname, sha256: $sha, size_bytes: $size }
    }')"

  if [[ "$DRY_RUN" == "1" ]]; then
    info "[$tid] DRY-RUN — manifeste qui aurait été publié :"
    jq . <<< "$manifest" >&2
    return 0
  fi

  # --- 5. publication : archive d'abord, manifeste ensuite ----------------- #
  local url
  url="$(art_put "$archive" "$espace_id" "$sha")" \
    || { REASON="échec du dépôt de l'archive"; return 20; }
  info "[$tid] archive publiée : $url"

  local mpath="${archive%.zip}.manifest.json"
  printf '%s\n' "$manifest" > "$mpath"
  # Le manifeste EN DERNIER : le script central ne réagit qu'à lui, il ne peut
  # donc jamais traiter une archive incomplète.
  if ! art_put "$mpath" "$espace_id" >/dev/null; then
    rm -f "$mpath"; REASON="échec du dépôt du manifeste"; return 20
  fi
  rm -f "$mpath"
  info "[$tid] manifeste publié"

  # --- 6. ménage ------------------------------------------------------------ #
  rm -f "$archive"
  info "[$tid] archive locale supprimée"
  return 0
}

# --------------------------------------------------------------------------- #
#  Cycle                                                                       #
# --------------------------------------------------------------------------- #

main() {
  collect_instance_facts || die "instance SonarQube injoignable" 2

  if [[ "$DRY_RUN" != "1" ]]; then
    curl --fail --silent --max-time "$HTTP_TIMEOUT" \
         -H "Authorization: Bearer ${ARTIFACTORY_TOKEN}" \
         "${ARTIFACTORY_URL}/api/system/ping" >/dev/null \
      || die "Artifactory injoignable" 3
  fi

  local since
  since="$(date -u -d "-${LOOKBACK_HOURS} hours" +%Y-%m-%d)"

  # Pagination de api/ce/activity
  local page=1 total=0 pagesize=100 tasks all='[]'
  while : ; do
    local resp
    resp="$(sq "/api/ce/activity?type=${CE_TASK_TYPE}&status=SUCCESS&minSubmittedAt=${since}&ps=${pagesize}&p=${page}")" \
      || die "lecture des tâches impossible" 2
    tasks="$(jq -c '.tasks // []' <<< "$resp")"
    all="$(jq -c --argjson a "$all" --argjson b "$tasks" -n '$a + $b')"
    total="$(jq -r '.paging.total // 0' <<< "$resp")"
    (( page * pagesize >= total )) && break
    page=$(( page + 1 ))
  done

  local n; n="$(jq 'length' <<< "$all")"
  info "$n tâche(s) d'export sur les ${LOOKBACK_HOURS} dernières heures"

  local i task tid rc
  for (( i = 0; i < n; i++ )); do
    task="$(jq -c ".[$i]" <<< "$all")"
    tid="$(jq -r '.id' <<< "$task")"

    [[ -n "$ONLY_TASK" && "$tid" != "$ONLY_TASK" ]] && continue
    [[ -z "$ONLY_TASK" ]] && state_is_done "$tid" && continue

    REASON=""
    rc=0
    process_task "$task" || rc=$?

    case "$rc" in
      0)
        [[ "$DRY_RUN" == "1" ]] || state_finish "$tid" published ok
        ;;
      10)
        info "[$tid] ignoré : $REASON"
        state_finish "$tid" ignored "$REASON"
        ;;
      20)
        local att; att="$(state_retry "$tid")"
        if (( att >= MAX_ATTEMPTS )); then
          err "[$tid] abandon après $att tentatives : $REASON"
          quarantine "$tid" "$(jq -r '.componentKey // empty' <<< "$task")"
          state_finish "$tid" quarantined "$REASON"
        else
          warn "[$tid] tentative $att/$MAX_ATTEMPTS : $REASON"
        fi
        ;;
      *)
        err "[$tid] erreur inattendue (code $rc)"
        state_retry "$tid" >/dev/null
        ;;
    esac
  done

  return 0
}

main
