# services/worker/graph/graph_runner.py
#
# Durable runner for the generic LangGraph engine. Compiles the graph for a PDD
# with a checkpointer (so a run can pause at a human step and resume later) and
# exposes two calls used by the orchestrator:
#
#   advance(txn_id, pdd)                 -> run from the start to the first pause/end
#   advance(txn_id, pdd, resume=payload) -> resume a paused run with a decision
#
# Both return a small status dict:
#   {"status": "paused", "node": <id>}          -> waiting for a human
#   {"status": "done",   "outcome": <outcome>}  -> finished
#
# Durability: DATABASE_URL present -> LangGraph Postgres checkpointer (survives
# restarts, weeks-long waits). Otherwise an in-memory saver (dev/test only).

import os
from contextlib import contextmanager

from pdd_graph import build_process_graph
from graph_handlers import RealHandlers


@contextmanager
def _checkpointer():
    url = os.getenv("DATABASE_URL")
    if url:
        # Normalize a SQLAlchemy-style URL (postgresql+psycopg://...) to the plain
        # libpq form psycopg / the saver expects (postgresql://...).
        url = url.replace("+psycopg2", "").replace("+psycopg", "")
        # Postgres saver: durable across restarts. .setup() (idempotent) ensures
        # the checkpoint tables exist.
        from langgraph.checkpoint.postgres import PostgresSaver
        with PostgresSaver.from_conn_string(url) as cp:
            cp.setup()
            yield cp
    else:
        from langgraph.checkpoint.memory import MemorySaver
        yield MemorySaver()


def _status(state) -> dict:
    interrupts = state.get("__interrupt__") if isinstance(state, dict) else None
    if interrupts:
        return {"status": "paused", "node": interrupts[0].value.get("node")}
    return {"status": "done", "outcome": (state or {}).get("outcome", "completed")}


def advance(txn_id: str, pdd: dict, resume=None) -> dict:
    """Run (or resume) the graph until it pauses at a human step or finishes."""
    cfg = {"configurable": {"thread_id": txn_id}}
    with _checkpointer() as cp:
        graph = build_process_graph(pdd, RealHandlers(pdd), checkpointer=cp, durable=True)
        if resume is None:
            out = graph.invoke({"txn_id": txn_id, "pdd": pdd, "data": {}}, cfg)
        else:
            from langgraph.types import Command
            out = graph.invoke(Command(resume=resume), cfg)
    return _status(out)
