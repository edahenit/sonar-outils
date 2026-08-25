# sonar-migration-runs

**Append-only** execution journal of the self-service SonarQube
migration. Project team access: **read-only**.

## What this repository contains

| Folder | Content |
|---|---|
| `journal/<instance_source>/<slug(cle_cible)>.jsonl` | One JSON line per state transition, in the order it was attempted |
| `rapports/` | Reserved for final report exports (the report itself is published as an MR comment — see `rapport.py`), not automatically populated at this stage |
| `preuves/` | Reserved for exporting authorization proofs outside the journal, not automatically populated at this stage |

## Why append-only

Migrating analysis history is a trust operation between entities. The
journal is the proof, not a byproduct: every state transition is written
here **before** it is attempted, never after
(`migration.journal.enregistrer_transition`). An entry is never modified
nor deleted; a resumption adds new entries, never rewrites any
(`migration.journal.ecrire_entree` only opens the file in append mode).

Technically: only the engine repository's privileged job writes here
(`git clone` authenticated by `CI_JOB_TOKEN`, pushed by
`migration.journal.publier_journal`); this repository grants no write
access to project teams.

## Journal entry format

One JSON line, `migration.journal.EntreeJournal.to_dict()`:

```json
{
  "horodatage": "2026-09-15T09:12:03Z",
  "run_id": "entite-alpha/grp-alpha-facturation-api",
  "etat": "AUTHZ_PASSED",
  "acteur": "pipeline",
  "etat_atteint": null,
  "detail": {
    "ok": true,
    "preuve_source": { "...": "see habilitation.PreuveAdmin.to_dict()" },
    "preuve_cible": { "...": "..." }
  }
}
```

- `run_id` = `<instance_source>/<slug(cle_cible)>` (`migration.modele.Demande.identifiant`): also the file name, without the extension.
- `etat`: one of the states in `migration.machine_etats.ETATS_SEQUENCE`, or `AUTHZ_REJECTED` / `FAILED`.
- `etat_atteint`: filled in **only** when `etat == "FAILED"` — the last confirmed state before the failure. `null` in every other case.
- `detail`: free-form structure depending on the state (authorization proofs at `AUTHZ_PASSED`/`AUTHZ_REJECTED`, preflight facts at `PREFLIGHT_OK`, etc.) — never a secret.

## Uniqueness lock and replay

No hand-rolled lock: mutual exclusion between two concurrent executions of
the same `run_id` is delegated to GitLab CI's `resource_group` (see the
engine repository's `ci/pipeline.yml`) — a concurrent pipeline on the
same `run_id` is queued, never run in parallel.

This repository does, however, refuse **replay**:
`migration.journal.verifier_pas_deja_reussie` interrupts any new attempt
for a `run_id` whose last entry is `DONE`. An interrupted run (last entry
`FAILED`, or an intermediate entry with no explicit `FAILED` after a
pipeline incident) is NOT locked: it gets **resumed**, it does not
restart from scratch.

## Resumption points

`migration.journal.dernier_etat_confirme(entrees)` gives the state to
restart from: the last entry's state, or its `etat_atteint` if that last
entry is `FAILED`. Resumption replays from the state **following** this
confirmed state (`migration.machine_etats.etat_suivant`) — never from
`RECEIVED`.

**Guard**: from `IMPORTED` onward, no deletion of the target project is
allowed, even during incident recovery —
`migration.machine_etats.interdire_suppression_projet_importe` raises an
exception if attempted. This is not just a runbook rule: it is a
mandatory call before any deletion task, wired in batch 4.

Detailed per-state recovery procedure: see the runbook (batch 5).

## What must be logged — contract by state

Every entry must make it possible, months later and with no other
context, to reconstruct what was checked and what was done. `detail`
therefore carries specific content depending on `etat`, never a plain
"ok":

| `etat` | `detail` must contain |
|---|---|
| `RECEIVED` | `fichier` (request path), `auteur_gitlab` (login re-read server-side — never a job variable) |
| `AUTHZ_PASSED` / `AUTHZ_REJECTED` | The full `DecisionHabilitation.to_dict()`: proof for both sides (path, login, group, groups examined), every refusal with its code, every flagged anomaly (`alerte: true`) — never a token |
| `PREFLIGHT_OK` | Facts observed by Ansible: compared versions, plugin comparison result, available disk space, confirmation that the target project is still blank and the source key is still free |
| `EXPORTED` / `TRANSFERRED` | SHA-256 fingerprint of the archive (never its content) |
| `TARGET_CAPTURED` | Reference to the captured configuration (the detailed content lives in the pipeline's local exchange file, not in the journal — see `ansible/roles/sonar_capture_config`) |
| `TARGET_DELETED` | Explicit confirmation that the target key no longer matches any project (result of the post-deletion recheck) |
| `PLACEHOLDER_CREATED` / `IMPORTED` / `KEY_RENAMED` / `CONFIG_APPLIED` | Result of the step's own check (see each Ansible role, final "assert" section) |
| `DONE` | Nothing more: the presence of this terminal entry is enough |
| `FAILED` | `etat_atteint` mandatory (see `journal.construire_entree`); `detail` carries, if available, the error message that interrupted execution |

**What must never appear here**: a token, a password, or any fragment
that would allow reconstructing either one (see the systematic
`no_log: true` on the Ansible side, and the total absence of secrets in
the `PreuveAdmin`/`DecisionHabilitation`/`Refus` types on the Python
side).

## What this repository does not contain

No secret, no token, no password. An entry's `detail` contains logins,
project identifiers, group names — never a token.
