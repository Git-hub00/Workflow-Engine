#!/usr/bin/env bash
# Every test suite in one command. Run from the repo root:
#   bash scripts/run_all_tests.sh
#
# test_api_guards.py skips itself when the API's dependencies (fastapi, sqlalchemy,
# psycopg, pyjwt) are not installed, so this is safe to run anywhere.
set -u
cd "$(dirname "$0")/.." || exit 1

FAILED=0
run() {
  echo
  echo "=============================================================="
  echo "  $1"
  echo "=============================================================="
  shift
  if "$@"; then
    echo "  -> PASSED"
  else
    echo "  -> FAILED"
    FAILED=$((FAILED + 1))
  fi
}

echo "### Compile check (every Python file that changed)"
python3 -m py_compile \
  services/api/app/main.py \
  services/worker/notifier/email_adapter.py \
  services/worker/notifier/mailboxes.py \
  services/worker/graph/pdd_graph.py \
  services/worker/graph/pdd_norm.py \
  services/worker/graph/graph_orchestrator.py \
  services/worker/graph/graph_handlers.py \
  services/worker/graph/graph_runner.py \
  services/worker/graph/graph_activities.py \
  services/worker/activities/invoice_activities.py \
  services/worker/decisions/decision_engine.py \
  scripts/pdd_validation.py \
  && echo "  -> compiles clean" || FAILED=$((FAILED + 1))

run "Tolerant PDD reader (any workflow dialect)"        python3 scripts/test_pdd_norm.py
run "PDD -> LangGraph compiler, durable pause/resume"   python3 scripts/test_pdd_graph.py
run "Step routing + design-time quorum validation"      python3 scripts/test_routing.py
run "Inbound email reply parsing"                       python3 scripts/test_email_reply.py
run "API access guards + SQL scope binding"             python3 scripts/test_api_guards.py
run "Transaction journey highlighting"                  node scripts/test_journey.mjs

echo
echo "=============================================================="
if [ "$FAILED" -eq 0 ]; then
  echo "  ALL SUITES PASSED"
else
  echo "  $FAILED SUITE(S) FAILED"
fi
echo "=============================================================="
exit "$FAILED"
