# Single-host packaging and operational handoff

**NOT approved for hosted traffic. No infrastructure has been provisioned or deployed.**
Lane005 packages foundation `0ccc06a` for Linux/amd64. This is one backend process,
SQLite and bounded browser concurrency, not a horizontally scalable service.
The feature defaults off. `/health` is liveness; enabled tasks must pass
`/health/browser-tasks`. A green Docker health status is not a host-security attestation.
`ops_config.py` deliberately reports named hosted blockers until independent evidence
is reviewed; `--local` validates only configuration and cannot waive those blockers.

## Artifacts and non-secret checks

- `Dockerfile.backend`: digest-pinned Node 22.22 (meets >=22.19), Python 3.12.12,
  uv 0.9.5; npm lock installs and frozen uv lock; non-root uid 10001, fixed single
  worker, no HTTP access logs, init through Compose, 45s application / 55s server /
  65s container shutdown budgets. `ops_start.sh` fails before serving on invalid
  settings, missing provider/JWT authentication, unwritable/low disk, or unavailable proxy.
- `Dockerfile.sandbox`: digest-pinned Python and exact Playwright 1.64.0.
  OS packages and browser download are upstream repository resolved, **not bit-for-bit
  reproducible**. Refresh pins/lockfiles through their owners; do not silently accept
  a digest pin as a vulnerability clearance.
- Compose: backend 3GiB / 2 CPU / 256 PIDs; socket proxy 128MiB / .25 CPU / 64 PIDs;
  restart unless-stopped, logs <=3 x 10MiB per service; backend loopback publication
  only. Sandbox logs/resource limits must also be enforced by lane002 at creation.
  Budget at least 8GiB host RAM for the initial **2 global / 1 account** browser cap,
  host/proxy overhead and 1GiB/browser + 256MiB SHM. This is a conservative starting
  budget, not a benchmark-backed production capacity guarantee. Load-test first.
- `.dockerignore`: excludes environment variants, subscription credentials, durable
  profiles/databases, traces, local chat state and recovery archives. The frontend
  `components/profiles/*.tsx` exception is source, not browser data.
- `deploy`: no installations, global Caddy rewrites, credential prompts/copies, image
  pruning or sandbox deletion. `setup`/`cleanup` refuse. `restart` does not rebuild.
  Host up/stop/restart require separate explicit operator confirmation; this lane tests
  them only with a fake `docker` executable in a disposable directory.

Run from the integrated checkout (never print `docker compose config` with real secrets):

```sh
python -m unittest discover -s scripts -p ops_tests.py -v
python scripts/ops_compose_verify.py
bash -n deploy
# Placeholder is ONLY for structural Compose validation, never for a service:
JWT_SECRET=structural-check-not-a-deploy-key docker compose config --quiet
python scripts/ops_context_check.py
python scripts/ops_asgi_rehearsal.py
# Built, uniquely labelled fixtures only; no Compose/deploy operations:
python scripts/ops_backend_rehearsal.py
python scripts/ops_sandbox_smoke.py
```

`ops_context_check.py` builds synthetic canaries and scans saved image contents/layers;
it never reads real credentials. Image removal is limited to its unique generated tag.

Disposable tooling (one build at a time; no host mounts, socket or published ports):

```sh
docker build --platform linux/amd64 --label flowpilot.ops=lane005 -f scripts/ops_verify.Dockerfile -t flowpilot-lane005-tools:verify .
docker run --rm --platform linux/amd64 --label flowpilot.ops=lane005 --network none --cpus 1 --memory 512m --pids-limit 128 --cap-add NET_ADMIN flowpilot-lane005-tools:verify
docker run --rm --platform linux/amd64 --label flowpilot.ops=lane005 --network none --cpus 1 --memory 512m --pids-limit 128 flowpilot-lane005-tools:verify python /verify/scripts/ops_proxy_rehearsal.py
# Additional capabilities below apply ONLY inside this throwaway namespace container.
docker run --rm --platform linux/amd64 --label flowpilot.ops=lane005 --network none --cpus 1 --memory 512m --pids-limit 128 --cap-add NET_ADMIN --cap-add SYS_ADMIN --security-opt seccomp=unconfined --sysctl net.ipv4.ip_forward=1 flowpilot-lane005-tools:verify python /verify/scripts/ops_network_rehearsal.py
```

## Network trust boundary — blocked until proven on target-compatible Linux

Browsers render hostile websites. A private control network is **not** sufficient:
the backend is on both networks, and the bridge gateway can reach host/private services.
`ops_network_policy.nft` is a reviewable **uninstalled** bridge + inet ruleset:

- deny new RFC1918, loopback, CGNAT, link-local/metadata, multicast/reserved IPv4;
- deny sandbox-originated IPv6 until a separately tested public-IPv6 policy exists;
- permit public TCP 80/443 and established backend-initiated CDP replies;
- deny new host-local traffic, including the host's public IP; same-bridge filtering
  handles backend/Docker destinations without assuming routed FORWARD interception.

DNS is Docker's embedded resolver; final destination filtering, not URL spelling,
must stop DNS changes and redirects. DNS-over-HTTPS may resolve names but cannot waive
destination filtering. UDP/QUIC and arbitrary website ports are intentionally unavailable.
Private websites/VPN destinations are unsupported by this policy. Custom private
routes and unusual assigned public ranges need operator review. gVisor is optional,
**not enabled or verified**. Chromium's `--no-sandbox` remains an existing application
design risk requiring operator review, not an accepted risk or container escape
mitigation. Do not treat the shared host as a
security boundary against a compromised backend.

The local Docker Desktop kernel rejected bridge `ct state` with **Protocol error**.
We did not load host modules, modify Docker daemon configuration, or install host rules.
`HOST_NETWORK_POLICY_UNVERIFIED` is therefore a hard readiness blocker. A Linux
operator must separately review kernel bridge conntrack support, persist the exact
rules, verify they survive Docker restart/reboot, and run the namespace rehearsal.
It creates only fixture-local public/private/metadata destinations, tests each route
before blocking it, and tests IPv6, DNS destination changes and HTTP redirects.
Do not probe actual metadata/private infrastructure. Passing that simulation still
requires lane006 probes from the **real assembled sandbox**, including CDP reply
availability and sibling-container isolation. A host firewall/nft install is a separate
authorized operation, not part of this handoff. No script installs the rules.

The socket proxy's CONTAINERS+POST permission is broad enough to create dangerous
containers if the backend is compromised. A read-only socket mount does not restrict
Docker API writes. No proxy port is published; only backend joins `control`. Proxy
privileged mode was removed; exec/images/volumes/swarm APIs are disabled. This is
defense-in-depth, not an authorization proxy for arbitrary untrusted backend code.

## Reverse proxy, account security and secrets

`scripts/ops_Caddyfile` is a candidate, **not an installer**. Merge its global log
exclusion and site manually with existing operator-owned configuration, validate
the combined file, and reload only under separate authority. Do not overwrite
`/etc/caddy/Caddyfile`. TLS/DNS/certificate issuance is not performed here.

The proxy supports WS upgrade, bounded bodies (128KB), 35s response headers (poll
wait <=25s) and 10m stream lifetime (capability authorization must still revalidate).
No access logger is enabled. Global `http.log.error` is excluded too because default
reverse-proxy errors can log secret URIs. Do not enable debug/request logs, CDN/WAF
request capture, tracing or external analytics without equivalent redaction for BOTH
capability path segments and queries. Losing request error logs is intentional; use
opaque metrics and `/health/browser-tasks` for operations.

`scripts/ops_logging.json` also discards Uvicorn/websockets protocol/access/error
loggers: WebSocket accept/reject messages can carry capability paths even when HTTP
access logging is disabled. This deliberately trades raw server tracebacks for
confidentiality. Application task/executor logs still require their owners' sanitized
logging contract; this file is not a generic secret scrubber for arbitrary app logs.
The ASGI rehearsal uses the packaged logging config and proves exact/oversized and
fragmented receive bounds against a fake application without any provider calls.

Uvicorn trusts forwarded headers only from explicit individual addresses (loopback
by default); `FLOWPILOT_TRUSTED_PROXY_IPS` accepts only loopback or the packaged
`172.30.240.1` host gateway, never wildcard/CIDR or browser addresses. Caddy reaches a loopback-only
published port whose Docker NAT may present a bridge address. Do **not** set `*`.
Set `FLOWPILOT_EXTERNAL_HOST` to the exact public URL authority; Caddy routes only
the configured site and replaces upstream Host/X-Forwarded-Host with that value.
Lane003 compares Origin to actual ASGI scheme/Host, so the observed NAT source must
be proved and explicitly trusted for scheme normalization before assistance works
over TLS. Do not independently trust client Forwarded/X-Forwarded headers in app code.
Uvicorn pins the websockets implementation and assembled receive limit **65,536
bytes**, queue 16, so fragmented commands cannot grow without an upstream bound.
Lane003/006 must verify that receive bound and exact host-proxy topology before
changing the trusted proxy allowlist. The local rehearsal uses HTTP+WS only: HTTPS,
real viewer/capability expiry and per-frame authorization remain integration gates.

Operator prerequisites (never commit these values):

1. Restricted `.env` owned by the operator, mode 0600. Stable random JWT secret of at
   least 32 bytes; preserve it separately from backups for JWT/receipt continuity.
2. Exactly one public HTTPS origin in `FLOWPILOT_BASE_URL`/`ALLOWED_ORIGINS`, operator
   DNS/TLS setup, no public backend/Docker/CDP ports, ingress/rate-limit review.
3. Selected Gemini/Anthropic/OpenAI API key **or** `LLM_PROVIDER=codex`, `AGENT_DRIVER=pi`
   with an explicitly supplied `.pi-auth/auth.json` owned by uid 10001, mode 0600.
   Subscription refresh may require a writable restricted directory and operator
   reauthentication; unattended refresh reliability is not guaranteed. No login/copy
   is automated. Pi auth is never mounted into browser containers.
4. Pre-create profiles, traces and optional `.pi-auth` directories with appropriate
   uid 10001 ownership, mode 0700. Set absolute `PROFILES_HOST_DIR` to the actual host
   profiles path, not a container path. Review logs/backup permissions separately.
5. Verify signup/login/API-key management is exposed only as intended, revocation
   works, and login/API rate limits apply across **legacy and new** routes. Task
   settings alone do not establish signup/login brute-force protection.

## Backup and restore (never browser-accessible)

Backups contain website credentials, account hashes and profile secrets. Use an
operator-supplied public `age` recipient; keep its private identity offline, mode
0600, separate from the host and archives. Host tool installation is manual.
The helper requires Python >=3.12 and `age` on the operator's chosen offline tool
environment. It does not install either or copy keys. Key arguments are **paths**
or public recipients, never secret key values. Backup parent must be mode 0700.

Pause admission, wait for all leases/writers and graceful profile saves, then stop
the service under separate authorization. `--quiesced` is an operator assertion,
not an automatically verified fence. SQLite's supported `Connection.backup()` API
captures each DB including committed WAL records; profile and cross-DB consistency
requires stopped writers. No live `.db` file copy is used. Source symlinks are rejected.

```sh
python scripts/ops_backup.py backup /isolated/state /restricted/backups/revision-date.age --revision EXACT_GIT_SHA --quiesced --recipient age1PUBLIC_RECIPIENT
python scripts/ops_backup.py restore /restricted/backups/revision-date.age /isolated/NEW-state --revision EXACT_GIT_SHA --identity /offline/age-identity
```

Restore refuses an existing target, traversal/link archive members, mismatched
revision/format, checksum mismatch and SQLite schema/integrity mismatch. Rehearse
with no live mounts/network. Verify records and account/profile separation before
manually connecting a recovered directory. No migration or service startup occurs.
Plaintext exists temporarily in a restricted temp directory on the backup/restore
filesystem; use encrypted storage. SIGKILL/power loss may leave such temp directories:
review restricted `.ops-*` recovery directories offline; never expose them publicly.
Backups publish atomically without overwriting previous archives. Disk exhaustion
or encryption failure must leave the previous archive intact. Identity/JWT recovery
and off-host retention/rotation remain operator responsibilities.

## Retention, disk and lease-aware cleanup

Docker logs rotate at 30MiB/service; tracing defaults off. `ops_retention.py` defaults
to a count-only dry run for known trace extensions; applying requires `--quiesced`.
It also recognizes the existing tracer's `NNNN_function.name.json` filename pattern,
but preserves arbitrary JSON/profile files rather than treating every JSON as a trace.
It never follows symlinks or deletes profiles, database rows or Docker resources.
Use an explicitly designated trace directory, never a generic profile/state path.

```sh
python scripts/ops_retention.py /isolated/traces --days 7
# Only after stopping trace writers, separately authorized:
python scripts/ops_retention.py /isolated/traces --days 7 --apply --quiesced
```

`history_retention_days` is forwarded, but lane001 must implement transactional,
terminal-only task pruning; lane002 must own idle eviction, lease-aware quarantine
expiration and failed-save recovery without deleting a prior good profile archive.
No broad label-based `docker rm`, generic cron deletion, profile glob deletion or
`docker system prune` is shipped. Until those owners expose safe cleanup, accumulating
history/quarantines is **RETENTION_RUNTIME_UNVERIFIED**, not a hidden destructive script.
Monitor disk/inodes and alert well before 1GiB free; startup preflight is not a
continuous disk monitor. ENOSPC during save/admission and abrupt reboot need assembled
failure tests proving interrupted tasks, revoked capabilities and archive preservation.

## Evidence ledger and remaining gates

Local evidence is recorded in plan005. Twelve offline tests cover settings forwarding,
safe error text, disabled/enabled readiness, mocked deployment, WAL-backed SQLite
recovery, isolated profiles, revision/checksum checks and conservative trace cleanup.
The real-age fixture round trip recovered records and two private profile archives,
rejected tampering and preserved an existing encrypted backup. Caddy config parsed.

Dependency inventory (2026-10-21; advisories change): agent production npm install
audited 93 packages with **0 reported vulnerabilities**; Linux Node 22.22 ran all four
agent tests. Frontend lockfile audit reports **7 high**, rooted at
`braces` stack exhaustion [GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm),
propagated through `micromatch`, `fast-glob`, `@ts-morph/common`, `ts-morph`,
`@shadcn/registry`, `shadcn`. The direct dependency is dev-only shadcn; frontend
node_modules/build tools are not copied into the runtime image, but build-time risk
still requires frontend-owner review. npm proposes a breaking shadcn change; this
lane did **not** run `npm audit fix`, change locks or accept that risk for the user.
Python production lock export audited with pip-audit 2.9.0 in a disposable container:
**28 known advisories in 6 packages**, not accepted or fixed in this lane:

| Locked package | Advisory count | Reported fixed version (review compatibility) |
| --- | ---: | --- |
| aiohttp 3.14.3 | 8 | 3.14.4 |
| anyio 4.12.1 | 2 | 4.14.2 |
| certifi 2026.7.22 | 1 | 2026.10.19 |
| mcp 2.1.1 (transitive) | 1 | 2.2.0 for the installed major |
| pillow 12.3.0 | 8 | 13.0.0 |
| pypdf 6.16.2 | 8 | 6.19.0 covers the reported set |

`python scripts/ops_dependency_audit.py` reproduces the Python/frontend/agent inventory,
printing advisory IDs, aliases and reported fixes, and exits nonzero on findings.
It installs the auditor **only inside a disposable container**, never the host or
production image. `--disable-pip --no-deps` audits the complete frozen lock export;
it does not re-resolve production dependencies or claim package hash verification.
Dependency-owner compatibility review, lock updates and repeat audits are a release
gate. OS/Chromium CVE inventory remains unverified. Image pins and successful builds
must not be described as a clean security scan.

All four Linux/amd64 production build stages (frontend, agent, complete backend,
sandbox) built successfully locally. Backend and sandbox image sizes in the initial
build were ~674MB and ~577MB respectively; these are packaging observations, not
disk-capacity guarantees. The real ASGI boundary rehearsal passed inside the backend
image as well as the local Python environment; uvicorn emits a deprecation warning
for the legacy websockets implementation. The frozen dependency revision currently
honors its assembled receive limit; any uvicorn upgrade must rerun the fragmented
message test rather than assuming future alias behavior is unchanged.

Disposable backend rehearsal passed disabled liveness, enabled-but-missing composition
503, both states after restart and graceful exit 0 against a fake Docker API on a
unique internal network. It did not attach the host socket or publish any ports.
The offline sandbox CDP smoke passed with no published ports. These are packaging
tests; they do not establish real task interruption/recovery, network isolation,
durable tmpfs state, or rate-limit correctness.

### Exact application/contract requests to the integrator

The following original checkpoint requests are historical. The read-only triage
below checks newer owner commits and supersedes blanket missing-implementation
claims for resource wiring, profile-turn retention, task recovery scope and idle cleanup.

- **001:** implement terminal-only history retention using `history_retention_days`;
  ensure login/signup and all existing/new API key routes have enforceable limits;
  retain fail-closed readiness until startup reconciliation is complete.
- **002:** consume all `TaskSettings.sandbox_*` resource values at Docker creation;
  set per-sandbox bounded Docker `log_config`, drop unnecessary capabilities
  (especially `NET_RAW`/`NET_ADMIN`), never publish remote CDP, and enforce idle /
  failed-save-quarantine retention without deleting leased profiles or previous
  known-good archives. Existing `cleanup_orphans()` uses broad Flowpilot labels:
  scope it to this deployment and generation before hosted startup. The packaging
  helper intentionally does not duplicate that unsafe cleanup externally.
- **000/001/002:** there is currently no lease-aware maintenance/retention port in
  `task_ports.py`. If cleanup must run outside the runtime, amend the shared contract
  with an atomic, deployment-scoped, generation-checked maintenance operation first;
  do not infer safe deletion from container age or account labels alone.
- **003/006:** actual ASGI scheme/Host is the Origin authority. Verify the observed
  host-proxy NAT peer and trust only that individual address; verify exact external
  Host normalization, 65,536-byte assembled and fragmented WS rejection, private
  path/query capability logs and real viewer authorization with the merged runtime.
- **004 / shared lock owner:** review the seven frontend build-tool advisories;
  coordinate any dependency/lock updates rather than modifying a peer lockfile here.

No live provider calls, real accounts, credentials, infrastructure installation,
certificate issuance, deployments, host firewall changes or peer files were used.
Required remaining gates include assembled lane006 acceptance, crash/reboot/graceful
stop semantics, resource/ENOSPC tests, real sandbox topology/policy probes, TLS/viewer
authorization and all dependency vulnerabilities reviewed by their owners. Do not
promote this branch based on its documentation or an HTTP 200 alone.

## Read-only dependency and implementation triage — 2026-10-21

**Coordinated implementation follow-up:** main `b55c32c` merged as `57372a2`.
Certifi-only patch committed `42d202c`; 30 focused tests passed in own frozen venv.
Frontend licensing gate is resolved from exact release tarball MIT LICENSE.md,
verified against formerly locked SHA512. CSS/license are vendored unchanged; CLI
removed. Clean Linux Node22 build/audit pass with **zero npm findings**, all four
merged-source dist files byte-identical, runtime dependency map unchanged, six
frontend/client checks (real Chromium DOM included) and four agent checks passed.
291 obsolete lock entries removed; no surviving version changes; six already-bundled
optional WASI metadata entries made explicit by npm (no WASI build claimed).
Python overrides remain pending actual compatibility proof; historical proposals
below are not proof those overrides have been accepted. Global readiness blockers
remain unchanged. Full incremental evidence is in plan005 and machine report.

Machine-readable actionable inventory: `scripts/ops_dependency_triage.json`.
It includes **all 28 Python advisory IDs, aliases and individual reported fixed
versions**, all seven frontend package/version findings, locked dependency paths,
conditional runtime exposure, exact candidate edits and immutable source references.
Original manifests/locks remain byte-identical to the recorded SHA256 values.
Experiments modified only disposable container copies; no provider was contacted.

### Smallest dependency amendments proposed, not applied

1. **Certifi first: `uv.lock` only.** Target `certifi==2026.10.19` using
   `uv lock --upgrade-package certifi==2026.10.19`. The disposable uv 0.9.5 resolver
   changed **only certifi**. It is transitive through HTTPX/HTTPcore/Requests without
   a conflicting upstream exact pin. Verify the new trust bundle, frozen sync/audit
   and existing tests; intentionally distrusted certificate chains may stop working.

2. **Frontend: preserve CSS, remove unused CLI.** The seven high package findings
   are **one** root braces stack-exhaustion advisory propagated through
   `shadcn -> {registry, ts-morph -> common, fast-glob} -> micromatch -> braces`.
   All seven entries are dev-only; no vulnerable CLI JavaScript is imported by the
   app or copied into the runtime image. However, `frontend/src/index.css:3` imports
   `shadcn/tailwind.css`: deleting only the dev dependency clears audit but **breaks
   the build**. A second disposable experiment preserved the pinned stylesheet
   byte-for-byte, changed only its import, and removed the CLI. Result: **zero npm
   findings, build passed, complete dist byte-identical, all TSX sources unchanged,
   runtime dependency map unchanged, no surviving lock version changed**, 291 unused
   lock entries removed.

   Proposed exact files: remove `devDependencies.shadcn` in `frontend/package.json`;
   regenerate `frontend/package-lock.json`; change the one import in
   `frontend/src/index.css`; add `frontend/src/shadcn-tailwind.css` with the exact
   16,340-byte pinned stylesheet (SHA256
   `4c371f7a1ff5d219ae2f7ff28bd256b4346fd546fe46fbae22092e57db2f0fae`);
   preserve verified release licensing in `frontend/src/shadcn-tailwind.LICENSE`.
   **License provenance review remains required.** Repeat clean npm ci, audit,
   build/dist comparison and UI animation smoke against the merged lane004 source.
   No component rewrite or runtime dependency upgrade is proposed. CLI-based future
   component generation becomes a separate explicitly pinned developer workflow.

   A targeted braces patch override is **not presently established**: npm registry
   latest is 3.0.3, the affected version; no 3.0.4 was published at this check.
   Do not invent that version, substitute an API-incompatible parser, or accept
   npm's suggested shadcn 1.0.0 breaking downgrade as a verified fix.

3. **Remaining Python dependencies: coordinated override amendment required.**
   Actual installed browser-use **0.13.11** metadata pins **aiohttp==3.14.3,
   anyio==4.12.1, mcp==2.1.1, pillow==12.3.0, pypdf==6.16.2** exactly;
   browser-harness 0.1.13 also pins Pillow. Therefore a normal targeted lock upgrade
   cannot fix these pins; the disposable aiohttp patch experiment was unsatisfiable.
   Proposed files are `pyproject.toml` and `uv.lock`, not browser-use source/upgrades:

   ```toml
   [tool.uv]
   override-dependencies = [
     "aiohttp==3.14.4", "anyio==4.14.2", "mcp==2.2.0",
     "pillow==13.0.0", "pypdf==6.19.0",
   ]
   ```

   With the certifi change, disposable resolution changed **exactly seven** packages:
   those six plus required `mcp-types 2.1.1 -> 2.2.0`; zero removals; browser-use stayed
   **0.13.11**. Frozen no-dev export audit then reported **zero Python findings**.
   This is a **resolver/advisory experiment, not runtime compatibility proof**.
   Overrides deliberately replace upstream requirements; Pillow 13 crosses a major.
   PyPI's published 12.x series ends at 12.3.0 at this check; there is no demonstrated
   12.3.1 security patch alternative. If 13 fails compatibility, a supported upstream
   backport or reviewed native-wheel backport is a separate amendment, not an invented
   version pin or automatic risk waiver.
   No candidate runtime packages were installed into the application environment.
   Before approval require owner acceptance of the exact override, offline full tests,
   screenshot/PNG/JPEG/GIF/video/image codec fixtures, PDF extraction/resource limits,
   cancellation/replay/repair, MCP schema imports without exposing an MCP listener,
   and assembled lane006 + backend/ASGI rebuild checks. No broad browser-use upgrade
   or change to shared public contracts is proposed.

The report distinguishes installed from exercised vulnerabilities: Flowpilot's
ingress uses FastAPI/Uvicorn, not aiohttp; the aiohttp server-specific issues are not
automatically ASGI ingress issues. MCP's advisory concerns a stateful Streamable
HTTP server not observed in this service. AnyIO TLS and certifi affect shared HTTP
infrastructure; browser-use directly reads PDFs, so parser/text-extraction exhaustion
deserves particular attention. Pillow's generic image opens exist, but not every
specific TIFF/EPS/PDF/morphology advisory path was demonstrated. No issue is silently
accepted because a targeted API has not yet been observed.

### Missing implementation versus missing assembled proof

Reviewed **immutable owner Git objects**, not peer files: lane002 `c90556f`, lane001
`efbcd76`; integration `main` was `d3301fe` at triage start. These are source-level
findings, not assertions that all those revisions are merged or deployed.

| Requirement | Current evidence | Remaining action |
| --- | --- | --- |
| Sandbox memory/CPU/PID/SHM settings | **Implemented**: runtime injects settings, browser adapter passes them, `sandbox.py:87-90` derives Docker arguments; memory/PIDs mock test exists | Real merged container/cgroup and bounded exhaustion proof; do not ask owner to wire these again |
| Global/account caps | **Implemented**: repository `127-137` checks reservations transactionally | Concurrency/paused/cleanup-failure assembled admission proof |
| Normal terminal idle browsers | **Eagerly closed**, not a retained warm pool: executor `108-155`, runtime `289-293`, profile save/remove path | Prove normal terminal containers are gone and follow-up restores state; unused idle timeout does not itself prove a leak |
| Profile conversational turns | **Implemented lazy retention**: `memory.py:28-31,50-52`, configured age + last 100 on write | Cutoff/lock tests; unopened inactive profiles are not continuously swept |
| Durable control/task history | **Missing age-based implementation** in lane001; profile deletion removes history but no retention sweep consumes the knob | Transactional terminal-only pruning, lease protection and explicit idempotency/tombstone retention semantics |
| Sandbox Docker logs | **Missing explicit creation-time log_config** in lane002; Compose service logging does not apply to spawned containers | Set bounded json-file 10m/3 or reviewed equivalent; inspect actual sandbox LogConfig; no daemon edits |
| Failed-save quarantine | **Preservation implemented**: bounded archives, prior archive survives, stopped recovery container retained, profile recovery marker and host-scoped locking | **Missing global age/count/byte retention**; explicit deletion exists but is not a sweep. Never delete only recovery source/active lease by age alone |
| Executor dispatch memory | **Missing bound**: `Executor.seen` grows per execution at `31,60-63`; only active map is removed | Coordinate bounded dedupe with durable terminal/generation fences; duplicate dispatch must remain safe |
| Raw traces | Hosted executor rejects raw tracing; offline safe retention tool exists | Operator scheduling/monitoring, not a destructive app/profile janitor |
| Recovery/orphan scope | Task recovery **host-scoped**, legacy cleanup now skips task containers | Legacy orphan selection remains broad; unnecessary capability drops remain absent |

**No readiness clearance:** kernel/network enforcement blocker is unchanged.
OS/Chromium advisories remain **unverified**, not included in the zero-findings
candidate Python/npm experiments. TLS/topology/real assistance and application
acceptance gates remain in force.
