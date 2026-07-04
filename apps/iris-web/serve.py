#!/usr/bin/env python3
"""Iris Web — minimal localhost chat UI for Hermes Agent.

A single static page (index.html) plus this tiny stdlib server. No build
step, no npm, no dependencies. The server does two things:

1. Serves index.html on http://127.0.0.1:8643
2. Proxies POST /api/chat to the Hermes API server (default
   http://127.0.0.1:8642/v1/chat/completions), injecting the
   Authorization header server-side so API_SERVER_KEY never reaches
   the browser.

Prerequisite — enable the API server in ~/.hermes/.env and start the
gateway:

    API_SERVER_ENABLED=true
    API_SERVER_KEY=change-me-local-dev

    hermes gateway

Then:

    python3 apps/iris-web/serve.py
    # → http://127.0.0.1:8643

Options:
    --port 8643            UI port
    --host 127.0.0.1       UI bind address (keep it loopback)
    --api http://127.0.0.1:8642   Hermes API server base URL
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).parent
DEFAULT_API = "http://127.0.0.1:8642"


def resolve_api_key() -> str:
    """API_SERVER_KEY from the environment, else from $HERMES_HOME/.env."""
    key = os.getenv("API_SERVER_KEY", "").strip()
    if key:
        return key
    env_file = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))) / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("API_SERVER_KEY="):
                    return line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            pass
    return ""


class IrisWebHandler(BaseHTTPRequestHandler):
    api_base: str = DEFAULT_API
    api_key: str = ""

    def log_message(self, fmt, *args):  # quiet by default
        if os.getenv("IRIS_WEB_DEBUG"):
            sys.stderr.write(fmt % args + "\n")

    # -- Static ------------------------------------------------------------

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                body = (HERE / "index.html").read_bytes()
            except OSError:
                self._json({"error": "index.html not found next to serve.py"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/health":
            self._json(self._health())
        else:
            self._json({"error": "not found"}, 404)

    def _health(self) -> dict:
        parsed = urlparse(self.api_base)
        api_up = False
        try:
            with socket.create_connection(
                (parsed.hostname, parsed.port or 80), timeout=1.5
            ):
                api_up = True
        except OSError:
            pass
        return {"api_up": api_up, "key_set": bool(self.api_key), "api": self.api_base}

    # -- Chat proxy ---------------------------------------------------------

    def do_POST(self):
        if self.path != "/api/chat":
            self._json({"error": "not found"}, 404)
            return
        if not self.api_key:
            self._json(
                {"error": "API_SERVER_KEY not set. Add it to ~/.hermes/.env "
                          "(with API_SERVER_ENABLED=true) and restart."},
                503,
            )
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"

        parsed = urlparse(self.api_base)
        conn_cls = (
            http.client.HTTPSConnection if parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        conn = conn_cls(parsed.hostname, parsed.port, timeout=600)
        try:
            conn.request(
                "POST",
                "/v1/chat/completions",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "text/event-stream, application/json",
                },
            )
            upstream = conn.getresponse()
            self.send_response(upstream.status)
            self.send_header(
                "Content-Type",
                upstream.getheader("Content-Type", "application/json"),
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            while True:
                chunk = upstream.read(512)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break  # browser tab closed or request aborted
        except OSError as e:
            self._json(
                {"error": f"Hermes API server unreachable at {self.api_base} — "
                          f"is 'hermes gateway' running? ({e})"},
                502,
            )
        finally:
            conn.close()

    # -- Helpers -------------------------------------------------------------

    def _json(self, obj: dict, code: int = 200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="Iris Web — minimal chat UI")
    parser.add_argument("--port", type=int, default=8643)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--api", default=os.getenv("IRIS_WEB_API", DEFAULT_API))
    args = parser.parse_args()

    IrisWebHandler.api_base = args.api.rstrip("/")
    IrisWebHandler.api_key = resolve_api_key()

    if not IrisWebHandler.api_key:
        print("⚠  API_SERVER_KEY not found (env or ~/.hermes/.env).")
        print("   The UI will load but chat will fail until it's set.")

    server = ThreadingHTTPServer((args.host, args.port), IrisWebHandler)
    print(f"Iris Web → http://{args.host}:{args.port}  (API: {IrisWebHandler.api_base})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
