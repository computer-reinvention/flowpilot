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
