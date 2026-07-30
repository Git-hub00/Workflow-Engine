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


class DefinitionError(Exception):
    """The workflow DEFINITION is wrong (a dead end, an unknown step). Raised so the
    run fails visibly instead of quietly ending as if it had completed."""


# --- pure edge helpers ----------------------------------------------------
def _as_num(v):
    """Number for comparison, or None if the value isn't numeric."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _cmp(lhs, rhs, op: str) -> bool:
    """Compare two values: numerically when BOTH sides look numeric (so "1500" and
    1500 match), otherwise as text."""
    a, b = _as_num(lhs), _as_num(rhs)
    if a is None or b is None:
        a, b = str(lhs), str(rhs)
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    if op == "<":
        return a < b
    return a <= b


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
    # NUMERIC / ORDERING comparisons. These used to be unsupported, so a perfectly
    # reasonable rule like "amount >= 5000" fell through to the truthiness check on
    # the literal string, was always False, matched NO connection, and the run
    # ended as "completed" — a silent auto-approval. Longest operators first, so
    # ">=" is never read as ">".
    for op in (">=", "<=", ">", "<"):
        if op in w:
            lhs, rhs = w.split(op, 1)
            lhs, rhs = lhs.strip(), rhs.strip().strip("'\"")
            if lhs in ctx or rhs in ctx:
                left = ctx.get(lhs, lhs)
                right = ctx.get(rhs, rhs)
                try:
                    return _cmp(left, right, op)
                except TypeError:
                    return False
            return False
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
    """Next step id to run (or None for a terminal step). Works on any dialect.

    A NON-terminal step that resolves to nothing raises DefinitionError instead of
    returning None. Returning None sent the run straight to the graph's END, where
    the outcome defaulted to "completed" — so a broken connection or an unmatched
    condition looked exactly like a successful finish, and the requester was told
    their request had gone through."""
    n = _canon(node)
    ntype = n["type"]
    nid = n.get("id")
    if ntype in ("start", "automated", "timer"):
        nxt = n.get("next")
        if not nxt:
            raise DefinitionError(
                f"step '{nid}' ({ntype}) has no next step — connect it to another step "
                "or to an end step")
        return nxt
    if ntype == "decision":
        route = state.get("route")
        for e in n.get("edges") or []:
            if e.get("edge") == route:
                return e.get("to")
        for r in n.get("routes") or []:
            if r.get("edge") == route:
                return r.get("to")
        if route is None:
            raise DefinitionError(
                f"decision step '{nid}' matched none of its rules — add an "
                "'Otherwise' rule so every request has a route")
        raise DefinitionError(
            f"decision step '{nid}' chose route '{route}', which is not connected "
            "to any step")
    if ntype == "gateway":
        target = _pick_edge(n.get("edges"),
                            {**(state.get("data") or {}), "route": state.get("route")})
        if not target:
            raise DefinitionError(
                f"gateway step '{nid}' matched none of its conditions — add a "
                "'default' branch so every request has a route")
        return target
    if ntype == "human":
        edges = n.get("edges") or []
        if edges:
            ctx = {
                "decision": (state.get("decision") or {}).get("decision"),
                "route": state.get("route"),
                "quorum_approved": state.get("quorum_approved"),
            }
            target = _pick_edge(edges, ctx)
            if not target:
                raise DefinitionError(
                    f"step '{nid}' has no branch for decision "
                    f"'{ctx.get('decision')}' — add a 'default' branch")
            return target
        nxt = n.get("next")
        if not nxt:
            raise DefinitionError(f"step '{nid}' has no next step")
        return nxt
    if ntype == "end":
        return None                       # terminal by design
    # Anything else is a step type this engine does not implement — for example
    # gateway_fork / gateway_join, which the PDD schema accepts but nothing here can
    # run. These used to fall through to `return None`, which sends the run to the
    # graph's END where the outcome defaults to "completed": the request was reported
    # as finished successfully while the step never happened.
    raise DefinitionError(
        f"step '{nid}' has the type '{ntype}', which this engine cannot run yet")


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
