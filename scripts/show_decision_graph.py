#!/usr/bin/env python3
"""Print the LangGraph decision graph for a PDD's llm_decision node.

This is the "show me the graph from LangGraph" tool. It builds the compiled graph
and prints it as Mermaid (paste into any mermaid viewer, e.g. mermaid.live) and
as ASCII. draw_mermaid_png() is intentionally not used here because it needs
network access to mermaid.ink; the Mermaid text renders anywhere.

Usage:
    python scripts/show_decision_graph.py [definitions/invoice.pdd.json] [node_id]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services" / "worker" / "decisions"))
from decision_engine import build_graph  # noqa: E402


def main(argv) -> int:
    pdd_path = argv[1] if len(argv) > 1 else "definitions/invoice.pdd.json"
    node_id = argv[2] if len(argv) > 2 else None
    pdd = json.loads(Path(pdd_path).read_text(encoding="utf-8"))

    node = None
    for n in pdd.get("nodes", []):
        if n.get("type") == "llm_decision" and (node_id is None or n.get("id") == node_id):
            node = n
            break
    if node is None:
        print(f"No llm_decision node found in {pdd_path}")
        return 1

    graph = build_graph(node.get("routes", []))
    drawable = graph.get_graph()
    print(f"# Decision graph for node '{node.get('id')}' in {pdd_path}\n")
    print("=== Mermaid (paste into https://mermaid.live) ===")
    print(drawable.draw_mermaid())
    print("\n=== ASCII ===")
    try:
        print(drawable.draw_ascii())
    except Exception as exc:  # draw_ascii needs the optional 'grandalf' package
        print(f"(ascii unavailable: {exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
