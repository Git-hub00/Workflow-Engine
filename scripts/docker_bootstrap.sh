#!/usr/bin/env sh
# One-shot bootstrap run by the `migrate` compose service before the app starts.
# Self-contained: waits for Postgres and Keycloak (using libraries already in the
# backend image), applies DB migrations, then seeds the PDD and Keycloak. Safe to
# re-run — Alembic and both seed scripts are idempotent.
set -eu

echo "==> waiting for Postgres ($DATABASE_URL)..."
python - <<'PY'
import os, time, sqlalchemy
url = os.environ["DATABASE_URL"]
for _ in range(60):
    try:
        sqlalchemy.create_engine(url).connect().close()
        print("    Postgres ready"); break
    except Exception:
        time.sleep(2)
else:
    raise SystemExit("ERROR: Postgres not ready after 120s")
PY

echo "==> alembic upgrade head"
cd /app/services/api && alembic upgrade head

echo "==> seeding PDD (definitions/invoice.pdd.json)"
python /app/scripts/seed_pdd.py

echo "==> waiting for Keycloak (${KEYCLOAK_URL:-http://keycloak:8080})..."
python - <<'PY'
import os, time, requests
base = os.environ.get("KEYCLOAK_URL", "http://keycloak:8080").rstrip("/")
for _ in range(90):
    try:
        if requests.get(base + "/realms/master", timeout=5).status_code == 200:
            print("    Keycloak ready"); break
    except Exception:
        pass
    time.sleep(2)
else:
    raise SystemExit("ERROR: Keycloak not ready after 180s")
PY

echo "==> seeding Keycloak (realm, client, roles, demo users)"
python /app/scripts/seed_keycloak.py

echo "==> bootstrap complete"
