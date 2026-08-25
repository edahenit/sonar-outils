# To verify on real instances

This document lists everything this repository's code treats as
**configurable rather than guessed**. Every point must be confirmed on a
test instance before going to production, in the order listed — the first
one conditions the most downstream code.

This file is updated with every batch delivered. Nothing here is a silent
assumption: every unconfirmed behavior has a "to verify" section in the
corresponding batch's README, and a parameter to change it without
touching the code.

## 1 — `externalIdentity` field in login resolution (batch 2's pivot)

**Status: the starting point of the entire authorization check, to verify
first.**

The field is returned to an administrator caller by `api/users/search`,
but its availability and exact name have changed across SonarQube
versions, and the v2 API (`api/v2/users-management/users`) is presented as
the recommended path on recent versions.

**Parameter provided**: `api_identite: v1 | v2` per instance in
`inventaire/instances.yml` (see [modele.py](../src/migration/modele.py) and
[inventaire.py](../src/migration/inventaire.py)). Delivered in batch 2:
identity resolution is isolated behind the `ClientSonar.resoudre_login_par_uid`
interface (module `sonar_client.py`), with two implementations,
`_resoudre_login_v1` (`api/users/search`, field
`externalIdentity.login` / `externalIdentity.provider`) and
`_resoudre_login_v2` (`api/v2/users-management/users`, flat fields
`externalLogin` / `externalProvider`), selected by this parameter.

**Sub-point not independently verified**: neither `api/users/search` (v1)
nor, in all likelihood, `api/v2/users-management/users` (v2) offers a
server-side filter by external identity. The current implementation
therefore paginates every account on the instance and filters client-side
— correct on a directory of a few thousand accounts, potentially slow on
a very large directory. To revisit once the API is confirmed: if v2
exposes a dedicated filter, the v2 resolver must be fixed to use it.

**SSO provider name**: the inventory's `fournisseur_identite_sso` field
(e.g. `saml-entreprise`) must exactly match the value of the `provider`
field (v1) / `externalProvider` (v2) as exposed by SonarQube — to confirm
on the real instance, together with the point above.

**Impact if the behavior differs**: strong. This is the pivot of the
entire GitLab → SonarQube mapping (prompt §6-7): without it, no
authorization decision is reliable.

## 2 — Project creation on import (`api/project_dump/import`)

The Project Move documentation states that the archive is only read for a
project **already created, blank, and never analyzed**, and asks that the
target project be created with the source key before importing. Other
sources suggest that the import creates the project itself.

**Parameter delivered in batch 4**: `sonar_create_placeholder_project`
(boolean, default `true`,
[`ansible/group_vars/all.yml`](../ansible/group_vars/all.yml)) — never in
the requester's request file (see batch 1's decision: this parameter
belongs to the engine repository, not to the requester's input surface).
The [`sonar_creer_placeholder`](../ansible/roles/sonar_creer_placeholder/)
role is a complete no-op if `false`, with no other role needing to know
about it. Still to be detected on a test instance before any deployment —
this parameter allows switching without touching the code once the answer
is known.

**Impact if the behavior differs**: strong. One more or one fewer step in
the sequence, and one more or one fewer state in batch 3's state machine.

## 3 — Endpoints `api/project_dump/export` and `api/project_dump/import`

Used in practice, but absent from the functional documentation, which
only describes the UI flow. Parameter names and stability across versions
to confirm on `<instance>/web_api` of the real 2026.1.2 version.

**Detail delivered in batch 4** ([`sonar_export`](../ansible/roles/sonar_export/),
[`sonar_import`](../ansible/roles/sonar_import/)): implemented as `POST`,
`project` parameter, trigger response read via
`task.id | default(taskId)` (both forms exist depending on the SonarQube
endpoint, the second as a fallback). Produced file name assumed to be
`<source key>.zip` under `data/governance/project_dumps/{export,import}/`
— this is what the prompt itself states for the export, extended by
analogy to the import. Each role explicitly checks (`stat` + `assert`)
that the expected file exists after a `SUCCESS` Compute Engine task, with
an error message pointing back here instead of failing silently or
producing a raw HTTP error.

**Impact if the behavior differs**: medium, localized to the export and
import roles — the failure would be immediate and explicit (an
assertion), not silent corruption.

## 4 — Endpoints `api/alm_settings/set_*_binding` and `get_binding`

Specific to the forge type (GitLab, GitHub, Azure DevOps, Bitbucket) and
likely to have evolved. `set_*_binding` reapplies the binding at step 12;
`get_binding` (a read endpoint, presumed unique regardless of the ALM
type) captures it at step 7.

**Detail delivered in batch 4**: captured in
[`sonar_capture_config`](../ansible/roles/sonar_capture_config/) via
`api/alm_settings/get_binding`, with `ignore_errors: true` (does not
interrupt the capture of the rest if the endpoint is absent or
different). Reapplied in
[`sonar_appliquer_config`](../ansible/roles/sonar_appliquer_config/) via
`api/alm_settings/set_{{ binding.alm }}_binding`, also non-blocking.

**Impact if the behavior differs**: low, a single project to fix by hand
in case of failure (DevOps binding only, the rest of the reapplication
does not depend on it).

## 5 — GitLab namespace path used by `ci/pipeline.yml`

The clones of the engine repository and the runs repository, in every
job, use `${CI_SERVER_HOST}/groupe/sonar-migration-engine.git` (and the
equivalent for `sonar-migration-runs`) — `groupe/` is a placeholder, to be
replaced with the real GitLab namespace once the three repositories are
created ([`scripts/split-repos.sh`](../../scripts/split-repos.sh)).

**Impact if not corrected**: strong but trivial to fix — every pipeline
job would fail at the clone step, with an explicit error message
(repository not found), not a silently incorrect behavior.

## 6 — Endpoints introduced in batch 4 (preflight, capture, reapplication)

Seven additional points, all handled with an explicit failure (`assert`
or documented `ignore_errors`) rather than a silently guessed behavior:

| Endpoint | Usage | Role | Handling of the uncertainty |
|---|---|---|---|
| `api/system/info` | Version, edition, plugins | [`sonar_preflight`](../ansible/roles/sonar_preflight/) | Extracted via `System.Version` / `System.Edition` / `Plugins` with `default('UNKNOWN'/{})` — a failure produces an explicitly wrong value (`UNKNOWN`) rather than a masked Jinja error, and fails the source/central comparison. |
| `api/qualitygates/get_by_project?project=` | Quality gate capture | [`sonar_capture_config`](../ansible/roles/sonar_capture_config/) | `status_code: [200, 404]` — a 404 is accepted (no custom gate), not treated as a failure. |
| `api/qualityprofiles/search?project=` | Quality profiles capture | idem | Filtered by project (the parameter name `project` matches the convention used elsewhere in the SonarQube API, e.g. `api/qualityprofiles/add_project`); not independently confirmed for this specific endpoint. |
| `api/components/show?component=` | Tags capture | idem | Used instead of `api/project_tags/search`, which is a global tag-autocomplete endpoint with no project filter and was therefore the wrong endpoint for this purpose regardless of any other uncertainty. `api/components/show` is a well-documented, stable endpoint; the exact field carrying tags in its response (assumed `component.tags`) is not independently confirmed — `ignore_errors: true`, falls back to an empty list. |
| `api/permissions/groups?projectKey=&permission=`, `api/permissions/users?projectKey=&permission=` | Permission capture, by type | idem | Same shape already confirmed by batch 2 (`habilitation.py`), looped over the six known permission types. |
| `api/projects/create` (parameters `project`, `name`) | Placeholder | [`sonar_creer_placeholder`](../ansible/roles/sonar_creer_placeholder/) | Reasonable confidence (historically stable endpoint), not specifically reverified. |
| Name of the permission template applied by the portal | Permission reapplication | [`sonar_appliquer_config`](../ansible/roles/sonar_appliquer_config/) | **A decision made, not an open uncertainty**: no reliable read endpoint identified to recover the name of an already-applied template. The capture reads the effective permission state (`api/permissions/groups`/`users`, already confirmed in shape by batch 2) and the reapplication puts it back permission by permission (`add_group`/`add_user`), rather than using `api/permissions/apply_template` with a guessed name. If the portal's naming convention turns out to be known and stable, this path could be added alongside without touching the capture. |

**Impact if one of these points differs**: localized to the role
concerned, never propagated — this is precisely what the `status_code`
values accepting absence, the documented `ignore_errors`, and the
`assert`s that turn a gap into a clean failure rather than silent
corruption, are meant to achieve.

## 7 — AWX / Ansible Automation Platform API (`awx_client.py`)

**Context**: the GitLab runners have no network path to the SonarQube
hosts (SSH blocked), so `preflight` and `executer` launch Ansible as an
AWX job template through AWX's REST API and poll it, instead of running
`ansible-playbook` locally — see `docs/installation.md`, step 8.

Two response shapes are assumed from AWX's documented API, not
independently confirmed against a real AWX/AAP instance/version:

| Call | Assumed field | Handling of the uncertainty |
|---|---|---|
| `POST /api/v2/job_templates/{id}/launch/` | New job id in the `job` field | `ClientAWX.lancer` falls back to `id` if `job` is absent, rather than failing outright — some AWX/Tower versions have used one or the other. |
| `GET /api/v2/jobs/{id}/` | Terminal statuses: `successful` (success), `failed` / `error` / `canceled` (failure, returned to the caller, not raised) | If your AWX version reports a different terminal status string, a job would never reach a terminal state as understood here and `attendre` would raise `DelaiJobAwxDepasse` after `timeout_secondes` — loud (the pipeline stage fails), not a silent hang forever, but worth confirming against a real job's status history before the first pilot run. |

**Not covered here, decided in `docs/installation.md` instead** (not code
parameters, pure AWX-side configuration): job template naming (must match
the Ansible tag exactly, since templates are resolved by name), which
Project/inventory each template runs against, and where AWX's own
SonarQube SSH credentials live.

## Fixed while reviewing this batch, not left as an open question

Every read call against `api/projects/search` across the Ansible roles
(`sonar_preflight`, `sonar_creer_placeholder`, `sonar_supprimer_cible`,
`sonar_renommer_cle`, `sonar_import`) now passes an explicit `projects=`
query filter. The initial version of these roles queried the endpoint with
no filter at all and then filtered the result client-side — correct only
by accident on a central instance with 100 or fewer projects (the API's
default page size), since an unfiltered call only returns its first page.
This was a real bug, not an open uncertainty, and has been corrected — not
a parameter to tune later.

The ten call sites that made this lookup were also independent
copy-pastes of the same five-line `uri` task, which is precisely why the
missing filter existed in five different places instead of one. They now
all include the single shared task file
[`ansible/tasks/rechercher_projet.yml`](../ansible/tasks/rechercher_projet.yml):
a future fix to this endpoint (or any change to how the lookup works)
happens once instead of five times.

## What is NOT uncertain

By contrast, these points are settled by the prompt and are not reopened:
strict version alignment between instances (H5 of the design document),
the single SSO with a common LDAP UID (H2), the absence of a DevOps portal
API (H7), the `sonar-<space_id>-managers` group as the holder of target
admin rights (H3).
