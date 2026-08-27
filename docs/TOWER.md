# Import sur l'instance centrale en cluster — montage Ansible Tower

Ce guide couvre le cas où l'instance SonarQube centrale comporte **plusieurs
nœuds applicatifs sans système de fichiers partagé**.

Il complète `INSTALL.md`, qui reste la référence pour le worker lui-même
(prérequis, comptes, configuration, contrôles). Si votre instance centrale est
mono-nœud, ce document ne vous concerne pas : la minuterie systemd décrite dans
`INSTALL.md` suffit.

---

## 1. Le problème, en une phrase

À l'import, SonarQube planifie la tâche Compute Engine sur **un nœud
applicatif imprévisible**, qui lit le fichier sur **son** disque local.

Poser l'archive sur un seul nœud donne donc un import qui réussit une fois sur
deux, au hasard. Un échec aléatoire coûte bien plus cher à diagnostiquer qu'un
échec franc — c'est le genre de défaut qui survit à la recette et se révèle en
production.

La documentation Sonar propose de réduire le déploiement à un seul réplica le
temps de l'import. Sur une instance partagée par toute l'entreprise, ce n'est
pas une opération de routine acceptable.

**La réponse retenue : l'archive est présente sur tous les nœuds avant que
l'import ne soit déclenché.** Peu importe alors quel nœud prend la tâche.

---

## 2. Découpage du worker

Le worker connaît trois modes. Le mode par défaut reste le mode mono-nœud :
le découpage ne dégrade pas l'existant.

| Mode | Écrit sur l'instance ? | Rôle |
|---|---|---|
| *(défaut)* | oui | tout le cycle en une fois — instance mono-nœud |
| `--prepare` | **non** | contrôles, téléchargement, dépôt local, descripteur |
| `--commit` | oui | rejeu du contrôle « projet vide », import, suppression, renommage, reconfiguration |

Entre les deux, Ansible recopie l'archive. Le point de coupe a été choisi juste
avant la première écriture : tant que `--commit` n'a pas tourné, aucune trace
n'existe sur l'instance et un abandon ne coûte rien.

### Le descripteur de travail

`--prepare` écrit `/var/lib/sonar-import-worker/work/pending.json`. C'est le
contrat entre le worker et Ansible :

```json
{
  "generated_at": "2026-08-26T09:12:04Z",
  "items": [
    {
      "manifest": "espace12/com.entite_mon-projet.manifest.json",
      "dossier": "espace12",
      "archive": "com.entite_mon-projet.zip",
      "source_key": "com.entite:mon-projet",
      "target_key": "p-espace12-mon-projet",
      "scm_repository": "app1234/mon-projet",
      "local_path": "/opt/sonarqube/data/governance/project_dumps/import/com.entite:mon-projet.zip"
    }
  ]
}
```

`local_path` est le chemin qu'Ansible doit répliquer à l'identique sur les
autres nœuds.

### La fenêtre entre les deux phases

Entre la préparation et le commit, une CI peut analyser le projet cible et le
rendre non vide. La fenêtre est courte, mais réelle sur une instance ouverte à
tous, et un import par-dessus un projet non vide est irrattrapable.

`--commit` **rejoue donc le contrôle « projet vide »** juste avant de
déclencher. Un appel d'API, et le garde-fou redevient valable au moment où il
compte. C'est le scénario 16 du banc d'essai.

---

## 3. Ce que Tower apporte, et ce qu'il n'apporte pas

**Il apporte** l'inventaire des deux nœuds, donc le fan-out sans effort ; le
magasin de credentials, donc les jetons hors des serveurs ; et un journal
centralisé de qui a lancé quoi.

**Il n'apporte pas** la logique. Les contrôles, l'attente de la tâche CE et
l'ordre `import → confirmation → suppression → renommage` restent dans le
script, où ils sont couverts par 51 assertions sur maquette. Réécrits en tâches
Ansible, ils perdraient cette couverture — c'est la raison principale de ce
découpage plutôt qu'une réécriture.

Tower ordonnance et transporte. Le script décide.

---

## 4. Le projet Tower

### 4.1 Arborescence du dépôt Git

```
ansible/
├── ansible.cfg
├── sonar_import.yml              ← playbook du job template
├── collections/requirements.yml
└── roles/sonar_import/
    ├── defaults/main.yml
    └── tasks/main.yml
```

### 4.2 Inventaire

Un groupe contenant **les deux nœuds applicatifs** :

```ini
[sonar_central_app]
sonar-app-01.groupe.corp
sonar-app-02.groupe.corp
```

Le rôle désigne le nœud pilote par `groups['sonar_central_app'] | sort | first`.
Le tri rend le choix déterministe : le journal Tower désignera toujours la même
machine, et « où regarder » ne sera jamais une question.

### 4.3 Type de credential personnalisé

Créer un *Credential Type* nommé **Sonar Migration**.

Entrées :

```yaml
fields:
  - id: sonar_token
    type: string
    label: Jeton SonarQube (Administer System)
    secret: true
  - id: artifactory_token
    type: string
    label: Jeton Artifactory
    secret: true
required:
  - sonar_token
  - artifactory_token
```

Injecteurs :

```yaml
env:
  SONAR_TOKEN: "{{ sonar_token }}"
  ARTIFACTORY_TOKEN: "{{ artifactory_token }}"
```

Le rôle relaie ces variables vers les nœuds via `lookup('env', …)`. Le worker
lit déjà ses jetons depuis l'environnement en priorité sur `config.sh` :
**aucune modification du script n'est nécessaire**, et les jetons disparaissent
des fichiers posés sur les serveurs.

### 4.4 Job template

| Réglage | Valeur | Pourquoi |
|---|---|---|
| Playbook | `sonar_import.yml` | |
| Inventaire | celui du §4.2 | |
| Credentials | machine + **Sonar Migration** | |
| **Enable Concurrent Jobs** | **désactivé** | deux cycles simultanés sur une instance partagée : le scénario à ne pas tester en production |
| **Timeout** | `> CE_TIMEOUT_SECONDS` (ex. 7200 s) | un job coupé en plein import laisse un projet importé sous la clé source, non renommé |
| Verbosity | 1 | 0 masque les messages du worker, qui sont l'essentiel du diagnostic |

Le `flock` du worker reste en place. Il protège le nœud ; le réglage Tower
protège l'orchestration. Les deux sont utiles, aucun ne remplace l'autre.

### 4.5 Planification

Une *Schedule* toutes les 10 à 15 minutes. Le worker est idempotent : un cycle
sans travail sort proprement, sans effet.

`MAX_BATCH=1` par défaut — un projet par exécution. Pour aller plus vite,
rapprocher la planification plutôt qu'augmenter le lot : la durée d'un job reste
bornée et un incident ne concerne qu'un projet.

---

## 5. Déroulé d'un cycle

```
1  --prepare        nœud pilote   contrôles, téléchargement, dépôt, descripteur
2  slurp            nœud pilote   lecture du descripteur
3  fetch            nœud pilote → contrôleur Tower
4  copy             contrôleur → autres nœuds
5  stat + assert    tous          l'archive est-elle partout ?
6  --commit         nœud pilote   import, suppression, renommage, reconfiguration
7  always: cleanup  tous          suppression des copies locales
```

L'étape 5 n'est pas décorative. Sans elle, un transport partiellement échoué
mènerait exactement au défaut qu'on cherche à éliminer : un import qui réussit
ou échoue selon le nœud tiré au sort. Mieux vaut refuser de déclencher.

L'étape 7 est dans un bloc `always` : les copies partent même en cas d'échec.
Artifactory reste la référence, les copies locales sont jetables — et un
répertoire d'import qui grossit en silence finit toujours par se rappeler à
vous.

### Mode de transport

`sonar_import_transfer` accepte deux valeurs :

| Valeur | Chemin | Quand |
|---|---|---|
| `via_tower` *(défaut)* | nœud → contrôleur → nœuds | aucun flux nœud-à-nœud requis ; le plus simple à faire homologuer |
| `node_to_node` | `rsync` direct | plus rapide sur les gros dumps ; exige un flux SSH entre nœuds applicatifs |

`via_tower` fait transiter le fichier deux fois. Sur des dumps volumineux c'est
mesurable : à arbitrer au POC, avec des chiffres réels plutôt qu'a priori.

Le rôle n'utilise que `ansible.builtin`, y compris pour `rsync` — aucune
collection externe à faire valider.

---

## 6. Exploitation

### Essai à blanc

```bash
sudo -u sonarimp env SONAR_TOKEN=squ_xxx ARTIFACTORY_TOKEN=xxx \
  /opt/sonar-import-worker/sonar-import-worker.sh \
  -c /etc/sonar-import-worker/config.sh --prepare --dry-run
```

`--prepare --dry-run` déroule tous les contrôles sans rien déposer ni écrire.
C'est le mode de diagnostic d'un dossier refusé.

`--commit` refuse `--dry-run` : un commit qui n'écrit pas n'a pas de sens, et
la combinaison serait un piège.

### Banc d'essai

```bash
cd tests && ./run-tests.sh          # 51 assertions
PORT=18095 ./run-tests.sh           # si le port par défaut est pris
```

Les scénarios 12 à 18 couvrent le mode cluster : préparation sans écriture,
exactitude du descripteur, borne `MAX_BATCH`, équivalence du commit avec le mode
monolithique, refus quand le projet cible s'est rempli entre les deux phases,
commit sans descripteur, et incompatibilité `--commit --dry-run`.

À rejouer après toute adaptation.

### Reprise après un import partiel

Le cas à connaître : l'import a réussi, mais la suppression ou le renommage a
échoué. Le journal affiche alors une ligne commençant par `ALERTE`.

L'instance contient un projet sous la **clé source**, et le projet vide du
portail existe peut-être encore. Le cycle suivant refusera le dossier — la clé
source est prise — ce qui est le comportement voulu : pas de correction
automatique sur un état incertain.

Reprise manuelle :

1. constater l'état des deux clés dans l'interface ;
2. supprimer le projet vide du portail s'il subsiste ;
3. renommer le projet importé vers la clé du portail ;
4. réappliquer template de permissions, quality gate et binding ;
5. purger le marqueur d'état pour que le worker cesse de s'en occuper :

```bash
sudo rm -f /var/lib/sonar-import-worker/done/<identifiant>
sudo rm -f /var/lib/sonar-import-worker/retry/<identifiant>
```

### Diagnostics propres au cluster

| Symptôme | Cause probable |
|---|---|
| L'assertion « archive absente » échoue | Droits ou espace disque sur le second nœud |
| Import en échec « fichier introuvable » | Le transport a été sauté — vérifier `sonar_import_transfer` |
| « n est plus vide depuis la preparation » | Une CI a analysé le projet cible pendant le cycle : garde-fou correct, à traiter avec l'équipe |
| Le job dépasse le timeout Tower | Gros projet — augmenter le timeout, pas `CE_TIMEOUT_SECONDS` seul |
| `rc=4` à chaque exécution | Un cycle précédent tourne encore : espacer la planification |

---

## 7. Avant la première exécution réelle

- [ ] Le worker est déployé et **exécutable** (`chmod 755`) sur les deux nœuds
- [ ] `config.sh` et le répertoire d'import existent sur les deux nœuds
- [ ] Le compte `sonarimp` peut écrire dans le répertoire d'import des deux nœuds
- [ ] « Enable Concurrent Jobs » est désactivé sur le job template
- [ ] Le timeout du job dépasse `CE_TIMEOUT_SECONDS`
- [ ] La lecture des dépôts Artifactory est restreinte au compte technique
- [ ] Le banc d'essai passe : 51/51
- [ ] Un premier passage a été fait sur un **projet de test sans valeur**
