# 000 — Shared contracts and integration foundation

## Outcome and execution order

Make Flowpilot a standalone service: a person signs up/logs in, creates an API key, and uses the conversational browser API from any client. Flowpilot owns account authentication, browser execution, isolated profiles, script memory and task state. Harp is one possible API consumer, not a privileged broker or a required implementation target.

Implement and verify this workstream before starting any other file in this folder. After it merges, 001–007 can start simultaneously. Their numbers identify workstreams, not execution order. No time estimates apply.

| Plan | Exclusive responsibility | Depends on |
| --- | --- | --- |
| 000 | Contracts, settings vocabulary, extension seams, composition skeleton and fixtures | None |
| 001 | Authenticated HTTP control plane, SQLite persistence and admission | 000 only |
| 002 | Browser executor, agent adaptation and lifecycle recovery | 000 only |
| 003 | Remote human-assistance transport and viewer | 000 only |
| 004 | Account/API-key experience, standalone client examples and owner UI | 000 only |
| 005 | Build/container hardening, configuration validation and operational readiness | 000 only |
| 006 | Independent contract/security/system verification harness | 000 only |
| 007 | Pi observation, state fingerprints, readiness and stale-state safety | 000 only |

006 starts with contract doubles and later runs against the assembled implementations. This is a final verification prerequisite, not a dependency preventing its development. Each implementation owner fixes failures in their own files.

## Scope boundary

Deliver a locally verified, deployment-ready single-host service usable with ordinary account-owned API keys. Support multiple isolated accounts and independently isolated profiles within each account, bounded concurrent browsers, sequential conversational follow-ups, persistent profiles/scripts, questions, confirmations and browser intervention. Start with a single backend process and SQLite; do not add a distributed scheduler or promise horizontal scaling.

Exclude MCP, Harp-specific authentication/tools/configuration, shared/team website credentials, migration of external browser-provider profiles, a dashboard redesign and arbitrary externally registered callbacks. API-key ownership is the security boundary: keys for the same account have the same account authority, not independent user identities. A future multi-user client must use separate Flowpilot accounts/keys per security principal; an untrusted user/profile header cannot create tenant isolation.

Deployment itself is excluded: no Droplet provisioning, DNS/firewall changes, certificate issuance, remote installation, deployment/rollback execution, live Slack messages or staging/production mutations. Building and checking artifacts, local reverse-proxy rehearsal and writing a manual handoff are included.

## Evidence and blast radius

- `backend/repl.py:BrowserSession` contains conversational context, per-user script memory and interaction callbacks. It currently constructs the local browser and local assistance server itself.
- `backend/repl_sandbox.py:ChatSandbox` publishes CDP to host loopback. That is not a usable backend-container address.
- `backend/handoff.py:HandoffServer` binds a random loopback port and has expiring pause capabilities. Reuse its interaction semantics without exposing it publicly unchanged.
- `backend/main.py` has flow-oriented APIs, background tasks and lifecycle reconciliation; retain these paths and their existing authentication.
- `backend/sandbox.py` owns container creation and profile restoration; its 256 PID limit differs from the capacity benchmark's 1024.
- `docker-compose.yml` already persists profiles/database and separates Docker control traffic; sandbox-to-backend reachability still requires explicit threat analysis.
- `backend/auth.py`, account/key routes in `backend/main.py`, and frontend auth/API-key hooks already provide login and bcrypt-hashed account-owned keys. Reuse and harden those, rather than inventing broker assertions.
- `backend/pi_browser.py` currently requests fresh browser state but returns a separate DOM dump and guards only URL/target changes. The installed browser-use DOM watchdog's pending-request delay is not a convergence detector. 007 owns closing these observation gaps.
- Local REPL identities are filesystem namespaces, not remote authentication. Never use an agent-supplied username as authorization.

Must preserve existing flow APIs, account/API-key behavior, local `./chat`, profile isolation, replay/repair semantics and bounded Pi capabilities. No changes to `../harp` are in scope.

## Freeze the wire contract

Implement typed schemas and machine-readable protocol fixtures, not just this prose. Version the new surface under `/api/v1/browser-tasks`; leave old `/api/v1/runs` unchanged.

1. Reuse `Authorization: Bearer fp_live_...` and the existing authenticated dashboard session. Resolve the immutable account ID server-side using the existing authentication dependency; enforce account ownership on every object. Keep browser-help capabilities a separate narrowly scoped credential. Do not introduce broker JWTs, trust client user/workspace headers, or store plaintext API keys. Revocation rejects subsequent requests and viewer mints; do not implicitly cancel an already admitted task when its key is revoked. Every later control operation still requires current authorization.
2. `POST /api/v1/browser-tasks`: bounded `task`, `inputs`, optional owned `session_id` or `profile_id`, required idempotency key. Return 202 only after durable admission, with stable task/session/profile IDs and an owned status URL. New sessions without a profile get a fresh server-generated private profile. Reusing a session/profile is explicit; reject incompatible profile/session pairs. Same account/key/payload returns the original admission; changed payload returns conflict. Include all behavior-affecting fields in its fingerprint. Define owned profile list/create/delete contracts, including rejection of deletion while leased and consistent removal of associated script/history data.
3. `GET /api/v1/browser-tasks/{id}`: status, revision, safe progress summary, pending interaction metadata, answer/source URLs/metrics, timestamps and typed error. Support bounded wait on a revision; polling is the authoritative first-version transport. No credentials, raw CDP or assistance capability in ordinary task results.
4. `POST .../{id}/cancel`: idempotent cancel intent; cancellation is terminal only after executor fencing/cleanup. A disconnected HTTP caller never implies cancellation.
5. `POST .../{id}/interactions/{interaction_id}/respond`: typed values, explicit approve/decline or browser-resolved response; bind interaction ID, generation and response idempotency key. Reject stale, foreign, expired and conflicting responses. A rejected confirmation must not authorize the proposed action.
6. `POST .../{id}/assistance`: account-authenticated, current-pause-only minting of a short-lived browser capability. Freeze the viewer, screenshot, input, stream, resume and cancel paths consumed by 003/004, including browser Origin behavior through a reverse proxy. Capabilities bind task, account, pause generation and expiry; store only token digests. A trusted API client may explicitly request a link to deliver privately to the owner; no API key is embedded in it, and ordinary status/results/logs never contain it. The dashboard must check the signed-in account before minting. Treat assistance links as bearer secrets, not as permanent authenticated account sessions.
7. Freeze a consistent error envelope and codes for unauthenticated, not found/foreign ownership, invalid input, conflict/stale generation, capacity exhausted, shutdown, interrupted execution, profile recovery required and provider failure. Fix retryability and retry-after behavior. Never return raw provider exceptions.

Use protocol examples for every request/response, including duplicate admission, a question, a declined confirmation, assistance expiry, cancellation and interrupted execution. Enforce body, field, text, wait and result bounds. Secrets entered in interaction responses must not be persisted in task history, logs or traces; pending response values can be transient and are lost on process restart.

## Freeze state, concurrency and failure semantics

- Task states: `queued`, `running`, `waiting_for_input`, `waiting_for_confirmation`, `waiting_for_browser`, `cancelling`, `completed`, `failed`, `cancelled`, `interrupted`. Define allowed transitions and terminal immutability in executable types/rules.
- Session/profile ownership is immutable. One nonterminal task per session and one browser writer/profile lease per account-owned profile; no implicit task queue behind a busy session. Different fresh profiles, including within one account, have separate cookies, storage, script memory and history and may run concurrently within configured global/account caps. API keys do not select profiles implicitly.
- Admission reserves bounded capacity transactionally, including paused browsers. Reject overload before spawning. Define idle-session, task, human-wait and cancellation deadlines in 000's typed settings. A terminal browser may remain available for follow-up only within idle/capacity policy; evict by gracefully saving the profile.
- Persist admission, revisions, interaction descriptors and terminal outcomes. A startup reconciliation marks previously active execution interrupted, revokes interactions/capabilities, fences stale owners and cleans up or quarantines associated containers. Do not automatically rerun a browser task after a crash.
- HTTP admission/response retries must not duplicate execution. This does not promise exactly-once third-party website effects: uncertain partial effects are reported as interrupted/unknown, and a fresh user request must inspect current website state before acting.
- Browser continuation resumes the existing live task; follow-up requests are distinct tasks attached to the owned session. After restart, continuing a session restores durable profile/context, not a suspended Python stack.
- Define what constitutes critical-action confirmation and how an explicit user decision reaches the executor. Existing sign-in/approval authority must not be reduced to an instruction in the model prompt. Do not claim arbitrary website actions can be made exactly-once or comprehensively classified by this integration.
- Polling is the first delivery mechanism. A caller restart can reattach using the same API key and task ID; consumer-specific conversation/job orchestration is outside this service. Do not introduce webhook infrastructure in these workstreams.

## Implement shared seams and assign files

Own new `backend/task_contracts.py`, `backend/task_ports.py`, `backend/task_settings.py`, `backend/task_bootstrap.py`, package scaffolding under `backend/tasks/`, `tests/fixtures/browser_tasks/`, and `tests/test_task_contracts.py`. Own the narrowly scoped registration/lifespan edits in `backend/main.py`. Own initial dependency/lock changes required by all workstreams; later dependency changes require coordination rather than concurrent lockfile edits.

Publish typed ports for task repository/control service, executor, browser handle/lease, interaction channel, assistance access and observation policy. Freeze method signatures, async/cancellation behavior, callback ordering, generation checks, lifecycle startup/shutdown and sanitized logging fields. The repository must support atomic transitions rather than read-then-write state updates. The observation port defines immutable snapshot IDs, same-snapshot element references, readiness/deadline outcomes, structural fingerprint purpose and pre-action freshness checks; this lets 002 and 007 implement independently without editing each other's files. 000 adds only the injection scaffolding in existing REPL/Pi/runner files; later semantic changes follow the ownership map.

Create the fixed composition factory referencing the implementation exports reserved below, using fail-closed placeholders until those exports exist. Add a feature switch disabled by default; missing components fail readiness and cannot accept tasks. Reserve concrete filenames/exports and test injection points so later owners need not edit `main.py` or bootstrap:

- 001: `backend/tasks/control.py`, `repository.py`, `routes.py`, `identity.py`, `schema.py`.
- 002: `backend/tasks/executor.py`, `runtime.py`, `browser_adapter.py`, `memory.py`; extraction edits in `backend/repl.py`, `repl_sandbox.py`, `sandbox.py`.
- 003: `backend/tasks/assistance.py`, `interaction_transport.py`, `assistance_page.py`; extraction edits in `backend/handoff.py`.
- 004: frontend auth/API-key/task/assistance owner experience, API usage documentation/examples; no backend production files.
- 005: Dockerfiles, Compose, `deploy`, `.env.example`, `.dockerignore`, operational scripts/docs.
- 006: dedicated new acceptance/contract/security verification files only; no production fixes.
- 007: `backend/observation.py`, `backend/pi_browser.py`, observation-related `backend/runner.py` edits and dedicated observation tests. 002 owns runtime/profile/REPL semantics and uses the observation port.

003 owns local handoff extraction; 002 must use the frozen injection seam rather than editing `handoff.py`. 000 introduces that seam and a local default so `./chat` remains usable before parallel work starts. 001 exposes the control-service port; 002 uses it via callbacks rather than importing SQLite internals. 003 submits decisions through the same service as API responses, not a second independent state machine.

All follow-on files own their dedicated tests. No two streams edit the same existing test file; use new workstream-specific files. Avoid broad formatting. Interface changes require an explicit foundation amendment and notification to every affected owner; do not silently adapt one side.

### Commit policy

Commit incrementally at logical, verified checkpoints, not only at the end of a lane. Each parallel implementation agent must commit its work frequently in its own branch/worktree, stage only its intended files, and include verification evidence and outstanding limitations in its handoff. Never commit credentials, profiles, traces or unrelated changes. Coordinate shared-contract changes with the integrator before editing them; do not silently fork the protocol.

### Approved additive port amendment — assistance integration

Lane003 identified two missing internal seams. The integrator owns this additive amendment; public v1 task/assistance JSON is unchanged:

- `TaskRepository.assistance_lease(principal, task_id, request: AssistanceRequest) -> Lease` atomically authorizes the current owned browser pause and returns its execution lease. Task state, interaction ID/generation/expiry and execution generation must be checked together. `TaskControl.assistance_lease` delegates this authority. Capability persistence rechecks the same binding at commit to fence cancel/resolve races. Execution generation remains absent from ordinary `TaskView`; it must never be guessed from pause generation.
- `BrowserHandle.metadata() -> BrowserMetadata` supplies the current page URL and viewport dimensions. `AssistanceState` reuses these exact fields plus the current interaction; no made-up URL or cached cross-task viewport is acceptable.

Affected owners: 001 implements authorized lookup/persistence checks, 002 supplies browser metadata, 003 wires public mint/state, and 006 verifies real-service expiry/race/ownership behavior. Peer doubles inherit these methods through autospec. Apply the integrator's amendment commit before continuing those lanes; do not independently edit the shared contracts.

## Foundation completion gate

- [x] Wire schemas, transitions, errors, settings and all port signatures are executable and versioned.
- [x] Golden fixtures exercise both happy and failure paths and contain no real user data.
- [x] Every workstream can instantiate its own implementation against supplied doubles without another stream's implementation.
- [x] Composition mounts the new surface once, disabled/fail-closed by default; existing APIs and local chat retain behavior.
- [x] File ownership, identity policy, assistance access policy and restart semantics have no unresolved product decisions.
- [x] Focused contract tests and existing relevant offline tests pass; commit/reference the foundation revision for all parallel branches.

### Implemented foundation and handoff evidence

Protocol version: `1`. Base Git revision: `851c75b9c7dec4c4d22e4d3bf9fd73677d104268`; foundation changes were verified in the working tree and must be committed before branching parallel work. Foundation content reference: `sha256:7aeeae6f21f7a5881b232c1a1ec6314f43aed518e82ded7d0cba9e77f9685656`. Branch from the committed foundation, not only from the base revision.

- Schemas/state/error/endpoint inventory: `backend/task_contracts.py`.
- Port signatures, atomicity, approval/lease semantics and lifecycle ordering: `backend/task_ports.py` and `backend/tasks/__init__.py`.
- Defaults/configuration: `backend/task_settings.py`. Environment names are `FLOWPILOT_TASKS_<FIELD_NAME_IN_UPPERCASE>`; `PROFILES_DIR` supplies the state root unless explicitly overridden. No new dependencies or lock changes are required.
- Fixed export map: `backend/task_bootstrap.py:COMPONENTS`. Implement the reserved synchronous factories; each lifecycle component binds before executor → assistance → control startup. Control startup reconciles interrupted records before admission. Shutdown closes admission before control → assistance → executor cleanup. Observation is a pure injected policy, not a separate lifecycle service.
- Wire, viewer and security/ownership policy examples: `tests/fixtures/browser_tasks/{exchanges,viewer_commands,policy}.json`. The policy fixes same-account API-key authority, explicit profile reuse, no crash replay, Origin/capability handling and keyed response-receipt equality without storing OTP values.
- Strict configurable peer doubles: `tests/fixtures/browser_tasks/doubles.py`. Unconfigured calls raise; no peer implementation, Docker, database or model is needed.
- Local REPL injection seams: keyword-only handoff/sandbox/browser factories and observation policy; defaults retain existing behavior. Pi execution/completion and runner replay/repair accept the observation policy but 007's detection algorithms are not implemented here.
- Readiness: `/health/browser-tasks`. `FLOWPILOT_TASKS_ENABLED` defaults false; disabled task requests return protocol 404. Enabled but incomplete composition returns protocol 503/Retry-After and readiness 503. `/health` and legacy API errors are unchanged. Admission at the exact API prefix does not redirect POST bodies.

Verified locally:

- `.venv/bin/python -m pytest tests/test_task_contracts.py -q`: **53 passed**, including isolated invocation without earlier test modules.
- `.venv/bin/python -m pytest -q`: **170 passed, 12 explicit live skips**.
- `npm --prefix agent-host test`: **4 passed**.
- `npm --prefix frontend run build`: passed; existing >500 kB chunk-size warning remains.
- Python compile checks, tracked diff whitespace check and new foundation-file whitespace scan: passed.

These checks verify the foundation and preservation of existing offline behavior. They do not establish real hosted task execution, Pi hashing/readiness algorithms, remote assistance security, Docker network isolation or deployment readiness; those remain in their assigned workstreams. No deployment, live provider call or Harp change was performed.

## Overall pre-deployment acceptance

After all streams land, run 006 against the assembled revision and 005's artifacts. Require signup/login/key creation, a standalone API-key client completing a task, explicit isolated profiles, follow-up, private profile persistence, scripts/replay, Pi state/readiness guards, question/confirmation, private browser help, isolation, overload, cancellation, restart fencing, backup/restore rehearsal and safe configuration checks. Record actual evidence and unresolved limitations. Deployment remains a separate manual activity, not a hidden final workstream.
