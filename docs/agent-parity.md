# Agent primitive parity

## Contract and change surface

The application runner already supplies the same `human_help` callback to Pi and
browser-use. It pauses RunControl, publishes run.paused through the existing
webhook transport, and resumes through the authenticated run API. Exploration
passes its original stage/input/confirmation/note callbacks directly to Pi.
Those paths remain the source of truth for application runs.

The conversational wrapper bypassed those transports. Its replacement must expose
the same pause event vocabulary, CDP and resume URLs at request time, and wait for
explicit resolution in the same browser. A local assistance transport is needed
because the wrapper owns a native headless browser rather than an application
sandbox. Requiring a dashboard account would change the local identity contract.

Implementation: shared pause-event constructor; ephemeral loopback assistance
server with per-request capability links; same-browser visual interaction;
structured input and confirmation tools; replay human boundaries; cancellation
and stale-link invalidation. Programmatic consumers can receive the event directly.
Local capability links grant access to this request, not other user profiles.

Verification: original run/exploration regression suite, Pi tool dispatch,
unauthorized/stale/repeated resume requests, cancellation, confirmation denial,
input delivery, real headless DOM intervention and persistent profile isolation.
Browser interaction must occur while the agent is blocked. Resume must not replay
already-completed side effects. No promise of production-site success follows from
fixture testing. Local links are usable on this machine; remote delivery remains
the application webhook/CDP-proxy path.

## Primitive map

| Original primitive | Pi application | Conversational wrapper |
| --- | --- | --- |
| Human help / auth / CAPTCHA | Original RunControl + webhook + resume API | Agent-triggered event + capability link + resume API |
| Real input requests | Original exploration callback | Chat question/callback; values returned to agent and replay inputs |
| Final-action confirmation | Original exploration callback | Explicit yes/no in chat/callback |
| Stage discovery, input declaration, notes, compilation | Original exploration callbacks passed through | Remains an application exploration workflow |
| Save/run reusable scripts | Existing runner callbacks | User/site-scoped persistent scripts |
| Script tags and pause-after-auth | Now exposed in Pi schema | Stored with additive SQLite migration; pause honored on replay |
| NEEDS_AUTH / NEEDS_HUMAN | Existing runner handoff | Same handoff, no automatic replay of partial stage |
| Cancellation | Existing task cancellation and Pi subprocess cleanup | Cancel link cancels owning ask, not merely tool call |
| Session persistence | Existing profile/session capture | Persistent isolated Chromium context |

The wrapper is a conversational test harness, not a replacement for the public
flow CRUD, stage-tree or deployment APIs. Pi continues to use their original
callbacks when invoked through application exploration/run paths.

## Evidence

`scripts/smoke_handoff.py` ran with the real Codex-backed Pi agent and two headless
browsers. Pi detected the fixture's OTP gate and called human_help. A simulated
human opened the delivered link, entered the OTP through the page's screenshot
controls, submitted once, and clicked Resume. Pi inspected the resulting state
and verified completion. The submit effect count was exactly one, the link
returned HTTP 410 afterward, and the assistance page had no JavaScript errors.
Observed wall time: 21.64s (fixture only; not a production-site benchmark).

Follow-up correction: the earlier handoff test explicitly requested browser help;
it did not establish independent choice of assistance mode. `scripts/smoke_chat_otp.py`
instead asks only to visit the fixture and complete verification. With real Pi
inference, the agent selected request_input_values, received a synthetic code from
a simulated conversational user, entered it, and completed verification exactly
once. Observed: one chat question, zero browser handoffs, 26.76s. The code was not
persisted in input history or included in the final answer. This verifies routing
on a fixture, not real SMS delivery or a production login.

## Remote-control fidelity

The original assistance viewer polled screenshots and forwarded only complete
clicks, explicit text entry and a few buttons. It did not transmit pointer motion
or dragging. The upgraded viewer uses CDP screencast frames over WebSocket and
ordered input delivery, forwarding sampled real pointer motion, button-down/up,
wheel and key-down/up events. Focus loss and completed handoffs release held keys
and buttons. Screenshot fallback remains available if streaming disconnects.

`scripts/smoke_remote_control.py` verifies Chromium and WebKit viewers against a
real headless Chromium target: direct typing, modifier keys, dragging, iframe
checkbox interaction, scrolling, pause/resume and expired-link rejection. The
target receives trusted browser input events. This does not imply an anti-bot
service considers the browser human. WebKit coverage is not a test of the user's
particular Safari installation.

The inspected Blinkit session showed a Cloudflare access-denied page. The target
browser identifies as headless Chromium; Safari only hosts the viewer. No evidence
establishes Safari as the source of the denial, and the site's precise rejection
reason is unavailable. Improved input fidelity does not establish Blinkit access.

## Virtual display restored

Chat now defaults to the original Docker image's full Chromium on Xvfb, not a
native headless Chromium process. Only its CDP port is published on loopback;
application-created sandboxes still have no host port publication. There are no
shared profile mounts. The native backend remains explicitly selectable for tests.

Per-user Linux profiles are restored before container startup and atomically
archived after an explicit Browser.close. A real restart check exposed lost recent
local-storage writes when relying on Supervisor SIGTERM alone; explicit browser
shutdown fixed the reproduction. Chat containers disable Chromium autorestart
for this intentional close; application sandboxes retain the previous setting.
Failed profile saves preserve the stopped container for recovery.

Real Docker checks passed for same-user cookie/localStorage restart persistence,
other-user isolation, and live CDP control from Chromium and WebKit viewers.
Previous macOS browser profiles are retained separately rather than being copied
into Linux. Existing conversation and reusable-script databases are retained.
# Pi/REPL runtime wiring — 19 October 2026

Pi observations now request fresh browser-use browser state, invoking DOMWatchdog
stability checks before reading controls. Tool dispatch is serialized and compares
URL and focused target before/after actions and against the last observation.
Navigation or a changed target requires a fresh observation before another action;
this also guards calls composed inside code mode. It does not interrupt individual
JavaScript statements within a reflex or detect every same-URL DOM mutation.

REPL reflex failures now invoke the runner's bounded `_repair_reflex` pipeline
with the fast-role model, preserving the original user/site key and pause/tags
when persisting a successful repair. Authentication/human barriers bypass repair.
Repair itself requests fresh stable browser state. Metrics distinguish initial
replay failures, repair invocations and successful repairs.

Flow-tree replay planning remains on the application flow runner; free-form REPL
sessions have no persisted exploration tree and continue model-selected reflex
orchestration. This is not a claim of complete browser-use agent parity.
