#!/usr/bin/env python3
"""Serve a remote widget host on 127.0.0.1.

Some sandboxes let CLI tools egress but not the test browser. This forwards
every request to the real host so Chromium only ever talks to localhost, while
the responses are genuinely the live service's.

Usage: python3 reverse_proxy.py --target https://host --port 8796
"""
import argparse
import http.server
import socketserver
import requests

TARGET = None
# Keep-alive matters: a fresh TLS handshake per asset pushed the loader past
# its 3s config timeout and made it fall back to an old bundle.
SESSION = requests.Session()
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-encoding",
    "content-length",
}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _relay(self, method):
        url = TARGET + self.path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_BY_HOP and k.lower() != "host"}
        # ngrok's free tier otherwise serves its interstitial instead of the app.
        headers["ngrok-skip-browser-warning"] = "true"
        headers["Accept-Encoding"] = "identity"
        try:
            res = SESSION.request(method, url, data=body, headers=headers,
                                  timeout=60, allow_redirects=False)
            payload, status, out_headers = res.content, res.status_code, res.headers
        except Exception as e:
            payload, status, out_headers = str(e).encode(), 502, {}
        self.send_response(status)
        for key, value in (out_headers.items() if out_headers else []):
            if key.lower() in HOP_BY_HOP:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._relay("GET")

    def do_POST(self):
        self._relay("POST")

    def do_HEAD(self):
        self._relay("HEAD")


def main():
    global TARGET
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--port", type=int, default=8796)
    args = ap.parse_args()
    TARGET = args.target.rstrip("/")
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", args.port), Handler) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
