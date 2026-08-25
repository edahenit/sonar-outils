# Compute Engine task tracking

Export (`api/project_dump/export`) and import (`api/project_dump/import`)
are asynchronous: the initial call returns a task identifier, not a
result. The result is obtained by polling `api/ce/task?id=<id>` until a
terminal status. The [`sonar_attendre_tache_ce`](../roles/sonar_attendre_tache_ce/)
role factors out this tracking — used by `sonar_export` and `sonar_import`,
never duplicated.

## Three outcomes, never confused

| Outcome | How it shows up | What it means |
|---|---|---|
| **In progress** | `task.status` is `PENDING` or `IN_PROGRESS` | Normal, keep polling. |
| **Compute Engine failure** | `task.status` is `FAILED` or `CANCELED`, obtained **before** retries are exhausted | The task genuinely failed on the SonarQube side (corrupted dump, insufficient permission detected late, etc.). The `task.errorMessage` message is surfaced as-is. |
| **Pipeline timeout** | No terminal status after `sonar_ce_timeout_secondes`, the `until` loop is exhausted | Tracking was interrupted **on our side**; the task may well keep running and succeed later on the SonarQube side. This is not a failure of the operation. |

Confusing the last two cases is the classic trap: treating a timeout as a
task failure can lead to relaunching an export that is still in progress
(a useless duplicate CE task), or worse, to considering an import
permanently failed when it actually succeeded a second after tracking was
abandoned.

## How the code distinguishes them

In [`tasks/main.yml`](../roles/sonar_attendre_tache_ce/tasks/main.yml):

1. The `uri` task with `until` / `retries` / `delay` polls `api/ce/task`
   until a terminal status. `ignore_errors: true` is necessary: without
   it, the `until` failure would stop the play before the next task,
   which is precisely the one that must distinguish *why* the loop
   exited.
2. A `fail` task conditioned on `task.status in ['FAILED', 'CANCELED']`
   covers the Compute Engine failure — SonarQube's error message included.
3. A `fail` task conditioned on `sonar_ce_reponse.failed` (which is true,
   at this point, only if the status never reached a terminal state)
   covers the pipeline timeout — with a message that explicitly says this
   is not a failure of the task itself, and that tracking can be safely
   replayed (the role only polled, never triggered a second task).
4. Otherwise, `task.status == 'SUCCESS'`: the result is recorded in
   `sonar_ce_resultat`, used by the calling role (`sonar_export`,
   `sonar_import`) for the next step.

## Timeout: value and adjustment

`sonar_ce_timeout_secondes` (default 1,800 s, [`group_vars/all.yml`](../group_vars/all.yml))
bounds the total tracking time, not the number of attempts directly —
`sonar_ce_tentatives` is derived from it (`timeout / interval`). On a
large project, increase `sonar_ce_timeout_secondes` as an extra-var rather
than modifying the role.

## To verify

The exact name of the error field (`task.errorMessage`) and the full set
of possible `task.status` values are not confirmed on a real 2026.1.2
instance — see [`docs/a-verifier.md`](../../docs/a-verifier.md), point 3
(`project_dump` endpoints). `PENDING`, `IN_PROGRESS`, `SUCCESS`, `FAILED`,
`CANCELED` are the values documented for the historical Compute Engine
API; to confirm they have not changed.
