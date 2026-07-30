# services/worker/decisions/decision_engine.py
#
# GENERIC bounded decision engine (Phase 2).
#
# Reads the ROUTES from a PDD `llm_decision` node and picks exactly one. The graph
# edges are the HARD RAIL: routing is decided by deterministic guardrail
# expressions (evaluated safely via `ast`, never eval()), so the model can never
# invent a route. A real LangGraph StateGraph wires the steps
#     START -> assess -> (chosen route marker) -> explain -> END
# which makes the decision a bounded, RENDERABLE graph
# (graph.get_graph().draw_mermaid()); the LLM only writes a one-sentence rationale.
#
# Works for ANY process — invoice is just one set of routes. `config` values and
# transaction `data` fields are exposed as a flat namespace to the route `when`
# expressions (e.g. "amount < autoApproveUnder and not anomaly").
import ast
import operator
import os
from typing import TypedDict

from langgraph.graph import StateGraph, START, END
from langchain_openai import ChatOpenAI


class DecisionState(TypedDict, total=False):
    data: dict
    cfg: dict
    routes: list
    missing: list
    anomalies: list
    anomaly: bool
    has_missing: bool
    route: str
    rationale: str


_CMP = {
    ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
    ast.GtE: operator.ge, ast.Eq: operator.eq, ast.NotEq: operator.ne,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
}


def _ev(node, ns):
    """Evaluate a whitelisted AST node against a namespace. Anything outside the
    allowed grammar raises; _safe_eval turns any failure into False."""
    if isinstance(node, ast.BoolOp):
        vals = [_ev(v, ns) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return not _ev(node.operand, ns)
        if isinstance(node.op, ast.USub):
            return -_ev(node.operand, ns)
    if isinstance(node, ast.Compare):
        left = _ev(node.left, ns)
        for op, comp in zip(node.ops, node.comparators):
            right = _ev(comp, ns)
            fn = _CMP.get(type(op))
            try:
                if fn is None or not fn(left, right):
                    return False
            except TypeError:
                return False
            left = right
        return True
    if isinstance(node, ast.Name):
        return ns.get(node.id)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        return [_ev(e, ns) for e in node.elts]
    raise ValueError(f"unsupported expression node: {type(node).__name__}")


def _safe_eval(expr, ns) -> bool:
    if not expr:
        return False
    try:
        return bool(_ev(ast.parse(expr, mode="eval").body, ns))
    except Exception:
        return False


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _facts(data: dict, cfg: dict):
    """Derived predicates the route conditions reference. Generic: a process that
    doesn't define requiredFields / approvedVendors / anomalyAmountAbove simply
    gets has_missing=False / anomaly=False."""
    required = cfg.get("requiredFields", []) or []
    missing = [f for f in required if not data.get(f)]
    anomalies = []
    approved = cfg.get("approvedVendors")
    if approved is not None and data.get("vendor") not in approved:
        anomalies.append("Vendor not on approved list")
    above = cfg.get("anomalyAmountAbove")
    if above is not None and _num(data.get("amount")) > _num(above):
        anomalies.append("Amount far above typical range")
    return missing, anomalies


def _assess(state: DecisionState) -> dict:
    data = state.get("data", {})
    cfg = state.get("cfg", {})
    routes = state.get("routes", [])
    missing, anomalies = _facts(data, cfg)
    namespace = {
        **{k: v for k, v in cfg.items() if k != "roles"},  # config values (thresholds…)
        **data,                                             # transaction fields (amount, vendor…)
        "has_missing": len(missing) > 0,
        "anomaly": len(anomalies) > 0,
        "missing": missing,
        "default": True,
    }
    route = None
    for r in routes:
        if _safe_eval(r.get("when"), namespace):
            route = r.get("edge")
            break
    # NO silent fallback. Previously this picked the LAST route when nothing
    # matched — which could wrongly auto-approve. A well-built decision always has
    # a 'default' (Otherwise) route, which matches here. If none does, route stays
    # None and the caller fails visibly instead of guessing.
    return {"missing": missing, "anomalies": anomalies,
            "anomaly": len(anomalies) > 0, "has_missing": len(missing) > 0,
            "route": route}


def _explain(state: DecisionState) -> dict:
    data = state.get("data", {})
    route = state.get("route")
    missing = state.get("missing")
    anomalies = state.get("anomalies")
    try:
        llm = ChatOpenAI(
            base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
            api_key="ollama",
            model=os.getenv("LLM_MODEL", "llama3.2:1b"),
            temperature=0,
            timeout=float(os.getenv("LLM_TIMEOUT", "60")),
        )
        prompt = (
            f"A workflow routing decision was made. Data: {data}. "
            f"Missing fields: {missing}. Anomalies: {anomalies}. Chosen route: {route}. "
            "In one sentence, explain why this route is appropriate."
        )
        rationale = llm.invoke(prompt).content
    except Exception:
        rationale = f"Rationale unavailable (LLM error): route {route} chosen by deterministic rules."
    return {"rationale": rationale}


def _passthru(state: DecisionState) -> dict:
    return {}


def build_graph(routes: list):
    """Compile the LangGraph StateGraph for these routes. Renderable via
    build_graph(routes).get_graph().draw_mermaid()."""
    builder = StateGraph(DecisionState)
    builder.add_node("assess", _assess)
    builder.add_node("explain", _explain)
    edges = [r.get("edge") for r in routes if r.get("edge")]
    for edge in edges:
        builder.add_node(edge, _passthru)
    builder.add_edge(START, "assess")
    if edges:
        builder.add_conditional_edges("assess", lambda s: s.get("route"), {e: e for e in edges})
        for edge in edges:
            builder.add_edge(edge, "explain")
    else:
        builder.add_edge("assess", "explain")
    builder.add_edge("explain", END)
    return builder.compile()


def decide(node: dict, data: dict, cfg: dict) -> dict:
    """Run the bounded decision for a PDD llm_decision node. Returns the same
    shape the interpreter/audit expect: route, missing, anomalies, rationale."""
    routes = node.get("routes", [])
    # Pre-flight the deterministic rules. If NOTHING matches there is no route to
    # branch to, and invoking the graph would blow up inside LangGraph's conditional
    # edge with an opaque "unknown node" error — inside a retried activity, which
    # meant the run retried forever. Return the honest result instead and let the
    # engine report a clear "add an Otherwise rule" definition error.
    pre = _assess({"data": data, "cfg": cfg, "routes": routes})
    if pre.get("route") is None:
        return {
            "route": None,
            "missing": pre.get("missing", []),
            "anomalies": pre.get("anomalies", []),
            "rationale": ("No rule on this decision step matched this request, so no "
                          "route could be chosen. Add an 'Otherwise' rule."),
        }
    final = build_graph(routes).invoke({"data": data, "cfg": cfg, "routes": routes})
    return {
        "route": final.get("route"),
        "missing": final.get("missing", []),
        "anomalies": final.get("anomalies", []),
        "rationale": final.get("rationale", ""),
    }
