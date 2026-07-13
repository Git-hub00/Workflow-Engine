# WHY this file exists:
# Unit tests for the PURE email-parsing functions in email_adapter.py. No network
# / no IMAP — just feed sample subjects/bodies/message-ids and assert the parsed
# results, so the parsing logic is proven independently of the live poller.

import sys
from pathlib import Path

# Make the notifier module importable (relative to this test file).
ROOT = Path(__file__).resolve().parent.parent
NOTIFIER_DIR = ROOT / "services" / "worker" / "notifier"
sys.path.insert(0, str(NOTIFIER_DIR))

from email_adapter import (  # noqa: E402  (after sys.path tweak)
    decode_subject,
    extract_txn_id,
    extract_decision,
    build_event_payload,
)

TXN = "32a551eb-8ec1-49ce-9d8d-64e505c429ae"

failures = []


def check(label, got, expected):
    ok = got == expected
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"       got:      {got!r}")
        print(f"       expected: {expected!r}")
        failures.append(label)


def main():
    # 1. decode_subject: MIME encoded-word -> Unicode (and plain passes through).
    check("decode_subject: MIME Q-encoded -> unicode",
          decode_subject("=?UTF-8?Q?caf=C3=A9?="), "café")
    check("decode_subject: plain ascii unchanged",
          decode_subject("Action needed: manager"), "Action needed: manager")

    # 2. extract_txn_id: pull uuid from the [invoice-<uuid>] tag; None if absent.
    check("extract_txn_id: tag present -> uuid",
          extract_txn_id(f"[invoice-{TXN}] Action needed: manager"), TXN)
    check("extract_txn_id: no tag -> None",
          extract_txn_id("Re: your invoice question"), None)

    # 3. extract_decision: case-insensitive, priority reject > return > approve.
    check("extract_decision: 'please approve' -> approve",
          extract_decision("please approve"), "approve")
    check("extract_decision: 'REJECT this' -> reject",
          extract_decision("REJECT this"), "reject")
    check("extract_decision: 'I approve but reject' -> reject (priority)",
          extract_decision("I approve but reject"), "reject")
    check("extract_decision: 'hello' -> None",
          extract_decision("hello"), None)

    # 4. build_event_payload: good subject+body -> full dict; bad subject -> None.
    good = build_event_payload(
        subject=f"[invoice-{TXN}] Action needed: manager",
        body="please approve",
        message_id="<abc123@mail.gmail.com>",
    )
    check("build_event_payload: good input -> correct dict", good, {
        "transaction_id": TXN,
        "idempotency_key": "email-<abc123@mail.gmail.com>",
        "kind": "human",
        "payload": {"decision": "approve"},
    })
    check("build_event_payload: no tag in subject -> None",
          build_event_payload(subject="no tag here", body="approve",
                              message_id="<x@mail>"), None)
    # Also: valid tag but no decision word -> None (can't act).
    check("build_event_payload: no decision in body -> None",
          build_event_payload(subject=f"[invoice-{TXN}] Action needed",
                              body="thanks, will look later", message_id="<y@mail>"), None)

    if failures:
        print(f"\n{len(failures)} CHECK(S) FAILED: {failures}")
        sys.exit(1)
    print("\nEMAIL ADAPTER PARSING TESTS PASSED")


if __name__ == "__main__":
    main()
