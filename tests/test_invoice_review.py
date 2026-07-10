# WHY this file exists:
# End-to-end check that the bounded decision node review_invoice() routes the
# four prototype invoices exactly as the deterministic "hard rail" rules dictate,
# using the REAL seeded config thresholds from definitions/invoice.pdd.json.
# It also prints the LLM rationale for each so we can eyeball that the LLM only
# explains the route (it can never change it). Route correctness is the only
# assertion; the rationale is informational.

import sys
import json
from pathlib import Path

# STEP 1 — make the decisions module importable.
# WHY: review_invoice lives in services/worker/decisions/invoice_review.py, which
# is not on sys.path. We compute that directory RELATIVE to this test file (so it
# works regardless of the current working directory) and prepend it before import.
ROOT = Path(__file__).resolve().parent.parent
DECISIONS_DIR = ROOT / "services" / "worker" / "decisions"
sys.path.insert(0, str(DECISIONS_DIR))

from invoice_review import review_invoice  # noqa: E402  (import after sys.path tweak)

# STEP 2 — load the REAL config the seed script published.
# WHY: using the actual pdd config (autoApproveUnder=500, financeThreshold=5000,
# requiredFields, approvedVendors, ...) means the test exercises the same
# thresholds the running system would, not hand-picked test values.
PDD_PATH = ROOT / "definitions" / "invoice.pdd.json"
cfg = json.loads(PDD_PATH.read_text(encoding="utf-8"))["config"]

# STEP 3 — the four prototype invoices and their EXPECTED routes.
# Each expected route is annotated with the specific rule that fires, given
# cfg = {autoApproveUnder: 500, financeThreshold: 5000,
#        requiredFields: [poNumber, costCenter, taxId],
#        approvedVendors: [Acme Supplies, Globex Inc, Initech, Northwind Traders]}.
CASES = [
    (
        {"vendor": "Acme Supplies", "amount": 180, "poNumber": "PO-1", "costCenter": "CC-1", "taxId": "TX-1"},
        "AUTO_APPROVE",
        # WHY AUTO_APPROVE: all required fields present (no REQUEST_INFO), vendor is
        # on the approved list and amount 180 is not > 20000 (no anomalies), and
        # amount 180 < autoApproveUnder(500) -> the AUTO_APPROVE branch fires.
    ),
    (
        {"vendor": "Umbrella Corp", "amount": 2400, "poNumber": "PO-2", "costCenter": "CC-2"},
        "REQUEST_INFO",
        # WHY REQUEST_INFO: taxId is missing from requiredFields, so `missing` is
        # non-empty. The missing-fields guard is checked FIRST, so it short-circuits
        # to REQUEST_INFO regardless of amount or the unapproved vendor.
    ),
    (
        {"vendor": "Globex Inc", "amount": 4800, "poNumber": "PO-3", "costCenter": "CC-3", "taxId": "TX-3"},
        "MANAGER_ONLY",
        # WHY MANAGER_ONLY: nothing missing; Globex is approved and 4800 is not
        # > 20000 (no anomalies); 4800 is NOT < 500 (not auto-approve) and NOT
        # >= financeThreshold(5000) -> falls through to the MANAGER_ONLY else branch.
    ),
    (
        {"vendor": "Stark Trading", "amount": 24000, "poNumber": "PO-4", "costCenter": "CC-4", "taxId": "TX-4"},
        "MANAGER_THEN_FINANCE",
        # WHY MANAGER_THEN_FINANCE: nothing missing, but Stark Trading is NOT on the
        # approved list AND 24000 > 20000 -> two anomalies. Also 24000 >= 5000. The
        # (amount >= financeThreshold OR anomalies) guard fires -> MANAGER_THEN_FINANCE.
    ),
]


def main():
    # STEP 4 — run each invoice through the decision node and report.
    failures = []
    for invoice, expected, *_ in CASES:
        result = review_invoice(invoice, cfg)
        route = result["route"]
        ok = route == expected
        if not ok:
            failures.append((invoice["vendor"], expected, route))
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {invoice['vendor']:<15} amount={invoice['amount']:<6} "
              f"route={route:<22} expected={expected}")
        print(f"        rationale: {result['rationale']}")
        print()

    # STEP 5 — overall verdict; non-zero exit if any route was wrong.
    if failures:
        print("ROUTE MISMATCHES:")
        for vendor, expected, got in failures:
            print(f"  - {vendor}: expected {expected}, got {got}")
        sys.exit(1)

    print("ALL ROUTES CORRECT")


if __name__ == "__main__":
    main()
