# Production Hardening (P7)

Status of each spec "non-functional / parity" item, and how to enable the ones
that are infra changes. Items marked **ready-to-enable** are deliberately NOT
switched on in the running demo (they change infra that would need on-VM testing
and could disrupt the working HTTP demo); the configs are provided so you can
enable them deliberately.

| Item | Status |
|---|---|
| Version pinning | ✅ Already satisfied (see §1) |
| Data retention | ✅ Implemented — `scripts/retention.py` (§2) |
| HTTPS / TLS | 🟡 Ready-to-enable — Caddy config provided (§3) |
| Keycloak on Postgres (persistence) | 🟡 Ready-to-enable — compose snippet (§4) |
| Business-calendar timers | ⏳ Designed / deferred (§5) |
| Supervised version migration | ⏳ Designed / deferred (§6) |

## 1. Version pinning (done)

Each transaction stores `definition_version_id` and is started with the exact PDD
resolved at start time (`POST /v1/transactions` passes that PDD to the
interpreter). In-flight transactions therefore keep the rules they started with;
publishing a new version only affects *new* transactions. No action needed.

## 2. Data retention (`scripts/retention.py`)

Per-process `process_definition.retention_policy` (JSONB) merged over an SMB
default (hot 12mo → archive 12mo → purge 7yr). The tool reports purge-due closed
transactions and, with `--apply`, deletes them and their children (FK-safe),
skipping any under `legal_hold`.

```bash
# safe report (no changes)
docker exec langgraph_durable_workflow_engine-worker-1 python /app/scripts/retention.py
# actually purge purge-due transactions
docker exec langgraph_durable_workflow_engine-worker-1 python /app/scripts/retention.py --apply
```

Schedule it (e.g. monthly) with cron on the VM or a scheduled task. Archive-to-
cold-storage is left as a follow-up; purge is the destructive step and is guarded
(dry-run by default).

## 3. HTTPS / TLS (ready-to-enable) — `deploy/caddy/Caddyfile`

Everything currently runs over HTTP; that's why the SPA has the `crypto.randomUUID`
polyfill and PKCE is disabled. Moving to HTTPS lets you remove both.

Enable with **Caddy** (automatic Let's Encrypt) using a free **nip.io** hostname
(no domain purchase). For VM IP `4.240.101.91`:

1. Open **443** in the Azure NSG.
2. Add a `caddy` service to compose (owns 80+443), on the same network, with:
   `APP_HOST=app.4.240.101.91.nip.io`, `KC_HOST=kc.4.240.101.91.nip.io`, and a
   volume for its cert store. Stop publishing `web`'s port 80 directly (Caddy
   fronts it).
3. Rebuild the SPA with HTTPS URLs: `VITE_KEYCLOAK_URL=https://kc.4.240.101.91.nip.io`
   and serve it at `https://app.4.240.101.91.nip.io`.
4. Set Keycloak `KC_HOSTNAME=https://kc.4.240.101.91.nip.io`.
5. **Re-enable PKCE** (`pkceMethod: 'S256'` in `services/spa/src/main.jsx`, and
   `"pkce.code.challenge.method": "S256"` in `scripts/seed_keycloak.py`) and you
   may drop `src/polyfills.js`.

Why both hosts need TLS: once the SPA is HTTPS, its background calls to Keycloak
must also be HTTPS or the browser blocks them as mixed content.

## 4. Keycloak on Postgres (ready-to-enable)

Keycloak currently runs on in-memory **H2**, so realms/users are recreated by the
seed on each deploy. To persist them, give Keycloak a Postgres database.

1. Create a `keycloak` database (once): 
   `docker exec langgraph_durable_workflow_engine-postgres-1 psql -U app -c "CREATE DATABASE keycloak;"`
2. In `docker-compose.dev.yml`, add to the `keycloak` service environment:
   ```yaml
   KC_DB: postgres
   KC_DB_URL: jdbc:postgresql://postgres:5432/keycloak
   KC_DB_USERNAME: app
   KC_DB_PASSWORD: app
   ```
3. Redeploy. Keycloak creates its schema on first start; the seed remains
   idempotent. Realms/users now survive restarts.

(Test on the VM — a misconfigured KC_DB will stop Keycloak from starting, which is
why this isn't switched on automatically.)

## 5. Business-calendar timers (deferred)

Today SLA timers use plain hours (`slaHours`). The `business_calendar` concept
(working hours + holiday sets, referenced by name from a node `timeout.calendar`)
needs: a `business_calendar` table, an admin UI to edit it, and calendar-aware
duration math in the interpreter before starting the Temporal timer. Designed in
`docs/REFACTOR-PLAN.md` §5; not yet implemented.

## 6. Supervised version migration (deferred)

Version pinning (§1) is done. Actively migrating *parked* in-flight transactions
from an old node id to a new one (an audited admin action) is the remaining piece;
Temporal patching / Worker Versioning covers interpreter-code changes.
