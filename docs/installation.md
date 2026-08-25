# Installation and configuration guide

Audience: the central/platform team doing the **initial deployment** of
this solution, once for the whole group. This is not a per-migration
guide — for running and recovering an actual migration, see
[`sonar-migration-engine/docs/runbook.md`](../sonar-migration-engine/docs/runbook.md);
for proving the deployment actually works end to end, see
[`sonar-migration-engine/docs/plan-validation-pilotes.md`](../sonar-migration-engine/docs/plan-validation-pilotes.md),
which should be run immediately after this guide, before any real
migration.

Every step below cites the exact file or GitLab setting it depends on, so
it can be re-verified independently of this document. Where a required
piece of environment-specific information is genuinely not decided by the
code (only by your GitLab/SonarQube estate), this guide says so explicitly
rather than assuming a default.

## Prerequisites

- One GitLab group to host all three repositories under a single
  namespace (see [Step 1](#step-1--create-the-shared-gitlab-group-and-the-three-repositories) for why this matters).
- Dedicated, tagged self-hosted GitLab Runners (tag `migration-sonar`,
  design document H6): sharing runners with unrelated projects would let
  those projects' jobs potentially reach this solution's protected
  variables.
- An **Ansible Automation Platform / AWX controller** with SSH access to
  every SonarQube host involved (central instance + every source instance
  to onboard) — the GitLab runners have **no** network path to those
  hosts, only AWX does. GitLab CI reaches AWX over its REST API instead of
  running `ansible-playbook` itself; see
  [Step 8](#step-8--awx-job-templates-for-each-migration-step).
- Network access from the GitLab runners to the GitLab instance and to
  the AWX controller's API (HTTPS) — not to the SonarQube hosts directly.
- Network access from GitLab CI to the SonarQube REST API over HTTPS for
  the authorization check (`habiliter` stage, `habilitation.py` /
  `sonar_client.py`) — a different access path from AWX's SSH access, and
  required even though the runners can't SSH to the same hosts.
- SonarQube **Enterprise Edition or higher** on the central instance and
  on every source instance (Project Move requires it), with matching
  versions and plugins between the central instance and each source —
  checked automatically at every run by `sonar_preflight`, but worth
  confirming up front.
- A GitLab personal/group access token with read rights on merge requests
  and users, for server-side identity resolution (`GITLAB_API_TOKEN`).
- A SonarQube user token with administrator rights on **every** instance
  involved (central + each active source) — one token per instance, never
  shared across instances. Used directly by GitLab CI for the
  authorization check, **and** held separately by AWX as its own
  credentials for the execution steps (see Step 8) — the two are not the
  same secret store.
- An AWX API token (`AWX_API_TOKEN`) that GitLab CI uses to launch and
  poll job templates — read/execute rights on the job templates from
  Step 8 are enough, no need for AWX admin rights.

## Step 1 — Create the shared GitLab group and the three repositories

Create one GitLab group (e.g. `groupe/sonar-migration`) and, inside it,
three empty projects:

```
<group>/sonar-migration-engine
<group>/sonar-migration-requests
<group>/sonar-migration-runs
```

This has to be a **single shared group**, not three unrelated projects:
protected CI/CD variables are set once at the group level (Step 5) and
GitLab cascades group-level variables to every project's pipelines within
that group — including `sonar-migration-requests`, which is where the
privileged jobs actually execute once the CI configuration path (Step 3)
redirects them to `sonar-migration-engine`'s pipeline file. Without a
shared group, those jobs would have no way to read the tokens.

Once the three projects exist and permissions are set (Step 2), populate
them from this development monorepo:

```bash
scripts/split-repos.sh <group_url>
# e.g. scripts/split-repos.sh git@gitlab.groupe.example:groupe/sonar-migration
```

The script requires a clean working tree and uses `git subtree split` to
give each target repository its own history, limited to the commits that
touched that folder — see
[`scripts/split-repos.sh`](../scripts/split-repos.sh) for the exact
commands it runs.

## Step 2 — Set repository permissions

| Repository | Project team permissions | Reason |
|---|---|---|
| `sonar-migration-engine` | **None** | Carries every protected token; a project team member with any access here could read them. |
| `sonar-migration-requests` | Developer | Project teams submit their own migration requests as merge requests here. |
| `sonar-migration-runs` | Read-only (Reporter) | Journal, reports and authorization proofs must be auditable but never editable by hand. |

Assign these under each project's **Settings → Members**. Only the
central team should hold Maintainer or above on any of the three — in
particular, Maintainer on `sonar-migration-requests` is what would let
someone change the CI configuration path back (Step 3) or the Token
Access allowlist (Step 4), which is exactly what those settings are
meant to prevent from being self-service.

## Step 3 — The CI configuration path (the setting that protects everything else)

Without this, none of the other guarantees hold, regardless of code
quality: it is what stops a contributor from bypassing the authorization
check by adding a job to their own merge request.

On **`sonar-migration-requests`**:

1. **Settings → CI/CD**, expand **General pipelines**.
2. **CI/CD configuration file** field:
   ```
   ci/pipeline.yml@<group>/sonar-migration-engine
   ```
3. **Save.**
4. Confirm that
   [`sonar-migration-requests/.gitlab-ci.yml`](../sonar-migration-requests/.gitlab-ci.yml)
   stays present but inert — its own content is never read once this
   setting is active, by design (see the comment at the top of that
   file). Do not delete it; do not add jobs to it.
5. **Settings → Repository → Protected branches**: `main` push-protected
   for everyone except the central team, Developer restricted to opening
   merge requests.

`<group>/sonar-migration-engine` must match the real path chosen in
Step 1. The clone URLs inside
[`ci/pipeline.yml`](../sonar-migration-engine/ci/pipeline.yml) currently
assume the literal namespace `groupe/` — update every
`groupe/sonar-migration-engine.git` / `groupe/sonar-migration-runs.git`
occurrence in that file to the real group path if it differs (flagged
in-file as "TO VERIFY").

## Step 4 — Token Access allowlists

Each privileged job clones `sonar-migration-engine` (for the code and the
inventory) and `sonar-migration-runs` (for the journal) using
`CI_JOB_TOKEN` — automatic, job-scoped, read-only — instead of an admin
token. GitLab refuses this by default across projects, so it must be
allowed explicitly:

1. On **`sonar-migration-engine`**: **Settings → CI/CD → Token Access**,
   add `sonar-migration-requests` to the list of projects allowed to use
   their `CI_JOB_TOKEN` against this repository.
2. On **`sonar-migration-runs`**: same setting, same reason — the
   `habiliter`, `preflight`, `executer`, and `rapport` stages all write
   or read the journal there.

## Step 5 — Protected and masked CI/CD variables

Set these at the **group** level (`<group>` → **Settings → CI/CD →
Variables**), each **Protected** (exposed only on protected branches —
`main` was protected in Step 3) and **Masked** where the value is a
secret:

| Variable | Value | Masked |
|---|---|---|
| `GITLAB_API_TOKEN` | GitLab token, read access to merge requests + users | Yes |
| `SONAR_CENTRALE_TOKEN` | Admin token for the central instance — used directly by the `habiliter` stage's authorization check (HTTPS, no SSH involved) | Yes |
| `SONAR_SRC_<ID>_TOKEN` | Admin token for source instance `<id>` — one per active source, name derived from the instance's `id` (see [Step 9](#step-9--declare-the-instance-inventory)); same use as above | Yes |
| `AWX_BASE_URL` | Base URL of the AWX/AAP controller (e.g. `https://awx.groupe.example`) | No |
| `AWX_API_TOKEN` | AWX API token used by `preflight` and `executer` to launch and poll job templates (see [Step 8](#step-8--awx-job-templates-for-each-migration-step)) | Yes |

Note what's *not* in this table: no GitLab SSO provider name to
configure (`ClientGitLab` requires every account to carry exactly one
external identity and reads it directly — see `gitlab_client.py` — a
simplification that holds because this org's GitLab accounts are
confirmed single-identity; if that ever stops being true, this client
would need a provider filter again, the way the SonarQube side already
has one). The Ansible roles also no longer read any `SONAR_*_TOKEN` from
the GitLab runner's environment — AWX supplies its own credentials to the
job templates directly (Step 8). The `SONAR_*_TOKEN` variables above
exist for the Python authorization check only.

Never set any of these directly on `sonar-migration-requests` or
`sonar-migration-runs` — the group is the single place they are
maintained; see
[`sonar-migration-engine/README.md`](../sonar-migration-engine/README.md),
§ "Protected variable naming convention", for the exact derivation rule
and the `variable_token` override escape hatch.

## Step 6 — Protect the production environment

**Settings → CI/CD → Environments** on `sonar-migration-engine`'s
pipeline (i.e. wherever the `executer` job's `environment:
migration-production` resolves, which is `sonar-migration-requests`'
project since that's where the pipeline runs): create the
`migration-production` environment and restrict who can approve deployments
to it to the central team. This — not `rules:` — is what reserves the
manual `executer` job for the central team even though every project team
member can see the pipeline.

## Step 7 — Dedicated runners

Register runners tagged `migration-sonar`, used by every job in
`ci/pipeline.yml`. They must not be shared with unrelated projects (the
protected variables from Step 5 would otherwise be reachable from jobs
outside this solution), and need:

- Outbound access to the GitLab instance (clone, API calls).
- Outbound HTTPS access to the SonarQube REST API on every instance
  (used directly by the `habiliter` stage's authorization check).
- Outbound HTTPS access to the AWX controller's API (used by `preflight`
  and `executer` to launch and poll job templates — see Step 8). **No
  SSH access to the SonarQube hosts is needed on the runners**: that
  access lives on the AWX controller only.
- A Python 3.11+-capable environment to create the venv each job builds
  (`pyproject.toml` requires `>=3.11`). No Ansible install on the
  runner: `ansible-core` runs inside AWX, never on the GitLab runner.

## Step 8 — AWX job templates for each migration step

The GitLab runners cannot reach the SonarQube hosts over SSH, so
`ansible/site.yml` and its roles run on the AWX controller instead of the
runner — GitLab CI launches each step as an AWX job template through the
AWX REST API and polls it to completion
(`migration.awx_client.ClientAWX`, `migration.cli lancer-gabarit`), the
same way it used to invoke `ansible-playbook --tags <step>` locally. The
roles themselves are unchanged; only who executes them changes.

**On the AWX/AAP side**, set up:

1. **A Project** pointing at this repository (or the collection it's
   published to via Ansible Galaxy, if that's your distribution path) —
   its `ansible/` directory, specifically. AWX must sync `ansible/site.yml`,
   `ansible/roles/`, `ansible/tasks/`, `ansible/group_vars/all.yml`, and
   the dynamic inventory script
   (`ansible/inventaire/depuis_instances.py`).
2. **An inventory sourced from that dynamic inventory script**, so AWX
   resolves `sonar_url`, `sonar_home`, `sonar_variable_token`, etc. per
   host exactly as a local run would — see
   [`ansible/README.md`](../sonar-migration-engine/ansible/README.md),
   § "Multi-entity inventory". This inventory is derived from the same
   `instances.yml` as the Python side (Step 9): keep it in sync by
   pointing AWX at the same source, not a copy.
3. **SSH credentials in AWX's own credential store**, for a system
   account able to read/write under each instance's `sonarqube_home`,
   and specifically able to `chown` the deposited archive to the
   SonarQube service account (`sonar_utilisateur_service` in
   `group_vars/all.yml`, default `sonarqube`) — see
   [`sonar_transfert`](../sonar-migration-engine/ansible/roles/sonar_transfert/tasks/main.yml).
   In practice this means either the account already *is* that service
   account, or the credential includes `become`/sudo rights to act as
   it. This is genuinely not decided by the code — confirm the real
   account and privilege model against your server provisioning before
   the first pilot run.
4. **SonarQube tokens as AWX Credentials too** (separate from point 3's
   SSH credential, and separate from the GitLab CI `SONAR_*_TOKEN`
   variables from Step 5): `group_vars/all.yml`'s
   `sonar_token: "{{ lookup('env', hostvars[inventory_hostname].sonar_variable_token) }}"`
   expects a variable like `SONAR_CENTRALE_TOKEN` in the **process
   environment ansible-playbook itself runs in** — which is now AWX's job
   environment, not the GitLab runner's. Inject these via an AWX Custom
   Credential Type (environment-variable injection) attached to every
   job template, one credential per instance, named after the same
   `sonar_variable_token` values already used in the inventory — do not
   hardcode a token as a job template's extra-var, which would put it in
   AWX's own job output/extra_vars, not just its credential store.
5. **One Job Template per Ansible tag**, named *exactly* as the tag it
   runs, since `lancer-gabarit` resolves templates by name:
   `preflight`, `export`, `transfert`, `capture_config`,
   `supprimer_cible`, `creer_placeholder`, `import`, `renommer_cle`,
   `appliquer_config` — each running `ansible-playbook site.yml --tags
   <name>` against the Project and inventory above, with the credentials
   from points 3 and 4 attached. Extra-vars (`sonar_run_id`,
   `sonar_cle_source`, `sonar_cle_cible`, `sonar_source_host`,
   `sonar_projet_cible_id`) arrive from GitLab CI at launch time — do
   not hardcode them on the template.
6. Consider enabling each template's **"prevent simultaneous jobs"**
   setting, as a second safety net alongside GitLab's own
   `resource_group` lock.

`docs/a-verifier.md` now also flags the exact AWX API response fields
(`job` on launch, the set of terminal `status` values) as unconfirmed
against a real AWX/AAP version — the client defensively falls back to
`id` if `job` is absent, but this should be checked once a real instance
is available.

### Alternative: reusing an existing shared-library job launcher

If your organization already has a shared-library function that launches
an AWX job template and waits for its result — several do, so this
solution doesn't need to own that responsibility — `ClientAWX` /
`migration.cli lancer-gabarit` can be swapped for it. The rest of the
pipeline (the state machine, the journal, the point-of-no-return handling
in `ci/pipeline.yml`'s `executer` loop) does not need to change: only the
one command each loop iteration runs changes.

**The exact swap point**, in `ci/pipeline.yml`:

- `preflight` stage: the line calling
  `"${ENGINE_CHEMIN}/.venv/bin/python" -m migration.cli lancer-gabarit --gabarit preflight --extra-vars "..."`.
- `executer` stage: the same call inside the `for etape in ...` loop,
  `"${ENGINE_CHEMIN}/.venv/bin/python" -m migration.cli lancer-gabarit --gabarit "${etiquette}" --extra-vars "${EXTRA_VARS}"`.

Replace either line with a call to the shared-library function instead —
everything around it (`enregistrer`, the loop, `set -e`) stays as-is.

**What the replacement must preserve**, for the rest of the pipeline to
keep working unmodified:

- **Synchronous / blocking**: it must not return until the AWX job has
  reached a terminal status. A fire-and-forget launch would let
  `enregistrer` log a state before the corresponding action actually
  succeeded — exactly what `lancer-gabarit`'s polling (`ClientAWX.attendre`)
  exists to prevent.
- **A job template name parameter**, fed the exact Ansible tag
  (`preflight`, `export`, `transfert`, …) — this solution treats the tag
  as the template name (Step 8, point 5); adjust if your shared library
  uses a different naming/lookup convention.
- **An extra-vars parameter**, fed the same `sonar_run_id=... sonar_cle_source=...
  ...` string this pipeline already builds (see `EXTRA_VARS` in the
  `executer` stage) — or an equivalent transformation into whatever
  format the shared function expects (JSON, a Python dict, etc.).
- **A nonzero exit / raised error on a failed or errored AWX job**, so
  the script's `set -e` still stops the loop before `enregistrer` runs —
  same contract `commande_lancer_gabarit`'s exit codes (0/1/5) provide
  today.

**Once swapped**, `migration.awx_client.py`, the `lancer-gabarit` CLI
command, and the `AWX_BASE_URL`/`AWX_API_TOKEN` GitLab CI variables
(Step 5) become unused — they can stay dormant (harmless) or be removed;
Step 8's AWX-specific setup (points 1-6 above) is then whatever your
shared library's own documentation already requires, not this document's
concern anymore.

The exact call syntax for your shared library isn't filled in above
because it isn't known yet — same spirit as `docs/a-verifier.md`: this
section names the contract, not a guessed implementation. Once the
function's signature is shared, this section (and the two call sites in
`ci/pipeline.yml`) can be updated for real.

## Step 9 — Declare the instance inventory

Edit
[`sonar-migration-engine/inventaire/instances.yml`](../sonar-migration-engine/inventaire/instances.yml)
— the single source of truth, shared by the Python authorization check
and the Ansible dynamic inventory (`ansible/inventaire/depuis_instances.py`).

1. **`centrale`** (exactly one): the group's central SonarQube instance —
   `id`, `libelle`, `url`, `api_identite` (`v1` or `v2` — confirm on the
   real instance, see `docs/a-verifier.md` point 2),
   `fournisseur_identite_sso`, `ssh_hote`, `sonarqube_home`.
2. **`instances_sources`**: one entry per entity SonarQube instance, same
   fields, plus `actif: true` once ready to accept requests. The
   delivered `entite-exemple` entry is a template (`actif: false`) —
   duplicate and adjust it per real entity rather than editing it in
   place.
3. **`groupes_interdits`**: SonarQube groups whose holding admin never
   counts as authorization (already populated with `Anyone`,
   `sonar-users`, `Members` — extend only if your instances use
   additional broad built-in groups).

Onboarding one new source instance requires, together, in the same
change where possible:

- the inventory entry above (`actif: true`),
- the matching `SONAR_SRC_<ID>_TOKEN` group variable (Step 5) — derived
  automatically from `id` (uppercase, `-` → `_`); use the optional
  `variable_token` field only for a documented exception,
  matching `^SONAR_SRC_[A-Z0-9_]{1,48}_TOKEN$`,
  AWX SSH credentials able to reach its `ssh_hote` (Step 8),
- a `CODEOWNERS` line for its requests folder (Step 10),
- the published catalogue entry (regenerated automatically by
  `inventaire.catalogue_public`, surfaced in
  `sonar-migration-requests/docs/instances-disponibles.md`).

## Step 10 — Per-entity setup in `sonar-migration-requests`

For each onboarded entity, add one line to
[`sonar-migration-requests/CODEOWNERS`](../sonar-migration-requests/CODEOWNERS)
designating that entity's own approvers for merge requests under
`requests/<instance_source>/` — human review is an additional layer, not
a substitute for the automated authorization check. The merge request
template
([`.gitlab/merge_request_templates/migration.md`](../sonar-migration-requests/.gitlab/merge_request_templates/migration.md))
and the informational schema copy are already provided and need no
per-entity change.

## Step 11 — Verify the installation

Before touching a real instance, validate the static configuration
locally:

```bash
cd sonar-migration-engine
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                                    # 180 tests, no network needed
.venv/bin/python -m migration.cli valider \
  ../sonar-migration-requests/requests/entite-exemple/*.yml
```

Then, on the real GitLab/SonarQube estate, submit an actual test request
and confirm the `valider` and `habiliter` stages behave as expected
(schema/consistency check with no token, then authorization check +
MR comment) before ever approving the manual `executer` job.

This guide only gets the plumbing in place. The actual proof that the
whole sequence works — refusals firing correctly, a full export/import
succeeding on a real project — is
[`sonar-migration-engine/docs/plan-validation-pilotes.md`](../sonar-migration-engine/docs/plan-validation-pilotes.md):
run it next, before any non-pilot migration.
