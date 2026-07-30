# Environment variables

Everything the engine reads from the environment, and which process needs it.
`services/api/.env` is loaded by **all three** processes (API, Temporal worker,
email adapter), so a variable put there reaches every part of the system.

Nothing here is required for the system to keep running as it does today — the two
NEW variables both have safe defaults. `INTERNAL_API_KEY` is the one worth setting.

---

## New in this release

### `INTERNAL_API_KEY` — closes the approval-bypass hole (recommended)

`POST /v1/events` marks a task done and resumes the workflow — it approves things.
It used to accept **any anonymous caller**, so the per-task role check on
`POST /v1/tasks/{token}/complete` could be bypassed simply by posting to `/v1/events`
instead. The email adapter is a trusted backend process with no user token, which is
why the endpoint was left open.

Set this to any long random string:

```
INTERNAL_API_KEY=<a long random string>
```

- **Unset (default):** behaviour is exactly as before — anonymous posting still works,
  so nothing breaks if you deploy without it.
- **Set:** `/v1/events` accepts only (a) a signed-in user who holds the task's role
  *and* is assigned to its workflow, or (b) a caller presenting this key in the
  `X-Internal-Key` header. The email adapter reads the same variable from the same
  `.env` file and sends the header automatically, so **setting this one line is all
  that is needed** — no other change.

Generate one with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### `CORS_ORIGINS` — where the browser app is served from

The allowed browser origin was hardcoded to `http://localhost:5173`. Any deployment
where the app is not same-origin with the API failed with an opaque browser CORS
error and an apparently dead UI.

```
CORS_ORIGINS=https://workflow.example.com
CORS_ORIGINS=http://localhost:5173,https://workflow.example.com   # several
CORS_ORIGINS=*                                                    # any origin
```

Default: `http://localhost:5173`. If nginx serves the app and proxies `/api` to the
API (the arrangement `VITE_API_BASE_URL=/api` produces), requests are same-origin and
this is not needed at all.

---

## Database and workflow engine

| Variable | Used by | Default | Notes |
|---|---|---|---|
| `DATABASE_URL` | API, worker | `postgresql+psycopg://app:app@localhost:5432/workflow_app` | Also used for LangGraph's checkpoints, which is what lets a run pause for weeks. |
| `TEMPORAL_ADDRESS` | API, worker | `localhost:7233` | |

## Identity (Keycloak)

| Variable | Used by | Default | Notes |
|---|---|---|---|
| `KEYCLOAK_URL` | API, worker | `http://localhost:8081` | |
| `KEYCLOAK_REALM` | API, worker | `workflow` | |
| `KEYCLOAK_ADMIN` | API, worker | `admin` | Master-realm admin, used to read roles and manage users. |
| `KEYCLOAK_ADMIN_PASSWORD` | API, worker | `admin` | **Change this in any real deployment.** |
| `AUTH_DISABLED` | API | unset | `1` bypasses token checks and treats the caller as an admin holding every role. **Local development only.** |

## Email — sending and receiving

One Gmail account serves exactly one workflow. The workflow names its mailbox in the
Builder (`mailbox: invoice`); the credentials live here, never in the definition.

| Variable | Used by | Notes |
|---|---|---|
| `MAILBOX_<NAME>_ADDRESS` | worker, adapter | e.g. `MAILBOX_INVOICE_ADDRESS`. `<NAME>` matches the Builder's Mailbox field, case- and `-`/`_`-insensitively. |
| `MAILBOX_<NAME>_APP_PASSWORD` | worker, adapter | Gmail **app password**, not the account password. |
| `MAILBOX_<NAME>_IMAP_HOST` / `_SMTP_HOST` / `_SMTP_PORT` | worker, adapter | Optional per-mailbox overrides. |
| `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` | worker, adapter | The legacy single mailbox. Still works; used only by a workflow that names no mailbox. |
| `IMAP_HOST` / `SMTP_HOST` / `SMTP_PORT` | worker, adapter | Defaults for all mailboxes (`imap.gmail.com` / `smtp.gmail.com` / `587`). |
| `NOTIFY_TO` | worker | Fallback recipient when a notification cannot be addressed to anyone. |
| `API_BASE_URL` | adapter | Where the adapter posts (`http://localhost:8000`). |

If a named mailbox is not fully configured, the engine does **not** fall back to
another account — sending a workflow's mail from the wrong address would be worse
than not sending it. The failure is logged instead.

## AI (Ollama or any OpenAI-compatible endpoint)

Every AI use is optional. Each one falls back to deterministic text, so the engine
runs correctly with no model available.

| Variable | Used by | Default | Notes |
|---|---|---|---|
| `LLM_BASE_URL` | API, worker | `http://localhost:11434/v1` | |
| `LLM_MODEL` | API, worker | `llama3.2:1b` | |
| `EMAIL_AI` | worker | `1` | `0` disables AI email wording; the facts are appended by code either way. |
| `LLM_EMAIL_TIMEOUT` | worker | `20` (seconds) | |
| `LLM_TIMEOUT` | worker | `60` (seconds) | Decision rationale. The route itself is always chosen by deterministic rules. |
| `NARRATE_TIMEOUT` | API | `30` (seconds) | Audit-trail narration. |

## Browser app (build time)

| Variable | Notes |
|---|---|
| `VITE_API_BASE_URL` | Set to `/api` in production so the app calls the API same-origin through nginx (no CORS needed). Defaults to `http://localhost:8000`. |
| `VITE_KEYCLOAK_URL`, `VITE_KEYCLOAK_REALM`, `VITE_KEYCLOAK_CLIENT_ID` | Keycloak details for the browser login. |
