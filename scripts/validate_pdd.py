#!/usr/bin/env python3
"""CLI wrapper around scripts/pdd_validation.validate_pdd (the shared validator).

Usage:
    python scripts/validate_pdd.py [path ...]      # defaults to definitions/invoice.pdd.json
Exit code 0 = all valid, 1 = at least one error.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pdd_validation import validate_pdd  # noqa: E402


def main(argv) -> int:
    paths = argv[1:] or ["definitions/invoice.pdd.json"]
    rc = 0
    for p in paths:
        try:
            pdd = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[FAIL] {p}: cannot read/parse: {exc}")
            rc = 1
            continue
        errors, warnings = validate_pdd(pdd)
        for w in warnings:
            print(f"[warn] {p}: {w}")
        if errors:
            rc = 1
            print(f"[FAIL] {p}: {len(errors)} error(s):")
            for e in errors:
                print(f"   - {e}")
        else:
            print(f"[OK]   {p}: valid PDD ({len(pdd.get('nodes', []))} nodes)")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
