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
from email.utils import parseaddr
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


def _sender_email(msg) -> str:
    # Bare From address, lowercased for matching.
    return parseaddr(msg.get("From", ""))[1].strip().lower()


def _pdf_attachment(msg):
    # (filename, bytes) of the first PDF attachment, or None.
    for part in msg.walk():
        filename = part.get_filename() or ""
        if part.get_content_type() == "application/pdf" or filename.lower().endswith(".pdf"):
            payload = part.get_payload(decode=True)
            if payload:
                return filename or "invoice.pdf", payload
    return None


def _vendor_for_sender(sender: str) -> str | None:
    # Identify the vendor by SENDER email via the Keycloak admin API (admin-cli
    # token from the master realm — the same pattern used elsewhere). Returns the
    # username of the vendor-role user whose email matches, else None. Raises on
    # Keycloak errors so the caller keeps the mail UNSEEN and retries.
    kc = os.getenv("KEYCLOAK_URL", "http://localhost:8081")
    realm = os.getenv("KEYCLOAK_REALM", "workflow")
    admin = os.getenv("KEYCLOAK_ADMIN", "admin")
    password = os.getenv("KEYCLOAK_ADMIN_PASSWORD", "admin")
    token = requests.post(
        f"{kc}/realms/master/protocol/openid-connect/token",
        data={"client_id": "admin-cli", "grant_type": "password", "username": admin, "password": password},
        timeout=10,
    ).json().get("access_token")
    if not token:
        raise RuntimeError("Keycloak admin token unavailable")
    users = requests.get(
        f"{kc}/admin/realms/{realm}/roles/vendor/users",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    ).json()
    for user in users if isinstance(users, list) else []:
        if sender and (user.get("email") or "").strip().lower() == sender:
            return user.get("username")
    return None


def _process_new_invoice(msg, api_base_url: str) -> str:
    # A fresh invoice arriving by email (no [invoice-<uuid>] tag): identify the
    # vendor by sender, extract the PDF via the SAME /v1/extract-invoice endpoint,
    # and create the transaction with submitted_by = the matched vendor username.
    attachment = _pdf_attachment(msg)
    if attachment is None:
        return "skipped (no [invoice-] tag and no PDF attachment)"
    sender = _sender_email(msg)
    try:
        vendor_user = _vendor_for_sender(sender)
    except Exception as exc:
        return f"error: Keycloak lookup failed: {exc}"
    if not vendor_user:
        return f"skipped (unknown sender {sender!r})"

    filename, pdf_bytes = attachment
    try:
        extracted = requests.post(
            f"{api_base_url}/v1/extract-invoice",
            files={"file": (filename, pdf_bytes, "application/pdf")},
            timeout=60,
        ).json()
    except Exception as exc:
        return f"error: extract failed: {exc}"

    data = {key: value for key, value in (extracted.get("fields") or {}).items() if value is not None}
    data.setdefault("vendor", vendor_user)  # fall back to the matched vendor
    amount = data.get("amount")
    if isinstance(amount, str):
        try:
            amount = float(re.sub(r"[^0-9.]", "", amount) or 0)
        except Exception:
            amount = 0
    data["amount"] = amount if isinstance(amount, (int, float)) else 0

    try:
        resp = requests.post(
            f"{api_base_url}/v1/transactions",
            json={"process_key": "invoice_approval", "data": data, "submitted_by": vendor_user},
            timeout=30,
        )
    except Exception as exc:
        return f"error: create failed: {exc}"
    if resp.status_code >= 400:
        return f"error: create HTTP {resp.status_code}: {resp.text[:120]}"
    txn_id = (resp.json() or {}).get("transaction_id")
    return f"new-invoice: created {txn_id} for {vendor_user} status=created"


_REQUIRED_FIELDS = ["poNumber", "costCenter", "taxId"]


def _parse_field_lines(body: str, need):
    # Parse "field: value" lines; keys matched case-insensitively against the
    # requested fields (the task's completion_policy.need, else the required set).
    wanted = {name.lower(): name for name in (need or _REQUIRED_FIELDS)}
    parsed = {}
    for line in body.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        canonical = wanted.get(key.strip().lower())
        if canonical and value.strip():
            parsed[canonical] = value.strip()
    return parsed


def _process_tagged_reply(txn_id: str, msg, message_id: str, api_base_url: str) -> str:
    # A reply carrying [invoice-<uuid>]. Resolve the transaction's CURRENT open task
    # to decide behaviour + get its token (first-wins dedup) + missing fields.
    body = _extract_body(msg)
    try:
        info = requests.get(f"{api_base_url}/v1/transactions/{txn_id}/open-task", timeout=10).json()
    except Exception as exc:
        return f"error: open-task lookup failed: {exc}"
    open_task = info.get("open_task")
    if not open_task:
        # DEDUP: the other channel (app) already closed the task / the step passed.
        return f"skipped (no open task for {txn_id}; already handled) status=none"

    node = open_task.get("node_id")
    if node == "request_info":
        payload = {"decision": "resubmit", "data": _parse_field_lines(body, open_task.get("need"))}
    elif node == "manager":
        decision = extract_decision(body)
        if decision is None:
            return "skipped (tagged reply with no decision)"
        payload = {"decision": decision}
    else:
        return f"skipped (open task {node!r} is not email-actionable) status=none"

    request_body = {
        "transaction_id": txn_id,
        "task_token": open_task.get("token"),
        "idempotency_key": f"email-{message_id}",
        "kind": "human",
        "payload": payload,
    }
    try:
        resp = requests.post(f"{api_base_url}/v1/events", json=request_body, timeout=10)
        try:
            status_field = resp.json().get("status")
        except Exception:
            status_field = None
        return f"posted HTTP {resp.status_code} status={status_field}"
    except Exception as exc:
        return f"error: {exc}"


def process_message(raw_bytes: bytes, api_base_url: str) -> str:
    # Turn ONE raw RFC822 message into (at most) one API call. Returns a short
    # status string; NEVER raises (a bad message must not kill the poll loop).
    msg = email.message_from_bytes(raw_bytes)
    subject = msg.get("Subject", "")
    message_id = msg.get("Message-ID", "")

    txn_id = extract_txn_id(decode_subject(subject))
    if txn_id is not None:
        # Tagged reply: approval decision OR request_info resubmit (Phase 4).
        return _process_tagged_reply(txn_id, msg, message_id, api_base_url)

    # No tag => a fresh invoice submitted by email (Phase 3).
    return _process_new_invoice(msg, api_base_url)


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
    # Re-SELECT INBOX each cycle: a long-lived SELECT does NOT surface messages
    # that arrived AFTER it, so newly delivered mail would never appear in SEARCH.
    client.select_folder("INBOX")
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
