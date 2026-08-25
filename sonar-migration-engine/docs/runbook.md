# Operations runbook

For the central team. Assumes prior reading of the root README
(architecture, sequence, security) and of [`ansible/README.md`](../ansible/README.md)
(per-role idempotence guarantees).

## 1 — Launching a migration

Nothing proactive to do: a migration starts when a project team merges its
request into `sonar-migration-requests`. The `habiliter` job (see
[`ci/pipeline.yml`](../ci/pipeline.yml)) runs automatically and publishes
its verdict as an MR comment within a few minutes.

**What the central team does**: once the `AUTHZ_PASSED` verdict is
published, launch the **`executer`** job (`when: manual`) from *CI/CD →
Pipelines*, within the window agreed with the project team. This is the
only launch action.

## 2 — Following the execution

Three places, from most readable to most detailed:

1. **The MR comment** — authorization verdict, then the final report
   (success or interruption), rendered by [`rapport.py`](../src/migration/rapport.py).
2. **The GitLab CI pipeline status** — which stage failed, job logs
   (never a token in the clear: `no_log: true` on every Ansible task that
   handles one — see each role).
3. **The journal** (`sonar-migration-runs/journal/<run_id>.jsonl`) — the
   source of truth. `<run_id>` = `<instance_source>/<slug(target_key)>`
   (see `modele.Demande.identifiant`).

Quick check of a run's state:

```bash
git -C sonar-migration-runs pull
python -m migration.cli metriques --depot-runs sonar-migration-runs   # overview
tail -n 1 sonar-migration-runs/journal/<instance_source>/<slug-target-key>.jsonl | python3 -m json.tool
```

The last line says it all: `etat` (and `etat_atteint` if `FAILED`) is the
resumption point (see `journal.dernier_etat_confirme`).

## 3 — Interpreting a failure

| Symptom | Where to look | Meaning |
|---|---|---|
| `valider` job failed | Pipeline status only (no comment — see `ci/pipeline.yml`, this job has no token) | Malformed request. The author must fix it and resubmit. |
| `habiliter` job failed, comment `❌ Authorization check refused` | MR comment | Normal refusal — the requester knows what to fix. No action needed from the central team, **unless** the comment contains a "Flagged for the central team" (🔔) section: directory duplicate, overly broad group, or an unprovisioned target project (see prompt §5). |
| `preflight` job failed | Job's Ansible logs | Technical gap (versions, plugins, disk space, target project changed since authorization). The run stays at `AUTHZ_PASSED`: replay `preflight` after fixing it, safely (a read-only role). |
| `executer` job failed | Ansible logs + last journal entry | See the recovery table below, by state reached. |
| Compute Engine task "failed" vs "timeout" | Message from the `sonar_attendre_tache_ce` role | Explicitly distinguished in the message itself — see [`ansible/docs/suivi_taches_ce.md`](../ansible/docs/suivi_taches_ce.md). A timeout is NOT a failure of the task: it may still succeed later. |

## 4 — Recovery procedure, by state

The resumption point is the **last confirmed state**
(`journal.dernier_etat_confirme` — the `etat` of the last entry, or its
`etat_atteint` if `FAILED`). Resuming = relaunching the `executer` job:
every Ansible role rechecks its own prerequisite before acting (see the
idempotence guarantees, [`ansible/README.md`](../ansible/README.md)), so
resumption **never** needs a `--from-state-X` parameter: it replays the
whole sequence, and every already-satisfied role is a quick no-op.

| Last confirmed state | What is already done | Recovery action |
|---|---|---|
| `RECEIVED` | Request logged | Replay `habiliter` (or wait: this state alone signals an interruption before the check even ran). |
| `AUTHZ_REJECTED` | — (terminal) | **No automatic recovery.** The project team resubmits a request after fixing the issue. |
| `AUTHZ_PASSED` | Authorization validated | Launch/relaunch `preflight`. |
| `PREFLIGHT_OK` | Preflight checks validated | Launch/relaunch `executer`. |
| `EXPORTED` | Archive exported on the source side | Relaunch `executer`: `sonar_transfert` finds the archive deterministically (`<source key>.zip`), no need to re-export — but re-exporting is also harmless (overwrite). |
| `TRANSFERRED` | Archive verified on the central instance | Relaunch `executer`: the capture (step 7) is idempotent (rewrites the capture file). |
| `TARGET_CAPTURED` | Portal configuration saved — **last reversible state** | Relaunch `executer`. If manual intervention is needed before continuing, it is still possible without loss: nothing irreversible has happened yet. |
| `TARGET_DELETED` | **Point of no return crossed** — portal project deleted | Relaunch `executer`: `sonar_creer_placeholder` rechecks the state before acting. **Never** try to "undo" this state: there is nothing to undo, only to continue. |
| `PLACEHOLDER_CREATED` | Blank project ready for import | Relaunch `executer`. |
| `IMPORTED` | **History imported, under the source key** | See §5 below — the trickiest case, handled separately. |
| `KEY_RENAMED` | Key switched to the target key | Relaunch `executer`: idempotent configuration reapplication. |
| `CONFIG_APPLIED` | Everything is done except the `DONE` mark | Relaunch `executer` (replays quickly, everything is already in place) or, if the team is certain everything is correct, log `DONE` directly: `python -m migration.cli enregistrer --depot-runs sonar-migration-runs --run-id <id> --etat DONE --acteur <your-name>`. |
| `DONE` | — (terminal) | Nothing. A replay is refused by construction (`journal.verifier_pas_deja_reussie`). |

## 5 — The `IMPORTED` case: rename failure

This is the case the prompt explicitly identifies as the trickiest.
The history is already in place, under the source key; the DevOps space
no longer has a project visible under its target key.

**The correct action**: relaunch `executer`. `sonar_renommer_cle` rechecks
the state before acting and replays `api/projects/update_key`
(idempotent). If it fails again, the most likely cause is that the target
key is no longer free (a collision detected late, or a project recreated
in the meantime) — resolve the collision by hand on the central instance,
then relaunch.

**What must NEVER be done**: deleting the imported project to "start
over". This would destroy the history already in place and require
starting everything over from the export. This is not just a rule in this
runbook: `machine_etats.interdire_suppression_projet_importe` raises an
exception if a role attempts a deletion from this state onward — a
code-level guard, not just a guideline.

## 6 — Replaying an Ansible role by hand

Every role is self-contained and replayable outside the pipeline
(`ansible/README.md`). The GitLab runners have no network path to the
SonarQube hosts (SSH blocked) — only the AWX/AAP controller does — so a
manual replay goes through AWX, not a local `ansible-playbook` call:

- **From the AWX UI or API**: relaunch the job template named after the
  step (`export`, `transfert`, `capture_config`, `supprimer_cible`,
  `creer_placeholder`, `import`, `renommer_cle`, `appliquer_config`, or
  `preflight`), passing the same extra-vars the pipeline would have used:
  `sonar_run_id=<id> sonar_cle_source=<...> sonar_cle_cible=<...>
  sonar_source_host=<...> sonar_projet_cible_id=<...>`. See
  `docs/installation.md`, step 8, for how job templates are set up.
- **Equivalently via the CLI**, from anywhere with `AWX_BASE_URL` /
  `AWX_API_TOKEN` in the environment:
  ```bash
  python -m migration.cli lancer-gabarit \
    --gabarit <step> \
    --extra-vars "sonar_run_id=<id> sonar_cle_source=<...> sonar_cle_cible=<...> sonar_source_host=<...> sonar_projet_cible_id=<...>"
  ```
- **Only if you have direct SSH access to the Sonar hosts** (e.g. running
  this from the AWX controller itself, or a bastion with the same
  reach) can you still fall back to a local `ansible-playbook site.yml
  --tags <step> --extra-vars "..."` — tokens read from the environment
  (`SONAR_SRC_<ID>_TOKEN`, `SONAR_CENTRALE_TOKEN` — see root README,
  naming convention), exported in the shell, never passed as
  `--extra-vars` (visible in the process list).

Do not forget to log the transition in the journal after a successful
manual replay (`migration.cli enregistrer`): Ansible acts, it never logs
by itself (see root README, § GitLab CI / Ansible split).

## 7 — Metrics

```bash
python -m migration.cli metriques --depot-runs sonar-migration-runs
```

See [`metriques.py`](../src/migration/metriques.py): number of runs by
outcome (successful / refused at authorization / interrupted / in
progress), failure rate, average/median duration of successful
migrations, authorization refusal causes broken down by code, states
reached before interruption broken down. Computed on demand against a
local clone; periodic distribution (dashboard, notification channel) is a
tooling choice specific to the central team, out of scope for this
repository.

## 8 — Escalation

To be filled in locally (Slack channel, on-call rotation, contacts) — not
defined by this prompt.
