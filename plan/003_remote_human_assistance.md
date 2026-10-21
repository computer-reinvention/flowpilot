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
