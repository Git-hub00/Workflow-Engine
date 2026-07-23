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
    from graph_runner import advance
    return advance(txn_id, pdd, resume)
