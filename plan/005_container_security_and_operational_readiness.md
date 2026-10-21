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
