# services/worker/graph/graph_orchestrator.py
#
# "LangGraph on top, Temporal handles the human waits."
#
# This Temporal workflow is a THIN durable spine. It does NOT decide the flow —
# the LangGraph graph does (via the graph_advance activity, which runs the
# generic PDD graph until it pauses at a human step or finishes). Temporal's job
# is only the part it is best at: durably WAITING for a human for days/weeks/
# months, sending reminders, enforcing the deadline (SLA), and collecting quorum
# votes. When a decision is in, it calls graph_advance(resume=...) and LangGraph
# continues. Generic across every workflow — invoice is just one PDD.
#
# The human signals are the SAME names the API already sends (human_decision,
# finance_vote), so the task-inbox / complete-task endpoint works unchanged.

from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from invoice_activities import append_event, notify
    from graph_activities import graph_advance
    from pdd_norm import normalize_pdd, is_quorum

_T_SHORT = timedelta(seconds=30)
_T_ADVANCE = timedelta(minutes=5)   # a single graph hop (may run extract/decision)


@workflow.defn
class GraphOrchestratorWorkflow:
    def __init__(self):
        self._signal = None
        self._votes = {}
        self._finance_result = None

    @workflow.signal
    def human_decision(self, payload: dict):
        self._signal = payload

    @workflow.signal
    def finance_vote(self, payload: dict):
        if payload.get("terminal") is True:
            if payload.get("decision") in {"approve", "reject"}:
                self._finance_result = payload.get("decision")
            return
        p, d = payload.get("participant"), payload.get("decision")
        if p is not None and d is not None:
            self._votes[p] = d

    @workflow.run
    async def run(self, txn_id: str, pdd: dict) -> str:
        # Read the PDD through the tolerant normalizer so ANY dialect works and a
        # quorum is detected from completion.n/of on any step (not by step name).
        norm = normalize_pdd(pdd)
        self._roles = norm["roles"]
        self._notifications = norm["notifications"]
        self._mailbox = norm["mailbox"]
        nodes = norm["nodes_by_id"]

        await workflow.execute_activity(
            append_event, args=[txn_id, "engine", "WORKFLOW_RUNNING", "LangGraph", "started"],
            start_to_close_timeout=_T_SHORT)

        # First hop: run the graph to the first human pause or to the end.
        status = await workflow.execute_activity(
            graph_advance, args=[txn_id, pdd, None], start_to_close_timeout=_T_ADVANCE)

        while status.get("status") == "paused":
            node = nodes.get(status.get("node"))
            if node is None:
                raise ApplicationError(f"paused on unknown node '{status.get('node')}'", non_retryable=True)
            decision = await self._handle_human(txn_id, node)
            status = await workflow.execute_activity(
                graph_advance, args=[txn_id, pdd, decision], start_to_close_timeout=_T_ADVANCE)

        outcome = status.get("outcome", "completed")
        await self._notify_completed(txn_id, outcome)
        return outcome

    # ---- human wait (durable) --------------------------------------------
    async def _handle_human(self, txn_id, node):
        if is_quorum(node):
            approved = await self._quorum(txn_id, node)
            return {"approved": approved, "decision": "approve" if approved else "reject"}
        return await self._await_human(txn_id, node)

    async def _await_human(self, txn_id, node):
        self._signal = None
        message, recipient = self._task_notification(node)
        await workflow.execute_activity(
            notify, args=[txn_id, "email", message, recipient, self._mailbox],
            start_to_close_timeout=_T_SHORT)

        sla_hours = node.get("sla_hours") or 48
        on_timeout = node.get("on_timeout") or "remind"
        if not await workflow.wait_condition(lambda: self._signal is not None,
                                             timeout=timedelta(hours=sla_hours)):
            if on_timeout == "auto_approve":
                return {"decision": "approve", "auto": True}
            if on_timeout == "auto_reject":
                return {"decision": "reject", "auto": True}
            await workflow.execute_activity(
                notify, args=[txn_id, "email", "Reminder / escalation", recipient, self._mailbox],
                start_to_close_timeout=_T_SHORT)
            await workflow.wait_condition(lambda: self._signal is not None)
        return self._signal

    async def _quorum(self, txn_id, node) -> bool:
        self._votes = {}
        self._finance_result = None
        completion = node.get("completion") or {}
        n = completion.get("n")
        of = completion.get("of")
        rsc = completion.get("rejectShortCircuits")

        message, recipient = self._task_notification(node)
        await workflow.execute_activity(
            notify, args=[txn_id, "email", message, recipient, self._mailbox],
            start_to_close_timeout=_T_SHORT)

        def decided():
            if self._finance_result is not None:
                return True
            approvals = sum(1 for v in self._votes.values() if v == "approve")
            rejects = sum(1 for v in self._votes.values() if v == "reject")
            if rsc and rejects:
                return True
            if approvals >= n:
                return True
            remaining = of - len(self._votes)
            return not rsc and approvals + remaining < n

        await workflow.wait_condition(decided)
        if self._finance_result is not None:
            return self._finance_result == "approve"
        return sum(1 for v in self._votes.values() if v == "approve") >= n

    # ---- notifications (PDD-driven; same rules as before) ----------------
    def _notify_rule(self, event):
        for rule in self._notifications:
            if rule.get("on") == event:
                return rule
        return None

    def _recipient_from_rule(self, rule, node=None):
        if rule:
            if rule.get("to_role"):
                return {"to_role": self._roles.get(rule["to_role"])}
            if rule.get("to"):
                return {"to": rule["to"]}
        if node:
            # canonical node -> node['role']; raw node -> assignment.role / role
            logical = (node.get("role")
                       or (node.get("assignment") or {}).get("role")
                       or ((node.get("raw") or {}).get("assignment") or {}).get("role"))
            if logical:
                return {"to_role": self._roles.get(logical, logical)}
        return None

    def _task_notification(self, node):
        rule = self._notify_rule(f"task_created:{node['id']}")
        if rule:
            return rule.get("template", f"Action needed: {node['id']}"), self._recipient_from_rule(rule, node)
        return f"Action needed: {node['id']}", self._recipient_from_rule(None, node)

    async def _notify_completed(self, txn_id, outcome):
        rule = self._notify_rule(f"completed:{outcome}")
        message = (rule.get("template") if rule else None) or f"Process {outcome}"
        recipient = self._recipient_from_rule(rule) if rule else {"to": "submitter"}
        await workflow.execute_activity(
            notify, args=[txn_id, "email", message, recipient, self._mailbox],
            start_to_close_timeout=_T_SHORT)
