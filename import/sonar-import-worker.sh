#!/usr/bin/env bash
#
# sonar-import-worker — version shell
# ==================================
#
# Importe sur l'instance SonarQube CENTRALE les projets publiés dans Artifactory
# par sonar-export-publisher.
#
# Principe
#   Le worker ne réagit qu'aux MANIFESTES. Une archive sans manifeste est
#   ignorée : c'est ce qui garantit qu'il ne traite jamais un dépôt incomplet.
#
#   Tous les contrôles ont lieu AVANT la moindre écriture. Puis l'ordre des
#   opérations est : importer → confirmer → supprimer → renommer. Rien n'est
#   détruit tant que l'import n'a pas réussi.
#
# Dépendances : bash 4+, curl, jq, sha256sum, flock
#
# Modes d'exécution
#   (défaut)    tout le cycle en une fois — instance centrale à UN SEUL nœud
#   --prepare   contrôles + téléchargement + dépôt dans le répertoire d'import,
#               puis écriture d'un descripteur de travail. N'écrit RIEN sur
#               l'instance.
#   --commit    rejoue le contrôle « projet vide », puis importe, supprime,
#               renomme, reconfigure et archive.
#
#   Le découpage existe pour les instances en cluster : entre les deux phases,
#   Ansible recopie l'archive sur tous les nœuds applicatifs. La tâche Compute
#   Engine étant planifiée sur un nœud imprévisible, c'est la seule façon de
#   garantir qu'elle trouve le fichier.
#
# Usage
#   sonar-import-worker.sh -c /etc/sonar-import-worker/config.sh
#   sonar-import-worker.sh -c ... --dry-run
#   sonar-import-worker.sh -c ... --manifest espace12/projet.manifest.json
#   sonar-import-worker.sh -c ... --prepare
#   sonar-import-worker.sh -c ... --commit
#
# Codes de sortie
#   0  cycle terminé      2  SonarQube injoignable
#   1  configuration      3  Artifactory injoignable      4  verrou déjà pris
#

set -uo pipefail

# --------------------------------------------------------------------------- #
#  Valeurs par défaut                                                          #
# --------------------------------------------------------------------------- #

SONAR_URL=""
SONAR_EDITION="enterprise"
IMPORT_DIR=""
ARTIFACTORY_URL=""
REPO_INBOX="sonar-projects-to-migrate"
REPO_DONE="sonar-projects-migrated"
TARGET_KEY_REGEX='^p-([A-Za-z0-9]+)-([A-Za-z0-9._-]+)$'
LINK_NAME="MIGRATION"

PERMISSION_TEMPLATE=""      # vide = ne pas appliquer
QUALITY_GATE=""             # vide = laisser le gate par défaut
DEVOPS_ALM_SETTING=""       # nom du binding GitLab configuré globalement

ENFORCE_VERSION=true
ENFORCE_PLUGINS=true
ENFORCE_REQUESTER_ADMIN=false   # à activer après le POC
CLEANUP_MIGRATION_LINK=true

STATE_DIR="/var/lib/sonar-import-worker"
LOCK_FILE="/var/lock/sonar-import-worker.lock"
WORK_DIR="/var/lib/sonar-import-worker/work"
MAX_ATTEMPTS=3
CE_POLL_SECONDS=10
CE_TIMEOUT_SECONDS=3600
HTTP_TIMEOUT=30
LOG_LEVEL="INFO"

MAX_BATCH=1                 # projets préparés par cycle ; 1 = le plus lisible

DRY_RUN=0
ONLY_MANIFEST=""
CONFIG=""
MODE="full"                 # full | prepare | commit
ITEM=""                     # descripteur du projet en cours, rempli par traiter()

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
#  Arguments et configuration                                                  #
# --------------------------------------------------------------------------- #

while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--config) CONFIG="$2"; shift 2 ;;
    --dry-run)   DRY_RUN=1; shift ;;
    --manifest)  ONLY_MANIFEST="$2"; shift 2 ;;
    --prepare)   MODE="prepare"; shift ;;
    --commit)    MODE="commit";  shift ;;
    -h|--help)   sed -n '2,30p' "$0"; exit 0 ;;
    *)           die "argument inconnu : $1" ;;
  esac
done

[[ -n "$CONFIG" && -r "$CONFIG" ]] || die "usage : $0 -c /chemin/config.sh"
# shellcheck disable=SC1090
source "$CONFIG"

for bin in curl jq sha256sum flock; do
  command -v "$bin" >/dev/null || die "commande absente : $bin"
done

for v in SONAR_URL SONAR_TOKEN IMPORT_DIR ARTIFACTORY_URL ARTIFACTORY_TOKEN; do
  [[ -n "${!v:-}" ]] || die "$v non defini (configuration ou variable d environnement)"
done
unset v

SONAR_URL="${SONAR_URL%/}"
ARTIFACTORY_URL="${ARTIFACTORY_URL%/}"
mkdir -p "$STATE_DIR/done" "$STATE_DIR/retry" "$WORK_DIR"

# Descripteur de travail : ce que --prepare a posé, ce que --commit doit traiter.
# C'est aussi le contrat lu par Ansible entre les deux phases.
PENDING_FILE="$WORK_DIR/pending.json"

[[ "$MODE" == "commit" && "$DRY_RUN" == "1" ]] \
  && die "--commit et --dry-run sont incompatibles : commit ecrit par definition"

exec 9>"$LOCK_FILE"
flock -n 9 || { warn "un cycle est déjà en cours, on sort"; exit 4; }

# --------------------------------------------------------------------------- #
#  Appels HTTP                                                                 #
# --------------------------------------------------------------------------- #

sq() {
  curl --fail --silent --show-error --max-time "$HTTP_TIMEOUT" \
       -u "${SONAR_TOKEN}:" "${SONAR_URL}$1"
}

sq_post() {   # sq_post <chemin> [données]
  curl --fail --silent --show-error --max-time "$HTTP_TIMEOUT" \
       -u "${SONAR_TOKEN}:" -X POST "${SONAR_URL}$1" ${2:+--data "$2"}
}

sq_code() {   # code HTTP seul, sans --fail
  curl --silent --output /dev/null --write-out '%{http_code}' \
       --max-time "$HTTP_TIMEOUT" -u "${SONAR_TOKEN}:" "${SONAR_URL}$1"
}

art() {   # art <méthode> <chemin> [options curl…]
  local m="$1" p="$2"; shift 2
  curl --fail --silent --show-error --max-time $((HTTP_TIMEOUT * 20)) \
       -H "Authorization: Bearer ${ARTIFACTORY_TOKEN}" \
       -X "$m" "${ARTIFACTORY_URL}${p}" "$@"
}

urlenc() { jq -rn --arg v "$1" '$v|@uri'; }

# --------------------------------------------------------------------------- #
#  État local                                                                  #
# --------------------------------------------------------------------------- #

st_key()      { printf '%s' "${1//\//_}"; }
st_is_done()  { [[ -f "$STATE_DIR/done/$(st_key "$1")" ]]; }
st_attempts() { cat "$STATE_DIR/retry/$(st_key "$1")" 2>/dev/null || echo 0; }

st_finish() {   # st_finish <chemin manifeste> <statut> <détail>
  [[ "$DRY_RUN" == "1" ]] && return 0
  printf '%s %s\n' "$2" "$3" > "$STATE_DIR/done/$(st_key "$1")"
  rm -f "$STATE_DIR/retry/$(st_key "$1")"
}

st_retry() {
  local n; n=$(( $(st_attempts "$1") + 1 ))
  [[ "$DRY_RUN" == "1" ]] || printf '%s\n' "$n" > "$STATE_DIR/retry/$(st_key "$1")"
  printf '%s\n' "$n"
}

# --------------------------------------------------------------------------- #
#  Faits de l'instance, calculés une fois par cycle                            #
# --------------------------------------------------------------------------- #

SQ_VERSION=""; SQ_PLUGINS=""

collect_facts() {
  local v p
  v="$(sq "/api/server/version" | tr -d '[:space:]')" || return 1
  [[ -n "$v" ]] || return 1
  SQ_VERSION="$v"
  p="$(sq "/api/plugins/installed")" || return 1
  # { "cle": "version", … } — permet une comparaison directe avec le manifeste
  SQ_PLUGINS="$(jq -c '[.plugins[]? | {(.key): (.version // "")}] | add // {}' <<< "$p")"
  dbg "version=$SQ_VERSION plugins=$(jq length <<< "$SQ_PLUGINS")"
  return 0
}

# --------------------------------------------------------------------------- #
#  Contrôles                                                                   #
# --------------------------------------------------------------------------- #

# Renvoie 0 si le projet existe
projet_existe() { [[ "$(sq_code "/api/components/show?component=$(urlenc "$1")")" == "200" ]]; }

# Renvoie 0 si le projet ne contient AUCUNE analyse.
# C'est le garde-fou le plus important du worker : on ne supprime jamais un
# projet qui a déjà servi.
projet_vide() {
  local j n
  j="$(sq "/api/project_analyses/search?project=$(urlenc "$1")&ps=1")" || return 1
  n="$(jq -r '.paging.total // (.analyses | length) // 0' <<< "$j")"
  [[ "$n" == "0" ]]
}

# Compatibilité des plugins : la CIBLE doit contenir tous ceux de la SOURCE,
# mêmes versions. Elle peut en avoir davantage — c'est autorisé.
plugins_manquants() {   # plugins_manquants <json plugins du manifeste>
  jq -r --argjson cible "$SQ_PLUGINS" '
    (. // [])
    | map(select($cible[.key] == null or $cible[.key] != .version)
          | if $cible[.key] == null
            then "\(.key) \(.version) absent"
            else "\(.key) attendu \(.version), trouve \($cible[.key])" end)
    | .[]' <<< "$1"
}

# Le demandeur est-il administrateur du projet cible ?
# Trois chemins : administrateur global, permission directe, appartenance à un
# groupe. Omettre le troisième rejetterait la majorité des cas légitimes.
demandeur_est_admin() {   # demandeur_est_admin <login> <cle projet>
  local login="$1" cle="$2" j

  j="$(sq "/api/permissions/users?permission=admin&ps=500")" || return 1
  jq -e --arg l "$login" '[.users[]?.login] | index($l)' <<< "$j" >/dev/null \
    && { dbg "$login : administrateur global"; return 0; }

  j="$(sq "/api/permissions/users?projectKey=$(urlenc "$cle")&permission=admin&ps=500")" || return 1
  jq -e --arg l "$login" '[.users[]?.login] | index($l)' <<< "$j" >/dev/null \
    && { dbg "$login : permission directe"; return 0; }

  local groupes_projet groupes_user
  groupes_projet="$(sq "/api/permissions/groups?projectKey=$(urlenc "$cle")&permission=admin&ps=500")" || return 1
  jq -e '[.groups[]?.name] | index("Anyone")' <<< "$groupes_projet" >/dev/null \
    && { err "admin accorde au groupe Anyone sur $cle — configuration anormale"; return 1; }

  groupes_user="$(sq "/api/users/groups?login=$(urlenc "$login")&ps=500")" || return 1
  jq -e -n \
     --argjson a "$(jq -c '[.groups[]?.name]' <<< "$groupes_projet")" \
     --argjson b "$(jq -c '[.groups[]?.name]' <<< "$groupes_user")" \
     '($a - ($a - $b)) | length > 0' >/dev/null \
    && { dbg "$login : via groupe"; return 0; }

  return 1
}

# Résout le login cible à partir de externalIdentity, avec repli sur l'email.
resoudre_login() {   # resoudre_login <external_identity> <provider> <email>
  local ident="$1" prov="$2" mail="$3" j hit

  if [[ -n "$ident" ]]; then
    j="$(curl --fail --silent --max-time "$HTTP_TIMEOUT" -u "${SONAR_TOKEN}:" \
         "${SONAR_URL}/api/v2/users-management/users?externalIdentity=$(urlenc "$ident")&pageSize=50" \
         2>/dev/null)" || j=""
    if [[ -n "$j" ]]; then
      hit="$(jq -r --arg p "$prov" '
        [ .users[]? | select((.externalProvider // "") == $p and (.active // true)) ][0].login // empty
      ' <<< "$j")"
      [[ -n "$hit" ]] && { printf '%s' "$hit"; return 0; }
    fi
  fi

  if [[ -n "$mail" ]]; then
    j="$(sq "/api/users/search?q=$(urlenc "$mail")&ps=50")" || return 1
    hit="$(jq -r --arg m "$mail" '
      [ .users[]? | select((.email // "") == $m and (.active // true)) ][0].login // empty
    ' <<< "$j")"
    [[ -n "$hit" ]] && { warn "correspondance par email seul pour $mail"
                         printf '%s' "$hit"; return 0; }
  fi
  return 1
}

# --------------------------------------------------------------------------- #
#  Suivi d'une tâche Compute Engine                                            #
# --------------------------------------------------------------------------- #

attendre_ce() {   # attendre_ce <task id>
  local id="$1" t=0 statut
  while (( t < CE_TIMEOUT_SECONDS )); do
    statut="$(sq "/api/ce/task?id=$(urlenc "$id")" | jq -r '.task.status // "UNKNOWN"')" || return 1
    case "$statut" in
      SUCCESS)             return 0 ;;
      FAILED|CANCELED)     err "tache $id en echec ($statut)"; return 1 ;;
      PENDING|IN_PROGRESS) sleep "$CE_POLL_SECONDS"; t=$(( t + CE_POLL_SECONDS )) ;;
      *)                   sleep "$CE_POLL_SECONDS"; t=$(( t + CE_POLL_SECONDS )) ;;
    esac
  done
  err "tache $id : delai depasse apres ${CE_TIMEOUT_SECONDS}s"
  return 1
}

# --------------------------------------------------------------------------- #
#  Renommage — l'API a changé de signature selon les versions                   #
# --------------------------------------------------------------------------- #

renommer_projet() {   # renommer_projet <ancienne cle> <nouvelle cle>
  local de="$(urlenc "$1")" vers="$(urlenc "$2")"
  # Signature récente
  sq_post "/api/projects/update_key?from=${de}&to=${vers}" >/dev/null 2>&1 && return 0
  # Signature alternative, selon les versions
  sq_post "/api/projects/update_key?project=${de}&newKey=${vers}" >/dev/null 2>&1 && return 0
  return 1
}

# --------------------------------------------------------------------------- #
#  Traitement d'un manifeste                                                   #
# --------------------------------------------------------------------------- #
# Retour : 0 importé · 10 rejeté définitivement · 20 à réessayer

REASON=""

traiter() {
  local chemin="$1"          # ex. espace12/com.entite_projet.manifest.json
  local m dossier

  m="$(art GET "/${REPO_INBOX}/${chemin}")" \
    || { REASON="manifeste illisible"; return 20; }

  jq -e '.schema_version and .source.project_key and .request.declared_target_project_key' \
     <<< "$m" >/dev/null \
    || { REASON="manifeste incomplet"; return 10; }

  local cle_source cle_cible espace ver_src ed_src sha_att nom_archive
  cle_source="$(jq -r '.source.project_key'                       <<< "$m")"
  cle_cible="$( jq -r '.request.declared_target_project_key'      <<< "$m")"
  espace="$(    jq -r '.request.espace_id // ""'                  <<< "$m")"
  ver_src="$(   jq -r '.source.sonar_version // ""'               <<< "$m")"
  ed_src="$(    jq -r '.source.edition // ""'                     <<< "$m")"
  sha_att="$(   jq -r '.archive.sha256 // ""'                     <<< "$m")"
  nom_archive="$(jq -r '.archive.filename // ""'                  <<< "$m")"

  info "[$chemin] $cle_source → $cle_cible"

  # --- 1. format de la clé cible et cohérence de l'espace ----------------- #
  [[ "$cle_cible" =~ $TARGET_KEY_REGEX ]] \
    || { REASON="cle cible « $cle_cible » hors format"; return 10; }
  if [[ -n "$espace" && "${BASH_REMATCH[1]}" != "$espace" ]]; then
    REASON="espace incoherent : cle dit « ${BASH_REMATCH[1]} », depot dit « $espace »"
    return 10
  fi
  dossier="$(dirname "$chemin")"
  if [[ -n "$espace" && "$dossier" != "$espace" ]]; then
    REASON="depot « $dossier » ne correspond pas a l espace « $espace »"
    return 10
  fi

  # --- 2. version et édition ---------------------------------------------- #
  if [[ "$ENFORCE_VERSION" == "true" ]]; then
    [[ "$ver_src" == "$SQ_VERSION" ]] \
      || { REASON="version source $ver_src != cible $SQ_VERSION"; return 10; }
    [[ -z "$ed_src" || "$ed_src" == "$SONAR_EDITION" ]] \
      || { REASON="edition source $ed_src != cible $SONAR_EDITION"; return 10; }
  fi

  # --- 3. plugins ---------------------------------------------------------- #
  if [[ "$ENFORCE_PLUGINS" == "true" ]]; then
    local liste manquants
    liste="$(jq -c '.source.plugins // []' <<< "$m")"
    if [[ "$liste" != "[]" ]]; then
      manquants="$(plugins_manquants "$liste")"
      [[ -z "$manquants" ]] \
        || { REASON="plugins incompatibles : $(tr '\n' ';' <<< "$manquants")"; return 10; }
    else
      warn "[$chemin] manifeste sans liste de plugins — controle ignore"
    fi
  fi

  # --- 4. le projet cible : existe, et surtout VIDE ------------------------ #
  projet_existe "$cle_cible" \
    || { REASON="projet cible « $cle_cible » inexistant"; return 20; }
  projet_vide "$cle_cible" \
    || { REASON="projet cible « $cle_cible » contient deja des analyses"; return 10; }

  # Le projet importé arrive avec la clé source : elle doit être libre.
  if projet_existe "$cle_source"; then
    REASON="la cle source « $cle_source » est deja prise sur la cible"
    return 10
  fi

  # --- 5. habilitation du demandeur --------------------------------------- #
  local login_cible=""
  if [[ "$ENFORCE_REQUESTER_ADMIN" == "true" ]]; then
    local ident prov mail
    ident="$(jq -r '.request.by.external_identity // ""' <<< "$m")"
    prov="$( jq -r '.request.by.external_provider // ""' <<< "$m")"
    mail="$( jq -r '.request.by.email // ""'             <<< "$m")"

    login_cible="$(resoudre_login "$ident" "$prov" "$mail")" \
      || { REASON="demandeur introuvable sur la cible — il doit s etre connecte au moins une fois"
           return 10; }

    demandeur_est_admin "$login_cible" "$cle_cible" \
      || { REASON="« $login_cible » n est pas administrateur de « $cle_cible »"; return 10; }
    info "[$chemin] habilitation verifiee pour $login_cible"
  fi

  # --- 6. archive : téléchargement, intégrité, dépôt ----------------------- #
  local local_zip="$WORK_DIR/${cle_source//\//_}.zip"
  art GET "/${REPO_INBOX}/${dossier}/${nom_archive}" -o "$local_zip" \
    || { REASON="telechargement de l archive impossible"; return 20; }

  local sha_reel; sha_reel="$(sha256sum "$local_zip" | cut -d' ' -f1)"
  [[ "$sha_reel" == "$sha_att" ]] \
    || { rm -f "$local_zip"; REASON="empreinte incorrecte"; return 10; }

  if [[ "$DRY_RUN" == "1" ]]; then
    info "[$chemin] DRY-RUN — tous les controles passent, aucun import effectue"
    rm -f "$local_zip"
    return 0
  fi

  # Project Move lit le fichier dans le répertoire d'import, nommé d'après la
  # clé du projet contenu dans le dump. Déposer n'écrit pas sur l'instance :
  # tant que l'import n'est pas déclenché, ce fichier est inerte.
  local depose="${IMPORT_DIR}/${cle_source}.zip"
  mv -f "$local_zip" "$depose" \
    || { rm -f "$local_zip"; REASON="depot dans le repertoire d import impossible"; return 20; }

  local repo; repo="$(jq -r '.source.scm_repository // ""' <<< "$m")"
  ITEM="$(jq -cn \
    --arg man "$chemin"     --arg dos "$dossier"  --arg arc "$nom_archive" \
    --arg src "$cle_source" --arg tgt "$cle_cible" \
    --arg scm "$repo"       --arg loc "$depose" \
    '{manifest:$man, dossier:$dos, archive:$arc, source_key:$src,
      target_key:$tgt, scm_repository:$scm, local_path:$loc}')"

  # En mode --prepare on s'arrête ici. Aucune écriture n'a eu lieu sur
  # l'instance : Ansible peut recopier l'archive sur les autres nœuds, et
  # --commit reprendra la suite.
  [[ "$MODE" == "prepare" ]] && return 0

  executer "$ITEM"
}

# --------------------------------------------------------------------------- #
#  Étapes 7 à 10 — la partie qui écrit sur l'instance centrale                 #
# --------------------------------------------------------------------------- #

executer() {   # executer <item json>
  local it="$1"
  local chemin dossier nom_archive cle_source cle_cible repo depose
  chemin="$(     jq -r '.manifest'            <<< "$it")"
  dossier="$(    jq -r '.dossier'             <<< "$it")"
  nom_archive="$(jq -r '.archive'             <<< "$it")"
  cle_source="$( jq -r '.source_key'          <<< "$it")"
  cle_cible="$(  jq -r '.target_key'          <<< "$it")"
  repo="$(       jq -r '.scm_repository // ""' <<< "$it")"
  depose="$(     jq -r '.local_path'          <<< "$it")"

  [[ -f "$depose" ]] \
    || { REASON="archive absente du repertoire d import : $depose"; return 20; }

  # Rejeu du contrôle le plus important. Entre la préparation et maintenant,
  # une CI a pu analyser le projet cible : la fenêtre est courte, mais réelle
  # sur une instance ouverte à tous. Un import par-dessus un projet non vide
  # est irrattrapable, ce contrôle ne coûte qu'un appel.
  projet_vide "$cle_cible" \
    || { REASON="projet cible « $cle_cible » n est plus vide depuis la preparation"
         return 10; }
  if projet_existe "$cle_source"; then
    REASON="la cle source « $cle_source » est desormais prise sur la cible"
    return 10
  fi

  # ======================================================================== #
  #  À partir d'ici on écrit sur l'instance centrale.                        #
  # ======================================================================== #

  # --- 7. import ----------------------------------------------------------- #
  local tid
  tid="$(sq_post "/api/project_dump/import?key=$(urlenc "$cle_source")" \
         | jq -r '.taskId // empty')" \
    || { REASON="appel d import refuse"; return 20; }
  [[ -n "$tid" ]] || { REASON="import sans identifiant de tache"; return 20; }
  info "[$chemin] import lance, tache $tid"

  attendre_ce "$tid" \
    || { REASON="import en echec (tache $tid)"; return 20; }
  info "[$chemin] import termine"

  # --- 8. bascule : supprimer PUIS renommer -------------------------------- #
  # Ordre volontaire : on ne détruit le projet vide qu'une fois l'import
  # confirmé. En cas d'échec avant ce point, rien n'a été perdu.
  sq_post "/api/projects/delete?project=$(urlenc "$cle_cible")" >/dev/null \
    || { REASON="ALERTE — import reussi mais suppression du projet vide impossible"
         return 20; }
  info "[$chemin] projet vide supprime, cle liberee"

  renommer_projet "$cle_source" "$cle_cible" \
    || { REASON="ALERTE — projet importe sous « $cle_source », renommage impossible"
         return 20; }
  info "[$chemin] renomme en $cle_cible"

  # --- 9. reconfiguration -------------------------------------------------- #
  if [[ -n "$PERMISSION_TEMPLATE" ]]; then
    sq_post "/api/permissions/apply_template?projectKey=$(urlenc "$cle_cible")&templateName=$(urlenc "$PERMISSION_TEMPLATE")" >/dev/null \
      && info "[$chemin] template de permissions applique" \
      || warn "[$chemin] template de permissions non applique"
  fi

  if [[ -n "$QUALITY_GATE" ]]; then
    sq_post "/api/qualitygates/select?projectKey=$(urlenc "$cle_cible")&gateName=$(urlenc "$QUALITY_GATE")" >/dev/null \
      && info "[$chemin] quality gate affecte" \
      || warn "[$chemin] quality gate non affecte"
  fi

  if [[ -n "$DEVOPS_ALM_SETTING" && -n "$repo" ]]; then
    sq_post "/api/alm_settings/set_gitlab_binding?almSetting=$(urlenc "$DEVOPS_ALM_SETTING")&project=$(urlenc "$cle_cible")&repository=$(urlenc "$repo")" >/dev/null \
      && info "[$chemin] binding GitLab recree" \
      || warn "[$chemin] binding GitLab non recree — decoration des MR inactive"
  fi

  # Le lien MIGRATION a voyagé dans l'archive : il pointe désormais vers le
  # projet lui-même. On le retire pour ne pas laisser d'artefact incongru.
  if [[ "$CLEANUP_MIGRATION_LINK" == "true" ]]; then
    local lid
    lid="$(sq "/api/project_links/search?projectKey=$(urlenc "$cle_cible")" \
           | jq -r --arg n "${LINK_NAME^^}" \
             '[.links[]? | select(((.name // "")|ascii_upcase|sub("^[[:space:]]+";"")|sub("[[:space:]]+$";"")) == $n)][0].id // empty')"
    [[ -n "$lid" ]] && sq_post "/api/project_links/delete?id=$(urlenc "$lid")" >/dev/null \
      && dbg "[$chemin] lien $LINK_NAME retire"
  fi

  # --- 10. archivage -------------------------------------------------------- #
  art POST "/api/move/${REPO_INBOX}/${dossier}/${nom_archive}?to=/${REPO_DONE}/${dossier}/${nom_archive}" >/dev/null \
    || warn "[$chemin] archive non deplacee vers ${REPO_DONE}"
  art POST "/api/move/${REPO_INBOX}/${chemin}?to=/${REPO_DONE}/${chemin}" >/dev/null \
    || warn "[$chemin] manifeste non deplace vers ${REPO_DONE}"

  # L'archive locale a fait son office. Elle reste disponible dans Artifactory,
  # qui est la référence : la copie sur le nœud est jetable.
  # En cluster, c'est Ansible qui nettoie tous les nœuds — le chemin est
  # identique, et la suppression est idempotente.
  rm -f "$depose"

  info "[$chemin] MIGRATION TERMINEE — ${SONAR_URL}/dashboard?id=$(urlenc "$cle_cible")"
  return 0
}

# --------------------------------------------------------------------------- #
#  Cycle                                                                       #
# --------------------------------------------------------------------------- #

lister_manifestes() {
  # AQL : la seule façon fiable de filtrer par nom sur toute la profondeur.
  local aql="items.find({\"repo\":\"${REPO_INBOX}\",\"name\":{\"\$match\":\"*.manifest.json\"}}).include(\"path\",\"name\")"
  art POST "/api/search/aql" -H "Content-Type: text/plain" --data "$aql" \
    | jq -r '.results[]? | "\(.path)/\(.name)"' | sed 's|^\./||'
}

# Classement d'un résultat. Factorisé pour que les deux cycles décident
# exactement de la même façon.
classer() {   # classer <chemin manifeste> <code de retour>
  local chemin="$1" rc="$2" att
  case "$rc" in
    0)  st_finish "$chemin" imported ok ;;
    10) err "[$chemin] REJETE : $REASON"
        st_finish "$chemin" rejected "$REASON" ;;
    20) att="$(st_retry "$chemin")"
        if (( att >= MAX_ATTEMPTS )); then
          err "[$chemin] ABANDON apres $att tentatives : $REASON"
          st_finish "$chemin" failed "$REASON"
        else
          warn "[$chemin] tentative $att/$MAX_ATTEMPTS : $REASON"
        fi ;;
    *)  err "[$chemin] erreur inattendue ($rc)"; st_retry "$chemin" >/dev/null ;;
  esac
}

PENDING_ITEMS=()

cycle_prepare() {   # modes « full » et « prepare »
  local manifestes n=0 rc
  manifestes="$(lister_manifestes)" || die "listing Artifactory impossible" 3

  while IFS= read -r chemin; do
    [[ -z "$chemin" ]] && continue
    [[ -n "$ONLY_MANIFEST" && "$chemin" != "$ONLY_MANIFEST" ]] && continue
    [[ -z "$ONLY_MANIFEST" ]] && st_is_done "$chemin" && continue
    n=$(( n + 1 ))

    REASON=""; ITEM=""; rc=0
    traiter "$chemin" || rc=$?

    if [[ "$MODE" == "prepare" && "$rc" == "0" ]]; then
      # Rien n'a été écrit sur l'instance : on ne marque donc rien comme fait.
      # C'est --commit qui tranchera, et lui seul.
      if [[ "$DRY_RUN" != "1" ]]; then
        PENDING_ITEMS+=("$ITEM")
        info "[$chemin] preparé — archive deposee, en attente de --commit"
        (( ${#PENDING_ITEMS[@]} >= MAX_BATCH )) && break
      fi
    else
      [[ "$DRY_RUN" == "1" && "$rc" == "0" ]] || classer "$chemin" "$rc"
    fi
  done <<< "$manifestes"

  if [[ "$MODE" == "prepare" && "$DRY_RUN" != "1" ]]; then
    if (( ${#PENDING_ITEMS[@]} > 0 )); then
      printf '%s\n' "${PENDING_ITEMS[@]}" \
        | jq -s '{generated_at:(now|todate), items:.}' > "$PENDING_FILE"
    else
      jq -n '{generated_at:(now|todate), items:[]}' > "$PENDING_FILE"
    fi
    info "descripteur : $PENDING_FILE — $(jq '.items|length' "$PENDING_FILE") element(s)"
  fi

  info "$n manifeste(s) examine(s)"
  return 0
}

cycle_commit() {
  [[ -f "$PENDING_FILE" ]] || { info "aucun descripteur — rien a valider"; return 0; }

  local n; n="$(jq '.items | length' "$PENDING_FILE")"
  (( n > 0 )) || { info "descripteur vide — rien a valider"; rm -f "$PENDING_FILE"; return 0; }

  local i it chemin rc
  for (( i = 0; i < n; i++ )); do
    it="$(jq -c ".items[$i]" "$PENDING_FILE")"
    chemin="$(jq -r '.manifest' <<< "$it")"
    REASON=""; rc=0
    executer "$it" || rc=$?
    classer "$chemin" "$rc"
  done

  # Le descripteur est consommé. Qu'il ait réussi ou échoué, le cycle suivant
  # repart d'une préparation propre : c'est Artifactory qui fait référence,
  # pas ce fichier.
  rm -f "$PENDING_FILE"
  info "$n element(s) valide(s)"
  return 0
}

main() {
  collect_facts || die "instance SonarQube injoignable" 2
  art GET "/api/system/ping" >/dev/null || die "Artifactory injoignable" 3

  [[ -d "$IMPORT_DIR" ]] || die "repertoire d import introuvable : $IMPORT_DIR"

  case "$MODE" in
    commit) cycle_commit ;;
    *)      cycle_prepare ;;
  esac
}

main
