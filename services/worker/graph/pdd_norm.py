# services/worker/graph/pdd_norm.py
#
# TOLERANT PDD READER (normalizer).
#
# Every workflow is described by a Process Definition Document (PDD). Different
# authors / tools spell the same idea differently:
#
#     {"type": "human_task", "assignment": {"role": "manager"}, "next": "pay"}
#     {"kind": "approval",   "role": "manager",                 "then": "pay"}
#
# Both mean "a person with role manager approves, then go to pay". This module
# turns ANY such dialect into ONE canonical shape the engine understands, so a
# brand-new process runs with no code change and no rigid JSON format.
#
# Canonical node types: start | automated | decision | gateway | human | timer | end
#
# Pure Python: no third-party imports, so it is unit-testable anywhere.

# --- type aliases ---------------------------------------------------------
_TYPE_ALIASES = {
    # start
    "start": "start", "begin": "start", "entry": "start", "trigger": "start",
    # end
    "end": "end", "finish": "end", "stop": "end", "terminate": "end", "done": "end",
    # automated (runs a built-in action)
    "automated": "automated", "automatic": "automated", "action": "automated",
    "service": "automated", "script": "automated", "system": "automated",
    "service_task": "automated", "auto": "automated",
    # AI / bounded decision (asks the decision engine to choose a route)
    "llm_decision": "decision", "decision": "decision", "ai_decision": "decision",
    "ai_review": "decision", "review": "decision", "llm": "decision",
    # plain rule gateway (evaluates rules on the data, no AI)
    "gateway_exclusive": "gateway", "gateway": "gateway", "exclusive": "gateway",
    "switch": "gateway", "choice": "gateway", "condition": "gateway", "branch": "gateway",
    # human step (approval / collect info)
    "human_task": "human", "human": "human", "approval": "human", "approve": "human",
    "user_task": "human", "manual": "human", "task": "human", "collect": "human",
    "collect_info": "human", "request_info": "human", "form": "human",
    # timer
    "timer": "timer", "wait": "timer", "delay": "timer", "sleep": "timer", "pause": "timer",
}

_ID_KEYS = ("id", "key", "name", "node_id", "step")
_TYPE_KEYS = ("type", "kind", "node_type")
_NEXT_KEYS = ("next", "then", "goto", "go_to", "to", "next_node", "default_next")
_EDGES_KEYS = ("edges", "transitions", "branches", "paths", "links")
_ROUTES_KEYS = ("routes", "options", "choices", "cases")
_ACTION_KEYS = ("action", "activity", "do", "operation", "task_name")
_OUTCOME_KEYS = ("outcome", "result", "status", "final_status")
_ROLE_KEYS = ("role", "assignee_role", "assigned_role", "group", "candidate_role")
_WHEN_KEYS = ("when", "condition", "if", "expr", "rule")
_TARGET_KEYS = ("to", "target", "next", "then", "goto", "node")
_EDGE_NAME_KEYS = ("edge", "name", "id", "label", "route")

# Action aliases: older PDDs say post_to_erp; the engine action is post_to_record.
_ACTION_ALIASES = {
    "post_to_erp": "post_to_record",
    "post": "post_to_record",
    "post_to_system": "post_to_record",
    "post_to_record": "post_to_record",
    "extract": "extract_fields",
    "extract_fields": "extract_fields",
}


def _first(d, keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _as_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_int(v):
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def canon_type(raw) -> str:
    t = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _TYPE_ALIASES.get(t, t or "automated")


def canon_action(raw):
    a = str(raw or "").strip().lower()
    return _ACTION_ALIASES.get(a, a) or None


def _norm_edge_list(raw):
    """Accept [{'when':..,'to':..}] / {'EDGE': 'target'} / ['target'] -> list of
    {'when','to'} preserving order (order = priority)."""
    out = []
    if isinstance(raw, dict):
        for name, target in raw.items():
            if isinstance(target, dict):
                out.append({"when": _first(target, _WHEN_KEYS, name),
                            "to": _first(target, _TARGET_KEYS), "edge": name})
            else:
                out.append({"when": name, "to": target, "edge": name})
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                out.append({"when": _first(item, _WHEN_KEYS, "default"),
                            "to": _first(item, _TARGET_KEYS),
                            "edge": _first(item, _EDGE_NAME_KEYS)})
            elif isinstance(item, str):
                out.append({"when": "default", "to": item, "edge": None})
    return [e for e in out if e.get("to")]


def _norm_timeout(node):
    """Return (sleep_seconds, sla_hours, on_timeout)."""
    t = node.get("timeout") if isinstance(node.get("timeout"), dict) else {}
    secs = _first(t, ("seconds", "secs", "duration_seconds", "wait_seconds"))
    if secs is None:
        secs = _first(node, ("seconds", "duration_seconds", "wait_seconds"))
    hours = _first(t, ("hours", "duration_hours", "wait_hours"))
    if hours is None:
        hours = _first(node, ("hours", "wait_hours", "duration_hours"))
    sleep_s = _as_float(secs, 0.0) if secs is not None else _as_float(hours, 0.0) * 3600.0
    sla = _first(t, ("slaHours", "sla_hours", "sla"))
    if sla is None:
        sla = _first(node, ("slaHours", "sla_hours", "sla"))
    on_timeout = (_first(t, ("on_timeout", "onTimeout", "escalation"))
                  or _first(node, ("on_timeout", "onTimeout")) or "remind")
    return sleep_s, (_as_float(sla, 0.0) or None), str(on_timeout)


def _norm_completion(node):
    """Detect a quorum on ANY human step, whatever the step is called.

    Returns None for a single approver, else
    {'mode':'quorum','n':int,'of':int,'rejectShortCircuits':bool}."""
    comp = node.get("completion") if isinstance(node.get("completion"), dict) else {}
    quorum = comp.get("quorum") if isinstance(comp.get("quorum"), dict) else None
    if quorum is None and isinstance(node.get("quorum"), dict):
        quorum = node["quorum"]
    src = quorum or comp
    n = _as_int(_first(src, ("n", "required", "approvals", "min_approvals")))
    of = _as_int(_first(src, ("of", "capacity", "participants", "out_of", "total")))
    mode = str(_first(comp, ("mode", "type"), "") or "").lower()
    if mode != "quorum" and (n is None or of is None):
        return None
    if n is None or of is None:
        return None
    rsc = _first(src, ("rejectShortCircuits", "reject_short_circuits", "rejectEnds"))
    if rsc is None:
        rsc = _first(comp, ("rejectShortCircuits", "reject_short_circuits"))
    if rsc is None:
        rsc = _first(node, ("rejectShortCircuits", "reject_short_circuits"), False)
    return {"mode": "quorum", "n": n, "of": of, "rejectShortCircuits": bool(rsc)}


def _norm_form_fields(node):
    fs = node.get("form_schema") if isinstance(node.get("form_schema"), dict) else {}
    raw = fs.get("fields") or node.get("fields") or node.get("form")
    out = []
    if isinstance(raw, list):
        for f in raw:
            if isinstance(f, str):
                out.append({"key": f, "type": "string", "required": True})
            elif isinstance(f, dict):
                key = _first(f, ("key", "name", "id"))
                if not key:
                    continue
                out.append({
                    "key": key,
                    "type": (_first(f, ("type", "kind"), "string") or "string"),
                    "required": bool(f.get("required", False)),
                    **({"values": f["values"]} if isinstance(f.get("values"), list) else {}),
                })
    return out


def normalize_node(raw: dict) -> dict:
    """One PDD node -> canonical node."""
    nid = _first(raw, _ID_KEYS)
    ntype = canon_type(_first(raw, _TYPE_KEYS))
    node = {"id": nid, "type": ntype, "raw": raw, "_canon": True}

    nxt = _first(raw, _NEXT_KEYS)
    node["next"] = nxt if isinstance(nxt, str) else None

    node["edges"] = _norm_edge_list(_first(raw, _EDGES_KEYS))
    routes_raw = _first(raw, _ROUTES_KEYS)
    routes = []
    if isinstance(routes_raw, list):
        for r in routes_raw:
            if isinstance(r, dict):
                routes.append({"edge": _first(r, _EDGE_NAME_KEYS),
                               "when": _first(r, _WHEN_KEYS, "default"),
                               "to": _first(r, _TARGET_KEYS)})
    node["routes"] = routes

    if ntype == "automated":
        node["action"] = canon_action(_first(raw, _ACTION_KEYS))
    if ntype == "end":
        node["outcome"] = _first(raw, _OUTCOME_KEYS, "completed")
    if ntype == "human":
        assignment = raw.get("assignment") if isinstance(raw.get("assignment"), dict) else {}
        node["role"] = _first(assignment, _ROLE_KEYS) or _first(raw, _ROLE_KEYS)
        node["completion"] = _norm_completion(raw)
        node["form_fields"] = _norm_form_fields(raw)
    # A decision written only with edges (no explicit routes) still needs routes,
    # because the bounded decision engine chooses among `routes`.
    if ntype == "decision" and not node["routes"] and node["edges"]:
        node["routes"] = [{"edge": e.get("edge") or f"E{i + 1}", "when": e.get("when"), "to": e.get("to")}
                          for i, e in enumerate(node["edges"])]
    # Give every decision route a usable edge name, and mirror routes into edges
    # so the router can resolve a chosen route name to a target node.
    if ntype == "decision":
        known = {e.get("edge") for e in node["edges"] if e.get("edge")}
        for i, r in enumerate(node["routes"]):
            if not r.get("edge"):
                r["edge"] = f"E{i + 1}"
            if r.get("to") and r["edge"] not in known:
                node["edges"].append({"when": r.get("when"), "to": r["to"], "edge": r["edge"]})
                known.add(r["edge"])

    sleep_s, sla, on_timeout = _norm_timeout(raw)
    node["sleep_seconds"] = sleep_s
    node["sla_hours"] = sla
    node["on_timeout"] = on_timeout
    return node


def normalize_pdd(pdd: dict) -> dict:
    """Whole PDD -> canonical dict: process_key, mailbox, roles, config,
    data_schema, notifications, nodes[], nodes_by_id, start_id."""
    pdd = pdd or {}
    nodes = []
    raw_nodes = pdd.get("nodes") or pdd.get("steps") or pdd.get("activities") or []
    if isinstance(raw_nodes, dict):  # {"id": {...}} form
        raw_nodes = [{**v, "id": k} for k, v in raw_nodes.items() if isinstance(v, dict)]
    for raw in raw_nodes:
        if isinstance(raw, dict):
            n = normalize_node(raw)
            if n["id"]:
                nodes.append(n)
    by_id = {n["id"]: n for n in nodes}

    starts = [n for n in nodes if n["type"] == "start"]
    start_id = None
    if starts:
        start_id = starts[0]["next"] or (starts[0]["edges"][0]["to"] if starts[0]["edges"] else None)
    if not start_id:
        explicit = pdd.get("start") or pdd.get("start_at") or pdd.get("first")
        if isinstance(explicit, str) and explicit in by_id:
            start_id = explicit
    if not start_id:
        non_start = [n for n in nodes if n["type"] != "start"]
        start_id = non_start[0]["id"] if non_start else None

    return {
        "process_key": pdd.get("process_key") or pdd.get("key") or pdd.get("id"),
        "version": pdd.get("version"),
        "mailbox": pdd.get("mailbox") or pdd.get("inbox"),
        "roles": pdd.get("roles") or {},
        "config": pdd.get("config") or pdd.get("settings") or {},
        "data_schema": pdd.get("data_schema") or pdd.get("fields") or {},
        "notifications": pdd.get("notifications") or [],
        "nodes": nodes,
        "nodes_by_id": by_id,
        "start_id": start_id,
    }


def role_for(node: dict, roles_map: dict):
    """Logical role on a node -> real (Keycloak) role via the PDD roles map."""
    logical = node.get("role")
    if not logical:
        return None
    return (roles_map or {}).get(logical, logical)


def is_quorum(node: dict) -> bool:
    return bool((node.get("completion") or {}).get("mode") == "quorum")
