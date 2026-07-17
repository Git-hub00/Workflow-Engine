# WHY this file exists:
# Idempotent Keycloak bootstrap for the "workflow" realm. Creates (or updates)
# the realm, the public SPA client, the realm roles, and the demo users with
# their role assignments and passwords. Safe to run on EVERY deploy: it checks
# for each object first and only creates what is missing, and PUTs updates for
# things that may have changed (client redirect URIs / web origins).
#
# Uses only `requests` (already a dependency) against the Keycloak admin REST API
# with the master-realm admin-cli token — the same pattern used elsewhere in the
# codebase (email adapter / notify).
#
# Config via env (all have sensible dev defaults):
#   KEYCLOAK_URL             base URL of Keycloak            (http://localhost:8081)
#   KEYCLOAK_REALM           realm to seed                   (workflow)
#   KEYCLOAK_ADMIN           master admin user               (admin)
#   KEYCLOAK_ADMIN_PASSWORD  master admin password           (admin)
#   VM_HOST                  public host for redirect URIs   (localhost)
#   SEED_USER_PASSWORD       password set for all demo users (12345)

import os
import sys

import requests

KC = os.getenv("KEYCLOAK_URL", "http://localhost:8081").rstrip("/")
REALM = os.getenv("KEYCLOAK_REALM", "workflow")
ADMIN = os.getenv("KEYCLOAK_ADMIN", "admin")
ADMIN_PW = os.getenv("KEYCLOAK_ADMIN_PASSWORD", "admin")
VM_HOST = os.getenv("VM_HOST", "localhost")
USER_PW = os.getenv("SEED_USER_PASSWORD", "12345")

CLIENT_ID = "workflow-spa"

# Roles for the realm. NOTE: ap_clerk is intentionally ABSENT — request_info was
# reassigned to the "vendor" role.
ROLES = ["ap_manager", "finance", "ops_admin", "process_author", "vendor"]

# Demo users -> (realm roles, optional email). Password is USER_PW for all.
USERS = {
    "manager1": (["ap_manager"], None),
    "finance1": (["finance"], None),
    "finance2": (["finance"], None),
    "author1": (["process_author"], None),
    "vendor_acme": (["vendor"], "og.gowtham.sk@gmail.com"),
    "vendor_globex": (["vendor"], "zencoderku001@gmail.com"),
}

# Public browser origins the SPA is served from (redirect URIs + web origins).
REDIRECT_URIS = [f"http://{VM_HOST}/*", "http://localhost:5173/*"]
WEB_ORIGINS = [f"http://{VM_HOST}", "http://localhost:5173", "+"]


def admin_token() -> str:
    r = requests.post(
        f"{KC}/realms/master/protocol/openid-connect/token",
        data={
            "client_id": "admin-cli",
            "grant_type": "password",
            "username": ADMIN,
            "password": ADMIN_PW,
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def main() -> int:
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {admin_token()}"
    base = f"{KC}/admin/realms"

    # --- realm (create if missing) ---
    if s.get(f"{base}/{REALM}", timeout=15).status_code == 404:
        s.post(f"{base}", json={"realm": REALM, "enabled": True}, timeout=15).raise_for_status()
        print(f"realm: created {REALM!r}")
    else:
        print(f"realm: {REALM!r} already exists")

    realm_base = f"{base}/{REALM}"

    # --- client workflow-spa (public + PKCE; create or update URIs) ---
    existing = s.get(f"{realm_base}/clients", params={"clientId": CLIENT_ID}, timeout=15).json()
    client_repr = {
        "clientId": CLIENT_ID,
        "protocol": "openid-connect",
        "publicClient": True,
        "standardFlowEnabled": True,
        "directAccessGrantsEnabled": True,  # password grant for scripted tests
        "redirectUris": REDIRECT_URIS,
        "webOrigins": WEB_ORIGINS,
        "attributes": {"pkce.code.challenge.method": "S256"},
    }
    if existing:
        cid = existing[0]["id"]
        merged = {**existing[0], **client_repr}
        s.put(f"{realm_base}/clients/{cid}", json=merged, timeout=15).raise_for_status()
        print(f"client: updated {CLIENT_ID!r} (redirect/web-origins refreshed)")
    else:
        s.post(f"{realm_base}/clients", json=client_repr, timeout=15).raise_for_status()
        print(f"client: created {CLIENT_ID!r}")

    # --- realm roles (create missing) ---
    for role in ROLES:
        if s.get(f"{realm_base}/roles/{role}", timeout=15).status_code == 404:
            s.post(f"{realm_base}/roles", json={"name": role}, timeout=15).raise_for_status()
            print(f"role: created {role!r}")
        else:
            print(f"role: {role!r} already exists")

    # --- users (create missing), then ensure password + role mappings each run ---
    for username, (roles, emailaddr) in USERS.items():
        found = s.get(f"{realm_base}/users", params={"username": username, "exact": "true"}, timeout=15).json()
        if found:
            uid = found[0]["id"]
            print(f"user: {username!r} already exists")
        else:
            payload = {
                "username": username,
                "enabled": True,
                "firstName": username,
                "lastName": "User",
            }
            if emailaddr:
                payload["email"] = emailaddr
                payload["emailVerified"] = True
            r = s.post(f"{realm_base}/users", json=payload, timeout=15)
            r.raise_for_status()
            uid = s.get(
                f"{realm_base}/users", params={"username": username, "exact": "true"}, timeout=15
            ).json()[0]["id"]
            print(f"user: created {username!r}")

        # password (idempotent — reset to the known dev password every run)
        s.put(
            f"{realm_base}/users/{uid}/reset-password",
            json={"type": "password", "value": USER_PW, "temporary": False},
            timeout=15,
        ).raise_for_status()

        # role mappings (add any missing; Keycloak ignores already-assigned)
        assign = []
        for role in roles:
            rr = s.get(f"{realm_base}/roles/{role}", timeout=15)
            if rr.status_code == 200:
                assign.append({"id": rr.json()["id"], "name": role})
        if assign:
            s.post(
                f"{realm_base}/users/{uid}/role-mappings/realm", json=assign, timeout=15
            ).raise_for_status()

    print("KEYCLOAK SEEDED (realm, client, roles, users)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
