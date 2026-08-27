# Installation — sonar-export-publisher

Service à installer **sur le serveur SonarQube source**. Il publie vers Artifactory
les exports de projets destinés à la migration.

Deux implémentations équivalentes sont fournies :

| Fichier | Dépendances | À privilégier si |
|---|---|---|
| `sonar-export-publisher.sh` | bash 4+, curl, **jq**, coreutils, util-linux | Vous ne voulez pas installer Python |
| `sonar_export_publisher.py` | Python 3.9+, `requests`, `PyYAML` | Python est déjà présent et maintenu |

Ce guide couvre la **version shell**. Pour la version Python, seules les
sections 3 et 5 changent — les commandes sont indiquées en fin de document.

---

## 1. Prérequis

| Élément | Détail |
|---|---|
| OS | Linux avec systemd |
| Binaires | `bash` 4+, `curl`, `jq`, `sha256sum`, `flock`, `awk`, `stat` |
| Accès disque | lecture **et écriture** sur le répertoire d'export de SonarQube |
| Réseau | HTTPS vers l'instance SonarQube locale et vers Artifactory |
| Token SonarQube | compte technique, droit *Administer System* |
| Token Artifactory | droit de dépôt sur `sonar-projects-to-migrate` |

L'écriture sur le répertoire d'export est nécessaire : le script supprime
l'archive après publication, et la déplace en quarantaine en cas d'échec répété.

### Pourquoi *Administer System*

Trois besoins : lire `api/ce/activity` toutes tâches confondues, obtenir `email`
et `externalIdentity` dans la recherche d'utilisateurs, et lister les plugins.
Un droit moindre fera échouer la résolution d'identité sans message explicite.
C'est le piège d'installation le plus fréquent.

### Si jq est absent

C'est la seule dépendance non standard. Sur un serveur sans dépôt de paquets,
`jq` est un binaire statique unique — le déposer suffit :

```bash
sudo cp jq-linux-amd64 /usr/local/bin/jq
sudo chmod 755 /usr/local/bin/jq
jq --version
```

---

## 2. Comptes et répertoires

```bash
# Compte de service, sans shell
sudo useradd --system --no-create-home --shell /usr/sbin/nologin sonarpub

# Accès au répertoire d'export de SonarQube
sudo usermod -aG sonarqube sonarpub
sudo chmod 770 /opt/sonarqube/data/governance/project_dumps/export

# Arborescence
sudo mkdir -p /opt/sonar-export-publisher
sudo mkdir -p /etc/sonar-export-publisher
sudo mkdir -p /var/lib/sonar-export-publisher/{done,retry,quarantine}
sudo chown -R sonarpub:sonarpub /var/lib/sonar-export-publisher
```

Vérifier le propriétaire réel du répertoire d'export avant d'adapter le groupe :

```bash
stat -c '%U %G %a' /opt/sonarqube/data/governance/project_dumps/export
```

---

## 3. Dépôt du script

```bash
sudo cp sonar-export-publisher.sh /opt/sonar-export-publisher/
sudo chmod 755 /opt/sonar-export-publisher/sonar-export-publisher.sh
sudo bash -n /opt/sonar-export-publisher/sonar-export-publisher.sh && echo "syntaxe OK"
```

Aucun environnement virtuel, aucun paquet à installer.

---

## 4. Configuration

```bash
sudo cp config.example.sh /etc/sonar-export-publisher/config.sh
sudo chown root:sonarpub /etc/sonar-export-publisher/config.sh
sudo chmod 640 /etc/sonar-export-publisher/config.sh
sudo vi /etc/sonar-export-publisher/config.sh
```

À renseigner : l'URL de l'instance source, le chemin exact du répertoire
d'export, le host de l'instance centrale, et le format des clés produites par le
portail.

### Les tokens dans un fichier séparé

Ne pas les mettre dans `config.sh` : ils seraient lisibles par tout membre du
groupe `sonarpub`, et se retrouveraient dans vos sauvegardes de configuration.

```bash
sudo tee /etc/sonar-export-publisher/env >/dev/null <<'EOF'
SONAR_TOKEN=squ_xxxxxxxxxxxxxxxxxxxx
ARTIFACTORY_TOKEN=cmVmdGtuOjAxOjE3xxxxxxxx
EOF

sudo chown root:sonarpub /etc/sonar-export-publisher/env
sudo chmod 640 /etc/sonar-export-publisher/env
```

systemd les injecte comme variables d'environnement ; le script les utilise en
priorité sur ce que contient `config.sh`.

---

## 5. Service et minuterie systemd

`/etc/systemd/system/sonar-export-publisher.service`

```ini
[Unit]
Description=Publication des exports SonarQube vers Artifactory
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=sonarpub
Group=sonarqube
EnvironmentFile=/etc/sonar-export-publisher/env
ExecStart=/opt/sonar-export-publisher/sonar-export-publisher.sh \
          -c /etc/sonar-export-publisher/config.sh

# Durcissement
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/sonar-export-publisher /var/lock \
               /opt/sonarqube/data/governance/project_dumps/export

[Install]
WantedBy=multi-user.target
```

> `PrivateTmp=true` donne au service son propre `/tmp`. Le script y écrit un
> fichier temporaire éphémère pour capturer les messages d'erreur du parsing —
> aucun impact, mais ne le retirez pas de `ReadWritePaths` par erreur.

`/etc/systemd/system/sonar-export-publisher.timer`

```ini
[Unit]
Description=Cycle de publication toutes les 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
RandomizedDelaySec=30
Unit=sonar-export-publisher.service

[Install]
WantedBy=timers.target
```

Activation :

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sonar-export-publisher.timer
systemctl list-timers sonar-export-publisher.timer
```

Une minuterie systemd plutôt que cron : journalisation intégrée, pas de
recouvrement d'exécutions, statut interrogeable.

---

## 6. Banc d'essai

Avant de toucher à une instance réelle :

```bash
cd tests && ./run-tests.sh
```

Le banc lance un faux SonarQube et un faux Artifactory sur `127.0.0.1` et
déroule 24 vérifications : détection du lien, rejet d'un host étranger, clé
encodée, compte local refusé, ordre de dépôt archive puis manifeste, intégrité
des empreintes, idempotence, quarantaine, verrou concurrent, codes de sortie.
Aucune instance réelle n'est sollicitée.

Rejouez-le après toute adaptation — format de clé, nom de lien, type de tâche
CE. Modifier `tests/scenario.json` suffit pour couvrir un cas propre à votre
contexte.

---

## 7. Vérification avant mise en service

### 7.1 Les trois points à confirmer sur votre 2026.2.1

```bash
export SONAR_TOKEN=squ_xxx
export SONAR_URL=https://sonar.entite.corp

# 1. Quel est le nom réel du type de tâche d'export ?
curl -su "$SONAR_TOKEN:" "$SONAR_URL/api/ce/activity?ps=50" \
  | jq -r '.tasks[].type' | sort -u

# 2. La recherche d'utilisateurs renvoie-t-elle externalIdentity ?
curl -su "$SONAR_TOKEN:" \
  "$SONAR_URL/api/v2/users-management/users?q=votre.login" | jq .

# 3. Les liens de projet sont-ils bien exposés ?
curl -su "$SONAR_TOKEN:" \
  "$SONAR_URL/api/project_links/search?projectKey=UN_PROJET" | jq .
```

Si le premier appel ne montre pas `PROJECT_EXPORT`, reporter la valeur réelle
dans `CE_TASK_TYPE` du fichier de configuration.

### 7.2 Essai à blanc

Sur un projet de test : poser le lien `MIGRATION`, lancer l'export depuis l'IHM,
puis :

```bash
sudo -u sonarpub env \
  SONAR_TOKEN=squ_xxx ARTIFACTORY_TOKEN=xxx \
  /opt/sonar-export-publisher/sonar-export-publisher.sh \
  -c /etc/sonar-export-publisher/config.sh --dry-run
```

Le mode `--dry-run` n'écrit rien, ne supprime rien, et affiche le manifeste tel
qu'il serait publié. Relire en particulier la clé cible et l'`espace_id` : c'est
là que se voient les erreurs de décodage d'URL.

### 7.3 Premier passage réel

```bash
sudo systemctl start sonar-export-publisher.service
sudo journalctl -u sonar-export-publisher.service -n 50 --no-pager
```

Vérifier ensuite dans Artifactory la présence des **deux** fichiers sous
`sonar-projects-to-migrate/<espace_id>/` — l'archive et son manifeste.

---

## 8. Exploitation

**Suivre l'activité**

```bash
journalctl -u sonar-export-publisher.service -f
```

**Consulter l'état**

L'état est un simple arbre de fichiers, lisible sans outil :

```bash
ls -l /var/lib/sonar-export-publisher/done/     # tâches terminées
cat  /var/lib/sonar-export-publisher/done/AY8x  # statut + motif
ls -l /var/lib/sonar-export-publisher/retry/    # tâches en cours de réessai
```

**Rejouer une tâche**

```bash
/opt/sonar-export-publisher/sonar-export-publisher.sh \
  -c /etc/sonar-export-publisher/config.sh --task AY8xxxxxxxx
```

L'option `--task` force le traitement même si la tâche est déjà marquée. Utile
après avoir corrigé un lien mal formé.

**Traiter la quarantaine**

Les archives abandonnées après six tentatives se trouvent dans
`/var/lib/sonar-export-publisher/quarantine`. Corriger la cause — le plus
souvent un lien absent ou mal formé — remettre l'archive dans le répertoire
d'export, supprimer le marqueur dans `done/`, puis rejouer la tâche.

---

## 9. Diagnostic

| Message | Cause la plus fréquente |
|---|---|
| `pas de lien « MIGRATION »` | Lien non posé, ou nom différent |
| `host inattendu` | URL copiée depuis une autre instance |
| `clé cible hors format` | Paramètres parasites, clé tronquée, ou regex à ajuster |
| `archive absente` | Export encore en cours, ou nom de fichier inattendu |
| `archive encore en cours d'écriture` | Normal sur un gros projet, se résout seul |
| `utilisateur non résolu` | Le token n'a pas *Administer System* |
| `compte local` | Export lancé par un compte technique |
| Code de sortie 4 | Un cycle tourne encore — normal sur projet volumineux |

**Le cas trompeur.** Si la clé cible contient encore des `%3A`, c'est que le
décodage d'URL n'a pas eu lieu. Le script s'en charge dans `urldecode` ; si vous
avez modifié `url_param_id`, vérifiez que vous passez bien par cette fonction et
non par un découpage manuel.

**Un piège de bash à connaître.** Ne mettez jamais d'apostrophe dans un message
`${VAR:?message}` : le parseur de bash s'y perd et le script devient
insyntaxique, avec une erreur signalée cent lignes plus loin. Le script
n'utilise volontairement pas cette construction.

---

## 10. Ce que ce script ne fait pas

Il ne vérifie **aucun droit sur l'instance centrale** — il ne la connaît pas. Il
ne décide pas de la destination : il recopie une déclaration. Il ne supprime
rien côté cible.

Tous les contrôles sensibles — existence du projet cible, absence d'analyses
avant suppression, droits du demandeur, cohérence de version — appartiennent au
script d'import, côté instance centrale, où l'information est fraîche au moment
de décider.

---

## Annexe — version Python

Remplacer les sections 3 et 5 :

```bash
# Section 3
sudo cp sonar_export_publisher.py /opt/sonar-export-publisher/
sudo python3 -m venv /opt/sonar-export-publisher/venv
sudo /opt/sonar-export-publisher/venv/bin/pip install requests PyYAML

# Sans accès Internet, depuis un poste connecté :
#   pip download requests PyYAML -d wheels/
# puis sur le serveur :
#   sudo /opt/sonar-export-publisher/venv/bin/pip install \
#        --no-index --find-links wheels/ requests PyYAML

# Section 4 : utiliser config.example.yml au lieu de config.example.sh

# Section 5 : ExecStart devient
#   /opt/sonar-export-publisher/venv/bin/python \
#   /opt/sonar-export-publisher/sonar_export_publisher.py \
#   --config /etc/sonar-export-publisher/config.yml
```

Les deux versions produisent le **même manifeste** et lisent la même
configuration logique. Elles sont interchangeables : le script d'import côté
central ne voit aucune différence.
