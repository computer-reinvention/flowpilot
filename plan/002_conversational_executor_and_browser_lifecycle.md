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

## Lane 002 implementation evidence (2026-10-21)

Branch: `implementation/lane-002`, foundation `0ccc06a`. Approved additive amendment `2201b5a` was cherry-picked as `de30664`; no independent shared-contract changes were made. Native lane007 has deliberately not been cherry-picked.

### Delivered

- Factories: `backend.tasks.executor.create_executor(settings)`, `runtime.create_runtime(settings, execution, bindings)`, and `browser_adapter.create_browser(settings, lease)`. Factory construction performs no I/O. Lifecycle follows the frozen bindings/control port and never imports control persistence.
- Authenticated account+server-issued profile IDs derive SHA-256 private namespaces, with cross-process flock ownership for profile and SQLite memory. Known input values are redacted in retained context/results; transient question responses never enter history inputs. After collecting private input, hosted script saving is refused and the model's result is replaced by a private-input placeholder in durable history. History is retention-limited/100 turns, scripts limited to 100 entries with bounded metadata/source. Local chat retains its terminal, local handoff, repair and locking defaults.
- Hosted browsers use existing image/resource settings and Docker-internal bridge addresses, without published CDP ports or a local/public assistance listener. Docker DNS authorities proved incompatible with Chromium's discovery/upgrade Host check; using the authoritative bridge IP fixed actual container-to-container transport. Legacy cleanup/reconciliation excludes `flowpilot-task` containers before task startup.
- Interaction waits use control's transient channel, deadline/pause generation, and affirmative approval verification. Decline ends/fences the hosted task rather than leaving further effects to a model interpretation. Human input and resume serialize/drain held input; screenshot PNG, JPEG frames and `metadata()` revalidate the current authorized pause. Metadata comes from the live selected page, never a cached/fabricated URL or desktop-size assumption.
- Duplicate dispatch is suppressed; generation/revision checks remain control-owned. No admission/status request can replay a browser script. Hosted failed replay returns partial-state evidence for inspection/remaining-work repair, without the local REPL's unsafe automatic whole-script repair/retry. Original local repair tests remain unchanged/green.
- Cancellation fences the runtime, cancels the agent (whose existing Pi bridge reaps its model subprocess/tool children), detaches browser-use watchdogs, requests Chromium's profile-flushing close, and enforces container stop. Only then does cleanup save/release/settle. A terminateExecution attempt alone is not accepted as proof of stopped timers/network writers. Failure/deadline/unknown-stop outcomes remain typed. Native policy `TaskFault` reasons are preserved, including interrupted state for unknown effects.
- Startup reconciliation only touches matching task-host/profile-labelled containers, locks each profile, stops/quarantines uncertain state and never starts an engine. Profile deletion removes matching quarantined Docker artifacts while holding the same memory/profile lock. Task-host identity hashes stable state-directory plus sandbox network.

### Verification commands and results

All commands run from `/Users/pgwork/Desktop/code/flowpilot-worktrees/lane-002`.

1. `/Users/pgwork/Desktop/code/flowpilot/.venv/bin/python -m pytest -q`: **195 passed, 12 explicit live skips** before the final small replay/frame test additions; rerun final count in the handoff. This includes original REPL/replay tests and actual synthetic Node-child cancellation/reaping. The first full run failed only because this worktree lacked agent-host dependencies; an own ignored `npm ci --prefix agent-host --ignore-scripts` install resolved it, without root-worktree edits.
2. Dedicated tests: `tests/test_task_executor_{storage,lifecycle,conversation}.py` exercise strict frozen peer doubles, profile/account isolation and adversarial opens, atomic dispatch suppression, cancellation, declined confirmation, authorized metadata and stale handles, in-flight input drain, typed observation faults, no stdin, no persisted private response, failed profile save/old archive preservation, unsafe archive rejection, and recovery lock exclusion.
3. Created a dedicated network only: `docker network create --label flowpilot-lane002-smoke=1 flowpilot_lane002_smoke`.
4. Real smoke command (existing local images; no build/provider/secrets/site writes):

   ```sh
   docker run --init --rm --name lane002-smoke --label flowpilot-lane002-smoke=1 \
     --network flowpilot_lane002_smoke -e SANDBOX_NETWORK=flowpilot_lane002_smoke \
     -e PYTHONPATH=/lane -v /var/run/docker.sock:/var/run/docker.sock \
     -v /Users/pgwork/Desktop/code/flowpilot-worktrees/lane-002:/lane:ro -w /lane \
     flowpilot-backend:latest /app/.venv/bin/python -m tests.test_task_executor_docker
   ```

   Passed: no host CDP bindings; Alice p cookies/local storage/history/scripts survive graceful close/restore; Alice q and Bob p start clean. Actual BrowserSession/Pi dispatch with injected deterministic inference saves a script on task 1, reuses on explicit task 2, verifies counters 1 then 2, and receives the exact injected policy. Real authorized metadata reads `http://lane002-smoke:8765/`, **831×617** viewport; screenshot/frame transport is viewport-only. Actual long-running JS cancellation reports cancelled, saves/restores counter **1**, never executes delayed counter **2**, and settles in **1.30 seconds** on the measured run. A stronger restore check initially exposed lost recent storage with stop-only cancellation; requesting Chromium close before forced-stop fallback fixed it.
5. Same Docker command with `--pids`: default **256** reproduced failure after **4** synthetic tabs, `pids.current=256`, `pids.events max=53`, tab-creation TimeoutError. Benchmark input **1024** opened **24** tabs, `pids.current=530`, `max=0`. Shared default remains unchanged: 1024 is measured workload input, not a universal capacity claim.
6. Abrupt crash: same command **with `--init`**, dedicated temporary `/state` mount, and `--crash` runs the actual Executor/BrowserSession/Pi-dispatch stack, mutates synthetic local storage to 1, then SIGKILLs the executor before completion/cleanup (**expected exit 137**). Re-run with the same state mount and `--recover`: verifies mutation 1, stops/quarantines the orphan, refuses reopening recovery-required state, never creates an engine/replays the mutation, and deletes the profile's retained artifact. `--init` is mandatory because namespace PID 1 can ignore a self-directed SIGKILL; the harness rejects that invalid setup.

### Integration requirements and remaining limits

- Parent must integrate its already-approved `2201b5a` once; skip duplicate lane cherry-pick `de30664`. 001 provides atomic lease/revision authority, dispatch/cancellation/interaction state changes, interrupted records and release accounting; 003 consumes the concrete handle including live metadata. 007 supplies the native observation implementation. Do not mistake strict policy doubles for verification of native readiness/freshness algorithms; assembled verification remains required.
- Native Pi checked execute needs explicit boolean async pre/postconditions and fresh observation between effects. Deterministic Docker scripts supply those arguments; native runtime/runner files are not forked here. All typed policy faults are covered at the executor boundary.
- Backend and sandbox must share `SANDBOX_NETWORK`; keep state-directory/network identity stable across restarts so reconciliation matches existing task-host labels. Disable legacy raw `FLOWPILOT_TRACE` tracing: executor startup fails closed if it is enabled. Task resources use the same injected CPU/memory/PID/shm settings as admission.
- Eager eviction policy: every terminal task closes/saves/releases its browser immediately (zero idle retention, within any configured idle maximum). There is no warm-browser optimization; follow-ups restore durable profile/history, not a suspended Python stack or guaranteed open-tab layout.
- Archives are bounded at 256 MiB (272 MiB source tar budget), regular-file-only, atomic replacement; failed save keeps the previous archive and quarantines the stopped container. Retention is bounded to the failed profile's artifact count by the profile lock/recovery gate, **not a Docker writable-layer byte quota**. Recovery requires operator inspection or explicit profile deletion; no automatic archive promotion or uncertain-effect replay is implemented. Extra disk-retention/quota policy remains an operational limitation, not proven complete.
- No provider-backed model-quality smoke, native lane007 integrated Docker run, public 001/003 HTTP assembly, or deployment was performed. Arbitrary JS/model-provided completion predicates are not a comprehensive critical-effect classifier or read-only enforcement sandbox. Known values are redacted; this is not a guarantee of detecting every transformed or unstructured secret a model could invent. Source URLs remain in the completion answer; the separate optional `TaskResult.source_urls` field is not extracted/populated by this lane.
- No credentials, real site writes, live infrastructure, other lane production files, shared bootstrap/contracts (apart from the exact approved cherry-pick), Docker/Compose, Harp or branch merges were changed.
