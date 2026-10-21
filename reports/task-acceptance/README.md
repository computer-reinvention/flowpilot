# Lane006 acceptance handoff

**Harness proof, not service readiness.** Foundation: `0ccc06a005c3ff680110de6b66a6606a4e48d3d3`. No other lane was integrated. No production files, shared fixtures, provider credentials, deployment, Harp files or external websites were changed/used.

## Proven at revision `473861d`

- Dedicated suite: **61 passed**. Actual Chromium, local SQLite website counters, separate origins, cookie/storage restoration and separate contexts; selector replacement, stale references, delayed same-URL DOM, keyboard/drag/frame effects; lost mutation response and an actual owned Chrome process-group SIGKILL after durable effect.
- Frozen fixture consumption is independent of producer models. Actual HTTP scripted peers check the standalone client's identical-payload lost-response retry, retryable capacity/backoff and nonretryable conflict. These are **consumer tests**, not proof that the service deduplicates execution.
- Real Uvicorn HTTP fail-closed checks run with **lifespan off**. Disabled task admission is 404; enabled/not-started admission is 503. Disabled health's HTTP 200 never becomes an acceptance pass. The legacy global Docker orphan sweep is deliberately not invoked.
- Scanner detects plaintext, URL, escaped JSON and base64 canaries in files and SQLite/WAL. Empty, oversized, compressed or symlinked inputs create explicit gaps. Independent read-only bcrypt key verification has positive and negative self-tests.
- Full repository suite at `8e6ce6e`: **227 passed, 12 explicit live skips, 1 failed**. Isolated rerun reproduces `tests/test_pi_agent.py::test_real_pi_host_rejects_unknown_model_without_provider_call`: expected `catalog does not contain`, actual `Pi exited without a result`. Ownership: Pi/agent integration (002/007). No cause is inferred; no repair attempted. Three additional harness tests were added afterward; final full-suite result is recorded in plan006 evidence if rerun.

## Exact commands

Run with cwd `/Users/pgwork/Desktop/code/flowpilot-worktrees/lane-006` (after integration, use that checkout as cwd instead):

```sh
/Users/pgwork/Desktop/code/flowpilot/.venv/bin/python -m pytest tests/test_task_acceptance_contract.py tests/test_task_acceptance_browser.py tests/test_task_acceptance_security.py tests/test_task_acceptance_baseline.py -q
/Users/pgwork/Desktop/code/flowpilot/.venv/bin/python scripts/verify_task_system.py --offline
```

The offline runner exits 0 only for its selected harness tests. Its report still says `readiness: not_established`. Add `--require-complete` to require all service gates (exit 2 while incomplete).

After the parent supplies an **assembled, disposable local service using deterministic local inference and no provider credentials**:

```sh
/Users/pgwork/Desktop/code/flowpilot/.venv/bin/python scripts/verify_task_system.py --base-url http://127.0.0.1:8080 --deterministic-local --site-browser-host host.docker.internal --require-complete
```

For host-native browsers, omit `--site-browser-host`. The site binds host loopback only. Docker Desktop's `host.docker.internal` must map back to those two ephemeral listeners; Linux needs a separately configured disposable forwarding topology. A mapping failure is a failure, not grounds to mock the browser. A readiness-only probe omits `--deterministic-local` and creates no accounts/tasks.

The service scenarios sign up/log in two run-labelled accounts, create ordinary API keys, check once-only disclosure, create two owned profiles, reject foreign/forged authority, concurrently retry admission, require exact website counters and an owned follow-up from a fresh HTTP transport, exercise secret-question receipts and decline, cancel a browser pause and require capability expiry/late-response rejection, then revoke the key. No SDK/backend imports. These integrated scenario drivers **have not yet passed against real peers**. Their natural-language tasks require a deterministic local inference implementation; they must fail rather than invent interaction states when a peer does not implement a case.

For a **read-only copy** of this run's DB/history/logs/results/unpacked traces, populate environment variables in memory (do not put secrets in arguments or tracked files):

```sh
/Users/pgwork/Desktop/code/flowpilot/.venv/bin/python scripts/verify_task_security.py --artifacts /absolute/path/to/task-acceptance-RUN/snapshot
/Users/pgwork/Desktop/code/flowpilot/.venv/bin/python scripts/verify_task_security.py --database /absolute/path/to/task-acceptance-RUN/snapshot/platform.db
```

`TASK_ACCEPTANCE_CANARIES_JSON` is a JSON array of distinctive synthetic password/OTP canaries (minimum 12 characters). `TASK_ACCEPTANCE_KEYS_JSON` contains this run's synthetic `fp_live_...` API keys. Include DB WAL files in the snapshot. A scan of arbitrary clean files is not full DB/history/log/trace coverage; the report deliberately leaves that gate open.

Optional packaged-browser diagnostics:

```sh
/Users/pgwork/Desktop/code/flowpilot/.venv/bin/python scripts/verify_task_security.py --network-manifest /absolute/path/to/task-acceptance-RUN/network.json
```

`TASK_ACCEPTANCE_CDP_URL` must be the explicitly disposable task browser's host-loopback CDP endpoint. Example manifest shape (ports are placeholders, not claimed running services):

```json
{"run_label":"task-acceptance-RUN","allow":"http://host.docker.internal:18080/health","deny":[{"browser_url":"http://backend:8080/health","host_control_url":"http://127.0.0.1:8080"}]}
```

The positive control must succeed, and each denied target must be demonstrably listening via its host control. GET-only diagnostic paths are restricted; no cloud credential paths or arbitrary external destinations. These are navigation diagnostics, **not complete metadata/DNS-rebinding/WebSocket/exfiltration proof**. A new context is closed, never the remote browser or unrelated contexts. No Docker resources are created or swept by these scripts.

Security runners exit 1 for an observed check failure and 2 for unresolved acceptance requirements. Reports never contain bodies, tokens, DOM, exception text or plaintext canaries; artifact references are hashes. Generated reports carry exact revision, dirty-tree flag and frozen fixture hashes. Images/topology/build IDs remain unpinned until the parent supplies assembled artifacts; none are invented.

## Evidence index

| Directory suffix | Meaning |
| --- | --- |
| `ce16c6112b6442db97be8b4b83752fd8` | Earlier offline harness checkpoint, 58 passed |
| `c9912c4efb8449ce92ecf2dcb1108428` | Current offline harness checkpoint, 61 passed |
| `3d835f2fbf8a4ea287e446ddce6d5593` | Actual HTTP baseline, lifecycle off; readiness 503 `starting`, acceptance **blocked**, exit 2 |
| `22c56a200586422dbd2024a14e4aaf3b` | Scanner CLI self-test on safe synthetic file; coverage incomplete, exit 2 |
| `fe9c62e3083740be8d24b518c71686a7` | Scanner CLI **deliberately injected synthetic leak**, correctly failed, exit 1; not a production leak |

Directories above have prefix `task-acceptance-`. No disposable website databases, browser profiles, canary values or raw traces are retained.

## Named unverified requirements / parent continuation

Every report preserves the complete requirement inventory, rather than dropping unselected checks. **This is a partial acceptance harness, not plan006 completion.**

- 001/002: real admission commit→dispatch fault, exclusive claim/execution under concurrent retries, paused/idle global/account/session/profile capacity, restart of queued admissions without redispatch, actual stale executor callbacks, client process restart.
- 002: actual service-owned two-account/two-profile website state/scripts/history, graceful restore and failed-profile-save quarantine, mutation→result crash reconciliation, model-subprocess exit, script replay/repair without repeating prerequisites. Direct fixture-browser proofs do not establish these.
- 001/002/003: input-resolution/process-crash fault, transient canary delivery to the current executor only, approved/expired decisions, assistance remint/wrong-owner/origin/expiry, in-flight stream/input→resume and cancel fencing. The supplied HTTP drivers cover only a subset and are unexecuted against peers.
- 003/005: real TLS proxy viewer, pointer/keyboard/drag, JPEG stream, screenshot fallback, held-input release and late frame/input fencing.
- 007: actual Pi readiness/hash/freshness/no-progress guards on the synthetic page; fixture behavior alone is not Pi algorithm verification.
- 005: packaged network/metadata policy, Docker-labelled topology/resource lifecycle, image IDs/build/config/log/disk checks, backup/restore and operations. No Docker fixture launcher or generic production kill/failpoint endpoint was invented; the parent must provide owned local topology/fault hooks first.
- 004: published standalone example execution and actual frontend onboarding/key/task UI. The independent stdlib client is not a substitute for running 004's example.
- Full relevant regressions (including the named Pi baseline failure); complete real-service artifact/key-hash checks; separately authorized provider-backed cases; hosted TLS/cloud validation remain open. Provider/hosted checks are intentionally not authorized in this run.

Only the harness-owned process groups, contexts, ephemeral listeners and uniquely labelled temporary directories are cleaned. Run-labelled accounts remain in the explicitly disposable service DB; no global account/profile/database deletion is attempted. No other branches are integrated by this lane.
