# services/worker/graph
#
# Phase A of the "LangGraph on top" migration. This package builds a LangGraph
# graph from ANY Process Definition Document (PDD) — the same definitions the
# author publishes from the Builder — so LangGraph can orchestrate the whole
# flow (not just the bounded decision). It is GENERIC: nothing here is
# invoice-specific; the graph shape comes entirely from the PDD.
#
# This module is ADDITIVE. It does not change or import the existing Temporal
# ProcessInterpreterWorkflow, so the running engine is untouched until a later
# phase flips the entry point.
