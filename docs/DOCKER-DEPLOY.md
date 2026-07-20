# Dockerized Deployment (branch `LangGraph-2`)

This branch moves the **app tier** (API, Temporal worker, email adapter, SPA) off
host **systemd + host nginx** and into **Docker containers**, matching the build
guide's "Dockerize api, worker, web, and the email-ingest adapter" step. Infra
(Postgres, Temporal, Keycloak, Ollama, Langfuse) already ran in Docker and is
unchanged.

> Your source code is **not** deleted. The same `services/api`, `services/worker`,
> and `services/spa` code is now *built into images* instead of run directly.

## What changed in this branch

New files:

- `deploy/docker/backend.Dockerfile` — one image for api + worker + adapter (shared code/deps).
- `deploy/docker/web.Dockerfile` — builds the Vite SPA, serves it with nginx, proxies `/api/`.
- `deploy/docker/web-nginx.conf` — the web container's nginx (proxies to `api:8000`).
- `docker-compose.app.yml` — overlay adding `api`, `worker`, `adapter`, `migrate`, `web`.
- `scripts/docker_bootstrap.sh` — one-shot: waits for infra, runs migrations + seeds.
- `scripts/deploy_docker.sh` — build + up + health check (replaces `deploy_vm.sh`).
- `.dockerignore`.

Code made container-ready (still defaults to localhost, so systemd keeps working):

- `services/api/app/main.py` — DB, Temporal, and Keycloak JWKS URLs now read env.
- `services/worker/main.py` — Temporal address reads `TEMPORAL_ADDRESS`.
- `services/worker/activities/invoice_activities.py` — DB URL reads `DATABASE_URL`.
- `services/api/migrations/env.py` and `scripts/seed_pdd.py` — DB URL reads `DATABASE_URL`.

Compose sets the container env to the docker **service names** (`postgres`,
`temporal`, `keycloak`, `ollama`); the browser-facing Keycloak URL stays the
public `http://<VM_HOST>:8081`.

## Deploy on the VM

**1. One-time — stop the old host services** so the containers can take ports 80/8000:

```bash
sudo systemctl disable --now workflow-api workflow-worker workflow-adapter nginx
```

**2. Make sure `services/api/.env` exists** (CI writes it; for a hand run, copy your
existing one — its localhost values are overridden by compose, secrets are kept).

**3. Build and start:**

```bash
cd ~/LangGraph_Durable_Workflow_Engine
git fetch origin && git checkout LangGraph-2 && git pull --ff-only origin LangGraph-2
VM_HOST=4.240.101.91 bash scripts/deploy_docker.sh
```

The `migrate` container runs first (migrations + PDD seed + Keycloak seed), then
`api`, `worker`, `adapter`, and `web` start. `docker ps` will now show those five
app containers alongside the infra ones.

**4. Open** `http://4.240.101.91/` (incognito) and log in (`manager1` / `12345`, etc.).

Ports 80 and 8081 are already open in your NSG — no new firewall rules needed.

## Editing code afterward

Same source files, one extra step (image rebuild):

1. Edit code in `services/...`.
2. `git commit` + `git push`.
3. Redeploy: `VM_HOST=<host> bash scripts/deploy_docker.sh` (rebuilds changed
   images and restarts). For one service: `docker compose -f docker-compose.dev.yml
   -f docker-compose.app.yml up -d --build api`.

For rapid local iteration you can bind-mount the source with `uvicorn --reload`
(a dev compose override) so edits apply without a rebuild — ask if you want that.

## CI/CD

`.github/workflows/deploy.yml` still runs `scripts/deploy_vm.sh` (systemd). To
switch the pipeline to Docker, change that one line to `scripts/deploy_docker.sh`.
The `.env` it writes needs no changes — compose overrides the host addresses.

## Rollback to systemd

```bash
docker compose -f docker-compose.dev.yml -f docker-compose.app.yml down
sudo systemctl enable --now workflow-api workflow-worker workflow-adapter nginx
```

(Infra keeps running either way.)

## Known follow-ups (not addressed here)

- Keycloak still uses in-memory H2 — give it a Postgres DB so realms/users persist.
- Everything is HTTP; move to HTTPS for production and re-enable PKCE S256.
