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
bash -n deploy
# Placeholder is ONLY for structural Compose validation, never for a service:
JWT_SECRET=structural-check-not-a-deploy-key docker compose config --quiet
python scripts/ops_context_check.py
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
**not enabled or verified**. Chromium's `--no-sandbox` remains an accepted application
design risk, not a container escape mitigation. Do not treat the shared host as a
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

Uvicorn trusts forwarded headers only from loopback; Caddy reaches a loopback-only
published port whose Docker NAT may present a bridge address. Do **not** set `*`.
Assistance must validate Origin against the explicit external base URL, not arbitrary
client/forwarded Host. Lane003/006 must verify the exact host-proxy topology before
expanding a trusted proxy allowlist. The local rehearsal uses HTTP+WS only: HTTPS,
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

Local evidence is recorded in plan005. Eight offline tests cover settings forwarding,
safe error text, disabled/enabled readiness, mocked deployment, WAL-backed SQLite
recovery, isolated profiles, revision/checksum checks and conservative trace cleanup.
The real-age fixture round trip recovered records and two private profile archives,
rejected tampering and preserved an existing encrypted backup. Caddy config parsed.

No live provider calls, real accounts, credentials, infrastructure installation,
certificate issuance, deployments, host firewall changes or peer files were used.
Required remaining gates include assembled lane006 acceptance, crash/reboot/graceful
stop semantics, resource/ENOSPC tests, real sandbox topology/policy probes, TLS/viewer
authorization and all dependency vulnerabilities reviewed by their owners. Do not
promote this branch based on its documentation or an HTTP 200 alone.
