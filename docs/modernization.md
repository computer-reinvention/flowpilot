# Modernization work log

## Acceptance audit

### Follow-up: subscription and conversational harness

The credential blocker below is historical: the user subsequently completed Codex
device login. Pi subscription inference passed inside Compose. A real Chromium
demo then completed a synthetic booking, saved its generated reflex, replayed it
with different inputs without another model call, and repaired a changed selector
using typed generation over the same subscription. See scripts/smoke_subscription.py.
This does not establish production Blinkit/X behavior or the full exploration API.

The `./chat` REPL wraps the existing Pi browser driver. Each local identity has a
locked persistent Chromium profile and SQLite conversation/reflex memory. Native
Playwright owns the profile so browser-use cannot silently copy it to a temporary
directory. A live restart/isolation check verified cookies and local storage survive
for Alice and are absent for Bob. Unit coverage verifies user/site script scoping,
same-user lock exclusion, failure recording and reuse metrics.

A real subscription-backed read-only website trial took 32.39s/8 model turns on
first ask and 19.18s/5 turns on a follow-up using the saved script. This is one
observed example, not a general benchmark. Metrics include model turns, tool calls,
reuse/save/failure counts, agent time and separate human wait time. Current gates:
105 backend passes, 12 explicit live skips; 4 Node/WASM/auth tests pass.

| Requirement | Evidence | Status |
|---|---|---|
| Local app | Both images build, Compose starts, UI/health 200, login renders without page errors | Verified |
| Explore/replay | Flow-scoped migration, tree planning, real two-stage Chromium smoke | Replay verified; AI exploration pending |
| HITL | Real smoke pauses, changes DOM, resumes at stage two with first-stage count exactly one; API regression tests | Replay verified; provider-driven boundary pending |
| Repair/fallback | Typed programs/postconditions, prerequisite failure tests, Pi/browser-use adapters | Contract tests pass; live AI pending |
| Programmatic tools | Pi QuickJS/WASM tests: composition, serialization, capability exclusion, timeout/cancel | Mechanism verified; model usage pending |
| Jev | Optional classifier with confidence/category validation, bounded requests and fallback tests | Contracts pass; external service pending |
| Prompts/models | Typed output, system/data separation, observable checks, centralized roles, Pi catalog IDs | Configuration verified; inference pending |
| Dependencies | Python lock refresh, Vite 8 production build, pinned Pi and sandbox Playwright | Builds pass; 7 shadcn transitive audit findings remain |
| Sandboxing | Default seccomp/no-new-privileges, isolated profile restore, zero mounts, proxy network probe fails | Local verified; optional gVisor not tested |
| Docs/checks | README/.env.example; 99 backend passes/12 live skips, 3 Node passes, diff whitespace check clean | Offline gates pass |

Completion is **not established**. Presence-only checks show no Google, Anthropic,
OpenAI or TypeSafe credentials in local .env. A supported model must still explore
a representative flow, save generated stages, replay them, exercise repair/fallback
and its human boundary. Deterministic scripts and mocked models do not establish
this behavior. Add one primary provider key locally and recreate Compose before
live verification. Optional Jev service verification needs a TypeSafe key as well.

## Scope and acceptance

Modernize the local application against developments over the nine months leading
to October 16, 2026. Preserve API-first explore/replay and human pause/resume.
Verify local startup, browser execution, complete multi-stage replay, repair and
fallback, and human intervention. Review dependencies, models, prompts, tool
calling, sandboxing, Jev, and programmatic tool calling. Completion requires
working integration evidence, not just a dependency bump.

## Baseline (October 16, 2026)

- Revision: `dbcaa82` (March 6, 2026).
- `uv sync --frozen` installed the existing lock on Python 3.13.15.
- `npm ci` succeeded; audit reported 28 vulnerabilities (2 low, 4 moderate,
  21 high, 1 critical). Dependency-specific assessment remains pending.
- `npm run build` passed.
- `docker compose config --quiet` passed. Docker Desktop started; initial image
  build launched, result pending.
- `FLOWPILOT_SKIP_E2E=1 uv run pytest -q`: 22 passed, 11 failed, 15 errors,
  8 skipped. Two live E2E modules ignore the skip switch; live agent tests require
  credentials. Flow/HITL fixtures use removed `description` instead of `task`;
  cancellation/reconnection fixtures patch removed `seed_default_flows`.
- Local `.env` has localhost settings and a generated JWT secret, but no LLM key.
  Real provider verification remains pending credentials.

## Implementation observations to investigate and cover

- `runner.py` returns COMPLETED after the first successful reflex, including a
  repaired reflex, rather than completing the documented ordered stage sequence.
- Reflex lookup is domain-wide and appends reflexes outside the flow's tree.
  Determine appropriate flow ownership/scoping before changing persistence.
- `_get_llm()` executes before cached replay, requiring a provider key even for
  the advertised zero-LLM path.
- Reflex execution has no explicit wall-clock deadline. Any changed URL can turn
  an evaluation exception into success, even if the complete action did not finish.
- Sandbox containers mount the entire profiles directory read/write. Chromium
  uses `--no-sandbox`; Docker uses `seccomp=unconfined`. Profile restoration happens
  after container startup, potentially racing Chromium. Verify boundaries and
  platform compatibility before changing isolation options.
- Sandbox image installs both system and Playwright Chromium plus an unpinned
  browser-use installation. Backend also installs a local Chromium despite
  connecting to a remote browser. Inspect actual requirements before simplifying.

## Research sources and preliminary fit

### Jev

Source: https://typesafe.ai/blog/introducing-system-one-models-and-jev
(retrieved October 16, 2026).

TypeSafe describes Jev as an early-access decision model emitting typed outputs
and probabilities rather than generated strings. Potential fit: choosing a
stage, classifying a blocker, or judging a page state. It cannot replace script
generation. Vendor speed, calibration, and reliability claims require local
evaluation; schema correctness does not establish semantic correctness. API,
access requirements, and integration choice are still under investigation.

### Programmatic tool calling

Source: https://platform.claude.com/docs/en/agents-and-tools/tool-use/programmatic-tool-calling
(retrieved October 16, 2026).

Claude's hosted implementation uses code execution and `allowed_callers`, returns
tool calls to the client, and requires container continuity on pending calls.
Intermediate tool results can be filtered before entering model context. Pending
calls have short timeouts, which matters for long human pauses. The documented
built-in browser/computer toolsets are direct-only; custom tools are distinct.

FlowPilot already amortizes model calls through JavaScript reflexes. Evaluate
bounded composition of browser operations and compact results while retaining
the existing provider-independent runtime and explicit HITL boundary. Hosted
PTC integration is not selected or implemented yet.

### Browser-use

Source: https://github.com/browser-use/browser-use (current documentation via
Context7, October 16, 2026).

Current docs support typed `ainvoke(..., output_format=Model)` completions and
Agent `output_model_schema`. Custom Tools actions can receive the injected
`browser_session: BrowserSession`. Evaluate typed generated scripts and explicit
execution outcomes instead of parsing markdown-fenced strings.

## Remaining work

### Baseline runtime and harness follow-up

Both Docker images built and `docker compose up -d` started the backend and socket
proxy. The local native backend also started with explicit PROFILES_DIR and
FRONTEND_DIR; `/health` and `/` returned HTTP 200. It was stopped before starting
Compose. Playwright loaded the Compose frontend: HTTP 200, title FlowPilot,
login form rendered, zero page errors. The desktop browser connector was offline.

Updated stale fixtures to the actual task/export_storage_state/profile_id
contracts. Isolated auto-exploration in CRUD/HITL fixtures, restored leaked mocks,
and synchronized lifecycle completion so session assertions do not race the
separate end-of-run capture. Existing offline suite: 44 passed; 12 live tests
are explicitly opt-in with `--live`. Provider-role adapter tests: 23 passed.
These are baseline/adapter checks, not proof of real AI execution.

## Runtime implementation design

### Agent-driver decision (user steering)

The user explicitly authorized replacing the driving agent with Pi or OpenCode.
The existing agent loop is browser-use, not a wholly home-grown loop. Reviewed
OpenCode V2 embedded SDK (`https://opencode.ai/v2/docs/build/sdk`) and Pi's current
agent-core and codemode documentation:
`https://github.com/earendil-works/pi/tree/main/packages/agent` and
`https://github.com/earendil-works/pi/tree/main/packages/codemode`.

Select Pi's embedded agent-core for an additional driver, with a narrow subprocess
bridge to FlowPilot's browser tools and no host shell/filesystem tools. Pi's
QuickJS/WASM codemode is an actual programmatic tool-calling sandbox, with only
injected tools, bounded execution/memory, and filtered results. Browser mutation
calls must remain serialized, even if model-written code uses Promise.all.
Human-help calls stay outside timed codemode batches so human pauses do not
expire. Cancellation must terminate child execution and reject pending calls.
Retain browser-use as an explicit compatibility driver until live comparative
checks establish the Pi path. The OpenCode SDK is capable but brings workspace/
coding host services that aren't needed for a browser-only embedded driver.
This choice supersedes the narrower composition-only plan in item 4 below;
implement real Pi codemode composition rather than just a batch helper.

### Implementation progress

Pi now has run and exploration adapters, typed capability schemas, a subprocess
transport, serialized browser tools, and real QuickJS/WASM code composition.
`agent-host` tests exercise actual WASM timeout/cancellation and host-capability
exclusion. Python bridge tests exercise actual Node subprocess cleanup and
unknown-tool rejection. Browser completion requires an observed postcondition.
Docker backend now includes Node and the pinned Pi packages.

Optional Jev failure classification is wired into replay handoff. It is disabled
unless JEV_ENABLED=true and TYPESAFE_API_KEY is provided. Responses must have a
known category and finite confidence >=0.9; service errors/uncertainty fall back
to the normal agent path. Observations are capped at 12 KB, requests at 5 seconds.
Enabling it sends failure observations to TypeSafe; no classification is made
on successful replay. Live TypeSafe behavior remains unverified without a key.

Sandbox creation no longer mounts all profiles. Only the selected local profile
is archived into the stopped container, before startup. Default Docker seccomp
and no-new-privileges replace unconfined seccomp. SANDBOX_RUNTIME optionally
selects an installed OCI runtime. Actual Chromium compatibility is pending the
image rebuild/live check; no claim of local gVisor availability is made.

Frontend migrated from Vite 5 to Vite 8.3.4/plugin-react 6.1.2 (Oxc/Rolldown),
with compatible dependency refresh. Production build passes. npm audit dropped
from 28 findings to 7 high findings, all through shadcn CLI's fast-glob/braces
dependency chain; current registry offers no nonbreaking fix. The CSS import
still uses shadcn, so it was not blindly removed or downgraded to v1.
Latest combined backend gate: 99 passed, 12 explicitly live-gated skips.

Live sandbox verification found two startup issues: Supervisor's backslash
continuations embedded literal newlines in Chromium arguments, and Chromium's
first-run flow prevented CDP readiness. A single-line command plus --no-first-run
and --no-default-browser-check corrected startup. Actual Docker container returned
Chrome/156.0.8078.4 at /json/version with default seccomp and
no-new-privileges:true. Container inspection also exposed an obsolete image
VOLUME /profiles declaration (anonymous volume); removed it from the Dockerfile.
Latest backend/frontend image built and Compose services recreated successfully.

`scripts/smoke_replay.py` now passes against real Docker Chromium and the runner:
two persisted stages execute sequentially, pause_after triggers HITL, a simulated
human changes the live DOM, and the second stage verifies that change and the
first-stage side-effect count remains exactly one. Completion is emitted only
after both stages. Uses a temporary SQLite DB and removes its sandbox.
This also exposed Chromium rejecting Docker hostnames in WebSocket Host headers;
runner/explorer now resolve container DNS to an IP for the WebSocket connection.
The smoke script passed after copying the updated runner into the running backend;
final image rebuild is still required for that connection fix.

The rebuilt image subsequently passed the same replay/HITL smoke without copied
source files. Local Playwright verified HTTP 200, FlowPilot title, visible Log in
button, /health 200 and no page errors on the rebuilt frontend. All six configured
default model IDs resolve in Pi's installed provider catalog (not a live inference
test). Provider key presence check in .env: Google, Anthropic, OpenAI and TypeSafe
all absent. AI exploration/repair and live Jev remain unverified.

Packaging follow-up removes unused browser-use and duplicate system Chromium from
the sandbox, pins Playwright 1.64.0 to match the backend lock, excludes nested
node_modules/dist from build context, and separates Docker control proxy onto an
internal control network. Backend joins control and sandbox networks; browser
containers join only the sandbox network. Both images subsequently rebuilt and
Compose recreated successfully; real replay/HITL smoke passed again. New sandbox
inspection confirms zero mounts and no-new-privileges. A direct bounded HTTP probe
from the sandbox to the Docker proxy control-network IP failed, confirming network
isolation. The probe used the local Docker client: proxy exec-start is disabled
and was not enabled for diagnostics.

Flow-scoped reflex migration preserves old IDs in a legacy scope. Explicit tree
references can read legacy scripts; unrelated scripts aren't appended. The fast
path completes the whole sequence and initializes models lazily. A controller is
registered before replay, supporting pause_after, manual pause and cancellation.
Unrepaired prerequisite failures stop replay and hand off current state.
Typed BrowserProgram generation composes script + read-only postcondition.
CDP execution rejects explicit failures, preserves data, bounds execution and
requests termination on timeout/cancellation; a URL change no longer masks errors.
`uv run pytest -q --tb=short`: 83 passed, 12 live tests skipped. Real replay/browser
execution and provider checks remain required; the running image predates these
latest runtime edits and must be rebuilt before final verification.

1. **Flow-bound replay:** Add flow ownership to reflex persistence with a migration
   that retains legacy data. New explorer/runner writes are scoped to a flow.
   Replay only the selected flow's ordered stages; legacy domain-only reflexes
   require explicit tree references. Never append unrelated domain scripts.
   Missing stages or unrepaired failures stop the sequence and hand current state
   to the agent rather than executing dependent stages or claiming completion.
2. **Execution outcomes:** Bound CDP execution time and output size. Preserve
   extracted results. Explicit failed outcomes must fail even if JS did not throw.
   Navigation alone cannot prove the remaining script ran. Generated scripts
   receive a typed contract with observed completion checks and explicit blocker
   signals. Keep existing plain-script storage readable during migration.
3. **HITL everywhere:** Register a run controller before the fast path. Manual
   pause, cancel, auth signals and `pause_after` must apply during replay, not just
   the agent loop. Resume continues at the correct stage and captures the same
   session/profile state through the existing API callback contract. Agent handoff
   must not repeat already completed side effects.
4. **Programmatic tools:** Extend bounded browser-side code execution with typed
   generation and compact outputs; expose ordered reflex composition to the
   agent, stopping on errors and human boundaries. This adopts PTC's low-roundtrip
   and result-filtering benefits without a mandatory hosted Python container.
   Claude hosted PTC is not the default: its pending-call expiry conflicts with
   arbitrarily long human intervention and adds a provider-specific runtime.
5. **Prompting:** Separate stable system rules from JSON-encoded page/task data,
   mark page content as observations rather than instructions, state concrete
   success criteria, use structured output rather than markdown stripping,
   parameterize inputs instead of embedding one user's values in saved code,
   and prefer observable waits over unconditional sleeps. Keep prompts concise;
   do not request chain-of-thought. Source:
   https://developers.openai.com/api/docs/guides/reasoning-best-practices
   (guidance includes older examples; use current model catalog for IDs).
6. **Jev:** Optional typed blocker classifier on error/handoff. Use the documented
   REST endpoint, bounded observations, validation, timeout, and confidence
   threshold. Low confidence/unavailable service falls back to existing agent
   reasoning. Do not treat a classifier as a permission or authentication check,
   or add calls to successful cached replay. Test payload and malformed/error/
   uncertain responses, and live-test if credentials are available.
7. **Sandbox:** Remove shared profile mounts from browser containers; restore only
   the selected profile before Chromium starts. Keep per-run resource limits,
   unexposed CDP, and isolated data. Use version-pinned Chromium and remove unused
   agent packages/browser duplication from images. Test default seccomp on this
   ARM host; make any necessary exception explicit rather than silently relaxing
   isolation. Offer configurable OCI runtime for hosts with gVisor installed;
   do not pretend Docker Desktop has runsc. Sources:
   https://playwright.dev/python/docs/docker,
   https://docs.docker.com/engine/security/seccomp/,
   https://gvisor.dev/docs/. gVisor adds userspace-kernel isolation but changes
   syscall compatibility/performance; a managed microVM service adds credentials,
   hosting costs and network/session changes without solving a demonstrated local
   requirement, so is not selected as a mandatory backend.
8. **Verification:** Focused tests for ordered replay, wrong-flow exclusion,
   complete/failed/timeout outcomes, partial-stage repair, cancellation and
   pause/resume; real browser tests on deterministic local forms plus AI-driven
   explore/replay with a configured provider; UI signup/flow/run checks; image
   build and real sandbox create/CDP/stop/profile roundtrip. Upgrade frontend
   tooling to current compatible releases and run build/typecheck/audit.

## First implementation wave: dependencies and provider configuration

Use the current browser-use release line (0.13), refresh the lock, and remove
unused direct LangChain dependencies (no imports in backend). Keep Playwright for
local verification and the current image until sandbox changes are validated.
Centralize model role selection (agent/exploration/fast) with explicit per-role
overrides for all providers; preserve existing environment variable names.
Avoid obsolete model-specific sampling assumptions. Constructor/serialization
tests must cover current providers before claiming compatibility; real model
calls remain a separate credential-dependent gate.

Official model catalogs retrieved October 16, 2026:

- https://ai.google.dev/gemini-api/docs/models — `gemini-4-argon` and
  `gemini-3.8-flash` are stable. Select Argon for exploration/agent and Flash for
  short code generation/extraction, with overrides available.
- https://developers.openai.com/api/docs/models — `gpt-6-astra`,
  `gpt-6.1-sol`, `gpt-6-luna`. Select Sol for agent/exploration and Luna for fast
  calls; Astra is configurable for demanding flows.
- https://platform.claude.com/docs/en/models/overview — `claude-sonnet-5-5`,
  `claude-haiku-5-5`, `claude-opus-5-5`, `claude-fable-5-1`. Select Sonnet and
  Haiku by role, leaving larger models configurable. Check current sampling
  constraints against the adapter before using these defaults.

Jev's documented REST API is `POST https://api.typesafe.ai/v1/systemone`, with
Bearer `TYPESAFE_API_KEY`, state, model, and typed questions. Responses contain
`answers`, choice probabilities and confidence. Proposed integration is opt-in
blocker classification with bounded page observations and conservative fallback,
not an added paid call on every warm reflex. See
https://docs.typesafe.ai/introduction/quickstart.

The authoritative phase/task plan is persisted with the OpenCode goal. Baseline
runtime verification, broader current-technology research, upgrade design,
implementation, and final end-to-end verification are all still in progress or
pending. No modernization completion is claimed by this log.
