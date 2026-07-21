# services/worker/workflows/process_interpreter.py
#
# GENERIC Process Interpreter Workflow (Phases 1 + 2 of the configurable-engine refactor).
#
# One durable workflow walks ANY Process Definition Document (PDD) — its nodes and
# edges — mapping each node type to a Temporal primitive. The process SHAPE lives
# in the PDD (data), not in code. Invoice approval is just one PDD.
#
# P2: the bounded decision reads its routes from the PDD (activity ai_review ->
# decision_engine, a real LangGraph graph), and notifications are routed by the
# PDD's `notifications` rules (role / submitter), which fixes the old
# "everything emails one inbox" behavior.
#
# Determinism: NO I/O at import (Temporal re-imports this module). All side
# effects live in ACTIVITIES, imported below via imports_passed_through.
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from invoice_activities import (
        append_event,
        extract_fields,
        ai_review,
        create_human_task,
        notify,
        post_to_erp,
        set_transaction_status,
    )

# Activity timeouts — identical to the original invoice workflow.
_T_SHORT = timedelta(seconds=30)
_T_EXTRACT = timedelta(seconds=60)
_T_DECISION = timedelta(minutes=2)
_T_ERP = timedelta(seconds=60)


@workflow.defn
class ProcessInterpreterWorkflow:
    def __init__(self):
        self._signal = None
        self._votes = {}
        self._finance_result = None
        self._roles = {}
        self._notifications = []

    @workflow.signal
    def human_decision(self, payload: dict):
        self._signal = payload

    @workflow.signal
    def finance_vote(self, payload: dict):
        if payload.get("terminal") is True:
            decision = payload.get("decision")
            if decision in {"approve", "reject"}:
                self._finance_result = decision
            return
        participant = payload.get("participant")
        decision = payload.get("decision")
        if participant is None or decision is None:
            return
        self._votes[participant] = decision

    @workflow.run
    async def run(self, txn_id: str, pdd: dict) -> str:
        cfg = {**pdd.get("config", {}), "roles": pdd.get("roles", {})}
        self._roles = pdd.get("roles", {})
        self._notifications = pdd.get("notifications", [])
        nodes = {n["id"]: n for n in pdd.get("nodes", []) if "id" in n}

        await workflow.execute_activity(
            append_event,
            args=[txn_id, "temporal", "WORKFLOW_RUNNING", "Temporal", "started"],
            start_to_close_timeout=_T_SHORT,
        )

        start_nodes = [n for n in pdd.get("nodes", []) if n.get("type") == "start"]
        if not start_nodes:
            raise ApplicationError("PDD has no start node", non_retryable=True)

        current = start_nodes[0].get("next")
        data = {}
        last_route = None
        pending_need = None

        while current is not None:
            node = nodes.get(current)
            if node is None:
                raise ApplicationError(f"PDD references unknown node '{current}'", non_retryable=True)
            ntype = node.get("type")

            if ntype == "end":
                return await self._finish(txn_id, data, node.get("outcome", "completed"))

            elif ntype == "automated":
                action = node.get("action")
                if action == "extract_fields":
                    data = await workflow.execute_activity(
                        extract_fields, txn_id, start_to_close_timeout=_T_EXTRACT)
                elif action == "post_to_erp":
                    await workflow.execute_activity(
                        post_to_erp, args=[txn_id, data], start_to_close_timeout=_T_ERP)
                else:
                    await workflow.execute_activity(
                        append_event,
                        args=[txn_id, "temporal", "ACTION_SKIPPED", "interpreter",
                              f"unknown action '{action}'"],
                        start_to_close_timeout=_T_SHORT)
                current = node.get("next")

            elif ntype == "llm_decision":
                # Pass the node so the decision engine reads THIS node's routes.
                result = await workflow.execute_activity(
                    ai_review, args=[txn_id, data, cfg, node],
                    task_queue="llm-tq", start_to_close_timeout=_T_DECISION)
                last_route = result.get("route")
                nxt = (node.get("edges") or {}).get(last_route)
                if nxt is None:
                    raise ApplicationError(
                        f"decision node '{node.get('id')}' has no edge for route '{last_route}'",
                        non_retryable=True)
                pending_need = result.get("missing") if last_route == "REQUEST_INFO" else None
                current = nxt

            elif ntype == "human_task":
                completion = node.get("completion") or {}
                if completion.get("mode") == "quorum":
                    approved = await self._quorum(txn_id, node, cfg)
                    current = self._pick_edge(node.get("edges", []),
                                              {"quorum_approved": approved, "route": last_route})
                else:
                    role = self._roles.get((node.get("assignment") or {}).get("role"))
                    need = pending_need if node.get("id") == "request_info" else None
                    sig = await self._await_human(txn_id, node, role, cfg, need)
                    pending_need = None
                    if node.get("edges"):
                        current = self._pick_edge(
                            node["edges"], {"decision": (sig or {}).get("decision"), "route": last_route})
                    else:
                        data = self._merge_corrected(data, sig)
                        current = node.get("next")

            elif ntype == "gateway_exclusive":
                current = self._pick_edge(node.get("edges", []),
                                          {**(data or {}), "route": last_route})

            elif ntype == "timer":
                secs = float((node.get("timeout") or {}).get("seconds", 0))
                if secs > 0:
                    await workflow.sleep(timedelta(seconds=secs))
                current = node.get("next")

            else:
                raise ApplicationError(f"unsupported node type '{ntype}'", non_retryable=True)

        return await self._finish(txn_id, data, "completed")

    # ---- edge / condition helpers (pure) ----------------------------------

    @staticmethod
    def _match(when, ctx) -> bool:
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

    def _pick_edge(self, edges, ctx):
        for edge in edges or []:
            if self._match(edge.get("when"), ctx):
                return edge.get("to")
        return None

    def _merge_corrected(self, data, sig):
        corrected = sig.get("data") if isinstance(sig, dict) else None
        return {**data, **corrected} if corrected else data

    # ---- notification helpers (PDD-driven) --------------------------------

    def _notify_rule(self, event):
        for rule in self._notifications:
            if rule.get("on") == event:
                return rule
        return None

    def _fill(self, template, need):
        filled = template or ""
        if "{missing}" in filled:
            filled = filled.replace("{missing}", ", ".join(need) if need else "the missing details")
        return filled

    def _recipient_from_rule(self, rule, node=None):
        # Translate a PDD notification rule into a recipient spec the notify
        # activity resolves to real emails. Logical roles map through the PDD
        # roles map to Keycloak realm roles.
        if rule:
            if rule.get("to_role"):
                return {"to_role": self._roles.get(rule["to_role"])}
            if rule.get("to"):
                return {"to": rule["to"]}
        if node:
            logical = (node.get("assignment") or {}).get("role")
            if logical:
                return {"to_role": self._roles.get(logical)}
        return None

    def _task_notification(self, node, need):
        node_id = node["id"]
        rule = self._notify_rule(f"task_created:{node_id}")
        if rule:
            return self._fill(rule.get("template", f"Action needed: {node_id}"), need), \
                   self._recipient_from_rule(rule, node)
        # Fallbacks preserve the old wording if a PDD omits a rule.
        if node_id == "request_info":
            wanted = ", ".join(need) if need else "the missing details"
            message = (f"More info needed on your invoice: please provide {wanted}. "
                       "Reply to this email (one 'field: value' per line) or use the app.")
            return message, {"to": "submitter"}
        return f"Action needed: {node_id}", self._recipient_from_rule(None, node)

    # ---- node handlers ----------------------------------------------------

    async def _await_human(self, txn_id, node, role, cfg, need=None):
        self._signal = None
        node_id = node["id"]
        await workflow.execute_activity(
            create_human_task, args=[txn_id, node_id, role, {"kind": node_id, "need": need}],
            start_to_close_timeout=_T_SHORT)

        message, recipient = self._task_notification(node, need)
        await workflow.execute_activity(
            notify, args=[txn_id, "email", message, recipient], start_to_close_timeout=_T_SHORT)

        sla_hours = (node.get("timeout") or {}).get("slaHours") or cfg.get("slaHours") or 48
        if not await workflow.wait_condition(lambda: self._signal is not None,
                                             timeout=timedelta(hours=sla_hours)):
            await workflow.execute_activity(
                notify, args=[txn_id, "email", "Reminder / escalation", recipient],
                start_to_close_timeout=_T_SHORT)
            await workflow.wait_condition(lambda: self._signal is not None)
        return self._signal

    async def _quorum(self, txn_id, node, cfg) -> bool:
        self._votes = {}
        self._finance_result = None
        completion = node.get("completion") or {}
        cfg_quorum = cfg.get("quorum") or {}
        n = completion.get("n", cfg_quorum.get("n"))
        of = completion.get("of", cfg_quorum.get("of"))
        rsc = completion.get("rejectShortCircuits", cfg.get("rejectShortCircuits"))
        role = self._roles.get((node.get("assignment") or {}).get("role"))

        # node id "finance" makes create_human_task pre-create participant tasks.
        await workflow.execute_activity(
            create_human_task,
            args=[txn_id, node["id"], role,
                  {"kind": "finance", "quorum": {"n": n, "of": of}, "rejectShortCircuits": rsc}],
            start_to_close_timeout=_T_SHORT)

        # Notify the approver role that a task awaits (the old flow did not do this;
        # PDD-driven notifications make it correct now).
        message, recipient = self._task_notification(node, None)
        await workflow.execute_activity(
            notify, args=[txn_id, "email", message, recipient], start_to_close_timeout=_T_SHORT)

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

    async def _finish(self, txn_id, data, outcome) -> str:
        rule = self._notify_rule(f"completed:{outcome}")
        if rule:
            message = self._fill(rule.get("template", f"Invoice {outcome}"), None)
            recipient = self._recipient_from_rule(rule)
        else:
            message = f"Invoice {outcome}"
            recipient = {"to": "submitter"}
        # post_to_erp is modeled as the PDD 'finalize' node, already run before an
        # approved 'end'. Here we only notify + persist the terminal status.
        await workflow.execute_activity(
            notify, args=[txn_id, "email", message, recipient], start_to_close_timeout=_T_SHORT)
        await workflow.execute_activity(
            set_transaction_status, args=[txn_id, outcome], start_to_close_timeout=_T_SHORT)
        return outcome
