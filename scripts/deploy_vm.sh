#!/usr/bin/env bash
# scripts/deploy_vm.sh — idempotent, run ON the Ubuntu VM by the CI pipeline
# (or by hand). Brings up infra (docker compose), the Python venv + DB migrations,
# seeds the PDD and Keycloak, (re)installs the systemd app services, builds the
# SPA, and configures nginx. Safe to run repeatedly.
#
# Prereqs already on the VM: docker, docker compose, uv, node20/npm, git, nginx,
# and a sudo-capable user. The repo must already be cloned (the pipeline pulls).
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
set -Eeuo pipefail
trap 'rc=$?; echo "DEPLOY FAILED at line ${LINENO}: ${BASH_COMMAND} (exit ${rc})" >&2' ERR

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
# Absolute -f so compose works regardless of the current directory (we cd around).
COMPOSE=(sudo docker compose -f "$APP_DIR/docker-compose.dev.yml")
RUN_USER="$(whoami)"
UV_BIN="$HOME/.local/bin/uv"
VM_HOST="${VM_HOST:-localhost}"     # exported by the pipeline; defaults for manual runs
echo "==> deploy_vm.sh  APP_DIR=$APP_DIR  USER=$RUN_USER  VM_HOST=$VM_HOST  uv=$UV_BIN"

# Fail early with a useful message if the non-interactive SSH environment cannot
# resolve a required host tool. `sudo docker` avoids relying on a freshly applied
# docker-group membership in the CI SSH session.
[ -x "$UV_BIN" ] || { echo "ERROR: uv is not executable at $UV_BIN" >&2; exit 1; }
required_tools=(cat chmod cp curl dirname docker git grep ln mkdir nginx node npm rm seq sleep sudo systemctl tee wc whoami)
for tool in "${required_tools[@]}"; do
  command -v "$tool" >/dev/null || { echo "ERROR: required tool '$tool' is not on PATH=$PATH" >&2; exit 1; }
done
sudo docker compose version >/dev/null

# --- 1. refresh checkout (idempotent; pipeline already pulled, kept for hand-runs)
git fetch origin LangGraph-1
git checkout LangGraph-1
git pull --ff-only origin LangGraph-1

# --- 2. infra up
"${COMPOSE[@]}" up -d
echo "==> compose services requested"

# --- 3. wait for Postgres + Keycloak (timeout ~180s each)
echo "==> waiting for Postgres..."
for i in $(seq 1 60); do
  if "${COMPOSE[@]}" exec -T postgres pg_isready -U app -d workflow_app >/dev/null 2>&1; then
    echo "    Postgres ready (${i}x3s)"; break
  fi
  [ "$i" = 60 ] && { echo "ERROR: Postgres not ready after 180s"; exit 1; }
  sleep 3
done

echo "==> waiting for Keycloak..."
for i in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8081/realms/master || true)"
  if [ "$code" = "200" ]; then echo "    Keycloak ready (${i}x3s)"; break; fi
  [ "$i" = 60 ] && { echo "ERROR: Keycloak not ready after 180s (last=$code)"; exit 1; }
  sleep 3
done

# --- 4. ensure the LLM model is present (pull once; big download tolerated)
if "${COMPOSE[@]}" exec -T ollama ollama list | grep -q 'llama3.2:3b'; then
  echo "==> ollama model llama3.2:3b already present"
else
  echo "==> pulling ollama model llama3.2:3b (first run only)..."
  "${COMPOSE[@]}" exec -T ollama ollama pull llama3.1:8b
fi

# --- 5. Python env + DB migrations (from services/api)
cd "$APP_DIR/services/api"
[ -d .venv ] || "$UV_BIN" venv
"$UV_BIN" pip install -r requirements.txt
"$UV_BIN" run alembic upgrade head
echo "==> migrations applied"

# --- 6. seed the PDD (idempotent) — publishes definitions/invoice.pdd.json
"$UV_BIN" run python ../../scripts/seed_pdd.py

# belt-and-suspenders: force roles.review = "vendor" on the published version
# (no-op if the PDD JSON already carries it, which it does).
"${COMPOSE[@]}" exec -T postgres \
  psql -U app -d workflow_app -c \
  "UPDATE definition_version dv SET pdd = jsonb_set(pdd,'{roles,review}','\"vendor\"') FROM process_definition pd WHERE pd.id = dv.definition_id AND pd.process_key = 'invoice_approval';" \
  >/dev/null && echo "==> ensured roles.review=vendor in DB"

# --- 7. seed Keycloak (idempotent). Load KEYCLOAK_* from the written .env and
#        pass VM_HOST so the SPA client's redirect URIs use the public host.
set -a; . "$APP_DIR/services/api/.env"; set +a
VM_HOST="$VM_HOST" "$UV_BIN" run python ../../scripts/seed_keycloak.py

# --- 8. systemd app services (write units every run -> idempotent + picks up
#        path changes; then daemon-reload, enable, restart).
API_SVC=/etc/systemd/system/workflow-api.service
WORKER_SVC=/etc/systemd/system/workflow-worker.service
ADAPTER_SVC=/etc/systemd/system/workflow-adapter.service
WD="$APP_DIR/services/api"
ENVF="$APP_DIR/services/api/.env"
SVC_PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

write_unit() {
  # $1=path  $2=description  $3=ExecStart command (relative to WorkingDirectory)
  sudo tee "$1" >/dev/null <<UNIT
[Unit]
Description=$2
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$WD
EnvironmentFile=$ENVF
Environment=HOME=$HOME
Environment=PATH=$SVC_PATH
ExecStart=$3
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
}

write_unit "$API_SVC"     "Workflow API (FastAPI/uvicorn)"        "$UV_BIN run uvicorn app.main:app --host 0.0.0.0 --port 8000"
write_unit "$WORKER_SVC"  "Workflow Temporal worker"              "$UV_BIN run python ../worker/main.py"
write_unit "$ADAPTER_SVC" "Workflow email adapter (IMAP poller)"  "$UV_BIN run python ../worker/notifier/email_adapter.py"

sudo systemctl daemon-reload
for svc in workflow-api workflow-worker workflow-adapter; do
  sudo systemctl enable "$svc" >/dev/null 2>&1 || true
  sudo systemctl restart "$svc"
  echo "==> $svc $(systemctl is-active "$svc")"
done

# --- 9. SPA: write build-time config, build, publish to nginx docroot.
cd "$APP_DIR/services/spa"
cat > .env.production <<SPAENV
VITE_API_BASE_URL=/api
VITE_KEYCLOAK_URL=http://${VM_HOST}:8081
VITE_KEYCLOAK_REALM=workflow
VITE_KEYCLOAK_CLIENT_ID=workflow-spa
SPAENV
if [ -f package-lock.json ]; then npm ci; else npm install; fi
npm run build
sudo mkdir -p /var/www/workflow-spa
sudo rm -rf /var/www/workflow-spa/*
sudo cp -r dist/. /var/www/workflow-spa/
echo "==> SPA built and published to /var/www/workflow-spa"

# --- 10. nginx site (install + enable + reload)
sudo cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/workflow-spa
sudo ln -sf /etc/nginx/sites-available/workflow-spa /etc/nginx/sites-enabled/workflow-spa
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
echo "==> nginx reloaded"

# --- 11. success summary
cat <<SUMMARY

============================================================
DEPLOY OK  (host: $VM_HOST)
------------------------------------------------------------
SPA        : http://$VM_HOST/            (nginx :80 -> /var/www/workflow-spa)
API        : http://$VM_HOST/api/  ->  127.0.0.1:8000   [systemd workflow-api]
Keycloak   : http://$VM_HOST:8081/       (realm=workflow, client=workflow-spa)
Worker     : systemd workflow-worker  ($(systemctl is-active workflow-worker))
Adapter    : systemd workflow-adapter ($(systemctl is-active workflow-adapter))
Infra      : postgres:5432 temporal:7233 temporal-ui:8080 ollama:11434 langfuse:3001
LLM model  : llama3.2:3b
============================================================
SUMMARY
