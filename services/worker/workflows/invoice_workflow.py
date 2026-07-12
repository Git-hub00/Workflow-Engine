# worker/workflows/invoice_workflow.py
#
# WHY this file exists:
# This is the Temporal WORKFLOW that orchestrates the invoice-approval process.
# A workflow is durable, deterministic code: Temporal records every step in
# history and can replay it after a crash, so the process can pause for hours or
# days (waiting on a human) and resume exactly where it left off. All side
# effects (DB writes, notifications, LLM calls) happen in ACTIVITIES; the
# workflow only decides WHAT to do and WHEN.
#
# Flow it drives:
#   started -> extract_fields -> ai_review
#           -> [REQUEST_INFO loop: await human, then re-review]
#           -> AUTO_APPROVE? finish(approved)
#           -> manager decision (reject / return / approve)
#           -> [MANAGER_THEN_FINANCE: finance quorum vote]
#           -> finish(approved | rejected)

from datetime import timedelta

from temporalio import workflow

# WHY imports_passed_through: a Temporal workflow runs inside a determinism
# sandbox that re-imports modules to guard against non-deterministic top-level
# code. Activity code touches the outside world (DB, network) and must NOT be
# re-imported/executed by that sandbox — so we import it inside this block, which
# tells Temporal to pass these modules through unchanged.
#
# The workflow must NOT touch the filesystem at import time: Temporal's
# determinism sandbox forbids calls like pathlib.Path(__file__).resolve(). So
# this file does NO sys.path/pathlib manipulation — the WORKER process
# (services/worker/main.py) is responsible for putting the activities directory
# on sys.path BEFORE it imports this workflow, which is what makes the bare
# `invoice_activities` import below resolve.
with workflow.unsafe.imports_passed_through():
    from invoice_activities import (
        append_event,
        extract_fields,
        ai_review,
        create_human_task,
        notify,
        post_to_erp,
    )


# WHY InvoiceWorkflow: the durable orchestrator for a single invoice. Its state
# (_signal, _votes) lives in workflow history, so it survives worker restarts.
# Humans interact with a running instance by sending SIGNALS (human_decision,
# finance_vote), which is how an external, asynchronous approval is fed back into
# the paused workflow.
@workflow.defn
class InvoiceWorkflow:
    def __init__(self):
        # _signal holds the most recent single-approver decision (manager /
        # request-info). _votes accumulates finance quorum votes keyed by
        # participant. Both are plain instance attributes => part of the
        # deterministic, replayable workflow state.
        self._signal = None
        self._votes = {}

    # WHY human_decision signal: the external world (task-inbox UI / API) calls
    # this to deliver a single approver's decision (approve / reject / return)
    # into the running workflow, unblocking _await_human.
    @workflow.signal
    def human_decision(self, payload: dict):
        self._signal = payload

    # WHY finance_vote signal: for the multi-approver finance stage, each
    # participant's vote arrives as its own signal and is recorded by
    # participant id so the quorum condition can be evaluated.
    @workflow.signal
    def finance_vote(self, payload: dict):
        self._votes[payload["participant"]] = payload["decision"]

    # WHY run(): the entrypoint that encodes the whole invoice lifecycle. It
    # audits "started", extracts fields, then loops on ai_review so a
    # REQUEST_INFO outcome can gather missing data and re-review. Once past that
    # loop it applies the route: auto-approve short-circuits to finish; otherwise
    # a manager decides, and MANAGER_THEN_FINANCE additionally requires a finance
    # quorum before final approval.
    @workflow.run
    async def run(self, txn_id: str, cfg: dict) -> str:
        await workflow.execute_activity(append_event, args=[txn_id,"temporal","WORKFLOW_RUNNING","Temporal","started"],
                                        start_to_close_timeout=timedelta(seconds=30))
        data = await workflow.execute_activity(extract_fields, txn_id, start_to_close_timeout=timedelta(seconds=60))

        while True:
            # ai_review runs on the isolated, low-concurrency "llm-tq" queue so
            # that several invoices processing in parallel do NOT fire many
            # simultaneous calls at the single local model — the dedicated LLM
            # worker throttles concurrency (see worker main.py).
            review = await workflow.execute_activity(ai_review, args=[txn_id, data, cfg],
                                                     task_queue="llm-tq",
                                                     start_to_close_timeout=timedelta(minutes=2))
            if review["route"] == "REQUEST_INFO":
                data = await self._await_human(txn_id, "request_info", cfg["roles"]["review"], cfg, review["missing"])
                continue
            break

        if review["route"] == "AUTO_APPROVE":
            return await self._finish(txn_id, data, "approved")

        decision = await self._await_human(txn_id, "manager", cfg["roles"]["manager"], cfg)
        if decision["decision"] == "reject":
            return await self._finish(txn_id, data, "rejected")
        if decision["decision"] == "return":
            data = await self._await_human(txn_id, "request_info", cfg["roles"]["review"], cfg)

        if review["route"] == "MANAGER_THEN_FINANCE":
            ok = await self._finance_quorum(txn_id, cfg)
            if not ok:
                return await self._finish(txn_id, data, "rejected")

        return await self._finish(txn_id, data, "approved")

    # WHY _await_human: this is the heart of durable execution. It creates the
    # human task and notifies the assignee, then DURABLY PAUSES via
    # workflow.wait_condition until a signal sets self._signal — the workflow can
    # sit here for hours/days (or across worker restarts) consuming no resources,
    # and resume the instant the decision arrives. If the SLA timer fires first,
    # it sends an escalation/reminder and keeps waiting for the decision.
    async def _await_human(self, txn_id, node, role, cfg, need=None):
        policy = {"kind": node, "need": need}
        await workflow.execute_activity(create_human_task, args=[txn_id, node, role, policy],
                                        start_to_close_timeout=timedelta(seconds=30))
        await workflow.execute_activity(notify, args=[txn_id, "email", f"Action needed: {node}"],
                                        start_to_close_timeout=timedelta(seconds=30))
        self._signal = None
        sla = timedelta(hours=cfg["slaHours"])
        if not await workflow.wait_condition(lambda: self._signal is not None, timeout=sla):
            await workflow.execute_activity(notify, args=[txn_id,"email","Reminder / escalation"],
                                            start_to_close_timeout=timedelta(seconds=30))
            await workflow.wait_condition(lambda: self._signal is not None)
        return self._signal

    # WHY _finance_quorum: implements the multi-approver finance gate. It creates
    # one finance task (carrying the quorum config), resets the vote tally, then
    # durably waits until the quorum is decided — either enough approvals (n of
    # the participants) OR, when rejectShortCircuits is set, the first reject ends
    # it immediately. Returns True only if approvals reached the required n.
    async def _finance_quorum(self, txn_id, cfg):
        await workflow.execute_activity(create_human_task,
            args=[txn_id, "finance", cfg["roles"]["finance"], {"kind":"finance", **cfg["quorum"]}],
            start_to_close_timeout=timedelta(seconds=30))
        self._votes = {}
        def decided():
            approvals = sum(1 for v in self._votes.values() if v == "approve")
            rejects = sum(1 for v in self._votes.values() if v == "reject")
            if cfg["rejectShortCircuits"] and rejects: return True
            return approvals >= cfg["quorum"]["n"]
        await workflow.wait_condition(decided)
        return sum(1 for v in self._votes.values() if v == "approve") >= cfg["quorum"]["n"]

    # WHY _finish: the single terminal step. On approval it performs the ERP post
    # side-effect (via the activity); either way it notifies the outcome and
    # returns it as the workflow result, giving every path one consistent exit.
    async def _finish(self, txn_id, data, outcome):
        if outcome == "approved":
            await workflow.execute_activity(post_to_erp, args=[txn_id, data], start_to_close_timeout=timedelta(minutes=1))
        await workflow.execute_activity(notify, args=[txn_id,"email",f"Invoice {outcome}"],
                                        start_to_close_timeout=timedelta(seconds=30))
        return outcome
