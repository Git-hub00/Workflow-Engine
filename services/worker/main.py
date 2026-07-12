# worker/main.py
#
# WHY this file exists:
# The workflow and activity files only DEFINE code; nothing actually runs until a
# Temporal WORKER polls a task queue, pulls tasks, and executes that code. This
# module is that runtime — it connects to the Temporal server and starts the
# workers that host our InvoiceWorkflow and its activities.
#
# WHY TWO task queues / two workers:
# We want to process many invoices concurrently WITHOUT overwhelming the single
# local LLM (llama3.2:1b). So we split the work:
#   * "invoice-tq" (main)  — the workflow plus all the cheap DB/notify activities,
#                            run with plenty of concurrency.
#   * "llm-tq"     (LLM)   — ONLY the ai_review activity, run on a separate worker
#                            whose concurrency is deliberately capped, so no more
#                            than 2 LLM calls ever run at once no matter how many
#                            invoices are in flight.
#
# WHY thread pools:
# Our activities do BLOCKING (synchronous SQLAlchemy / psycopg) DB I/O even though
# they are declared `async def`. Running them directly on the asyncio event loop
# would stall it. Giving each Worker a ThreadPoolExecutor as its activity_executor
# offloads those blocking calls to threads, keeping the loop responsive.

import asyncio
import os
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

# Make the activities and workflows importable (they are plain modules under this
# worker/ tree, not installed packages) by adding their dirs to sys.path.
HERE = Path(__file__).resolve().parent
ACTIVITIES_DIR = HERE / "activities"
WORKFLOWS_DIR = HERE / "workflows"
for d in (ACTIVITIES_DIR, WORKFLOWS_DIR):
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))

from invoice_activities import (  # noqa: E402  (import after sys.path tweak)
    append_event,
    extract_fields,
    ai_review,
    create_human_task,
    notify,
    post_to_erp,
)
from invoice_workflow import InvoiceWorkflow  # noqa: E402


async def main():
    # One client/connection is shared by both workers.
    client = await Client.connect("localhost:7233")

    # Worker 1 — the MAIN queue. Hosts the workflow and every activity EXCEPT
    # ai_review. 8 threads so many invoices' DB/notify work runs concurrently.
    worker_main = Worker(
        client,
        task_queue="invoice-tq",
        workflows=[InvoiceWorkflow],
        activities=[append_event, extract_fields, create_human_task, notify, post_to_erp],
        activity_executor=ThreadPoolExecutor(max_workers=8),
    )

    # Worker 2 — the LLM queue. Hosts ONLY ai_review, with tightly limited
    # concurrency so the local 1b model is never asked to serve more than 2
    # requests at once, regardless of how many invoices are running.
    # Default 2 is safe for the local 1b model on limited memory; set
    # LLM_MAX_CONCURRENCY higher (env var, no code change) when moving to a more
    # powerful model/server.
    worker_llm = Worker(
        client,
        task_queue="llm-tq",
        activities=[ai_review],
        activity_executor=ThreadPoolExecutor(max_workers=int(os.getenv("LLM_MAX_CONCURRENCY", "2"))),
        max_concurrent_activities=int(os.getenv("LLM_MAX_CONCURRENCY", "2")),
    )

    print("Workers started: invoice-tq (main) + llm-tq (LLM, max 2 concurrent). Ctrl+C to stop.")

    # Run both workers concurrently; each polls its own queue forever.
    await asyncio.gather(worker_main.run(), worker_llm.run())


if __name__ == "__main__":
    asyncio.run(main())
