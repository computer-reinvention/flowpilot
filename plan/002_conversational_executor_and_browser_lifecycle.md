# 002 — Conversational executor and browser lifecycle

## Outcome

Run the existing conversational agent as a remotely controlled, private browser session without changing its script/replay value or relying on a local terminal. Restore website state safely across graceful closes; report interruptions without replaying uncertain effects.

Prerequisite: completed 000 only. Use supplied fake control-service and interaction ports; no dependency on other parallel implementations.

## Ownership

Own `backend/tasks/{executor,runtime,browser_adapter,memory}.py`, extraction changes in `backend/repl.py`, `backend/repl_sandbox.py`, `backend/sandbox.py`, and new `tests/test_task_executor_*.py`. Do not edit `handoff.py`, control persistence/routes, bootstrap/contracts, Compose, Dockerfiles or Harp. Coordinate sandbox configuration through 000's settings, not new ad hoc environment reads.

## Implementation slices

1. Extract/inject browser creation, memory ownership and interaction transport from `BrowserSession`. Local `./chat` retains terminal questions, the loopback handoff default and identity locking. Hosted execution has no stdin fallback and never starts a per-session public/loopback help server.
2. Implement an internal Docker-network CDP adapter. The current `ChatSandbox` loopback-published port is for host-local use, not backend-container access. Hosted browsers publish no CDP host ports. Use the existing sandbox image and restore-before-launch/profile-archive safety semantics.
3. Derive private storage keys from authenticated account ID plus server-issued profile ID, never untrusted path segments, API-key strings or a client user header. Lock profile/memory access, serialize browser writes and prevent simultaneous processes opening one profile. Distinct profiles under one account isolate cookies/local storage/scripts/history; explicit profile/session reuse restores them. Preserve per-site script lookup and follow-up history. Bound/redact persisted task context and script metadata; do not persist transient question responses.
4. Implement the task executor port with atomic claim/generation ownership supplied by 001's interface. Publish progress/interaction/terminal events through that port. Restore durable session context for a new task; do not mistake that for recovering a suspended agent after restart.
5. Preserve saved-script replay, partial-state inspection, repair and observable completion checks. A run reports success only on actual completion evidence. Script reuse is scoped to the correct personal identity/site. No replay is allowed merely because a client retried an admission or status request.
6. Bridge input/confirmation/browser-help requests to 000's interaction channel. Keep browser/model work paused while waiting. Explicit decline is not approval; browser resolution requires inspecting current state before more effects. Enforce bounded waits and current task/pause generation.
7. Implement browser handle access for 003 only during the authorized active pause. Agent and human must never write concurrently; resume waits for in-flight human inputs and releases held keys/buttons. The assistance implementation owns its HTTP transport, not this stream.
8. Stop model subprocesses, timed script execution and browser writers on cancellation/deadline. Fence callbacks from old tasks, save/close within a budget and accurately report failure to stop. Distinguish completed, cancelled, interrupted and profile-recovery-required outcomes.
9. Implement idle eviction, graceful archive save and startup cleanup/quarantine by task/session labels. Do not globally kill unrelated flow/REPL containers. Failed profile save retains the previous archive and a bounded recoverable artifact instead of silently declaring persistence successful.
10. Make sandbox memory/CPU/PID/shared-memory controls configurable through 000. Reproduce the 256-PID failure before selecting a default; 1024 is a benchmark input, not an automatically justified universal setting. Admission and measured settings must agree.
11. Use 000's observation policy injection for Pi execution, completion and repair; 007 implements that policy and owns `pi_browser.py`/observation-related `runner.py` edits. Develop against the contract double. Do not reimplement freshness hashing or readiness waits inside the runtime, and do not bypass the injected guard during code-mode/reflex dispatch.

## Verification

- Real local Docker Chromium inside the same topology as the backend, with no host CDP publication.
- Alice cookies/local storage/history/scripts survive graceful restart; Bob starts clean. Parallel or adversarial profile opens fail safely.
- Synthetic first task plus follow-up; save/reuse/repair checks with observable counters so repeated submissions are detectable.
- Human wait, explicit decline, cancel while model/script/help is active, idle timeout and profile-save failure.
- Kill the executor after a synthetic website mutation; recovery reports unknown/interrupted and never replays that mutation automatically.
- Local REPL tests and existing replay/sandbox tests remain green. Provider-backed smoke is explicit opt-in with synthetic sites and authorized credentials; injected inference alone does not prove live model quality.

## Handoff / definition of done

Export 000's runtime/executor/browser factories, deterministic engine fixtures and local Docker smoke evidence. Document profile-save and interruption limits in the evidence output. Do not modify other owners' production files to make integration pass.
