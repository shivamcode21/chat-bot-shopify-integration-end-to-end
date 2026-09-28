#!/bin/bash
set -euo pipefail

: "${MCP_PATH_SECRET:?MCP_PATH_SECRET env var must be set}"
: "${GRAFANA_URL:?GRAFANA_URL env var must be set}"
: "${GRAFANA_SERVICE_ACCOUNT_TOKEN:?GRAFANA_SERVICE_ACCOUNT_TOKEN env var must be set}"

/app/mcp-grafana --transport streamable-http --address 127.0.0.1:10001 --disable-write &
MCP_PID=$!

caddy run --config /etc/caddy/Caddyfile --adapter caddyfile &
CADDY_PID=$!

trap 'kill ${MCP_PID} ${CADDY_PID} 2>/dev/null || true' EXIT INT TERM

wait -n
EXIT_CODE=$?

echo "[entrypoint] one process exited (code=${EXIT_CODE}), shutting down" >&2
exit "${EXIT_CODE}"
