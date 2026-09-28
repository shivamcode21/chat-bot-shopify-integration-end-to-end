#!/usr/bin/env python3
"""
Security smoke tests for HTTP middleware, WebSocket policy, widget config, and optional demo backend.

Usage:
  export SECURITY_TEST_API_BASE=http://127.0.0.1:8000
  python scripts/security_smoke_tests.py

  python scripts/security_smoke_tests.py --probe-rate-limit   # after lowering RATE_LIMIT_MAX_REQUESTS on server
  python scripts/security_smoke_tests.py --ws-client-name MyClient --ws-api-key secret --ws-origin https://shop.example.com
  python scripts/security_smoke_tests.py --demo-url http://127.0.0.1:8001 --demo-api-key secret-demo-key

Exit code 0 if all executed checks pass; non-zero otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

logger = logging.getLogger("security_smoke_tests")

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore


def _base_url() -> str:
    raw = (os.environ.get("SECURITY_TEST_API_BASE") or "").strip().rstrip("/")
    return raw


def _http_to_ws(base: str) -> str:
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :]
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :]
    raise ValueError(f"Unsupported API base URL: {base}")


def check_health(client: "httpx.Client", base: str) -> Tuple[bool, str]:
    try:
        r = client.get(f"{base}/health", timeout=30.0)
    except httpx.RequestError as exc:
        return False, f"GET /health failed: {exc}"
    if r.status_code != 200:
        return False, f"GET /health expected 200, got {r.status_code}"
    return True, "GET /health OK"


def check_widget_config(client: "httpx.Client", base: str) -> Tuple[bool, str]:
    try:
        r = client.get(f"{base}/widget/config.json", timeout=30.0)
    except httpx.RequestError as exc:
        return False, f"GET /widget/config.json failed: {exc}"
    if r.status_code != 200:
        return False, f"GET /widget/config.json expected 200, got {r.status_code}"
    try:
        data = r.json()
    except Exception as exc:
        return False, f"GET /widget/config.json invalid JSON: {exc}"
    if not isinstance(data, dict) or not data:
        return False, "GET /widget/config.json empty or not an object"
    return True, "GET /widget/config.json OK"


def check_max_body_413(base: str) -> Tuple[bool, str]:
    """POST /chat with Content-Length > MAX_REQUEST_BYTES (middleware should 413 before body read)."""
    # Use curl so we can set a large Content-Length without uploading 3 MiB.
    chat_url = f"{base}/chat"
    cmd = [
        "curl",
        "-s",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        "-X",
        "POST",
        chat_url,
        "-H",
        "Content-Type: application/json",
        "-H",
        "Content-Length: 3000000",
        "-d",
        "",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30.0)
    except FileNotFoundError:
        return False, "curl not found (install curl or run on a system with curl)"
    except subprocess.TimeoutExpired:
        return False, "curl timed out"
    if result.returncode != 0:
        err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        return False, f"curl failed (exit {result.returncode}){': ' + err if err else ''}"

    out = result.stdout
    code_str = (out or b"").decode("ascii", errors="replace").strip()
    if code_str != "413":
        return (
            False,
            f"POST /chat with oversized Content-Length expected 413, got {code_str!r}",
        )
    return True, "POST /chat oversized Content-Length -> 413 OK"


def check_cors_preflight(client: "httpx.Client", base: str, origin: str) -> Tuple[bool, str]:
    try:
        r = client.request(
            "OPTIONS",
            f"{base}/",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
            },
            timeout=30.0,
        )
    except httpx.RequestError as exc:
        return False, f"OPTIONS / failed: {exc}"
    if r.status_code not in (200, 204, 405):
        return False, f"OPTIONS / unexpected status {r.status_code}"
    acao = r.headers.get("access-control-allow-origin")
    if not acao:
        return (
            True,
            "OPTIONS / OK (no Access-Control-Allow-Origin — CORS may be wildcard or unset)",
        )
    return True, f"OPTIONS / OK (Access-Control-Allow-Origin={acao!r})"


def check_rate_limit_burst(client: "httpx.Client", base: str, attempts: int) -> Tuple[bool, str]:
    codes: List[int] = []
    for _ in range(attempts):
        try:
            r = client.get(f"{base}/", timeout=30.0)
        except httpx.RequestError as exc:
            return False, f"GET / failed during rate-limit probe: {exc}"
        codes.append(r.status_code)
        if r.status_code == 429:
            return True, f"Rate limit: got 429 after {len(codes)} GET / requests"
    if 429 in codes:
        return True, "Rate limit: saw 429"
    return (
        False,
        f"No 429 in {attempts} GET / requests (raise RATE_LIMIT_MAX_REQUESTS or use --probe-rate-limit with tuned server)",
    )


async def _ws_first_message(
    uri: str,
    origin: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[int], Optional[str], Optional[str]]:
    """
    Connect to WS, read first text frame. Returns (json_dict, close_code, close_reason, err).
    """
    if websockets is None:
        return None, None, None, "websockets package not installed"
    connect_kw: Dict[str, Any] = {
        "open_timeout": 15,
        "close_timeout": 5,
        "max_size": None,
    }
    if origin:
        connect_kw["origin"] = origin
    try:
        async with websockets.connect(uri, **connect_kw) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return None, ws.close_code, ws.close_reason, f"first frame not JSON: {raw[:200]!r}"
            return data, ws.close_code, ws.close_reason, None
    except (
        websockets.exceptions.InvalidStatus,
        websockets.exceptions.InvalidStatusCode,
    ) as exc:
        return None, None, None, f"WebSocket HTTP upgrade failed: {exc}"
    except websockets.exceptions.ConnectionClosed as exc:
        return None, exc.code, exc.reason, None
    except Exception as exc:
        return None, None, None, str(exc)


def _is_unauthorized_rejection(
    data: Optional[Dict[str, Any]], code: Optional[int], reason: Optional[str]
) -> bool:
    if code == 1008 and reason and "unauthorized" in reason.lower():
        return True
    if data and data.get("type") == "error":
        msg = str(data.get("message") or "")
        if "unauthorized" in msg.lower() or "api key" in msg.lower():
            return True
    return False


def _is_forbidden_origin_rejection(
    data: Optional[Dict[str, Any]], code: Optional[int], reason: Optional[str]
) -> bool:
    if code == 1008 and reason and "origin" in reason.lower():
        return True
    if data and data.get("type") == "error":
        msg = str(data.get("message") or "")
        if "forbidden" in msg.lower() and "origin" in msg.lower():
            return True
    return False


async def run_ws_matrix(
    base: str,
    client_name: str,
    session_id: str,
    api_key: Optional[str],
    wrong_key: str,
    allowed_origin: Optional[str],
    forbidden_origin: str,
    test_oversized: bool,
    max_ws_bytes: int,
) -> List[Tuple[str, bool, str]]:
    ws_base = _http_to_ws(base)
    results: List[Tuple[str, bool, str]] = []

    def _uri(q: Dict[str, str]) -> str:
        path = f"/ws/chat/{quote(client_name, safe='')}/{quote(session_id, safe='')}"
        qs = urlencode(q)
        return f"{ws_base}{path}?{qs}"

    # 1) No api_key — either welcome (policy off) or unauthorized (policy on)
    uri_no_key = _uri({})
    data, code, reason, err = await _ws_first_message(uri_no_key, allowed_origin)
    if err:
        results.append(("WS_no_api_key", False, err))
    elif data and data.get("type") == "system":
        results.append(("WS_no_api_key", True, "system welcome (REQUIRE_WIDGET_API_KEY may be false)"))
    elif _is_unauthorized_rejection(data, code, reason):
        results.append(("WS_no_api_key", True, "rejected missing key as expected"))
    else:
        results.append(
            ("WS_no_api_key", False, f"unexpected first frame: data={data!r} code={code} reason={reason!r}"),
        )

    if api_key:
        uri_wrong = _uri({"api_key": wrong_key})
        data2, code2, reason2, err2 = await _ws_first_message(uri_wrong, allowed_origin)
        if err2:
            results.append(("WS_wrong_api_key", False, err2))
        elif _is_unauthorized_rejection(data2, code2, reason2):
            results.append(("WS_wrong_api_key", True, "invalid api_key rejected"))
        else:
            results.append(
                ("WS_wrong_api_key", False, f"expected rejection: data={data2!r} code={code2}"),
            )

        uri_good = _uri({"api_key": api_key})
        data3, code3, reason3, err3 = await _ws_first_message(uri_good, allowed_origin)
        if err3:
            results.append(("WS_good_key_and_origin", False, err3))
        elif data3 and data3.get("type") == "system":
            results.append(("WS_good_key_and_origin", True, "system welcome with valid key"))
        else:
            results.append(
                ("WS_good_key_and_origin", False, f"expected system welcome: data={data3!r} code={code3}"),
            )

        uri_forbidden = _uri({"api_key": api_key})
        data4, code4, reason4, err4 = await _ws_first_message(uri_forbidden, forbidden_origin)
        if err4:
            results.append(("WS_forbidden_origin", False, err4))
        elif _is_forbidden_origin_rejection(data4, code4, reason4):
            results.append(("WS_forbidden_origin", True, "forbidden origin rejected"))
        elif data4 and data4.get("type") == "system":
            results.append(
                (
                    "WS_forbidden_origin",
                    True,
                    "system welcome on alternate origin (widget_allowed_origins may be empty)",
                ),
            )
        else:
            results.append(
                ("WS_forbidden_origin", False, f"unexpected: data={data4!r} code={code4}"),
            )

    if test_oversized and api_key and allowed_origin:
        uri_good = _uri({"api_key": api_key})
        connect_kw: Dict[str, Any] = {
            "open_timeout": 15,
            "close_timeout": 5,
            "max_size": None,
            "origin": allowed_origin,
        }
        try:
            async with websockets.connect(uri_good, **connect_kw) as ws:
                await asyncio.wait_for(ws.recv(), timeout=15.0)
                big = "x" * (max_ws_bytes + 1024)
                await ws.send(json.dumps({"type": "message", "message": big}))
                try:
                    nxt = await asyncio.wait_for(ws.recv(), timeout=5.0)
                except Exception:
                    nxt = ""
                results.append(
                    (
                        "WS_oversized_message",
                        True,
                        f"server response after oversized send: {str(nxt)[:240]}",
                    ),
                )
        except Exception as exc:
            results.append(("WS_oversized_message", False, str(exc)))

    return results


def check_demo_missing_key(demo_base: str) -> Tuple[bool, str]:
    try:
        r = httpx.post(
            f"{demo_base.rstrip('/')}/chat",
            json=_demo_min_body(),
            timeout=60.0,
        )
    except httpx.RequestError as exc:
        return False, f"POST /chat failed: {exc}"
    if r.status_code == 401:
        return True, "POST /chat without X-API-Key -> 401 (DEMO_CHAT_X_API_KEY set)"
    if r.status_code == 200:
        return True, "POST /chat without X-API-Key -> 200 (DEMO_CHAT_X_API_KEY unset)"
    return False, f"POST /chat unexpected status {r.status_code}"


def check_demo_valid_key(demo_base: str, api_key: str) -> Tuple[bool, str]:
    try:
        r = httpx.post(
            f"{demo_base.rstrip('/')}/chat",
            json=_demo_min_body(),
            headers={"X-API-Key": api_key},
            timeout=120.0,
        )
    except httpx.RequestError as exc:
        return False, f"POST /chat failed: {exc}"
    if r.status_code != 200:
        return False, f"POST /chat with X-API-Key expected 200, got {r.status_code}: {r.text[:300]}"
    return True, "POST /chat with valid X-API-Key OK"


def check_demo_invalid_key(demo_base: str) -> Tuple[bool, str]:
    try:
        r = httpx.post(
            f"{demo_base.rstrip('/')}/chat",
            json=_demo_min_body(),
            headers={"X-API-Key": "definitely-wrong-key-for-smoke-test"},
            timeout=60.0,
        )
    except httpx.RequestError as exc:
        return False, f"POST /chat failed: {exc}"
    if r.status_code == 401:
        return True, "POST /chat with wrong X-API-Key -> 401"
    if r.status_code == 200:
        return (
            True,
            "POST /chat with wrong key -> 200 (DEMO_CHAT_X_API_KEY unset on server)",
        )
    return False, f"POST /chat with wrong key unexpected status {r.status_code}"


def _demo_min_body() -> Dict[str, Any]:
    return {
        "message": "security smoke test",
        "clientId": "00000000-0000-0000-0000-000000000001",
        "context": {
            "url": "https://test.example.com/",
            "domain": "test.example.com",
        },
        "history": [],
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Security smoke tests")
    parser.add_argument(
        "--api-base",
        default=_base_url(),
        help="API base URL (or SECURITY_TEST_API_BASE)",
    )
    parser.add_argument(
        "--cors-origin",
        default=os.environ.get("SECURITY_TEST_CORS_ORIGIN", "https://app.example.com"),
        help="Origin header for OPTIONS preflight test",
    )
    parser.add_argument(
        "--probe-rate-limit",
        action="store_true",
        help="Send many GET / until 429 (lower RATE_LIMIT_MAX_REQUESTS on server first)",
    )
    parser.add_argument(
        "--rate-limit-attempts",
        type=int,
        default=int(os.environ.get("SECURITY_TEST_RL_ATTEMPTS", "300")),
    )
    parser.add_argument(
        "--ws-client-name",
        default=os.environ.get("SECURITY_TEST_WS_CLIENT_NAME", "").strip() or None,
        help="If set, run WebSocket matrix (needs real client name in DB)",
    )
    parser.add_argument(
        "--ws-session-id",
        default=os.environ.get(
            "SECURITY_TEST_WS_SESSION_ID", "11111111-1111-1111-1111-111111111111"
        ),
    )
    parser.add_argument(
        "--ws-api-key",
        default=os.environ.get("SECURITY_TEST_WS_API_KEY", "").strip() or None,
    )
    parser.add_argument(
        "--ws-wrong-key",
        default="wrong-key-smoke-test",
        help="Deliberately invalid api_key for negative test",
    )
    parser.add_argument(
        "--ws-origin",
        default=os.environ.get("SECURITY_TEST_WS_ORIGIN", "").strip() or None,
        help="Origin header for WS (must match widget_allowed_origins if configured)",
    )
    parser.add_argument(
        "--ws-forbidden-origin",
        default="https://evil.example.com",
        help="Origin for negative embed test",
    )
    parser.add_argument(
        "--ws-test-oversized-message",
        action="store_true",
        help="After connect, send oversized JSON message (uses MAX_WS_MESSAGE_BYTES from server env)",
    )
    parser.add_argument(
        "--max-ws-bytes",
        type=int,
        default=int(os.environ.get("MAX_WS_MESSAGE_BYTES", str(2 * 1024 * 1024))),
        help="Must match server MAX_WS_MESSAGE_BYTES for oversized test",
    )
    parser.add_argument(
        "--demo-url",
        default=os.environ.get("SECURITY_TEST_DEMO_BASE", "").strip() or None,
        help="Demo Chrome extension backend base URL (separate process)",
    )
    parser.add_argument(
        "--demo-api-key",
        default=os.environ.get("DEMO_CHAT_X_API_KEY", "").strip() or None,
        help="Expected X-API-Key when DEMO_CHAT_X_API_KEY is set on demo server",
    )
    args = parser.parse_args()

    if httpx is None:
        logger.error("httpx is required: pip install httpx")
        return 2

    base = (args.api_base or "").strip().rstrip("/")
    if not base:
        logger.error("Set --api-base or SECURITY_TEST_API_BASE")
        return 2

    failed = 0

    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for name, fn in [
            ("health", lambda: check_health(client, base)),
            ("widget_config", lambda: check_widget_config(client, base)),
        ]:
            ok, msg = fn()
            print(f"[{'PASS' if ok else 'FAIL'}] {name}: {msg}")
            if not ok:
                failed += 1

        ok, msg = check_max_body_413(base)
        print(f"[{'PASS' if ok else 'FAIL'}] max_body_413: {msg}")
        if not ok:
            failed += 1

        ok, msg = check_cors_preflight(client, base, args.cors_origin)
        print(f"[{'PASS' if ok else 'FAIL'}] cors_preflight: {msg}")
        if not ok:
            failed += 1

        if args.probe_rate_limit:
            ok, msg = check_rate_limit_burst(client, base, args.rate_limit_attempts)
            print(f"[{'PASS' if ok else 'FAIL'}] rate_limit: {msg}")
            if not ok:
                failed += 1
        else:
            print("[SKIP] rate_limit: pass --probe-rate-limit to exercise 429")

    if args.ws_client_name:
        if websockets is None:
            print("[FAIL] ws_matrix: websockets package not installed")
            failed += 1
        else:
            print(
                f"[INFO] WebSocket matrix: client={args.ws_client_name!r} "
                f"origin={args.ws_origin!r} api_key={'set' if args.ws_api_key else 'missing'}"
            )

            async def _run():
                return await run_ws_matrix(
                    base,
                    args.ws_client_name,
                    args.ws_session_id,
                    args.ws_api_key,
                    args.ws_wrong_key,
                    args.ws_origin,
                    args.ws_forbidden_origin,
                    args.ws_test_oversized_message,
                    args.max_ws_bytes,
                )

            ws_results = asyncio.run(_run())
            for name, ok, msg in ws_results:
                print(f"[{'PASS' if ok else 'FAIL'}] {name}: {msg}")
                if not ok:
                    failed += 1
    else:
        print("[SKIP] WebSocket matrix: pass --ws-client-name (and optional --ws-api-key / --ws-origin)")

    if args.demo_url:
        demo_base = args.demo_url.rstrip("/")
        print(f"[INFO] Demo backend tests: {demo_base}")
        ok, msg = check_demo_missing_key(demo_base)
        print(f"[{'PASS' if ok else 'FAIL'}] demo_missing_key: {msg}")
        if not ok:
            failed += 1
        ok, msg = check_demo_invalid_key(demo_base)
        print(f"[{'PASS' if ok else 'FAIL'}] demo_invalid_key: {msg}")
        if not ok:
            failed += 1
        if args.demo_api_key:
            ok, msg = check_demo_valid_key(demo_base, args.demo_api_key)
            print(f"[{'PASS' if ok else 'FAIL'}] demo_valid_key: {msg}")
            if not ok:
                failed += 1
        else:
            print("[SKIP] demo_valid_key: pass --demo-api-key when server has DEMO_CHAT_X_API_KEY set")
    else:
        print("[SKIP] Demo backend: pass --demo-url")

    print(f"\nDone. Failures: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
