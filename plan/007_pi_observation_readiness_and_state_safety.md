# 007 — Pi observation, readiness and state safety

## Outcome

Make Pi's browser state and action-safety path explicit and behaviorally verified. Reusing browser-use's Browser object is not the same as inheriting its Agent orchestration, element matching or loop detection. Correctly distinguish structural element identity, page readiness, action freshness and lack of progress.

Prerequisite: completed 000 only. Develop against observation/browser/control doubles and real standalone Chromium fixtures. No task API, remote assistance, UI or packaged service implementation is required to start.

## Current evidence — not completion claims

- `backend/pi_browser.py:observe` calls `get_browser_state_summary(..., cached=False)` but discards its element representation, then separately evaluates `_DUMP_ELEMENTS_JS`. The model-visible numbered controls are not hash-bound browser-use action references.
- Pi action dispatch serializes calls and tracks URL/focused target; it cannot detect a replaced/reordered control on the same URL/target. It does not interrupt statements inside one arbitrary JavaScript reflex.
- In the installed browser-use source, `DOMWatchdog.on_BrowserStateRequestEvent` checks pending requests and sleeps 0.3 seconds if any are present. Its log wording says stability complete, but it does not establish DOM convergence. Check the locked/image dependency too rather than assuming all environments use the local installation.
- Browser-use Agent code has exact/stable hash matching for history replay and other orchestration. Pi executes custom JS/reflexes and does not invoke that replay mechanism. Hash data existing in a dependency is not proof Pi consumes it.
- `tests/test_pi_browser.py` currently passes five mocked tests covering fresh-state invocation, URL/target guards, completion and callbacks. These do not prove real dynamic-page readiness, same-URL freshness, element rematching or lack-of-progress handling.

## Ownership

Own `backend/observation.py`, semantic changes in `backend/pi_browser.py`, observation-related `backend/runner.py` edits and new `tests/test_observation_*.py` / `scripts/smoke_observation.py`. 000 adds shared port definitions/injection scaffolding first. 002 owns REPL/runtime/sandbox/profile code and calls the observation port; do not edit those files here. Shared lockfile updates require foundation coordination, not independent dependency upgrades.

## Implementation slices

1. Produce one immutable observation from a coherent snapshot: URL, target/document identity, relevant DOM representation, snapshot/generation ID and bounded structural fingerprint. The model-visible controls and any element references must come from that snapshot, not a second unrelated DOM evaluation. Reuse dependency facilities where they meet the contract; document executable semantics instead of promising unspecified browser-use parity.
2. Separate structural/stable element fingerprints from snapshot freshness and result/progress signals. Filter volatile attributes/timestamps only with tested rules; element identity alone must not hide meaningful enabled/disabled, form-value, price, result or permission changes. Avoid treating raw full-page HTML equality as readiness. No credential values or raw sensitive DOM in hash telemetry or persisted snapshots by default.
3. Implement bounded readiness using meaningful DOM/interaction stability observations, minimum observation windows and a hard deadline. Pending relevant requests may contribute but perpetual analytics/WebSockets must not block indefinitely. Delayed same-URL updates must be captured. Return explicit ready/timeout/unstable/unknown outcomes; timeout never silently becomes proof of stable state or successful action.
4. Enforce snapshot freshness before Pi mutating calls and between code-mode-dispatched calls. URL, target, document replacement or meaningful same-page state changes invalidate stale action references/queues and require re-observation. Define which state changes are expected consequences of an action and which require a new plan. Do not silently remap a stale index to a different element.
5. If offering element-targeted actions, bind references to snapshot IDs and verify target identity at use. Exact/stable fingerprint rematching must reject ambiguity rather than pick the first match. Custom JS/reflex selectors remain a separate execution path: do not claim per-statement element-identity protection for arbitrary scripts. Require explicit script preconditions/postconditions and bounded execution; guard entry/exit and stop dependent queued calls on unknown state.
6. Apply the same observation/readiness semantics to Pi exploration, completion and repair, not only `observe`. Completion must use an actual checked postcondition; document that model-generated verification JS is not a sandbox-enforced read-only authority boundary. Avoid races where snapshot acquisition and later control listing describe different documents.
7. Add bounded repeated-action/no-progress detection using task-local action signatures plus relevant state/progress evidence. Repeated observation alone and deliberate waits are not failures. On a proven loop, stop repeating potential side effects, request a fresh observation/repair/help or return a typed failure according to 000. Respect cancellation and human wait budgets rather than spending the entire turn limit retrying.
8. Expose safe metrics for observation time, readiness timeout, invalidated action, rematch ambiguity and no-progress recovery. Keep element credentials, raw DOM dumps, script inputs and browser-help tokens out of traces; audit existing `runner.trace(...)` calls because they currently accept full scripts/inputs. Redact at the call sites owned here and request any shared trace changes through foundation coordination.

## Verification

- Real Chromium synthetic pages with delayed hydration, element replacement/reordering at the same URL, navigation/document replacement, disabled-to-enabled controls, frames, duplicate labels and constant background network traffic.
- Prove stale action prevention with distinct click/effect counters; the wrong replacement control must never receive the queued action.
- Demonstrate readiness waits for delayed meaningful changes, completes despite irrelevant perpetual traffic, and reports a hard timeout for never-ready content.
- Distinguish structural identity from changed values/results; benign timestamp churn must not create endless invalidations, and ignored fields must not hide changed task evidence.
- Execute multiple Pi code-mode calls with a same-URL mutation between them; the next stale action is rejected. Verify/reflex/repair paths exercise the same policy.
- Repeated no-op actions trigger bounded recovery without automatically repeating a completed form submission. Human waits and cancellation terminate cleanly.
- Golden hashes are not hard-coded brittle DOM fixtures: verify behavioral invariants and ambiguity rejection. Compare installed and packaged dependency behavior and record exact versions.

## Handoff / definition of done

Export 000's observation policy factory with deterministic doubles and real-browser evidence. Existing Pi tests remain green, but completion additionally requires the dynamic-page scenarios above. Report remaining custom-JS/per-statement and third-party-site uncertainty honestly. No deployment or provider-account mutations are authorized.

## Lane implementation evidence (incremental)

Checkpoint 1: immutable policy factory and coherent shared Observation/ElementReference models; keyed structural/progress/freshness digests; replacement-instance/document detection; bounded convergence/deadline/unknown outcomes; duplicate identity rejection; task-local progress guard. Pi dispatch checks same-page freshness across serialized code-mode calls and requires explicit execute pre/postconditions. Real local Browser objects receive the policy by default; historical duck-typed clients without injection keep the compatibility path. Runner entry/exit and repair observations use the injected policy, preserve the original snapshot across model latency, and no longer trace raw scripts/inputs/DOM/repair prompts in the owned call sites.

- `/Users/pgwork/Desktop/code/flowpilot/.venv/bin/python -m pytest tests/test_observation_policy.py tests/test_pi_browser.py -q`: 18 passed.
- Installed browser-use 0.13.11 matches `uv.lock`; Dockerfile.backend uses `uv sync --frozen --no-dev`. No image build/version assertion yet. Installed DOM watchdog lines 286–289 use the fixed pending-request 0.3-second sleep, not DOM convergence. Pi does not call Agent replay/history matching.
- Chromium behavioral verification remains pending at this checkpoint. Readiness is finite-window evidence (minimum twice the configured window, then a stable window), not prediction of arbitrary future updates. Cross-origin frames, shadow trees and oversized captures fail closed as unknown. No dependency or shared-contract edits.
