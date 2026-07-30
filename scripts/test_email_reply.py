#!/usr/bin/env python3
"""Tests for the inbound-email reply parser.

These cover the two bugs that silently corrupted real decisions:

  1. QUOTED TEXT. Our approval email ends "...reply with 'approve' or 'reject'", and
     nearly every mail client quotes the original underneath the reply. The decision
     scanner read the WHOLE body, found "reject" in OUR OWN quoted instructions, and
     (reject having priority) turned EVERY approval into a REJECTION.

  2. TEMPLATE PLACEHOLDERS. The request-info email lists "poNumber: <value>" lines.
     The field parser walked every line with last-one-wins, so the quoted template
     OVERWROTE the real value the person had typed above it.

Run: python3 scripts/test_email_reply.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "services", "worker", "notifier"))

from email_adapter import (  # noqa: E402
    _parse_body_fields,
    _parse_field_lines,
    decode_subject,
    extract_decision,
    extract_txn_id,
    strip_quoted,
)

FAILS = 0


def ok(cond, msg):
    global FAILS
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILS += 1


# --- 1. A real Gmail approval reply -----------------------------------------
GMAIL_APPROVE = """approve

On Wed, 29 Jul 2026 at 16:42, Invoice Bot <bot@example.com> wrote:
> Hello,
>
> A request in 'invoice_approval' is waiting for your approval at the
> 'manager' step.
>
> Open the app to approve or reject, or reply with 'approve' or 'reject'.
"""
ok(extract_decision(GMAIL_APPROVE) == "approve",
   "an 'approve' reply quoting our own 'approve or reject' text reads as APPROVE")

GMAIL_REJECT = "reject - the amount is wrong\n\nOn Wed wrote:\n> reply with 'approve' or 'reject'.\n"
ok(extract_decision(GMAIL_REJECT) == "reject", "a genuine reject still reads as REJECT")

# Outlook style: header block instead of "On ... wrote:".
OUTLOOK = """Approve

________________________________
From: Invoice Bot <bot@example.com>
Sent: Wednesday, 29 July 2026 16:42
Subject: Action needed

Open the app to approve or reject.
"""
ok(extract_decision(OUTLOOK) == "approve", "Outlook-style quoting is stripped too")

# Reject genuinely wins when BOTH words are in the person's OWN text.
ok(extract_decision("I approve but actually reject this") == "reject",
   "an ambiguous reply is still treated conservatively as reject")

ok(extract_decision("no idea") is None, "a reply with no decision word returns None")
ok(extract_decision("") is None, "an empty body returns None")

# Bottom-posting: nothing above the quote -> fall back to the whole body.
ok(extract_decision("On Wed wrote:\n> please decide\napprove") == "approve",
   "a reply typed BELOW the quote is still read (fallback to the full body)")


# --- 2. Field replies --------------------------------------------------------
VENDOR_REPLY = """poNumber: PO-8891
costCenter: CC-42
taxId: GB123456789

On Wed, 29 Jul 2026 at 16:42, Invoice Bot <bot@example.com> wrote:
> Please REPLY to this email with one line per item, exactly like this:
> poNumber: <value>
> costCenter: <value>
> taxId: <value>
"""
parsed = _parse_field_lines(VENDOR_REPLY, ["poNumber", "costCenter", "taxId"])
ok(parsed.get("poNumber") == "PO-8891", "the vendor's real poNumber survives the quoted template")
ok(parsed.get("costCenter") == "CC-42", "costCenter survives")
ok(parsed.get("taxId") == "GB123456789", "taxId survives")
ok("<value>" not in "".join(parsed.values()), "no template placeholder leaks into the data")

# Label spellings are matched loosely.
loose = _parse_field_lines("PO Number: PO-1\npo_number: PO-2\n", ["poNumber"])
ok(loose.get("poNumber") == "PO-1", "'PO Number:' matches poNumber, and the FIRST value wins")

# A placeholder ON ITS OWN is ignored rather than stored.
ok(_parse_field_lines("poNumber: <value>", ["poNumber"]) == {},
   "a bare placeholder is not accepted as an answer")

# No requested fields -> nothing is invented (there is no invoice fallback list).
ok(_parse_field_lines("poNumber: PO-9", None) == {},
   "with no field list nothing is parsed (no hardcoded invoice fields)")

# Intake parsing keeps first-wins + placeholder filtering too.
intake = _parse_body_fields("amount: 500\nvendor: Acme\namount: 999", ["amount", "vendor"])
ok(intake == {"amount": "500", "vendor": "Acme"}, "intake parsing is first-wins")


# --- 3. strip_quoted itself --------------------------------------------------
ok(strip_quoted("hi\n> quoted") == "hi", "'>' starts the quote")
ok(strip_quoted("hi\n-----Original Message-----\nx") == "hi", "Original Message divider")
ok(strip_quoted("hi\nOn Mon, someone wrote:\nx") == "hi", "'On ... wrote:' divider")
ok(strip_quoted("plain text only") == "plain text only", "an unquoted body is unchanged")


# --- 4. Subject tag ----------------------------------------------------------
UUID = "3f7c1a2b-4d5e-6f70-8a9b-0c1d2e3f4a5b"
ok(extract_txn_id(f"Re: [ref-{UUID}] Action needed") == UUID, "the new [ref-…] tag is matched")
ok(extract_txn_id(f"Re: [invoice-{UUID}] Action needed") == UUID,
   "tags already sent as [invoice-…] still correlate")
ok(extract_txn_id("Re: no tag here") is None, "an untagged subject returns None")

# A malformed encoded-word must not raise (it used to kill the whole poll pass).
ok(decode_subject("=?utf-8?Q?ok?=") == "ok", "a valid encoded-word decodes")
ok(isinstance(decode_subject("=?bogus-charset?Q?x?="), str),
   "an unknown charset returns a string instead of raising")

print()
print(f"{FAILS} FAILURE(S)" if FAILS else "ALL GOOD")
sys.exit(1 if FAILS else 0)
