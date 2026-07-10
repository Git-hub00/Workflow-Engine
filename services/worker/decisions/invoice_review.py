# worker/decisions/invoice_review.py
#
# WHY this file exists / design contract:
# This is a "bounded" LangGraph decision node for invoice review. The KEY safety
# property is that the ROUTE is chosen ENTIRELY by deterministic Python rules
# (the "hard rail") — the LLM is never allowed to pick or change the route. The
# LLM is used only to produce a human-readable rationale explaining a decision
# that has already been made. If the LLM is unavailable, routing still works and
# is completely unaffected.
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI   # points at Ollama/vLLM (OpenAI-compatible)

ROUTES = ["REQUEST_INFO", "AUTO_APPROVE", "MANAGER_ONLY", "MANAGER_THEN_FINANCE"]

# WHY: review_invoice is the hard rail. It computes missing fields and anomalies,
# then selects exactly one route using deterministic guardrails (checked in a
# fixed priority order). Only AFTER the route is fixed does it ask the LLM for a
# rationale — so the LLM can explain the decision but can never influence it.
def review_invoice(data: dict, cfg: dict) -> dict:
    missing = [f for f in cfg["requiredFields"] if not data.get(f)]
    anomalies = []
    if data["vendor"] not in cfg["approvedVendors"]:
        anomalies.append("Vendor not on approved list")
    if data["amount"] > 20000:
        anomalies.append("Amount far above typical range")

    # Deterministic guardrails first (the "hard rail"); LLM used for nuanced cases.
    if missing:
        route = "REQUEST_INFO"
    elif data["amount"] < cfg["autoApproveUnder"] and not anomalies:
        route = "AUTO_APPROVE"
    elif data["amount"] >= cfg["financeThreshold"] or anomalies:
        route = "MANAGER_THEN_FINANCE"
    else:
        route = "MANAGER_ONLY"

    rationale = _llm_rationale(data, cfg, route, missing, anomalies)  # bounded to explain, not to re-route
    return {"route": route, "missing": missing, "anomalies": anomalies, "rationale": rationale}

# WHY: _llm_rationale is bounded to EXPLAIN the already-chosen route, never to
# choose it. The whole body is wrapped in try/except so that if the LLM call
# fails (e.g. Ollama is down/unreachable) it returns a safe fallback string
# instead of raising — the deterministic route stands on its own.
def _llm_rationale(data, cfg, route, missing, anomalies):
    try:
        llm = ChatOpenAI(base_url="http://localhost:11434/v1", api_key="ollama",
                         model="llama3.1:8b-instruct-q4_K_M", temperature=0)
        prompt = (f"Invoice from {data['vendor']} for ${data['amount']}. "
                  f"Missing: {missing}. Anomalies: {anomalies}. Chosen route: {route}. "
                  f"In one sentence, explain why this route is appropriate.")
        return llm.invoke(prompt).content
    except Exception:
        return f"Rationale unavailable (LLM error): route {route} chosen by deterministic rules."
