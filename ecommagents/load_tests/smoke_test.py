#!/usr/bin/env python3
"""
Smoke test: single WebSocket connection, 5 messages, verify everything works.

Run this before the full load test to catch config/connection issues early.

Usage:
    python smoke_test.py
    python smoke_test.py --messages 10
    python smoke_test.py --host localhost --port 8000

Exit code 0 = all passed, 1 = failures.
"""
import argparse
import json
import os
import sys
import time
from urllib.parse import quote

# Safety guard: refuse to run without LOAD_TEST_MODE
if os.environ.get("LOAD_TEST_MODE", "").lower() not in ("true", "1", "yes"):
    print("ERROR: LOAD_TEST_MODE is not set. Refusing to run.")
    print("       Source .env.loadtest first: set -a && source .env.loadtest && set +a")
    sys.exit(1)

import websocket

from fashion_bot.env_loader import get_env, get_int

# Defaults from env (same as load test config)
DEFAULT_HOST = get_env("LOAD_TEST_HOST", "localhost")
DEFAULT_PORT = get_int("LOAD_TEST_PORT", 8000)
DEFAULT_CLIENT = get_env("LOAD_TEST_CLIENT_NAME", "Concept Groove")

SMOKE_MESSAGES = [
    "hello",
    "show me denim jackets",
    "what is your return policy?",
    "where is my order?",
    "thanks bye",
]


def smoke_test(host: str, port: int, client_name: str, messages: list[str]) -> bool:
    url = f"ws://{host}:{port}/ws/chat/{quote(client_name, safe='')}/smoke-test-session"
    total = len(messages)
    passed = 0
    failed = 0
    latencies = []

    print(f"\n{'='*60}")
    print(f"  SMOKE TEST: {total} messages to {url}")
    print(f"{'='*60}\n")

    # ── Connect ──────────────────────────────────────────────────
    print("[1/3] Connecting...", end=" ")
    t0 = time.perf_counter()
    try:
        ws = websocket.create_connection(url, timeout=10)
        welcome_raw = ws.recv()
        elapsed = (time.perf_counter() - t0) * 1000
        welcome = json.loads(welcome_raw)
        if welcome.get("type") != "system":
            print(f"FAIL (unexpected welcome type: {welcome.get('type')})")
            return False
        print(f"OK ({elapsed:.0f}ms) - {welcome.get('message', '')}")
    except Exception as e:
        print(f"FAIL ({e})")
        return False

    # ── Register phone (avoids webchat phone gate after 2 exchanges) ──
    print("[2/4] Registering phone...", end=" ")
    try:
        ws.send(json.dumps({"type": "phone_update", "phone": "9000000001"}))
        ws.settimeout(5)
        phone_ack = ws.recv()
        phone_data = json.loads(phone_ack)
        if phone_data.get("type") == "system":
            print(f"OK - {phone_data.get('message', '')}")
        else:
            print(f"OK (got type={phone_data.get('type')})")
    except Exception as e:
        print(f"WARN ({e}) - continuing anyway")

    # ── Heartbeat ────────────────────────────────────────────────
    print("[3/4] Heartbeat ping/pong...", end=" ")
    t0 = time.perf_counter()
    try:
        ws.send(json.dumps({"type": "ping", "timestamp": time.time()}))
        ws.settimeout(5)
        pong_raw = ws.recv()
        elapsed = (time.perf_counter() - t0) * 1000
        pong = json.loads(pong_raw)
        if pong.get("type") == "pong":
            print(f"OK ({elapsed:.0f}ms)")
        else:
            print(f"FAIL (got type={pong.get('type')} instead of pong)")
            failed += 1
    except Exception as e:
        print(f"FAIL ({e})")
        failed += 1

    # ── Messages ─────────────────────────────────────────────────
    print(f"[4/4] Sending {total} messages...\n")

    for i, msg in enumerate(messages, 1):
        label = f"  [{i}/{total}]"
        payload = json.dumps({"type": "message", "message": msg, "streaming": False})

        t0 = time.perf_counter()
        try:
            ws.send(payload)

            # Read responses until we get a "message" or "end" type (skip typing, products, etc.)
            ws.settimeout(30)
            response = None
            while True:
                raw = ws.recv()
                data = json.loads(raw)
                msg_type = data.get("type", "")
                if msg_type in ("message", "end"):
                    response = data
                    break
                if msg_type == "error":
                    response = data
                    break
                # Skip typing, stream, products, queued, etc.

            elapsed = (time.perf_counter() - t0) * 1000
            latencies.append(elapsed)

            if response is None:
                print(f"{label} \"{msg}\" -> FAIL (no response)")
                failed += 1
            elif response.get("type") == "error":
                print(f"{label} \"{msg}\" -> FAIL ({elapsed:.0f}ms) error: {response.get('message', '?')[:80]}")
                failed += 1
            else:
                reply = response.get("message", response.get("full_response", ""))
                preview = (reply[:60] + "...") if len(reply) > 60 else reply
                print(f"{label} \"{msg}\" -> OK ({elapsed:.0f}ms) reply: \"{preview}\"")
                passed += 1

        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            print(f"{label} \"{msg}\" -> FAIL ({elapsed:.0f}ms) {e}")
            failed += 1

    # ── Close ────────────────────────────────────────────────────
    try:
        ws.close()
    except Exception:
        pass

    # ── Summary ──────────────────────────────────────────────────
    avg_ms = sum(latencies) / len(latencies) if latencies else 0
    p95_ms = sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0

    print(f"\n{'='*60}")
    print(f"  RESULTS: {passed}/{total} passed, {failed} failed")
    if latencies:
        print(f"  LATENCY: avg={avg_ms:.0f}ms  min={min(latencies):.0f}ms  max={max(latencies):.0f}ms  p95={p95_ms:.0f}ms")
    status = "PASS" if failed == 0 else "FAIL"
    print(f"  STATUS:  {status}")
    print(f"{'='*60}\n")

    return failed == 0


def main():
    parser = argparse.ArgumentParser(description="Smoke test for WebSocket load test")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Server host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Server port (default: {DEFAULT_PORT})")
    parser.add_argument("--client", default=DEFAULT_CLIENT, help=f"Client name (default: {DEFAULT_CLIENT})")
    parser.add_argument("--messages", type=int, default=5, help="Number of messages to send (default: 5)")
    args = parser.parse_args()

    messages = SMOKE_MESSAGES[:args.messages]
    if args.messages > len(SMOKE_MESSAGES):
        # Repeat messages to reach desired count
        while len(messages) < args.messages:
            messages.extend(SMOKE_MESSAGES)
        messages = messages[:args.messages]

    ok = smoke_test(args.host, args.port, args.client, messages)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
