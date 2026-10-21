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

Branch/worktree: `implementation/lane-003`, `/Users/pgwork/Desktop/code/flowpilot-worktrees/lane-003`, foundation `0ccc06a`. No applicable `AGENTS.md` was present. Only lane-owned production files, dedicated tests and this evidence section were changed.

### Delivered, independently verified

- `assistance.create_assistance(settings)` and `assistance.create_router(bindings)` match bootstrap's fixed exports; routes are relative to its existing mount.
- Browser transport accepts typed commands only, provides binary JPEG streams and CSS-pixel viewport PNG fallback, bounds the frame queue, releases held controls and permanently fences a released writer. Coordinates are checked against the current viewport. Existing local handoff reuses held-control cleanup without changing its routes/events/decisions.
- Hosted routes recheck persisted digest, account-owned current task, interaction generation and expiry on each operation and during idle/active streams. Input and viewer decisions serialize; all local writers for the lease drain/release before the shared control service receives respond/cancel. Control service remains the sole decision authority. Expiry/revocation watchers fence cached writers without resuming the task.
- Same-external-origin POST/WebSocket checks reject missing/null/foreign origins, unsafe Host syntax and spoofed forwarded claims. Only normalized ASGI scheme/Host is used. Viewer uses relative HTTP paths and external-origin WSS, nonce CSP, no-store/no-referrer, no HTML insertion of task text, no token embedded in the page body and no generic CDP endpoint.
- Strict bounded input and sanitized provider errors prevent input/OTP canaries from reaching error bodies or application error logs. Private issuance primitive uses 32 random bytes, SHA-256 persistence only, min(TTL, pause expiry), and revoke-before-remint; public mint remains deliberately blocked as described below.
- Independent strict peer doubles are configured from the shared fixtures. Synthetic Chromium tests exercise real drag/type/scroll, held-key release, post-release fencing, CSS-pixel screenshots at device scale factor 2, JPEG streaming, and actual viewer controls through a loopback TLS reverse proxy. The proxy test proves real external WSS binary JPEG delivery, PNG fallback after stream disconnect, secret insertion, same-origin paths and cancellation. It uses a short-lived self-signed test certificate and disables access logging; no external site, infrastructure or provider is involved.

### Actual checks

- Root interpreter, cwd lane worktree: `python -m pytest tests/test_task_assistance_security.py tests/test_task_assistance_transport.py tests/test_task_assistance_proxy.py tests/test_handoff.py tests/test_task_contracts.py -q`: **88 passed**.
- Earlier full `python -m pytest -q`: **197 passed, 12 skipped, 1 failed**. The unrelated `tests/test_pi_agent.py::test_real_pi_host_rejects_unknown_model_without_provider_call` expected a model-catalog error but got `Pi exited without a result`. Direct `node agent-host/host.mjs` confirms missing worktree dependency `@earendil-works/pi-agent-core` (`ERR_MODULE_NOT_FOUND`); this lane did not install dependencies or edit packaging. The three additional stream-generation/expiry cases were added after that full run.
- No deployment, real provider credential, external write, shared-contract change, other-lane edit or merge was performed.
- Python compile checks for all changed production/test modules and `git diff --check`: passed.

### Blocking foundation gaps — not a completed lane

1. **Mint has no authorized execution-lease lookup.** `TaskControl.get` returns `TaskView`, which contains session/profile IDs and interaction generation but not execution lease generation. `AssistanceBinding` and `executor.browser` require a real `Lease`. An interaction generation is not an execution lease generation and cannot safely substitute for it. Parent must amend the foundation with an account-bound, current-pause lease lookup, for example `TaskControl.assistance_lease(principal, task_id, request) -> Lease`, implemented by 001 and added to shared doubles. Public `mint` checks owned/current browser pause then returns protocol **503 unavailable**, without guessing a lease or saving a capability. `_issue` is an explicitly private, tested primitive ready for the verified lookup; its tests do not claim public mint works.
2. **Frozen `/state` cannot be populated.** `AssistanceState` requires URL and viewport width/height. `BrowserHandle` supplies only screenshot/frames/send_input/release, not URL/viewport metadata. Parent must amend the typed browser port and shared doubles (002 implements metadata). `/state` authorizes then returns protocol **503 unavailable**, never a made-up URL, raw CDP endpoint or incompatible response. Viewer honestly reports unavailable interaction metadata and disables resume; screenshot/stream/input/cancel remain testable with persisted fixture capabilities.
3. **Deployment boundaries remain with parent/005.** Trusted TLS proxy configuration must normalize Host/scheme and restrict accepted external Hosts. Arbitrary forwarded headers are not trusted here. Server/proxy access logs and tracing must redact the secret-bearing help path; handler code alone cannot prevent upstream logging. Set WebSocket server receive limit to the frozen 65,536-byte bound; this stream accepts no incoming commands and closes on any message, but ASGI receives already assembled messages. The rehearsal is a synthetic loopback proxy, not evidence for deployed proxy/logging configuration.
4. **External API response races require the executor's port guarantee.** `BrowserHandle.release` drains/fences this adapter, and viewer responses invoke it before waking control. API responses/cancellation must also await the executor's human-writer release before restarting agent writes, as required by frozen `BrowserHandle`/control semantics. This lane tests independent doubles and its own real browser adapter, not the absent 001/002 implementations.

These gaps were reported to the parent; no incompatible shared methods, fabricated metadata or shared-contract edits were introduced. Public mint/state, account-authenticated end-to-end minting, assembled restart fencing and deployed log redaction are **not claimed complete**.
