# WHY this file exists:
# Seed (publish) the invoice-approval Process Definition Document (PDD) into the
# workflow_app database as a *published* definition_version. This is how a
# process definition authored as JSON becomes an executable, versioned record
# the rest of the platform (API, worker, monitor) can load. The script is
# deliberately IDEMPOTENT so it can be run repeatedly (dev bootstrap, CI seed,
# re-publish after editing the JSON) without ever violating the
# UNIQUE(definition_id, version) constraint or creating duplicates.

import json
import os
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text

# Env-driven so the seed runs against localhost (host/systemd) or the Docker
# service name (compose sets DATABASE_URL=...@postgres:5432/...).
DB_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://app:app@localhost:5432/workflow_app")

# WHY resolve the path relative to __file__: the script is invoked from the
# services/api directory (via `uv run --directory services\api ...`), so the
# current working directory is NOT the project root. Anchoring to this file's
# location makes it find definitions/invoice.pdd.json regardless of CWD.
PDD_PATH = Path(__file__).resolve().parent.parent / "definitions" / "invoice.pdd.json"


def main():
    # STEP 1 — load and parse the PDD JSON.
    # WHY read it first: we need the process_key and version to decide what to
    # upsert, and the full document to store as the pdd payload.
    doc = json.loads(PDD_PATH.read_text(encoding="utf-8"))
    process_key = doc["process_key"]
    version = doc["version"]
    # WHY serialize the whole doc: the pdd column stores the COMPLETE definition
    # as jsonb, not just a subset — so an executing transaction can read every
    # config/role value back later.
    pdd_json = json.dumps(doc)
    print(f"Loaded PDD from {PDD_PATH}")
    print(f"  process_key = {process_key!r}, version = {version}")

    engine = create_engine(DB_URL)

    # STEP 2 — perform all writes in ONE transaction.
    # WHY a single `with engine.begin()`: ensuring the process_definition and its
    # definition_version are created/updated atomically — either both land or
    # neither does, so we never leave a half-published definition behind.
    with engine.begin() as conn:
        # STEP 2a — ensure the process_definition exists (idempotent).
        # WHY select-then-insert on process_key: process_key is the stable
        # business identity. If it already exists we reuse its id; only if it is
        # missing do we create it. (process_key has no UNIQUE constraint in the
        # schema, so we guard against duplicates explicitly here.)
        definition_id = conn.execute(
            text("SELECT id FROM process_definition WHERE process_key = :pk"),
            {"pk": process_key},
        ).scalar_one_or_none()

        if definition_id is None:
            definition_id = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO process_definition (id, process_key) "
                    "VALUES (CAST(:id AS uuid), :pk)"
                ),
                {"id": str(definition_id), "pk": process_key},
            )
            print(f"  process_definition: INSERTED new id={definition_id}")
        else:
            print(f"  process_definition: reused existing id={definition_id}")

        # STEP 2b — upsert the definition_version (idempotent on definition_id+version).
        # WHY check (definition_id, version) first: the schema enforces
        # UNIQUE(definition_id, version). If this exact version already exists we
        # UPDATE its pdd/status/published_at (re-publish) instead of inserting a
        # duplicate that would raise an IntegrityError.
        version_id = conn.execute(
            text(
                "SELECT id FROM definition_version "
                "WHERE definition_id = CAST(:def_id AS uuid) AND version = :version"
            ),
            {"def_id": str(definition_id), "version": version},
        ).scalar_one_or_none()

        if version_id is None:
            # WHY INSERT with published_at = now(): a brand-new version is being
            # published for the first time; stamp its publish time server-side.
            version_id = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO definition_version "
                    "(id, definition_id, version, pdd, status, published_at) "
                    "VALUES (CAST(:id AS uuid), CAST(:def_id AS uuid), :version, "
                    "        CAST(:pdd AS jsonb), :status, now())"
                ),
                {
                    "id": str(version_id),
                    "def_id": str(definition_id),
                    "version": version,
                    "pdd": pdd_json,
                    "status": "published",
                },
            )
            print(f"  definition_version: INSERTED new id={version_id} (v{version}, published)")
        else:
            # WHY UPDATE instead of insert: same (definition, version) already
            # present — re-publish it with the latest document/status/time.
            conn.execute(
                text(
                    "UPDATE definition_version "
                    "SET pdd = CAST(:pdd AS jsonb), status = :status, published_at = now() "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": str(version_id), "pdd": pdd_json, "status": "published"},
            )
            print(f"  definition_version: UPDATED existing id={version_id} (v{version}, re-published)")

    # STEP 3 — read the row back on a FRESH connection and prove the config is
    # persisted and readable. WHY a separate connection: querying after the
    # transaction committed confirms the data really landed in the database
    # (not just visible inside the writing transaction).
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT dv.id AS version_id, dv.definition_id AS definition_id, "
                "       dv.version AS version, dv.status AS status, dv.pdd AS pdd "
                "FROM definition_version dv "
                "WHERE dv.definition_id = CAST(:def_id AS uuid) AND dv.version = :version"
            ),
            {"def_id": str(definition_id), "version": version},
        ).mappings().one()

    # WHY pull config out of the DB-returned pdd (not the file): this demonstrates
    # the jsonb round-tripped through Postgres and the config is readable from it.
    cfg = row["pdd"]["config"]
    print("\nRead back from database:")
    print(f"  definition id (process_definition) = {row['definition_id']}")
    print(f"  version id (definition_version)    = {row['version_id']}")
    print(f"  version                            = {row['version']}")
    print(f"  status                             = {row['status']}")
    print("  config values:")
    print(f"    autoApproveUnder = {cfg['autoApproveUnder']}")
    print(f"    financeThreshold = {cfg['financeThreshold']}")
    print(f"    quorum           = {cfg['quorum']}")
    print(f"    slaHours         = {cfg['slaHours']}")

    engine.dispose()
    print("\nPDD SEEDED (published)")


if __name__ == "__main__":
    main()
