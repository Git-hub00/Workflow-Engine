# services/worker/notifier/email_probe.py
#
# WHY this file exists:
# A minimal connectivity/credentials probe for the Gmail mailbox — NOT the real
# email adapter. Before we build the polling adapter (that turns inbound email
# replies into /v1/events calls), we want a dead-simple check that the app
# password works and IMAP is reachable. It logs in, selects INBOX read-only,
# reports the message count, and prints the last few subjects. It NEVER prints
# the password.

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from imapclient import IMAPClient

# Load services/api/.env. This file is at services/worker/notifier/email_probe.py,
# so the project root is parents[3]; the .env lives under services/api/.
ENV_PATH = Path(__file__).resolve().parents[3] / "services" / "api" / ".env"
load_dotenv(ENV_PATH)

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
IMAP_HOST = os.getenv("IMAP_HOST")


def main():
    print(f"Using .env: {ENV_PATH}")
    print(f"Address:    {GMAIL_ADDRESS}")
    print(f"IMAP host:  {IMAP_HOST}")

    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and IMAP_HOST):
        print("ERROR: missing one of GMAIL_ADDRESS / GMAIL_APP_PASSWORD / IMAP_HOST in .env")
        sys.exit(1)

    client = None
    try:
        # SSL IMAP connection + app-password login.
        client = IMAPClient(IMAP_HOST, ssl=True)
        client.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)

        # Read-only select so the probe can't accidentally mark mail as read, etc.
        client.select_folder("INBOX", readonly=True)

        all_uids = client.search(["ALL"])
        print(f"INBOX total messages: {len(all_uids)}")

        # Show subjects of the most recent 3 messages (highest UIDs), if any.
        recent = all_uids[-3:] if all_uids else []
        if recent:
            resp = client.fetch(recent, ["ENVELOPE"])
            print("Most recent subjects:")
            for uid in reversed(recent):  # newest first
                env = resp[uid][b"ENVELOPE"]
                subj = env.subject.decode("utf-8", "replace") if env.subject else "(no subject)"
                print(f"  - {subj}")
        else:
            print("Inbox is empty (no messages).")

        print("IMAP LOGIN OK")

    except Exception as e:
        # Never print the password; just the error class + message.
        print(f"IMAP LOGIN FAILED: {type(e).__name__}: {e}")
        sys.exit(1)
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


if __name__ == "__main__":
    main()
