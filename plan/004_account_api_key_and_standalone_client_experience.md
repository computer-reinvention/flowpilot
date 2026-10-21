# 004 — Account, API-key and standalone client experience

## Outcome

A person opens Flowpilot, signs up/logs in, creates an API key and can immediately use the browser-task API from any client with explicit isolated profiles. No Harp implementation or broker configuration is required.

Prerequisite: completed 000 only. Build frontend and client examples against its wire fixtures and a local fake HTTP service; do not wait for the other implementation streams.

## Ownership

Own frontend auth/key/task/profile/help UI and hooks, new frontend verification files, `docs/browser-task-api.md` and runnable standalone examples under `examples/browser_tasks/`. Backend account/auth/key/profile handlers are owned by 001; assistance transport by 003. Do not change backend contracts, Dockerfiles, legacy flow execution semantics or `../harp`.

## Implementation slices

1. Reuse the existing login/signup/account state and API-key settings screen. Verify actual server error/loading/logout behavior. Key creation reveals the plaintext once, copying is explicit, list views show metadata only and revocation updates UI state. Never place a key into URLs, analytics, screenshots, examples or browser logs. Check key-response caching and logout cleanup.
2. Provide a minimal browser-task surface using 000's contracts: enter task, create/select an owned profile, follow a session, view status/result and cancel. Fresh profiles are the default; profile/session reuse is explicit. Display profile identity and explain that separate API keys for one account do not create separate tenants or browser profiles.
3. Show pending questions and approve/decline decisions bound to task/interaction/generation. Clear sensitive entry values after submission and avoid persisting them in frontend storage or rendered task history. Recover pending metadata after refresh without recovering secret values. Reject stale decisions instead of silently applying them to a new pause.
4. Add current-pause assistance entry using the account-authenticated mint endpoint and 003's viewer contract. Keep API keys out of viewer links; avoid storing bearer viewer links in task history. Handle expiry, cancellation and resume revocation. Account switching/logout must stop stale status/input requests and prevent the new account seeing the old task UI.
5. Add owned-profile management with explicit reuse/delete behavior and useful busy-profile errors. Explain which state persists (website login, script memory, conversational context), which is isolated and what cannot be resumed after a crash.
6. Publish bounded curl and Python/JavaScript HTTP examples covering login/key creation, start with idempotency key, status polling, a follow-up with the returned session ID, fresh versus reused profiles, responding, private help, cancel and key revocation. Use standard HTTP dependencies or existing project dependencies; don't invent a required SDK/service. Examples obtain keys from a secret environment/input, never commit values.
7. Document timeout/lost-response recovery: reuse the same admission key/payload, reattach by task ID, never automatically resubmit uncertain website effects as a new task. Report interrupted/unknown outcomes honestly. Document API-key account authority so a multi-user client cannot pretend one shared key plus a user string is secure tenant isolation.
8. Keep existing flow dashboard behavior available. This is a small onboarding/task API experience, not a frontend redesign. MCP and application-specific client integrations are optional future consumers, not prerequisites.

## Verification

- Frontend component/HTTP-mock checks for signup/login/logout, once-only key disclosure, list/revoke errors and account switching.
- Fresh client example can use only a configured base URL/key and 000's fixtures; no Flowpilot Python internals or shared filesystem.
- Two isolated profiles under the same account plus two separate accounts behave distinctly in the UI/examples.
- Question/approve/decline/help/cancel round trips render correctly; stale polling and decisions cannot cross account/task generations.
- Frontend production build and existing relevant checks pass. Final real-service execution belongs to 006.

## Handoff / definition of done

Provide frontend fixtures, runnable HTTP examples, precise API usage documentation and passing isolated checks. Log in → create key → run isolated browser task is the acceptance path. No Harp changes, deployment or real website transactions are needed for this stream.

## Lane004 implementation evidence

Branch: `implementation/lane-004`, based on frozen foundation `0ccc06a`. All implementation stayed in `/Users/pgwork/Desktop/code/flowpilot-worktrees/lane-004`; no backend production/shared-contract, lockfile, Docker/Compose, Harp or other-lane changes were made. No deployment, provider credential, live service or website effect was used.

### Delivered checkpoints (ordered)

1. `d7351f4` — frozen browser-task HTTP types/client, no-store/redirect refusal, account disposal/generation guards, golden fixture tests.
2. `51dee1d` — separate Browser tasks surface; fresh/explicit owned-profile/session reuse; task-ID reattachment; serialized status polling; questions/approve/decline/browser resume; cancel intent; current-pause private viewer; account-keyed dashboard and key/password cleanup; real-DOM HTTP mock checks.
3. `915d74b` — retryable key-list UI, metadata-only key state, exact uncertain-admission retries, definite rejection recovery and useful busy-profile errors.
4. `ab2672c` — `docs/browser-task-api.md`, standalone Python/JavaScript clients, bounded curl helper, synthetic fixture HTTP service and executable example checks.
5. `47b3a93` — late-401 account-switch fencing, expired secret-entry/viewer cleanup, private-terminal input enforcement and extra account/DOM regressions.

Files: `frontend/src/{lib/browserTasks.ts,lib/api.ts,hooks/useBrowserTasks.ts,hooks/useAuth.ts,hooks/useApiKeys.ts,App.tsx}`, auth forms, sidebar/shell, API-key settings, new `components/tasks/BrowserTasks.tsx`, dedicated `frontend/tests/{accountHttp,browserTasks,ui}.test.mjs`, `docs/browser-task-api.md`, and `examples/browser_tasks/{http_client.py,http_client.mjs,curl_http.sh,mock_http.mjs,test_http.mjs}`. This evidence section is the only plan change.

### Verification actually executed

- `npm --prefix frontend run build`: passed, including TypeScript; existing >500 kB bundle warning remains (final bundle ~570 kB). Dependencies were reused through the ignored worktree-local `frontend/node_modules` symlink; no package manifest/lock changes.
- `node --test frontend/tests/*.test.mjs examples/browser_tasks/test_http.mjs`: **8 passed**, repeatedly. Runs local headless Chrome with a new temporary profile and local mock HTTP server, plus real Python/JS/curl subprocesses. Covers signup/login error/password clearing, once-only plaintext disclosure and no key storage, metadata/list/revoke failure/success, two fresh owned profiles, busy/delete handling, fresh/default versus explicit profile/session payloads, exact lost-admission retry payload/key, private answer clearing, declined confirmation, stale-generation rejection, browser-help expiry/window closure on resume, cancellation intent/terminal rendering, expired unsubmitted values, account logout and second-account foreign-task denial. The account HTTP regression delays an old 401 until after token switch and proves the new token survives.
- `/Users/pgwork/Desktop/code/flowpilot/.venv/bin/python -m pytest -q`: **170 passed, 12 live skips**. First run without agent-host dependencies had one Pi host failure; repeating with a temporary worktree-local `agent-host/node_modules` symlink to existing root dependencies passed. That unignored symlink was removed after verification; no root modules were changed.
- `npm --prefix agent-host test`: **4 passed**, with the same temporary dependency symlink.
- `git diff --check 0ccc06a..HEAD`, `bash -n examples/browser_tasks/curl_http.sh`, Python example compile: passed.

### Exact contract assumptions and integration limits

- Uses the existing account routes/envelopes and JWT storage convention. Browser-task endpoints/payloads mirror the frozen foundation; idempotency is a **body field**, not an invented header. Auth/key responses and assistance mints are never intentionally cached by the clients. Foundation `backend/main.py` has no no-store response middleware/headers for legacy auth/key routes: **lane001 must ensure server/proxy `Cache-Control: no-store`**, and lane006 must verify it. Frontend request cache flags alone do not prove server-side cache safety.
- Frozen policy permits assistance minting only for a current browser pause and returns `frame-ancestors 'none'`. The owner UI therefore prepares a grant, then opens a top-level private popup on explicit click; no API key goes into a viewer URL. Viewer addresses are not rendered in task history/storage. Popup blocking has an explicit retry message.
- A managed viewer closes on pause/account/navigation/expiry changes; remint/resume/cancel/expiry/restart are the server-revocation mechanisms. **No independent grant-revoke-on-logout endpoint is frozen**. A deliberately copied capability cannot be invalidated solely by closing UI/logout; do not claim otherwise. No schema was changed to conceal this limitation.
- `ProfileList.next_cursor` exists but no cursor request parameter is frozen. The UI reports truncation rather than guessing pagination. Profile reuse is explicit; busy/recovery profiles are not silently replaced. Browser-task and legacy flow profile surfaces remain distinct.
- Refresh recovers pending metadata by explicit task-ID reattachment; it does not recover answers, prompts, viewer grants or pending uncertain-admission payloads. Exact admission retry state is memory-only. Preserve the caller's original payload/key outside logs through a suitable client-secret boundary if process-crash recovery is required. Private responses are never automatically replayed/rebound after stale/uncertain outcomes.
- Python/JS bootstrap creates an in-memory temporary key, performs one operation, then attempts explicit revocation using the dashboard token; it prints no key/JWT. Revocation does not cancel an admitted task. Curl output deliberately strips returned keys/tokens/help links, so usable key creation should use dashboard or in-memory bootstrap. All ordinary clients require only a base origin/key; no broker or SDK.
- The mock service is a fixture responder, not an authorization, persistence or executor implementation. Local checks prove UI/client contract behavior, **not** actual server ownership enforcement, durable profile restoration, provider execution, reverse-proxy Origin handling, viewer revocation enforcement or website exactly-once effects. Assembled system acceptance remains with lane006; no branch merge was performed.

Verdict: **conditionally verified** — lane004 is complete against frozen fixtures/local HTTP and real DOM; lane001/003 composition and lane006 real-service verification remain integration prerequisites, not work implemented here.
