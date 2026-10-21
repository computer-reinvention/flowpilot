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

No deployment/setup/up/stop/cleanup was run on the actual host. No host tools were
installed, certificates issued, live accounts contacted or credentials copied.
Only labelled disposable fixture containers/networks were removed. Reusable uniquely
named `flowpilot-lane005-*:verify` build images remain for integrator rechecks; no shared
cache/daemon/image pruning was performed. Hosted readiness remains intentionally blocked.
