#!/usr/bin/env bash
#
# compare-plugins.sh — compatibilité des plugins entre deux instances SonarQube
#
# La règle de Project Move : l'instance CIBLE doit posséder tous les plugins de
# l'instance SOURCE, dans les mêmes versions. La cible peut en avoir davantage.
#
# Le script classe donc les écarts en trois catégories, qui n'appellent pas les
# mêmes actions :
#
#   BLOQUANT   plugin présent à la source, absent de la cible
#              → la CIBLE doit l'installer, ou la source le retirer
#
#   BLOQUANT   même plugin, versions différentes
#              → la SOURCE s'aligne sur la version de la cible
#
#   SANS EFFET plugin présent à la cible seulement
#              → aucune action, c'est autorisé
#
# Usage
#   export SRC_URL=https://sonar.entite.corp   SRC_TOKEN=squ_xxx
#   export TGT_URL=https://sonar.groupe.corp   TGT_TOKEN=squ_yyy
#   ./compare-plugins.sh
#
#   ./compare-plugins.sh --json          sortie exploitable par un script
#   ./compare-plugins.sh --from-file a.json b.json    hors ligne
#
# Codes de sortie
#   0  compatible          1  écart bloquant          2  erreur d'appel
#

set -uo pipefail

FORMAT="text"
SRC_FILE=""; TGT_FILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json)      FORMAT="json"; shift ;;
    --from-file) SRC_FILE="$2"; TGT_FILE="$3"; shift 3 ;;
    -h|--help)   sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "argument inconnu : $1" >&2; exit 2 ;;
  esac
done

command -v jq >/dev/null || { echo "jq est requis" >&2; exit 2; }

# --------------------------------------------------------------------------- #
#  Récupération des inventaires                                                #
# --------------------------------------------------------------------------- #

fetch() {   # fetch <url> <token> — nécessite le droit Administer System
  curl --fail --silent --show-error --max-time 30 \
       -u "$2:" "${1%/}/api/plugins/installed"
}

if [[ -n "$SRC_FILE" ]]; then
  SRC_RAW="$(cat "$SRC_FILE")" || exit 2
  TGT_RAW="$(cat "$TGT_FILE")" || exit 2
  SRC_NAME="$SRC_FILE"; TGT_NAME="$TGT_FILE"
else
  : "${SRC_URL:?SRC_URL non défini}" ; : "${SRC_TOKEN:?SRC_TOKEN non défini}"
  : "${TGT_URL:?TGT_URL non défini}" ; : "${TGT_TOKEN:?TGT_TOKEN non défini}"
  SRC_RAW="$(fetch "$SRC_URL" "$SRC_TOKEN")" || { echo "source injoignable" >&2; exit 2; }
  TGT_RAW="$(fetch "$TGT_URL" "$TGT_TOKEN")" || { echo "cible injoignable"  >&2; exit 2; }
  SRC_NAME="$SRC_URL"; TGT_NAME="$TGT_URL"
fi

norm() {   # normalise en [{key,version,bundled}] trié
  jq -c '[ .plugins[]
           | { key,
               version: (.version // ""),
               bundled: (.editionBundled // false) } ]
         | sort_by(.key)'
}

SRC="$(norm <<< "$SRC_RAW")" || exit 2
TGT="$(norm <<< "$TGT_RAW")" || exit 2

# --------------------------------------------------------------------------- #
#  Comparaison                                                                 #
# --------------------------------------------------------------------------- #

REPORT="$(jq -n --argjson s "$SRC" --argjson t "$TGT" '
  ($s | map({ (.key): . }) | add // {}) as $S
| ($t | map({ (.key): . }) | add // {}) as $T
| {
    absents_cible:
      [ $s[] | select($T[.key] == null) ],

    versions_differentes:
      [ $s[]
        | select($T[.key] != null and $T[.key].version != .version)
        | { key, bundled,
            version_source: .version,
            version_cible:  $T[.key].version } ],

    cible_seulement:
      [ $t[] | select($S[.key] == null) ]
  }
| . + { compatible: ((.absents_cible | length) == 0
                     and (.versions_differentes | length) == 0) }
')" || exit 2

# --------------------------------------------------------------------------- #
#  Restitution                                                                 #
# --------------------------------------------------------------------------- #

if [[ "$FORMAT" == "json" ]]; then
  jq . <<< "$REPORT"
  jq -e '.compatible' <<< "$REPORT" >/dev/null && exit 0 || exit 1
fi

bold=$'\033[1m'; red=$'\033[31m'; grn=$'\033[32m'; yel=$'\033[33m'; off=$'\033[0m'

printf '%sComparaison des plugins%s\n' "$bold" "$off"
printf '  source : %s  (%s plugins)\n' "$SRC_NAME" "$(jq length <<< "$SRC")"
printf '  cible  : %s  (%s plugins)\n' "$TGT_NAME" "$(jq length <<< "$TGT")"

n_abs=$(jq '.absents_cible        | length' <<< "$REPORT")
n_ver=$(jq '.versions_differentes | length' <<< "$REPORT")
n_ext=$(jq '.cible_seulement      | length' <<< "$REPORT")

if (( n_abs > 0 )); then
  printf '\n%s%sBLOQUANT — %d plugin(s) de la source absent(s) de la cible%s\n' \
         "$bold" "$red" "$n_abs" "$off"
  printf '  La CIBLE doit les installer, ou la source doit les retirer.\n\n'
  jq -r '.absents_cible[] | "    \(.key) \(.version)" +
         (if .bundled then "   [fourni avec l édition]" else "" end)' <<< "$REPORT"
fi

if (( n_ver > 0 )); then
  printf '\n%s%sBLOQUANT — %d plugin(s) en version différente%s\n' \
         "$bold" "$red" "$n_ver" "$off"
  printf '  La SOURCE doit s aligner sur la version de la cible.\n\n'
  jq -r '.versions_differentes[]
         | "    \(.key)   source \(.version_source)  →  cible \(.version_cible)" +
           (if .bundled then "   [fourni avec l édition]" else "" end)' <<< "$REPORT"
fi

if (( n_ext > 0 )); then
  printf '\n%s%sSANS EFFET — %d plugin(s) présent(s) uniquement sur la cible%s\n' \
         "$yel" "$bold" "$n_ext" "$off"
  printf '  Autorisé par Project Move. Aucune action côté source.\n\n'
  jq -r '.cible_seulement[] | "    \(.key) \(.version)"' <<< "$REPORT" | head -20
  (( n_ext > 20 )) && printf '    … et %d autre(s)\n' $(( n_ext - 20 ))
fi

printf '\n'
if jq -e '.compatible' <<< "$REPORT" >/dev/null; then
  printf '%s%sCOMPATIBLE%s — l import peut avoir lieu.\n' "$bold" "$grn" "$off"
  exit 0
else
  printf '%s%sINCOMPATIBLE%s — %d écart(s) bloquant(s) à traiter.\n' \
         "$bold" "$red" "$off" $(( n_abs + n_ver ))
  exit 1
fi
