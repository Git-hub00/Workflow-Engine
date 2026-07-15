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
        set_transaction_status,
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
        self._finance_result = None

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
        if payload.get("terminal") is True:
            decision = payload.get("decision")
            if decision in {"approve", "reject"}:
                self._finance_result = decision
            return
        # Defensive: ignore malformed votes (missing participant/decision) instead
        # of raising KeyError, which would poison the workflow on replay. A well-
        # formed vote must carry both a participant id and a decision.
        participant = payload.get("participant")
        decision = payload.get("decision")
        if participant is None or decision is None:
            return
        self._votes[participant] = decision

    # WHY run(): the entrypoint that encodes the whole invoice lifecycle. It
    # audits "started", extracts the invoice fields, then loops on ai_review. The
    # loop is the key structure: EVERY pass re-runs ai_review on the current
    # `data`, so any request-info correction (from the REQUEST_INFO route OR a
    # manager "return") is merged into `data` and re-reviewed/re-routed from
    # scratch — auto-approve, manager-only, or manager-then-finance. Terminal
    # routes return through _finish; only request-info paths loop back.
    @workflow.run
    async def run(self, txn_id: str, cfg: dict) -> str:
        await workflow.execute_activity(append_event, args=[txn_id,"temporal","WORKFLOW_RUNNING","Temporal","started"],
                                        start_to_close_timeout=timedelta(seconds=30))
        # `data` holds the extracted invoice FIELDS and is the single source of
        # truth for the whole run. Human signals NEVER overwrite it; a request-info
        # completion can only MERGE corrected fields into it (see _merge_corrected).
        data = await workflow.execute_activity(extract_fields, txn_id, start_to_close_timeout=timedelta(seconds=60))

        while True:
            # ai_review runs on the isolated, low-concurrency "llm-tq" queue so
            # that several invoices processing in parallel do NOT fire many
            # simultaneous calls at the single local model — the dedicated LLM
            # worker throttles concurrency (see worker main.py).
            review = await workflow.execute_activity(ai_review, args=[txn_id, data, cfg],
                                                     task_queue="llm-tq",
                                                     start_to_close_timeout=timedelta(minutes=2))

            # REQUEST_INFO: gather corrected fields, MERGE them into data, and loop
            # back to the top to re-review the corrected invoice.
            if review["route"] == "REQUEST_INFO":
                sig = await self._await_human(txn_id, "request_info", cfg["roles"]["review"], cfg, review["missing"])
                data = self._merge_corrected(data, sig)
                continue

            if review["route"] == "AUTO_APPROVE":
                return await self._finish(txn_id, data, "approved")

            # MANAGER_ONLY and MANAGER_THEN_FINANCE both require a manager decision.
            decision = await self._await_human(txn_id, "manager", cfg["roles"]["manager"], cfg)
            if decision["decision"] == "reject":
                return await self._finish(txn_id, data, "rejected")
            if decision["decision"] == "return":
                # Send back for correction, MERGE the corrected fields, then loop
                # (continue) to RE-REVIEW — the route is recomputed on the updated
                # invoice instead of falling through to approval on stale data.
                sig = await self._await_human(txn_id, "request_info", cfg["roles"]["review"], cfg)
                data = self._merge_corrected(data, sig)
                continue

            # Manager approved. MANAGER_THEN_FINANCE additionally needs a finance
            # quorum; a failed quorum rejects.
            if review["route"] == "MANAGER_THEN_FINANCE":
                ok = await self._finance_quorum(txn_id, cfg)
                if not ok:
                    return await self._finish(txn_id, data, "rejected")

            return await self._finish(txn_id, data, "approved")

    # WHY _merge_corrected: a request-info completion may carry corrected invoice
    # fields under the signal's "data" key, e.g. {"decision":"resubmit","data":{...}}.
    # We MERGE those over the current invoice fields (never replacing `data` with
    # the raw signal payload) so the corrected invoice — not a decision dict —
    # flows into the next ai_review. A signal with no "data" key leaves the
    # invoice fields unchanged. Pure and deterministic, so it is replay-safe.
    def _merge_corrected(self, data, sig):
        corrected = sig.get("data") if isinstance(sig, dict) else None
        if corrected:
            data = {**data, **corrected}
        return data

    # WHY _await_human: this is the heart of durable execution. It creates the
    # human task and notifies the assignee, then DURABLY PAUSES via
    # workflow.wait_condition until a signal sets self._signal — the workflow can
    # sit here for hours/days (or across worker restarts) consuming no resources,
    # and resume the instant the decision arrives. If the SLA timer fires first,
    # it sends an escalation/reminder and keeps waiting for the decision.
    async def _await_human(self, txn_id, node, role, cfg, need=None):
        self._signal = None
        policy = {"kind": node, "need": need}
        await workflow.execute_activity(create_human_task, args=[txn_id, node, role, policy],
                                        start_to_close_timeout=timedelta(seconds=30))
        if node == "request_info":
            wanted = ", ".join(need) if need else "the missing details"
            message = (
                f"More info needed on your invoice: please provide {wanted}. "
                "Reply to this email (one 'field: value' per line) or use the app."
            )
        else:
            message = f"Action needed: {node}"
        await workflow.execute_activity(notify, args=[txn_id, "email", message],
                                        start_to_close_timeout=timedelta(seconds=30))
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
        self._votes = {}
        self._finance_result = None
        policy = {
            "kind": "finance",
            "quorum": {"n": cfg["quorum"]["n"], "of": cfg["quorum"]["of"]},
            "rejectShortCircuits": cfg["rejectShortCircuits"],
        }
        await workflow.execute_activity(create_human_task,
            args=[txn_id, "finance", cfg["roles"]["finance"], policy],
            start_to_close_timeout=timedelta(seconds=30))
        def decided():
            if self._finance_result is not None:
                return True
            approvals = sum(1 for v in self._votes.values() if v == "approve")
            rejects = sum(1 for v in self._votes.values() if v == "reject")
            if cfg["rejectShortCircuits"] and rejects: return True
            if approvals >= cfg["quorum"]["n"]: return True
            remaining = cfg["quorum"]["of"] - len(self._votes)
            return not cfg["rejectShortCircuits"] and approvals + remaining < cfg["quorum"]["n"]
        await workflow.wait_condition(decided)
        if self._finance_result is not None:
            return self._finance_result == "approve"
        return sum(1 for v in self._votes.values() if v == "approve") >= cfg["quorum"]["n"]

    # WHY _finish: the single terminal step. On approval it performs the ERP post
    # side-effect (via the activity); either way it notifies the outcome, persists
    # the terminal status to the transaction row, and returns the result — giving
    # every path one consistent exit AND keeping transaction.status truthful.
    async def _finish(self, txn_id, data, outcome):
        if outcome == "approved":
            await workflow.execute_activity(post_to_erp, args=[txn_id, data], start_to_close_timeout=timedelta(minutes=1))
        await workflow.execute_activity(notify, args=[txn_id,"email",f"Invoice {outcome}"],
                                        start_to_close_timeout=timedelta(seconds=30))
        # Persist the terminal outcome so the Monitor UI stops showing this run as
        # 'running'. Every terminal path in run() returns through _finish, so this
        # always fires exactly once at the end.
        await workflow.execute_activity(set_transaction_status, args=[txn_id, outcome],
                                        start_to_close_timeout=timedelta(seconds=30))
        return outcome
