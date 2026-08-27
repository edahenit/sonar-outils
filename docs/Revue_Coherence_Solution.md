# Revue de cohérence — solution de migration SonarQube

Relecture de bout en bout : parcours utilisateur, procédure, script de publication,
supports de réunion. Ce document liste ce qui tient, ce qui cloche, et ce qui reste
à faire.

---

## 1. L'incohérence bloquante — l'ordre suppression / import

**Ce qui était écrit** dans la procédure et dans le deck :

> Supprimer le projet vide → importer → renommer

**Pourquoi c'est faux.** Depuis qu'on a retenu le renommage **côté cible**,
l'archive part avec la clé **source**. L'import crée donc un projet portant la clé
source — il n'y a **aucune collision** avec le projet créé par le portail. La
collision n'apparaît qu'au **renommage**, puisqu'on ne peut pas renommer vers une
clé déjà prise.

Supprimer avant d'importer revient donc à **détruire avant de savoir si l'import
va réussir**. Si l'import échoue, l'équipe se retrouve sans projet cible et sans
historique.

**L'ordre correct :**

1. Importer l'archive → un projet apparaît avec la clé source
2. Attendre la confirmation `SUCCESS` de la tâche Compute Engine
3. **Alors seulement** supprimer le projet vide créé par le portail
4. Renommer vers la clé cible
5. Réappliquer template de permissions, quality gate, quality profile, binding

Rien n'est détruit tant que l'import n'a pas réussi. Le seul effet transitoire est
la coexistence de deux projets pendant quelques secondes — sans conséquence.

**Corrigé** dans le deck de réunion (slide « The migration procedure », étapes 4
et 5, et le script de prise de parole).

---

## 2. Deux manques entre la conception et l'implémentation

### 2.1 Le dépôt Git absent du manifeste

La conception prévoyait `scm_repository` dans le manifeste, alimenté par
`api/alm_settings/get_binding`, pour offrir au script central un **second contrôle
indépendant des permissions** : les deux projets doivent pointer vers le même
dépôt GitLab.

Le script de publication ne le collecte pas. Deux contrôles de nature différente
valent nettement mieux que deux fois le même — c'est à ajouter avant la fin du POC.

### 2.2 Le binding DevOps perdu, jamais recréé

Le projet importé ne vient plus du portail : son binding vers le dépôt GitLab
n'existe pas. La liste des étapes post-import mentionne « binding DevOps » sans
préciser qu'il faut le **recréer** (`api/alm_settings/set_gitlab_binding`), et non
simplement le vérifier.

Sans ce binding, la décoration des merge requests ne fonctionne plus. C'est le
genre de régression que les équipes remontent trois semaines après la migration.

---

## 3. Un risque non couvert — la lecture des dépôts Artifactory

Le répertoire de destination est dérivé de l'`espace_id` extrait de la clé cible,
elle-même **déclarée par l'équipe source** via le lien.

Rien n'empêche une équipe de déclarer la clé cible d'une **autre** application.
Le contrôle d'habilitation côté central rattrapera la tentative — l'import sera
refusé. Mais entre-temps, **l'archive aura transité dans l'espace Artifactory
d'autrui**, et une archive Project Move contient l'historique complet du projet.

Si les équipes ont accès en lecture à leur espace Artifactory, c'est une fuite.

**Parade, simple et suffisante :** restreindre la **lecture** des dépôts
`sonar-projects-to-migrate` et `sonar-projects-migrated` au seul compte technique
du script central. Les équipes n'ont aucune raison d'y accéder ; le répertoire par
espace sert à l'organisation et à l'audit, pas au partage.

À faire avant le premier export réel du POC.

---

## 4. Points mineurs, à traiter au fil de l'eau

**Le lien MIGRATION voyage dans l'archive.** Les paramètres de projet sont inclus
dans l'export. Après import, le projet cible portera un lien pointant vers
lui-même. Sans gravité, mais à nettoyer côté central — sinon quelqu'un se
demandera un jour pourquoi.

**Un projet hors convention si le renommage échoue.** Entre l'import et le
renommage, l'instance centrale héberge un projet portant la clé source, hors du
nommage du portail. Prévoir la détection et le nettoyage dans le runbook.

**La fenêtre de rattrapage de 24 h.** Si le service de publication reste arrêté
plus d'une journée, les exports concernés ne seront jamais publiés — le script ne
les verra plus. Prévoir une procédure de rattrapage manuel, ou surveiller l'arrêt
du timer.

**L'archive locale est supprimée après publication.** Si la rétention Artifactory
purge le dépôt avant l'import, l'archive est définitivement perdue et il faut
refaire l'export. Acceptable, à condition que la rétention soit largement
supérieure au délai d'import.

**On crée un projet pour le détruire.** Le portail doit créer le projet cible
uniquement pour générer sa clé, et ce projet sera supprimé. C'est inélégant mais
nécessaire tant que le portail ne sait pas réserver une clé. À reposer à l'équipe
portail : une simple API « génère-moi la clé sans créer » supprimerait toute cette
gymnastique.

---

## 5. Ce qui est cohérent

**Le parcours utilisateur.** Trois gestes seulement — créer via le portail, poser
le lien, lancer l'export. Aucun script à exécuter, aucun ticket à ouvrir. Les deux
gestes qui portent une décision exigent le droit *Administer* sur le projet :
SonarQube fait donc deux contrôles d'habilitation gratuitement.

**Le pilotage par les tâches Compute Engine.** L'identité du demandeur vient d'une
source authentifiée, jamais d'un nom de fichier. L'identifiant de tâche sert
d'identifiant d'idempotence. Cohérent de bout en bout, et vérifié par les tests.

**Le manifeste publié en dernier.** Un seul choix d'ordre qui supprime toute la
classe des archives incomplètes, sans verrou distribué ni convention de nommage.

**La séparation des responsabilités.** Le script source déclare, il ne décide de
rien : pas de contrôle de droits sur la cible, pas de choix de destination, aucune
suppression. Tous les contrôles sensibles sont côté central, au moment de
l'import, quand l'information est fraîche. Le script respecte cette règle sans
exception.

**Le discours de réunion.** Aligné sur la technique, y compris sur le point
délicat : « vos groupes et votre template de permissions ne sont pas perdus, ils
appartiennent à l'instance ». C'est exact, et c'est rassurant à dire.

---

## 6. Ce qui reste à produire

| # | Livrable | Priorité |
|---|---|---|
| 1 | Le script d'import, côté instance centrale | Bloquant pour le POC |
| 2 | Restriction de lecture sur les dépôts Artifactory | Avant le premier export réel |
| 3 | Ajout de `scm_repository` au manifeste | Avant la fin du POC |
| 4 | Recréation du binding DevOps après import | Avec le script d'import |
| 5 | Nettoyage du lien MIGRATION importé | Avec le script d'import |
| 6 | Runbook : échec de renommage, projet hors convention | Avant les vagues |
| 7 | Notification du demandeur | Après le POC |
| 8 | Contrôle d'habilitation sur le projet cible | Après le POC, avant l'ouverture |

Les points 1 et 2 conditionnent le POC. Les autres peuvent suivre.

---

## 7. Les trois vérifications terrain, avant d'écrire la moindre ligne de plus

Elles n'ont pas encore été faites sur votre instance, et chacune peut invalider
une partie du code :

1. Le **nom réel du type de tâche** d'export dans `api/ce/activity`. Si ce n'est
   pas `PROJECT_EXPORT`, le script ne verra jamais rien et ne dira pas pourquoi.
2. La **forme de `externalIdentity`** dans votre annuaire — matricule,
   `prenom.nom`, ou email complet. Elle conditionne toute la logique de
   correspondance entre les deux instances.
3. Le **comportement du portail après un import** : tente-t-il de recréer le
   projet qu'il ne reconnaît plus ? C'est le seul inconnu qui pourrait remettre en
   cause la procédure elle-même.

Vingt minutes de `curl` et un projet de test. C'est le meilleur investissement du
projet à ce stade.
