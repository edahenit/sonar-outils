# sonar-migration-engine

Private repository of the central team. Contains the `.gitlab-ci.yml`
actually executed for the self-service SonarQube migration, the Ansible
roles, and the authorization check code. **Project team access: none.**

This repository carries every protected and masked variable (all the
tokens). This is why nothing it contains is writable by project teams —
see the monorepo's root README, § "the central control".

## Directory layout

```
inventaire/instances.yml     SonarQube instance inventory (authoritative)
src/migration/                Control plane: decides and traces, no shell call
  modele.py                   Domain types (Demande, Instance, Refus, Inventaire)
  chargement.py                Hardened YAML reading (anti-alias, anti-duplicate, anti-size)
  messages.py                  Single catalogue of messages shown to the requester
  validation.py                 YAML -> schema -> consistency -> uniqueness
  inventaire.py                 Loading and validation of the instance inventory
  sonar_client.py               SonarQube HTTP client: project, permissions, groups, v1/v2 identity
  gitlab_client.py              Server-side resolution of the requester's identity (never a job variable)
  habilitation.py               Authorization check — security core of the solution (§5)
  notification.py               Publishing comments on the merge request (the only channel used)
  rapport.py                    Markdown rendering of comments (authorization verdict, final report)
  machine_etats.py              State sequence, authorized transitions, anti-rollback guard
  journal.py                    Append-only journal + git publishing (sonar-migration-runs)
  decouverte.py                 Isolates the request file carried by a commit
  metriques.py                   Operational metrics: duration, failure rate, causes broken down (batch 5)
  awx_client.py                 AWX/AAP client: launches and polls Ansible job templates (runners have no SSH path to the Sonar hosts)
  cli.py                        Command-line interface — 6 commands, one per pipeline stage + metrics
  schema/*.schema.json          JSON schemas, authoritative
tests/                        Unit tests (pytest)
ansible/                      Server execution roles (batch 4) — one role per step
ci/pipeline.yml                Privileged pipeline, loaded by sonar-migration-requests (batch 3)
docs/a-verifier.md            Technical uncertainties and their configurable handling
docs/runbook.md                 Launching, following, interpreting a failure, recovery by state (batch 5)
docs/plan-validation-pilotes.md Pilot project validation plan (batch 6)
```

## Developing and testing locally

```bash
cd sonar-migration-engine
python3.11 -m venv .venv          # 3.11+ required (see pyproject.toml)
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/python -m migration.cli valider ../sonar-migration-requests/requests/entite-exemple/*.yml
```

## Deviation assumed from the prompt: schema and inventory in this repository

The prompt places the validation schema and the instance inventory in
`sonar-migration-requests`, where project teams hold the Developer role.
This repository holds them instead, for a concrete security reason: the
inventory contains the **URL** of each source instance, queried by the
authorization check with an admin token. An MR that changed this URL to a
server controlled by the requester would make the source-side check
answer "yes, they are admin", taking down the "AND" that is the whole
security barrier of the solution (prompt §5). The same reasoning applies
to the schema: loosening the project key regex from a file the requester
can modify would amount to having the input validated by its own
contract.

`sonar-migration-requests` keeps an **informational** copy of the schema
(for IDE support) and a public catalogue derived from the inventory (id +
label only, no URL or SSH host) — see
[`sonar-migration-requests/schema/README.md`](../sonar-migration-requests/schema/README.md).

## Protected variable naming convention

One entity = one instance = one set of variables. The variable name
carrying an instance's admin token is **derived** from its identifier in
the inventory, so there is only one rule to remember:

| Instance | Identifier (`id`) | GitLab CI variable |
|---|---|---|
| Central | `centrale` | `SONAR_CENTRALE_TOKEN` |
| Source | `entite-alpha` | `SONAR_SRC_ENTITE_ALPHA_TOKEN` |
| Source | `entite-beta-2` | `SONAR_SRC_ENTITE_BETA_2_TOKEN` |

Derivation: uppercase, `-` replaced with `_`, wrapped in `SONAR_SRC_` and
`_TOKEN`. A one-off exception is possible via the inventory's optional
`variable_token` field, but only if it matches
`^SONAR_SRC_[A-Z0-9_]{1,48}_TOKEN$` — see `inventaire.py::_variable_token`.
All of these variables are protected and masked, carried by the GitLab
group containing this repository, never by a requests or runs repository.

Dedicated GitLab token for identity resolution (prompt §6-7):
`GITLAB_API_TOKEN` — a GitLab API access token with read rights on merge
requests and users, never a requester's own token.

## Authorization check (batch 2)

`habilitation.est_admin()` implements the §5 algorithm: project found
unambiguously, identity resolution by UID (never by assuming login =
username), direct path then group path with exact comparison (never the
`q` filter) and systematic pagination, overly broad groups excluded and
flagged independently of the final verdict. `controler_habilitation()`
**always** runs both sides — source and target — even if the first one
fails, plus the source key collision check on the central instance, and
returns a `DecisionHabilitation` serializable with no secret whatsoever.

A design detail added in this batch, absent from the prompt: external
identity comparison is done by UID *value* (`extern_uid` on the GitLab
side, `externalIdentity.login` / `externalLogin` on the SonarQube side).
On the SonarQube side, resolving a UID means searching *every* account on
the instance, which needs the `fournisseur_identite_sso` field (one value
per instance in the inventory) to avoid a false match against an
unrelated account tied to a different identity source — still to be
confirmed on the real systems, see point 2 below. On the GitLab side, the
account is already known (resolved from the merge request), so
`ClientGitLab` only needs to check that account's own identities: since
every account here is confirmed to carry exactly one external identity,
it requires exactly that — no provider name to configure — and refuses
as a directory anomaly if it ever finds zero or several.

## The pipeline (batch 3)

`ci/pipeline.yml` defines 5 stages: `valider` (schema + consistency, **no
token at all**) → `habiliter` (identity + authorization check, journal,
MR comment) → `preflight` (Ansible, batch 4) → `executer` (`when:
manual`, protected environment, Ansible, batch 4) → `rapport` (publishing
the final report, `on_success` and `on_failure`).

**Required GitLab settings, in addition to the CI configuration path**
(see root README):

1. On `sonar-migration-engine`: *Settings → CI/CD → Token Access*, add
   `sonar-migration-requests` to the list of projects allowed to use
   their `CI_JOB_TOKEN` to clone this repository. This is what lets the
   `valider` stage read the code and the inventory **with no admin token
   at all** — `CI_JOB_TOKEN` is automatic, specific to each job, and
   grants nothing beyond a read-only clone of the explicitly authorized
   repositories.
2. Same setting on `sonar-migration-runs`, for the same reasons (the
   `habiliter`, `preflight`, `executer`, and `rapport` stages write the
   journal there via `CI_JOB_TOKEN`, never via an admin token).
3. A protected `migration-production` environment (*Settings → CI/CD →
   Environments*), whose triggering only the central team can approve —
   it is this setting, not `rules:`, that reserves the `executer` job for
   the central team.
4. Dedicated, tagged (`migration-sonar`) self-hosted runners (H6 of the
   design document): without them, sonar-migration-engine's protected
   variables could land on a runner shared with another repository.
5. An AWX/AAP job template per Ansible step, reachable over the AWX API
   from these runners — the runners themselves have no SSH path to the
   SonarQube hosts, so `preflight` and `executer` launch and poll AWX
   jobs (`awx_client.py`, `migration.cli lancer-gabarit`) instead of
   running `ansible-playbook` locally. See `docs/installation.md`, step 8,
   for the full AWX-side setup.

No hand-rolled lock: `resource_group: migration-${CI_COMMIT_SHA}` (native
to GitLab CI) answers the environment's "uniqueness lock per target
project" need — a concurrent pipeline on the same run is queued, never
run in parallel.

## To verify

See [`docs/a-verifier.md`](docs/a-verifier.md): the technical points that
cannot be verified without access to real instances (access path to
`externalIdentity`/v2 API and exact SSO provider name, project creation
behavior on import, signature of the `project_dump` endpoints,
`alm_settings`, exact GitLab namespace path used by `ci/pipeline.yml`),
treated as parameters rather than as assumptions.
