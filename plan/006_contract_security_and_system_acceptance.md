# 006 — Contract, security and system acceptance

## Outcome

Independently verify that the assembled change delivers a standalone account/API-key browser service and is ready for a manual deployment decision. Passing mocks, static checks or image builds alone is not sufficient.

Prerequisite to begin: completed 000 only. Start with golden fixtures and fake peers while all other streams implement. Running final integrated acceptance requires their assembled artifacts, but harness development must not wait for them.

## Ownership and independence

Own new `tests/test_task_acceptance_*.py`, `tests/task_acceptance_support/`, `scripts/verify_task_system.py`, `scripts/verify_task_security.py` and sanitized outputs under `reports/task-acceptance/`. Use dedicated files, not concurrent edits to other streams' tests or contracts. No Harp changes or verification are required.

Do not repair production code or weaken acceptance criteria. Report reproducible failures to the responsible owner; rerun after their fix. Harness doubles must not import implementation internals that make producer and consumer share the same bug.

## Parallel build slices

1. Parse 000's machine-readable fixtures and build independent protocol consumer/provider checks. Check HTTP status codes, response fields, error retryability and state transitions, not prose fragments.
2. Build a deterministic local website with login/cookie/local-storage fixtures, question/confirmation boundaries, multi-step/cart-like mutations, counters, selector changes, navigation, drag/input and cross-origin frames. No real purchases, account writes or external customer data.
3. Build fault injection at admission commit/dispatch, browser mutation/result commit, interaction resolution, stream input/resume, profile save, model subprocess exit and ordinary API-client disconnect/restart boundaries.
4. Prepare temporary SQLite, disposable Docker networks/containers and a runner for 004's standalone client examples. Every resource has a unique run label/directory; cleanup targets only that run. Keep Docker-required, provider-backed and offline proof separately selectable.
5. Produce machine-readable evidence with revisions/image IDs, configuration excluding secrets, scenario outcome, observable counters, safe trace references and proof limitations. A skipped or unavailable check is not a pass.

## Acceptance matrix

| Boundary | Required proof |
| --- | --- |
| Onboarding/API use | Sign up/log in, create key and use 004's ordinary HTTP client against real local control plane; no broker auth or repository imports |
| Conversational continuity | First request plus follow-up use the intended owned session and website state |
| Script reuse/repair | Synthetic counters prove reuse and selector repair without repeating a completed prerequisite; model-backed proof labelled separately |
| Personal isolation | Two accounts cannot read/mutate each other's objects; two profiles of one account independently isolate website state, scripts and history |
| Authentication | Invalid/revoked keys, forged user headers, wrong/expired dashboard JWTs and foreign task/profile IDs fail closed; once-only key disclosure and hashing verified |
| Lost responses/retries | Concurrent duplicate admission creates one durable task and one execution; mismatched reuse conflicts |
| Questions/secrets | Values reach only the current executor; OTP/password canaries absent from DB/history/logs/traces/results |
| Confirmation | Approve, decline, duplicate, expired and wrong-owner responses behave correctly without accidental approval |
| Human browser | Real pointer/keyboard/stream/fallback works through local TLS proxy; resume/cancel expires all access and fences late input |
| Capacity | Busy-session/global caps count paused browsers, reject before spawn and release leases on all terminal paths |
| Cancellation | Browser/agent stop, stale callbacks cannot settle completed, and unresolved stopping is not called cancelled |
| Crash recovery | Kill after an observable website effect; startup reports interrupted/unknown and never automatically replays it |
| Persistence | Graceful session close/reopen preserves private website state; failed save preserves recoverable evidence |
| Client recovery | Fresh HTTP client reattaches after disconnect/restart with owned task/session ID; no duplicate website effects or need for caller process continuity |
| Pi state safety | Real same-URL mutations, delayed DOM updates, volatile pages, stale element references and repeated no-progress actions exercise 007's readiness/hash guards |
| Network boundary | Browser probes cannot access control/backend administrative/private/metadata services under the packaged policy |
| Operations | Builds/config/restart/log bounds/disk handling/backup-restore checks from 005 pass against assembled artifacts |
| Compatibility | Existing flow/run APIs, account/API-key UI, local chat and Pi capabilities retain relevant regressions |

## Final verification procedure

1. Pin assembled Flowpilot revision plus sandbox/backend image IDs. Verify all streams used the same foundation contract revision.
2. Run Flowpilot offline backend tests, Node agent tests, frontend production build and new contract checks. Preserve explicit live-test skips.
3. Run actual Docker/browser/control-plane/assistance scenarios with deterministic inference. Do not substitute a mocked browser for the real topology.
4. Run 004's frontend onboarding and standalone-client examples against the real local service. Verify signup/login/key creation/revocation, separate accounts and separate same-account profiles without importing backend internals. Run 007's real-browser readiness/freshness cases, not just mocked state-summary assertions.
5. With separately supplied authorized model credentials, run bounded synthetic-site model-backed task, follow-up, script and human-boundary cases. If credentials are unavailable, record a named readiness gap rather than reporting live AI success. No real website effects are required.
6. Run restart/failure/ownership/network/secret-canary/backup-restore scenarios and inspect artifacts for leaks.
7. Produce one final report separating passed, failed, skipped and blocked criteria. Attribute failure ownership without changing scope; implementation owners fix and provide new revisions for reruns.

## Completion rule

Pre-deployment readiness requires all required local acceptance checks green and no unresolved security/data-integrity blockers. Explicitly distinguish future hosted TLS, cloud-runtime and real provider acceptance checks: they cannot be established by local evidence and remain manual post-deployment validation. This plan does not provision, deploy, post live messages or claim production readiness from local tests.

## Lane006 independent evidence (partial harness; acceptance not complete)

Foundation branch point: `0ccc06a005c3ff680110de6b66a6606a4e48d3d3`. Worktree: `flowpilot-worktrees/lane-006`, branch `implementation/lane-006`. Only lane006-owned files changed; no production repairs or other-branch integration.

Verified milestones, committed incrementally:

1. `1fdadf2` — independent frozen-wire assertions, strict actual-HTTP peers and standalone stdlib client; 37 tests passed.
2. `bc4312d` — synthetic dual-origin website, durable SQLite effect counters and real Chromium fixture tests; 3 browser tests passed.
3. `e79e923` — black-box service drivers, bounded retries, safe evidence and canary scanner; 54 dedicated tests passed.
4. `8e6ce6e` — actual Uvicorn fail-closed baseline without lifecycle, owned browser SIGKILL, local resource fencing; 58 dedicated tests passed.
5. `473861d` — read-only key-hash verification, retryable-capacity and viewer fixture coverage; **61 dedicated tests passed**. `scripts/verify_task_system.py --offline` exits 0, but records all unresolved real-service criteria as blocked and `readiness: not_established`.

Actual baseline probe: a separately owned local Uvicorn process with lifespan disabled returned readiness **503 / starting**; complete acceptance exits **2**, not success. Lifecycle is deliberately disabled to avoid the pre-existing global Docker orphan sweep. This is fail-closed HTTP boundary proof, not assembled startup/recovery proof.

Repository-wide check at `8e6ce6e`: **227 passed, 12 skipped, 1 failed**. Isolated rerun reproduces `tests/test_pi_agent.py::test_real_pi_host_rejects_unknown_model_without_provider_call`, expected `catalog does not contain`, actual `Pi exited without a result`. Pi/agent owner (002/007) must adjudicate; no root cause or repair claimed.

Exact runnable offline/integrated/security commands, evidence index, limitations, and named remaining requirements are in `reports/task-acceptance/README.md`. Machine evidence pins source revision and frozen fixture hashes, includes no secret bodies/DOM/traces, and distinguishes consumer proof from integrated requirements. Security CLI self-tests correctly return 2 for incomplete clean coverage and 1 for a deliberately injected synthetic leak without leaking the canary in evidence.

Still blocked/unverified: assembled real executor/control/assistance/observation peers; owned service-level commit/dispatch, interaction, stream/resume, profile-save, subprocess and restart fault hooks; real profile/script/history isolation; pause capacity and execution fencing; TLS viewer and packaged network topology; operations/backup/build/image IDs; 004's published example/UI; actual Pi guard behavior; full artifact coverage; provider-backed and hosted validation. Some have HTTP scenario drivers ready, while the remaining fault/topology drivers require parent continuation after assembly. No substitutes or fake service passes are claimed. **Plan006's completion rule remains unmet.**

### Approved amendment follow-up

- At committed checkpoint `673f069`, cherry-picked **only** integrator-approved `2201b5a`, now local `2ff7646`. This is the sole authorized exception to lane006's no-integration rule; no production peer implementations were integrated or repaired.
- `a692441` adds independent public assistance consumers and integrated targets for successful mint/state, current URL/viewport, two pause generations within one uninterrupted task, no public execution epoch, foreign/stale mint rejection, remint and wall-clock expiry, and concurrent mint/resolve/cancel with expired state/late-input access. A deterministic lookup→capability-save commit failpoint remains a named blocked requirement, not a stress-test pass.
- Dedicated harness plus amended foundation checks: **125 passed**. Post-amendment offline runner: **71 passed**, exit 0, still `readiness: not_established`; safe report `reports/task-acceptance/task-acceptance-bd02374d18f24cdbb8bd01cd5eb1ece9/evidence.json` pins `a692441`.
- Final full repository suite at `a692441`: **241 passed, 12 skipped, 1 failed**, the same named Pi-host failure above. No new production failure or successful real-peer assistance execution is claimed. Public integration targets are ready to run after owners001/002/003 assemble the amended seams; actual integrated results remain blocked.

### Authorized assembled run continuation

- Applied only amendment `561383d` as `c06505a`; implemented no-store/same-account-key consumers (`752fde4`) and direct API fence targets (`4f4d3fb`, `59f77a3`). Offline **90 harness checks** passed at `59f77a3`; `2c406de` pins that evidence. Input fault instrumentation cannot treat cancellation as proof a still-outstanding I/O operation drained.
- At an explicitly authorized clean checkpoint merged main **8d6d2f845e5b92e5bdb757631bd06ee3ceee0fb7** as `cf73e2c`, preserving harness work. Production files are changed solely by that authorized merge, never repaired by lane006.
- `8ff1573`, `80c0764`, `708cb58` add the actual owned Docker assembly, real account/task HTTP, real browser profiles/script reuse, private viewer and capability expiry, genuine backend-child SIGKILL/reconciliation, native Pi safety, paused capacity and the published standalone client target. Deterministic inference is injected only at `run_pi`; actual Control/Repository/Executor/Assistance/Observation run unchanged. The only other instrumentation is constructor-owned input/release fault injection, never a public endpoint or fake service.
- Real checkpoint `task-acceptance-c46c6a8e5fe54a618b8b504ed371f24a`: **25 passed observations, zero failures**, full-readiness exit **2**. Direct-API active-stream/held-input drain, wrong generation, failure and timeout all passed against the real services/browser. Backend SIGKILL after an actual website effect reconciles interrupted/no replay, fences old capabilities, preserves duplicate admission and quarantines/removes only the owned recovery profile. Checkpoint source was marked dirty, not misrepresented as a clean pinned revision; a clean final rerun is to be recorded next.
- Explicit test PID limit **1024**; no production default/tag changed. Dedicated backend/browser bridge and run-labelled temporary directory only; existing backend/browser image IDs, all limits, exact commands, failure-history explanations and remaining named gates are in `reports/task-acceptance/README.md`. All generated evidence is sanitized; raw auth/profile/trace/log resources are deleted with the unique run only.
- Full merged local regression at `708cb58`: **450 passed, 12 skipped, 1 failed**, 125.85s. The prior Pi-host error is dependency-conditioned in this worktree; parent reports main/root-installed-dependencies do not reproduce it (405/12 checkpoint). Not a new production regression or a request for production repair. One initial full command timed out at 120s; the complete result used 360s.
- Required readiness remains incomplete: TLS/private network policy/operations/UI/provider/hosted proof and named additional fault/secret/repair variants are not inferred from core scenario success. No deployment/live provider or external website effect occurred.

### Final clean-source assembled evidence

- Exact command (cwd lane006): `/Users/pgwork/Desktop/code/flowpilot/.venv/bin/python scripts/verify_task_system.py --assembled-docker --deterministic-local --require-complete`.
- Report: `reports/task-acceptance/task-acceptance-3fce85f79237496eb484aa04e308f99c/evidence.json`. **27 passed checks, zero failed**, source **`4693718d1a0477f71ad59b8bafde4b3c6d714109`**, clean working tree at execution. Exit **2**, not 0: the full required inventory remains incomplete.
- Includes actual unmodified 004 CLI subprocess profile-create/start/poll/delete and a completed browser task, no repository imports/caller-process continuity. Actual owned network/container cleanup is recorded green. Docker images and PID/memory/time/rate inputs are pinned in machine evidence and README. No provider quality or production deployment claim.
- Actual secret question/decline/cancel/core private assistance passed, but their larger adversarial matrices are not automatically marked fully complete. Remaining exact gate names and failure reproducers/limitations are in the README. No production repair was made, and legacy cleanup was never called. The only full-suite local failure remains the dependency-conditioned Pi-host error already reported as nonreproducing with installed root dependencies by the integrator.
