# services/worker/graph/pdd_graph.py
#
# GENERIC "PDD -> LangGraph" compiler — the heart of "LangGraph on top".
#
# Turns ANY Process Definition Document (PDD) into a LangGraph StateGraph:
#   * every PDD node becomes a LangGraph node,
#   * every PDD edge (next / edges / routes) becomes a LangGraph edge,
#   * routing uses the SAME deterministic rules as the old Temporal interpreter.
#
# Definition-driven: a brand-new workflow the author publishes runs with NO code
# change. Node BEHAVIOUR is supplied by a `handlers` object so the same compiler
# works in production (real activities + Temporal human-wait) and in tests
# (mock handlers, no DB/Temporal/LLM).
#
# Nodes are SYNCHRONOUS (LangGraph's interrupt() needs the runnable context,
# which is reliably present in sync nodes run via graph.invoke). Handlers that
# need async work (e.g. a Temporal client) run it internally (asyncio.run) — the
# graph itself is driven synchronously by the runner.
#
# Two modes:
#   durable=False -> human steps call handlers.run_human() straight through
#                    (fast unit tests, no pausing).
#   durable=True  -> human steps split into an "open" node (create task + start
#                    reminders) and a "wait" node that calls interrupt() to PAUSE
#                    the run; with a checkpointer the run is saved and resumed
#                    later with the person's decision (waits days/weeks/months).

from typing import Any, Optional, TypedDict


class ProcState(TypedDict, total=False):
    txn_id: str
    pdd: dict
    data: dict
    route: str
    missing: list            # fields the last decision flagged as missing
    decision: dict
    quorum_approved: bool
    outcome: str
    _next: str


# --- pure edge helpers (same semantics as the Temporal interpreter) --------
def _match(when: Optional[str], ctx: dict) -> bool:
    if when is None:
        return False
    w = when.strip()
    if w == "default":
        return True
    if "==" in w:
        lhs, rhs = w.split("==", 1)
        return str(ctx.get(lhs.strip())) == rhs.strip().strip("'\"")
    if "!=" in w:
        lhs, rhs = w.split("!=", 1)
        return str(ctx.get(lhs.strip())) != rhs.strip().strip("'\"")
    return bool(ctx.get(w))


def _pick_edge(edges, ctx: dict):
    for edge in edges or []:
        if _match(edge.get("when"), ctx):
            return edge.get("to")
    return None


def compute_next(node: dict, state: dict):
    ntype = node.get("type")
    if ntype in ("start", "automated", "timer"):
        return node.get("next")
    if ntype == "llm_decision":
        return (node.get("edges") or {}).get(state.get("route"))
    if ntype == "gateway_exclusive":
        return _pick_edge(node.get("edges", []),
                          {**(state.get("data") or {}), "route": state.get("route")})
    if ntype == "human_task":
        if node.get("edges"):
            ctx = {
                "decision": (state.get("decision") or {}).get("decision"),
                "route": state.get("route"),
                "quorum_approved": state.get("quorum_approved"),
            }
            return _pick_edge(node["edges"], ctx)
        return node.get("next")
    return None


class Handlers:
    """Override the methods you need. All synchronous."""

    def run_action(self, txn_id, action, data):     # automated
        return data

    def decide(self, node, data, cfg):              # llm_decision
        return {"route": None}

    def run_human(self, txn_id, node):              # human (sync mode)
        return {}

    def open_human(self, txn_id, node, missing=None):   # human (durable mode)
        return None

    def wait(self, seconds):                        # timer
        return None

    def finish(self, txn_id, data, outcome):        # end
        return None

    def merge(self, data, decision):                # collect-info resubmit
        if not isinstance(decision, dict):
            return data
        corrected = decision.get("data")
        if corrected:
            return {**data, **corrected}
        control = {"decision", "reason", "kind", "idempotency_key",
                   "transaction_id", "participant", "terminal", "approved", "auto"}
        extra = {k: v for k, v in decision.items() if k not in control}
        return {**data, **extra} if extra else data


def _apply_human_decision(node, state, decision, h):
    upd = {"decision": decision or {}}
    if (node.get("completion") or {}).get("mode") == "quorum":
        upd["quorum_approved"] = (decision or {}).get("approved")
    elif not node.get("edges"):
        upd["data"] = h.merge(dict(state.get("data") or {}), decision or {})
    upd["_next"] = compute_next(node, {**state, **upd})
    return upd


def _make_node_fn(node, cfg, h):
    ntype = node.get("type")

    def fn(state):
        data = dict(state.get("data") or {})
        upd: dict = {}
        if ntype == "automated":
            upd["data"] = h.run_action(state["txn_id"], node.get("action"), data) or data
        elif ntype == "llm_decision":
            res = h.decide(node, data, cfg) or {}
            upd["route"] = res.get("route")
            upd["missing"] = res.get("missing") or []
        elif ntype == "timer":
            h.wait(float((node.get("timeout") or {}).get("seconds", 0)))
        elif ntype == "human_task":                  # synchronous mode (tests)
            return _apply_human_decision(node, state, h.run_human(state["txn_id"], node) or {}, h)
        elif ntype == "end":
            outcome = node.get("outcome", "completed")
            h.finish(state["txn_id"], data, outcome)
            upd["outcome"] = outcome
        upd["_next"] = compute_next(node, {**state, **upd})
        return upd

    return fn


def _make_open_fn(node, h):
    def fn(state):
        # Pass the missing-field list the last decision computed so the task can
        # show it and the email reply-parser knows exactly which fields to expect.
        h.open_human(state["txn_id"], node, state.get("missing"))
        return {}
    return fn


def _make_wait_fn(node, h):
    def fn(state):
        from langgraph.types import interrupt
        decision = interrupt({"node": node["id"], "await": "human"})
        return _apply_human_decision(node, state, decision if isinstance(decision, dict) else {}, h)
    return fn


def _router_factory(entry_map):
    def router(state):
        from langgraph.graph import END
        return entry_map.get(state.get("_next")) or END
    return router


def build_process_graph(pdd: dict, handlers: Optional[Handlers] = None,
                        checkpointer: Any = None, durable: bool = False):
    """Compile a LangGraph graph for THIS pdd (generic — any workflow)."""
    from langgraph.graph import StateGraph, START, END

    h = handlers or Handlers()
    node_list = [n for n in pdd.get("nodes", []) if "id" in n]
    cfg = {**pdd.get("config", {}), "roles": pdd.get("roles", {})}

    g = StateGraph(ProcState)
    non_start = [n for n in node_list if n.get("type") != "start"]

    def is_durable_human(n):
        return durable and n.get("type") == "human_task"

    entry_map = {n["id"]: (f"{n['id']}__open" if is_durable_human(n) else n["id"]) for n in non_start}
    graph_ids = []
    for node in non_start:
        nid = node["id"]
        if is_durable_human(node):
            g.add_node(f"{nid}__open", _make_open_fn(node, h))
            g.add_node(f"{nid}__wait", _make_wait_fn(node, h))
            graph_ids += [f"{nid}__open", f"{nid}__wait"]
        else:
            g.add_node(nid, _make_node_fn(node, cfg, h))
            graph_ids.append(nid)

    starts = [n for n in node_list if n.get("type") == "start"]
    first_pdd = (starts[0].get("next") if starts else None) or (non_start[0]["id"] if non_start else None)
    g.add_edge(START, entry_map.get(first_pdd, END))

    mapping = {gid: gid for gid in graph_ids}
    mapping[END] = END
    router = _router_factory(entry_map)
    for node in non_start:
        nid = node["id"]
        if is_durable_human(node):
            g.add_edge(f"{nid}__open", f"{nid}__wait")
            g.add_conditional_edges(f"{nid}__wait", router, mapping)
        elif node.get("type") == "end":
            g.add_edge(nid, END)
        else:
            g.add_conditional_edges(nid, router, mapping)

    return g.compile(checkpointer=checkpointer)
