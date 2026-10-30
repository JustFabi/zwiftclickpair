"""Local HTTP control API for ClickBridge (binds 127.0.0.1 only).

Lets a dashboard — e.g. the Laravel control panel — read bridge status and
change behaviour without touching the console window:

    GET  /api/status       -> JSON snapshot (see Bridge.snapshot)
    POST /api/pairing      body {"enabled": true|false}
    POST /api/disconnect   body {}   (drop the Click session + app sessions)
    GET  /health           -> {"ok": true}

Handlers run on a worker thread; anything that touches the asyncio loop is
marshalled over ``loop.call_soon_threadsafe``.  Uses the stdlib HTTP server,
so there are no extra dependencies.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("control")


class ControlApi:
    """Runs a tiny HTTP control server in a daemon thread."""

    def __init__(self, bridge, port: int):
        self.bridge = bridge
        self.port = int(port or 0)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ life
    def start(self) -> None:
        if self.port <= 0 or self._httpd is not None:
            return

        api = self
        api.bridge.record("control", f"control API starting on port {self.port}")

        class Handler(BaseHTTPRequestHandler):
            def _json(self, code: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path in ("/api/status", "/"):
                    self._json(200, api.bridge.snapshot())
                elif self.path == "/health":
                    self._json(200, {"ok": True})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    self._json(400, {"error": "invalid json"})
                    return
                if self.path == "/api/pairing":
                    enabled = bool(body.get("enabled"))
                    api._submit(api.bridge.set_pairing(enabled))
                    # state is applied on the loop momentarily; acknowledge intent
                    self._json(202, {"pairing_enabled": enabled, "pending": True})
                elif self.path == "/api/disconnect":
                    api._submit(api.bridge.drop_all())
                    self._json(202, {"ok": True, "pending": True})
                else:
                    self._json(404, {"error": "not found"})

            def log_message(self, fmt, *args) -> None:
                log.debug("control api: %s", fmt % args)

        try:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        except OSError as exc:
            log.warning(
                "control API could not bind port %d (%s) -- control disabled",
                self.port, exc,
            )
            self._httpd = None
            return
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="control-api", daemon=True,
        )
        self._thread.start()
        log.info("control API listening on http://127.0.0.1:%d/api/status", self.port)

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2)

    # ------------------------------------------------------------------ glue
    def _submit(self, coro) -> None:
        """Schedule a Bridge coroutine on its asyncio loop from this thread."""
        loop = self.bridge.loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(loop.create_task, coro)
            except RuntimeError:
                pass
        else:
            coro.close()
