"""
Locust WebSocket load test for Fashion Bot.

Uses websocket-client (sync) with Locust's gevent model.
Each virtual user maintains a persistent WebSocket connection and
sends chat messages, measuring end-to-end response latency.

Usage:
    # Headless (CI)
    locust -f ws_load_test.py --headless -u 50 -r 5 --run-time 5m

    # Web UI
    locust -f ws_load_test.py
"""
import json
import logging
import os
import random
import sys
import time
import uuid
from typing import Optional
from urllib.parse import quote

# Safety guard: refuse to run without LOAD_TEST_MODE
if os.environ.get("LOAD_TEST_MODE", "").lower() not in ("true", "1", "yes"):
    print("ERROR: LOAD_TEST_MODE is not set. Refusing to run.")
    print("       Source .env.loadtest first: set -a && source .env.loadtest && set +a")
    sys.exit(1)

import psutil
import gevent
import websocket
from locust import User, task, between, events

from config import (
    HOST,
    PORT,
    WS_URL,
    CLIENT_NAME,
    MESSAGES,
)

logger = logging.getLogger(__name__)

# ── OS Metrics Reporter ────────────────────────────────────────────────────────
# Periodically logs CPU, memory, load avg, and open FDs to Locust's request stream
# so they appear in the web UI charts alongside WS metrics.

_OS_METRICS_INTERVAL = 5  # seconds

def _report_os_metrics(environment):
    """Background greenlet: emit OS-level metrics as Locust custom entries."""
    while True:
        try:
            cpu_pct = psutil.cpu_percent(interval=1)
            mem = psutil.virtual_memory()
            load_1, load_5, load_15 = os.getloadavg()
            proc = psutil.Process()
            open_fds = proc.num_fds() if hasattr(proc, "num_fds") else 0

            # Report as custom Locust request entries (visible in web UI stats table)
            for name, value in [
                ("cpu_percent", cpu_pct),
                ("mem_used_percent", mem.percent),
                ("mem_used_gb", round(mem.used / (1024**3), 2)),
                ("load_avg_1m", round(load_1, 2)),
                ("load_avg_5m", round(load_5, 2)),
                ("open_fds", open_fds),
            ]:
                environment.events.request.fire(
                    request_type="OS_Metric",
                    name=name,
                    response_time=value,
                    response_length=0,
                    exception=None,
                    context={},
                )
        except Exception as e:
            logger.debug(f"OS metrics error: {e}")

        gevent.sleep(_OS_METRICS_INTERVAL)


def _report_server_metrics(environment):
    """Background greenlet: poll server /debug/load_test_metrics and report to Locust.

    Reports deltas (change per interval) for cumulative counters so Locust
    shows meaningful per-interval rates instead of ever-growing totals.
    Gauges (active_connections, msgs_per_sec) are reported as-is.
    """
    import urllib.request
    metrics_url = f"http://{HOST}:{PORT}/debug/load_test_metrics"
    prev_cache = {}
    prev_ws = {}

    while True:
        try:
            with urllib.request.urlopen(metrics_url, timeout=3) as resp:
                data = json.loads(resp.read())

            cache = data.get("cache", {})
            ws = data.get("websocket", {})

            # Cumulative counters → report delta since last poll
            cache_deltas = {}
            for key in ("memory_hits", "redis_hits", "db_hits", "misses"):
                cur = cache.get(key, 0)
                delta = cur - prev_cache.get(key, 0)
                cache_deltas[key] = max(delta, 0)
            prev_cache.update(cache)

            ws_msg_delta = ws.get("total_messages", 0) - prev_ws.get("total_messages", 0)
            ws_err_delta = ws.get("total_errors", 0) - prev_ws.get("total_errors", 0)
            prev_ws.update(ws)

            for name, value in [
                # Cache deltas per interval
                ("cache_memory_hits/interval", cache_deltas["memory_hits"]),
                ("cache_redis_hits/interval", cache_deltas["redis_hits"]),
                ("cache_db_hits/interval", cache_deltas["db_hits"]),
                ("cache_misses/interval", cache_deltas["misses"]),
                # WebSocket gauges
                ("ws_active_connections", ws.get("active_connections", 0)),
                ("ws_messages/interval", max(ws_msg_delta, 0)),
                ("ws_errors/interval", max(ws_err_delta, 0)),
                ("ws_msgs_per_sec", ws.get("messages_per_second", 0)),
            ]:
                environment.events.request.fire(
                    request_type="Server_Metric",
                    name=name,
                    response_time=value,
                    response_length=0,
                    exception=None,
                    context={},
                )
        except Exception as e:
            logger.debug(f"Server metrics poll error: {e}")

        gevent.sleep(_OS_METRICS_INTERVAL)


@events.test_start.add_listener
def _on_test_start(environment, **kwargs):
    """Spawn OS + server metrics reporters when test begins."""
    gevent.spawn(_report_os_metrics, environment)
    gevent.spawn(_report_server_metrics, environment)


class FashionBotUser(User):
    """
    Locust user that connects to the Fashion Bot WebSocket endpoint
    and sends chat messages.
    """
    wait_time = between(1, 3)  # seconds between tasks
    abstract = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ws: Optional[websocket.WebSocket] = None
        self.session_id = str(uuid.uuid4())
        self._connected = False
        self._response_queue = gevent.queue.Queue()

    def on_start(self):
        """Connect WebSocket on user start."""
        url = f"{WS_URL}/{quote(CLIENT_NAME, safe='')}/{self.session_id}"
        start_time = time.perf_counter()
        try:
            self.ws = websocket.create_connection(url, timeout=10)
            # Read welcome message
            welcome = self.ws.recv()
            elapsed_ms = (time.perf_counter() - start_time) * 1000

            welcome_data = json.loads(welcome)
            if welcome_data.get("type") == "system":
                self._connected = True
                self.environment.events.request.fire(
                    request_type="WebSocket",
                    name="connect",
                    response_time=elapsed_ms,
                    response_length=len(welcome),
                    exception=None,
                    context={},
                )
                # Register phone to bypass webchat phone gate
                phone = f"900{random.randint(1000000, 9999999)}"
                self.ws.send(json.dumps({"type": "phone_update", "phone": phone}))
                self.ws.recv()  # consume ack
                # Start background receiver
                self._receiver = gevent.spawn(self._receive_loop)
            else:
                raise Exception(f"Unexpected welcome: {welcome_data.get('type')}")

        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            self.environment.events.request.fire(
                request_type="WebSocket",
                name="connect",
                response_time=elapsed_ms,
                response_length=0,
                exception=e,
                context={},
            )
            logger.error(f"Connection failed: {e}")

    def on_stop(self):
        """Disconnect WebSocket on user stop."""
        self._connected = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

    def _reconnect(self):
        """Attempt to re-establish the WebSocket connection."""
        self._connected = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

        url = f"{WS_URL}/{quote(CLIENT_NAME, safe='')}/{self.session_id}"
        try:
            self.ws = websocket.create_connection(url, timeout=10)
            welcome = self.ws.recv()
            welcome_data = json.loads(welcome)
            if welcome_data.get("type") == "system":
                self._connected = True
                phone = f"900{random.randint(1000000, 9999999)}"
                self.ws.send(json.dumps({"type": "phone_update", "phone": phone}))
                self.ws.recv()
                self._receiver = gevent.spawn(self._receive_loop)
                logger.debug(f"Reconnected session {self.session_id[:8]}")
                return True
        except Exception as e:
            logger.debug(f"Reconnect failed: {e}")
        return False

    def _receive_loop(self):
        """Background greenlet that reads all incoming WS messages."""
        while self._connected and self.ws:
            try:
                self.ws.settimeout(30)
                raw = self.ws.recv()
                if raw:
                    data = json.loads(raw)
                    self._response_queue.put(data)
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as e:
                if self._connected:
                    logger.debug(f"Receive error: {e}")
                break
        # Connection lost — mark disconnected so tasks trigger reconnect
        self._connected = False

    def _wait_for_response(self, timeout: float = 30.0) -> Optional[dict]:
        """
        Wait for the bot's reply message, skipping typing indicators.
        Returns the response dict or None on timeout.
        """
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            remaining = deadline - time.perf_counter()
            try:
                data = self._response_queue.get(timeout=min(remaining, 1.0))
                msg_type = data.get("type", "")
                # Skip typing indicators, system messages, etc.
                if msg_type in ("message", "end"):
                    return data
                if msg_type == "error":
                    return data
                # Keep waiting for other types (typing, stream, products, etc.)
            except gevent.queue.Empty:
                continue
        return None

    @task(3)
    def send_chat_message(self):
        """Send a chat message and measure response time."""
        if not self._connected or not self.ws:
            self._reconnect()
            if not self._connected:
                return

        message = random.choice(MESSAGES)
        payload = json.dumps({
            "type": "message",
            "message": message,
            "streaming": False,
        })

        start_time = time.perf_counter()
        try:
            self.ws.send(payload)
            response = self._wait_for_response(timeout=30.0)
            elapsed_ms = (time.perf_counter() - start_time) * 1000

            if response is None:
                self.environment.events.request.fire(
                    request_type="WebSocket",
                    name="chat_message",
                    response_time=elapsed_ms,
                    response_length=0,
                    exception=Exception("Timeout waiting for response"),
                    context={},
                )
            elif response.get("type") == "error":
                self.environment.events.request.fire(
                    request_type="WebSocket",
                    name="chat_message",
                    response_time=elapsed_ms,
                    response_length=len(json.dumps(response)),
                    exception=Exception(response.get("message", "Unknown error")),
                    context={},
                )
            else:
                self.environment.events.request.fire(
                    request_type="WebSocket",
                    name="chat_message",
                    response_time=elapsed_ms,
                    response_length=len(json.dumps(response)),
                    exception=None,
                    context={},
                )
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            self.environment.events.request.fire(
                request_type="WebSocket",
                name="chat_message",
                response_time=elapsed_ms,
                response_length=0,
                exception=e,
                context={},
            )

    @task(1)
    def send_heartbeat(self):
        """Send a ping and measure pong response time."""
        if not self._connected or not self.ws:
            self._reconnect()
            if not self._connected:
                return

        payload = json.dumps({"type": "ping", "timestamp": time.time()})
        start_time = time.perf_counter()
        try:
            self.ws.send(payload)
            # Pong comes through the response queue
            deadline = time.perf_counter() + 5.0
            while time.perf_counter() < deadline:
                remaining = deadline - time.perf_counter()
                try:
                    data = self._response_queue.get(timeout=min(remaining, 0.5))
                    if data.get("type") == "pong":
                        elapsed_ms = (time.perf_counter() - start_time) * 1000
                        self.environment.events.request.fire(
                            request_type="WebSocket",
                            name="heartbeat",
                            response_time=elapsed_ms,
                            response_length=len(json.dumps(data)),
                            exception=None,
                            context={},
                        )
                        return
                except gevent.queue.Empty:
                    continue

            # Timeout
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            self.environment.events.request.fire(
                request_type="WebSocket",
                name="heartbeat",
                response_time=elapsed_ms,
                response_length=0,
                exception=Exception("Pong timeout"),
                context={},
            )
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            self.environment.events.request.fire(
                request_type="WebSocket",
                name="heartbeat",
                response_time=elapsed_ms,
                response_length=0,
                exception=e,
                context={},
            )
