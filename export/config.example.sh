# sonar-export-publisher — configuration (version shell)
# Copier vers /etc/sonar-export-publisher/config.sh puis chmod 640.
# Ce fichier est chargé par « source » : c'est du shell, pas du YAML.

# --------------------------------------------------------------------------- #
#  Instance SonarQube source                                                   #
# --------------------------------------------------------------------------- #

SONAR_URL="https://sonar.entite.corp"

# Token d'un compte technique avec le droit « Administer System ».
# Laisser vide ici et l'injecter par systemd depuis /etc/sonar-export-publisher/env.
# SONAR_TOKEN="squ_xxxxxxxx"

# community | developer | enterprise | datacenter
SONAR_EDITION="enterprise"

# Répertoire où SonarQube dépose les archives d'export.
EXPORT_DIR="/opt/sonarqube/data/governance/project_dumps/export"


# --------------------------------------------------------------------------- #
#  Instance cible                                                              #
# --------------------------------------------------------------------------- #

# Host attendu dans le lien MIGRATION. Toute autre valeur est rejetée : c'est
# ce qui empêche de faire atterrir un import sur une instance tierce.
TARGET_HOST="sonar-centrale.groupe.corp"

# Format de la clé cible produite par le portail DevOps.
# Le PREMIER groupe capturant doit être l'espace_id : il détermine le
# répertoire Artifactory de destination.
TARGET_KEY_REGEX='^p-([A-Za-z0-9]+)-([A-Za-z0-9._-]+)$'

# Nom du lien à poser sur le projet source (comparaison insensible à la casse).
LINK_NAME="MIGRATION"


# --------------------------------------------------------------------------- #
#  Artifactory                                                                 #
# --------------------------------------------------------------------------- #

ARTIFACTORY_URL="https://artifactory.groupe.corp/artifactory"
ARTIFACTORY_REPO="sonar-projects-to-migrate"

# Comme pour SONAR_TOKEN : préférer /etc/sonar-export-publisher/env.
# ARTIFACTORY_TOKEN="cmVmdGtuOjAxOjE3xxxx"


# --------------------------------------------------------------------------- #
#  Exécution                                                                   #
# --------------------------------------------------------------------------- #

STATE_DIR="/var/lib/sonar-export-publisher"
QUARANTINE_DIR="/var/lib/sonar-export-publisher/quarantine"
LOCK_FILE="/var/lock/sonar-export-publisher.lock"

# Fenêtre de rattrapage. 24 h couvre un arrêt d'une journée sans dépendre
# de la purge de l'historique Compute Engine.
LOOKBACK_HOURS=24

# Nombre de cycles avant mise en quarantaine.
# 6 cycles de 5 minutes = 30 minutes de tolérance.
MAX_ATTEMPTS=6

# Deux mesures de taille espacées de N secondes, pour écarter les archives
# encore en cours d'écriture.
STABILITY_SECONDS=5

HTTP_TIMEOUT=30

# Type de tâche Compute Engine pour l'export de projet.
# À VÉRIFIER sur votre version :
#   curl -su "$SONAR_TOKEN:" "$SONAR_URL/api/ce/activity?ps=20" | jq -r '.tasks[].type' | sort -u
CE_TASK_TYPE="PROJECT_EXPORT"

# INFO ou DEBUG
LOG_LEVEL="INFO"
