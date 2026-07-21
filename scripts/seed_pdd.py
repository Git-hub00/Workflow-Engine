# WHY this file exists:
# Seed (publish) EVERY Process Definition Document (PDD) under definitions/ into
# the workflow_app database as a published definition_version. This is how a
# process authored as JSON becomes an executable, versioned record. Idempotent:
# safe to run on every deploy (upsert on process_key+version), so dropping a new
# definitions/<name>.pdd.json file auto-publishes it — NO code change per process.
import glob
import json
import os
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text

# Env-driven so it runs against localhost (host) or the Docker service name.
DB_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://app:app@localhost:5432/workflow_app")

# definitions/ sits at the project root; this script lives in scripts/.
DEFINITIONS_DIR = Path(__file__).resolve().parent.parent / "definitions"


def seed_one(conn, doc: dict) -> None:
    process_key = doc["process_key"]
    version = doc["version"]
    pdd_json = json.dumps(doc)

    # ensure the process_definition exists (process_key is the stable identity)
    definition_id = conn.execute(
        text("SELECT id FROM process_definition WHERE process_key = :pk"),
        {"pk": process_key},
    ).scalar_one_or_none()
    if definition_id is None:
        definition_id = uuid.uuid4()
        conn.execute(
            text("INSERT INTO process_definition (id, process_key) VALUES (CAST(:id AS uuid), :pk)"),
            {"id": str(definition_id), "pk": process_key},
        )
        print(f"  process_definition: INSERTED {process_key!r}")
    else:
        print(f"  process_definition: reused {process_key!r}")

    # upsert the definition_version (idempotent on definition_id+version)
    version_id = conn.execute(
        text("SELECT id FROM definition_version "
             "WHERE definition_id = CAST(:d AS uuid) AND version = :v"),
        {"d": str(definition_id), "v": version},
    ).scalar_one_or_none()
    if version_id is None:
        conn.execute(
            text("INSERT INTO definition_version "
                 "(id, definition_id, version, pdd, status, published_at) "
                 "VALUES (CAST(:id AS uuid), CAST(:d AS uuid), :v, CAST(:pdd AS jsonb), 'published', now())"),
            {"id": str(uuid.uuid4()), "d": str(definition_id), "v": version, "pdd": pdd_json},
        )
        print(f"  definition_version: INSERTED v{version} (published)")
    else:
        conn.execute(
            text("UPDATE definition_version "
                 "SET pdd = CAST(:pdd AS jsonb), status = 'published', published_at = now() "
                 "WHERE id = CAST(:id AS uuid)"),
            {"id": str(version_id), "pdd": pdd_json},
        )
        print(f"  definition_version: UPDATED v{version} (re-published)")


def main() -> int:
    files = sorted(glob.glob(str(DEFINITIONS_DIR / "*.pdd.json")))
    if not files:
        print(f"No *.pdd.json files found in {DEFINITIONS_DIR}")
        return 0
    engine = create_engine(DB_URL)
    for path in files:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        print(f"Seeding {Path(path).name}  (process_key={doc.get('process_key')!r}, v{doc.get('version')})")
        with engine.begin() as conn:
            seed_one(conn, doc)
    engine.dispose()
    print(f"\nPDD SEED COMPLETE ({len(files)} file(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
