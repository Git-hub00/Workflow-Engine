# services/worker/notifier/email_debug.py
#
# WHY this file exists:
# A READ-ONLY inbox inspector for debugging the email adapter. It never marks
# messages \Seen (INBOX opened readonly) and never prints the password. It shows
# the inbox totals, which messages are UNSEEN, and any messages whose subject
# mentions "invoice-" (our notification tag), so we can see exactly what the live
# poller would/would not pick up.

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from imapclient import IMAPClient

# Reuse the adapter's MIME subject decoder so debug subjects match what the
# real poller sees.
from email_adapter import decode_subject

# Windows consoles default to cp1252, which crashes when printing emoji / other
# non-latin subject characters. Force UTF-8 output (replacing anything the
# terminal can't render) so the debug dump never dies on a decorative subject.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ENV_PATH = Path(__file__).resolve().parents[3] / "services" / "api" / ".env"
load_dotenv(ENV_PATH)

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
IMAP_HOST = os.getenv("IMAP_HOST")


def _subject_of(fetch_resp, uid):
    env = fetch_resp[uid][b"ENVELOPE"]
    raw = env.subject.decode("utf-8", "replace") if env.subject else "(no subject)"
    return decode_subject(raw)


def main():
    print(f"Using .env: {ENV_PATH}")
    print(f"Address:    {GMAIL_ADDRESS}")
    print(f"IMAP host:  {IMAP_HOST}")

    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and IMAP_HOST):
        print("ERROR: missing GMAIL_ADDRESS / GMAIL_APP_PASSWORD / IMAP_HOST in .env")
        sys.exit(1)

    client = None
    try:
        # Fresh SSL connection, READONLY select so nothing is ever marked seen.
        client = IMAPClient(IMAP_HOST, ssl=True)
        client.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        client.select_folder("INBOX", readonly=True)

        all_uids = client.search(["ALL"])
        unseen = client.search(["UNSEEN"])
        unseen_set = set(unseen)
        print(f"\ntotal = {len(all_uids)}")
        print(f"unseen_count = {len(unseen)}")
        print(f"UNSEEN uids: {unseen}")

        # Messages whose SUBJECT contains "invoice-" (our notification tag).
        print("\n--- subject contains 'invoice-' ---")
        invoice_uids = client.search(["SUBJECT", "invoice-"])
        if invoice_uids:
            resp = client.fetch(invoice_uids, ["ENVELOPE"])
            for uid in invoice_uids:
                print(f"  uid {uid}: {_subject_of(resp, uid)}")
        else:
            print("  (none)")

        # Last 5 overall: uid, decoded subject, UNSEEN?
        print("\n--- last 5 messages ---")
        recent = all_uids[-5:] if all_uids else []
        if recent:
            resp = client.fetch(recent, ["ENVELOPE"])
            for uid in reversed(recent):  # newest first
                seen_flag = "UNSEEN" if uid in unseen_set else "seen"
                print(f"  uid {uid} [{seen_flag}]: {_subject_of(resp, uid)}")
        else:
            print("  (inbox empty)")

    except Exception as e:
        print(f"EMAIL DEBUG FAILED: {type(e).__name__}: {e}")
        sys.exit(1)
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


if __name__ == "__main__":
    main()
