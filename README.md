# Migration SonarQube — outillage

Chaîne d'automatisation pour migrer des projets SonarQube d'une instance
d'entité vers l'instance centrale de l'entreprise, **avec leur historique**.

Deux services indépendants, reliés par Artifactory. Aucun des deux n'appelle
l'autre : le seul lien est un dépôt d'artefacts, ce qui permet aux deux
instances de ne jamais se connaître ni se joindre directement.

```
Instance entité (mono-nœud)          Artifactory          Instance centrale (cluster 2 nœuds)
─────────────────────────            ───────────          ───────────────────────────────────
export via l'IHM
   ↓
export/sonar-export-publisher.sh  →  to-migrate     →     import/sonar-import-worker.sh
   publie archive + manifeste                              orchestré par ansible/
                                     migrated       ←      archive après succès
```

---

## Arborescence

| Répertoire | Contenu |
|---|---|
| `export/` | Service publiant les exports vers Artifactory, à installer sur le serveur SonarQube de l'entité |
| `import/` | Worker important sur l'instance centrale |
| `ansible/` | Rôle et playbook Tower — **requis uniquement si l'instance centrale est en cluster** |
| `docs/` | Guides d'installation, montage Tower, notes de conception |
| `tools/` | Utilitaires ponctuels (comparaison de plugins entre instances) |

Les deux services ont une implémentation shell (bash + curl + jq), retenue
pour ne rien installer sur les serveurs SonarQube. Une version Python
équivalente du publisher est fournie dans `export/` pour les contextes où
Python est déjà maintenu.

---

## Par où commencer

| Vous voulez… | Lisez |
|---|---|
| Installer le service côté entité | `docs/INSTALL-export.md` |
| Installer le worker côté central | `docs/INSTALL-import.md` |
| Orchestrer sur un cluster sans stockage partagé | `docs/TOWER.md` |
| Comprendre les choix de conception | `docs/Analyse_Solution_Artifactory_Migration_Sonar.md` |
| Revoir la cohérence d'ensemble | `docs/Revue_Coherence_Solution.md` |

---

## La procédure, en une page

La clé du projet change à la migration : le portail DevOps en génère une
nouvelle, et l'archive Project Move porte l'ancienne. Toute la difficulté
tient dans cette bascule.

1. L'équipe projet crée son projet dans le **portail DevOps**. Le portail
   génère la clé cible et provisionne groupes et template de permissions.
2. L'équipe entité pose un lien nommé `MIGRATION` sur le projet source,
   pointant vers le projet cible. C'est ainsi que la clé cible voyage.
3. L'équipe entité lance l'export depuis l'IHM SonarQube.
4. `export/sonar-export-publisher.sh` détecte l'archive, résout l'identité du
   demandeur, écrit un manifeste et publie **l'archive puis le manifeste** —
   dans cet ordre, pour que le consommateur ne voie jamais un dépôt incomplet.
5. `import/sonar-import-worker.sh` réagit au manifeste, déroule ses contrôles,
   puis exécute la bascule.

### L'ordre de la bascule n'est pas négociable

```
import (clé source)  →  confirmation SUCCESS  →  suppression du projet vide
                     →  renommage vers la clé du portail  →  reconfiguration
```

L'import porte la clé source, il ne heurte donc rien. Le projet vide créé par
le portail n'est supprimé **qu'après** confirmation de l'import : si l'import
échoue, rien n'a été détruit. Le renommage vient ensuite, parce que c'est le
seul moment où la clé du portail est libre.

Inverser suppression et import ferait perdre la clé du portail en cas d'échec.

---

## Contrôles avant toute écriture

Le worker vérifie huit points avant de toucher à l'instance centrale :

1. cohérence entre la clé cible déclarée et le répertoire Artifactory
2. version SonarQube identique entre les deux instances
3. édition compatible
4. tous les plugins de la source présents en central, mêmes versions
5. le projet cible existe
6. le projet cible est **vide** — le garde-fou le plus important
7. la clé source est libre sur la cible
8. l'empreinte SHA-256 de l'archive correspond au manifeste

Optionnellement, que le demandeur soit administrateur du projet cible
(`ENFORCE_REQUESTER_ADMIN`, à activer après le POC).

---

## Bancs d'essai

Les deux services se testent sans instance réelle : un faux SonarQube et un
faux Artifactory tournent sur `127.0.0.1`.

```bash
cd export/tests && ./run-tests.sh     # 24 assertions
cd import/tests && ./run-tests.sh     # 51 assertions
```

Un port peut être imposé : `PORT=18095 ./run-tests.sh`.

À rejouer après **toute** adaptation — format de clé, nom du lien, nom du
template, type de tâche CE. C'est ce qui a permis de trouver cinq défauts
réels avant toute mise en service.

> Ces bancs prouvent que les scripts sont cohérents avec eux-mêmes. Ils ne
> prouvent pas la conformité aux API de **votre** instance : les maquettes
> encodent des hypothèses. Voir la section « Vérification avant mise en
> service » de chaque guide d'installation.

---

## État

| Élément | Statut |
|---|---|
| `export/sonar-export-publisher.sh` | testé sur maquette, 24/24 |
| `import/sonar-import-worker.sh` | testé sur maquette, 51/51 |
| `ansible/` | syntaxe validée, **jamais exécuté** |
| Exécution contre une instance réelle | **aucune à ce jour** |

Reste à faire avant le premier passage réel :

- [ ] Vérifier la signature de `api/projects/update_key` sur l'instance centrale
- [ ] Ajouter `scm_repository` au manifeste produit par le publisher
- [ ] Restreindre la lecture des deux dépôts Artifactory au compte technique
- [ ] Essai à blanc (`--dry-run`) sur l'instance réelle
- [ ] Premier passage sur un projet de test sans valeur
