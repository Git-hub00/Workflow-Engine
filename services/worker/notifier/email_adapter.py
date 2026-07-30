# services/worker/notifier/email_adapter.py
#
# WHY this file exists:
# The email adapter (Section 9.3) — it turns inbound email into workflow input:
#   * a reply to a notification  -> a decision posted to /v1/events, which resumes
#     the durably-paused run;
#   * any other mail arriving in a workflow's mailbox -> a NEW transaction.
#
# The parsing half (subject/body/field/decision readers) is deliberately PURE — no
# IMAP, no network, no I/O — so it is fully unit-testable on its own; see
# scripts/test_email_reply.py. The live poller at the bottom is a thin shell that
# only fetches messages and calls those parsers.

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

# Matches our notification-subject tag "[<prefix>-<uuid>]" and captures the uuid.
# The prefix is ANY word (we send "invoice-", but a future/renamed prefix must not
# break reply correlation — the uuid shape is what actually identifies the run).
_TXN_RE = re.compile(
    r"\[[A-Za-z0-9_]+-("
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r")\]"
)


def decode_subject(raw_subject: str) -> str:
    # Email subjects arrive as MIME "encoded-words" (e.g. "=?UTF-8?Q?caf=C3=A9?=")
    # for any non-ASCII content. make_header(decode_header(...)) reassembles the
    # parts into a normal Unicode string.
    #
    # NEVER raises: a malformed encoded-word or an unknown charset name makes
    # decode_header/make_header raise (LookupError / UnicodeDecodeError). This
    # function is called from the poll loop, so a single hostile subject line used
    # to abort the whole polling pass and stall EVERY pending reply in that inbox.
    try:
        return str(make_header(decode_header(raw_subject or "")))
    except Exception:
        return str(raw_subject or "")


def extract_txn_id(subject: str) -> str | None:
    # Pull the transaction id out of the "[invoice-<uuid>]" tag we embed in
    # notification subjects. Returns the uuid, or None if the tag isn't present.
    m = _TXN_RE.search(subject)
    return m.group(1) if m else None


# Lines that begin the QUOTED ORIGINAL in a reply. Everything from the first such
# line onwards is the mail WE sent, not what the person wrote.
_QUOTE_MARKERS = (
    re.compile(r"^\s*>"),                                     # > quoted line
    re.compile(r"^\s*On .*wrote:\s*$", re.IGNORECASE),         # Gmail / Apple Mail
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Forwarded message\s*-{2,}", re.IGNORECASE),
    re.compile(r"^\s*_{10,}\s*$"),                             # Outlook divider
    re.compile(r"^\s*(From|Sent|To|Subject)\s*:\s*.+", re.IGNORECASE),  # Outlook header block
)


def strip_quoted(body: str) -> str:
    """Return ONLY what the person typed, dropping the quoted original below it.

    WHY THIS MATTERS (this was a real, severe bug):
    our approval email ends with "...reply with 'approve' or 'reject'", and our
    request-info email lists "poNumber: <value>" template lines. Almost every mail
    client quotes that original text underneath the reply. So:
      * extract_decision() scanned the WHOLE body, found the word "reject" in OUR
        OWN quoted instructions, and (reject having priority) turned EVERY approval
        into a REJECTION;
      * _parse_field_lines() walked every line and let the LAST match win, so the
        quoted template line "poNumber: <value>" OVERWROTE the real value the
        vendor typed above it.
    Cutting the quoted part off before parsing fixes both."""
    lines = (body or "").splitlines()
    kept = []
    for line in lines:
        if any(rx.match(line) for rx in _QUOTE_MARKERS):
            break
        kept.append(line)
    top = "\n".join(kept).strip()
    # If the person replied INSIDE / BELOW the quote (nothing above it), fall back
    # to the full body rather than seeing an empty reply.
    return top if top else (body or "")


# A value that is still our own template placeholder, e.g. "<value>" or "<amount>".
_PLACEHOLDER_RE = re.compile(r"^<[^>]*>$")


def _is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER_RE.match((value or "").strip()))


def extract_decision(body: str) -> str | None:
    # Map the free-text reply to a decision, case-insensitively. Priority order is
    # reject > return > approve, so an ambiguous reply like "I approve but reject"
    # is treated as the more conservative "reject" (never auto-approve on doubt).
    # Scans only the person's own words (the quoted original is stripped first),
    # otherwise our own "reply approve or reject" instruction decides for them.
    low = strip_quoted(body).lower()
    if "reject" in low:
        return "reject"
    if "return" in low:
        return "return"
    if "approve" in low:
        return "approve"
    return None


# NOTE on idempotency: every reply posted below uses
# idempotency_key = "email-" + Message-ID. A Message-ID uniquely identifies an email,
# so if the SAME email is processed twice (poller restart, overlapping runs, IMAP
# re-delivery) the key is identical and /v1/events collapses it to exactly one
# signal instead of acting on the reply twice.


# ---------------------------------------------------------------------------
# Live adapter (IMAP + HTTP). The functions above stay pure/unit-testable; the
# code below wires the IMAP connection and the parsers
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


def _get_pdd(api_base_url: str, process_key: str):
    try:
        r = requests.get(f"{api_base_url}/v1/definitions/{process_key}", timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _mailbox_key(name) -> str:
    # Mailbox names are compared case-insensitively and -/_ insensitively, because
    # the registry derives names from env vars (MAILBOX_INVOICE_ADDRESS -> INVOICE)
    # while the Builder stores what the author typed ("invoice").
    return str(name or "").strip().upper().replace("-", "_")


def _process_for_mailbox(api_base_url: str, mailbox_name: str):
    # Find the published process bound to THIS mailbox (pdd.mailbox). One Gmail
    # serves exactly one process, so the first match is the answer.
    try:
        defs = requests.get(f"{api_base_url}/v1/definitions", timeout=10).json()
    except Exception:
        return None, None
    want = _mailbox_key(mailbox_name)
    for d in defs if isinstance(defs, list) else []:
        pk = d.get("process_key")
        pdd = _get_pdd(api_base_url, pk)
        if pdd and _mailbox_key(pdd.get("mailbox")) == want:
            return pk, pdd
    return None, None


_MACHINE_SENDERS = ("mailer-daemon", "postmaster", "no-reply", "noreply",
                    "do-not-reply", "donotreply", "bounce", "notification-daemon")
_MACHINE_SUBJECTS = ("delivery status notification", "undelivered mail",
                     "undeliverable", "mail delivery", "address not found",
                     "returned mail", "failure notice", "out of office",
                     "automatic reply", "auto-reply", "autoreply")


def is_machine_mail(msg, mailbox_address: str = "") -> str | None:
    """Return a reason string when this message is a bounce / auto-reply / our own
    mail, else None. Such mail must NEVER start a process or complete a task —
    a bounce for a bad recipient was previously read back in as a NEW request."""
    frm = (_sender_email(msg) or "").lower()
    local = frm.split("@", 1)[0] if "@" in frm else frm
    if any(tok in local for tok in _MACHINE_SENDERS) or frm.startswith("mailer-daemon"):
        return f"machine sender {frm!r}"
    if mailbox_address and frm == mailbox_address.strip().lower():
        return "message sent by this mailbox to itself"

    auto = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto and auto != "no":
        return f"Auto-Submitted: {auto}"
    for header in ("X-Autoreply", "X-Autorespond", "X-Failed-Recipients"):
        if msg.get(header):
            return f"header {header}"
    if (msg.get("Precedence") or "").strip().lower() in ("auto_reply", "bulk", "junk"):
        return "Precedence header"

    ctype = (msg.get("Content-Type") or "").lower()
    if "multipart/report" in ctype or "delivery-status" in ctype:
        return "delivery-status report"

    subject = decode_subject(msg.get("Subject", "")).strip().lower()
    if any(s in subject for s in _MACHINE_SUBJECTS):
        return f"machine subject {subject[:40]!r}"
    return None


def _norm_key(s: str) -> str:
    # Normalize a field label for matching: drop spaces/underscores, lowercase.
    # So "PO Number", "po_number" and "poNumber" all match the schema key poNumber.
    return re.sub(r"[\s_]+", "", s or "").lower()


def _parse_body_fields(body: str, field_names) -> dict:
    # Parse "field: value" lines, matched to the process's data_schema field names
    # ignoring spaces/underscores/case (so "PO Number:" maps to poNumber).
    # FIRST value wins and template placeholders ("<value>") are ignored, so a
    # quoted copy of our own instructions can never overwrite a real answer.
    wanted = {_norm_key(name): name for name in (field_names or [])}
    out = {}
    for line in (body or "").splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        canon = wanted.get(_norm_key(key))
        value = value.strip()
        if canon and value and canon not in out and not _is_placeholder(value):
            out[canon] = value
    return out


def _process_new_transaction(msg, api_base_url: str, mailbox_name: str) -> str:
    # GENERIC email intake: start ANY process from an inbound email. The process is
    # chosen by a "[start:<process_key>]" subject tag, or by the process bound to
    # this mailbox (pdd.mailbox). Fields come from a PDF attachment (via the PDD's
    # extraction) and/or "field: value" body lines. NEVER raises.
    subject = decode_subject(msg.get("Subject", ""))
    sender = _sender_email(msg)

    # STRICT MAILBOX BINDING: the process is decided ONLY by which mailbox the
    # mail arrived in (pdd.mailbox == this mailbox's name). One Gmail = one
    # process. No [start:...] subject selection and no "default/active process"
    # fallback — so mail can never start the wrong workflow.
    process_key, pdd = _process_for_mailbox(api_base_url, mailbox_name)
    if not process_key:
        return (f"skipped (mailbox {mailbox_name!r} is not bound to any published process; "
                f"set that process's Mailbox field to {mailbox_name!r}) status=none")

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
            headers=_internal_headers(), timeout=30,
        )
    except Exception as exc:
        return f"error: create failed: {exc}"
    if resp.status_code >= 400:
        return f"error: create HTTP {resp.status_code}: {resp.text[:120]}"
    txn_id = (resp.json() or {}).get("transaction_id")
    return f"started {process_key} txn {txn_id} from {sender or 'unknown'} status=created"


def _schema_fields(api_base_url: str, process_key: str) -> list:
    """The field names THIS workflow declares (pdd.data_schema). Used as the
    fallback set when a reply has no decision and the task listed no required
    fields — previously this fell back to the hardcoded invoice field names, so a
    leave or refund reply carrying real values parsed as nothing."""
    if not process_key:
        return []
    pdd = _get_pdd(api_base_url, process_key) or {}
    schema = pdd.get("data_schema")
    return list(schema.keys()) if isinstance(schema, dict) else []


def _parse_field_lines(body: str, need):
    # Parse "field: value" lines; keys matched case-insensitively against the
    # requested fields (the task's completion_policy.need, else the given set).
    # The quoted original is stripped, the FIRST value wins, and our own template
    # placeholders ("<value>") are skipped — see strip_quoted() for why.
    wanted = {_norm_key(name): name for name in (need or [])}
    parsed = {}
    for line in strip_quoted(body).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        canonical = wanted.get(_norm_key(key))
        value = value.strip()
        if canonical and value and canonical not in parsed and not _is_placeholder(value):
            parsed[canonical] = value
    return parsed


def _internal_headers() -> dict:
    """Service credential for the write endpoints, when one is configured.

    The adapter is a trusted backend process, not a browser, so it has no user
    token. Setting INTERNAL_API_KEY in services/api/.env (which BOTH the API and
    this adapter load) lets the API stop accepting anonymous /v1/events calls
    without breaking email intake."""
    key = os.getenv("INTERNAL_API_KEY")
    return {"X-Internal-Key": key} if key else {}


def _process_tagged_reply(txn_id: str, msg, message_id: str, api_base_url: str) -> str:
    # A reply carrying [invoice-<uuid>]. Resolve the transaction's CURRENT open task
    # to decide behaviour + get its token (first-wins dedup) + missing fields.
    body = _extract_body(msg)
    try:
        info = requests.get(f"{api_base_url}/v1/transactions/{txn_id}/open-task", timeout=10).json()
    except Exception as exc:
        return f"error: open-task lookup failed: {exc}"
    open_task = info.get("open_task") if isinstance(info, dict) else None
    if not open_task:
        # DEDUP: the other channel (app) already closed the task / the step passed.
        return f"skipped (no open task for {txn_id}; already handled) status=none"

    # A MULTI-APPROVER (quorum) step cannot be settled by one email: each approver
    # must claim their own slot and vote, so the votes can be counted. Completing it
    # through /v1/events used to close the parent task while leaving every slot
    # unvoted — the run then waited FOREVER with no task visible to anyone.
    if open_task.get("is_quorum"):
        return (f"skipped (step {open_task.get('node_id')!r} needs a multi-approver vote; "
                "each approver must decide in the app) status=none")

    # GENERIC reply handling — works for ANY step name in ANY workflow.
    #   * the step asked for missing fields  -> parse "field: value" lines
    #   * otherwise it is an approval step   -> read approve / reject from the text
    node = open_task.get("node_id")
    need = open_task.get("need")
    if need:
        provided = _parse_field_lines(body, need)
        if not provided:
            return (f"skipped (reply to {node!r} had no 'field: value' lines for {need}) "
                    "status=none")
        payload = {"decision": "resubmit", "data": provided}
    else:
        decision = extract_decision(body)
        if decision is None:
            # Maybe it is a data step whose fields we can still parse — matched
            # against the fields THIS workflow declares, not a fixed list.
            provided = _parse_field_lines(
                body, _schema_fields(api_base_url, open_task.get("process_key")))
            if provided:
                payload = {"decision": "resubmit", "data": provided}
            else:
                return f"skipped (reply to {node!r} had no decision) status=none"
        else:
            payload = {"decision": decision}

    request_body = {
        "transaction_id": txn_id,
        "task_token": open_task.get("token"),
        "idempotency_key": f"email-{message_id}",
        "kind": "human",
        "payload": payload,
    }
    try:
        resp = requests.post(f"{api_base_url}/v1/events", json=request_body,
                             headers=_internal_headers(), timeout=10)
        # 5xx / transient: report an error so the mail stays UNSEEN and is retried.
        # 4xx is PERMANENT (bad decision value, quorum step, unknown transaction) —
        # retrying it forever would re-read the same mail every 15s, so we mark it
        # handled and log why.
        if resp.status_code >= 500:
            return f"error: events HTTP {resp.status_code}: {resp.text[:160]}"
        if resp.status_code >= 400:
            return (f"skipped (rejected by API: HTTP {resp.status_code} "
                    f"{resp.text[:120]}) status=none")
        try:
            status_field = resp.json().get("status")
        except Exception:
            status_field = None
        return f"posted HTTP {resp.status_code} status={status_field}"
    except Exception as exc:
        return f"error: {exc}"


def process_message(raw_bytes: bytes, api_base_url: str, mailbox_name: str = "",
                    mailbox_address: str = "") -> str:
    # Turn ONE raw RFC822 message into (at most) one API call. Returns a short
    # status string; NEVER raises (a bad message must not kill the poll loop).
    # The whole body is guarded: a malformed MIME structure, an undecodable part or
    # a hostile header used to raise out of here, abort the polling pass and stall
    # every other pending reply in that inbox until a human noticed.
    try:
        msg = email.message_from_bytes(raw_bytes)
        subject = msg.get("Subject", "")
        message_id = msg.get("Message-ID", "")

        # Bounces, auto-replies and our own outgoing mail are NOT human input.
        machine = is_machine_mail(msg, mailbox_address)
        if machine:
            return f"skipped ({machine}) status=none"

        txn_id = extract_txn_id(decode_subject(subject))
        if txn_id is not None:
            # Tagged reply: a decision / resubmit on an existing transaction.
            return _process_tagged_reply(txn_id, msg, message_id, api_base_url)

        # No reply tag => generic intake: start a NEW transaction for this process.
        return _process_new_transaction(msg, api_base_url, mailbox_name)
    except Exception as exc:
        # Unparseable message: mark it handled (a retry would fail identically and
        # re-read it every cycle forever) but say loudly what happened.
        return f"skipped (could not process message: {type(exc).__name__}: {exc}) status=none"


def poll_once(client, api_base_url: str, mailbox_name: str = "",
              mailbox_address: str = "") -> list[str]:
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
        # BODY.PEEK[] — NOT RFC822. Fetching "RFC822" makes the IMAP SERVER set the
        # \Seen flag as a side effect of the read, BEFORE we know whether we managed
        # to deliver the decision. So the careful "don't mark \Seen on error" logic
        # below was useless: the mail was already read, the next SEARCH UNSEEN never
        # returned it again, and a transient API outage SILENTLY LOST the reply
        # forever. BODY.PEEK[] fetches the identical bytes WITHOUT setting \Seen, so
        # we alone decide when a message counts as handled.
        try:
            resp = client.fetch([uid], ["BODY.PEEK[]"])
            # Servers key the response as BODY[] even though we asked with .PEEK.
            entry = resp.get(uid) or {}
            raw = entry.get(b"BODY[]") or entry.get(b"RFC822")
        except Exception as exc:
            # One unreadable uid (deleted/moved between SEARCH and FETCH, or a
            # server hiccup) must not abort the pass for every OTHER pending reply.
            results.append(f"uid {uid}: error: fetch failed: {exc}")
            continue
        if not raw:
            results.append(f"uid {uid}: error: fetch returned no body (will retry)")
            continue
        result = process_message(raw, api_base_url, mailbox_name, mailbox_address)
        results.append(f"uid {uid}: {result}")
        if "status=" in result or "skipped" in result:
            try:
                client.add_flags([uid], [b"\\Seen"])
            except Exception as exc:
                # Handled but not flagged: idempotency on /v1/events makes the
                # inevitable re-read harmless.
                results.append(f"uid {uid}: warning: could not mark seen: {exc}")
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
                    results = poll_once(client, api_base_url, box["name"], box.get("address", ""))
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
