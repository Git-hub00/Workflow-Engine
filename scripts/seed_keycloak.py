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
#   KEYCLOAK_SSL_REQUIRED    realm SSL policy (dev HTTP)      (none)
#   VM_HOST                  public host for redirect URIs   (localhost)
#   SEED_USER_PASSWORD       password set for all demo users (12345)

import os
import sys

import requests

KC = os.getenv("KEYCLOAK_URL", "http://localhost:8081").rstrip("/")
REALM = os.getenv("KEYCLOAK_REALM", "workflow")
ADMIN = os.getenv("KEYCLOAK_ADMIN", "admin")
ADMIN_PW = os.getenv("KEYCLOAK_ADMIN_PASSWORD", "admin")
SSL_REQUIRED = os.getenv("KEYCLOAK_SSL_REQUIRED", "none").lower()
VM_HOST = os.getenv("VM_HOST", "localhost")
USER_PW = os.getenv("SEED_USER_PASSWORD", "12345")

CLIENT_ID = "workflow-spa"

# Core app roles only. Business roles (manager, finance, vendor, …) are created by
# the admin in-app per the processes they build — not seeded here.
ROLES = ["ops_admin", "process_author"]

# Demo users -> (realm roles, optional email). Password is USER_PW for all.
USERS = {
    # The engine starts with just an ADMIN and an AUTHOR. The admin manages roles
    # and users; the author builds workflows. Business roles/users are created by
    # the admin in-app — nothing invoice-specific is seeded.
    "admin1": (["ops_admin"], "realgowtham2005@gmail.com"),
    "author1": (["process_author"], "realgowtham2005@gmail.com"),
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
    if SSL_REQUIRED not in {"none", "external", "all"}:
        raise ValueError("KEYCLOAK_SSL_REQUIRED must be one of: none, external, all")

    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {admin_token()}"
    base = f"{KC}/admin/realms"

    # Public HTTP is intentional for this start-dev deployment. KC_HTTP_ENABLED
    # opens the HTTP listener but does not override realm-level SSL enforcement,
    # so update master as well as the application realm. This is idempotent and
    # can be switched to "external"/"all" when HTTPS is introduced later.
    master = s.get(f"{base}/master", timeout=15)
    master.raise_for_status()
    master_repr = master.json()
    if master_repr.get("sslRequired") != SSL_REQUIRED:
        master_repr["sslRequired"] = SSL_REQUIRED
        s.put(f"{base}/master", json=master_repr, timeout=15).raise_for_status()
        print(f"realm: updated 'master' sslRequired={SSL_REQUIRED!r}")
    else:
        print(f"realm: 'master' sslRequired already {SSL_REQUIRED!r}")

    # --- realm (create if missing) ---
    # duplicateEmailsAllowed lets multiple accounts share one inbox (handy for the
    # demo, and for business users an admin creates later). It REQUIRES
    # loginWithEmailAllowed=false, so users log in by username.
    realm_flags = {"sslRequired": SSL_REQUIRED, "loginWithEmailAllowed": False,
                   "duplicateEmailsAllowed": True}
    realm_response = s.get(f"{base}/{REALM}", timeout=15)
    if realm_response.status_code == 404:
        s.post(f"{base}", json={"realm": REALM, "enabled": True, **realm_flags}, timeout=15).raise_for_status()
        print(f"realm: created {REALM!r}")
    else:
        realm_response.raise_for_status()
        realm_repr = {**realm_response.json(), **realm_flags}
        s.put(f"{base}/{REALM}", json=realm_repr, timeout=15).raise_for_status()
        print(f"realm: updated {REALM!r} (ssl + duplicate-emails)")

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
        # PKCE S256 enforcement is disabled: the SPA is served over plain HTTP on a
        # bare IP (no secure context => no crypto.subtle to compute the S256
        # challenge). Empty string = do not require PKCE. Restore "S256" once the
        # SPA is served over HTTPS (see services/spa/src/main.jsx pkceMethod).
        "attributes": {"pkce.code.challenge.method": ""},
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
