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


_START_RE = re.compile(r"\[start:([A-Za-z0-9_\-]+)\]", re.IGNORECASE)


def _process_from_subject(subject: str):
    m = _START_RE.search(subject or "")
    return m.group(1) if m else None


def _get_pdd(api_base_url: str, process_key: str):
    try:
        r = requests.get(f"{api_base_url}/v1/definitions/{process_key}", timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _get_active_pdd(api_base_url: str):
    # The single active (latest published) process, or None.
    try:
        r = requests.get(f"{api_base_url}/v1/active-process", timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _process_for_mailbox(api_base_url: str, mailbox_name: str):
    # Find the published process whose PDD is bound to this mailbox (pdd.mailbox).
    try:
        defs = requests.get(f"{api_base_url}/v1/definitions", timeout=10).json()
    except Exception:
        return None, None
    for d in defs if isinstance(defs, list) else []:
        pk = d.get("process_key")
        pdd = _get_pdd(api_base_url, pk)
        if pdd and pdd.get("mailbox") == mailbox_name:
            return pk, pdd
    return None, None


def _norm_key(s: str) -> str:
    # Normalize a field label for matching: drop spaces/underscores, lowercase.
    # So "PO Number", "po_number" and "poNumber" all match the schema key poNumber.
    return re.sub(r"[\s_]+", "", s or "").lower()


def _parse_body_fields(body: str, field_names) -> dict:
    # Parse "field: value" lines, matched to the process's data_schema field names
    # ignoring spaces/underscores/case (so "PO Number:" maps to poNumber).
    wanted = {_norm_key(name): name for name in (field_names or [])}
    out = {}
    for line in (body or "").splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        canon = wanted.get(_norm_key(key))
        if canon and value.strip():
            out[canon] = value.strip()
    return out


def _process_new_transaction(msg, api_base_url: str, mailbox_name: str) -> str:
    # GENERIC email intake: start ANY process from an inbound email. The process is
    # chosen by a "[start:<process_key>]" subject tag, or by the process bound to
    # this mailbox (pdd.mailbox). Fields come from a PDF attachment (via the PDD's
    # extraction) and/or "field: value" body lines. NEVER raises.
    subject = decode_subject(msg.get("Subject", ""))
    sender = _sender_email(msg)

    process_key = _process_from_subject(subject)
    pdd = _get_pdd(api_base_url, process_key) if process_key else None
    if process_key and pdd is None:
        return f"skipped (subject names unknown process {process_key!r}) status=none"
    if not process_key:
        process_key, pdd = _process_for_mailbox(api_base_url, mailbox_name)
    if not process_key:
        # Single-workflow fallback: with one active process, ANY subject starts it
        # (no [start:...] tag or mailbox binding required).
        pdd = _get_active_pdd(api_base_url)
        process_key = (pdd or {}).get("process_key")
    if not process_key:
        return "skipped (no active process to start) status=none"

    data_schema = (pdd or {}).get("data_schema") or {}

    data = {}
    attachment = _pdf_attachment(msg)
    if attachment:
        filename, pdf_bytes = attachment
        try:
            extracted = requests.post(
                f"{api_base_url}/v1/extract",
                files={"file": (filename, pdf_bytes, "application/pdf")},
                data={"process_key": process_key}, timeout=120,
            ).json()
            data.update({k: v for k, v in (extracted.get("fields") or {}).items() if v is not None})
        except Exception as exc:
            print(f"intake: extract failed, continuing: {exc}")
    data.update(_parse_body_fields(_extract_body(msg), list(data_schema.keys())))

    for name, typ in data_schema.items():
        if typ == "number" and isinstance(data.get(name), str):
            try:
                data[name] = float(re.sub(r"[^0-9.\-]", "", data[name]) or 0)
            except Exception:
                data[name] = 0

    try:
        resp = requests.post(
            f"{api_base_url}/v1/transactions",
            json={"process_key": process_key, "data": data, "submitted_by": sender or None},
            timeout=30,
        )
    except Exception as exc:
        return f"error: create failed: {exc}"
    if resp.status_code >= 400:
        return f"error: create HTTP {resp.status_code}: {resp.text[:120]}"
    txn_id = (resp.json() or {}).get("transaction_id")
    return f"started {process_key} txn {txn_id} from {sender or 'unknown'} status=created"


_REQUIRED_FIELDS = ["poNumber", "costCenter", "taxId"]


def _parse_field_lines(body: str, need):
    # Parse "field: value" lines; keys matched case-insensitively against the
    # requested fields (the task's completion_policy.need, else the required set).
    wanted = {_norm_key(name): name for name in (need or _REQUIRED_FIELDS)}
    parsed = {}
    for line in body.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        canonical = wanted.get(_norm_key(key))
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


def process_message(raw_bytes: bytes, api_base_url: str, mailbox_name: str = "") -> str:
    # Turn ONE raw RFC822 message into (at most) one API call. Returns a short
    # status string; NEVER raises (a bad message must not kill the poll loop).
    msg = email.message_from_bytes(raw_bytes)
    subject = msg.get("Subject", "")
    message_id = msg.get("Message-ID", "")

    txn_id = extract_txn_id(decode_subject(subject))
    if txn_id is not None:
        # Tagged reply: a decision / resubmit on an existing transaction.
        return _process_tagged_reply(txn_id, msg, message_id, api_base_url)

    # No reply tag => generic intake: start a NEW transaction for the resolved process.
    return _process_new_transaction(msg, api_base_url, mailbox_name)


def poll_once(client, api_base_url: str, mailbox_name: str = "") -> list[str]:
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
        result = process_message(raw, api_base_url, mailbox_name)
        results.append(f"uid {uid}: {result}")
        if "status=" in result or "skipped" in result:
            client.add_flags([uid], [b"\\Seen"])
    return results


def main():
    # WHY this is a standalone process: the live email adapter runs as its OWN
    # process alongside the API and the Temporal worker. It watches EVERY
    # configured mailbox and converts reply emails into /v1/events calls that
    # resume paused workflows. Different processes can use different mailboxes
    # (see mailboxes.py), so we connect to and poll each one.
    env_path = Path(__file__).resolve().parents[3] / "services" / "api" / ".env"
    load_dotenv(env_path)
    api_base_url = os.getenv("API_BASE_URL", "http://localhost:8000")

    from mailboxes import all_mailboxes  # notifier dir is on sys.path (script dir)
    boxes = all_mailboxes()
    if not boxes:
        print("ERROR: no mailboxes configured "
              "(set GMAIL_ADDRESS/GMAIL_APP_PASSWORD or MAILBOX_<NAME>_ADDRESS/_APP_PASSWORD)")
        return

    # Connect to each mailbox (NOT readonly: we set \Seen on handled messages).
    clients = []
    for box in boxes:
        try:
            client = IMAPClient(box["imap_host"], ssl=True)
            client.login(box["address"], box["app_password"])
            client.select_folder("INBOX")
            clients.append((box, client))
            print(f"Email adapter connected: {box['address']} (mailbox '{box['name']}')")
        except Exception as exc:
            print(f"Email adapter: FAILED to connect {box['address']}: {exc}")

    if not clients:
        print("ERROR: no mailbox connections succeeded")
        return
    print(f"Polling {len(clients)} mailbox(es) every 15s (POSTing to {api_base_url}/v1/events). "
          "Ctrl+C to stop.")

    try:
        while True:
            ts = datetime.now().strftime("%H:%M:%S")
            total = 0
            for box, client in clients:
                try:
                    results = poll_once(client, api_base_url, box["name"])
                except Exception as exc:
                    print(f"[{ts}] {box['name']}: poll error: {exc}")
                    continue
                for r in results:
                    print(f"[{ts}] {box['name']}: {r}")
                total += len(results)
            print(f"[{ts}] polled {len(clients)} mailbox(es), {total} new")
            time.sleep(15)
    except KeyboardInterrupt:
        print("\nStopping email adapter...")
    finally:
        for _, client in clients:
            try:
                client.logout()
            except Exception:
                pass


if __name__ == "__main__":
    main()
