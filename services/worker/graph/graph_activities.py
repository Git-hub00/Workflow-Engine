# services/worker/graph/graph_activities.py
#
# The single activity the LangGraph orchestrator calls to move a run forward.
# It is a SYNC activity on purpose: Temporal runs sync activities in a worker
# thread (no running event loop), so the graph handlers can use asyncio.run()
# to invoke the async activity bodies (extract/post/etc.) safely.
import sys
from pathlib import Path

from temporalio import activity

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


@activity.defn(name="graph_advance")
def graph_advance(txn_id: str, pdd: dict, resume: dict | None = None) -> dict:
    """Run/resume the LangGraph run until it pauses at a human step or finishes.
    Returns {"status":"paused","node":id} or {"status":"done","outcome":...}."""
    from temporalio.exceptions import ApplicationError

    from graph_runner import advance
    from pdd_graph import DefinitionError
    try:
        return advance(txn_id, pdd, resume)
    except DefinitionError as exc:
        # The DEFINITION is broken (a dead end, an unmatched condition, a connection
        # to a step that no longer exists). Retrying cannot help — the same PDD will
        # fail identically forever — so fail the run NON-RETRYABLY with a message an
        # author can act on, instead of retrying every few seconds until someone
        # notices the transaction is stuck.
        #
        # Close the transaction row FIRST. Failing the Temporal workflow does not
        # touch the database, so the run would otherwise sit in the Monitor as a
        # phantom 'running' record with no task and no way to close it.
        _mark_failed(txn_id, f"definition error: {exc}")
        raise ApplicationError(f"workflow definition error: {exc}",
                               type="DefinitionError", non_retryable=True) from exc


def _mark_failed(txn_id: str, reason: str) -> None:
    """Best-effort: record the failure so the run stops showing as 'running'."""
    try:
        import asyncio

        import invoice_activities as A
        asyncio.run(A.append_event(txn_id, "engine", "WORKFLOW_FAILED", "LangGraph", reason))
        asyncio.run(A.set_transaction_status(txn_id, "failed"))
    except Exception as exc:                 # never mask the original error
        print(f"could not mark transaction {txn_id} failed: {exc}")
