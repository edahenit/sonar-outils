# sonar-import-worker — configuration
# Copier vers /etc/sonar-import-worker/config.sh puis chmod 640.

# --------------------------------------------------------------------------- #
#  Instance SonarQube centrale                                                 #
# --------------------------------------------------------------------------- #

SONAR_URL="https://sonar-centrale.groupe.corp"

# Token d'un compte technique avec le droit « Administer System ».
# Laisser vide ici, injecter par systemd depuis /etc/sonar-import-worker/env.
# SONAR_TOKEN=""

SONAR_EDITION="enterprise"

# Répertoire lu par Project Move à l'import.
IMPORT_DIR="/opt/sonarqube/data/governance/project_dumps/import"


# --------------------------------------------------------------------------- #
#  Artifactory                                                                 #
# --------------------------------------------------------------------------- #

ARTIFACTORY_URL="https://artifactory.groupe.corp/artifactory"
REPO_INBOX="sonar-projects-to-migrate"
REPO_DONE="sonar-projects-migrated"
# ARTIFACTORY_TOKEN=""

# IMPORTANT : restreindre la LECTURE de ces deux dépôts au seul compte
# technique de ce worker. Une archive Project Move contient l'historique
# complet d'un projet ; le répertoire par espace sert à l'organisation, pas
# au partage entre équipes.


# --------------------------------------------------------------------------- #
#  Conventions                                                                 #
# --------------------------------------------------------------------------- #

# Le PREMIER groupe capturant doit être l'espace_id : il est comparé au
# répertoire Artifactory, ce qui empêche qu'un projet d'un espace atterrisse
# dans un autre.
TARGET_KEY_REGEX='^p-([A-Za-z0-9]+)-([A-Za-z0-9._-]+)$'

# Nom du lien posé côté source, à retirer après import.
LINK_NAME="MIGRATION"


# --------------------------------------------------------------------------- #
#  Reconfiguration après import — laisser vide pour ne rien appliquer          #
# --------------------------------------------------------------------------- #

PERMISSION_TEMPLATE=""      # ex. "Template applicatif standard"
QUALITY_GATE=""             # ex. "Groupe - Niveau 1"
DEVOPS_ALM_SETTING=""       # nom du binding GitLab configuré globalement

CLEANUP_MIGRATION_LINK=true


# --------------------------------------------------------------------------- #
#  Contrôles                                                                   #
# --------------------------------------------------------------------------- #

ENFORCE_VERSION=true
ENFORCE_PLUGINS=true

# À laisser à false pendant le POC, à activer avant d'ouvrir le service aux
# équipes. Le manifeste porte déjà l'identité du demandeur : activer ce
# contrôle ne demandera aucune reprise côté export.
ENFORCE_REQUESTER_ADMIN=false


# --------------------------------------------------------------------------- #
#  Exécution                                                                   #
# --------------------------------------------------------------------------- #

STATE_DIR="/var/lib/sonar-import-worker"
WORK_DIR="/var/lib/sonar-import-worker/work"
LOCK_FILE="/var/lock/sonar-import-worker.lock"

MAX_ATTEMPTS=3

# Projets préparés par cycle, en mode --prepare.
# 1 est volontairement le défaut : un projet par exécution Tower, c'est le
# plus simple à diagnostiquer et cela borne la durée du job. Augmenter le
# rythme de la planification plutôt que ce nombre.
MAX_BATCH=1

# Un gros projet peut demander plusieurs dizaines de minutes d'import.
CE_POLL_SECONDS=10
CE_TIMEOUT_SECONDS=3600

HTTP_TIMEOUT=30
LOG_LEVEL="INFO"
