#!/usr/bin/env bash
# =============================================================================
# WebSocket Load Test Runner
#
# Starts the Fashion Bot server with LOAD_TEST_MODE=true (mock LLM),
# then runs Locust against the WebSocket endpoint.
#
# Prerequisites:
#   - Redis running (for state cache)
#   - PostgreSQL/Neon accessible (for config + conversation storage)
#   - pip install -r requirements.txt  (in this directory)
#   - pip install -r ../fashion_bot/requirements.txt
#
# Usage:
#   ./run.sh              # headless mode (CI)
#   ./run.sh --web        # web UI mode (interactive)
#   ./run.sh --verify     # just run the async LLM verifier
#   ./run.sh --smoke      # smoke test: 1 connection, 5 messages, pass/fail
#   ./run.sh --infra      # start observability stack only (Grafana + Prometheus + OTel)
#   ./run.sh --infra-down # stop observability stack
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
FASHION_BOT_DIR="$PROJECT_ROOT/fashion_bot"

# ── Parse arguments ──────────────────────────────────────────────────────────
MODE="headless"
if [[ "${1:-}" == "--web" ]]; then
    MODE="web"
elif [[ "${1:-}" == "--verify" ]]; then
    echo "Running async LLM verifier..."
    cd "$SCRIPT_DIR"
    python verify_async_llm.py
    exit $?
elif [[ "${1:-}" == "--smoke" ]]; then
    MODE="smoke"
elif [[ "${1:-}" == "--infra" ]]; then
    echo "Starting observability stack (Grafana + Prometheus + OTel Collector)..."
    cd "$SCRIPT_DIR"
    docker compose up -d
    echo ""
    echo "Observability stack is running:"
    echo "  Grafana:    http://localhost:3000  (admin/admin)"
    echo "  Prometheus: http://localhost:9090"
    echo "  OTel gRPC:  localhost:4317"
    echo ""
    echo "Dashboard auto-provisioned at: Grafana > Load Tests > Fashion Bot - WebSocket Load Test"
    exit 0
elif [[ "${1:-}" == "--infra-down" ]]; then
    echo "Stopping observability stack..."
    cd "$SCRIPT_DIR"
    docker compose down
    exit 0
fi

# ── Load environment ─────────────────────────────────────────────────────────
echo "Loading load test environment..."
set -a
source "$SCRIPT_DIR/.env.loadtest"
# Also load the app's .env for DATABASE_URL, REDIS_URL, etc.
if [[ -f "$FASHION_BOT_DIR/fashion_bot/.env" ]]; then
    source "$FASHION_BOT_DIR/fashion_bot/.env"
fi
# Re-apply load test overrides (they take precedence)
source "$SCRIPT_DIR/.env.loadtest"
set +a

# Ensure load_tests package is importable (parent of load_tests/ dir)
export PYTHONPATH="${PROJECT_ROOT}:${SCRIPT_DIR}:${FASHION_BOT_DIR}:${PYTHONPATH:-}"

# Safety guard: refuse to run without LOAD_TEST_MODE
if [[ "${LOAD_TEST_MODE}" != "true" ]]; then
    echo "ERROR: LOAD_TEST_MODE is not set to 'true'. Aborting."
    echo "       This should not happen if .env.loadtest was sourced correctly."
    exit 1
fi
echo "LOAD_TEST_MODE=$LOAD_TEST_MODE (mock LLM active)"

# ── Create results directory ─────────────────────────────────────────────────
mkdir -p "$SCRIPT_DIR/results"

# ── Start server ─────────────────────────────────────────────────────────────
echo "Starting Fashion Bot server with LOAD_TEST_MODE=true..."
cd "$FASHION_BOT_DIR"
python -m uvicorn fashion_bot.main:app \
    --host 0.0.0.0 \
    --port "${LOAD_TEST_PORT:-8000}" \
    --log-level info \
    --timeout-keep-alive 30 &
SERVER_PID=$!

# Cleanup on exit
cleanup() {
    echo "Stopping server (PID=$SERVER_PID)..."
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
    echo "Done."
}
trap cleanup EXIT

# ── Wait for server readiness ────────────────────────────────────────────────
echo "Waiting for server to be ready..."
for i in $(seq 1 30); do
    if curl -sf "http://localhost:${LOAD_TEST_PORT:-8000}/health" > /dev/null 2>&1; then
        echo "Server is ready (took ${i}s)."
        break
    fi
    if [[ $i -eq 30 ]]; then
        echo "ERROR: Server did not start within 30s"
        exit 1
    fi
    sleep 1
done

# ── Run test ─────────────────────────────────────────────────────────────────
cd "$SCRIPT_DIR"

if [[ "$MODE" == "smoke" ]]; then
    echo "Running smoke test (1 connection, 5 messages)..."
    python smoke_test.py
    exit $?
elif [[ "$MODE" == "web" ]]; then
    echo "Starting Locust web UI on http://localhost:8089 ..."
    locust -f ws_load_test.py
else
    echo "Starting headless Locust load test..."
    echo "  Users: ${LOAD_TEST_USERS:-50}"
    echo "  Spawn rate: ${LOAD_TEST_SPAWN_RATE:-5}/s"
    echo "  Duration: ${LOAD_TEST_DURATION:-5m}"
    echo ""
    locust -f ws_load_test.py \
        --headless \
        -u "${LOAD_TEST_USERS:-50}" \
        -r "${LOAD_TEST_SPAWN_RATE:-5}" \
        --run-time "${LOAD_TEST_DURATION:-5m}" \
        --csv=results/load_test \
        --html=results/report.html
    echo ""
    echo "Results saved to:"
    echo "  CSV:  $SCRIPT_DIR/results/load_test_*.csv"
    echo "  HTML: $SCRIPT_DIR/results/report.html"
fi
