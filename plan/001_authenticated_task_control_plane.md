# 001 — Authenticated task control plane

## Outcome

Implement the durable HTTP boundary for conversational browser tasks. A logged-in account or its API key can admit, inspect, cancel and respond to its own tasks, with explicit isolated browser profiles and no duplicated execution on HTTP retries.

Prerequisite: completed 000 only. Use fake executor/assistance ports during development. No dependency on 002–006 implementations.

## Ownership

Own `backend/tasks/{control,repository,routes,identity,schema}.py`, auth hardening in `backend/auth.py` and the account/API-key handlers in `backend/main.py`, and new `tests/test_task_control_*.py`. Use 000's bootstrap/ports; do not edit main registration/lifespan outside the assigned handlers, shared contracts, browser engine, assistance implementation or container files. Use new idempotent SQLite schema initialization/migration code within this package rather than concurrent edits to legacy `db.py`.

## Implementation slices

1. Reuse `auth.get_current_user` for existing dashboard JWTs and bcrypt-verified API keys. Bind ownership to the resolved account ID; reject forged client identity headers. Harden login/key creation request bounds, generic credential errors and rate limiting without logging passwords/keys. Require stable configured JWT secrets in hosted mode, preserve local development behavior and use 000's settings. Preserve create-once key disclosure, own-key list/delete and revocation semantics; verify prefix lookup/collision behavior rather than assuming prefixes uniquely identify secrets. No broker auth or Harp configuration.
2. Add additive task/session/interaction/idempotency/lease persistence under the existing durable profiles volume. Use transactions, unique constraints and conditional updates for capacity reservation, one-task-per-session, revisions and terminal transitions. Exercise migration reapplication and upgrades from an existing platform database.
3. Implement bounded requests and owner-checked routes exactly as 000 specifies. Foreign IDs must not leak profile/site/task details. Validate session ownership and current interaction generation on every mutation.
4. Commit admission before returning 202; dispatch only after commit. On dispatch failure settle or reconcile the admitted record explicitly. Claim execution atomically; a duplicate wake cannot start a second executor. Never hold a database transaction across model/browser/network waits.
5. Implement revision polling with bounded waits, disconnect cleanup and database-backed readback. Responses contain safe summaries, typed errors and result metrics, not credentials or raw exception text.
6. Implement interaction creation/resolution through one authority path shared by API and viewer. Store descriptors and resolution metadata, never OTP/password values. Deliver transient response values only to the current claimed executor. Duplicate decisions are idempotent; contradictory decisions conflict. Expired/orphaned interactions never wake a fresh task.
7. Persist cancellation intent, fence late completion/interaction updates and delegate bounded stopping to the executor. Do not mark cancelled merely because the request was accepted. Expose uncertain outcomes honestly.
8. Implement startup reconciliation and shutdown admission gating using 000's lifecycle ports. Reconcile stale execution without automatically rerunning website effects; invoke the executor cleanup port and retain recovery evidence. Define behavior for crash between database commit and dispatch and between browser action and result persistence.
9. Implement owned profile create/list/delete and explicit session/profile selection. Fresh profile means fresh browser storage, script memory and context, including under the same API key. Reusing an owned profile explicitly preserves them. Reject foreign profile IDs, conflicting session/profile combinations and deletion while leased; deletion must coordinate memory/archive removal through the runtime port. Distinguish account ownership from browser-profile isolation.

## Verification

- Race concurrent identical admissions: one task, one reserved slot, one executor start. Changed payload or wrong principal cannot reuse the admission.
- Two tasks against one session conflict; different owners cannot attach to each other's sessions. Capacity accounting includes pauses and releases on every settled failure path.
- Sign up/log in, create/list/revoke a key and call tasks from a fresh HTTP client. Two keys of one account share account authority; two explicit profiles stay storage-isolated. A revoked key cannot poll, mutate or mint viewer access, while a separate valid key can still inspect previously admitted tasks.
- Exercise all state transitions through actual HTTP handlers and real temporary SQLite, not mocked repository methods alone.
- Retry/lost-response admission, duplicate response, stale generation, decline, cancel-versus-complete and cancel-versus-resume settle according to 000.
- Interrupt a subprocess before/after dispatch and confirm startup readback, fencing and no automatic replay.
- Use credential canaries to check database/log/error output exclusion; ensure request body validation does not echo secret values.
- Existing flow authentication and route regressions remain green.

## Handoff / definition of done

Export the exact control-service/router factories reserved in 000. Provide fixtures with persisted running/paused/interrupted tasks for other streams, route-level evidence and schema upgrade evidence. No browser implementation or deployment is required to finish this stream. Final system compatibility is checked by 006.

## Lane 001 implementation and verification evidence

Branch: `implementation/lane-001`; isolated worktree: `flowpilot-worktrees/lane-001`.
Foundation: `0ccc06a`. Approved additive assistance amendment `2201b5a` was
cherry-picked alone as `f6a5750`; no other lane changes were imported.

Implemented:

- Side-effect-free `create_control(settings)` and `create_router(bindings)` exports;
  bind-before-startup, durable admission, exclusive claim and database-backed polling.
- Additive/reapplicable SQLite tables, transactional owner/idempotency/session/profile/
  capacity checks, terminal/revision/lease fencing, cleanup reservations and profile
  deletion tombstones. Legacy platform schema/data survive reapplication.
- Existing account/JWT/bcrypt API-key authority, colliding-prefix verification,
  create-once key disclosure and revocation; bounded/sanitized auth/key handlers,
  login/account rate limits and hosted configured-secret validation.
- Transient executable requests and interaction values; durable descriptors and
  context-bound keyed receipts. Duplicate decisions do not redeliver values;
  changed decisions conflict, and declined confirmations cannot pass approval checks.
- Cancellation intent before bounded executor stopping; unknown outcomes become
  interrupted, never automatically replayed. Caller disconnect cannot strand the
  commit-to-dispatch or response-to-wakeup windows.
- Startup interruption/revocation/fencing, cleanup through executor recovery and
  admission gating during shutdown. Unattested profile recovery is quarantined.
- `assistance_lease` atomically checks owner, claimed execution lease, current browser
  pause ID/generation and expiry. `save_capability` repeats validation in its commit;
  execution and pause generations remain distinct, with no lease data in TaskView.

Dedicated evidence:

- `tests/test_task_control_repository.py`: temporary SQLite admission/claim races,
  legacy upgrade/reapplication, side-effect-free factories, profile reuse and recovery.
- `tests/test_task_control_http.py`: actual handlers + SQLite, and a fresh loopback TCP
  client against uvicorn for signup/key/admission/poll/cancel. Includes owner isolation,
  retries, key revocation/collisions, capacity, profiles/deletion, transient OTP canaries,
  confirmation approval/decline, cancellation races, bounded polling, disconnect,
  failure sanitization, secret configuration and shutdown gating.
- `tests/test_task_control_recovery.py`: kills subprocesses at queued/running/paused
  durable checkpoints; verifies interruption, fencing and zero automatic redispatch.
  Exports `persisted_control_plane_fixture(root)` for synthetic persisted
  running/paused/interrupted integration state without editing foundation fixtures.
- `tests/test_task_control_assistance.py`: real SQLite lookup ownership/expiry,
  distinct execution/pause generations, mint revalidation after cancel/resolve/restart,
  concurrent mint/mutation commits and capability rotation.

Verified commands, with this worktree as cwd:

- `/Users/pgwork/Desktop/code/flowpilot/.venv/bin/python -m pytest -q`:
  **200 passed, 12 explicit live skips**.
- Same interpreter, `-m pytest tests/test_task_control_repository.py
  tests/test_task_control_http.py tests/test_task_control_recovery.py
  tests/test_task_control_assistance.py -q`: **29 passed**.
- `npm ci --prefix agent-host`: installed worktree-local dependencies; zero audit
  vulnerabilities. No lockfile/dependency changes.
- `npm --prefix agent-host test`: **4 passed**.
- Python compileall, `git diff --check 0ccc06a..HEAD`, and structural port parameter
  inventory/module-path checks: passed; production modules came from this worktree.
- Original red: repository test failed with missing `backend.tasks.repository` before
  implementation. Legacy short-password response regression found by the broad gate
  was corrected while still in implementation; original auth tests now pass.

### Original adversarial audit: failed criterion, subsequently repaired below

**Criterion:** foreign/missing object IDs use `not_found`, not a different envelope.

**Reproduction:** admit a task for account A; create a profile for account B; admit
another request as A with A's valid session ID and B's profile ID. The same issue
applies to an arbitrary nonexistent profile ID paired with the owned session.

**Expected:** `not_found` (HTTP 404).

**Observed:** `conflict` (HTTP 409). No profile/site/task details are disclosed, but
the error precedence does not satisfy the frozen foreign-object policy.

**Owning seam and proposed correction:** `Repository.admit` compares an explicitly
supplied profile ID with the owned session's profile before resolving ownership of
the supplied profile. Resolve any explicit profile via `_profile(db, principal,
profile_id)` first, then compare two owned IDs and return conflict only for an
owned-but-incompatible pair. Add HTTP/SQLite foreign-and-missing pair regression
cases. No shared-contract amendment is required.

The independent-verification workflow forbids silently repairing a discovered
failure. This criterion was left open until the integrator separately authorized
the focused repair pass recorded below. The original passing automated checks did
not supersede this observed failure.

### Integration boundaries and remaining risks

- Executor must terminally settle before releasing a browser reservation. Completed
  idle browsers remain counted until `release_browser`; executor owns idle eviction.
- During `recover`, an executor may attest a safely saved profile through
  `control.release_browser(lease, profile_saved=True)`. Control quarantines only leases
  not already released; duplicate old releases cannot quarantine a reused profile.
- Browser storage/script/context isolation, actual provider/browser shutdown and
  private viewer transport remain peer-owned and require assembled lane006 checks.
- No real browser/provider credentials, external website effects, deployment,
  cloud/live mutation, Harp edits, merges or parent-session moves were performed.
- Original lane verdict before repair: **failed final audit on the single
  error-precedence criterion above** despite passing automated local gates.

### Authorized focused repairs and final read-only verification

The integrator explicitly authorized a new implementation pass for the paired-ID
ownership defect, and separately authorized closing legacy auth/key response cache
gaps. No unrelated production repair or shared-contract change was made.

- `daa5503` — `Repository.admit` ownership-resolves explicit `profile_id` inside
  the admission transaction before comparing it to an owned session's profile.
  Foreign and missing IDs now produce byte-identical sanitized HTTP 404 envelopes;
  two owned incompatible objects still conflict, and a valid matching pair succeeds.
- `d700ebf` — scoped `auth.SensitiveRoute` preserves existing `no-store` success/
  validation handling and adds it to HTTP errors, early body/rate-limit errors and
  sanitized internal errors. Existing status and Retry-After headers are preserved.
  No frontend, task bootstrap or broad middleware change was needed.

Regression evidence:

- Before the ownership fix: both SQLite parameterizations (busy and settled owned
  sessions) and the actual HTTP route regression failed with conflict instead of
  not_found: **3 failed**. After the fix: **3 passed**.
- Rejected foreign/missing/incompatible admissions leave complete SQLite dumps and
  transient request maps unchanged: no capacity reservation, idempotency binding,
  profile generation/lease mutation or new session. The same rejected request key
  can subsequently admit a valid matching pair exactly once.
- Before the cache fix: the route regression confirmed signup/login/me/key-create/
  list/revoke success responses were already no-store, then failed on unauthenticated
  `/api/auth/me` lacking the header. After repair, coverage includes 200, 400, 401,
  404, 409, 422, 429 and sanitized 500 responses, including early bounds failures.
  Key metadata lists contain neither the raw key nor its hash; credential/error
  canaries are absent from error bodies and captured logs.

Final commands (same isolated worktree and shared virtualenv interpreter):

- `python -m pytest tests/test_task_control_repository.py
  tests/test_task_control_http.py tests/test_task_control_recovery.py
  tests/test_task_control_assistance.py tests/test_auth.py -q`:
  **41 passed** (33 lane tests plus 8 legacy auth tests).
- `python -m pytest -q`: **204 passed, 12 explicit live skips**.
- `python -m compileall -q backend/tasks backend/auth.py backend/main.py`:
  passed.
- `git diff --check 0ccc06a..HEAD`: passed.
- Read-only final review of both production commit diffs and changed-path inventory:
  only the named auth and repository repairs, dedicated tests and this plan's
  evidence were changed. No newly observed failing criterion or frozen-port conflict.

Final verdict: **conditionally verified**. The named defects are repaired and all
safe local gates pass. Assembled browser/profile/runtime/viewer behavior and the
explicitly skipped live checks remain lane006 integration obligations; no live
service mutation or deployment was performed. All prior commits and the approved
assistance amendment are preserved.
