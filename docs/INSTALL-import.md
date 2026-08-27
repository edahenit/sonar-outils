# Installation — sonar-import-worker

Service à installer **sur le serveur SonarQube central**. Il consomme les
archives publiées dans Artifactory par `sonar-export-publisher` et importe les
projets sur l'instance de l'entreprise.

C'est le seul composant de la chaîne qui **écrit** sur l'instance centrale
partagée : il supprime un projet et en renomme un autre. Le guide insiste donc
sur les vérifications avant mise en service.

| Fichier | Rôle |
|---|---|
| `sonar-import-worker.sh` | le worker (bash, curl, jq) |
| `config.example.sh` | modèle de configuration |
| `tests/run-tests.sh` | banc d'essai sur maquette |
| `tests/mock_central.py` | faux SonarQube + faux Artifactory |
| `TOWER.md` | montage Ansible Tower — **instance en cluster** |
| `ansible/` | rôle et playbook associés |

> **Instance centrale en cluster ?** Si elle comporte plusieurs nœuds
> applicatifs sans système de fichiers partagé, la minuterie systemd décrite
> ici ne suffit pas : la tâche Compute Engine d'import est planifiée sur un
> nœud imprévisible, qui doit trouver l'archive sur son disque local.
> Lisez ce guide pour le worker lui-même, puis `TOWER.md` pour l'orchestration.

---

## 1. Ce que fait le worker, dans l'ordre

Le worker ne réagit **qu'aux manifestes**. Une archive déposée sans son manifeste
est ignorée : c'est ce qui garantit qu'il ne traite jamais un dépôt incomplet.

Pour chaque manifeste, tous les contrôles ont lieu **avant la moindre écriture** :

1. cohérence entre la clé cible déclarée et le répertoire Artifactory (`espace_id`)
2. version SonarQube source identique à la version centrale
3. édition compatible
4. tous les plugins de la source présents en central, mêmes versions
5. le projet cible existe, et il est **vide** (aucune analyse)
6. la clé source n'est pas déjà occupée en central
7. le demandeur est administrateur du projet cible *(optionnel, voir §6)*
8. l'empreinte SHA-256 de l'archive correspond au manifeste

Puis, seulement si tout passe :

```
import (clé source)  →  attente SUCCESS  →  suppression du projet vide
                     →  renommage vers la clé du portail
                     →  template de permissions, quality gate, binding GitLab
                     →  retrait du lien MIGRATION
                     →  déplacement des artefacts vers le dépôt « migrated »
```

**L'ordre n'est pas négociable.** Le projet vide créé par le portail n'est
supprimé qu'une fois l'import confirmé en `SUCCESS`. Si l'import échoue, rien
n'a été détruit et la demande repart au cycle suivant. Inverser ces deux étapes
ferait perdre la clé du portail en cas d'échec d'import.

Le renommage n'intervient qu'après la suppression, parce que c'est le seul
moment où la clé du portail est libre. L'import lui-même ne provoque aucune
collision : l'archive porte la clé **source**.

---

## 2. Prérequis

| Élément | Détail |
|---|---|
| OS | Linux avec systemd |
| Binaires | `bash` 4+, `curl`, `jq`, `sha256sum`, `flock` |
| Édition SonarQube | Enterprise ou supérieure, sur les **deux** instances |
| Accès disque | écriture sur le répertoire d'import de SonarQube |
| Réseau | HTTPS vers l'instance centrale et vers Artifactory |
| Token SonarQube | compte technique, droit *Administer System* |
| Token Artifactory | lecture + déplacement sur les deux dépôts |

### Pourquoi *Administer System*

Le worker doit importer un projet, en supprimer un autre, renommer une clé,
appliquer un template de permissions, lire les plugins installés et résoudre
l'identité du demandeur. Aucun droit projet ne suffit. C'est un compte à traiter
comme un compte d'administration : token en coffre, rotation planifiée.

### Si jq est absent

C'est la seule dépendance non standard. `jq` est un binaire statique unique :

```bash
sudo cp jq-linux-amd64 /usr/local/bin/jq
sudo chmod 755 /usr/local/bin/jq
jq --version
```

---

## 3. Comptes et répertoires

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin sonarimp
sudo usermod -aG sonarqube sonarimp

# Répertoire lu par Project Move à l'import
sudo chmod 770 /opt/sonarqube/data/governance/project_dumps/import

sudo mkdir -p /opt/sonar-import-worker
sudo mkdir -p /etc/sonar-import-worker
sudo mkdir -p /var/lib/sonar-import-worker/{done,retry,work}
sudo chown -R sonarimp:sonarimp /var/lib/sonar-import-worker
```

Vérifier le propriétaire réel avant d'adapter le groupe :

```bash
stat -c '%U %G %a' /opt/sonarqube/data/governance/project_dumps/import
```

---

## 4. Dépôt du script

```bash
sudo cp sonar-import-worker.sh /opt/sonar-import-worker/
sudo chmod 755 /opt/sonar-import-worker/sonar-import-worker.sh
sudo bash -n /opt/sonar-import-worker/sonar-import-worker.sh && echo "syntaxe OK"
```

> Le bit d'exécution compte. Sans lui le worker n'échoue pas bruyamment : il ne
> démarre simplement pas, et le cycle paraît vide. C'est le premier point à
> vérifier si le service « ne fait rien ».

---

## 5. Configuration

```bash
sudo cp config.example.sh /etc/sonar-import-worker/config.sh
sudo chown root:sonarimp /etc/sonar-import-worker/config.sh
sudo chmod 640 /etc/sonar-import-worker/config.sh
sudo vi /etc/sonar-import-worker/config.sh
```

À renseigner en priorité :

| Clé | Remarque |
|---|---|
| `IMPORT_DIR` | chemin exact du répertoire d'import de **votre** installation |
| `TARGET_KEY_REGEX` | le **premier groupe capturant doit être l'espace_id** |
| `PERMISSION_TEMPLATE` | nom exact du template appliqué par le portail |
| `QUALITY_GATE` | laisser vide pour conserver le gate par défaut |
| `DEVOPS_ALM_SETTING` | nom du binding GitLab configuré globalement |

Le premier groupe capturant sert à comparer la clé cible au répertoire
Artifactory. C'est ce qui empêche qu'un projet d'un espace atterrisse dans un
autre — un contrôle utile le jour où deux entités migrent en parallèle.

### Les tokens dans un fichier séparé

```bash
sudo tee /etc/sonar-import-worker/env >/dev/null <<'EOF'
SONAR_TOKEN=squ_xxxxxxxxxxxxxxxxxxxx
ARTIFACTORY_TOKEN=cmVmdGtuOjAxOjE3xxxxxxxx
EOF

sudo chown root:sonarimp /etc/sonar-import-worker/env
sudo chmod 640 /etc/sonar-import-worker/env
```

systemd les injecte comme variables d'environnement ; le script les utilise en
priorité sur ce que contient `config.sh`.

### Restreindre la lecture des dépôts Artifactory

À faire avant le premier export réel, côté Artifactory :

> Une archive Project Move contient l'historique complet d'un projet — code
> analysé, incidents, mesures. Le découpage par `espace_id` sert à
> l'organisation, pas au cloisonnement. La **lecture** de
> `sonar-projects-to-migrate` et `sonar-projects-migrated` doit être réservée
> au compte technique du worker et aux comptes de dépôt des instances sources.

---

## 6. Le contrôle d'habilitation

`ENFORCE_REQUESTER_ADMIN` vérifie que la personne ayant lancé l'export est bien
administrateur du projet cible sur l'instance centrale. Le worker part de
`external_identity` (l'identifiant annuaire porté par le manifeste), pas du
login ni de l'email : c'est le seul pivot stable entre les deux instances.

Il accepte trois situations : administrateur global, permission `admin` directe
sur le projet, ou permission `admin` héritée d'un groupe dont la personne est
membre. Un droit accordé au pseudo-groupe *Anyone* est rejeté.

**Laisser `false` pendant le POC**, l'activer avant d'ouvrir le service aux
équipes. Le manifeste porte déjà l'identité : l'activer plus tard ne demandera
aucune reprise côté export.

---

## 7. Service et minuterie systemd

`/etc/systemd/system/sonar-import-worker.service`

```ini
[Unit]
Description=Import des projets SonarQube publies dans Artifactory
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=sonarimp
Group=sonarqube
EnvironmentFile=/etc/sonar-import-worker/env
ExecStart=/opt/sonar-import-worker/sonar-import-worker.sh \
          -c /etc/sonar-import-worker/config.sh

# Un gros projet peut demander plusieurs dizaines de minutes d'import.
TimeoutStartSec=7200

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/sonar-import-worker /var/lock \
               /opt/sonarqube/data/governance/project_dumps/import

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/sonar-import-worker.timer`

```ini
[Unit]
Description=Cycle d'import toutes les 10 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=10min
RandomizedDelaySec=60
Unit=sonar-import-worker.service

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sonar-import-worker.timer
systemctl list-timers sonar-import-worker.timer
```

Le verrou `flock` interdit deux cycles simultanés. Un cycle qui dépasse
l'intervalle de la minuterie ne se recouvre donc pas : le suivant sort en
code 4 et attend son tour.

---

## 8. Banc d'essai

Avant de toucher à l'instance centrale :

```bash
cd tests && ./run-tests.sh
```

Le banc lance un faux SonarQube central et un faux Artifactory sur `127.0.0.1`
et déroule 51 vérifications : refus d'un projet cible non vide, refus d'une
version divergente, refus d'un plugin manquant, contrôle d'habilitation par
groupe, empreinte corrompue, ordre `import → suppression → renommage`,
reconfiguration, idempotence, verrou concurrent, codes de sortie — puis les
scénarios du mode cluster (§12 à 18, voir `TOWER.md`).

```bash
PORT=18095 ./run-tests.sh     # si le port par défaut est pris
```

Rejouez-le après toute adaptation : format de clé, nom du lien, nom du template.
Modifier `tests/run-tests.sh` suffit pour couvrir un cas propre à votre contexte.

---

## 9. Vérification avant mise en service

### 9.1 Les points à confirmer sur votre instance

```bash
export SONAR_TOKEN=squ_xxx
export SONAR_URL=https://sonar-centrale.groupe.corp

# Version et édition
curl -su "$SONAR_TOKEN:" "$SONAR_URL/api/server/version"

# Le renommage : quelle signature accepte votre version ?
# Le worker essaie les deux (from/to puis project/newKey), mais autant savoir.
curl -su "$SONAR_TOKEN:" "$SONAR_URL/api/webservices/list" \
  | jq '.webServices[] | select(.path=="api/projects")
        | .actions[] | select(.key=="update_key") | .params[].key'

# Le template de permissions existe-t-il sous ce nom exact ?
curl -su "$SONAR_TOKEN:" "$SONAR_URL/api/permissions/search_templates" \
  | jq -r '.permissionTemplates[].name'

# Le binding GitLab global
curl -su "$SONAR_TOKEN:" "$SONAR_URL/api/alm_settings/list" | jq .
```

### 9.2 Essai à blanc

```bash
sudo -u sonarimp env \
  SONAR_TOKEN=squ_xxx ARTIFACTORY_TOKEN=xxx \
  /opt/sonar-import-worker/sonar-import-worker.sh \
  -c /etc/sonar-import-worker/config.sh --dry-run
```

Le mode `--dry-run` déroule **tous** les contrôles et n'écrit rien : ni sur
l'instance, ni dans l'état, ni dans Artifactory. C'est le mode à utiliser pour
diagnostiquer un dossier refusé.

Pour n'examiner qu'une demande :

```bash
... --dry-run --manifest espace12/com.entite_mon-projet.manifest.json
```

### 9.3 Premier passage réel

Faites-le sur **un projet de test sans valeur**, pas sur le premier projet
d'équipe. Vérifiez ensuite, dans l'interface :

- l'historique d'analyses est bien celui de l'instance source ;
- la clé est celle du portail ;
- les groupes du portail sont toujours attachés au projet ;
- le quality gate attendu est appliqué ;
- le lien MIGRATION a disparu.

Les groupes et le template sont des objets d'instance : la suppression du projet
vide ne les détruit pas. Seule l'association projet↔template doit être rejouée,
ce que fait le worker.

---

## 10. Exploitation

### Codes de sortie

| Code | Signification |
|---|---|
| 0 | cycle terminé |
| 1 | configuration invalide |
| 2 | SonarQube injoignable |
| 3 | Artifactory injoignable |
| 4 | verrou déjà pris — un cycle est en cours |

### Suivi

```bash
journalctl -u sonar-import-worker.service -f
journalctl -u sonar-import-worker.service --since today | grep -E 'REJETE|TERMINEE'
```

Un dossier **rejeté** ne sera pas retenté : la cause est structurelle (version,
plugin, projet non vide, habilitation). Un dossier en **réessai** est retenté
jusqu'à `MAX_ATTEMPTS`, puis abandonné avec une trace explicite.

### Rejouer un dossier corrigé

L'état est un simple répertoire de marqueurs :

```bash
sudo rm /var/lib/sonar-import-worker/done/<identifiant>
sudo rm -f /var/lib/sonar-import-worker/retry/<identifiant>
```

Le cycle suivant le reprendra depuis le début.

---

## 11. Diagnostics fréquents

| Symptôme | Cause probable |
|---|---|
| Le cycle ne traite rien, aucun message | Le script n'est pas exécutable (`chmod 755`) |
| « projet cible inexistant », en boucle | Le projet n'a pas encore été créé dans le portail |
| « contient deja des analyses » | Une CI a analysé le projet cible avant la migration |
| « plugins incompatibles » | Le central n'a pas encore installé les plugins de l'entité |
| « n est pas administrateur » | L'identité annuaire ne correspond pas, ou le droit passe par un groupe absent en central |
| Import bloqué en `PENDING` | Compute Engine saturé — allonger `CE_TIMEOUT_SECONDS` |
| Code 4 à chaque cycle | Un cycle précédent tourne encore : intervalle de minuterie trop court |

Sur « n est pas administrateur », comparer les deux côtés :

```bash
curl -su "$SONAR_TOKEN:" \
  "$SONAR_URL/api/v2/users-management/users?externalIdentity=u123456" | jq .
```

Si la réponse est vide, la personne n'existe pas encore sur l'instance centrale :
c'est en général qu'elle ne s'y est jamais connectée.
