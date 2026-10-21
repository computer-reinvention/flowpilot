# 003 — Remote human assistance

## Outcome

An authorized person can resolve the currently paused task through a remote browser viewport, then resume or cancel exactly that interaction. No public CDP, desktop/VNC server, permanent browser link or model-visible capability is introduced.

Prerequisite: completed 000 only. Use fake control-service and browser handles. No dependency on the runtime or Harp implementation to build and verify this slice.

## Ownership

Own `backend/tasks/{assistance,interaction_transport,assistance_page}.py`, extraction changes in `backend/handoff.py`, and new `tests/test_task_assistance_*.py`. Preserve the local handoff API. Do not edit executor/REPL, task repository, main/bootstrap, frontend dashboard, packaging or Harp UI.

## Implementation slices

1. Extract reusable screencast/input/viewer behavior from the local handoff implementation behind 000's browser handle port. The hosted transport is mounted by the existing backend composition; no random externally exposed ports or loopback URLs in remote responses.
2. Implement 000's current-pause-only capability mint/validation. Bind principal, task, interaction generation and expiry; persist hashes/metadata through the control-service port. Keep capabilities out of normal status/results, traces and access logs. Restrict endpoints to the authorized task browser, never caller-provided CDP hosts.
3. Implement HTTPS/reverse-proxy-safe same-origin viewer, state, screenshot, bounded input and WebSocket stream paths. Set no-store/referrer protections and a restrictive content policy. Validate Origin/Host behavior, prevent cross-site input submission and ensure stream authorization is checked after connect and during its lifetime.
4. Support existing pointer down/up/move, drag, scroll, keyboard down/up, text insertion and screenshot fallback. Use viewport-correct coordinates. Restrict command kinds and payload sizes; no generic CDP evaluation endpoint, filesystem access or native dialog claim.
5. Resolve questions/confirmations/browser intervention through the same interaction service used by 001. Resume waits for in-flight input, releases held controls and revokes access. Decline does not resume with approval. Concurrent viewer/API decisions have one authoritative resolution.
6. Revoke viewer/stream/input on resume, cancel, timeout, restart and pause-generation changes. A link cannot control a later pause of the same task. Disconnected viewers do not silently resume the agent; dropped keys/buttons are released.
7. Provide the authenticated minting contract consumed by the Flowpilot dashboard and any trusted API client. Dashboard minting rechecks the signed-in account; API clients must deliberately request and privately deliver the expiring bearer link. No API key appears in the link, no arbitrary identity header changes ownership and ordinary task status does not expose capabilities. Communicate the bearer-link security model honestly; a copied live capability can be used until expiry/revocation.

## Verification

- Fake browser protocol checks plus real Chromium control on a synthetic drag/type/scroll page.
- Expired, forged, foreign, superseded and reused capabilities fail for every endpoint, including already-connected streams.
- Adversarial Origin, unsafe Host/forwarded headers, oversized inputs and attempted arbitrary CDP commands are rejected.
- Race a human click against resume/cancel; no input arrives after agent writing is released. Test held controls on blur/disconnect.
- Run locally behind a TLS reverse proxy; links and WebSockets use the correct external origin without leaking internal container names.
- Secret canaries in text entry/OTP fields do not appear in logs, durable history or error bodies.
- Existing local handoff tests retain their semantics; add stream-specific and stale-generation coverage in dedicated files.

## Handoff / definition of done

Export the router/transport factories fixed in 000, a viewer contract double and reverse-proxy smoke evidence. Flowpilot dashboard UI remains owned by 004; deployment and public link distribution are excluded.

## Lane 003 implementation evidence (2026-10-21)

Branch/worktree: `implementation/lane-003`, `/Users/pgwork/Desktop/code/flowpilot-worktrees/lane-003`, foundation `0ccc06a`. No applicable `AGENTS.md` was present. Implementation edits are limited to lane-owned production files, dedicated tests and this evidence section. The integrator explicitly authorized cherry-picking shared amendment `2201b5a`, applied here as `b58585b`; no other shared files were independently edited.

### Delivered, independently verified

- `assistance.create_assistance(settings)` and `assistance.create_router(bindings)` match bootstrap's fixed exports; routes are relative to its existing mount.
- Browser transport accepts typed commands only, provides binary JPEG streams and CSS-pixel viewport PNG fallback, bounds the frame queue, releases held controls and permanently fences a released writer. Coordinates are checked against the current viewport. Existing local handoff reuses held-control cleanup without changing its routes/events/decisions.
- Hosted routes recheck persisted digest, account-owned current task, execution lease generation, interaction generation and expiry on each operation and during idle/active streams. Input and viewer decisions serialize; all local writers for the lease drain/release before the shared control service receives respond/cancel. Control service remains the sole decision authority. Expiry/revocation watchers fence cached writers without resuming the task.
- Same-external-origin POST/WebSocket checks reject missing/null/foreign origins, unsafe Host syntax and spoofed forwarded claims. Only normalized ASGI scheme/Host is used. Viewer uses relative HTTP paths and external-origin WSS, nonce CSP, no-store/no-referrer, no HTML insertion of task text, no token embedded in the page body and no generic CDP endpoint.
- Strict bounded input and sanitized provider errors prevent input/OTP canaries from reaching error bodies or application error logs. Public `mint` obtains `TaskControl.assistance_lease`, preserves its execution generation independently of the pause generation, and persists only a SHA-256 digest through `save_capability` with its required atomic recheck. Issuance uses 32 random bytes, min(TTL, pause expiry), and revoke-before-remint. `/state` uses `BrowserHandle.metadata()` and returns the unchanged frozen `AssistanceState` JSON.
- Independent strict peer doubles are configured from the shared fixtures. Synthetic Chromium tests exercise real drag/type/scroll, held-key release, post-release fencing, CSS-pixel screenshots at device scale factor 2, browser metadata, JPEG streaming, and actual viewer controls through a loopback TLS reverse proxy. The proxy test now starts from a successful public service mint, proves real external WSS binary JPEG delivery, PNG fallback after stream disconnect, secret insertion, same-origin paths and successful viewer resume through shared control. It uses a short-lived self-signed test certificate and disables access logging; no external site, infrastructure or provider is involved.
- Dedicated mounted-public-route tests use the real assistance router and an explicitly test-only authenticated mint-route peer (001 owns production account authentication/routes). They exercise the canonical `/api/v1/browser-tasks/...` URLs, bearer-link use without account credentials, public state/input/screenshot/respond, reuse rejection, remint rotation/draining, missing/foreign account credentials, ignored identity headers, stale/foreign interaction IDs, expired/nonwaiting tasks, and atomic-save races against resolution/cancellation/new pause/new execution/expiry. Strict double save repeats the new atomic checks rather than accepting stale bindings.

### Actual checks

- Root interpreter, cwd lane worktree: `python -m pytest tests/test_task_assistance_mint.py tests/test_task_assistance_security.py tests/test_task_assistance_transport.py tests/test_task_assistance_proxy.py tests/test_handoff.py tests/test_task_contracts.py -q`: **106 passed**.
- Final full `python -m pytest -q`: **219 passed, 12 explicit live skips**. The earlier **197 passed, 12 skipped, 1 failed** run had a missing local `@earendil-works/pi-agent-core` dependency, not an assistance regression. At the integrator's explicit direction, `npm ci --prefix agent-host` installed this worktree's ignored dependencies (92 packages, zero audit vulnerabilities); no root files or package/lock files changed. The full suite is now green without credential copying or model calls.
- `npm --prefix agent-host test`: **4 passed**.
- No deployment, real provider credential, external site write, independent shared-contract change, other-lane edit or merge was performed. The only shared changes are the authorized amendment cherry-pick.
- Python compile checks for all changed production/test modules and `git diff --check`: passed.

### Resolved foundation gaps

1. **Execution-lease lookup — resolved.** The initial frozen `TaskView` lacked execution lease generation. Amendment `2201b5a` adds the exact account-bound `assistance_lease(principal, task_id, request) -> Lease` port and atomic save recheck. `7c62b9a` removes the deliberate 503 placeholder and uses that verified lease. Tests independently use execution generation **7** and pause generation **3**, and reject a change to execution generation **8** even when pause metadata is unchanged.
2. **Browser metadata — resolved.** The initial handle lacked URL/viewport metadata required by `/state`. Amendment `2201b5a` supplies `BrowserMetadata`/`metadata()` without changing public JSON. `7c62b9a` implements the method on this lane's real `BrowserViewport`, consumes it in `/state`, and removes the deliberate 503 placeholder. Synthetic target pages use intercepted HTTPS URLs; no fabricated production metadata or CDP endpoint is returned.

### Remaining integration responsibilities and verification boundary

1. **Deployment boundaries remain with parent/005.** Trusted TLS proxy configuration must normalize Host/scheme and restrict accepted external Hosts. Arbitrary forwarded headers are not trusted here. Server/proxy access logs and tracing must redact the secret-bearing help path; handler code alone cannot prevent upstream logging. Set WebSocket server receive limit to the frozen 65,536-byte bound; this stream accepts no incoming commands and closes on any message, but ASGI receives already assembled messages. The rehearsal is a synthetic loopback proxy, not evidence for deployed proxy/logging configuration.
2. **External API response races require the executor's port guarantee.** `BrowserHandle.release` drains/fences this adapter, and viewer responses invoke it before waking control. API responses/cancellation must also await the executor's human-writer release before restarting agent writes, as required by frozen `BrowserHandle`/control semantics. A new browser acquisition after release must supply a fresh human-writer handle, not reuse this permanently fenced adapter. Parent/006 must test the assembled 001/002 control/executor implementations, real account authentication and process-restart reconciliation; lane003's strict peers deliberately do not replace that acceptance work.

Lane-owned implementation and local verification are complete against the approved ports; no remaining lane003 contract blocker is known. Verdict: **conditionally verified** for assembled service use, pending the named cross-lane/deployment checks above. Real production-account authentication, assembled restart fencing and deployed log redaction are not claimed verified by these isolated tests.

### Ordered commits

1. `71d205d` — fenced viewport transport and Chromium input verification.
2. `f92ea6e` — same-origin capability transport and serialized decisions.
3. `1a0e2fc` — TLS viewer rehearsal and secret-safe browser failures.
4. `7cc242d` — lease-wide drain and initial blocker evidence.
5. `b58585b` — authorized cherry-pick of integrator amendment `2201b5a` only.
6. `7c62b9a` — enabled public mint/state via approved ports; real viewer resume.
7. `6e1fd88` — mounted public-route ownership checks and atomic-save race coverage.
