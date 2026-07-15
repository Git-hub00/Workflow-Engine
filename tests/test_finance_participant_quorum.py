import asyncio
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text


ROOT = Path(__file__).resolve().parent.parent
for path in (
    ROOT / "services" / "worker" / "activities",
    ROOT / "services" / "worker" / "workflows",
    ROOT / "services" / "api",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import invoice_activities  # noqa: E402
from app import main as api_main  # noqa: E402


DB_URL = "postgresql+psycopg://app:app@localhost:5432/workflow_app"


class FakeTemporal:
    def __init__(self):
        self.signals = []

    def get_workflow_handle(self, workflow_id):
        fake = self

        class Handle:
            async def signal(self, name, payload):
                fake.signals.append((workflow_id, name, payload))

        return Handle()


@pytest.fixture(scope="module")
def isolated_finance_db():
    """Use a disposable schema so focused tests never touch application rows."""
    schema = f"finance_quorum_test_{uuid.uuid4().hex}"
    admin_engine = create_engine(DB_URL)
    with admin_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.execute(
            text(
                f'CREATE TABLE "{schema}"."transaction" ('
                "id uuid PRIMARY KEY, status text NOT NULL DEFAULT 'running')"
            )
        )
        conn.execute(
            text(
                f'CREATE TABLE "{schema}".task ('
                "id uuid PRIMARY KEY, transaction_id uuid NOT NULL REFERENCES "
                f'"{schema}"."transaction"(id), node_id text, token text UNIQUE, '
                "assigned_role text, status text NOT NULL DEFAULT 'open', "
                "completion_policy jsonb, claimed_by text, created_at timestamptz DEFAULT now())"
            )
        )
        conn.execute(
            text(
                f'CREATE TABLE "{schema}".participant_task ('
                f'id uuid PRIMARY KEY, task_id uuid NOT NULL REFERENCES "{schema}".task(id), '
                "participant text, claimed_by text, decision jsonb, status text DEFAULT 'open')"
            )
        )
        conn.execute(
            text(
                f'CREATE UNIQUE INDEX uq_test_participant_claim ON "{schema}".participant_task '
                "(task_id, claimed_by) WHERE claimed_by IS NOT NULL"
            )
        )
        conn.execute(
            text(
                f'CREATE TABLE "{schema}".event ('
                "id uuid PRIMARY KEY, transaction_id uuid NOT NULL REFERENCES "
                f'"{schema}"."transaction"(id), seq bigserial, type text, payload jsonb, '
                "actor text, before_hash text, after_hash text, occurred_at timestamptz DEFAULT now())"
            )
        )
        conn.execute(
            text(
                f'CREATE TABLE "{schema}".idempotency_key ('
                f'key text PRIMARY KEY, event_id uuid REFERENCES "{schema}".event(id))'
            )
        )

    test_engine = create_engine(
        DB_URL,
        connect_args={"options": f"-c search_path={schema},public"},
    )
    original_api_engine = api_main.engine
    original_activity_engine = invoice_activities.engine
    fake_temporal = FakeTemporal()
    api_main.engine = test_engine
    invoice_activities.engine = test_engine
    api_main.app.state.temporal = fake_temporal
    try:
        yield test_engine, fake_temporal
    finally:
        api_main.engine = original_api_engine
        invoice_activities.engine = original_activity_engine
        test_engine.dispose()
        with admin_engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.fixture(autouse=True)
def clean_isolated_schema(isolated_finance_db):
    test_engine, fake_temporal = isolated_finance_db
    fake_temporal.signals.clear()
    yield
    with test_engine.begin() as conn:
        conn.execute(
            text(
                'TRUNCATE idempotency_key, event, participant_task, task, "transaction" '
                "RESTART IDENTITY CASCADE"
            )
        )


def finance_user(username):
    return {"username": username, "roles": ["finance"]}


def seed_finance_task(test_engine, required, capacity, reject_short_circuits):
    transaction_id = str(uuid.uuid4())
    with test_engine.begin() as conn:
        conn.execute(
            text('INSERT INTO "transaction" (id) VALUES (CAST(:id AS uuid))'),
            {"id": transaction_id},
        )
    policy = {
        "kind": "finance",
        "quorum": {"n": required, "of": capacity},
        "rejectShortCircuits": reject_short_circuits,
    }
    token = asyncio.run(
        invoice_activities.create_human_task(
            transaction_id,
            "finance",
            "finance",
            policy,
        )
    )
    return transaction_id, token, policy


def claim(token, username, claimed_by="browser-supplied"):
    return asyncio.run(
        api_main.claim_task(
            token,
            api_main.ClaimIn(claimed_by=claimed_by),
            finance_user(username),
        )
    )


def decide(token, username, decision, *, key=None, extra_payload=None):
    payload = {"decision": decision, **(extra_payload or {})}
    return asyncio.run(
        api_main.complete_task(
            token,
            api_main.CompleteIn(
                idempotency_key=key or str(uuid.uuid4()),
                kind="finance",
                payload=payload,
            ),
            finance_user(username),
        )
    )


def participant_count(test_engine, token):
    with test_engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT count(*) FROM participant_task pt "
                "JOIN task t ON t.id = pt.task_id WHERE t.token = :token"
            ),
            {"token": token},
        ).scalar_one()


def test_two_of_three_creates_slots_and_two_approvals_complete(isolated_finance_db):
    test_engine, fake_temporal = isolated_finance_db
    transaction_id, token, policy = seed_finance_task(test_engine, 2, 3, True)
    assert participant_count(test_engine, token) == 3

    # A Temporal activity retry reuses the parent task and does not fan out twice.
    retry_token = asyncio.run(
        invoice_activities.create_human_task(
            transaction_id, "finance", "finance", policy
        )
    )
    assert retry_token == token
    assert participant_count(test_engine, token) == 3

    claim(token, "finance1")
    claim(token, "finance2")
    assert decide(token, "finance1", "approve")["finance_status"] == "pending"
    with test_engine.connect() as conn:
        assert conn.execute(
            text("SELECT status FROM task WHERE token = :token"), {"token": token}
        ).scalar_one() == "open"
    result = decide(token, "finance2", "approve")
    assert result["finance_status"] == "approved"
    assert result["approval_count"] == 2
    with test_engine.connect() as conn:
        assert conn.execute(
            text("SELECT status FROM task WHERE token = :token"), {"token": token}
        ).scalar_one() == "done"
        assert conn.execute(
            text(
                "SELECT count(*) FROM participant_task pt JOIN task t ON t.id = pt.task_id "
                "WHERE t.token = :token AND pt.status <> 'done'"
            ),
            {"token": token},
        ).scalar_one() == 0
    assert fake_temporal.signals == [
        (transaction_id, "finance_vote", {"terminal": True, "decision": "approve"})
    ]


def test_seven_of_ten_and_eleventh_claimant(isolated_finance_db):
    test_engine, fake_temporal = isolated_finance_db
    transaction_id, token, _ = seed_finance_task(test_engine, 7, 10, False)
    assert participant_count(test_engine, token) == 10
    for index in range(10):
        claim(token, f"finance{index}")
    with pytest.raises(HTTPException) as exc:
        claim(token, "finance10")
    assert exc.value.status_code == 409

    for index in range(6):
        assert decide(token, f"finance{index}", "approve")["finance_status"] == "pending"
    result = decide(token, "finance6", "approve")
    assert result["finance_status"] == "approved"
    assert result["approval_count"] == 7
    assert fake_temporal.signals == [
        (transaction_id, "finance_vote", {"terminal": True, "decision": "approve"})
    ]


def test_reject_short_circuits(isolated_finance_db):
    test_engine, fake_temporal = isolated_finance_db
    transaction_id, token, _ = seed_finance_task(test_engine, 7, 10, True)
    claim(token, "finance1")
    result = decide(token, "finance1", "reject")
    assert result["finance_status"] == "rejected"
    assert fake_temporal.signals == [
        (transaction_id, "finance_vote", {"terminal": True, "decision": "reject"})
    ]


def test_non_short_circuit_rejects_only_when_approval_impossible(isolated_finance_db):
    test_engine, fake_temporal = isolated_finance_db
    transaction_id, token, _ = seed_finance_task(test_engine, 7, 10, False)
    for index in range(10):
        claim(token, f"finance{index}")
    for index in range(3):
        assert decide(token, f"finance{index}", "reject")["finance_status"] == "pending"
    result = decide(token, "finance3", "reject")
    assert result["finance_status"] == "rejected"
    assert result["remaining_undecided_slots"] == 6
    assert fake_temporal.signals == [
        (transaction_id, "finance_vote", {"terminal": True, "decision": "reject"})
    ]


def test_claim_identity_vote_guards_and_task_projection(isolated_finance_db):
    test_engine, _ = isolated_finance_db
    _, token, _ = seed_finance_task(test_engine, 2, 3, False)
    first = claim(token, "finance1", claimed_by="spoofed-user")
    repeated = claim(token, "finance1", claimed_by="another-spoof")
    assert repeated["participant_id"] == first["participant_id"]
    with test_engine.connect() as conn:
        claims = conn.execute(
            text(
                "SELECT claimed_by, count(*) AS count FROM participant_task "
                "WHERE claimed_by IS NOT NULL GROUP BY claimed_by"
            )
        ).mappings().all()
    assert claims == [{"claimed_by": "finance1", "count": 1}]

    with pytest.raises(HTTPException) as invalid_decision:
        decide(token, "finance1", "return")
    assert invalid_decision.value.status_code == 422

    with pytest.raises(HTTPException) as unclaimed:
        decide(token, "finance2", "approve")
    assert unclaimed.value.status_code == 409

    with pytest.raises(HTTPException) as forbidden:
        asyncio.run(
            api_main.claim_task(
                token,
                api_main.ClaimIn(claimed_by="manager1"),
                {"username": "manager1", "roles": ["ap_manager"]},
            )
        )
    assert forbidden.value.status_code == 403

    with pytest.raises(HTTPException) as direct_finance_event:
        asyncio.run(
            api_main.ingest_event(
                api_main.EventIn(
                    transaction_id=str(uuid.uuid4()),
                    task_token=token,
                    idempotency_key=str(uuid.uuid4()),
                    kind="finance",
                    payload={"participant": "spoofed-user", "decision": "approve"},
                )
            )
        )
    assert direct_finance_event.value.status_code == 400

    result = decide(
        token,
        "finance1",
        "approve",
        extra_payload={"participant": "spoofed-user"},
    )
    assert result["finance_status"] == "pending"
    with test_engine.connect() as conn:
        recorded_participant = conn.execute(
            text(
                "SELECT payload->>'participant' FROM event "
                "WHERE type = 'FINANCE_VOTE'"
            )
        ).scalar_one()
    assert recorded_participant == "finance1"

    with pytest.raises(HTTPException) as second_vote:
        decide(token, "finance1", "reject")
    assert second_vote.value.status_code == 409

    tasks = asyncio.run(
        api_main.list_tasks(role=None, status="open", user=finance_user("finance1"))
    )
    finance_task = tasks[0]
    assert finance_task["required_approvals"] == 2
    assert finance_task["participant_capacity"] == 3
    assert finance_task["claimed_count"] == 1
    assert finance_task["approval_count"] == 1
    assert finance_task["rejection_count"] == 0
    assert finance_task["completed_count"] == 1
    assert finance_task["available_slots"] == 2
    assert finance_task["current_user_claimed"] is True
    assert finance_task["current_user_participant_id"] == first["participant_id"]
    assert finance_task["current_user_decision"] == "approve"
    assert finance_task["can_claim"] is False
    assert finance_task["can_decide"] is False
    assert finance_task["reject_short_circuits"] is False


def test_same_idempotency_key_is_a_safe_noop(isolated_finance_db):
    test_engine, _ = isolated_finance_db
    _, token, _ = seed_finance_task(test_engine, 2, 3, False)
    claim(token, "finance1")
    key = str(uuid.uuid4())
    assert decide(token, "finance1", "approve", key=key)["finance_status"] == "pending"
    assert decide(token, "finance1", "approve", key=key) == {"status": "duplicate-ignored"}


def test_manager_claim_and_complete_are_unchanged(isolated_finance_db):
    test_engine, fake_temporal = isolated_finance_db
    transaction_id = str(uuid.uuid4())
    with test_engine.begin() as conn:
        conn.execute(
            text('INSERT INTO "transaction" (id) VALUES (CAST(:id AS uuid))'),
            {"id": transaction_id},
        )
    token = asyncio.run(
        invoice_activities.create_human_task(
            transaction_id, "manager", "ap_manager", {"kind": "manager"}
        )
    )
    claimed = asyncio.run(
        api_main.claim_task(
            token,
            api_main.ClaimIn(claimed_by="manager1"),
            {"username": "manager1", "roles": ["ap_manager"]},
        )
    )
    assert claimed == {"status": "claimed", "token": token, "claimed_by": "manager1"}
    completed = asyncio.run(
        api_main.complete_task(
            token,
            api_main.CompleteIn(
                idempotency_key=str(uuid.uuid4()),
                payload={"decision": "return"},
                kind="human",
            ),
            {"username": "manager1", "roles": ["ap_manager"]},
        )
    )
    assert completed == {"status": "accepted"}
    assert fake_temporal.signals == [
        (transaction_id, "human_decision", {"decision": "return"})
    ]


@pytest.mark.parametrize(
    "config",
    [
        {"quorum": {"n": 0, "of": 3}, "rejectShortCircuits": True},
        {"quorum": {"n": 4, "of": 3}, "rejectShortCircuits": True},
        {"quorum": {"n": 2, "of": 0}, "rejectShortCircuits": False},
    ],
)
def test_invalid_author_quorum_is_rejected(config):
    with pytest.raises(HTTPException) as exc:
        api_main._validate_author_finance_config(config)
    assert exc.value.status_code == 422
    assert "1 <= quorum.n <= quorum.of" in exc.value.detail
