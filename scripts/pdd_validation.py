# scripts/pdd_validation.py
#
# Canonical, dependency-free PDD structural validation. Single source of truth,
# imported by the CLI (validate_pdd.py) AND the API's Definition Service (P3). It
# checks the graph is well-formed; Keycloak role-existence is layered on top by
# the API (which has network access), not here.
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


def validate_pdd(pdd: dict):
    """Return (errors, warnings). errors are blocking; warnings are advisory."""
    errors, warnings = [], []

    for key in ("process_key", "version", "roles", "nodes"):
        if key not in pdd:
            errors.append(f"missing top-level key: {key}")
    if errors:
        return errors, warnings

    roles = pdd.get("roles", {})
    if not isinstance(roles, dict):
        errors.append("roles must be an object")
        roles = {}

    nodes = pdd.get("nodes", [])
    if not isinstance(nodes, list) or not nodes:
        errors.append("nodes must be a non-empty list")
        return errors, warnings

    ids = [n.get("id") for n in nodes]
    idset = set(ids)
    dupes = sorted({i for i in ids if ids.count(i) > 1 and i is not None})
    if dupes:
        errors.append(f"duplicate node ids: {dupes}")

    starts = [n for n in nodes if n.get("type") == "start"]
    ends = [n for n in nodes if n.get("type") == "end"]
    if len(starts) != 1:
        errors.append(f"exactly one 'start' node required (found {len(starts)})")
    if not ends:
        errors.append("at least one 'end' node required")

    for n in nodes:
        nid = n.get("id", "<no-id>")
        if "id" not in n:
            errors.append("a node is missing 'id'")
        ntype = n.get("type")
        if ntype not in NODE_TYPES:
            errors.append(f"node {nid!r}: invalid type {ntype!r}")
        for tgt in targets_of(n):
            if tgt not in idset:
                errors.append(f"node {nid!r}: edge points to unknown node {tgt!r}")
        if ntype == "human_task":
            role = (n.get("assignment") or {}).get("role")
            if not role:
                errors.append(f"node {nid!r}: human_task needs assignment.role")
            elif role not in roles:
                errors.append(f"node {nid!r}: role {role!r} not in roles map {sorted(roles)}")
        if ntype == "llm_decision":
            routes = n.get("routes")
            edges = n.get("edges")
            if not isinstance(routes, list) or not routes:
                errors.append(f"node {nid!r}: llm_decision needs a non-empty routes[]")
            if not isinstance(edges, dict):
                errors.append(f"node {nid!r}: llm_decision needs edges map {{EDGE: node_id}}")
            elif isinstance(routes, list):
                for r in routes:
                    edge = r.get("edge") if isinstance(r, dict) else None
                    if edge and edge not in edges:
                        errors.append(f"node {nid!r}: route {edge!r} has no entry in edges map")

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
            warnings.append(f"unreachable nodes (never entered from start): {unreachable}")
        if not any(byid.get(x, {}).get("type") == "end" for x in seen):
            errors.append("no 'end' node is reachable from 'start'")

    return errors, warnings
