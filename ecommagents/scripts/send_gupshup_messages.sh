#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/send_gupshup_messages.sh [webhook_url] [count]
  scripts/send_gupshup_messages.sh [webhook_url] --single [--message "custom text"]

Examples:
  scripts/send_gupshup_messages.sh
  scripts/send_gupshup_messages.sh http://localhost:8000/gupshup/webhook 10
  scripts/send_gupshup_messages.sh --single --message "Hello from Codex"
  scripts/send_gupshup_messages.sh http://localhost:8000/gupshup/webhook --single --message "Need help with my order"
EOF
}

json_escape() {
  local value="$1"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  value=${value//$'\b'/\\b}
  value=${value//$'\f'/\\f}
  printf '%s' "$value"
}

POSITIONAL_ARGS=()
SINGLE_MESSAGE=false
CUSTOM_MESSAGE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --single)
      SINGLE_MESSAGE=true
      shift
      ;;
    --message)
      if [[ $# -lt 2 ]]; then
        echo "--message requires a value" >&2
        exit 1
      fi
      CUSTOM_MESSAGE="$2"
      SINGLE_MESSAGE=true
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      POSITIONAL_ARGS+=("$@")
      break
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      POSITIONAL_ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ "${#POSITIONAL_ARGS[@]}" -gt 2 ]]; then
  echo "Too many positional arguments." >&2
  usage >&2
  exit 1
fi

WEBHOOK_URL="${POSITIONAL_ARGS[0]:-http://localhost:8000/gupshup/webhook}"
COUNT="${POSITIONAL_ARGS[1]:-10}"

APP_NAME="${APP_NAME:-Ecommagents}"
SOURCE="${SOURCE:-15557872987}"
SENDER_PHONE="${SENDER_PHONE:-917503014404}"
SENDER_NAME="${SENDER_NAME:-prabhjot}"
COUNTRY_CODE="${COUNTRY_CODE:-91}"
DIAL_CODE="${DIAL_CODE:-7503014404}"

if [[ "$SINGLE_MESSAGE" == true ]]; then
  COUNT=1
fi

if ! [[ "$COUNT" =~ ^[0-9]+$ ]] || [[ "$COUNT" -le 0 ]]; then
  echo "COUNT must be a positive integer. Got: $COUNT" >&2
  exit 1
fi

base_timestamp_ms="$(( $(date +%s) * 1000 ))"

for i in $(seq 1 "$COUNT"); do
  timestamp_ms="$(( base_timestamp_ms + i ))"
  message_text="${CUSTOM_MESSAGE:-${i}d}"
  escaped_message_text="$(json_escape "${message_text}")"
  wamid="wamid.${timestamp_ms}.${i}.${RANDOM}"

  payload=$(cat <<EOF
{
  "app": "${APP_NAME}",
  "timestamp": ${timestamp_ms},
  "version": 2,
  "type": "message",
  "payload": {
    "id": "${wamid}",
    "source": "${SOURCE}",
    "type": "text",
    "payload": {
      "text": "${escaped_message_text}"
    },
    "sender": {
      "phone": "${SENDER_PHONE}",
      "name": "${SENDER_NAME}",
      "country_code": "${COUNTRY_CODE}",
      "dial_code": "${DIAL_CODE}"
    }
  }
}
EOF
)

  echo "[$i/$COUNT] Sending text='${message_text}' with wamid='${wamid}'"

  curl --silent --show-error --fail --location --request POST "${WEBHOOK_URL}" \
    --header "Content-Type: application/json" \
    --data-raw "${payload}" >/dev/null
done

echo "Sent ${COUNT} messages to ${WEBHOOK_URL}"
