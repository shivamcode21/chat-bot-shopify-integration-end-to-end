#!/usr/bin/env python3
"""Local stand-in for the widget config API, for running the scenario suite
without a database.

Responses mirror fashion_bot/fashion_bot/widget_config.py exactly, including
the important detail that a disabled feature answers HTTP 200 with
{"enabled": false} rather than an error status.

Usage:  python3 harness.py [--port 8795] [--version v78]
"""
import argparse
import http.server
import json
import os
import socketserver
import urllib.parse

STATIC_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "fashion_bot", "static",
)

# Mirrors the shapes returned by the real handlers.
CONFIG_RESPONSES = {
    "/widget/launcher-theme": {
        "enabled": True,
        "launcher_theme": {"background": "#0f3d2e", "text_color": "#ffffff"},
    },
    "/widget/logo": {"enabled": True, "widget_logo": {"logo_url": ""}},
    "/widget/desktop-widget-text": {"desktop_widget_text": {"isDisplay": True}},
    "/widget/mobile-widget-text": {
        "mobile_widget_text": {"isDisplay": True, "textToDisplay": "Chat with us"}
    },
    "/widget/launcher-hints": {
        "enabled": True,
        "launcher_hints": {"default": ["Branded hint from config"]},
    },
}

# What the real service returns when the feature flags are off (the default).
DISABLED_RESPONSES = {
    "/widget/launcher-theme": {"enabled": False, "launcher_theme": {}},
    "/widget/logo": {"enabled": False, "widget_logo": {}},
    "/widget/desktop-widget-text": {"desktop_widget_text": {}},
    "/widget/mobile-widget-text": {"mobile_widget_text": {}},
    "/widget/launcher-hints": {"enabled": False, "launcher_hints": {}},
}

HOST_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>widget scenario host</title></head>
<body style="margin:0;min-height:100vh;background:#fff">
<h1>Scenario host page</h1>
<script src="/static/chat-widget.%(version)s.js"></script>
<script>FashionBotWidget.init({ clientName: 'scenario', position: 'bottom-right' });</script>
</body></html>"""


def build_handler(version, disabled):
    payloads = DISABLED_RESPONSES if disabled else CONFIG_RESPONSES

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=STATIC_ROOT, **kw)

        def log_message(self, *a):
            pass

        def _json(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path in payloads:
                return self._json(payloads[path])
            if path in ("/test", "/test/"):
                body = (HOST_PAGE % {"version": version}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path.startswith("/static/"):
                self.path = path[len("/static"):]
            return super().do_GET()

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8795)
    ap.add_argument("--version", default="v78")
    ap.add_argument("--disabled", action="store_true",
                    help="answer every config endpoint with the feature-off payload")
    args = ap.parse_args()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(
        ("127.0.0.1", args.port), build_handler(args.version, args.disabled)
    ) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
