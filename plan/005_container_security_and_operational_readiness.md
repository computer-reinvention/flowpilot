# 005 — Container security and operational readiness

## Outcome

Produce buildable, bounded and recoverable single-VM artifacts that an operator can deploy manually. Do not provision or deploy anything.

Prerequisite: completed 000 only. Use its settings/readiness contract and a fake task service to develop packaging checks independently of 001–004.

## Ownership

Own `Dockerfile.backend`, `Dockerfile.sandbox`, `docker-compose.yml`, `deploy`, `.env.example`, `.dockerignore`, new `scripts/ops_*` helpers and `docs/hosting-readiness.md`. Do not edit task modules, sandbox Python code, contracts, main/lifespan, Harp or another stream's tests. Request application changes from their owner when a packaging check reveals a defect.

## Implementation slices

1. Build reproducible backend/frontend/agent/sandbox artifacts for the target Linux architecture. Check runtime Node compatibility and dependency/image pins. Inventory relevant dependency vulnerabilities and distinguish accepted risk from fixed findings. Never include `.env`, `.pi-auth`, profiles, traces or local chat state in image layers/build context.
2. Add restart policy, health/readiness checks, graceful shutdown budgets and bounded log rotation. Assert exactly one backend worker/process for SQLite/session ownership; configuration must not imply safe multi-worker scaling. Detect incomplete new feature composition through readiness rather than a misleading health 200.
3. Define a conservative initial active-browser cap and expose 000's resource/timeout/retention knobs in examples. Reuse measured evidence without converting synthetic capacity into production guarantees. 002 owns sandbox resource enforcement; this stream verifies packaged settings reach it.
4. Threat-model sandbox egress and lateral access. Browsers need website access, but must not reach Docker control, backend administrative routes, host/private networks or cloud metadata. A Docker network split alone does not prove this. Implement/rehearse enforceable container/host-policy artifacts and tests, including IPv4/IPv6, redirects and DNS changes. Do not claim URL-string filters or documented firewall intentions are a completed boundary. If root/host rules require manual installation, ship the validated artifact and make its absence an explicit readiness blocker.
5. Keep Docker socket/proxy control private, backend port loopback-only and raw CDP unpublished. Audit socket-proxy permissions and sandbox privileges. Retain container isolation and no-new-privileges; gVisor is optional and must not be advertised as active unless verified. Clarify the remaining risk of Chromium's `--no-sandbox` and shared-host trust.
6. Package reverse-proxy configuration for HTTPS/WebSockets, correct external base URL, trusted forwarded headers, safe assistance access logging and bounded requests/timeouts. Rehearse locally; do not issue real domain certificates. Ensure query/path capabilities and credentials are not written by proxy access logs.
7. Audit `deploy` as an artifact only: avoid overwriting unrelated Caddy configuration, correct captured interactive output, validate non-secret prerequisites and support the chosen model/provider options without destructive defaults. Do not execute its installation/start/stop/cleanup commands against a remote machine. Mock external commands when testing shell behavior.
8. Add consistent SQLite backup plus profile/archive backup tooling with restricted permissions and encryption/key-handling requirements. Back up using SQLite's supported backup mechanism, not a potentially inconsistent live file copy. Rehearse restore into a disposable isolated directory/service, record version/schema compatibility and keep recovery artifacts out of browser-accessible paths.
9. Add bounded retention/cleanup tooling for task history, traces, idle resources and quarantined failed-save containers. Coordinate lease-aware container cleanup with the runtime port; never delete an active profile or unrelated containers. Check disk exhaustion behavior and protect previous known-good archives.
10. Add non-secret configuration validation and safe diagnostics: image/revision identity, database writability, stable account JWT secret presence/permissions, provider authentication presence (not values), Docker/network reachability and optional admission readiness. Support API-key providers and existing subscription configuration without copying local credentials automatically; document unattended refresh limitations and operator-supplied credential requirements. Validate public signup/login/key-management exposure and rate-limit policy; ordinary account credentials are the service boundary, not a Harp broker signing key.

## Verification and manual handoff

- Local builds, Compose validation, secret-canary image inspection and Linux topology smoke.
- Reboot/restart rehearsal in disposable local infrastructure; healthy backend readiness and interrupted-task semantics must agree.
- Real network reachability probes from a browser sandbox, including Docker/backend/metadata targets; never probe sensitive live infrastructure.
- HTTPS/WebSocket viewer rehearsal, log redaction checks, resource exhaustion and graceful stop.
- Backup/restore drill proves database records and isolated browser profile state are recoverable, not merely that an archive file exists.
- Handoff lists artifacts/revisions, required secrets without values, resource assumptions, checks already run and unresolved prerequisites. Document limitations and operator decisions, not an automated deployment workstream.

## Definition of done / exclusions

All local artifact checks are runnable and evidence-backed. Actual Droplet purchase/provisioning, SSH installation, DNS, cloud firewall changes, secret installation, remote rollout, migrations on a live host, live canaries and rollback execution are excluded. Do not run deployment just to prove packaging works.

## Lane005 implementation evidence — 2026-10-21

Branch: `implementation/lane-005`, exclusively in `flowpilot-worktrees/lane-005`,
from foundation `0ccc06a`. No applicable AGENTS.md was present in the worktree or
its ancestor directories. No production Python task/settings/bootstrap/sandbox,
frontend, lockfile, root-checkout or peer-worktree files were modified.

### Incremental commits (ordered)

1. `a0ee364` — pinned runtime/build images, single-process bounds, health/config seam.
2. `89f3f4a` — safe scoped deployment artifact, SQLite/age backup and trace retention.
3. `6eff2d6` — isolated recovery/proxy/network policy rehearsals and operator handoff.
4. `4183cda` — failure-safe backup, traversal, disk exhaustion preflight tests.
5. `fe3ce79` — explicit proxy Host/trust, WS receive limits and protocol log suppression.
6. `efc3d01` — resolved Compose checks and nested secret-context regression closure.
7. `00cc3a5` — reproducible dependency audit/release gates and sandbox revision identity.
8. `c7fc157` — evidence ledger and explicit machine-visible hosted readiness blockers.

### Delivered executable artifacts

- Digest-pinned Linux/amd64 backend/frontend/agent/sandbox builds; exact Node 22.22,
  Python 3.12.12, uv 0.9.5, frozen uv/npm locks. OS apt/browser download remains
  repository-resolved, not bit-for-bit reproducible. Runtime images carry revisions.
- Compose bounded restart/logging/resources, backend non-root, loopback-only backend,
  private non-privileged socket proxy and isolated control network. All foundation
  settings forwarded; default cap **2 global / 1 account**, tracing off. Startup
  probes settings/auth presence, disk/writability and a private Docker API ping.
- `deploy` refuses setup/cleanup, never installs tools/rewrites global Caddy/prunes
  unrelated resources/copies credentials. Tests use only a mocked Docker executable.
- SQLite-supported snapshots + quiesced profile copy, encrypted age archive, manifest
  hashes/schema/revision checks, atomic no-clobber publication, restricted temp/files,
  restore traversal/link rejection into a new isolated directory; no service startup.
- Dry-run-first, explicitly quiesced trace retention; no external lease/profile/DB or
  container deletion. Missing lease-aware retention is a named readiness blocker.
- Caddy candidate normalizes external Host and forwarded scheme, bounds bodies/streams,
  excludes request error/access logs. ASGI server pins WS protocol, **65,536-byte
  assembled receive limit**, queue 16, explicit individual proxy addresses and null
  handlers for secret-bearing Uvicorn/WebSocket protocol logs.
- Reviewable, **uninstalled** nft bridge/host rules plus disposable fixture-only
  namespace probes for public/private/backend/control/host/metadata, IPv6, redirects
  and DNS destination changes. No URL-only/network-split claim is made.
- Dedicated checks under `scripts/ops_*`; operational prerequisites and exact peer-owner
  requests are in `docs/hosting-readiness.md`.

### Verified local evidence

- `python -m unittest discover -s scripts -p ops_tests.py -v`: **12 passed**.
- Foundation `pytest tests/test_task_contracts.py -q`: **53 passed**.
- Resolved Compose validation: pass, including custom sandbox memory/CPU/PIDs reaching
  typed TaskSettings, exact default caps, private proxy, loopback publication, restart,
  65s stop budget and 3x10MiB log bounds. This verifies forwarding, not lane002 enforcement.
- All Linux/amd64 image builds passed: frontend, agent, complete backend and sandbox.
  Build stages were executed sequentially; Node heap capped 1GiB, uv downloads 2/install 1.
  Disposable running fixtures capped CPU/memory/PIDs. No daemon/host config changed.
- Linux Node **v22.22.0**: all **4 agent tests passed**, with network disabled.
- Synthetic secret-canary build and saved-image/layer inspection: pass, including
  nested frontend source-directory `.env` and positive proof legitimate source remains.
  The stronger test initially caught an unsafe directory unignore; fixed before handoff.
- Real age encryption/decryption restore: pass for SQLite records + two distinct
  account profile archives; tamper rejection, wrong revision, checksums, traversal,
  failed-encryption/disk publication and previous-archive protection checked.
- Disposable Caddy HTTP/WS upgrade, 128KB body bound, normalized Host/forwarded headers,
  foreign Host not reaching backend, and capability-free normal/**502 error** logs:
  pass. TLS/certificate issuance was deliberately not exercised.
- Real backend-image ASGI test: exact-size accepted; oversized and fragmented oversized
  messages rejected **1009 before application delivery**; capability absent in protocol
  logs. Also passed in local Python. Uvicorn emits its legacy-WS deprecation warning;
  frozen-version behavior is verified, not assumed for a future alias migration.
- Backend fixture on a unique internal Docker network with fake Docker API, no host
  socket/mounts/ports: disabled liveness, enabled incomplete composition **503**, both
  after restart and graceful exit **0** passed. No real task crash recovery claim.
- Offline real sandbox Chromium/CDP readiness passed, no host CDP publication.
- Bash/sh syntax, Python compilation and Git whitespace checks passed.

### Explicit unresolved readiness gates / manual prerequisites

1. **HOST_NETWORK_POLICY_UNVERIFIED**: isolated `nft --check` parses policy but kernel
   rejects bridge `ct state` with **Protocol error**. No host modules/rules were changed.
   The namespace reachability harness therefore cannot certify isolation on this kernel.
   A target-compatible disposable Linux kernel and independently authorized operator
   installation/persistence/reboot tests remain required. Real assembled sandbox
   probes are still a lane006 gate. Never advertise network isolation as completed.
2. **DEPENDENCY_SECURITY_UNRESOLVED**: Python production frozen export audit found
   **28 advisories / 6 packages**: aiohttp 3.14.3 (8; reported fix 3.14.4), anyio 4.12.1
   (2; 4.14.2), certifi 2026.7.22 (1; 2026.10.19), mcp 2.1.1 (1; 2.2.0), pillow 12.3.0
   (8; 13.0.0), pypdf 6.16.2 (8; 6.19.0). Frontend dev shadcn/braces chain: **7 high**.
   Agent production npm audit: **0 reported**. OS/Chromium inventory unverified.
   No shared lock updates, automatic audit fixes or risk acceptance were performed.
3. **RETENTION_RUNTIME_UNVERIFIED**: 001 must implement terminal-only transactional
   history pruning; 002 must consume resource limits, bound sandbox Docker logs,
   drop unnecessary capabilities, scope broad orphan labels and own idle/quarantine
   retention. No lease-aware external maintenance port exists; request a foundation
   amendment before any external janitor deletes runtime state.
4. **ASSEMBLED_ACCEPTANCE_UNVERIFIED**: real task crash/interruption/fencing, durable
   profile recovery, low-disk save preservation, load/resource enforcement, legacy
   signup/login/API rate limits and real assistance ownership/expiry remain with their
   owners and lane006. Packaging fixtures are not task execution evidence.
5. Manual operator prerequisites: reviewed Linux kernel policy, verified exact proxy
   NAT source and HTTPS/DNS configuration, manually merged safe Caddy logging options,
   uid 10001 state dirs mode0700, owned `.env` mode0600, stable random JWT secret >=32
   bytes, chosen provider key or deliberately supplied private Pi subscription auth
   (refresh limitations documented), offline age identity and off-host backups.

No `deploy setup/up/stop/cleanup` command targeted the actual installation; Docker
stop/restart commands affected only disposable labelled fixtures. No host tools were
installed, certificates issued, live accounts contacted or credentials copied.
Only labelled disposable fixture containers/networks were removed. Reusable uniquely
named `flowpilot-lane005-*:verify` build images remain for integrator rechecks; no shared
cache/daemon/image pruning was performed. Hosted readiness remains intentionally blocked.

Final recheck: **12 ops tests + 53 foundation tests passed**, resolved Compose,
compilation/shell/whitespace checks passed; no lane005 fixture containers or networks
remained. Backend image `sha256:080641dddf90993ebd961b9c315f85679024da98bac842c593b4c2fd90311902`
(revision `c7fc157`) passed real ASGI limits + backend restart/503/graceful stop again.
Sandbox image `sha256:8df29717efd7cae7db0761ae0a5393d88f560d677224caea6d44483db95c849c`
(revision `00cc3a5`) passed offline CDP again. Proxy normal/error logging rehearsal
passed again. One transient shared-Docker extraction snapshot NotFound occurred;
retry succeeded with no daemon repair, reset or shared pruning. The trace-retention
test additionally covers existing `NNNN_function.name.json` traces while preserving
arbitrary JSON/profile files. The reproducible dependency-audit helper returned the
same 28 Python / 7 high frontend / 0 agent findings (expected nonzero audit exit).

## New bounded read-only triage checkpoint — 2026-10-21

Integrator explicitly authorized dependency/implementation triage after accepting
the packaging checkpoint, **not remediation or deployment**. New machine report:
`scripts/ops_dependency_triage.json`; narrative/action table appended to
`docs/hosting-readiness.md`. No `pyproject.toml`, `uv.lock`, frontend manifest/lock,
shared contract or production application file changed. Baseline SHA256 values are
recorded in the machine report and rechecked against the unchanged checkout.

### Actual dependency evidence and disposable proposals

- Enumerated **all 28 Python advisory IDs/aliases/individual fixed versions** across
  six exact locked packages; matched every entry against prior sanitized pip-audit
  output. Verified all recorded dependency paths against `uv.lock` and read installed
  distribution METADATA in the existing non-secret backend image.
- **Important correction:** browser-use 0.13.11 pins aiohttp, AnyIO, MCP, Pillow and
  pypdf exactly; browser-harness 0.1.13 repeats the Pillow pin. A targeted aiohttp
  3.14.4 lock request failed resolution against that pin. An ordinary lock refresh
  cannot silently fix those packages without a coordinated metadata override or
  upstream compatible release. No browser-use upgrade was attempted.
- Certifi is independently unpinned by HTTPX/HTTPcore/Requests: disposable
  `uv lock --upgrade-package certifi==2026.10.19` changed **only certifi**. Proposed
  smallest first shared amendment is **uv.lock only**, not pyproject changes.
- Disposable five-override experiment (aiohttp 3.14.4, AnyIO 4.14.2, MCP 2.2.0,
  Pillow 13.0.0, pypdf 6.19.0) plus certifi resolved with uv 0.9.5: **exactly seven
  changed packages** including mcp-types 2.2.0, none removed, browser-use stayed
  **0.13.11**. Candidate frozen no-dev export pip-audit returned **0 findings**.
  This was resolution/advisory triage, **not runtime compatibility testing**; no
  candidate package replacement occurred in an app environment. Pillow major 13
  and deliberate upstream-pin bypass require explicit owner acceptance and gates.
- Seven frontend highs are **one root braces advisory propagated to seven dev
  package entries**, not seven independent CVEs. All exact versions/parent edges
  are in the report. Npm registry has braces latest **3.0.3**, still affected;
  no published 3.0.4 patch override was established. A breaking shadcn 1.0.0
  downgrade or unreviewed parser alias is not a compatible remediation proposal.
- **Important correction:** CLI JS is unused by app/build commands, but package
  CSS is used at `frontend/src/index.css:3`. Removing shadcn alone in a disposable
  image cleared audit but **failed build** due to the missing CSS import.
- Preserving the exact 16,340-byte shadcn 4.22.0 stylesheet and redirecting its one
  import before removing the CLI in a second disposable image: **0 npm findings,
  build PASS, entire dist byte-identical, all TSX sources byte-identical, runtime
  dependency map unchanged, zero surviving lock versions changed**, 291 unnecessary
  lock entries removed. Proposed coordinated frontend files: package.json,
  package-lock.json, index.css, added shadcn-tailwind.css and verified upstream
  license/attribution file. No such files were changed in the checkout. Licensing
  provenance review and merged-lane004 clean build/UI smoke remain prerequisites.

### Corrected missing-app versus proof-only requirements

Read immutable **Git objects**, not peer checkout files: lane002 `c90556f`, lane001
`efbcd76`; main was `d3301fe` at start. This is evidence at named owner revisions,
not a claim those revisions are all merged/deployed. Exact file/line references
and owner actions are in the machine report.

- **Implemented, assembled proof pending:** all four sandbox resource settings
  reach Docker create; repository global/account reservation caps; terminal browsers
  close eagerly rather than accumulating in a warm-idle pool; profile turns prune
  by configured age on open/write and last 100 on write; failed-save prior-archive
  preservation, byte budget, recovery marker and stopped-container retention;
  task startup recovery scoped by task-host and profile lock. Legacy orphan cleanup
  now skips task-labelled containers. Do not reassign these as wholly missing code.
- **Actual missing implementations:** age-based **control/task DB** terminal history
  retention (distinct from profile turns); explicit **sandbox Docker log_config**
  (Compose rotation does not apply); global quarantine age/count/byte retention;
  bounded in-memory executor `seen` dispatch receipt set. Idle timeout is unused,
  but eager terminal close is a stricter normal-path resource policy, not proof of
  an idle-browser leak. Inactive profile turn stores are not continuously swept.
- **Partial hardening:** task recovery is scoped, but legacy orphan cleanup is still
  broad and explicit unnecessary capability drops remain absent. Cleanup fixes must
  preserve lease/generation checks, idempotency and the last recovery source. No
  generic age-based profile/container janitor was introduced.

### Bounded verification and unchanged blockers

Machine report validation passed: four baseline file hashes unchanged; six Python
locked versions and all graph paths correct; **28 exact advisory IDs/aliases/fixes**
match recorded audit; seven exact frontend entries are dev-only. All experiments
ran in disposable labelled containers with CPU/memory/PID caps, without host mounts,
secret files, Docker socket, provider calls or daemon/firewall changes. Package and
advisory registries were read publicly; auditor installed only into disposable tools.
No candidates were committed to shared manifests/locks. Network bridge conntrack
**Protocol error remains a readiness blocker**; OS/Chromium inventories remain
**unverified**, outside the zero-findings candidate package audit. No release or
hosted-isolation clearance is granted by this triage checkpoint.

## Coordinated dependency implementation — approved after b55c32c

Integrated main `b55c32c` with merge checkpoint `57372a2`. Integrator now exclusively
assigns this lane the shared dependency/CSS files and dedicated compatibility tests.
No other application modules, contracts, root venv, credentials or infrastructure
are mutation targets. Preserve the integrated PID1024 and retention/drain changes.

### Bounded blast-radius / verification contract

- Primary paths: public FastAPI/HTTPX/AnyIO TLS and cancellation; browser-use local
  browser discovery through aiohttp; browser-use screenshot/highlight/GIF/video and
  PDF reader; MCP schemas imported by browser-use but no MCP listener; Pi observation,
  replay/repair/completion; assistance JPEG/input/drain over actual Chromium.
- Transforming seams: exact browser-use/browser-harness dependency pins, uv overrides
  replacing metadata, mcp-types coupling; Tailwind importing shadcn package CSS while
  CLI code is unused. Static CSS has the same user-visible animation/style behavior.
- State/concurrency: no task/profile schema, identity, lease or filesystem semantics
  change is intended. AnyIO/HTTP clients must preserve cancellation, timeouts and
  TLS hostname checking. Parser exhaustion and failed-save behavior must not worsen.
- Siblings: browser-use and Pi drivers; legacy replay/repair and hosted tasks; direct
  screenshot and streamed JPEG; PDF read success and oversized/malformed read paths.
- Environments: independent worktree venv, clean Node22 Linux image builds, target
  Python3.12 backend and existing local Python3.13. Never sync the root venv. Synthetic
  local-only browser/model fixtures; no provider or hosted account calls.
- Existing proof: merged 001/002/003/006/007 suites exercise lifecycle/assistance/Pi;
  existing ops checks cover packaging/proxy. Their doubles do not prove codec/API
  compatibility; add focused actual dependency regressions. Prior disposable resolver
  and audit success is explicitly NOT compatibility proof.
- Known failure probes: ordinary exact-pin patch fails resolution; CLI removal without
  CSS fails build; wrong TLS hostname must fail; cancelled tasks must settle; excessive
  PDF input must stay bounded by actual browser-use read contract; stale/no-progress
  browser actions and completion must remain fenced.
- Unknowns to test before acceptance: Pillow13 actual screenshot/image/GIF/video APIs,
  pypdf6.19 extraction/limits, MCP2.2 schema imports and AnyIO4.14 cancellation/TLS,
  aiohttp3.14.4 discovery behavior. Do not infer unexercised codec/format coverage.

### Ordered checkpoints

1. Certifi only: prove one-package graph delta, frozen own-venv sync, CA bundle/TLS and
   relevant offline checks, then commit independently.
2. Frontend: verify exact 4.22.0 release CSS/license provenance; stop if unresolved.
   Preserve exact CSS, remove unused CLI, clean npm ci/build, runtime graph/component
   and complete dist hash equivalence; run frontend/client/agent tests; commit.
3. Python: exact five overrides plus mcp-types coupling only. Prove exactly documented
   package deltas; focused actual API/browser compatibility, complete offline suites,
   advisory audits and rebuilt backend/ASGI. Commit only if all required compatibility
   passes; otherwise report failure and keep the failing slice out of the accepted
   branch without weakening tests.
4. Record immutable evidence and remaining kernel/network + OS/Chromium gates. Zero
   package advisories is not global security or deployment clearance.

### Certifi checkpoint

Own lane005 `.venv` frozen sync completed (Python 3.13.15; root interpreter used
read-only, root environment never synced). Exact lock graph delta verified: only
certifi 2026.7.22 -> 2026.10.19, no additions/removals. Dedicated CA regression plus
control HTTP and retention suites: **30 passed**. CA regression verifies revoked
emSign ECC Root CA G3 is absent and SSL context retains mandatory chain/hostname
verification with >100 roots. Cryptography warns about a nonpositive serial in a
remaining bundled certificate; no warning filter or trust relaxation added. This
checkpoint does not claim real outgoing TLS exchange coverage yet.

### Frontend checkpoint

Exact npm shadcn 4.22.0 tarball SHA512 matches formerly locked integrity; extracted
`package/LICENSE.md` is MIT, copyright (c) 2023 shadcn. Vendored that license unchanged
beside byte-identical `dist/tailwind.css` (16340 bytes, recorded SHA256 unchanged).
Reproduction helper `scripts/ops_vendor_shadcn.py` verifies archive and CSS hashes,
extracts only the two named members, executes no CLI and preserves release bytes.
Removed only the direct dev CLI and redirected the CSS import; no TSX changes.

Clean Linux/amd64 Node22.22.0 npm ci + build PASS; npm audit **0**. Against actual
merged-source baseline image, **all four dist files byte-identical** (index CSS
`b5f0c360581832fc31989b7a1a7cbd82bee5d945a1e0c35bf6f6251d8fb7e08a`,
JS `5ffec946afcf74716bbe564ef4bc419ace15866c6835c58560e1e76a57668313`).
Runtime dependency map unchanged; **291 lock entries removed, zero surviving version
changes**. npm also made six formerly bundled optional WASI dependency records
explicit under unchanged Tailwind oxide 4.3.3 (`inBundle=true`); these were already
listed in the old package's bundleDependencies, not six new upgrades. Dist identity
and native Linux build are verified; a WASI platform build was not exercised.

Frontend/client Node suite **6 passed**, including real cached Chromium DOM account/
task/viewer flow on synthetic local HTTP fixtures. Agent suite **4 passed** after
clean locked agent npm ci; initial run failed only because own-worktree agent
node_modules was absent, not from a source regression. Two dedicated dependency
regressions pass. License and CSS provenance gate is resolved; no CSS trimming or
component-runtime dependency change. Prior 500kB Vite chunk warning remains.

Distribution follow-up: backend packaging copies the exact MIT notice to
`/usr/share/licenses/shadcn-tailwind/LICENSE` in the runtime image, outside dist,
so minified asset bytes remain identical and the deployed distribution retains its
attribution. Standalone dist redistributors must likewise include the source notice.

### Python candidate compatibility evidence (acceptance pending final image gates)

Applied only the approved five exact overrides in own worktree and frozen own venv.
Graph from `fd81220`: exactly aiohttp 3.14.4, AnyIO 4.14.2, MCP/mcp-types 2.2.0,
Pillow 13.0.0, pypdf 6.19.0; no added/removed package names. Certifi was already
patched independently. Browser-use remains 0.13.11, browser-harness 0.1.13.
Full versioned-lock-entry comparison additionally finds one metadata-only change:
the approved global AnyIO override removes httpx2's `sys_platform != 'emscripten'`
marker from its AnyIO edge. No other unchanged-version package entry changes. AnyIO
was already unconditional elsewhere in the project; supported Linux/macOS target
selection is unchanged. No Emscripten support/build is claimed. Lock manifest records
exactly the five approved overrides, not additional overrides/upgrades.
Candidate SHA256: pyproject
`58e96aea9a6c6978c1faccb28b79aae6b88aca5e9cd8058203969f4d5f6f6f94`, uv.lock
`a24c0b9204db335c62e01a14a689ecaf374d589b53d8f1830bbc4ff1a440967a`.

Dedicated actual-dependency regressions in `tests/test_dependency_compatibility.py`:

- Real ephemeral loopback TLS handshake and echo through AnyIO: trusted localhost
  succeeds, wrong hostname fails SSLCertVerificationError; cancelled task group
  executes finally and settles. No external TLS/provider request.
- Actual Chromium PNG/JPEG screenshots decode/resize in Pillow13; browser-use Python
  highlight image conversion; browser-use GIF with task/font/goal overlays writes
  multiframe artifact and seeks/decodes second frame. Local-browser watchdog's actual
  aiohttp `/json/version` discovery succeeds, closed-port deadline fails as expected.
- Benign six-codec PNG/JPEG/GIF/WebP/TIFF/BMP roundtrips and deliberately lowered
  pixel-bomb rejection thresholds. This is **not** a native-code exploit/fuzz proof;
  EPS/Ghostscript, malformed TIFF vulnerability variants and other codecs unexercised.
- Actual browser-use FileSystem reads a generated receipt PDF, prioritizes/truncates
  a 24-page >60k-character PDF, returns <62k output including metadata, and handles
  malformed PDF. The upstream limit is **output only**, not CPU/RAM containment:
  it parses all pages synchronously before truncation. Hostile-PDF resource bounds
  remain an explicit application gap; no configuration/contract was changed here.
- MCP2.2 client/server imports and CallToolRequest/Result schema roundtrip, with
  socket bind/connect forbidden. This deliberately runs in a child process: browser-use
  MCP CLI server import reconfigures/disables process logging by design (unchanged
  browser-use0.13.11). First in-process test contaminated a later retention caplog
  check; corrected **test isolation**, never weakened logging assertion/application.
- Production frozen graph has **no video extra**. Default absence branch remains
  explicit; separate auxiliary tools (imageio2.37.2, numpy2.4.1, imageio-ffmpeg0.6.0)
  exercise real browser-use VideoRecorderService resize/pad -> numpy -> ffmpeg MP4
  encode/decode: six PNG/JPEG-derived frames, 33x25 viewport padded to 48x32, exact
  frame count/dimensions verified. Tools are outside own frozen venv/production graph.

Initial pre-new-test full offline suite: **434 passed, 12 live deselected**. Expanded
suite exposed only the MCP test's logging contamination above, now isolated; focused
compatibility+retention rerun **26 passed, 1 optional video skipped**. Separate real
video run **1 passed**. Ops suite **12 passed**; actual ASGI rehearsal rejects >65536
assembled and fragmented WS messages (1009) and keeps capability paths out of logs.
Uvicorn warns its pinned websockets implementation is deprecated; no opportunistic
protocol implementation switch was made. Final full suite/image/audit results follow.

### Production image build completion

Original background build `sh_15f2b56ff002U9lZHKX1J6D8eL` completed successfully,
image config digest `sha256:e4eb02238dc6832e56957e8f1f1a1476fd922244188c695eff23f78f65d16d34`.
No duplicate build was started while it ran and no polling loop was used. Delay
was dependency download throughput, not an override error: Debian 96.7MB fetched
in 13m44s (~117kB/s), then Chromium156.0.8078.4 + Playwright FFmpeg/headless shell
downloaded successfully. Frozen uv0.9.5 production sync passed on Python3.12.12.
This initial image predates the final license COPY line added during its build;
a sequential cached packaging refresh adds only that attribution layer, followed
by test-only frozen-dev and optional-video overlays. No production dependency
install layer or app source change is intentionally introduced by the refresh.
Integrator subsequently reports parent main `7dccdfe` includes lane002 bounded
sandbox logs and dispatch receipt CAS. This candidate intentionally remains on
its coherent merged `b55c32c` base until committed; it does not overwrite or revert
those newer fixes. Historical triage's missing-code requests are not a current
assessment of `7dccdfe`. Rebase/integration verification of that newer source is
the next parent gate, distinct from this exact dependency amendment.

Reproduction after image build (network is disabled for actual runtime checks):

```sh
docker build --platform linux/amd64 --target offline-tests -f scripts/ops_dependency_verify.Dockerfile -t flowpilot-lane005-deps:offline-tests .
docker build --platform linux/amd64 --target video-tests -f scripts/ops_dependency_verify.Dockerfile -t flowpilot-lane005-deps:video-tests .
.venv/bin/python scripts/ops_dependency_image_checks.py --suite full
.venv/bin/python scripts/ops_dependency_image_checks.py --suite asgi
.venv/bin/python scripts/ops_dependency_image_checks.py --suite frontend
.venv/bin/python scripts/ops_dependency_image_checks.py --image flowpilot-lane005-deps:video-tests --suite video
```

Only own source fixtures/scripts/frontend/manifests are bound read-only. Built app
code and agent come from production image; no host socket, credentials, profiles or
provider network. Test image freezes dev requirements; optional video libraries go
to separate `/opt/video-tools`, not the production environment. Runtime resource
caps: 2 CPUs, 2GiB, PID1024, 256MiB SHM, disposable profile tmpfs. The Node22 UI
fixture wrapper uses no-sandbox only for its owned ephemeral headless browser;
this does not alter deployment sandbox policy or claim kernel/network isolation.

Image refresh and both test overlays completed. Final production image config digest
`sha256:1e9e9b1b97c62683ce4e04139b1e3780be665c8789351b334343155a2ddd6032`,
inspected image ID/manifest `sha256:4e4d790968c9b692035cdd3b7c5930570eade51a62b392311415b8464e0032b4`;
label `fd81220-deps-lock-a24c0b9204db` identifies the tested precommit bytes, not an
invented final commit SHA. Frozen test overlay installed only five locked dev
packages. Optional video overlay installed only the three named auxiliary tools.

Image checks so far: ASGI 65536 assembled/fragmented bound and log secrecy **PASS**;
Node22 frontend/client **6 passed**, including actual Linux Chromium UI; auxiliary
actual MP4 **1 passed**. First Linux UI run failed on teardown with ENOTEMPTY after
root Chrome exited while renderer children still wrote to its owned temp profile.
The disposable wrapper now starts a separate Chromium process group, forwards stop,
and reaps only that group before Node removes the profile. No frontend test/assertion
or component code changed. Full Node22 UI rerun passed; original failure retained
here rather than presented as a first-pass success.

The foundation-era backend rehearsal also failed its obsolete assumption that enabled
composition always returns503. Merged bootstrap `_load` now loads all lane components.
Own rehearsal now separately asserts disabled liveness200, assembled idle readiness200,
and explicit forbidden-raw-tracing startup503, including restart and graceful exit for
each. This preserves the unavailable-path requirement with a deliberate failure
fixture instead of requiring absent source modules; no production readiness code
or assertion is bypassed. Final lifecycle/full-image suite outcomes follow.

Audit rerun against the **actual three candidate image tags** returned Python0,
frontend0 and agent-production0; OS/Chromium explicitly **UNVERIFIED**. Auditor/tool
installs stayed in discarded overlays. No provider calls or host daemon/firewall
changes; a ready idle process is not sandbox/network/security clearance.

### Independent verification verdict — BLOCKED, candidate NOT committed

Production provenance check **PASS**: UID10001; 112 installed distributions match
frozen lock; exact pyproject/lock bytes; all **42 backend Python source files** and
all **4 frontend dist files** match the own merged worktree; runtime MIT notice
byte-identical. Actual image build, frozen sync and dependency audits are complete.
Actual startup/restart/graceful-exit rehearsal **PASS** for disabled liveness200,
assembled enabled idle readiness200 and explicit raw-trace startup rejection503.
This is idle startup proof, not actual task interruption/recovery or host isolation.

Full Linux/amd64 Python3.12 image suite: **444 passed, 1 failed, 1 optional-video
skipped, 12 live deselected** (304.76s). Required unresolved criterion:

- **Criterion:** unchanged direct-Chromium owned-process SIGKILL/durable-effect test
  can start its synthetic browser under the Linux image runtime.
- **Reproduction:** `tests/test_task_acceptance_browser.py::test_owned_browser_sigkill_after_durable_effect_without_replay`
  through the full image runner; direct packaged Chromium with its existing flags.
- **Expected:** `DevToolsActivePort` appears, then the durable-effect/SIGKILL/no-replay
  assertions execute successfully.
- **Observed:** browser exits before readiness; diagnostic launch returns **-6**
  and `FATAL ... No usable sandbox!`. No later effect/recovery assertion is reached
  in this image test. Do not describe it as an override regression or a passing gate.
- **Comparison:** exact direct-launch failure (-6, same sandbox diagnostic) reproduced
  in both pre-amendment image `sha256:080641dddf90993ebd961b9c315f85679024da98bac842c593b4c2fd90311902`
  (revision c7fc157) and candidate inspected above. The unchanged test passes on
  own macOS candidate venv (**1 passed**, 1.90s), and the entire macOS suite passed
  **445**. This narrows the blocker to this local Linux Chromium-sandbox environment,
  not evidence against any specific updated Python API. Exact kernel mechanism is
  not adjudicated merely from Chromium's generic suggested-workaround message.
- **Boundary:** no `--no-sandbox` added to this test, no skip/assertion relaxation,
  privilege/capability addition, host namespace/AppArmor/daemon/kernel modification,
  or peer-file repair. A sandbox-capable disposable Linux environment or explicit
  integrator decision about the existing macOS proof is required. Do not silently
  treat either as authority to waive the image gate.

Therefore **do not commit/cherry-pick the Python candidate yet**. Preserve own
pyproject/lock, dedicated regressions, runtime-license COPY and fixture scripts
uncommitted. Independent certifi `42d202c` and frontend `fd81220` remain verified
checkpoints with their stated scopes. This evidence-only checkpoint is NOT approval
of uncommitted overrides. Parent `7dccdfe` remains unmerged/unmodified here.

Global blockers remain separately named: nft bridge conntrack policy rejected with
Protocol error (host network enforcement unverified), OS/Chromium advisory inventory
unverified, plus the direct-Chromium image-fixture sandbox blocker above. Zero Python/
npm advisory counts clear none of them. No provider calls/deployment occurred.

Candidate remains in the worktree, not staged. Additional sanitized source-only
backup (seven explicit own source/test/fixture files, no secrets/peer files):
`/private/var/folders/tp/nvzb0t5s0jqb9lyy9r0wn0940000gn/T/opencode/lane005-python-candidate-a24c0b9204db-7cf8ed439d80.patch`,
SHA256 `7cf8ed439d80f499f6e3e06fd5605bd095c03e82a8b20af1b8fd204d42b45e0b`.
This is a backup of the existing dirty code slice, not a new integration commit;
do not apply it on top of those same working changes. Only narrative/machine
evidence is committed at this blocked checkpoint.

Final focused Linux image rerun (dedicated dependencies, actual Chromium Pi
observation/stale/no-progress/completion/repair, actual TLS viewer/assistance and
retention): **51 passed, 1 auxiliary-video skipped**, 146.15s. Separate actual
video test remains **1 passed**, so this skip does not conceal missing codec proof.
This focused success does not waive the unchanged full-image direct-launch failure.
Final verdict remains **blocked**, not override incompatibility established and not
global hosting/security approval. Candidate runtime-license COPY also remains in
the uncommitted slice; source license in frontend commit is verified, but deployers
must not omit the runtime attribution follow-up.

## R3 OS/Chromium image audit evidence — 2026-10-22

Branch from main `0b5d115`. Commits: `fef81de` (pinned-Trivy gate
`scripts/ops_image_audit.py`), `b938943` (base bumps + in-suite upgrade, sandbox on
sha256-pinned Chrome for Testing 156.0.8078.47, curl/python3.11/Playwright driver
removed), `ae42705` (fixable OS findings can never be allowlisted; backend-to-sandbox
CDP smoke), `07adc26` (both images on Debian 13 trixie), followed by the evidence
commit adding `scripts/ops_image_allowlist.json`, `scripts/ops_image_triage.json` and
the hosting-readiness "OS and bundled-runtime image audit" section.

HIGH/CRITICAL counts, OS packages plus bundled runtimes: sandbox 11C/153H -> 1C/69H;
backend 11C/130H -> 1C/70H. No remaining OS finding has a distro fix. The 141
remaining findings are 72 unique, justified allowlist entries. Two of them are backend
runtimes owned by the `uv.lock` Playwright pin: Chromium 156.0.8078.4 and the Node
24.21.0 driver.

Gate results: final images exit 0; baseline images exit 1 (186 unaccepted).

Final-image verification:
- sandbox CDP on 9222 and socat 9223;
- backend Playwright `connect_over_cdp` render/screenshot;
- backend native launch;
- ASGI rehearsal;
- agent-host 4/4;
- ops tests 13/13;
- lifecycle rehearsal identical to baseline. Its enabled-503 expectation fails on both
  baseline and final, so the failure predates this lane.

This does NOT clear `DEPENDENCY_SECURITY_UNRESOLVED`, because the Python advisories and
the lane-005 candidate are untouched. It also does not clear the Linux "No usable
sandbox!" gate or `HOST_NETWORK_POLICY_UNVERIFIED`; both need a real Linux host.
