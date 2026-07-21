#!/usr/bin/env python3
"""Validate a Process Definition Document (PDD).

Dependency-free structural validation used both as a CLI (dev/CI) and, later, by
the Definition Service on publish (Phase 3). Checks the graph is well-formed:
required keys, valid node types, unique ids, exactly one start + a reachable end,
every edge points at a real node, human_task roles exist in the roles map, and
llm_decision routes have edge mappings.

Usage:
    python scripts/validate_pdd.py [path ...]      # defaults to definitions/invoice.pdd.json
Exit code 0 = all valid, 1 = at least one error.
"""
import json
import sys
from pathlib import Path

NODE_TYPES = {
    "start", "end", "automated", "llm_decision", "human_task",
    "gateway_exclusive", "gateway_fork", "gateway_join", "timer",
}


def targets_of(node: dict) -> list:
    """All node ids this node can transition to (next + edges in either form)."""
    out = []
    nxt = node.get("next")
    if isinstance(nxt, str):
        out.append(nxt)
    edges = node.get("edges")
    if isinstance(edges, dict):
        out += [v for v in edges.values() if isinstance(v, str)]
    elif isinstance(edges, list):
        out += [e["to"] for e in edges if isinstance(e, dict) and "to" in e]
    return out


def validate(pdd: dict):
    errs, warns = [], []

    for key in ("process_key", "version", "roles", "nodes"):
        if key not in pdd:
            errs.append(f"missing top-level key: {key}")
    if errs:
        return errs, warns

    roles = pdd.get("roles", {})
    if not isinstance(roles, dict):
        errs.append("roles must be an object")
        roles = {}

    nodes = pdd.get("nodes", [])
    if not isinstance(nodes, list) or not nodes:
        errs.append("nodes must be a non-empty list")
        return errs, warns

    ids = [n.get("id") for n in nodes]
    idset = set(ids)
    dupes = sorted({i for i in ids if ids.count(i) > 1 and i is not None})
    if dupes:
        errs.append(f"duplicate node ids: {dupes}")

    starts = [n for n in nodes if n.get("type") == "start"]
    ends = [n for n in nodes if n.get("type") == "end"]
    if len(starts) != 1:
        errs.append(f"exactly one 'start' node required (found {len(starts)})")
    if not ends:
        errs.append("at least one 'end' node required")

    for n in nodes:
        nid = n.get("id", "<no-id>")
        if "id" not in n:
            errs.append("a node is missing 'id'")
        ntype = n.get("type")
        if ntype not in NODE_TYPES:
            errs.append(f"node {nid!r}: invalid type {ntype!r}")
        for tgt in targets_of(n):
            if tgt not in idset:
                errs.append(f"node {nid!r}: edge points to unknown node {tgt!r}")
        if ntype == "human_task":
            role = (n.get("assignment") or {}).get("role")
            if not role:
                errs.append(f"node {nid!r}: human_task needs assignment.role")
            elif role not in roles:
                errs.append(f"node {nid!r}: role {role!r} not in roles map {sorted(roles)}")
        if ntype == "llm_decision":
            routes = n.get("routes")
            edges = n.get("edges")
            if not isinstance(routes, list) or not routes:
                errs.append(f"node {nid!r}: llm_decision needs a non-empty routes[]")
            if not isinstance(edges, dict):
                errs.append(f"node {nid!r}: llm_decision needs edges map {{EDGE: node_id}}")
            elif isinstance(routes, list):
                for r in routes:
                    edge = r.get("edge") if isinstance(r, dict) else None
                    if edge and edge not in edges:
                        errs.append(f"node {nid!r}: route {edge!r} has no entry in edges map")

    # reachability from start (only meaningful if structure is otherwise sane)
    if len(starts) == 1 and not dupes:
        byid = {n["id"]: n for n in nodes if "id" in n}
        seen, stack = set(), [starts[0]["id"]]
        while stack:
            cur = stack.pop()
            if cur in seen or cur not in byid:
                continue
            seen.add(cur)
            stack += targets_of(byid[cur])
        unreachable = sorted(idset - seen)
        if unreachable:
            warns.append(f"unreachable nodes (never entered from start): {unreachable}")
        if not any(byid.get(x, {}).get("type") == "end" for x in seen):
            errs.append("no 'end' node is reachable from 'start'")

    return errs, warns


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
        errs, warns = validate(pdd)
        for w in warns:
            print(f"[warn] {p}: {w}")
        if errs:
            rc = 1
            print(f"[FAIL] {p}: {len(errs)} error(s):")
            for e in errs:
                print(f"   - {e}")
        else:
            print(f"[OK]   {p}: valid PDD ({len(pdd.get('nodes', []))} nodes)")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
