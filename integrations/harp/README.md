# Integrating FlowPilot browser tasks into Harp

FlowPilot is a hosted browser-task service at `https://flowpilot.technology`. Harp keeps
the conversation, the user's identity and approvals. FlowPilot owns the isolated browser,
saved logins (cookies), reusable page scripts and the browsing agent. This is the same
split as Harp's existing `browse` tool, except the browsing agent runs remotely.

Files here:

- `README.md`: this developer guide.
- `skills/flowpilot/SKILL.md`: agent instructions. Copy it to
  `harp/toolkits/skills/flowpilot/SKILL.md`.
- `flowpilot_client.py`: a dependency-light reference client (httpx), shaped like
  `harp/browser_use_infra.py`.

## Service contract

Every request carries `Authorization: Bearer <FlowPilot API key>`. The full contract is in
`docs/browser-task-api.md`.

| Call | Purpose |
|---|---|
| `POST /api/v1/browser-tasks/profiles` `{label, idempotency_key}` | Create an isolated browser profile (cookies, storage, saved scripts, history). |
| `POST /api/v1/browser-tasks` `{task, inputs?, profile_id?, session_id?, idempotency_key}` | Start a task. Returns 202 with `task_id`, `session_id`, `profile_id`, `revision`. |
| `GET /api/v1/browser-tasks/{id}?after_revision=N&wait_seconds=25` | Long-poll the task view: `state`, `progress`, `interaction`, `result`, `error`. |
| `POST /api/v1/browser-tasks/{id}/interactions/{iid}/respond` | Answer a question, approve or decline a confirmation, or report a browser step resolved. |
| `POST /api/v1/browser-tasks/{id}/assistance` `{interaction_id, generation}` | Mint a short-lived private viewer link for a `waiting_for_browser` pause. |
| `POST /api/v1/browser-tasks/{id}/cancel` | Request cancellation, then poll until the task reaches a terminal state. |

States are: `queued → running → waiting_for_input | waiting_for_confirmation |
waiting_for_browser → completed | failed | interrupted`, and `cancelling → cancelled`.

A completed task's `result` is `{answer, source_urls, metrics}`. `answer` is written for
the end user.

## Identity and isolation

- **One FlowPilot account per Harp deployment.** Store its API key as a service secret:
  - add a `flowpilot_api_key: SecretStr` field to `Settings` in `harp/config.py`;
  - add the `FLOWPILOT_API_KEY` env alias;
  - declare it under `secrets:` in `config/<env>.yaml`;
  - store the value with `scripts/vault.py`.

  Add `flowpilot_base_url` as a non-secret config value.
- **One FlowPilot profile per Harp `(workspace_id, user_id)`.** Create it lazily, persist
  `profile_id` in Postgres, and always pass it explicitly. Profiles isolate cookies, site
  logins, saved scripts and history, so one user's Amazon login is never visible to
  another user.
- **Harp is the authorization boundary.** API keys on one FlowPilot account share
  authority, so Harp must only ever use the caller's own `profile_id`, looked up from
  `turn.store.principal_ids(turn.ev)`. Never take a profile or task id from model output
  without checking that its owner is the current principal.
- **Shared workspace logins** follow the existing `shared_access_owner_required` rules: one
  workspace profile, and only its owner can resolve its browser pauses.

## Toolkit shape

Add a `FlowPilotToolkit` frozen dataclass in `harp/toolkits/flowpilot.py`, following
`BrowserControlToolkit`:

- `name="flowpilot"`, `skill_names=("flowpilot",)`.
- Active when the key and base URL are configured and the feature flag is on.
- Register it in `harp/run/base_toolkits.py` **before** `BrowserControlToolkit` if it
  should win the `browse` name. Otherwise expose `flowpilot_browse` and route by flag.

Tools (async closures over `turn` that return JSON strings; all HTTP goes through
`call_blocking` or an async httpx client):

1. `flowpilot_browse(task: str, follow_up: bool = False)`
   - Resolve or create the caller's profile.
   - Start the task with `idempotency_key = sha256(turn event id + task)[:32]`, so a retried
     turn never starts a second task.
   - If `follow_up` is true, pass the user's last `session_id` instead of `profile_id`.
   - Long-poll up to about 120 s inside the turn.
     - If the task finishes, return `answer` and `source_urls`.
     - If it is still running, persist the task and return
       `{"status":"running","task_id":...}`. The maintenance reconcile then resumes the
       conversation when it finishes (see below).
2. `flowpilot_task(task_id: str)`: status of one of the caller's own tasks.
3. `flowpilot_cancel(task_id: str)`.

## Human-in-the-loop mapping

FlowPilot pauses a task with a typed `interaction`. Map each kind to an existing Harp
primitive. Never let model-visible text carry secret values.

| FlowPilot interaction | Harp handling |
|---|---|
| `waiting_for_input` with only `text` fields (e.g. delivery address, passenger count) | Ask in the conversation; `DecisionToolkit.choose` works for `select` fields. Respond with `{kind:"input", generation, idempotency_key, values}`. |
| `waiting_for_input` with any `secret` field (OTP, PIN, password) | Use the private-input path of `BrowserTaskService.request_step` / `private_recipient(turn)`: a Slack or WhatsApp DM whose reply is captured by the host (`capture_private_reply`), never shown to the model. The host posts the value straight to FlowPilot's respond endpoint, then drops it. |
| `waiting_for_confirmation` | Show `message` (what will happen, e.g. "Book Uber Go for ₹412") with approve and decline buttons. Only an explicit approval from the owning user sends `confirmed:true`. A timeout or any other answer sends `confirmed:false`. |
| `waiting_for_browser` (CAPTCHA, passkey, user wants to sign in personally) | Mint `/assistance`, then send `assistance_url` (prefix it with the base URL) **only to the owner, privately**, as a managed setup card. Never post it in a channel. After the user finishes, respond with `{kind:"browser", generation, idempotency_key, browser_resolved:true}`. |

Always bind a response to the exact `task_id`, `interaction_id` and `generation` returned,
using a fresh `idempotency_key`. On 409 (stale generation), re-poll and handle the new
interaction; never re-send an old answer to it.

## Long-running tasks and resumption

Most tasks finish in 20–120 s. When one outlives the turn:

1. **Persist** a `flowpilot_task` row: workspace_id, user_id, task_id, session_id,
   profile_id, conversation reference, last revision, state. Add it as a numbered,
   idempotent migration registered in `_SCHEMA_REQUIRED_TABLES`.
2. **Reconcile** in `harp/maintenance.py` next to `browser_tasks.reconcile`. For each
   non-terminal row, poll with `after_revision`.
   - On a new interaction, run the mapping above.
   - On a terminal state, `turn.spawn` a continuation event with
     `kind="flowpilot_task_resolved"` and `trigger_kind="browser_task_callback"`, so the
     agent reports the result in the original thread.
3. **Never auto-retry** a task that ended `interrupted` or `failed` with a website effect
   possible. Tell the user what FlowPilot reported and ask before starting again.

## Errors

Task errors are `{error:{code,message,retryable,retry_after_seconds}}`:

| Status | Meaning | Handling |
|---|---|---|
| 401 | Key revoked | Configuration error. |
| 404 | Not yours or missing | |
| 409 | Conflict: busy profile, stale generation or profile recovery | Re-poll before doing anything else. |
| 422 | Invalid input | |
| 429 | Capacity | Honour `Retry-After`. |
| 503 | Unavailable | Retry later with the same idempotency key. |

`profile_recovery_required` means the profile's saved state needs operator attention. Tell
the user, and do not create a replacement profile silently.

## Rollout checklist

1. Create the FlowPilot service account and API key in the dashboard at
   `https://flowpilot.technology` (API Keys → Create Key), then store the key in the vault.
2. Add the `Settings` fields, the toolkit, the skill, the migration and the reconcile hook.
3. Behavioral eval (`scripts/verify_*`):
   - a read-only flight search returns the answer and its sources;
   - a follow-up in the same session reuses the session;
   - a confirmation decline books nothing;
   - an OTP flow keeps the code out of the transcript;
   - two users get different `profile_id`s.
4. Ship behind a `flowpilot` feature flag per workspace.
