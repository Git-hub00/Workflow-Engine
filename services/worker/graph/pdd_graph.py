# services/worker/graph/pdd_graph.py
#
# GENERIC "PDD -> LangGraph" compiler — the heart of "LangGraph on top".
#
# Any Process Definition (PDD), in ANY dialect, becomes a LangGraph StateGraph:
#   * every step becomes a graph node,
#   * every connection (next / edges / routes) becomes a graph edge,
#   * routing is deterministic and driven purely by the definition.
#
# The PDD is first put through pdd_norm.normalize_pdd(), so a step may be spelled
# "human_task" or "approval", carry "next" or "then", and a quorum may sit on ANY
# step (it is detected from completion.n/of — NEVER from a step being named
# "finance"). That is what lets five unrelated workflows run on one engine.
#
# Node BEHAVIOUR comes from a `handlers` object, so the same compiler serves
# production (real activities + Temporal human waits) and tests (mocks).
#
# Nodes are SYNCHRONOUS: LangGraph's interrupt() needs the runnable context,
# which is reliably present in sync nodes driven by graph.invoke().
#
# Modes:
#   durable=False -> human steps call handlers.run_human() straight through (tests)
#   durable=True  -> human steps split into "open" (create task + notify) and
#                    "wait" (interrupt -> PAUSE). With a checkpointer the run is
#                    saved and resumed later with the person's decision, so it can
#                    wait days, weeks or months.

from typing import Any, Optional, TypedDict

from pdd_norm import normalize_pdd, normalize_node, is_quorum


class ProcState(TypedDict, total=False):
    txn_id: str
    pdd: dict
    data: dict
    route: str
    missing: list
    decision: dict
    quorum_approved: bool
    outcome: str
    _next: str


# --- pure edge helpers ----------------------------------------------------
def _match(when: Optional[str], ctx: dict) -> bool:
    if when is None:
        return False
    w = str(when).strip()
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


def _canon(node: dict) -> dict:
    """Accept either a canonical node or a raw PDD node."""
    if isinstance(node, dict) and node.get("_canon"):
        return node
    return normalize_node(node or {})


def compute_next(node: dict, state: dict):
    """Next step id to run (or None for a terminal step). Works on any dialect."""
    n = _canon(node)
    ntype = n["type"]
    if ntype in ("start", "automated", "timer"):
        return n.get("next")
    if ntype == "decision":
        route = state.get("route")
        for e in n.get("edges") or []:
            if e.get("edge") == route:
                return e.get("to")
        for r in n.get("routes") or []:
            if r.get("edge") == route:
                return r.get("to")
        return None
    if ntype == "gateway":
        return _pick_edge(n.get("edges"), {**(state.get("data") or {}), "route": state.get("route")})
    if ntype == "human":
        edges = n.get("edges") or []
        if edges:
            ctx = {
                "decision": (state.get("decision") or {}).get("decision"),
                "route": state.get("route"),
                "quorum_approved": state.get("quorum_approved"),
            }
            return _pick_edge(edges, ctx)
        return n.get("next")
    return None  # end / unknown


class Handlers:
    """Override what you need. All synchronous. `node` is a CANONICAL node
    (node['raw'] holds the original PDD node)."""

    def run_action(self, txn_id, action, data):        # automated
        return data

    def decide(self, node, data, cfg, txn_id=None):    # decision -> {route, missing, rationale}
        return {"route": None}

    def run_human(self, txn_id, node):                 # human (sync mode)
        return {}

    def open_human(self, txn_id, node, missing=None):  # human (durable mode)
        return None

    def wait(self, seconds):                           # timer
        return None

    def finish(self, txn_id, data, outcome):           # end
        return None

    def merge(self, data, decision):                   # collect-info resubmit
        if not isinstance(decision, dict):
            return data
        corrected = decision.get("data")
        if corrected:
            return {**data, **corrected}
        control = {"decision", "reason", "kind", "idempotency_key", "transaction_id",
                   "participant", "terminal", "approved", "auto"}
        extra = {k: v for k, v in decision.items() if k not in control}
        return {**data, **extra} if extra else data


def _apply_human_decision(node, state, decision, h):
    upd = {"decision": decision or {}}
    if is_quorum(node):
        upd["quorum_approved"] = (decision or {}).get("approved")
    elif not (node.get("edges") or []):
        upd["data"] = h.merge(dict(state.get("data") or {}), decision or {})
    upd["_next"] = compute_next(node, {**state, **upd})
    return upd


def _make_node_fn(node, cfg, h):
    ntype = node["type"]

    def fn(state):
        data = dict(state.get("data") or {})
        upd: dict = {}
        if ntype == "automated":
            upd["data"] = h.run_action(state["txn_id"], node.get("action"), data) or data
        elif ntype == "decision":
            res = h.decide(node, data, cfg, state["txn_id"]) or {}
            upd["route"] = res.get("route")
            upd["missing"] = res.get("missing") or []
        elif ntype == "timer":
            h.wait(float(node.get("sleep_seconds") or 0))
        elif ntype == "human":                       # synchronous mode (tests)
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
        # Pass the missing-field list the last decision computed, so the task and
        # its email can say exactly which fields the person must supply.
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
    """Compile a LangGraph graph for THIS pdd (generic — any workflow, any dialect)."""
    from langgraph.graph import StateGraph, START, END

    h = handlers or Handlers()
    norm = normalize_pdd(pdd)
    cfg = {**(norm["config"] or {}), "roles": norm["roles"] or {}}

    g = StateGraph(ProcState)
    steps = [n for n in norm["nodes"] if n["type"] != "start"]

    def durable_human(n):
        return durable and n["type"] == "human"

    entry_map = {n["id"]: (f"{n['id']}__open" if durable_human(n) else n["id"]) for n in steps}
    graph_ids = []
    for node in steps:
        nid = node["id"]
        if durable_human(node):
            g.add_node(f"{nid}__open", _make_open_fn(node, h))
            g.add_node(f"{nid}__wait", _make_wait_fn(node, h))
            graph_ids += [f"{nid}__open", f"{nid}__wait"]
        else:
            g.add_node(nid, _make_node_fn(node, cfg, h))
            graph_ids.append(nid)

    g.add_edge(START, entry_map.get(norm["start_id"], END))

    mapping = {gid: gid for gid in graph_ids}
    mapping[END] = END
    router = _router_factory(entry_map)
    for node in steps:
        nid = node["id"]
        if durable_human(node):
            g.add_edge(f"{nid}__open", f"{nid}__wait")
            g.add_conditional_edges(f"{nid}__wait", router, mapping)
        elif node["type"] == "end":
            g.add_edge(nid, END)
        else:
            g.add_conditional_edges(nid, router, mapping)

    return g.compile(checkpointer=checkpointer)
