# sonar-migration-requests

Repository where project teams submit their SonarQube migration requests
to the group's central instance, preserving analysis history. Project
team access: **Developer**.

For the full walkthrough — exact field formats, what every refusal
message means and who needs to act on it, what happens after acceptance
— see [`docs/team-guide.md`](docs/team-guide.md). This README stays the
short version.

## Before you start

Your application's DevOps space, and its target SonarQube project, must
already have been created via the **DevOps portal**. This is what
generates the target key and the group that makes you an administrator of
that project. Without this step, your request will be refused with the
message "Create the DevOps space and its project via the portal first,
then resubmit your request."

Your source instance must also be onboarded (referenced in
`docs/instances-disponibles.md`). If it is not listed, contact the
central team before continuing.

## Submitting a request

1. Create a branch and duplicate
   [`requests/entite-exemple/grp-exemple-facturation-api.yml`](requests/entite-exemple/grp-exemple-facturation-api.yml)
   to `requests/<your instance identifier>/<your target key>.yml`.
2. Fill in `instance_source`, `cle_source`, `cle_cible`. The `ticket`,
   `fenetre_souhaitee`, and `commentaire` fields are optional: they are
   copied as-is into the final report, never used in any decision.
3. Open a merge request to `main` with the provided template.
4. **Wait for the verdict before doing anything else.** The merge
   automatically triggers, within a few minutes, a check that verifies
   you are indeed an administrator of both the source project **and**
   the target project, along with technical preflight checks. The result
   is published as a comment on your MR.
5. On refusal, the comment precisely states which of the two checks
   failed and what action to take. Fix it, then submit a new request —
   do not modify or relaunch the old one.
6. On success, the actual migration is **not** immediate: it is scheduled
   and triggered by the central team within a window agreed with you.
   You will be notified via a new comment.

## What you cannot do from this repository

This repository's [`.gitlab-ci.yml`](.gitlab-ci.yml) file is **never
read**, even if modified in the same MR as your request — see the
comment at the top of that file. Modifying a file other than your request
in an MR therefore has no effect on the pipeline; it is neither necessary
nor useful.

## After the migration

The migration switches the history over to your new key, but **does not
modify your code repositories**. It is up to your team to point your
analyses (`sonar-project.properties`, your projects' CI configuration) to
the target key after the success is confirmed — the final report is a
reminder of this.

## One request, one file, once

A request exists only once: the journal refuses any `(source instance,
source key)` or `(source instance, target key)` pair already processed
successfully. No need to resubmit an identical request "just in case" —
if in doubt about the state of a previous request, contact the central
team rather than opening a second one.
