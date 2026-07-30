# CI/CD Setup — deploy to the Ubuntu VM

The pipeline SSHes into the VM on every push to **`LangGraph-1`** (or a manual
run), pulls the repo, writes `services/api/.env` from GitHub secrets/variables,
and runs the idempotent `scripts/deploy_docker.sh` (build images → infra →
migrations → seeds → app containers → health check). Everything runs in Docker;
see [DOCKER-DEPLOY.md](DOCKER-DEPLOY.md) for the container layout.

## 1. One-time manual clone on the VM (required)

This is a **private** repo and no GitHub token is stored on the VM, so the
pipeline only does `git pull`. Clone it **once** as the deploy user (this also
caches the git credentials the later pulls reuse):

```bash
git clone https://github.com/sharpmindlabs/LangGraph_Durable_Workflow_Engine.git \
  ~/LangGraph_Durable_Workflow_Engine
```

If the directory is missing, the pipeline stops with a clear message pointing here.

The deploy user must be **sudo-capable** (systemd units, nginx, `/var/www`). The
VM already has: docker, docker compose, uv, node20/npm, git, nginx.

## 2. GitHub repo configuration

Settings → Secrets and variables → Actions.

### Secrets (Settings → Secrets)
| Secret | Meaning |
|---|---|
| `VM_SSH_KEY` | **Private** SSH key (PEM) whose public half is in the VM user's `~/.ssh/authorized_keys`. |
| `GMAIL_APP_PASSWORD` | Gmail app password for the notifier mailbox. |

### Variables (Settings → Variables)
| Variable | Example | Meaning |
|---|---|---|
| `VM_HOST` | `203.0.113.10` | VM public host/IP. Used for SSH, the SPA's Keycloak URL, and Keycloak redirect URIs. |
| `VM_USER` | `ubuntu` | SSH / deploy user (sudo-capable). |
| `GMAIL_ADDRESS` | `forpersonal002@gmail.com` | From-address for outbound mail. |
| `NOTIFY_TO` | `realgowtham2005@gmail.com` | Fallback notification recipient. |

> The remaining `.env` values (LLM model, DB URL, Temporal/Keycloak addresses,
> Keycloak admin creds) are **fixed in the workflow** — edit
> `.github/workflows/deploy.yml` if they ever change. `.env` is written on the VM
> and **never committed**.

## 3. Triggering a deploy

- **Automatic:** push to `LangGraph-1`.
- **Manual:** Actions → **deploy** → **Run workflow** (`workflow_dispatch`).

Deploys are serialized (`concurrency: deploy-vm`); the LLM model pull on first run
can take several minutes (job timeout is 30 min).

## 4. What gets deployed

| Piece | Where | How it runs |
|---|---|---|
| SPA | `http://VM_HOST/` | nginx `:80` serving `/var/www/workflow-spa` |
| API | `http://VM_HOST/api/` → `127.0.0.1:8000` | systemd `workflow-api` (uvicorn) |
| Worker | — | systemd `workflow-worker` (Temporal) |
| Email adapter | — | systemd `workflow-adapter` (IMAP poller) |
| Keycloak | `http://VM_HOST:8081/` | docker compose (realm `workflow`, client `workflow-spa`) |
| Infra | postgres `5432`, temporal `7233`, temporal-ui `8080`, ollama `11434`, langfuse `3001` | docker compose |

**Public-URL wiring decision:** the SPA calls the **API same-origin** through
nginx (`VITE_API_BASE_URL=/api`) — no CORS needed. It talks to **Keycloak
directly** at `http://VM_HOST:8081` (`VITE_KEYCLOAK_URL`). Keycloak is *not*
proxied under `/auth/` because a path prefix requires
`KC_HTTP_RELATIVE_PATH=/auth`, which would break the API's server-side JWKS URL
(`http://localhost:8081/realms/…`), the admin-REST paths, and the login page's
`/resources/` links. The API does not verify the token issuer, so direct access
is fully compatible. The web container's nginx config is
`deploy/docker/web-nginx.conf`.

## 5. Checking logs / health on the VM

```bash
cd ~/LangGraph_Durable_Workflow_Engine

# app containers (api, worker, adapter, web)
docker compose -f docker-compose.dev.yml -f docker-compose.app.yml ps
docker compose -f docker-compose.dev.yml -f docker-compose.app.yml logs -f api
docker compose -f docker-compose.dev.yml -f docker-compose.app.yml logs -n 200 worker
docker compose -f docker-compose.dev.yml -f docker-compose.app.yml logs -f adapter

# infra
docker compose -f docker-compose.dev.yml logs -f keycloak

# quick smoke
curl -s localhost/api/health          # through the web container's nginx
curl -s -o /dev/null -w '%{http_code}\n' localhost:8081/realms/master
```

## 6. Re-running / idempotency

`scripts/deploy_docker.sh` is safe to run by hand at any time:

```bash
cd ~/LangGraph_Durable_Workflow_Engine && VM_HOST=<host> bash scripts/deploy_docker.sh
```

Seeds are check-then-create (PDD upsert; Keycloak realm/client/roles/users),
systemd units are rewritten + restarted, the SPA is rebuilt, and nginx is
reloaded. Nothing errors on repeat runs.

## 7. Demo users (seeded)

All passwords `12345`. Roles: `ap_manager`, `finance`, `ops_admin`,
`process_author`, `vendor` (no `ap_clerk`).

| User | Role | Email |
|---|---|---|
| `manager1` | ap_manager | — |
| `finance1`, `finance2` | finance | — |
| `author1` | process_author | — |
| `vendor_acme` | vendor | og.gowtham.sk@gmail.com |
| `vendor_globex` | vendor | zencoderku001@gmail.com |
