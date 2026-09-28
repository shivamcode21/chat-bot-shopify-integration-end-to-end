#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

TARGET_FILES=(
  "fashion_bot/streamlit_app.py"
  "fashion_bot/fashion_bot/main.py"
  "fashion_bot/fashion_bot/gupshup_webhook.py"
  "fashion_bot/fashion_bot/websocket_chat.py"
  "fashion_bot/fashion_bot/database_manager.py"
  "fashion_bot/fashion_bot/langsmith_config.py"
  "fashion_bot/fashion_bot/core/llm_config.py"
  "fashion_bot/fashion_bot/core/llm_factory.py"
  "fashion_bot/fashion_bot/config_manager.py"
  "fashion_bot/fashion_bot/state_cache.py"
)

ENV_ACCESS_PATTERN='os\.getenv\(|os\.environ\.get\(|os\.environ\['
DOTENV_PATTERN='load_dotenv\('

echo "Checking for direct environment access in runtime files..."

ENV_VIOLATIONS="$(rg -n "$ENV_ACCESS_PATTERN" "${TARGET_FILES[@]}" || true)"
DOTENV_VIOLATIONS="$(rg -n "$DOTENV_PATTERN" "${TARGET_FILES[@]}" || true)"

if [[ -n "$ENV_VIOLATIONS" || -n "$DOTENV_VIOLATIONS" ]]; then
  echo "❌ Environment policy violations found."
  if [[ -n "$ENV_VIOLATIONS" ]]; then
    echo
    echo "Direct os environment reads are not allowed:"
    echo "$ENV_VIOLATIONS"
  fi
  if [[ -n "$DOTENV_VIOLATIONS" ]]; then
    echo
    echo "Direct dotenv loading is not allowed:"
    echo "$DOTENV_VIOLATIONS"
  fi
  echo
  echo "Use fashion_bot.env_loader helpers instead."
  exit 1
fi

echo "✅ Environment policy check passed."

