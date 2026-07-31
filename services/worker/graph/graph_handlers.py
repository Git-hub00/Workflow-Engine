# services/worker/graph/graph_handlers.py
#
# Production handlers for the generic LangGraph engine. They reuse the SAME
# activities as the old engine (extract_fields, post_to_erp, decision_engine,
# create_human_task, set_transaction_status) so behaviour is identical — only
# the orchestrator changed. Generic: nothing invoice-specific.
#
# The graph runs SYNCHRONOUSLY (graph.invoke), so these handlers run the async
# activity bodies with asyncio.run(). Human steps do NOT block here: open_human
# just creates the task; the surrounding Temporal orchestrator owns the wait +
# reminders and resumes the graph with the decision (see graph_orchestrator.py).

import asyncio
import sys
from pathlib import Path

from pdd_graph import Handlers

_WORKER = Path(__file__).resolve().parent.parent
for _p in ("activities", "decisions"):
    _d = str(_WORKER / _p)
    if _d not in sys.path:
        sys.path.insert(0, _d)


def _run(coro):
    """Run an async activity body from sync graph code."""
    if asyncio.iscoroutine(coro):
        return asyncio.run(coro)
    return coro


def _node_role(node):
    """Logical role of a human step. Works for a canonical node (node['role'])
    and for a raw PDD node (assignment.role / role), so either shape is safe."""
    if not isinstance(node, dict):
        return None
    return (node.get("role")
            or (node.get("assignment") or {}).get("role")
            or ((node.get("raw") or {}).get("assignment") or {}).get("role")
            or (node.get("raw") or {}).get("role"))


class RealHandlers(Handlers):
    def __init__(self, pdd: dict):
        self._roles = (pdd or {}).get("roles", {})

    def _role(self, logical):
        return self._roles.get(logical, logical)

    def run_action(self, txn_id, action, data):
        # NOTE the canonical action names. pdd_norm.canon_action() rewrites the
        # PDD's "post_to_erp" / "post" / "post_to_system" to "post_to_record", so
        # matching on "post_to_erp" here NEVER fired: the posting step silently did
        # nothing and no ERP_POSTED event was ever written to the audit trail. Both
        # spellings are accepted now.
        import invoice_activities as A
        if action in ("extract_fields", "extract"):
            # MERGE, never replace. extract reads the stored request details, so a
            # plain overwrite would throw away anything already collected in this run
            # (e.g. fields a vendor supplied after a "more information needed" email)
            # if the flow ever passes through an extract step again. Stored values win
            # for the keys they define; everything already in hand is kept.
            stored = _run(A.extract_fields(txn_id)) or {}
            return {**data, **{k: v for k, v in stored.items() if v not in (None, "")}} or data
        if action in ("post_to_record", "post_to_erp"):
            _run(A.post_to_erp(txn_id, data))
            return data
        if action:
            print(f"run_action: no built-in action named {action!r}; step did nothing")
        return data

    def merge(self, data, decision, txn_id=None):
        """Fold a person's reply into the request's details AND PERSIST them.

        The base implementation merges into the workflow's in-memory state only. That
        state is invisible to the rest of the product: the Task Inbox card, the Monitor
        list and the notification emails all read transaction.data_snapshot. So a
        vendor could reply with the missing PO number and the approver would still see
        an incomplete request. Writing the merged values back to the row is what makes
        the newly supplied information show up everywhere."""
        merged = super().merge(data, decision, txn_id)
        new_values = {k: v for k, v in merged.items() if k not in data or data[k] != v}
        if new_values and txn_id:
            import invoice_activities as A
            try:
                _run(A.save_request_data(txn_id, new_values))
            except Exception as exc:
                # Never lose the run over a bookkeeping write; the values are still in
                # the workflow state, so the flow itself continues correctly.
                print(f"merge: could not persist supplied fields {sorted(new_values)}: {exc}")
        return merged

    def decide(self, node, data, cfg, txn_id=None):
        """Run the bounded decision AND record it in the audit trail.

        Without this the Monitor audit and the Task Inbox showed
        "No LLM_DECISION rationale has been recorded yet", because the LangGraph
        engine called the decision function directly and nothing was written."""
        from decision_engine import decide
        result = decide(node, data, cfg) or {}
        if txn_id:
            import invoice_activities as A
            payload = {
                "route": result.get("route"),
                "missing": result.get("missing", []),
                "anomalies": result.get("anomalies", []),
                "rationale": result.get("rationale", ""),
                "node_id": node.get("id"),
            }
            try:
                _run(A.append_event(
                    txn_id, "llm", "LLM_DECISION", "LangGraph",
                    result.get("rationale") or f"Routed to {result.get('route')}",
                    payload=payload, idempotency_key=None))
            except Exception as exc:      # audit must never break the workflow
                print(f"decide: audit write failed: {exc}")
        return result

    def open_human(self, txn_id, node, missing=None):
        # Create the task row (and, for a quorum, the participant slots). The
        # Temporal orchestrator handles reminders/SLA and the actual wait.
        # `node` is a CANONICAL node (see pdd_norm): the role lives at node['role']
        # and a quorum is detected from completion.n/of on ANY step — never from
        # the step being named "finance".
        import invoice_activities as A
        completion = node.get("completion") or {}
        policy = {"kind": node["id"]}
        # Attach the missing fields (from the decision) so the task shows what to
        # provide and the email reply-parser knows exactly which fields to expect.
        if missing:
            policy["need"] = missing
        if completion.get("mode") == "quorum":
            policy["quorum"] = {"n": completion.get("n"), "of": completion.get("of")}
            policy["rejectShortCircuits"] = bool(completion.get("rejectShortCircuits"))
        _run(A.create_human_task(txn_id, node["id"], self._role(_node_role(node)), policy))

    def finish(self, txn_id, data, outcome):
        import invoice_activities as A
        _run(A.set_transaction_status(txn_id, outcome))
