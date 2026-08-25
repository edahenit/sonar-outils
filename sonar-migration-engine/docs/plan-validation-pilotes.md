# Pilot project validation plan

The final batch before going to production. Objective: prove that the
authorization check refuses exactly what it must refuse, accepts exactly
what it must accept, and that the full execution sequence (steps 4 to 13)
works on real SonarQube instances — something none of this repository's
unit tests can prove, since they all run against doubles
(`httpx.MockTransport`), never against a real server.

## 1 — Selecting pilot projects

**Two projects, a third optional**:

| Pilot | Role | Why |
|---|---|---|
| **A** | Full end-to-end migration | Must be a real but low-stakes project (little to no production use, an owner available for the whole test window) — a successful import here validates steps 4 to 13 under real conditions. |
| **B** | Authorization check only, never executed to completion | Covers the refusal scenarios (§3) without ever crossing the point of no return (step 8). Can be a fictitious project created for the occasion. |
| **C** *(optional)* | Source-admin-via-group case | Needed only if no existing project of the pilot entity already has its admin held by a group on the source side (see prompt §7: on the source side, the convention is not guaranteed — a directly-administered project does not cover this case). |

**Selection criteria for pilot A**:
- Belongs to an entity already onboarded in `inventaire/instances.yml`
  (or onboarded for the occasion — also an opportunity to validate the
  onboarding procedure itself).
- DevOps space and target project already created via the portal
  (an absolute prerequisite — see §2).
- Modest analysis history (the first real export/import must stay quick
  to replay if iteration is needed).
- A project team owner available to merge the request and confirm the
  outcome after switching their analyses over.

## 2 — Prerequisites, to check before the first attempt

In order — each point blocks the next if it fails:

1. **GitLab settings** (see root README and `ansible/README.md`): CI
   configuration path on `sonar-migration-requests`, Token Access
   allowlist on `sonar-migration-engine` and `sonar-migration-runs`,
   protected environment `migration-production`, runners tagged
   `migration-sonar`, protected branches.
2. **AWX job templates** for every step (`preflight`, `export`,
   `transfert`, `capture_config`, `supprimer_cible`, `creer_placeholder`,
   `import`, `renommer_cle`, `appliquer_config`), with SSH credentials to
   pilot A's source and central hosts already attached — see
   `docs/installation.md`, step 8. Without this, `preflight` fails
   immediately on the first AWX API call, before ever reaching a
   SonarQube host.
3. **Protected variables** set on the group holding
   `sonar-migration-engine`: `GITLAB_API_TOKEN`, `SONAR_CENTRALE_TOKEN`, one
   `SONAR_SRC_<ID>_TOKEN` per pilot source instance, `AWX_BASE_URL`,
   `AWX_API_TOKEN`.
4. **Pilot A's DevOps space created via the portal** — this is what
   produces the target key and the `sonar-<space_id>-managers` group.
   Without this step, every attempt fails immediately on
   `PROJET_CIBLE_INCONNU`, which is the desired behavior but not what is
   being verified here.
5. **Uncertainty point #1 first** (`docs/a-verifier.md`): identity
   resolution (v1 or v2, exact SSO provider name). Without it, no
   scenario below is interpretable — an unexpected `COMPTE_INCONNU_INSTANCE`
   refusal could mean "the person never logged in" or "resolution is
   misconfigured", and these two causes are only distinguished by
   confirming this point first. **Verification**: submit a request with an
   account known to be an administrator on both sides (scenario §3.1); a
   refusal at this stage flags this point above all others.
6. **Uncertainty point #2**: behavior of project creation on import
   (`sonar_create_placeholder_project`). To confirm on pilot A before
   step 10 — see §5.
7. **Uncertainty points #3, 4, 6, 7**: `project_dump`, `alm_settings`,
   `api/system/info` endpoints, and the AWX API response fields
   (`awx_client.py`). Self-confirm by running pilot A; no separate
   verification needed.

## 3 — Authorization scenarios to cover

All run against pilot **B** (never to completion), unless stated
otherwise. Each scenario checks three things: the MR comment, the last
journal entry (`sonar-migration-runs/journal/<run_id>.jsonl`), and the
absence of any action on the Sonar instances in case of refusal.

| # | Scenario | Setup required | Expected outcome |
|---|---|---|---|
| 3.1 | **Admin nowhere** | An account with admin on neither the source nor the target project submits the request. | `AUTHZ_REJECTED`. Both proofs (`preuve_source`, `preuve_cible`) carry `ok: false`, code `PAS_ADMIN` on both sides (or `PROJET_SOURCE_INCONNU`/`PROJET_CIBLE_INCONNU` depending on whether the projects exist). The comment names both sides separately. |
| 3.2 | **Source admin only** | The account is admin (direct or group) of the source project, not of the target project. | `AUTHZ_REJECTED`. `preuve_source.ok == true`, `preuve_cible.ok == false` (`PAS_ADMIN`, or one of §5's special cases depending on the target project's real state). The comment explicitly says only the target side is a problem. |
| 3.3 | **Target admin only** | The account is a member of the `sonar-<space_id>-managers` group (target admin), not admin of the source project. | `AUTHZ_REJECTED`. `preuve_cible.ok == true`, `preuve_source.ok == false`. Verifies that the check does not stop at the first success — the most direct proof that "both checks always run" (prompt §5) is not just a code promise but an observed behavior. |
| 3.4 | **Admin via group on both sides** | The account has **no** direct permission at all; its admin status comes solely from group membership, on the source side AND the target side (`sonar-<space_id>-managers`). | `AUTHZ_PASSED`. Both proofs carry `voie: "GROUPE"` with the group name. This is the scenario that covers H3 of the design document (the central instance never has a direct permission) — the only one of the six that **must succeed**, also to be run on pilot A to validate the rest of the sequence. |
| 3.5 | **Target project already analyzed** | Pilot B's target project has received at least one analysis (trigger a minimal scan after its creation by the portal). | `AUTHZ_REJECTED`, code `PROJET_CIBLE_DEJA_ANALYSE`, regardless of the requester's authorization level — this refusal happens before identity resolution even runs (see `habilitation.est_admin`). |
| 3.6 | **Source key collision** | Manually create, on the central instance, a project holding exactly the source key declared in the request (simulating another entity already migrated under that key). | `AUTHZ_REJECTED`, code `CLE_SOURCE_COLLISION_CENTRALE` present in `decision.refus`, independent of the two `PreuveAdmin` results — to verify in particular with an account that is otherwise admin on both sides (3.4), to confirm that the collision alone is enough to refuse. |

**Cases not listed by the prompt but worth checking along the way**,
since scenarios 3.1–3.4 naturally exercise them:
- **Direct admin on both sides** (the simplest path) — serves as a
  reference case before 3.4, to isolate a possible group-resolution issue
  from a broader identity-resolution issue.
- **Overly broad group** (`sonar-users` holding `admin` on the source
  project) — a case to set up on a test project if a pilot entity has
  one; otherwise, deferred to production with monitoring of
  `GROUPE_TROP_LARGE` alerts (see runbook, §3).
- **Directory duplicate** — hard to trigger deliberately without
  modifying the corporate directory; absent the ability to test it,
  reread `test_doublon_annuaire_est_refuse_avec_alerte`
  (`tests/test_habilitation.py`) as proof of logical coverage, and
  monitor production alerts during the first weeks.

## 4 — Running pilot A (full migration)

1. Submit the request with an account that is admin on both sides
   (preferably via the group path, scenario 3.4, to cover the real
   nominal case).
2. Check `AUTHZ_PASSED`, then launch `preflight` — confirm that the
   preflight checks pass (versions, plugins, disk space, referenced
   quality gate/profile).
3. Launch `executer`. **Pay particular attention to step 9**
   (placeholder): confirm whether the project is created by this role or
   by the import itself (uncertainty point #1), and adjust
   `sonar_create_placeholder_project` in `ansible/group_vars/all.yml`
   accordingly going forward.
4. At `DONE`, check on the central instance: history visible, correct
   key, permissions/quality gate/profiles/tags reapplied (compare
   against the pre-migration configuration, captured in the journal at
   step 7).
5. Ask the project owner to confirm the switch of their analyses to the
   new key (`sonar-project.properties`), and to confirm that the
   displayed history matches their expectations.

## 5 — Cleanup between attempts (pilot B)

Pilot B never reaches step 8: no instance-side cleanup is needed on the
Sonar side between two §3 scenarios. Only the requests repository and the
journal accumulate test runs:

- A refused request can be resubmitted identically (the journal only
  blocks the replay of a run that has **already succeeded** — see
  `journal.verifier_pas_deja_reussie`): just change the target key or
  remove the previous request file before relaunching.
- Test runs can stay in `sonar-migration-runs`: it is a journal, not
  meant to be cleaned up. Distinguishing them from production runs is
  only useful for reading purposes (prefix `instance_source` with a test
  identifier, e.g. `entite-alpha-pilote`, if the pilot entity also has
  production projects tracked in parallel).

## 6 — Success criteria

Going to production is worth considering once:

- [ ] The six scenarios in §3 produce exactly the expected outcome.
- [ ] Pilot A reaches `DONE` with correct history and configuration,
      confirmed by the project owner.
- [ ] Uncertainty points #1 and #2 (`docs/a-verifier.md`) are resolved
      and the corresponding parameters (`api_identite`,
      `sonar_create_placeholder_project`) are fixed in the inventory and
      `group_vars/all.yml`.
- [ ] No secret appears in a job log or in the journal — to explicitly
      verify by rereading the pilots' logs (not just trusting
      `no_log: true`).
- [ ] The runbook (`docs/runbook.md`) has been followed at least once for
      a real recovery (deliberately trigger a `preflight` failure on
      pilot A, e.g. by temporarily lowering
      `sonar_espace_disque_minimum_mo` to an unreachable value, then
      follow the recovery procedure).

## 7 — Report template

For each scenario run: date, run_id, outcome obtained, any gap from the
expected outcome, a link to the corresponding journal entry, and for
uncertainty points, the confirmed value to report in
`docs/a-verifier.md` (which must be updated at the end of this batch —
each verified point moves from "to verify" to "confirmed on YYYY-MM-DD on
`<instance>`").
