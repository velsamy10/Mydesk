"""
Vercel serverless: POST /api/refresh — same as state (fetch fresh snapshot).
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("VERCEL", "1")


def _cfg_from_query(path: str) -> dict:
    qs = parse_qs(urlparse(path).query)
    cfg: dict = {}
    if "range" in qs:
        try:
            cfg["range"] = int(qs["range"][0])
        except ValueError:
            pass
    if "strike" in qs:
        try:
            cfg["strike"] = int(qs["strike"][0])
        except ValueError:
            pass
    if "expiry" in qs and qs["expiry"][0]:
        cfg["expiry"] = qs["expiry"][0]
    return cfg


class handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        self._run()

    def do_GET(self):  # noqa: N802
        # Allow GET refresh for simple clients
        self._run()

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _run(self) -> None:
        try:
            from nifty_monitor import build_snapshot

            snap = build_snapshot(_cfg_from_query(self.path))
            snap["ok"] = True
            body = json.dumps(snap, default=str).encode("utf-8")
            code = 200
        except Exception as exc:  # noqa: BLE001
            body = json.dumps({"ok": False, "error": str(exc), "signal": {"call": "WAIT"}}).encode("utf-8")
            code = 500
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return
