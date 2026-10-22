# FlowPilot implementation tracker

Rebuilt on 2026-10-22 from two Opencode sessions, cross-checked against git:

- **S1** `ses_ebbb90a42ffe…`: "Cloning Flowpilot for local setup" (Oct 16–21). Covers the Pi driver, REPL, human-in-the-loop (HITL), Blinkit, and reflexes. Ends at commit `851c75b`.
- **S2** `ses_ea1b5ab99ffe…`: "Hosting FlowPilot on DigitalOcean" (Oct 21). Covers the 000–007 hosted browser-service lanes. Ends at `main` = `7dccdfe`.

S2 ended badly. The last prompt ("continue. what remains?") failed twice with a provider HTTP 400, so it never got an answer. The lane 005 and 006 subagents finished their work, but nobody integrated it.

Legend: ✅ done and verified · 🟡 partial, or done but not merged · ❌ not started or open · ⛔ blocked

---

## A. Local agent and REPL (S1), committed in `851c75b`

| Item | Status | Notes |
|---|---|---|
| Clone and local setup | ✅ | Repository is `computer-reinvention/flowpilot` |
| Pi agent driver with Codex OAuth (`LLM_PROVIDER=codex`, `AGENT_DRIVER=pi`) | ✅ | Real subscription inference, generation, replay, and repair passed |
| `./chat` REPL / `BrowserSession.ask()` with per-user identity (`pankaj`) | ✅ | Identities are local namespaces, not dashboard accounts |
| Chat asks for OTPs, missing values, and confirmations; browser handoff only for direct intervention | ✅ | Synthetic OTP page: 1 question, 0 handoffs |
| HITL parity: `human_help`, CDP assistance links, pause/resume webhooks | ✅ | |
| VNC-like remote control (pointer, drag, keys, scroll) | ✅ | Tested on Chromium and WebKit |
| Full Chromium on Xvfb in Docker (not headless, `navigator.webdriver=false`) | ✅ | |
| Profile persistence across a graceful restart | ✅ | Blinkit login and cart survived. An abrupt-crash path is covered later by lane 002 |
| Catalog reads (Blinkit, BigBasket, Amazon.in, Flipkart, IKEA India) | ✅ | |
| Blinkit cold vs warm cart benchmark | ✅ | 235.8s cold vs 173.5s warm (26% faster) |
| Train search cold, warm, warm benchmark | ✅ | 3.9–6.3× faster by the second warm run (`reports/train-replay-*.json`) |
| Concurrency benchmark and scaling research | ✅ | Research notes only |
| Reflex repair pipeline wired into the REPL | ✅ | |
| Stability and change guards in the Pi path | ✅ | Replaced later by lane 007's native observation policy |
| **Reflex argument binding**: `run_reflex` takes only `{name}`, no inputs | ✅ | Merged from R2. Declared inputs are validated; missing inputs return `invalid_input` and never trigger repair; partial-effect failures are not auto-repaired |
| Reflex lifecycle: TTL, site-version check, eviction after N failures | ❌ | Counters are recorded but nothing acts on them |
| Optional Jev classification, live run | ⛔ | Needs `TYPESAFE_API_KEY` |

## B. Hosted browser service lanes (S2)

The goal is to log in, create an API key, and call FlowPilot from any client with isolated browser profiles. Plans are in `plan/000–007`. Deployment itself is manual and out of scope.

| Lane | Scope | Status | Evidence / open items |
|---|---|---|---|
| **000** Shared contracts | Schemas, ports, settings, fail-closed bootstrap, fixtures | ✅ on main | `0ccc06a`, amended `2201b5a`, `561383d` |
| **001** Task control plane | SQLite admission, leases, fencing, authenticated HTTP, crash recovery, history pruning | ✅ on main | Ownership 404 fix, no-store headers, drain before API resume (`462e3cf`), pruning (`7526292`). Dedup tombstones grow without limit (intentional) |
| **002** Executor and browser lifecycle | Internal CDP network, private profiles, conversation execution, cancellation, quarantine | 🟡 on main | Real Docker isolation and persistence proven. Log rotation 10m×3 and dispatch receipt limit merged. **Quarantine quota is designed (`7dccdfe`) but not implemented**: total quarantine count and bytes are unbounded. Cleanup of legacy non-task orphans lacks proof |
| **003** Remote human assistance | Capability links, TLS/WSS viewer, input streaming, serialized resume | ✅ on main | Real Chromium/TLS viewer tested. Proxy and log-redaction setup is part of integration (see 005) |
| **004** Account, API key, and client experience | UI for tasks, profiles, keys, and private viewer; `docs/browser-task-api.md`; `examples/browser_tasks/` | ✅ on main | Frontend bundle-size warning remains |
| **005** Packaging and operations | Hardened images and Compose, safe `deploy`, encrypted backups, network policy, proxy/ASGI limits | 🟡 merged `0b5d115` | Main has artifacts and triage (`b55c32c`). **Not merged**: `42d202c` (certifi), `fd81220` (remove vulnerable shadcn CLI), `5d917a9` (blocker docs). **Uncommitted** in `lane-005`: Python override candidate (aiohttp, AnyIO, MCP, Pillow 13, pypdf) |
| **006** Acceptance harness | Black-box and security runners, assembled Docker acceptance | 🟡 merged `a2b0e7e` | **10 commits not merged**, through `639dc34`. Clean assembled run passed 27/27, but `--require-complete` exits 2 because some required gates have no proof yet (section D) |
| **007** Pi observation and state safety | Element hashing, coherent snapshots, DOM readiness, stale-target and no-progress guards | ✅ on main | 24 + 14 real Chromium cases, zero wrong-target effects. Readiness is checked over a finite window. Guards run at JavaScript entry and exit, not per statement |

Main is **81 commits ahead of `origin/main` and has not been pushed**. Worktrees for lanes 001–007 are still in `../flowpilot-worktrees/`. Lanes 001, 002, 003, 004, and 007 have nothing left to merge.

## C. Next actions, in order

1. ✅ (2026-10-22, suite 461 passed / 13 skipped with Docker running) **Merge lane 006** (`implementation/lane-006`, 10 commits: assembled acceptance harness and evidence) into main, then rerun the full suite.
2. ✅ (2026-10-22) **Merge the lane 005 commits** `42d202c`, `fd81220`, `5d917a9` (certifi patch, shadcn CLI removal with the license notice kept, blocker docs).
3. ⛔ **Python dependency overrides** (uncommitted in `lane-005`). Lane 005's own Python 3.13 run passed 445 tests with zero advisories. In the Linux image, 444 passed and 1 failed: the SIGKILL/durable-effect test's Chromium exits with `No usable sandbox!`. The old image fails the same way, so this is the local Linux sandbox, not the candidate. Rerun on a sandbox-capable Linux host, or decide explicitly to accept it.
4. ✅ (R1 merged 2026-10-22, settings packaged in `60dda35`) **Implement fail-closed quarantine quotas** (lane 002 design in `7dccdfe`).
5. ⛔ **Prove network isolation**: the local Docker kernel rejects the bridge conntrack policy, so private and metadata egress denial is unproven. Needs a real Linux host.
6. ✅ (R3 merged 2026-10-22; `scripts/ops_image_audit.py` gate passes, allowlist re-review due 2026-11-15/22) **OS and Chromium image audit** (still open per lane 005).
7. ✅ (R2 merged 2026-10-22; gate check still to be wired into `verify_task_system.py` after R4) **Reflex argument binding** in `run_reflex` (A above).
8. ❌ Push main once items 1–2 are merged and the suite is green.

## D. Acceptance gates still unproven (from the lane 006 README)

Lane R4 is closing every gate below except `profile_save_failure` (R1), `script_reuse_repair` (R2), and the ⛔ ones.

These are gaps in proof, not known regressions.

- ❌ `questions_secrets`: full adversarial and secret-restart matrix
- ❌ `confirmation_decline`: approve, expired, and wrong-owner cases (decline itself passed)
- ❌ `assistance_tls`: the run used plain HTTP/WS, not a TLS proxy
- ❌ `assistance_atomic_commit`: deterministic lookup→save failpoint
- ❌ `cancel_fencing`: stale callbacks and long-running writer cases
- ✅ `profile_save_failure`: proven by R1 on real Docker (opt-in test `FLOWPILOT_R1_SAVE_FAILURE_DOCKER=1`); still to be wired into `verify_task_system.py`
- ✅ `script_reuse_repair`: proven by R2 on real Chromium; still to be wired into `verify_task_system.py`
- ⛔ `packaged_network`: private and metadata egress denial (see C5)
- ❌ `artifact_secret_scan`: browser logs, traces, and transformed-secret variants
- ❌ `operations`: completeness of backup/restore, disk, logs, and build
- ❌ `compatibility`: full UI, flow, and chat boundary coverage. The Pi-host test fails only in worktrees that lack the dependency
- ⛔ `provider_backed`: real model quality (not authorized)
- ⛔ `hosted_postdeployment`: after the manual deployment

## E. Known limitations (accepted, recorded for reference)

- A shared service credential does not isolate users. Isolation is per account, and per profile within an account.
- Hosted tasks don't repair and replay whole scripts automatically, to avoid repeating partial effects. The agent repairs inline instead.
- Logging out of the UI does not revoke an issued assistance link. The link still expires, and remint, resume, or cancel revoke it.
- browser-use's PDF 60k-character cap limits output only, not CPU or RAM use on a hostile file.
- Capacity figures (about 60–70 active sessions on 32 vCPU / 64 GiB) are estimates from synthetic benchmarks, not verified limits.

## F. Follow-ups raised by the R1/R2 lanes

- ❌ Quota refusal happens at executor dispatch, after HTTP 202 (task ends `failed/capacity_exhausted`). Refusing at admission needs a ledger check in `Repository.admit` (lane 001 area).
- ❌ Quarantine bytes are counted, not hard-limited; no Docker storage-driver quota per browser.
- ❌ No operator tools for quarantines (`list`/`resolve`/verified export); explicit profile deletion is the only way to free a slot.
- ❌ Partial-effect detection only sees page changes, not background requests.
- ⛔ Repair quality with a real model is unproven (deterministic stand-in only; needs provider calls).
- ❌ Backend image still bundles Playwright 1.64.0's pre-stable Chromium 156.0.8078.4 and Node 24.21.0 (allowlisted until 2026-11-15). Bump Playwright in `uv.lock` when a release ships stable 156+.
- ❌ `scripts/ops_backend_rehearsal.py` expects 503 in enabled mode but the app returns 200 (pre-dates R3; reproduces on the baseline image). Decide which is correct.
- ❌ Image audit gate must run on every rebuild; allowlist entries expire 2026-11-15 (runtimes) and 2026-11-22 (OS).

## G. End-to-end status (2026-10-22)

- LLM provider: Codex subscription ended; FlowPilot now uses a dedicated Anthropic key (`LLM_PROVIDER=claude`, Sonnet 5.5).
- Agent loop fixes (local `scripts/agent_lab.py` iterations):
  - real sites no longer observe as `unknown` (open shadow roots traversed, small cross-origin frames reported, large pages truncated but fingerprinted);
  - each action returns the post-action page, so the model makes no extra observe round trips (cab task: 31 → 9 turns);
  - script timeouts are model-visible and never replayed;
  - `complete` returns a user-facing answer with sources, and blocked sites are reported instead of silently failing.
- Droplet `flowpilot-blr1` (s-2vcpu-4gb, blr1, ~$24/mo) runs the stack at revision 3c7388c, with Caddy 2.11.7 configured for `flowpilot.technology`.
- Benchmarks through the public HTTP API on the droplet (`reports/e2e/droplet-*.json`):

  | Scenario | Cold | Warm 1 | Warm 2 | Outcome |
  |---|---:|---:|---:|---|
  | Flight search, BOM→DEL, Google Flights | 35 s | 29 s | 29 s | 3 cheapest nonstops with times, prices and source |
  | Cab, BOM T2→BKC, Uber | 205 s | 81 s | n/a | Trip entered; Uber requires login for fares, so it was reported honestly and nothing was booked |

- ❌ DNS: the `flowpilot.technology` A record must point to 64.227.128.41 (GoDaddy). Caddy then issues the certificate automatically.
- ❌ Cab booking needs a one-time Uber sign-in by the user through the private viewer link (`waiting_for_browser`). The profile then keeps the login, and booking still requires an explicit confirmation.
- ❌ Bot walls (DataDome/Cloudflare) on some sites, e.g. Trainline and Omio. The agent requests `human_help` (CAPTCHA via viewer) or reports the block. Datacenter IPs make this more likely.
- ✅ Harp integration instructions: `integrations/harp/` (guide, agent skill, reference client).
- 🟡 Network isolation on the droplet:
  - The kernel accepts `ops_network_policy.nft` (`nft --check` passes; Docker Desktop rejected it).
  - `ops_network_rehearsal.py` fails before loading the policy, because its browser namespace cannot reach the fixture across the namespace bridge (the outer namespace reaches it with HTTP 200). This is a harness routing issue still to debug.
  - Until it is proven, sandbox browsers can reach private and metadata addresses (e.g. 169.254.169.254); DigitalOcean user-data here holds no secrets.
