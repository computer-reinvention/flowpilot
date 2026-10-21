# Lane006 acceptance handoff

**Acceptance evidence, not a deployment approval.** Foundation: `0ccc06a005c3ff680110de6b66a6606a4e48d3d3`. Authorized amendments `2201b5a`/`561383d` are local `2ff7646`/`c06505a`. Following explicit integrator authority, main `8d6d2f845e5b92e5bdb757631bd06ee3ceee0fb7` was merged as `cf73e2c` for assembled testing. No production repairs, shared fixture edits, provider credentials, deployment, Harp changes or external website effects occurred.

## Latest assembled checkpoint (supersedes pre-assembly gaps below)

The actual Control/SQLite Repository/Executor/Assistance/native Observation and account router now run in a dedicated backend container with real browser containers on one uniquely labelled disposable bridge. Only `run_pi` is replaced by a deterministic conversational/model driver. The real BrowserSession, scripts, browser tools, DOM readiness/freshness checks and all HTTP producers remain intact. For the explicit fault scenarios only, constructor instrumentation gates a real Playwright mouse operation **under the runtime writer lock**; a transparent executor proxy delegates successful `release_human`, or deliberately raises/stalls it for failure/timeout cases. No public failpoint endpoint exists. No strict peers are used in this assembled command.

```sh
/Users/pgwork/Desktop/code/flowpilot/.venv/bin/python scripts/verify_task_system.py --assembled-docker --deterministic-local --require-complete
```

Actual observed checkpoint `task-acceptance-c46c6a8e5fe54a618b8b504ed371f24a/evidence.json`: **25 passed observations, zero failed**; exit **2** because the full acceptance inventory still has unmet gates. Source at that checkpoint was deliberately marked dirty; a final clean-source rerun is recorded separately below when available. Earlier real run `f7980340f7f94eb1bfdc015027e99912` also proves viewer/expiry and actual backend SIGKILL/restart. The published 004 CLI target was added after those checkpoints and must be read from the final rerun, not inferred from its source code.

Proven actual paths: signup/login and ordinary key/no-store; foreign task/profile/session failures and forged/expired JWT/header rejection; two same-account keys share authority/idempotency while profiles remain explicit; four concurrent admissions yield one task and one cart effect; saved script/cookie/localStorage/history restore on a fresh-client follow-up without repeating the prerequisite; another same-account profile and a foreign account have clean website/scripts/history. Native Pi tests cover cross-origin unknown, volatile timeout, delayed same-URL stale action and repeated consumed-action fencing. Actual paused browsers hit account cap **2** and global cap **3**, with rejected admissions spawning no extra browsers and all three cancelling/releasing. Transient OTP question receipts/conflicts and real declined confirmation prevent submission. Private HTML/PNG/pointer/type/keyboard/viewer resume works through two distinct pause generations; actual elapsed capability expiry rejects state and late input. Direct authenticated API resume with a real active JPEG stream and held human input cannot wake agent effects before drain; failure and timeout preserve the unresolved interaction. Revocation rejects later operations/minting without cancelling an admitted task. Independent bcrypt/SQLite and raw snapshot/backend-log canary checks are green. SIGKILL of one owned backend child after a website effect produces `interrupted`, revokes prior access, preserves the original admission receipt, quarantines the profile, and never automatically replays the website mutation; explicit profile deletion cleans the recovery artifact.

Configuration: sandbox **1024 PIDs**, 1024 MiB memory, 1 CPU, 256 MiB shm; backend 1024 PIDs/2 GiB. Task 180s, human wait 90s, capability TTL 30s, cancellation 2s, shutdown 30s, observation deadline 15s, convergence window 0.5s; API 10,000/minute and login 100/minute are explicit test inputs, not changed production defaults. Existing images: backend `sha256:163eb2e0517aaac2d52188815f81edb18dc305b62b9ee4fc7c3c55dce3f162be`; browser `sha256:5725dc387dab4ce81d4f68358523de708118ea3a39a2edea0e0bd182b31aba2d`. These are **not** evidence for lane005's hardened packaged topology/network policy. No image/tag or production setting was changed.

`docker_run.py` creates only its unique `task-acceptance-<uuid>` bridge and backend, mounts this checkout read-only and a labelled temporary state directory, and passes only synthetic auth secrets—never inherited provider credentials. Cleanup selects containers by this exact network, checks expected labels, removes those containers and that network only, then deletes its own temporary state. Raw profiles, logs, DBs, JWTs, keys, OTP and capabilities are not committed. The actual legacy main lifespan/global orphan sweep is never run; actual account routes and task-host lifecycle are composed explicitly.

Earlier assembled reports are retained as harness-development history, not production defects: `7d5cc50...`/`400c9e...` had an acceptance-launcher factory-name error; `98cab...`/`bcddb...`/`d312...` used the cross-origin fault page as the normal site, which correctly failed native readiness closed; `f0d770...` hit the actual default API rate limit while the constructor-only override did not configure the environment-reading auth dependency; `6dc884...` used a login rate above the frozen maximum and failed setup. These were fixed only in harness fixtures/configuration. The normal site is now stable, `/frames` explicitly exercises cross-origin unknown and `/volatile` visibly changes an output.

Full local merged-worktree regression at source `708cb58`: **450 passed, 12 skipped, 1 failed** in 125.85s (first attempt exceeded a 120s command timeout and was rerun with a larger bound). The single Pi-host catalogue-message failure persists in this worktree without its own installed agent-host dependencies; the integrator explicitly reports it does **not** reproduce with installed root dependencies and main's pre-final checkpoint passed 405 tests/12 skips. It is not being reported as a new production defect. No dependencies or production files were repaired by this lane.

Remaining inventory is intentionally conservative: full secret restart/expiry variants, affirmative/expired confirmation coverage, TLS proxy/frontend delivery, deterministic mint-lookup→save commit failpoint, all stale executor callbacks/long-running JS cancellation variants, profile-save failure/quarantine injection, actual script selector repair, packaged private/metadata network policy, operations/backup/build/UI completeness, provider-backed quality and hosted validation are **not** established by the narrower green observations. The final complete-readiness command therefore must not return 0 merely because core deterministic scenarios pass.

## Historical pre-assembly checkpoints

## Proven at revision `a692441`

- Dedicated suite: **71 passed**; dedicated plus amended foundation contracts: **125 passed**. Actual Chromium, local SQLite website counters, separate origins, cookie/storage restoration and separate contexts; selector replacement, stale references, delayed same-URL DOM, keyboard/drag/frame effects; lost mutation response and an actual owned Chrome process-group SIGKILL after durable effect.
- Frozen fixture consumption is independent of producer models. Actual HTTP scripted peers check the standalone client's identical-payload lost-response retry, retryable capacity/backoff and nonretryable conflict. These are **consumer tests**, not proof that the service deduplicates execution.
- Real Uvicorn HTTP fail-closed checks run with **lifespan off**. Disabled task admission is 404; enabled/not-started admission is 503. Disabled health's HTTP 200 never becomes an acceptance pass. The legacy global Docker orphan sweep is deliberately not invoked.
- Scanner detects plaintext, URL, escaped JSON and base64 canaries in files and SQLite/WAL. Empty, oversized, compressed or symlinked inputs create explicit gaps. Independent read-only bcrypt key verification has positive and negative self-tests.
- Full repository suite at `a692441`: **241 passed, 12 explicit live skips, 1 failed**. Isolated earlier rerun and final full run reproduce `tests/test_pi_agent.py::test_real_pi_host_rejects_unknown_model_without_provider_call`: expected `catalog does not contain`, actual `Pi exited without a result`. Ownership: Pi/agent integration (002/007). No cause is inferred; no repair attempted.
- Amendment consumer self-tests require successful public mint and `/state` with pause generation 9 and no lease field, validate current URL/viewport, require capability-only requests without forwarding the account key, and reject leaked execution identity, stale pause metadata and invalid viewport bounds. Strict peers prove the consumer only, not producer integration.

## Exact commands

Run with cwd `/Users/pgwork/Desktop/code/flowpilot-worktrees/lane-006` (after integration, use that checkout as cwd instead):

```sh
/Users/pgwork/Desktop/code/flowpilot/.venv/bin/python -m pytest tests/test_task_acceptance_contract.py tests/test_task_acceptance_browser.py tests/test_task_acceptance_security.py tests/test_task_acceptance_baseline.py tests/test_task_acceptance_assistance.py -q
/Users/pgwork/Desktop/code/flowpilot/.venv/bin/python scripts/verify_task_system.py --offline
```

The offline runner exits 0 only for its selected harness tests. Its report still says `readiness: not_established`. Add `--require-complete` to require all service gates (exit 2 while incomplete).

After the parent supplies an **assembled, disposable local service using deterministic local inference and no provider credentials**:

```sh
/Users/pgwork/Desktop/code/flowpilot/.venv/bin/python scripts/verify_task_system.py --base-url http://127.0.0.1:8080 --deterministic-local --site-browser-host host.docker.internal --require-complete
```

For host-native browsers, omit `--site-browser-host`. The site binds host loopback only. Docker Desktop's `host.docker.internal` must map back to those two ephemeral listeners; Linux needs a separately configured disposable forwarding topology. A mapping failure is a failure, not grounds to mock the browser. A readiness-only probe omits `--deterministic-local` and creates no accounts/tasks.

The service scenarios sign up/log in two run-labelled accounts, create ordinary API keys, check once-only disclosure, create two owned profiles, reject foreign/forged authority, concurrently retry admission, require exact website counters and an owned follow-up from a fresh HTTP transport, exercise secret-question receipts and decline, cancel a browser pause and require capability expiry/late-response rejection, then revoke the key. No SDK/backend imports. These integrated scenario drivers **have not yet passed against real peers**. Their natural-language tasks require a deterministic local inference implementation; they must fail rather than invent interaction states when a peer does not implement a case.

The amendment target additionally requires two successful public mint/state cycles on **different pause generations within the same uninterrupted task**, URL/viewport metadata and no exposed lease generation; foreign-owner and stale-generation mint rejection; remint revocation; actual wall-clock expiry; and concurrent mint/resolve/cancel followed by rejection of every minted capability's state and late input. Launch the disposable service with `FLOWPILOT_TASKS_ASSISTANCE_TTL_SECONDS=3` to exercise expiry within the bounded 10-second test window (longer TTL reports that check blocked). A public HTTP scheduling race is not deterministic proof of the `assistance_lease` lookup→`save_capability` commit race: that stronger requirement stays blocked pending a separately supplied owned failpoint. The harness never substitutes pause generation for the private execution lease.

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
| `c9912c4efb8449ce92ecf2dcb1108428` | Pre-amendment offline harness checkpoint, 61 passed |
| `bd02374d18f24cdbb8bd01cd5eb1ece9` | Post-amendment offline harness checkpoint, 71 passed; all integrated requirements remain blocked |
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
