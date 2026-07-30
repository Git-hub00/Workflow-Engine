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

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from invoice_activities import append_event, notify, set_transaction_status
    from graph_activities import graph_advance
    from pdd_norm import normalize_pdd, is_quorum

_T_SHORT = timedelta(seconds=30)
_T_ADVANCE = timedelta(minutes=5)   # a single graph hop (may run extract/decision)
# Sending a notification can be SLOW: the AI writes the wording and SMTP may sit
# waiting on a bad recipient domain. With the old 30s limit the activity timed
# out, Temporal retried it forever, the same email went out again and again, and
# the workflow NEVER got past the notify — so an approved task never moved on.
_T_NOTIFY = timedelta(minutes=3)
# And if a notification genuinely cannot be sent, give up after a few tries
# instead of blocking the whole process. The audit event is already written.
_NOTIFY_RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5))


@workflow.defn
class GraphOrchestratorWorkflow:
    def __init__(self):
        self._signal = None
        self._votes = {}
        self._finance_result = None
        self._awaiting = None       # id of the step currently waiting for a person
        self._roles = {}
        self._notifications = []
        self._mailbox = None
        self._process_key = "request"

    @workflow.signal
    def human_decision(self, payload: dict):
        # CORRELATION GUARD. A task row stays 'open' when the workflow abandons its
        # step (e.g. an SLA auto_approve), and Temporal delivers signals at least
        # once — so a decision for an EARLIER step could arrive while a LATER step is
        # waiting and resolve the wrong step. If the sender tells us which step the
        # decision belongs to, ignore it unless it matches the step we are waiting
        # on. Payloads without a node_id are accepted unchanged (older callers).
        node_id = (payload or {}).get("node_id")
        if node_id and self._awaiting and node_id != self._awaiting:
            workflow.logger.warning(
                "ignoring decision for step %r while waiting on %r", node_id, self._awaiting)
            return
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

        self._process_key = norm.get("process_key") or "request"
        # Bound the loop. Every iteration is one human step; a definition that keeps
        # bouncing between the same two steps (a collect-info loop with a rule that
        # never clears) would otherwise grow Temporal history without limit until the
        # workflow is force-terminated. 500 human steps is far beyond any real
        # process, so hitting it means the definition is broken.
        hops = 0
        while status.get("status") == "paused":
            hops += 1
            if hops > 500:
                await self._fail(
                    txn_id,
                    f"stopped after {hops - 1} human steps — the flow is looping "
                    f"through '{status.get('node')}' without ever finishing")
            node = nodes.get(status.get("node"))
            if node is None:
                await self._fail(txn_id, f"paused on unknown step '{status.get('node')}'")
            decision = await self._handle_human(
                txn_id, node, status.get("data") or {}, status.get("missing") or [])
            status = await workflow.execute_activity(
                graph_advance, args=[txn_id, pdd, decision], start_to_close_timeout=_T_ADVANCE)

        outcome = status.get("outcome", "completed")
        await self._notify_completed(txn_id, outcome, status.get("data") or {})
        return outcome

    async def _fail(self, txn_id, reason: str):
        """Record the failure in the database, THEN fail the workflow.

        Failing a Temporal workflow does not touch the database, so without this the
        transaction stayed 'running' forever in the Monitor — a phantom run with no
        task and nothing in the product able to close it."""
        try:
            await workflow.execute_activity(
                append_event,
                args=[txn_id, "engine", "WORKFLOW_FAILED", "LangGraph", reason],
                start_to_close_timeout=_T_SHORT)
            await workflow.execute_activity(
                set_transaction_status, args=[txn_id, "failed"],
                start_to_close_timeout=_T_SHORT)
        except Exception as exc:                # never hide the real reason
            workflow.logger.warning("could not mark transaction failed: %s", exc)
        raise ApplicationError(f"workflow definition error: {reason}",
                               type="DefinitionError", non_retryable=True)

    # ---- human wait (durable) --------------------------------------------
    async def _handle_human(self, txn_id, node, data=None, missing=None):
        if is_quorum(node):
            approved = await self._quorum(txn_id, node, data, missing)
            return {"approved": approved, "decision": "approve" if approved else "reject"}
        return await self._await_human(txn_id, node, data, missing)

    def _email_context(self, node, data, missing):
        """What the email writer needs. A step that asked for missing fields is a
        'request_info' style email (whatever the step is called)."""
        return {
            "kind": "request_info" if missing else "task",
            "process": getattr(self, "_process_key", "request"),
            "step": node.get("id"),
            "data": data or {},
            "missing": missing or [],
        }

    async def _await_human(self, txn_id, node, data=None, missing=None):
        self._awaiting = node.get("id")
        # KEEP a decision that already arrived FOR THIS STEP. The task row is created
        # inside the graph_advance activity, so a fast reply (or an email that lands
        # the instant the task appears) can be signalled BEFORE this method runs.
        # Unconditionally clearing _signal here threw that decision away, and the
        # person then waited for the SLA reminder before anything happened.
        # Anything belonging to a DIFFERENT step is still discarded.
        pending = self._signal
        if not (isinstance(pending, dict)
                and pending.get("node_id") in (None, self._awaiting)):
            self._signal = None
        message, recipient = self._task_notification(node)
        # A step that needs missing fields emails the SUBMITTER (they supply them);
        # a pure approval step emails the assigned role.
        if missing:
            recipient = {"to": "submitter"}
        await workflow.execute_activity(
            notify, args=[txn_id, "email", message, recipient, self._mailbox,
                          self._email_context(node, data, missing)],
            start_to_close_timeout=_T_NOTIFY, retry_policy=_NOTIFY_RETRY)

        sla_hours = node.get("sla_hours") or 48
        on_timeout = node.get("on_timeout") or "remind"
        # NOTE: wait_condition returns None and RAISES asyncio.TimeoutError when the
        # deadline passes. Testing its return value ("if not await ...") was always
        # true, so the deadline branch fired the moment a human answered — sending a
        # bogus "Reminder / escalation" every time, and (with auto_approve /
        # auto_reject configured) even overriding the person's real decision.
        timed_out = False
        try:
            await workflow.wait_condition(lambda: self._signal is not None,
                                          timeout=timedelta(hours=sla_hours))
        except asyncio.TimeoutError:
            timed_out = True

        if timed_out:
            if on_timeout == "auto_approve":
                self._awaiting = None
                return {"decision": "approve", "auto": True}
            if on_timeout == "auto_reject":
                self._awaiting = None
                return {"decision": "reject", "auto": True}
            # Nudge, then keep waiting for a real person (no deadline this time).
            await workflow.execute_activity(
                notify, args=[txn_id, "email", f"Reminder: {node.get('id')} is still waiting",
                              recipient, self._mailbox,
                              {**self._email_context(node, data, missing), "kind": "task"}],
                start_to_close_timeout=_T_NOTIFY, retry_policy=_NOTIFY_RETRY)
            await workflow.wait_condition(lambda: self._signal is not None)
        self._awaiting = None
        return self._signal

    async def _quorum(self, txn_id, node, data=None, missing=None) -> bool:
        self._votes = {}
        self._finance_result = None
        completion = node.get("completion") or {}
        n = completion.get("n")
        of = completion.get("of")
        rsc = completion.get("rejectShortCircuits")

        message, recipient = self._task_notification(node)
        await workflow.execute_activity(
            notify, args=[txn_id, "email", message, recipient, self._mailbox,
                          self._email_context(node, data, missing)],
            start_to_close_timeout=_T_NOTIFY, retry_policy=_NOTIFY_RETRY)

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

        # A multi-approver step gets a deadline and a REMINDER, so it can no longer
        # wait forever when one of the required approvers never votes (the run used to
        # sit at 'running' indefinitely with no nudge and nothing able to rescue it).
        #
        # But a quorum deliberately NEVER auto-decides. The Builder writes
        # `on_timeout` for every approval step, so honouring auto_approve here would
        # have let a "2 of 3" gate approve itself after the deadline with ZERO votes
        # recorded — silently defeating the whole point of requiring a quorum. A
        # missed deadline nudges the approvers and keeps waiting for real votes.
        sla_hours = node.get("sla_hours") or 48
        timed_out = False
        try:
            await workflow.wait_condition(decided, timeout=timedelta(hours=sla_hours))
        except asyncio.TimeoutError:
            timed_out = True

        if timed_out:
            await workflow.execute_activity(
                notify, args=[txn_id, "email",
                              f"Reminder: {node.get('id')} is still waiting for votes",
                              recipient, self._mailbox,
                              {**self._email_context(node, data, missing), "kind": "task"}],
                start_to_close_timeout=_T_NOTIFY, retry_policy=_NOTIFY_RETRY)
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

    async def _notify_completed(self, txn_id, outcome, data=None):
        rule = self._notify_rule(f"completed:{outcome}")
        message = (rule.get("template") if rule else None) or f"Process {outcome}"
        recipient = self._recipient_from_rule(rule) if rule else {"to": "submitter"}
        context = {"kind": "outcome", "process": getattr(self, "_process_key", "request"),
                   "outcome": outcome, "data": data or {}}
        await workflow.execute_activity(
            notify, args=[txn_id, "email", message, recipient, self._mailbox, context],
            start_to_close_timeout=_T_NOTIFY, retry_policy=_NOTIFY_RETRY)
