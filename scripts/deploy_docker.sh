#!/usr/bin/env bash
# scripts/deploy_docker.sh — THE deploy script (it replaced an older systemd
# path, which has since been removed). Builds and starts the full stack — infra + app tier
# (api, worker, adapter, migrate, web) — with Docker Compose. Idempotent: safe to
# run repeatedly. Run on the VM (or from CI over SSH).
#
#   VM_HOST=<public-ip-or-host> bash scripts/deploy_docker.sh
#
# Prereqs on the VM: docker + docker compose, the repo cloned, and
# services/api/.env present (CI writes it; for a hand run, create it once).
set -Eeuo pipefail
trap 'rc=$?; echo "DEPLOY FAILED at line ${LINENO}: ${BASH_COMMAND} (exit ${rc})" >&2' ERR

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
export VM_HOST="${VM_HOST:-localhost}"

# Use sudo for docker only if the current user can't talk to the daemon directly.
DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then DOCKER=(sudo docker); fi
COMPOSE=("${DOCKER[@]}" compose -f docker-compose.dev.yml -f docker-compose.app.yml)

echo "==> deploy_docker.sh  APP_DIR=$APP_DIR  VM_HOST=$VM_HOST"

[ -f services/api/.env ] || { echo "ERROR: services/api/.env missing (CI writes it; create it for hand runs)"; exit 1; }

# Pre-flight: warn about anything still holding the ports the containers need.
if systemctl is-active --quiet nginx 2>/dev/null; then
  echo "WARN: host nginx is active and will conflict with the web container on :80."
  echo "      Disable it first:  sudo systemctl disable --now nginx"
fi
for svc in workflow-api workflow-worker workflow-adapter; do
  if systemctl is-active --quiet "$svc" 2>/dev/null; then
    echo "WARN: systemd $svc is still running (old deploy). Disable it:"
    echo "      sudo systemctl disable --now workflow-api workflow-worker workflow-adapter"
  fi
done

echo "==> building images"
"${COMPOSE[@]}" build

echo "==> starting stack (migrate runs once, then api/worker/adapter/web)"
"${COMPOSE[@]}" up -d

echo "==> waiting for the app to answer through nginx..."
ok=0
for i in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost/api/health" || true)"
  if [ "$code" = "200" ]; then echo "    API healthy via web (:80 -> /api/health)"; ok=1; break; fi
  sleep 3
done
[ "$ok" = 1 ] || echo "WARN: /api/health not 200 yet — check: ${COMPOSE[*]} logs migrate api"

echo "==> current state:"
"${COMPOSE[@]}" ps

cat <<SUMMARY

============================================================
DOCKER DEPLOY OK  (host: $VM_HOST)
------------------------------------------------------------
App (SPA)  : http://$VM_HOST/           web container (nginx :80)
API        : http://$VM_HOST/api/  ->  api:8000 (internal)
Keycloak   : http://$VM_HOST:8081/      (realm=workflow, client=workflow-spa)
Infra      : postgres temporal temporal-ui(:8080) ollama(:11434) langfuse(:3001)
------------------------------------------------------------
Logs:  ${COMPOSE[*]} logs -f api worker adapter
============================================================
SUMMARY
