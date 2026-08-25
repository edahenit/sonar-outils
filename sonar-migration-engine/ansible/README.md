# ansible/

Execution plan: acts on the SonarQube servers. Decides nothing —
GitLab CI ([`../ci/pipeline.yml`](../ci/pipeline.yml)) records every state
transition before launching the corresponding role, and logs the result
afterward. See root README, § GitLab CI / Ansible split.

The GitLab runners have no network path to the SonarQube hosts (SSH
blocked): this content runs on an AWX/AAP controller instead, launched
and polled by GitLab CI through the AWX API
(`../src/migration/awx_client.py`, `migration.cli lancer-gabarit`) rather
than a local `ansible-playbook` call — see
[`../../docs/installation.md`](../../docs/installation.md), step 8. This
directory's content (`site.yml`, roles, dynamic inventory) does not
change because of this: AWX runs the exact same playbook a local
`ansible-playbook` invocation would.

## One role per step

| Role | Prompt step | Idempotence |
|---|---|---|
| [`sonar_preflight`](roles/sonar_preflight/) | 4 — preflight checks | Total: read-only end to end. Replayable at any time, including after success. |
| [`sonar_export`](roles/sonar_export/) | 5 — export | By overwrite: SonarQube rewrites the export zip on every successful trigger; retriggering after a failure/timeout never leaves two inconsistent archives. |
| [`sonar_transfert`](roles/sonar_transfert/) | 6 — transfer | Total: `fetch`/`copy` are inherently idempotent operations (recopy if different, change nothing otherwise); the SHA-256 fingerprint is rechecked on every run. |
| [`sonar_capture_config`](roles/sonar_capture_config/) | 7 — capture | Total: read-only, the capture file is rewritten (not merged) on every run — a more recent capture always replaces the previous one. |
| [`sonar_supprimer_cible`](roles/sonar_supprimer_cible/) | 8 — deletion (**point of no return**) | **Not idempotent by nature** (replaying after success fails: the project no longer exists — the desired behavior). Safe on retry after a failure thanks to the internal id recheck before any deletion. |
| [`sonar_creer_placeholder`](roles/sonar_creer_placeholder/) | 9 — placeholder (configurable) | Total: no-op if the project already exists under the source key; a complete no-op if `sonar_create_placeholder_project: false`. |
| [`sonar_import`](roles/sonar_import/) | 10 — import | Partial, deliberately: SonarQube normally refuses to import onto an already-analyzed project — a replay after success fails on the API side rather than duplicating the history. A replay after a failure/timeout stays safe. |
| [`sonar_renommer_cle`](roles/sonar_renommer_cle/) | 11 — rename | Total and deliberate: this is the trickiest case in the prompt (§6). The role checks the current state before calling the API, so it only renames what is still left to rename. |
| [`sonar_appliquer_config`](roles/sonar_appliquer_config/) | 12 — reapplication | Total: every call (`add_group`, `add_user`, `select`, `add_project`…) is already idempotent on the SonarQube API side. |

Cross-cutting role, included by `sonar_export` and `sonar_import`:
[`sonar_attendre_tache_ce`](roles/sonar_attendre_tache_ce/) — see
[`docs/suivi_taches_ce.md`](docs/suivi_taches_ce.md) (Compute Engine task
tracking, timeout, in-progress / failure / timeout distinction).

## What every role guarantees, systematically

- **An `assert` on entry** checks the expected variables before any
  action — never an API call with a missing or malformed variable.
- **No `shell` or `command` task**: API via `uri` (`body_format:
  form-urlencoded`, never string interpolation into a command), transfer
  via `fetch`/`copy`, disk space via `setup` (`ansible_mounts`),
  fingerprints via `stat` (`checksum_algorithm: sha256`).
- **`no_log: true`** on every task carrying an `Authorization` header.
- **Replayable outside the pipeline**, for incident recovery (see
  runbook, § 6): from an AWX/AAP controller with SSH access to the Sonar
  hosts — the GitLab runners have none — either through the AWX UI/API
  (relaunch the job template for that step with the same extra-vars), or
  locally with direct SSH access to the same hosts:
  `ansible-playbook site.yml --tags <step> --extra-vars
  "sonar_run_id=... sonar_cle_source=... sonar_cle_cible=... ..."`

## Multi-entity inventory

[`inventaire/depuis_instances.py`](inventaire/depuis_instances.py) is a
**dynamic** Ansible inventory, derived from
[`../inventaire/instances.yml`](../inventaire/instances.yml) — the same
source of truth as the Python code (`migration.inventaire`), never
duplicated. Two groups (`sonar_source`, `sonar_centrale`), one host per
instance, no limit on the number of entities: adding an instance to
`instances.yml` makes it immediately available here.

Admin tokens never transit through this inventory: each host only carries
the *name* of its protected variable (`sonar_variable_token`), resolved at
execution time by
`lookup('env', hostvars[inventory_hostname].sonar_variable_token)`
([`group_vars/all.yml`](group_vars/all.yml)) — never passed as an
extra-var on the command line (visible in the runner's process list).

## What is deliberately missing

**Portfolios** are neither captured nor reapplied: no "portfolios
containing this project" read endpoint has been identified with enough
confidence to write without guessing (see
[`../docs/a-verifier.md`](../docs/a-verifier.md)). Handled by hand by the
central team until this point is resolved.

## How this was tested

This environment has neither Ansible installed nor a real SonarQube server
reachable from this development setup: these roles have **not** been
executed. Verified: every YAML file is syntactically valid
(`yaml.safe_load`), the `tasks`/`defaults` structure is consistent per
role, and the reasoning behind every task is documented. Real validation
(`ansible-playbook --check`, then under real conditions on a test
instance) is part of batch 6's test plan.
