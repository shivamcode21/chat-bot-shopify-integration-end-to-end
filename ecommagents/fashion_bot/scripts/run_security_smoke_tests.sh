#!/usr/bin/env bash
# Run security smoke tests (HTTP + optional WS + optional demo).
# Usage:
#   export SECURITY_TEST_API_BASE=http://127.0.0.1:8000
#   ./scripts/run_security_smoke_tests.sh
#   ./scripts/run_security_smoke_tests.sh --probe-rate-limit --ws-client-name groovee

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}:${PYTHONPATH:-}"
exec python3 "${ROOT}/scripts/security_smoke_tests.py" "$@"
