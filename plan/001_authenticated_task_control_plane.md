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
