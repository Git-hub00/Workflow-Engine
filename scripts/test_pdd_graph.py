#!/usr/bin/env python3
# Tests for the generic PDD -> LangGraph compiler.
#   Part 1: pure routing (no third-party deps).
#   Part 2: synchronous end-to-end run of a GENERIC workflow (mock handlers).
#   Part 3: DURABLE run — pause at each human step (interrupt) and resume with a
#           decision, using a checkpointer. This is the "wait for a human" proof.
# (asyncio no longer needed)
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services", "worker", "graph"))

from pdd_graph import compute_next, Handlers  # noqa: E402


def part1_pure_routing():
    assert compute_next({"type": "automated", "next": "x"}, {}) == "x"
    assert compute_next({"type": "llm_decision", "edges": {"A": "n1", "B": "n2"}}, {"route": "B"}) == "n2"
    mgr = {"type": "human_task",
           "edges": [{"when": "decision == 'reject'", "to": "r"}, {"when": "default", "to": "f"}]}
    assert compute_next(mgr, {"decision": {"decision": "reject"}}) == "r"
    assert compute_next(mgr, {"decision": {"decision": "approve"}}) == "f"
    quo = {"type": "human_task", "completion": {"mode": "quorum"},
           "edges": [{"when": "quorum_approved", "to": "f"}, {"when": "default", "to": "r"}]}
    assert compute_next(quo, {"quorum_approved": True}) == "f"
    assert compute_next(quo, {"quorum_approved": False}) == "r"
    assert compute_next({"type": "human_task", "next": "review"}, {}) == "review"
    assert compute_next({"type": "end", "outcome": "approved"}, {}) is None
    print("Part 1 (pure routing): PASS")


PDD = {
    "process_key": "demo", "version": 1,
    "roles": {"clerk": "clerk", "mgr": "mgr", "panel": "panel"},
    "nodes": [
        {"id": "start", "type": "start", "next": "review"},
        {"id": "review", "type": "llm_decision", "routes": [{"edge": "INFO", "when": "x"}],
         "edges": {"AUTO": "finalize", "INFO": "collect", "MGR": "manager"}},
        {"id": "collect", "type": "human_task", "assignment": {"role": "clerk"}, "next": "review"},
        {"id": "manager", "type": "human_task", "assignment": {"role": "mgr"},
         "edges": [{"when": "decision == 'reject'", "to": "rejected"}, {"when": "default", "to": "finance"}]},
        {"id": "finance", "type": "human_task", "assignment": {"role": "panel"},
         "completion": {"mode": "quorum", "n": 2, "of": 3},
         "edges": [{"when": "quorum_approved", "to": "wait"}, {"when": "default", "to": "rejected"}]},
        {"id": "wait", "type": "timer", "timeout": {"seconds": 0}, "next": "finalize"},
        {"id": "finalize", "type": "automated", "action": "post", "next": "approved"},
        {"id": "approved", "type": "end", "outcome": "approved"},
        {"id": "rejected", "type": "end", "outcome": "rejected"},
    ],
}


class MockHandlers(Handlers):
    def __init__(self, manager="approve", quorum=True):
        self.finished, self.waited, self.opened = [], 0, []
        self._manager, self._quorum = manager, quorum

    def run_action(self, txn_id, action, data):
        return {**data, "posted": True}

    def decide(self, node, data, cfg):
        if not data.get("ref"):
            return {"route": "INFO"}
        return {"route": "AUTO"} if float(data.get("amount", 0)) < 500 else {"route": "MGR"}

    def run_human(self, txn_id, node):          # synchronous mode
        return self._decision_for(node["id"])

    def open_human(self, txn_id, node):         # durable mode side-effect
        self.opened.append(node["id"])

    def wait(self, seconds):
        self.waited += 1

    def finish(self, txn_id, data, outcome):
        self.finished.append(outcome)

    def _decision_for(self, nid):
        if nid == "collect":
            return {"ref": "R-100"}
        if nid == "manager":
            return {"decision": self._manager}
        if nid == "finance":
            return {"approved": self._quorum, "decision": "approve" if self._quorum else "reject"}
        return {}


def _run_sync(pdd, handlers, data):
    from pdd_graph import build_process_graph
    graph = build_process_graph(pdd, handlers, durable=False)
    final = graph.invoke({"txn_id": "t1", "data": data})
    return final.get("outcome")


def part2_synchronous():
    try:
        import langgraph  # noqa: F401
    except Exception:
        print("Part 2/3: SKIPPED (langgraph not installed)")
        return False
    assert _run_sync(PDD, MockHandlers(), {"amount": 6000}) == "approved"
    assert _run_sync(PDD, MockHandlers(manager="reject"), {"amount": 6000}) == "rejected"
    assert _run_sync(PDD, MockHandlers(manager="approve", quorum=False), {"amount": 6000}) == "rejected"
    assert _run_sync(PDD, MockHandlers(), {"amount": 100, "ref": "R-1"}) == "approved"
    print("Part 2 (synchronous, 4 paths): PASS")
    return True


def _run_durable(pdd, handlers, data):
    from pdd_graph import build_process_graph
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command
    graph = build_process_graph(pdd, handlers, checkpointer=MemorySaver(), durable=True)
    cfg = {"configurable": {"thread_id": "run-1"}}
    out = graph.invoke({"txn_id": "run-1", "data": data}, cfg)
    for _ in range(30):
        interrupts = out.get("__interrupt__") if isinstance(out, dict) else None
        if not interrupts:
            break
        node_id = interrupts[0].value["node"]                 # which human step is waiting
        decision = handlers._decision_for(node_id)            # the person's reply
        out = graph.invoke(Command(resume=decision), cfg)
    return out.get("outcome")


def part3_durable():
    # High amount: pauses at collect -> resume -> manager -> resume -> finance -> resume -> approved
    h = MockHandlers()
    out = _run_durable(PDD, h, {"amount": 6000})
    assert out == "approved", out
    assert h.opened == ["collect", "manager", "finance"], h.opened
    assert h.waited == 1 and h.finished == ["approved"], (h.waited, h.finished)
    print("Part 3a (durable pause/resume x3 -> approved): PASS")

    assert _run_durable(PDD, MockHandlers(manager="reject"), {"amount": 6000}) == "rejected"
    print("Part 3b (durable, manager reject -> rejected): PASS")

    assert _run_durable(PDD, MockHandlers(quorum=False), {"amount": 6000}) == "rejected"
    print("Part 3c (durable, quorum fails -> rejected): PASS")


if __name__ == "__main__":
    part1_pure_routing()
    if part2_synchronous():
        part3_durable()
    print("ALL GOOD")
