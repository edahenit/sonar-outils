# Analyse — Migration SonarQube via Artifactory

Solution proposée : deux scripts planifiés, Artifactory comme bus de transport.
Ce document évalue la solution, identifie ce qui manque, et propose une version durcie.

---

## 1. La solution telle que décrite

**Côté instance source**
Un script s'exécute toutes les 5 minutes. Il scanne le répertoire d'export de SonarQube et téléverse les archives trouvées vers le dépôt Artifactory `sonar-projects-to-migrate`.

**Côté équipe projet**
Le projet est renommé manuellement, ou via l'API, avec la nouvelle clé générée par le portail.

**Côté instance centrale**
Un second script récupère les archives depuis `sonar-projects-to-migrate`, les importe via l'API, puis déplace les fichiers traités vers `sonar-projects-migrated`.

---

## 2. Ce qui est bien vu

**Le découplage par Artifactory est le bon choix.** Aucun flux réseau direct entre les deux serveurs SonarQube, aucune ouverture de firewall à négocier, aucun partage de secret entre les deux côtés. Chaque script n'utilise que ses propres identifiants, sur son propre serveur. C'est précisément le « canal de transfert sécurisé » qui figurait dans les prérequis, et il existe déjà dans l'entreprise.

**La simplicité est un atout réel, pas un compromis.** Deux scripts planifiés sont compréhensibles, débogables et transmissibles. Une pipeline GitLab avec rôles Ansible et contrôle d'habilitation coûte dix fois plus cher à construire et à maintenir. Pour un POC, et probablement au-delà, cette simplicité a plus de valeur qu'une architecture élégante.

**Artifactory apporte gratuitement plusieurs briques** qu'il aurait fallu écrire : stockage, checksums, journal des dépôts et téléchargements, contrôle d'accès par dépôt, quotas, et une interface où l'on voit d'un coup d'œil ce qui est en attente.

**Le couple `to-migrate` / `migrated` est une machine à états visible.** N'importe qui peut ouvrir Artifactory et savoir où en sont les choses. À ne pas sous-estimer en exploitation.

---

## 3. Ce qui manque — par ordre de gravité

### 3.1 Aucun contrôle d'habilitation

C'est le point le plus grave. Dans la solution décrite, **quiconque peut déclencher un export sur l'instance source peut faire importer un projet dans l'instance centrale**. Rien ne vérifie que le demandeur a le droit sur le projet cible.

Combiné au point suivant, cela permet à une équipe de faire atterrir son historique dans l'espace d'une autre application.

### 3.2 La clé cible est décidée unilatéralement côté source

Puisque l'équipe renomme son projet source avec la clé de son choix, c'est **le côté source qui décide où l'import atterrit**. L'instance centrale ne fait qu'obéir au nom du fichier.

Il n'existe aucun point de contrôle entre « je choisis une clé » et « le projet est créé sur l'instance centrale avec cette clé ».

### 3.3 Le déclencheur est un répertoire, pas une intention

Le script publie tout ce qu'il trouve dans le répertoire d'export. Or ce répertoire sert aussi aux exports faits pour d'autres raisons : sauvegarde, test, diagnostic. **Un export de confort devient une migration.**

Il faut distinguer « un fichier existe » de « quelqu'un a demandé une migration ».

### 3.4 Collision de clé à l'import — étape manquante

Le portail a déjà créé le projet cible avec cette clé. L'import de Project Move **crée** le projet. La séquence décrite ne contient aucune étape de suppression du projet vide : l'import échouera.

Cette suppression est une opération destructive sur l'instance centrale. Elle doit être explicite, contrôlée et journalisée — pas un effet de bord d'un script planifié.

### 3.5 Renommer côté source casse leur production immédiatement

Dès le renommage, les pipelines, badges, liens et bindings SonarLint du projet source cessent de fonctionner — **avant** que la migration ne soit terminée. Si l'import échoue, l'équipe reste avec un projet cassé des deux côtés.

### 3.6 Aucune métadonnée de corrélation

Un fichier `PROJET.zip` ne dit pas qui l'a demandé, depuis quelle instance, vers quelle clé cible, quand, ni sous quelle version de SonarQube. Le script central importe à l'aveugle, et aucun audit n'est possible après coup.

### 3.7 Pas de vérification de version

Si les versions divergent — ce qui arrivera au premier upgrade de l'une des deux instances — l'import échoue. Rien ne le détecte avant la tentative.

### 3.8 Course sur les fichiers partiels

Le script peut lire une archive pendant que SonarQube est encore en train de l'écrire. Résultat : un zip tronqué publié dans Artifactory, et un import qui échoue de façon incompréhensible.

### 3.9 Gestion des échecs indéfinie

Si un import échoue, le fichier reste dans `to-migrate` et sera réessayé toutes les 5 minutes, indéfiniment. Sans compteur, sans quarantaine, sans alerte.

### 3.10 L'import est asynchrone

`api/project_dump/import` retourne un identifiant de tâche Compute Engine. Il ne faut **pas** considérer l'import comme réussi au retour de l'appel, ni déplacer le fichier vers `migrated` avant que la tâche soit en `SUCCESS`.

### 3.11 Configuration post-import absente

Template de permissions, quality gate, quality profile, binding DevOps, portefeuille : rien de tout cela n'est réappliqué. Le projet importé arrive nu.

### 3.12 Sensibilité des archives

Une archive Project Move contient l'historique complet d'analyse. Elle mérite une politique de rétention et un contrôle d'accès explicites sur les deux dépôts Artifactory.

---

## 4. La version durcie

Les améliorations ci-dessous conservent l'esprit de la solution — deux scripts, Artifactory au milieu — et corrigent les manques. Aucune n'ajoute de composant.

### 4.1 Un manifeste à côté de chaque archive

L'amélioration la plus rentable. Chaque publication comporte deux fichiers : `PROJET.zip` et `PROJET.manifest.json`.

```json
{
  "schema_version": "1.0",
  "request_id": "b7c1e2f4-...",
  "requested_at": "2026-08-25T09:14:00Z",
  "requested_by": "prenom.nom@entreprise.com",
  "source": {
    "instance": "https://sonar.entite.corp",
    "project_key": "com.entite:mon-projet",
    "sonar_version": "2026.2.1",
    "edition": "enterprise",
    "plugins_fingerprint": "sha256:..."
  },
  "target": {
    "project_key": "APP1234-mon-projet",
    "instance": "https://sonar.groupe.corp"
  },
  "archive": {
    "filename": "com.entite_mon-projet.zip",
    "sha256": "...",
    "size_bytes": 184320512
  }
}
```

Le manifeste résout d'un coup la traçabilité, le contrôle de version, l'intégrité, et fournit la donnée sur laquelle appuyer l'habilitation.

**Règle : le script central n'agit que sur les archives accompagnées d'un manifeste valide.** Tout le reste est ignoré.

### 4.2 Publier le manifeste en dernier

Corrige la course sur les fichiers partiels sans mécanisme supplémentaire. L'archive est téléversée d'abord ; le manifeste ensuite. Le script central ne déclenche rien tant que le manifeste n'est pas là — donc tant que l'archive n'est pas complète.

### 4.3 Une demande explicite, pas un scan de répertoire

Le script source ne scanne plus le répertoire d'export. Il traite une **file de demandes** : un petit fichier de demande déposé par l'équipe, ou un wrapper qu'elle appelle.

Le wrapper enchaîne : vérifier que le demandeur est administrateur du projet source → enregistrer la clé cible → déclencher l'export via l'API → attendre la fin de la tâche → publier archive puis manifeste.

Le planificateur ne sert plus qu'à traiter la file, pas à deviner l'intention.

### 4.4 Le contrôle d'habilitation, côté central

Avant tout import, le script central vérifie, avec son propre token d'administration :

1. Le projet cible existe bien sur l'instance centrale.
2. `manifest.requested_by` est administrateur de ce projet cible — **droit direct ou hérité d'un groupe**.
3. La clé cible correspond au format attendu des clés générées par le portail.

Si l'une des trois échoue : pas d'import, statut `rejected`, notification au demandeur avec le motif.

C'est ce contrôle qui ferme la faille des points 3.1 et 3.2. Il ne coûte que deux appels d'API.

### 4.5 Vérifier version, édition et intégrité avant l'import

Comparer `manifest.source.sonar_version` avec la version locale, et `archive.sha256` avec le checksum Artifactory. Un rejet propre avec un message clair vaut infiniment mieux qu'un import qui échoue à mi-parcours.

Idéalement, le script source refuse déjà de publier si les versions divergent — il faut alors qu'il connaisse la version cible, par exemple via un petit fichier `target-version.json` publié dans Artifactory par le côté central.

### 4.6 La suppression du projet vide, comme étape explicite

Étape à part entière du script central, journalisée, et **conditionnée** : on ne supprime que si le projet cible est vide, c'est-à-dire sans aucune analyse. Un projet cible qui contient déjà des analyses signale une erreur — on rejette au lieu de détruire.

C'est le garde-fou qui évite d'effacer un projet actif à cause d'une clé erronée.

### 4.7 Rejouer le renommage côté cible plutôt que côté source

À reconsidérer sérieusement. Si l'export part avec la clé d'origine et que le renommage a lieu **après l'import**, sur l'instance centrale :

- l'instance source n'est jamais touchée, leurs pipelines continuent de tourner jusqu'à la bascule ;
- en cas d'échec, il n'y a rien à réparer de leur côté ;
- la clé cible n'est plus décidée par le nom du fichier mais par le manifeste, après contrôle.

Le coût : une étape de renommage supplémentaire côté central. Le gain est sans commune mesure.

### 4.8 États portés par des propriétés Artifactory

Plutôt que de multiplier les dépôts, utiliser les **propriétés** Artifactory sur chaque archive :

| Propriété | Valeurs |
|---|---|
| `migration.status` | `pending`, `running`, `done`, `failed`, `rejected` |
| `migration.attempts` | entier |
| `migration.last_error` | texte |
| `migration.ce_task_id` | identifiant de tâche Compute Engine |
| `migration.completed_at` | horodatage |

Un seul dépôt, interrogeable en AQL, et l'historique reste attaché au fichier. `sonar-projects-migrated` garde son utilité comme dépôt d'archivage après succès, avec sa propre rétention.

### 4.9 Idempotence et verrou

Positionner `migration.status=running` **avant** de démarrer, et ignorer tout ce qui est déjà en `running`. Deux protections nécessaires :

- un `flock` autour du script, pour qu'une exécution longue ne chevauche pas la suivante ;
- un délai d'expiration sur `running`, pour libérer une migration dont le script est mort en cours de route.

### 4.10 Suivre la tâche Compute Engine jusqu'au bout

Interroger `api/ce/task?id=...` jusqu'à `SUCCESS` ou `FAILED`, avec un timeout dimensionné sur le plus gros projet observé. Ne passer en `done` et n'archiver qu'après un `SUCCESS` confirmé.

### 4.11 Limite de réessais et quarantaine

Trois tentatives, puis `migration.status=failed` et alerte. Sans cela, une archive corrompue génère 288 tentatives par jour et noie les journaux.

### 4.12 Configuration post-import, dans le même script

Après le renommage : appliquer le template de permissions, affecter le quality gate et le quality profile, configurer le binding DevOps, rattacher au portefeuille. C'est la différence entre un projet importé et un projet utilisable.

### 4.13 Notification du demandeur

Le manifeste contient `requested_by` — s'en servir. Un message de succès avec le lien vers le projet, ou d'échec avec le motif. Sans retour, les équipes vous relanceront une par une.

### 4.14 Hygiène d'exploitation

- Supprimer l'archive locale côté source après publication réussie.
- Rétention sur le dépôt d'archivage : 90 jours suffisent généralement.
- Contrôle d'accès strict sur les deux dépôts : les archives contiennent l'historique complet.
- Préférer un **timer systemd** à cron : journalisation intégrée, pas de recouvrement, statut interrogeable.

---

## 5. Séquence cible

1. L'équipe projet crée le projet cible via le portail DevOps → clé cible générée.
2. L'équipe dépose une demande de migration : clé source, clé cible.
3. **Script source** — vérifie que le demandeur est admin du projet source, déclenche l'export via l'API, attend la fin de la tâche, publie l'archive puis le manifeste dans Artifactory.
4. **Script central** — détecte le manifeste, vérifie l'intégrité, la version, et **l'habilitation du demandeur sur le projet cible**.
5. Vérifie que le projet cible est vide, puis le supprime.
6. Importe l'archive, suit la tâche Compute Engine jusqu'à `SUCCESS`.
7. Renomme la clé vers la clé cible.
8. Réapplique le template de permissions, le quality gate, le quality profile, le binding DevOps.
9. Passe le statut à `done`, archive l'archive, notifie le demandeur.

---

## 6. Verdict

L'ossature est bonne et je la garderais. Artifactory comme bus asynchrone est le bon choix, et deux scripts valent mieux qu'une pipeline complexe pour ce volume.

Trois ajouts la font passer du POC à la production :

1. **Le manifeste** — sans lui, rien n'est traçable ni vérifiable.
2. **Le contrôle d'habilitation sur le projet cible** — sans lui, la solution n'est pas défendable en revue de sécurité.
3. **La machine à états avec réessais et quarantaine** — sans elle, le premier échec devient une boucle infinie.

Le reste relève du confort d'exploitation et peut arriver après le POC.

**Une recommandation pour le POC** : commencer sans le contrôle d'habilitation, sur des projets pilotes choisis, mais **avec** le manifeste dès le premier jour. Le manifeste est ce qui rend le reste possible ; l'ajouter après coup oblige à tout reprendre.
