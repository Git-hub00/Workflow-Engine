# services/worker/notifier/email_adapter.py
#
# WHY this file exists:
# The PARSING half of the email adapter (Section 9.3) — the pure logic that turns
# an inbound email (subject + body + Message-ID) into a POST /v1/events payload.
# These functions are deliberately PURE: no IMAP, no network, no I/O. That keeps
# them fully unit-testable, and lets the (later) live polling loop stay a thin
# shell that just fetches messages and calls build_event_payload() + HTTP.

import email
import os
import re
import time
from datetime import datetime
from email.header import decode_header, make_header
from pathlib import Path

import requests
from dotenv import load_dotenv
from imapclient import IMAPClient

# Matches our notification-subject tag "[invoice-<uuid>]" and captures the uuid.
# The workflow/transaction id is the <uuid> part after "invoice-".
_TXN_RE = re.compile(
    r"\[invoice-("
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r")\]"
)


def decode_subject(raw_subject: str) -> str:
    # Email subjects arrive as MIME "encoded-words" (e.g. "=?UTF-8?Q?caf=C3=A9?=")
    # for any non-ASCII content. make_header(decode_header(...)) reassembles the
    # parts into a normal Unicode string.
    return str(make_header(decode_header(raw_subject)))


def extract_txn_id(subject: str) -> str | None:
    # Pull the transaction id out of the "[invoice-<uuid>]" tag we embed in
    # notification subjects. Returns the uuid, or None if the tag isn't present.
    m = _TXN_RE.search(subject)
    return m.group(1) if m else None


def extract_decision(body: str) -> str | None:
    # Map the free-text reply to a decision, case-insensitively. Priority order is
    # reject > return > approve, so an ambiguous reply like "I approve but reject"
    # is treated as the more conservative "reject" (never auto-approve on doubt).
    low = body.lower()
    if "reject" in low:
        return "reject"
    if "return" in low:
        return "return"
    if "approve" in low:
        return "approve"
    return None


def build_event_payload(subject: str, body: str, message_id: str) -> dict | None:
    # Combine the parsers into a ready-to-POST /v1/events body. If we can't find
    # BOTH a transaction id and a decision, we can't act -> return None.
    decoded = decode_subject(subject)
    txn_id = extract_txn_id(decoded)
    decision = extract_decision(body)
    if txn_id is None or decision is None:
        return None

    # WHY idempotency_key = "email-" + Message-ID: a Message-ID uniquely identifies
    # an email. If the SAME email is processed twice (poller restart, overlapping
    # runs, IMAP re-delivery), it yields the SAME key — so /v1/events dedupes it to
    # exactly one signal instead of acting on the reply twice.
    return {
        "transaction_id": txn_id,
        "idempotency_key": "email-" + message_id,
        "kind": "human",
        "payload": {"decision": decision},
    }


# ---------------------------------------------------------------------------
# Live adapter (IMAP + HTTP). The functions above stay pure/unit-testable; the
# code below wires the proven IMAP connection (email_probe.py) and the parsers
# together into a running poller.
# ---------------------------------------------------------------------------


def _extract_body(msg) -> str:
    # WHY prefer text/plain: a reply's plain-text alternative is clean to keyword
    # scan for approve/reject/return, whereas the text/html part is full of tags
    # and quoted-reply markup that would pollute (or mislead) the detection.
    # Walk the parts and take the FIRST text/plain part; otherwise fall back to
    # decoding the whole payload.
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload is not None:
                    return payload.decode("utf-8", errors="replace")
    # Non-multipart message, or no text/plain part found.
    payload = msg.get_payload(decode=True)
    if payload is not None:
        return payload.decode("utf-8", errors="replace")
    return ""


def process_message(raw_bytes: bytes, api_base_url: str) -> str:
    # Turn ONE raw RFC822 message into (at most) one /v1/events call. Returns a
    # short human-readable status string; NEVER raises (a bad message must not
    # kill the poll loop).
    msg = email.message_from_bytes(raw_bytes)
    subject = msg.get("Subject", "")
    message_id = msg.get("Message-ID", "")
    body = _extract_body(msg)

    payload = build_event_payload(subject, body, message_id)
    if payload is None:
        # Not one of our notification replies (no [invoice-<uuid>] tag), or no
        # decision word — nothing to act on.
        return "skipped (no txn id or decision)"

    try:
        resp = requests.post(f"{api_base_url}/v1/events", json=payload, timeout=10)
        try:
            status_field = resp.json().get("status")
        except Exception:
            status_field = None
        return f"posted HTTP {resp.status_code} status={status_field}"
    except Exception as e:
        return f"error: {e}"


def poll_once(client, api_base_url: str) -> list[str]:
    # One polling pass: process every UNSEEN message, marking it \Seen ONLY when
    # it was definitively handled:
    #   * result contains "status=" -> the POST reached /v1/events and got a
    #     response (accepted / duplicate-ignored / workflow-already-closed), or
    #   * result contains "skipped"  -> not our email / no decision, safe to read.
    # WHY we do NOT mark \Seen on an "error:" result (API unreachable, timeout,
    # etc.): an error means the decision was NOT successfully delivered, so the
    # message must stay UNSEEN and be RETRIED on the next cycle rather than being
    # silently swallowed. (Idempotency on /v1/events keeps a later retry safe.)
    results = []
    uids = client.search(["UNSEEN"])
    for uid in uids:
        resp = client.fetch([uid], ["RFC822"])
        raw = resp[uid][b"RFC822"]
        result = process_message(raw, api_base_url)
        results.append(f"uid {uid}: {result}")
        if "status=" in result or "skipped" in result:
            client.add_flags([uid], [b"\\Seen"])
    return results


def main():
    # WHY this is a standalone process: the live email adapter runs as its OWN
    # process alongside the API and the Temporal worker. It watches the mailbox
    # and converts reply emails into /v1/events calls that resume paused workflows.
    env_path = Path(__file__).resolve().parents[3] / "services" / "api" / ".env"
    load_dotenv(env_path)

    gmail_address = os.getenv("GMAIL_ADDRESS")
    gmail_app_password = os.getenv("GMAIL_APP_PASSWORD")
    imap_host = os.getenv("IMAP_HOST")
    api_base_url = os.getenv("API_BASE_URL", "http://localhost:8000")

    if not (gmail_address and gmail_app_password and imap_host):
        print("ERROR: missing GMAIL_ADDRESS / GMAIL_APP_PASSWORD / IMAP_HOST in .env")
        return

    client = IMAPClient(imap_host, ssl=True)
    client.login(gmail_address, gmail_app_password)
    # NOT readonly: we need write access to set the \Seen flag.
    client.select_folder("INBOX")
    print(f"Email adapter connected as {gmail_address}; polling INBOX every 15s "
          f"(POSTing to {api_base_url}/v1/events). Ctrl+C to stop.")

    try:
        while True:
            results = poll_once(client, api_base_url)
            ts = datetime.now().strftime("%H:%M:%S")
            for r in results:
                print(f"[{ts}] {r}")
            print(f"[{ts}] polled, {len(results)} new")
            time.sleep(15)
    except KeyboardInterrupt:
        print("\nStopping email adapter...")
    finally:
        try:
            client.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
